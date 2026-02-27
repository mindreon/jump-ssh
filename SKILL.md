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

### 2. 在指定服务器上执行命令

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${AGENTS_HOME:-$HOME/.agents}/skills/jump-ssh}"
"${PYTHON_BIN:-python3}" "$SKILL_DIR/scripts/jump_ssh.py" exec \
  --host "VM-4-13" \
  --cmd "df -h"
```

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

### 4. 服务启停与部署

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

## 参数说明

| 参数 | 说明 |
|------|------|
| `--host` | 目标服务器名称，必须与 `allowed_hosts[].name` 匹配（不区分大小写） |
| `--cmd` | 在目标服务器执行的 shell 命令 |
| `--config` | 配置文件路径（可选，默认使用 `resources/config.yaml`） |
| `--workdir` | 工作目录（可选；未指定时优先使用 `default_workdir`） |

## 安全约束

- `--host` 必须在 `config.yaml` 的 `allowed_hosts` 中，否则报错。
- Agent 不能自行发现服务器，只能访问用户配置的白名单。
- 敏感凭据保存在本地 `config.yaml` 中。
