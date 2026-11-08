"""Offline tests; SSH integration tests bind only 127.0.0.1 with generated keys."""
import copy
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import paramiko
import yaml

logging.getLogger("paramiko").addHandler(logging.NullHandler())
logging.getLogger("paramiko").propagate = False

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("jump_ssh", ROOT / "scripts" / "jump_ssh.py")
jump = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jump)


def config():
    return {"jumpserver": {"host": "jump.example.com", "user": "operator", "auth_mode": "key"},
            "allowed_hosts": [{"name": "dev-1", "ip": "192.0.2.10", "user": "deploy"}]}


def validated(cfg=None, base=None):
    return jump.validate_config(copy.deepcopy(cfg or config()), Path(base or "."))


class ConfigTests(unittest.TestCase):
    def test_key_defaults(self):
        cfg = validated()
        self.assertEqual(cfg["jumpserver"]["auth_mode"], "key")
        self.assertEqual(cfg["jumpserver"]["port"], 2222)
        self.assertEqual(cfg["timeout"], jump.DEFAULT_TIMEOUTS)

    def test_legacy_password_and_auto_key(self):
        cfg = config()
        del cfg["jumpserver"]["auth_mode"]
        self.assertEqual(validated(cfg)["jumpserver"]["auth_mode"], "key")
        cfg["jumpserver"]["password"] = "test-password"
        self.assertEqual(validated(cfg)["jumpserver"]["auth_mode"], "password")

    def test_password_environment(self):
        cfg = config()
        cfg["jumpserver"].update(auth_mode="password", password_env="JUMP_TEST_SECRET")
        with patch.dict(os.environ, {"JUMP_TEST_SECRET": "fixture-only"}):
            self.assertEqual(validated(cfg)["jumpserver"]["password"], "fixture-only")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "unset or empty"):
                validated(cfg)

    def test_conflicting_secret_sources(self):
        cfg = config()
        cfg["jumpserver"].update(auth_mode="password", password="fixture", password_env="TEST")
        with self.assertRaisesRegex(ValueError, "only one"):
            validated(cfg)

    def test_password_required(self):
        cfg = config()
        cfg["jumpserver"]["auth_mode"] = "password"
        with self.assertRaisesRegex(ValueError, "requires"):
            validated(cfg)

    def test_key_mode_cannot_contain_password(self):
        cfg = config()
        cfg["jumpserver"]["password"] = "fixture"
        with self.assertRaisesRegex(ValueError, "must not"):
            validated(cfg)

    def test_relative_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "identity"
            key.write_text("fixture")
            cfg = config()
            cfg["jumpserver"]["identity_file"] = "identity"
            self.assertEqual(validated(cfg, directory)["jumpserver"]["private_key"], str(key.resolve()))

    def test_missing_key(self):
        cfg = config()
        cfg["jumpserver"]["private_key"] = "nonexistent-fixture-key"
        with self.assertRaisesRegex(ValueError, "existing file"):
            validated(cfg)

    def test_no_key_source(self):
        cfg = config()
        cfg["jumpserver"].update(allow_agent=False, look_for_keys=False)
        with self.assertRaisesRegex(ValueError, "requires private_key"):
            validated(cfg)

    def test_empty_config_and_hosts(self):
        for value in (None, [], {}, {"jumpserver": {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                jump.validate_config(value, Path("."))
        cfg = config()
        cfg["allowed_hosts"] = []
        with self.assertRaises(ValueError):
            validated(cfg)

    def test_duplicate_names_and_missing_target_user(self):
        cfg = config()
        cfg["allowed_hosts"].append(dict(cfg["allowed_hosts"][0], name="DEV-1"))
        with self.assertRaisesRegex(ValueError, "unique"):
            validated(cfg)
        cfg = config()
        del cfg["allowed_hosts"][0]["user"]
        with self.assertRaisesRegex(ValueError, "user"):
            validated(cfg)

    def test_invalid_timeouts(self):
        for value in (0, -1, True, "60", float("inf"), float("nan")):
            cfg = config()
            cfg["timeout"] = {"command": value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                validated(cfg)

    def test_invalid_ports(self):
        for port in (0, 65536, True, "2222"):
            cfg = config()
            cfg["jumpserver"]["port"] = port
            with self.subTest(port=port), self.assertRaises(ValueError):
                validated(cfg)

    def test_lookups_are_allowlisted_case_insensitive(self):
        cfg = validated()
        self.assertEqual(jump.lookup_host(cfg, "DEV-1")["user"], "deploy")
        with self.assertRaisesRegex(ValueError, "not in allowed_hosts"):
            jump.lookup_host(cfg, "other")

    def test_workdir_quotes_without_injection(self):
        self.assertEqual(jump.remote_directory("~/my app"), '"$HOME"/\'my app\'')
        self.assertEqual(jump.remote_directory("~"), '"$HOME"')
        self.assertEqual(jump.remote_directory("/opt/a; touch /tmp/x"), "'/opt/a; touch /tmp/x'")
        with self.assertRaises(ValueError):
            jump.remote_directory("relative")

    def test_redacts_auth_secrets(self):
        cfg = {"jumpserver": {"password": "fixture-secret", "key_passphrase": "fixture-passphrase"}}
        self.assertEqual(jump.redact("fixture-secret fixture-passphrase", cfg), "[REDACTED] [REDACTED]")

    def test_yaml_error_does_not_echo_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text('password: "SENSITIVE-FIXTURE\nbad: [', encoding="utf-8")
            with self.assertRaises(ValueError) as error:
                jump.load_config(path)
            self.assertNotIn("SENSITIVE-FIXTURE", str(error.exception))
            self.assertIn("line", str(error.exception))

    def test_example_config_loads(self):
        cfg = jump.load_config(ROOT / "resources/config.example.yaml")
        self.assertEqual(cfg["allowed_hosts"][0]["name"], "dev-1")

    def test_doctor_has_no_network_and_checks_trust(self):
        cfg = validated()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "known_hosts"
            path.write_text("")
            cfg["jumpserver"]["known_hosts"] = str(path)
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
                with self.assertRaisesRegex(ValueError, "No trusted"):
                    jump.doctor(cfg)
                key = paramiko.RSAKey.generate(2048)
                path.write_text(f"[jump.example.com]:2222 {key.get_name()} {key.get_base64()}\n")
                result = jump.doctor(cfg)
                self.assertTrue(result["success"])
                self.assertFalse(result["network_checked"])

    def test_doctor_rejects_non_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-key"
            path.write_text("This is not a private key.")
            cfg = config()
            cfg["jumpserver"]["private_key"] = str(path)
            with self.assertRaisesRegex(ValueError, "Unable to read private_key"):
                jump.doctor(validated(cfg))

    def test_doctor_checks_encrypted_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = paramiko.RSAKey.generate(2048)
            identity, known = root / "identity", root / "known_hosts"
            key.write_private_key_file(str(identity), password="fixture-passphrase")
            known.write_text(f"[jump.example.com]:2222 {key.get_name()} {key.get_base64()}\n")
            cfg = config()
            cfg["jumpserver"].update(private_key=str(identity), known_hosts=str(known))
            with self.assertRaisesRegex(ValueError, "Unable to read private_key"):
                jump.doctor(validated(cfg))
            cfg["jumpserver"]["key_passphrase"] = "fixture-passphrase"
            self.assertTrue(jump.doctor(validated(cfg))["private_key_checked"])


class CommandTests(unittest.TestCase):
    def test_nonzero_result_is_not_success(self):
        session = MagicMock()
        session.exec_command.return_value = {"success": False, "exit_code": 7, "output": "failed"}
        with patch.object(jump, "SSHJumpSession", return_value=session):
            result = jump.execute(validated(), "dev-1", "false")
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 7)
        session.close.assert_called_once()

    def test_workdir_guards_entire_command(self):
        session = MagicMock()
        session.exec_command.return_value = {"success": True, "exit_code": 0}
        with patch.object(jump, "SSHJumpSession", return_value=session):
            jump.execute(validated(), "dev-1", "pwd; whoami", "/missing")
        self.assertEqual(session.exec_command.call_args.args[0], "cd /missing && {\npwd; whoami\n}")

    def test_connect_failure_closes(self):
        session = MagicMock()
        session.connect.side_effect = RuntimeError("failed")
        with patch.object(jump, "SSHJumpSession", return_value=session), self.assertRaises(RuntimeError):
            jump.execute(validated(), "dev-1", "pwd")
        session.close.assert_called_once()

    def test_native_windows_sessions_fail_early(self):
        with patch.object(jump, "session_supported", return_value=False), patch.object(jump, "ensure_server") as spawn:
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                jump.send_session_request(None, validated(), {"action": "start"})
            spawn.assert_not_called()

    def test_session_cleanup_on_initial_workdir_failure(self):
        session = MagicMock()
        session.exec_command.return_value = {"success": False, "exit_code": 1}
        manager = jump.SessionManager(validated())
        with patch.object(jump, "SSHJumpSession", return_value=session), self.assertRaises(RuntimeError):
            manager.dispatch({"action": "start", "host": "dev-1", "workdir": "/missing"})
        self.assertEqual(manager.records, {})
        session.close.assert_called_once()

    def test_session_lifecycle_and_idle_reaping(self):
        session = MagicMock()
        session.is_alive.return_value = True
        manager = jump.SessionManager(validated())
        with patch.object(jump, "SSHJumpSession", return_value=session):
            result = manager.dispatch({"action": "start", "host": "dev-1"})
        self.assertEqual(len(manager.dispatch({"action": "list"})["sessions"]), 1)
        manager.records[result["session_id"]]["last_used"] -= 1000
        manager.reap()
        self.assertEqual(manager.records, {})
        session.close.assert_called_once()

    def test_timeout_closes_session(self):
        session = jump.SSHJumpSession(validated(), config()["allowed_hosts"][0])
        session.channel = MagicMock(closed=False)
        session.client = MagicMock()
        with patch.object(session, "_read_until", side_effect=TimeoutError("test")), self.assertRaises(TimeoutError):
            session.exec_command("sleep 60")
        session.channel.close.assert_called_once()
        session.client.close.assert_called_once()

    def test_channel_request_acknowledgement_has_timeout(self):
        cfg = validated()
        cfg["timeout"]["expect"] = 0.02
        session = jump.SSHJumpSession(cfg, cfg["allowed_hosts"][0])
        closed = threading.Event()
        session.channel = MagicMock()
        session.channel.close.side_effect = closed.set
        with self.assertRaisesRegex(TimeoutError, "setup timed out"):
            session._channel_request(lambda: closed.wait(1))
        self.assertTrue(closed.is_set())

    def test_exit_code_waits_for_complete_network_frame(self):
        session = jump.SSHJumpSession(validated(), config()["allowed_hosts"][0])
        session.channel = MagicMock(closed=False)
        session.channel.recv_ready.return_value = True
        session.channel.recv.side_effect = [b"__JUMP_START_abc__\n\n__JUMP_END_abc__ 1", b"27\r\n"]
        with patch.object(jump.uuid, "uuid4", return_value=MagicMock(hex="abc")):
            result = session.exec_command("missing-command")
        self.assertEqual(result["exit_code"], 127)
        self.assertEqual(session.channel.recv.call_count, 2)

    def test_cli_is_cwd_independent_and_reports_elapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/jump_ssh.py"), "--config", str(ROOT / "resources/config.example.yaml"), "list"], cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["success"])
        self.assertGreaterEqual(payload["elapsed_seconds"], 0)

    def test_cli_invalid_config_is_json_and_nonzero(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/jump_ssh.py"), "--config", str(ROOT / "missing-fixture.yaml"), "doctor"], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["success"])
        self.assertEqual(result.stderr, "")


class LocalSSHServer(paramiko.ServerInterface):
    def __init__(self, root, key_mode=False):
        self.server_key = paramiko.RSAKey.generate(2048)
        self.user_key = paramiko.RSAKey.generate(2048)
        self.key_mode = key_mode
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.shell_ready = threading.Event()
        self.transport = None
        self.commands = []
        self.errors = []
        self.cfg = config()
        self.cfg["jumpserver"].update(host="127.0.0.1", port=self.port, auth_mode="key" if key_mode else "password", allow_agent=False, look_for_keys=False)
        known = root / "known_hosts"
        known.write_text(f"[127.0.0.1]:{self.port} {self.server_key.get_name()} {self.server_key.get_base64()}\n")
        self.cfg["jumpserver"]["known_hosts"] = str(known)
        if key_mode:
            private = root / "identity"
            self.user_key.write_private_key_file(str(private))
            self.cfg["jumpserver"]["private_key"] = str(private)
        else:
            self.cfg["jumpserver"]["password"] = "local-fixture-only"
        self.cfg["timeout"] = {"connect": 2, "expect": 2, "command": 2}
        self.thread = threading.Thread(target=self.run, daemon=True)

    def check_auth_password(self, username, password):
        ok = not self.key_mode and username == "operator@deploy@192.0.2.10" and password == "local-fixture-only"
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        ok = self.key_mode and username == "operator@deploy@192.0.2.10" and key == self.user_key
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey" if self.key_mode else "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_ready.set()
        return True

    def run(self):
        try:
            connection, _ = self.listener.accept()
            self.transport = paramiko.Transport(connection)
            self.transport.add_server_key(self.server_key)
            self.transport.start_server(server=self)
            channel = self.transport.accept(5)
            if channel is None or not self.shell_ready.wait(5):
                return
            channel.sendall("test$ ")
            pending = ""
            while True:
                data = channel.recv(65536)
                if not data:
                    break
                pending += data.decode()
                if not pending.endswith('"$__jump_rc"\n'):
                    continue
                match = re.search(r"'__JUMP_START_' '([a-f0-9]+)__'", pending)
                if not match:
                    raise AssertionError("Missing marker framing")
                token = match.group(1)
                quoted = pending.split("; eval ", 1)[1].rsplit("; __jump_rc=$?;", 1)[0]
                command = shlex.split(quoted)[0]
                self.commands.append(command)
                output, code = ("\nhello\n\n", 0) if command == "fixture-output" else ("", 7 if command == "false" else 0)
                channel.sendall(f"__JUMP_START_{token}__\r\n" + output + f"\r\n__JUMP_END_{token}__ {code}\r\n")
                pending = ""
        except (EOFError, OSError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            if self.transport:
                self.transport.close()
            self.listener.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        if self.transport:
            self.transport.close()
        self.listener.close()
        self.thread.join(timeout=6)
        if self.errors:
            raise AssertionError(self.errors)


class SSHIntegrationTests(unittest.TestCase):
    def test_password_connection_and_output_framing(self):
        with tempfile.TemporaryDirectory() as directory, LocalSSHServer(Path(directory)) as server:
            result = jump.execute(validated(server.cfg), "dev-1", "fixture-output")
            self.assertTrue(result["success"])
            self.assertEqual(result["output"], "\nhello\n\n")
            self.assertEqual(result["exit_code"], 0)

    def test_passwordless_private_key_connection(self):
        with tempfile.TemporaryDirectory() as directory, LocalSSHServer(Path(directory), key_mode=True) as server:
            result = jump.execute(validated(server.cfg), "dev-1", "fixture-output")
            self.assertTrue(result["success"])
            self.assertNotIn("password", server.cfg["jumpserver"])

    def test_remote_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory, LocalSSHServer(Path(directory)) as server:
            result = jump.execute(validated(server.cfg), "dev-1", "false")
            self.assertFalse(result["success"])
            self.assertEqual(result["exit_code"], 7)

    def test_reject_unknown_server_key(self):
        with tempfile.TemporaryDirectory() as directory, LocalSSHServer(Path(directory)) as server:
            Path(server.cfg["jumpserver"]["known_hosts"]).write_text("")
            with self.assertRaises(paramiko.SSHException):
                jump.execute(validated(server.cfg), "dev-1", "fixture-output")
            self.assertEqual(server.commands, [])

    def test_reject_wrong_password(self):
        with tempfile.TemporaryDirectory() as directory, LocalSSHServer(Path(directory)) as server:
            server.cfg["jumpserver"]["password"] = "wrong-fixture"
            with self.assertRaises(paramiko.AuthenticationException):
                jump.execute(validated(server.cfg), "dev-1", "fixture-output")
            self.assertEqual(server.commands, [])


SHELL = os.environ.get("JUMP_SSH_TEST_SH") or shutil.which("bash") or shutil.which("sh")


@unittest.skipUnless(SHELL, "A local POSIX shell is required for wrapper execution tests")
class ShellWrapperTests(unittest.TestCase):
    def setUp(self):
        self.cfg = validated()
        self.session = jump.SSHJumpSession(self.cfg, self.cfg["allowed_hosts"][0])
        self.session.channel = MagicMock(closed=False)

    def real_output(self, pattern, timeout):
        wrapped = self.session.channel.sendall.call_args.args[0]
        env = dict(os.environ)
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        result = subprocess.run([SHELL, "-c", wrapped], capture_output=True, text=True, timeout=timeout, env=env)
        self.assertRegex(result.stdout, pattern)
        return result.stdout

    def test_quotes_multiline_and_blank_lines(self):
        with patch.object(self.session, "_read_until", side_effect=self.real_output):
            result = self.session.exec_command("printf '%s\\n' \"it's working\"\nprintf '\\nlast\\n'")
        self.assertEqual(result["output"], "it's working\n\nlast\n")
        self.assertTrue(result["success"])

    def test_nonzero_exit_is_preserved_by_real_shell(self):
        with patch.object(self.session, "_read_until", side_effect=self.real_output):
            result = self.session.exec_command("false")
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["success"])

    def test_failed_workdir_prevents_all_following_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "not-created")
            with patch.object(self.session, "connect"), patch.object(jump, "SSHJumpSession", return_value=self.session), patch.object(self.session, "_read_until", side_effect=self.real_output):
                result = jump.execute(self.cfg, "dev-1", "printf 'UNEXPECTED'; printf 'ALSO_UNEXPECTED'", missing if missing.startswith("/") else "/nonexistent-jump-ssh-test-dir")
        self.assertFalse(result["success"])
        self.assertNotIn("UNEXPECTED", result["output"])


if __name__ == "__main__":
    unittest.main()
