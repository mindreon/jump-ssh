---
name: jump-ssh
description: 通过 JumpServer 堡垒机在指定服务器上执行远程命令。用户预先配置好白名单服务器和认证信息，Agent 可直接调用。
---

# jump-ssh Skill

## 执行规则

先确定 skill 根目录，再执行脚本。不要依赖当前工作目录。

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# 可选：先确认脚本存在
test -f "$SKILL_DIR/scripts/jump_ssh.py"
```

## 前置配置

**第一步：安装依赖**
```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" -m pip install -r "$SKILL_DIR/requirements.txt"
```

**第二步：创建配置文件**
```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
cp "$SKILL_DIR/resources/config.example.yaml" "$SKILL_DIR/resources/config.yaml"
```

然后编辑 `config.yaml`，填入 JumpServer 账号密码，并配置允许访问的服务器白名单。

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
    default_workdir: "~/falsework"
```

## 调用方式

所有输出均为 JSON，方便 Agent 解析。

### 1. 列出允许访问的服务器

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" list
```

### 2. 在指定服务器上执行命令（无状态）

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
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
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --workdir "/opt/myapp" \
  --cmd "ls && cat config.yaml"
```

### 3. 指定配置文件路径（可选）

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" \
  --config /custom/path/config.yaml \
  exec --host "VM-4-13" --cmd "uname -a"
```

### 3.1 持久 session 模式

适合需要保留 `cd`、环境变量、shell 上下文的连续命令场景。

启动 session：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-start \
  --host "VM-4-13"
```

返回结果里会包含 `session_id`。

在同一个 session 中连续执行：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-exec \
  --session "<session_id>" \
  --cmd "cd /tmp && pwd"

"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-exec \
  --session "<session_id>" \
  --cmd "pwd"
```

列出当前活跃 session：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-list
```

关闭 session：

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" session-close \
  --session "<session_id>"
```

说明：
- `session-*` 通过本地 Unix socket daemon 维持远端终端，不会在每次调用后断开。
- 输出仍然是 JSON，但只保留最小元信息：`session_id`、`output`、`exit_code`、`alive`。
- 更适合排障、探索式操作、依赖上下文的连续命令。

### 4. 标准发版流程（本地构建镜像 + 远程发布）

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
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --workdir "~/falsework" \
  --cmd "git pull && ./run.sh restart <service_name>"
```

执行约束：
- 发版请求默认包含“构建推送镜像 -> 更新 falsework -> 远程重启”全链路。
- 若用户只要求“重启服务”，才跳过前置构建与部署脚本更新。

### 5. 服务启停与部署

如果在目标服务器上配置了 `default_workdir`，且未传 `--workdir`，会先进入该目录。

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"

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

### 6. 校验 Woodpecker 与 Kubernetes 是否完成更新

适合“代码已推送，不想本地构建镜像，希望 Agent 自动判断是否已经真正更新好”的场景。

该命令会：

1. 查找指定 `repo + commit` 对应的 Woodpecker 流水线
2. 等待流水线结束并确认 `success`
3. 在目标服务器上执行 `kubectl`，查询 deployment 对应 Pod 的实际 `imageID`
4. 在目标服务器上执行 `docker manifest inspect`，获取目标 tag 当前的 manifest digest 集合
5. 判断 Pod 的 `imageID` digest 是否命中目标 manifest digest

前置配置：

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
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
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
- `session-*` 模式会在本地保留常驻 daemon；用完应调用 `session-close` 回收会话。
