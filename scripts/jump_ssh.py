#!/usr/bin/env python3
"""Allowlisted JumpServer SSH commands. Run --help or doctor before connecting."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_DIR / "resources" / "config.yaml"
CACHE_DIR = Path.home() / ".cache" / "jump-ssh"
SESSION_PROTOCOL_VERSION = "3"
DEFAULT_TIMEOUTS = {"connect": 15, "expect": 15, "command": 60, "idle": 900}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\r")


def require_text(value, field):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def positive_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return value


def resolved_config_path(config_path=None):
    return Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG


def local_file(value, field, base_dir):
    path = Path(require_text(value, field)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"{field} does not point to an existing file")
    return str(path)


def validate_config(cfg, base_dir):
    if not isinstance(cfg, dict):
        raise ValueError("Config root must be a YAML mapping")
    js = cfg.get("jumpserver")
    if not isinstance(js, dict):
        raise ValueError("jumpserver must be a mapping")
    for field in ("host", "user"):
        require_text(js.get(field), f"jumpserver.{field}")
        if re.search(r"[\s@]", js[field]):
            raise ValueError(f"jumpserver.{field} cannot contain whitespace or @")
    port = js.setdefault("port", 2222)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("jumpserver.port must be an integer from 1 to 65535")
    mode = js.get("auth_mode", "auto")
    if mode not in ("auto", "password", "key"):
        raise ValueError("jumpserver.auth_mode must be auto, password, or key")
    for secret, env_field in (("password", "password_env"), ("key_passphrase", "key_passphrase_env")):
        if env_field in js:
            env_name = require_text(js[env_field], f"jumpserver.{env_field}")
            if secret in js:
                raise ValueError(f"Use only one of jumpserver.{secret} and {env_field}")
            value = os.environ.get(env_name)
            if not value:
                raise ValueError(f"Environment variable required by jumpserver.{env_field} is unset or empty")
            js[secret] = value
        if secret in js:
            require_text(js[secret], f"jumpserver.{secret}")
    mode = ("password" if js.get("password") else "key") if mode == "auto" else mode
    if mode == "password" and not js.get("password"):
        raise ValueError("Password mode requires password or password_env")
    if mode == "key" and js.get("password"):
        raise ValueError("Key mode must not include password or password_env")
    js["auth_mode"] = mode
    if js.get("identity_file"):
        if js.get("private_key"):
            raise ValueError("Use only one of private_key and identity_file")
        js["private_key"] = js["identity_file"]
    if js.get("private_key"):
        js["private_key"] = local_file(js["private_key"], "jumpserver.private_key", base_dir)
    if mode == "password" and (js.get("private_key") or js.get("key_passphrase")):
        raise ValueError("Password mode cannot also configure a private key or passphrase")
    if js.get("known_hosts"):
        js["known_hosts"] = local_file(js["known_hosts"], "jumpserver.known_hosts", base_dir)
    for field in ("allow_agent", "look_for_keys"):
        if field in js and type(js[field]) is not bool:
            raise ValueError(f"jumpserver.{field} must be true or false")
    if mode == "key" and not js.get("private_key") and not js.get("allow_agent", True) and not js.get("look_for_keys", True):
        raise ValueError("Key mode requires private_key, allow_agent, or look_for_keys")
    hosts = cfg.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("allowed_hosts must be a non-empty list")
    names = set()
    for i, host in enumerate(hosts):
        if not isinstance(host, dict):
            raise ValueError(f"allowed_hosts[{i}] must be a mapping")
        for field in ("name", "ip", "user"):
            require_text(host.get(field), f"allowed_hosts[{i}].{field}")
        if any(re.search(r"[\s@]", host[field]) for field in ("ip", "user")):
            raise ValueError(f"allowed_hosts[{i}].ip and user cannot contain whitespace or @")
        name = host["name"].casefold()
        if name in names:
            raise ValueError("allowed_hosts names must be unique, ignoring case")
        names.add(name)
        if host.get("default_workdir") is not None:
            remote_directory(host["default_workdir"])
        selectors = [host[k] for k in ("selector", "asset_id", "node") if host.get(k) is not None]
        if len(selectors) > 1:
            raise ValueError("Use only one of selector, asset_id, or node for a host")
        if selectors:
            selector = str(selectors[0])
            if not selector.strip() or any(c in selector for c in "\r\n\x00"):
                raise ValueError("Host selector must be a non-empty single line")
            host["selector"] = selector
    timeout = cfg.setdefault("timeout", {})
    if not isinstance(timeout, dict):
        raise ValueError("timeout must be a mapping")
    for field, default in DEFAULT_TIMEOUTS.items():
        timeout[field] = positive_number(timeout.get(field, default), f"timeout.{field}")
    return cfg


def load_config(config_path=None):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("Missing PyYAML; install requirements.txt with this Python interpreter") from exc
    path = resolved_config_path(config_path)
    if not path.is_file():
        raise ValueError("Config file not found; copy resources/config.example.yaml and configure it, or pass --config")
    try:
        with path.open(encoding="utf-8-sig") as stream:
            cfg = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"Invalid YAML{location}; source omitted to protect credentials") from None
    return validate_config(cfg, path.parent)


def lookup_host(cfg, name):
    for host in cfg["allowed_hosts"]:
        if host["name"].casefold() == name.casefold():
            return host
    raise ValueError("Requested --host is not in allowed_hosts; use list to see configured names")


def remote_directory(value):
    value = require_text(value, "workdir")
    if value == "~":
        return '"$HOME"'
    if value.startswith("~/"):
        return '"$HOME"/' + shlex.quote(value[2:])
    if not value.startswith("/"):
        raise ValueError("Remote workdir must be an absolute POSIX path or start with ~/")
    return shlex.quote(value)


def redact(message, cfg):
    if cfg:
        for key in ("password", "key_passphrase"):
            secret = cfg.get("jumpserver", {}).get(key)
            if secret:
                message = message.replace(secret, "[REDACTED]")
    return message


def session_supported():
    return os.name != "nt" and hasattr(socketserver, "UnixStreamServer")


def doctor(cfg):
    if importlib.util.find_spec("paramiko") is None:
        raise RuntimeError("Missing Paramiko; install requirements.txt with this Python interpreter")
    js = cfg["jumpserver"]
    known_hosts = Path(js.get("known_hosts", Path.home() / ".ssh" / "known_hosts"))
    import paramiko
    private_key_checked = False
    if js.get("private_key"):
        try:
            phrase = js.get("key_passphrase")
            paramiko.PKey.from_path(js["private_key"], passphrase=phrase.encode("utf-8") if phrase else None)
            private_key_checked = True
        except Exception:
            raise ValueError("Unable to read private_key; supply a supported private key (not a public key) and the correct key_passphrase or key_passphrase_env if encrypted") from None
    keys = paramiko.HostKeys()
    if known_hosts.is_file():
        keys.load(str(known_hosts))
    lookup = js["host"] if js["port"] == 22 else f'[{js["host"]}]:{js["port"]}'
    if not keys.lookup(lookup):
        raise ValueError("No trusted JumpServer host key found; verify its fingerprint with your administrator and add it to known_hosts (see references/configuration.md)")
    warnings = []
    if js["auth_mode"] == "key" and not js.get("private_key"):
        warnings.append("Key discovery/SSH Agent availability and server authorization are only proven by an explicit connection test")
    if any(host.get("selector") for host in cfg["allowed_hosts"]):
        warnings.append("Menu selector mode depends on the gateway UI; direct login is preferred")
    return {"success": True, "network_checked": False, "auth_mode": js["auth_mode"],
            "private_key_checked": private_key_checked,
            "host_count": len(cfg["allowed_hosts"]), "session_supported": session_supported(),
            "timeouts": cfg["timeout"], "warnings": warnings}


class SSHJumpSession:
    def __init__(self, cfg, host):
        self.cfg, self.host = cfg, host
        self.client = self.channel = None
        self.lock = threading.Lock()

    def connect(self):
        import paramiko
        js, timeout = self.cfg["jumpserver"], self.cfg["timeout"]
        self.client = paramiko.SSHClient()
        known_hosts = js.get("known_hosts")
        if known_hosts:
            self.client.load_host_keys(known_hosts)
        else:
            self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        selector = self.host.get("selector")
        username = js["user"] if selector else f'{js["user"]}@{self.host["user"]}@{self.host["ip"]}'
        key_mode = js["auth_mode"] == "key"
        try:
            self.client.connect(hostname=js["host"], port=js["port"], username=username,
                                password=js.get("password"), key_filename=js.get("private_key"),
                                passphrase=js.get("key_passphrase"), timeout=timeout["connect"],
                                banner_timeout=timeout["expect"], auth_timeout=timeout["expect"],
                                channel_timeout=timeout["expect"],
                                allow_agent=key_mode and js.get("allow_agent", True),
                                look_for_keys=key_mode and js.get("look_for_keys", True))
            # Open the channel explicitly so PTY/shell setup is bounded as well.
            transport = self.client.get_transport()
            self.channel = transport.open_session(timeout=timeout["expect"])
            self.channel.settimeout(timeout["expect"])
            self._channel_request(self.channel.get_pty, term="dumb", width=220, height=50)
            self._channel_request(self.channel.invoke_shell)
            if selector:
                self._read_until(re.compile(r"Opt>\s*$"), timeout["expect"])
                self.channel.sendall((selector if selector.startswith(":") else ":" + selector) + "\r")
            self._read_until(re.compile(r"[$#]\s*$"), timeout["expect"])
            # Set a deterministic prompt and suppress terminal echo for reliable framing.
            setup = "stty -echo; PS1=''; unset PROMPT_COMMAND"
            result = self.exec_command(setup)
            if result["exit_code"] != 0:
                raise RuntimeError("Unable to initialize the remote POSIX terminal")
        except Exception:
            self.close()
            raise

    def _channel_request(self, action, **kwargs):
        # Paramiko's PTY/shell request acknowledgements ignore Channel.settimeout.
        expired = threading.Event()

        def expire():
            expired.set()
            self.close()

        timer = threading.Timer(self.cfg["timeout"]["expect"], expire)
        timer.daemon = True
        timer.start()
        try:
            action(**kwargs)
            if expired.is_set():
                raise TimeoutError("SSH channel setup timed out")
        except Exception:
            if expired.is_set():
                raise TimeoutError("SSH channel setup timed out") from None
            raise
        finally:
            timer.cancel()

    def _read_until(self, pattern, timeout):
        deadline = time.monotonic() + timeout
        raw = b""
        while time.monotonic() < deadline:
            if self.channel.recv_ready():
                data = self.channel.recv(65536)
                if not data:
                    raise RuntimeError("SSH channel closed before command completion")
                raw += data
                text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
                if pattern.search(text):
                    return text
            elif self.channel.closed or self.channel.exit_status_ready():
                raise RuntimeError("SSH channel closed before command completion")
            else:
                time.sleep(0.02)
        raise TimeoutError("SSH operation timed out; the remote process may still be running. Do not automatically retry mutations")

    def exec_command(self, command):
        require_text(command, "--cmd")
        if not self.is_alive():
            raise RuntimeError("Session is disconnected")
        timeout = self.cfg["timeout"]["command"]
        if not self.lock.acquire(timeout=timeout):
            raise TimeoutError("Session is busy; wait for the preceding command before retrying")
        try:
            token = uuid.uuid4().hex
            # Split marker literals prevent echoed input from matching output markers.
            start, end = f"__JUMP_START_{token}__", f"__JUMP_END_{token}__"
            wrapped = (f"printf '%s%s\\n' '__JUMP_START_' '{token}__'; "
                       f"eval {shlex.quote(command)}; __jump_rc=$?; "
                       f"printf '\\n%s%s %s\\n' '__JUMP_END_' '{token}__' \"$__jump_rc\"\n")
            self.channel.settimeout(timeout)
            self.channel.sendall(wrapped)
            pattern = re.compile(rf"(?:^|\n){re.escape(end)} ([0-9]+)\n")
            raw = self._read_until(pattern, timeout)
            end_match = pattern.search(raw)
            start_match = re.search(rf"(?:^|\n){re.escape(start)}\n", raw)
            if not start_match or not end_match or end_match.start() < start_match.end():
                raise RuntimeError("Incomplete SSH command framing; command result is unknown")
            output = raw[start_match.end():end_match.start()]
            code = int(end_match.group(1))
            return {"success": code == 0, "output": output, "exit_code": code, "alive": self.is_alive()}
        except Exception:
            self.close()
            raise
        finally:
            self.lock.release()

    def is_alive(self):
        return self.channel is not None and not self.channel.closed

    def close(self):
        if self.channel is not None:
            self.channel.close()
        if self.client is not None:
            self.client.close()


def execute(cfg, host_name, command, workdir=None):
    host = lookup_host(cfg, host_name)
    directory = workdir if workdir is not None else host.get("default_workdir")
    if directory:
        command = f"cd {remote_directory(directory)} && {{\n{command}\n}}"
    session = SSHJumpSession(cfg, host)
    try:
        session.connect()
        return {"host": host["name"], "workdir": directory, **session.exec_command(command), "alive": False}
    finally:
        session.close()


class SessionManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.records = {}
        self.lock = threading.RLock()

    def dispatch(self, request):
        # Serialize operations: one terminal cannot safely run concurrent commands.
        with self.lock:
            action = request.get("action")
            if action == "list":
                return {"success": True, "sessions": [{"session_id": sid, "host": r["host"], "alive": r["session"].is_alive()} for sid, r in self.records.items()]}
            if action == "start":
                host = lookup_host(self.cfg, request["host"])
                session = SSHJumpSession(self.cfg, host)
                try:
                    session.connect()
                    directory = request.get("workdir") or host.get("default_workdir")
                    if directory:
                        result = session.exec_command(f"cd {remote_directory(directory)}")
                        if not result["success"]:
                            raise RuntimeError("Initial remote workdir is unavailable")
                except Exception:
                    session.close()
                    raise
                sid = uuid.uuid4().hex
                self.records[sid] = {"session": session, "host": host["name"], "last_used": time.monotonic()}
                return {"success": True, "session_id": sid, "host": host["name"], "alive": True}
            sid = request.get("session_id")
            record = self.records.get(sid)
            if record is None:
                raise ValueError("Session not found or expired; start a new session")
            if action == "close":
                record["session"].close()
                del self.records[sid]
                return {"success": True, "session_id": sid, "closed": True}
            if action == "exec":
                try:
                    return {"session_id": sid, **record["session"].exec_command(request["command"])}
                finally:
                    record["last_used"] = time.monotonic()
            raise ValueError("Unknown session action")

    def reap(self):
        with self.lock:
            for sid, record in list(self.records.items()):
                if not record["session"].is_alive() or time.monotonic() - record["last_used"] > self.cfg["timeout"]["idle"]:
                    record["session"].close()
                    del self.records[sid]

    def close_all(self):
        with self.lock:
            for record in self.records.values():
                record["session"].close()
            self.records.clear()


def socket_path_for_config(config_path):
    identity = f"{resolved_config_path(config_path)}::{SESSION_PROTOCOL_VERSION}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return CACHE_DIR / f"jump-ssh-{digest}.sock"


def config_fingerprint(config_path):
    return hashlib.sha256(resolved_config_path(config_path).read_bytes()).hexdigest()


def ipc_request(path, request, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        deadline = time.monotonic() + timeout
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((json.dumps(request) + "\n").encode())
        response = b""
        while not response.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Local session response timed out; do not automatically repeat the command")
            client.settimeout(remaining)
            data = client.recv(65536)
            if not data:
                raise RuntimeError("Session daemon closed without a response")
            response += data
        return json.loads(response)


def ping_server(path):
    try:
        return ipc_request(path, {"action": "ping"}, 0.5).get("pong", False)
    except (OSError, ValueError, RuntimeError):
        return False


def ensure_server(path, config_path):
    if ping_server(path):
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE_DIR.chmod(0o700)
    if len(os.fsencode(path)) > 100:
        raise RuntimeError("Session socket path is too long; use exec instead")
    # Do not remove an existing socket: it could belong to a busy live daemon.
    if path.exists():
        raise RuntimeError("Session daemon did not respond; verify it has stopped before removing the stale socket")
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--config", str(resolved_config_path(config_path)), "serve"],
                               cwd=str(SKILL_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ping_server(path):
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError("Session daemon failed to start; run doctor or use exec")


def send_session_request(config_path, cfg, request):
    if not session_supported():
        raise RuntimeError("Persistent sessions require Linux/macOS/WSL; on native Windows use exec")
    path = socket_path_for_config(config_path)
    if request["action"] != "start" and not path.exists():
        if request["action"] == "list":
            return {"success": True, "sessions": []}
        raise ValueError("No active session daemon for this config")
    if request["action"] == "start":
        ensure_server(path, config_path)
    request["config_fingerprint"] = config_fingerprint(config_path)
    timeout = cfg["timeout"]
    budget = timeout["connect"] + 5 * timeout["expect"] + 3 * timeout["command"] + 5
    return ipc_request(path, request, budget)


class SessionRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        try:
            raw = self.rfile.readline(1024 * 1024)
            request = json.loads(raw)
            if request.get("action") == "ping":
                response = {"success": True, "pong": True}
            elif request.get("config_fingerprint") != self.server.fingerprint:
                response = {"success": False, "error": "Config changed; close the old daemon and start a new session"}
            else:
                response = self.server.manager.dispatch(request)
            self.server.last_activity = time.monotonic()
        except Exception as exc:
            response = {"success": False, "error": redact(str(exc), self.server.manager.cfg)}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())


_UnixStreamServer = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class SessionServer(socketserver.ThreadingMixIn, _UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, cfg, fingerprint):
        self.manager = SessionManager(cfg)
        self.fingerprint = fingerprint
        self.last_activity = time.monotonic()
        super().__init__(str(path), SessionRequestHandler)
        self.timeout = 1


def serve(config_path, cfg):
    if not session_supported():
        raise RuntimeError("Persistent sessions require Linux/macOS/WSL")
    path = socket_path_for_config(config_path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE_DIR.chmod(0o700)
    old_mask = os.umask(0o077)
    try:
        server = SessionServer(path, cfg, config_fingerprint(config_path))
    finally:
        os.umask(old_mask)
    try:
        while True:
            server.handle_request()
            if config_fingerprint(config_path) != server.fingerprint:
                break
            server.manager.reap()
            if not server.manager.records and time.monotonic() - server.last_activity > 10:
                break
    finally:
        server.manager.close_all()
        server.server_close()
        path.unlink(missing_ok=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Run allowlisted remote commands through JumpServer")
    parser.add_argument("--config", help="YAML config path (default: skill resources/config.yaml)")
    subs = parser.add_subparsers(dest="subcommand", required=True)
    subs.add_parser("doctor", help="Validate local configuration and prerequisites without connecting")
    subs.add_parser("list", help="List configured allowed servers without connecting")
    execute_parser = subs.add_parser("exec", help="Execute one POSIX shell command in a new SSH connection")
    execute_parser.add_argument("--host", required=True)
    execute_parser.add_argument("--cmd", required=True)
    execute_parser.add_argument("--workdir")
    start = subs.add_parser("session-start", help="Start a persistent terminal (Linux/macOS/WSL)")
    start.add_argument("--host", required=True)
    start.add_argument("--workdir")
    session_exec = subs.add_parser("session-exec", help="Execute in an existing terminal")
    session_exec.add_argument("--session", required=True)
    session_exec.add_argument("--cmd", required=True)
    subs.add_parser("session-list", help="List terminals for this config")
    close = subs.add_parser("session-close", help="Close an existing terminal")
    close.add_argument("--session", required=True)
    subs.add_parser("serve", help="Internal session daemon; do not invoke manually")
    return parser


def run(args, cfg):
    if args.subcommand == "doctor":
        return doctor(cfg)
    if args.subcommand == "list":
        return {"success": True, "hosts": [{k: h[k] for k in ("name", "ip", "user", "default_workdir") if k in h} for h in cfg["allowed_hosts"]]}
    if args.subcommand == "exec":
        return execute(cfg, args.host, args.cmd, args.workdir)
    if args.subcommand.startswith("session-"):
        action = args.subcommand.removeprefix("session-")
        request = {"action": action}
        if action == "start":
            lookup_host(cfg, args.host)
            request.update(host=args.host, workdir=args.workdir)
        elif action in ("exec", "close"):
            request["session_id"] = args.session
            if action == "exec":
                request["command"] = require_text(args.cmd, "--cmd")
        return send_session_request(args.config, cfg, request)
    raise ValueError("Unsupported command")


def main():
    args = build_parser().parse_args()
    started = time.monotonic()
    cfg = None
    try:
        cfg = load_config(args.config)
        if args.subcommand == "serve":
            serve(args.config, cfg)
            return 0
        result = run(args, cfg)
    except KeyboardInterrupt:
        result = {"success": False, "error": "Interrupted; remote command completion is unknown"}
    except Exception as exc:
        result = {"success": False, "error": redact(str(exc), cfg), "error_type": type(exc).__name__}
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
