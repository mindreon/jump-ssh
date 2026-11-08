---
name: jump-ssh
version: "1.0.0"
display_name: "JumpServer 远程运维"
display_name_en: "JumpServer Remote Operations"
description: "通过 JumpServer 堡垒机在用户配置的白名单服务器上执行远程命令。适用于远程排障、服务启停、发版和 Woodpecker/Kubernetes 更新校验。"
description_zh: "通过 JumpServer 堡垒机在用户配置的白名单服务器上执行远程命令。适用于远程排障、服务启停、发版和 Woodpecker/Kubernetes 更新校验。"
description_en: "Run remote commands on user-configured allowlisted servers through JumpServer. Use for remote troubleshooting, service management, deployment, and Woodpecker/Kubernetes update verification."
---

# jump-ssh Skill

## 执行规则

以当前加载的 `SKILL.md` 所在目录作为 skill 根目录，不要依赖当前工作目录。以下示例使用 Codex 默认安装位置；若实际位置不同，将 `AGENT_SKILL_DIR` 设置为实际根目录。

### Windows PowerShell

```powershell
$SkillDir = if ($env:AGENT_SKILL_DIR) {
    $env:AGENT_SKILL_DIR
} elseif ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME "skills/jump-ssh"
} else {
    Join-Path $env:USERPROFILE ".codex/skills/jump-ssh"
}
$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$JumpSshScript = Join-Path $SkillDir "scripts/jump_ssh.py"
if (-not (Test-Path -LiteralPath $JumpSshScript -PathType Leaf)) {
    throw "jump-ssh script not found: $JumpSshScript"
}

& $PythonBin $JumpSshScript list
& $PythonBin $JumpSshScript exec --host "VM-4-13" --cmd "df -h"
```

### Bash（Linux / macOS / WSL）

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# 可选：先确认脚本存在
test -f "$SKILL_DIR/scripts/jump_ssh.py"
```

后续示例采用 Bash 语法。PowerShell 中使用 `& $PythonBin $JumpSshScript` 调用同一脚本，参数保持一致；不要直接复制 Bash 的变量赋值和反斜杠续行语法。

## 前置配置

**第一步：安装依赖**

仅在依赖尚未安装时执行：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" -m pip install -r "$SKILL_DIR/requirements.txt"
```

**第二步：创建配置文件**
```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
if [ ! -e "$SKILL_DIR/resources/config.yaml" ]; then
  cp "$SKILL_DIR/resources/config.example.yaml" "$SKILL_DIR/resources/config.yaml"
fi
```

PowerShell 对应操作（沿用前文的变量）：

```powershell
& $PythonBin -m pip install -r (Join-Path $SkillDir "requirements.txt")
$ConfigPath = Join-Path $SkillDir "resources/config.yaml"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Copy-Item -LiteralPath (Join-Path $SkillDir "resources/config.example.yaml") -Destination $ConfigPath
}
```

首次配置时编辑 `config.yaml`，填入 JumpServer 账号密码，并配置允许访问的服务器白名单。已配置的文件应继续使用，避免用示例覆盖。

```yaml
jumpserver:
  host: "10.0.0.1"
  port: 2222
  user: "your-username"
  password: "your-password"

# 允许 Agent 访问的服务器白名单
allowed_hosts:
  - name: "VM-4-13"
    ip: "192.168.1.100"
    user: "root"
    default_workdir: "/opt/falsework"
```

## 调用方式

业务命令输出 JSON，方便 Agent 解析；`--help` 和命令行参数错误使用普通文本。示例中的服务器名、目录和服务名需要替换为实际配置。

### 1. 列出允许访问的服务器

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" list
```

### 2. 在指定服务器上执行命令（无状态）

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --cmd "df -h"
```

说明：
- 每次调用都会新建并关闭一次远端 shell。
- 适合单条命令、稳定自动化场景。
- 输出包含 `output`，并额外带 `exit_code`。

指定工作目录：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --workdir "/opt/myapp" \
  --cmd "pwd && ls -la"
```

### 3. 指定配置文件路径（可选）

全局参数 `--config` 必须放在子命令之前。

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" \
  --config /custom/path/config.yaml \
  exec --host "VM-4-13" --cmd "uname -a"
```

### 4. 持久 session 模式

适合需要保留 `cd`、环境变量、shell 上下文的连续命令场景。

启动 session：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-start \
  --host "VM-4-13"
```

返回结果里会包含 `session_id`。

在同一个 session 中连续执行：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-exec \
  --session "<session_id>" \
  --cmd "cd /tmp && pwd"

"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-exec \
  --session "<session_id>" \
  --cmd "pwd"
```

列出当前活跃 session：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-list
```

关闭 session：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-close \
  --session "<session_id>"
```

说明：
- `session-*` 通过本地 Unix socket daemon 维持远端终端，不会在每次调用后断开。
- 持久 session 依赖 Python 的 Unix domain socket 支持；Windows 环境不支持时使用 WSL，或使用无状态 `exec`。
- 输出仍然是 JSON，但只保留最小元信息：`session_id`、`output`、`exit_code`、`alive`。
- 更适合排障、探索式操作、依赖上下文的连续命令。

### 5. 标准发版流程（本地构建镜像 + 远程发布）

用于“代码已修改，需要快速发版到开发环境”的场景。默认按以下顺序执行，避免只重启服务但镜像未更新：

