#!/usr/bin/env python3
"""
jump-ssh: 通过 JumpServer 直连目标服务器执行命令

用法:
    python jump_ssh.py list
    python jump_ssh.py exec --host VM-4-13 --cmd "ls"
    python jump_ssh.py session-start --host VM-4-13
    python jump_ssh.py session-exec --session <id> --cmd "pwd"
    python jump_ssh.py session-close --session <id>
"""

import argparse
import hashlib
import importlib
import json
import re
import shlex
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pexpect
import yaml

SKILL_DIR = Path(__file__).parent.parent
DEFAULT_CONFIG = SKILL_DIR / "resources" / "config.yaml"
CACHE_DIR = Path.home() / ".cache" / "jump-ssh"
PROMPT_SHELL = [r"\$\s*$", r"#\s*$"]
SESSION_PROTOCOL_VERSION = "2"


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def fatal(msg: str) -> None:
    print_json({"success": False, "error": msg})
    sys.exit(1)


def resolved_config_path(config_path: Optional[str]) -> Path:
    return Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG.resolve()


def load_config(config_path: Optional[str] = None) -> dict[str, Any]:
    path = resolved_config_path(config_path)
    if not path.exists():
        example = SKILL_DIR / "resources" / "config.example.yaml"
        fatal(f"配置文件不存在: {path}\n请参考 {example} 创建配置文件")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_host(name: str, allowed_hosts: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for host in allowed_hosts:
        if host["name"].lower() == name.lower():
            return host
    return None


def lookup_host(cfg: dict[str, Any], host_name: str) -> tuple[dict[str, Any], str, str, Optional[str]]:
    allowed = cfg.get("allowed_hosts", [])
    host = resolve_host(host_name, allowed)
    if not host:
        available = [item["name"] for item in allowed]
        raise ValueError(f"服务器 '{host_name}' 不在允许列表中。可用: {available}")

    target_ip = host.get("ip")
    if not target_ip:
        raise ValueError(f"配置错误: '{host_name}' 缺少 ip 字段")

    target_user = host.get("user", "root")
    workdir = host.get("default_workdir")
    return host, target_ip, target_user, workdir


class SSHJumpSession:
    """基于系统 ssh + pexpect 的持久终端会话。"""

    def __init__(self, cfg: dict[str, Any], target_ip: str, target_user: str):
        js = cfg["jumpserver"]
        self.host = js["host"]
        self.port = int(js["port"])
        self.js_user = js["user"]
        self.password = js.get("password")
        self.direct_user = f"{self.js_user}@{target_user}@{target_ip}"

        timeouts = cfg.get("timeout", {})
        self.t_connect = timeouts.get("connect", 15)
        self.t_expect = timeouts.get("expect", 15)
        self.t_cmd = timeouts.get("command", 60)

        self.child: Optional[pexpect.spawn] = None
        self.command_lock = threading.Lock()

    def connect(self) -> None:
        cmd = [
            "ssh",
            "-tt",
            "-p",
            str(self.port),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={self.t_connect}",
            "-l",
            self.direct_user,
            self.host,
        ]
        child = pexpect.spawn(
            cmd[0],
            cmd[1:],
            encoding="utf-8",
            codec_errors="replace",
            timeout=max(self.t_connect, self.t_expect),
            dimensions=(50, 220),
        )

        try:
            while True:
                idx = child.expect(
                    [
                        r"(?i)are you sure you want to continue connecting",
                        r"(?i)password:",
                        *PROMPT_SHELL,
                        pexpect.EOF,
                        pexpect.TIMEOUT,
                    ],
                    timeout=max(self.t_connect, self.t_expect),
                )
                if idx == 0:
                    child.sendline("yes")
                    continue
                if idx == 1:
                    if self.password is None:
                        raise RuntimeError("JumpServer 要求密码，但配置中未提供 password")
                    child.sendline(self.password)
                    continue
                if idx in (2, 3):
                    self.child = child
                    return
                if idx == 4:
                    raise RuntimeError(f"SSH 连接提前关闭: {(child.before or '').strip()}")
                raise RuntimeError(f"SSH 连接超时: {(child.before or '').strip()}")
        except Exception:
            child.close(force=True)
            raise

    def is_alive(self) -> bool:
        return bool(self.child and self.child.isalive())

    def close(self) -> None:
        if self.child:
            self.child.close(force=True)

    def exec_command(self, command: str) -> dict[str, Any]:
        if not self.child or not self.is_alive():
            raise RuntimeError("session 已断开")

        with self.command_lock:
            token = uuid.uuid4().hex
            exit_marker = f"__JUMP_EXIT__ {token}"
            end_marker = f"__JUMP_END__ {token}"
            marker_prefix = "__JUMP"
            exit_suffix = f"_EXIT__ {token}"
            end_suffix = f"_END__ {token}"
            full_cmd = (
                f"{command}; "
                "__jump_ssh_exit=$?; "
                f"printf '{marker_prefix}''{exit_suffix} %s\\n' \"$__jump_ssh_exit\"; "
                f"printf '{marker_prefix}''{end_suffix}\\n'"
            )

            self.child.sendline(full_cmd)
            try:
                self.child.expect(re.escape(end_marker), timeout=self.t_cmd)
            except pexpect.EOF as exc:
                raise RuntimeError(f"session 在命令执行期间断开: {(self.child.before or '').strip()}") from exc
            except pexpect.TIMEOUT as exc:
                raise TimeoutError(f"命令执行超时: {(self.child.before or '').strip()}") from exc

            raw = self.child.before or ""
            self._drain_prompt()
            output, exit_code = self._clean(raw, command, token)
            return {
                "command": command,
                "output": output,
                "exit_code": exit_code,
                "alive": self.is_alive(),
            }

    def _drain_prompt(self) -> None:
        if not self.child or not self.is_alive():
            return
        try:
            self.child.expect(PROMPT_SHELL, timeout=0.5)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass

    @staticmethod
    def _clean(raw: str, command: str, token: str) -> tuple[str, Optional[int]]:
        ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\[[0-9;]*m|\r")
        cleaned = ansi.sub("", raw)

        exit_code = None
        exit_pattern = re.compile(rf"__JUMP_EXIT__ {re.escape(token)} (\d+)")
        match = exit_pattern.search(cleaned)
        if match:
            exit_code = int(match.group(1))
            cleaned = exit_pattern.sub("", cleaned)

        lines = [line for line in cleaned.split("\n") if line.strip()]
        if lines:
            first = lines[0].strip()
            cmd = command.strip()
            if cmd and (first == cmd or first.startswith(cmd) or first.endswith(cmd)):
                lines = lines[1:]

        lines = [line for line in lines if "__JUMP_END__" not in line and "__JUMP_EXIT__" not in line]

        if lines and re.match(r"^[^\n]*[#$]\s*$", lines[-1].strip()):
            lines = lines[:-1]

        return "\n".join(lines).strip(), exit_code


class SessionManager:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.sessions: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            items = []
            for session_id, record in self.sessions.items():
                session = record["session"]
                items.append(
                    {
                        "session_id": session_id,
                        "host": record["host_name"],
                        "ip": record["ip"],
                        "user": record["user"],
                        "workdir": record.get("workdir"),
                        "created_at": record["created_at"],
                        "alive": session.is_alive(),
                    }
                )
            return items

    def start_session(self, host_name: str, workdir: Optional[str]) -> dict[str, Any]:
        host, target_ip, target_user, default_workdir = lookup_host(self.cfg, host_name)
        session = SSHJumpSession(self.cfg, target_ip, target_user)
        session.connect()

        effective_workdir = workdir or default_workdir
        init_result = None
        if effective_workdir:
            init_result = session.exec_command(f"cd {shlex.quote(effective_workdir)}")
            if init_result["exit_code"] != 0:
                session.close()
                raise RuntimeError(
                    f"初始化工作目录失败: {effective_workdir}\n{init_result['output']}".strip()
                )

        session_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.sessions[session_id] = {
                "session": session,
                "host_name": host["name"],
                "ip": target_ip,
                "user": target_user,
                "workdir": effective_workdir,
                "created_at": int(time.time()),
            }

        return {
            "session_id": session_id,
            "host": host["name"],
            "ip": target_ip,
            "user": target_user,
            "workdir": effective_workdir,
            "alive": session.is_alive(),
            "init_output": init_result["output"] if init_result else "",
            "init_exit_code": init_result["exit_code"] if init_result else 0,
        }

    def exec_session(self, session_id: str, command: str) -> dict[str, Any]:
        with self.lock:
            record = self.sessions.get(session_id)
        if not record:
            raise KeyError(f"session 不存在: {session_id}")

        session = record["session"]
        result = session.exec_command(command)
        return {
            "session_id": session_id,
            "host": record["host_name"],
            "ip": record["ip"],
            "user": record["user"],
            "workdir": record.get("workdir"),
            **result,
        }

    def close_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            record = self.sessions.pop(session_id, None)
        if not record:
            raise KeyError(f"session 不存在: {session_id}")

        session = record["session"]
        was_alive = session.is_alive()
        session.close()
        return {
            "session_id": session_id,
            "host": record["host_name"],
            "alive_before_close": was_alive,
            "closed": True,
        }


class SessionRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return

        try:
            request = json.loads(raw.decode("utf-8"))
            response = self.server.dispatch(request)
        except Exception as exc:  # pragma: no cover
            response = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class SessionServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, cfg: dict[str, Any]):
        self.manager = SessionManager(cfg)
        super().__init__(socket_path, SessionRequestHandler)

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        try:
            if action == "ping":
                return {"success": True, "pong": True}
            if action == "list":
                return {"success": True, "sessions": self.manager.list_sessions()}
            if action == "start":
                return {"success": True, **self.manager.start_session(request["host"], request.get("workdir"))}
            if action == "exec":
                return {
                    "success": True,
                    **self.manager.exec_session(request["session_id"], request["command"]),
                }
            if action == "close":
                return {"success": True, **self.manager.close_session(request["session_id"])}
            return {"success": False, "error": f"未知 action: {action}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class RemoteCompletedProcess:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RemoteCommandRunner:
    def __init__(self, cfg: dict[str, Any], host_name: str):
        self.cfg = cfg
        self.host_name = host_name

    def __call__(self, cmd, capture_output=True, text=True, check=False):
        del capture_output, text, check

        _, target_ip, target_user, _ = lookup_host(self.cfg, self.host_name)
        session = SSHJumpSession(self.cfg, target_ip, target_user)
        try:
            session.connect()
            result = session.exec_command(shlex.join(cmd))
        finally:
            session.close()

        output = result.get("output", "")
        exit_code = result.get("exit_code") or 0
        stderr = output if exit_code != 0 else ""
        return RemoteCompletedProcess(returncode=exit_code, stdout=output, stderr=stderr)


class RemoteKubectlRunner(RemoteCommandRunner):
    pass


def socket_path_for_config(config_path: Optional[str]) -> Path:
    resolved = f"{resolved_config_path(config_path)}::{SESSION_PROTOCOL_VERSION}"
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"jump-ssh-{digest}.sock"


def ping_server(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.5)
    try:
        client.connect(str(socket_path))
        client.sendall(b'{"action":"ping"}\n')
        response = client.recv(4096)
        if not response:
            return False
        payload = json.loads(response.decode("utf-8"))
        return bool(payload.get("success") and payload.get("pong"))
    except Exception:
        return False
    finally:
        client.close()


def ensure_server(socket_path: Path, config_path: Optional[str]) -> None:
    if ping_server(socket_path):
        return

    if socket_path.exists():
        socket_path.unlink()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--config",
            str(resolved_config_path(config_path)),
            "serve",
            "--socket-path",
            str(socket_path),
        ],
        cwd=str(SKILL_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + 5
    while time.time() < deadline:
        if ping_server(socket_path):
            return
        time.sleep(0.1)
    raise RuntimeError(f"session daemon 启动失败: {socket_path}")


def send_session_request(config_path: Optional[str], payload: dict[str, Any]) -> dict[str, Any]:
    socket_path = socket_path_for_config(config_path)
    ensure_server(socket_path, config_path)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(socket_path))
        client.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        response_bytes = b""
        while not response_bytes.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            response_bytes += chunk
    finally:
        client.close()

    if not response_bytes:
        raise RuntimeError("session daemon 未返回数据")
    return json.loads(response_bytes.decode("utf-8"))


def cmd_list(cfg: dict[str, Any]) -> None:
    allowed = cfg.get("allowed_hosts", [])
    if not allowed:
        fatal("配置文件中 allowed_hosts 为空")

    result = []
    for host in allowed:
        info = {
            "name": host["name"],
            "ip": host.get("ip", ""),
            "user": host.get("user", "root"),
        }
        if "default_workdir" in host:
            info["default_workdir"] = host["default_workdir"]
        result.append(info)
    print_json({"success": True, "hosts": result})


def cmd_exec(cfg: dict[str, Any], host_name: str, command: str, workdir: Optional[str] = None) -> None:
    _, target_ip, target_user, default_workdir = lookup_host(cfg, host_name)
    effective_workdir = workdir or default_workdir

    if effective_workdir:
        command = f"cd {shlex.quote(effective_workdir)} && {command}"

    session = SSHJumpSession(cfg, target_ip, target_user)
    try:
        session.connect()
        result = session.exec_command(command)
        print_json(
            {
                "success": True,
                "host": host_name,
                "ip": target_ip,
                "user": target_user,
                "workdir": effective_workdir,
                **result,
            }
        )
    except TimeoutError as exc:
        fatal(f"超时: {exc}")
    except RuntimeError as exc:
        fatal(f"执行失败: {exc}")
    except Exception as exc:
        fatal(f"未知错误: {type(exc).__name__}: {exc}")
    finally:
        session.close()


def cmd_session_start(config_path: Optional[str], host_name: str, workdir: Optional[str]) -> None:
    response = send_session_request(
        config_path,
        {"action": "start", "host": host_name, "workdir": workdir},
    )
    print_json(response)


def cmd_session_exec(config_path: Optional[str], session_id: str, command: str) -> None:
    response = send_session_request(
        config_path,
        {"action": "exec", "session_id": session_id, "command": command},
    )
    print_json(response)


def cmd_session_list(config_path: Optional[str]) -> None:
    response = send_session_request(config_path, {"action": "list"})
    print_json(response)


def cmd_session_close(config_path: Optional[str], session_id: str) -> None:
    response = send_session_request(
        config_path,
        {"action": "close", "session_id": session_id},
    )
    print_json(response)


def cmd_serve(cfg: dict[str, Any], socket_path: str) -> None:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    server = SessionServer(str(path), cfg)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        if path.exists():
            path.unlink()


def resolve_woodpecker_watch_dir(
    cfg: dict[str, Any],
    explicit_dir: Optional[str],
    cwd: Optional[Path] = None,
) -> Path:
    candidates: list[Path] = []
    if explicit_dir:
        candidates.append(Path(explicit_dir))

    tools_cfg = cfg.get("tools", {})
    configured_dir = tools_cfg.get("woodpecker_watch_dir")
    if configured_dir:
        candidates.append(Path(configured_dir))

    base_dir = cwd.resolve() if cwd else Path.cwd().resolve()
    candidates.append(base_dir / "woodpecker-watch")

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "woodpecker_watch").is_dir():
            return resolved

    raise ValueError(
        "未找到 woodpecker-watch 项目目录。请通过 --watch-dir 指定，或在配置文件中设置 tools.woodpecker_watch_dir"
    )


