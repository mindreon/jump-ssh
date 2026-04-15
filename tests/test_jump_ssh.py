import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path("/home/sanmu/.agents/skills/jump-ssh/scripts/jump_ssh.py")
SPEC = importlib.util.spec_from_file_location("jump_ssh_script", MODULE_PATH)
jump_ssh = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(jump_ssh)


def test_resolve_woodpecker_watch_dir_uses_explicit_value(tmp_path):
    explicit_dir = tmp_path / "woodpecker-watch"
    (explicit_dir / "woodpecker_watch").mkdir(parents=True)

    result = jump_ssh.resolve_woodpecker_watch_dir({}, str(explicit_dir), cwd=tmp_path)

    assert result == explicit_dir.resolve()


def test_resolve_woodpecker_watch_dir_falls_back_to_tools_config(tmp_path):
    configured_dir = tmp_path / "configured-watch"
    (configured_dir / "woodpecker_watch").mkdir(parents=True)
    cfg = {"tools": {"woodpecker_watch_dir": str(configured_dir)}}

    result = jump_ssh.resolve_woodpecker_watch_dir(cfg, None, cwd=tmp_path)

    assert result == configured_dir.resolve()


def test_resolve_woodpecker_watch_dir_falls_back_to_cwd_sibling(tmp_path):
    sibling = tmp_path / "woodpecker-watch"
    (sibling / "woodpecker_watch").mkdir(parents=True)

    result = jump_ssh.resolve_woodpecker_watch_dir({}, None, cwd=tmp_path)

    assert result == sibling.resolve()


def test_cmd_woodpecker_verify_prints_json_result(monkeypatch):
    captured = io.StringIO()
    fake_result = {
        "success": True,
        "repo": "mindreon/agentic-service",
        "pipeline_number": 12,
        "reason": "ok",
    }
    fake_args = SimpleNamespace(
        watch_dir="/tmp/woodpecker-watch",
        repo="mindreon/agentic-service",
        commit="abc1234",
        namespace="prod",
        kubectl_context="prod-cluster",
        deployment=None,
        container="agentic-service",
        timeout_seconds=300,
        poll_interval_seconds=5,
    )

    monkeypatch.setattr(jump_ssh, "print_json", lambda payload: captured.write(jump_ssh.json.dumps(payload)))
    monkeypatch.setattr(jump_ssh, "run_woodpecker_verify", lambda cfg, args: fake_result)

    jump_ssh.cmd_woodpecker_verify({"tools": {}}, fake_args)

    assert "\"success\": true" in captured.getvalue()
    assert "\"pipeline_number\": 12" in captured.getvalue()


def test_build_parser_supports_woodpecker_verify_command():
    parser = jump_ssh.build_parser()

    args = parser.parse_args(
        [
            "woodpecker-verify",
            "--repo",
            "mindreon/agentic-service",
            "--commit",
            "abc1234",
            "--watch-dir",
            "/tmp/woodpecker-watch",
        ]
    )

    assert args.subcommand == "woodpecker-verify"
    assert args.repo == "mindreon/agentic-service"
    assert args.commit == "abc1234"
    assert args.watch_dir == "/tmp/woodpecker-watch"


def test_build_watch_config_allows_missing_kubernetes_section():
    models = SimpleNamespace(
        AppConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        WoodpeckerConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        KubernetesConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        DefaultsConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        ImageConfig=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    cfg = {
        "woodpecker": {"server": "https://woodpecker.example.com", "token": "secret"},
        "defaults": {"namespace": "prod", "timeoutSeconds": 900, "pollIntervalSeconds": 10},
        "image": {"repositoryTemplate": "registry.example.com/{serviceName}"},
    }

    watch_config = jump_ssh.build_watch_config(cfg, models)

    assert watch_config.kubernetes.kubectl_context is None
    assert watch_config.defaults.namespace == "prod"


def test_resolve_woodpecker_verify_host_uses_single_allowed_host_by_default():
    cfg = {"allowed_hosts": [{"name": "VM-4-13", "ip": "192.168.4.13", "user": "root"}]}

    host = jump_ssh.resolve_woodpecker_verify_host(cfg, None)

    assert host == "VM-4-13"


def test_remote_kubectl_runner_executes_command_via_jump_session(monkeypatch):
    calls = []

    class FakeSession:
        def __init__(self, cfg, target_ip, target_user):
            calls.append(("init", target_ip, target_user))

        def connect(self):
            calls.append(("connect",))

        def exec_command(self, command):
            calls.append(("exec_command", command))
            return {"output": '{"ok": true}', "exit_code": 0}

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(jump_ssh, "SSHJumpSession", FakeSession)
    cfg = {
        "jumpserver": {"host": "1.1.1.1", "port": 2222, "user": "u"},
        "allowed_hosts": [{"name": "VM-4-13", "ip": "192.168.4.13", "user": "root"}],
    }
    runner = jump_ssh.RemoteKubectlRunner(cfg, "VM-4-13")

    result = runner(
        ["kubectl", "-n", "default", "get", "deployment", "agentic-service", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == '{"ok": true}'
    assert ("exec_command", "kubectl -n default get deployment agentic-service -o json") in calls