1. 在本地目标项目目录确认镜像构建/推送方式（先看 `Makefile`，不要硬编码目标）。
2. 执行该项目的构建并推送镜像命令（通常是 `make <target>`）。
3. 在 `falsework` 仓库更新对应服务部署脚本/镜像 tag。
4. 使用 jump-ssh 到目标机器拉取最新 `falsework` 并重启服务。

建议先做最小检查：

```bash
# 在项目根目录识别可用目标（按项目实际 Makefile 为准）
rg -n "^(\.PHONY:|[a-zA-Z0-9_.-]+:)" Makefile
rg -n "image|docker|build|push|publish" Makefile
```

本地构建推送（示例，目标名以项目 Makefile 为准）：

```bash
cd <project_dir>
make <build-and-push-target>
```

更新 falsework 并远程发布（示例）：

```bash
# 1) 本地更新 falsework 部署脚本中的镜像 tag
cd <falsework_dir>
# 编辑对应部署文件，提交到目标分支

# 2) 远程主机拉取并重启
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --workdir "/opt/falsework" \
  --cmd "git pull && ./run.sh restart <service_name>"
```

执行约束：
- 发版请求默认包含“构建推送镜像 -> 更新 falsework -> 远程重启”全链路。
- 若用户只要求“重启服务”，才跳过前置构建与部署脚本更新。

### 6. 服务启停与部署

如果在目标服务器上配置了 `default_workdir`，且未传 `--workdir`，会先进入该目录。`default_workdir` 和 `--workdir` 应使用远端绝对路径；当前脚本会对目录做 shell 转义，`~/falsework` 不会展开为用户主目录。

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"

# 启动服务
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --cmd "./run.sh start <service_name>"

# 停止服务
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --cmd "./run.sh stop <service_name>"

# 更新并重启服务
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --cmd "git pull && ./run.sh restart <service_name>"
```

### 7. 校验 Woodpecker 与 Kubernetes 是否完成更新

适合“代码已推送，不想本地构建镜像，希望 Agent 自动判断是否已经真正更新好”的场景。

该命令会：

1. 查找指定 `repo + commit` 对应的 Woodpecker 流水线
2. 等待流水线结束并确认 `success`
3. 在目标服务器上执行 `kubectl`，查询 deployment 对应 Pod 的实际 `imageID`
4. 在目标服务器上执行 `docker manifest inspect`，获取目标 tag 当前的 manifest digest 集合
5. 判断 Pod 的 `imageID` digest 是否命中目标 manifest digest

前置配置：

本地需要可导入的 `woodpecker-watch` 项目及其 Python 依赖，目标服务器需要可用的 `kubectl` 和 `docker`。

在 `config.yaml` 中补充以下配置段：

```yaml
tools:
  woodpecker_watch_dir: "/mnt/data/code/mindreon/woodpecker-watch"

woodpecker:
  server: "https://woodpecker.example.com"
  token: "your-woodpecker-token"

kubernetes:
  # 可选；不填时使用目标服务器上 kubectl 当前默认 context
  kubectlContext: "prod-cluster"

defaults:
  namespace: "prod" # 目标 deployment 所在的 Kubernetes namespace
  timeoutSeconds: 900
  pollIntervalSeconds: 10

image:
  repositoryTemplate: "registry.example.com/{serviceName}"
```

调用方式：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" woodpecker-verify \
  --repo "mindreon/agentic-service" \
  --commit "abc1234"
```

可选覆盖：

```bash
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" woodpecker-verify \
  --repo "mindreon/agentic-service" \
  --commit "abc1234" \
  --namespace "prod" \
  --deployment "agentic-service" \
  --container "agentic-service" \
  --watch-dir "/mnt/data/code/mindreon/woodpecker-watch"
```

说明：
- 默认按 `repo` 最后一段推导 deployment 名
- 若白名单中配置了多台服务器，必须通过 `--host` 指定执行校验的目标；只有一台时自动选用该服务器
- 默认按 Woodpecker 流水线分支/标签推导目标镜像 tag；例如 `release/v0.1.0 -> release-v0.1.0`
- `defaults.namespace` 是目标 deployment 所在的 Kubernetes namespace
- 不配置 `kubectlContext` 时，直接使用目标服务器上的 `kubectl` 默认 context
- 若当前目录下存在 `woodpecker-watch` 项目目录，可不配 `tools.woodpecker_watch_dir`
- 输出仍为 JSON，便于 Agent 继续解析

## 参数说明

| 参数 | 说明 |
|------|------|
| `--host` | 目标服务器名称，必须与 `allowed_hosts[].name` 匹配（不区分大小写） |
| `--cmd` | 在目标服务器执行的 shell 命令 |
| `--config` | 配置文件路径（可选，默认使用 `resources/config.yaml`） |
| `--workdir` | 工作目录（可选；未指定时优先使用 `default_workdir`） |
| `--session` | 持久 session ID（仅 `session-exec` / `session-close` 使用） |

## 安全约束

- `--host` 必须在 `config.yaml` 的 `allowed_hosts` 中，否则报错。
- Agent 不能自行发现服务器，只能访问用户配置的白名单。
- 敏感凭据保存在本地 `config.yaml` 中。
- 不在回复、日志或提交中输出密码、私钥或 token；配置示例仅使用占位值。
- `session-*` 模式会在本地保留常驻 daemon；用完应调用 `session-close` 回收会话。