def resolve_woodpecker_verify_host(cfg: dict[str, Any], explicit_host: Optional[str]) -> str:
    if explicit_host:
        lookup_host(cfg, explicit_host)
        return explicit_host

    allowed_hosts = cfg.get("allowed_hosts", [])
    if len(allowed_hosts) == 1:
        return allowed_hosts[0]["name"]

    raise ValueError("woodpecker-verify 需要 --host，或配置中只能存在一个 allowed_hosts")


def load_woodpecker_watch_modules(watch_dir: Path) -> dict[str, Any]:
    watch_dir_str = str(watch_dir)
    if watch_dir_str not in sys.path:
        sys.path.insert(0, watch_dir_str)

    return {
        "models": importlib.import_module("woodpecker_watch.models"),
        "watch_service": importlib.import_module("woodpecker_watch.watch_service"),
        "woodpecker_client": importlib.import_module("woodpecker_watch.woodpecker_client"),
        "kube_client": importlib.import_module("woodpecker_watch.kube_client"),
        "registry_client": importlib.import_module("woodpecker_watch.registry_client"),
    }


def build_watch_config(cfg: dict[str, Any], models: Any) -> Any:
    try:
        woodpecker_cfg = cfg["woodpecker"]
        defaults_cfg = cfg["defaults"]
        image_cfg = cfg["image"]
    except KeyError as exc:
        raise ValueError(
            "缺少 woodpecker-watch 配置。需要配置 woodpecker / defaults / image 段"
        ) from exc
    kubernetes_cfg = cfg.get("kubernetes", {})

    return models.AppConfig(
        woodpecker=models.WoodpeckerConfig(
            server=woodpecker_cfg["server"],
            token=woodpecker_cfg["token"],
        ),
        kubernetes=models.KubernetesConfig(
            kubectl_context=kubernetes_cfg.get("kubectlContext"),
        ),
        defaults=models.DefaultsConfig(
            namespace=defaults_cfg["namespace"],
            timeout_seconds=defaults_cfg["timeoutSeconds"],
            poll_interval_seconds=defaults_cfg["pollIntervalSeconds"],
        ),
        image=models.ImageConfig(
            repository_template=image_cfg["repositoryTemplate"],
        ),
    )


def run_woodpecker_verify(cfg: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    watch_dir = resolve_woodpecker_watch_dir(cfg, args.watch_dir)
    host_name = resolve_woodpecker_verify_host(cfg, getattr(args, "host", None))
    modules = load_woodpecker_watch_modules(watch_dir)
    models = modules["models"]
    watch_config = build_watch_config(cfg, models)
    service = modules["watch_service"].WatchService(
        config=watch_config,
        woodpecker_client=modules["woodpecker_client"].WoodpeckerClient(
            server=watch_config.woodpecker.server,
            token=watch_config.woodpecker.token,
        ),
        kube_client=modules["kube_client"].KubernetesClient(
            runner=RemoteKubectlRunner(cfg, host_name),
        ),
        registry_client=modules["registry_client"].RegistryClient(
            runner=RemoteCommandRunner(cfg, host_name),
        ),
    )
    request = models.VerifyRequest(
        repo=args.repo,
        commit=args.commit,
        namespace=args.namespace,
        kubectl_context=args.kubectl_context,
        deployment=args.deployment,
        container=args.container,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    result = service.verify(request)
    if hasattr(result, "to_dict"):
        return result.to_dict()
    return dict(result)


def cmd_woodpecker_verify(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    try:
        print_json(run_woodpecker_verify(cfg, args))
    except Exception as exc:
        fatal(f"woodpecker 校验失败: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 JumpServer 在指定服务器执行命令")
    parser.add_argument("--config", help="配置文件路径（默认: resources/config.yaml）")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("list", help="列出白名单中允许访问的服务器")

    exec_parser = subparsers.add_parser("exec", help="在指定服务器上执行命令")
    exec_parser.add_argument("--host", required=True, help="目标服务器名称（来自白名单 name 字段）")
    exec_parser.add_argument("--cmd", required=True, help="要执行的 shell 命令")
    exec_parser.add_argument("--workdir", default=None, help="工作目录，进入服务器后先 cd 到该目录")

    session_start = subparsers.add_parser("session-start", help="启动持久终端 session")
    session_start.add_argument("--host", required=True, help="目标服务器名称（来自白名单 name 字段）")
    session_start.add_argument("--workdir", default=None, help="初始工作目录")

    session_exec = subparsers.add_parser("session-exec", help="在已有 session 中执行命令")
    session_exec.add_argument("--session", required=True, help="session ID")
    session_exec.add_argument("--cmd", required=True, help="要执行的 shell 命令")

    subparsers.add_parser("session-list", help="列出当前 config 下的活跃 session")

    session_close = subparsers.add_parser("session-close", help="关闭 session")
    session_close.add_argument("--session", required=True, help="session ID")

    watch_verify = subparsers.add_parser(
        "woodpecker-verify",
        help="校验指定 commit 的 Woodpecker 流水线和 Kubernetes 镜像是否已更新",
    )
    watch_verify.add_argument("--host", default=None, help="执行 kubectl 的目标服务器名称；未传时若白名单只有一台则自动使用")
    watch_verify.add_argument("--repo", required=True, help="Woodpecker repo 全名，如 mindreon/agentic-service")
    watch_verify.add_argument("--commit", required=True, help="要校验的 commit sha，支持前缀匹配")
    watch_verify.add_argument("--namespace", default=None, help="Kubernetes namespace，可覆盖配置")
    watch_verify.add_argument("--kubectl-context", default=None, help="kubectl context，可覆盖配置")
    watch_verify.add_argument("--deployment", default=None, help="deployment 名，可覆盖默认 repo 尾段")
    watch_verify.add_argument("--container", default=None, help="container 名，多容器场景建议显式指定")
    watch_verify.add_argument("--timeout-seconds", type=int, default=None, help="轮询超时秒数，可覆盖配置")
    watch_verify.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=None,
        help="轮询间隔秒数，可覆盖配置",
    )
    watch_verify.add_argument(
        "--watch-dir",
        default=None,
        help="woodpecker-watch 项目目录；未传时优先取配置 tools.woodpecker_watch_dir，再退回当前目录下的 woodpecker-watch",
    )

    serve_parser = subparsers.add_parser("serve", help=argparse.SUPPRESS)
    serve_parser.add_argument("--socket-path", required=True, help=argparse.SUPPRESS)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand == "serve":
        cmd_serve(load_config(args.config), args.socket_path)
        return

    if args.subcommand == "list":
        cmd_list(load_config(args.config))
        return

    if args.subcommand == "exec":
        cmd_exec(load_config(args.config), args.host, args.cmd, args.workdir)
        return

    if args.subcommand == "session-start":
        cmd_session_start(args.config, args.host, args.workdir)
        return

    if args.subcommand == "session-exec":
        cmd_session_exec(args.config, args.session, args.cmd)
        return

    if args.subcommand == "session-list":
        cmd_session_list(args.config)
        return

    if args.subcommand == "session-close":
        cmd_session_close(args.config, args.session)
        return

    if args.subcommand == "woodpecker-verify":
        cmd_woodpecker_verify(load_config(args.config), args)
        return

    parser.error(f"未知子命令: {args.subcommand}")


if __name__ == "__main__":
    main()
