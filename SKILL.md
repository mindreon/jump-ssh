---
name: jump-ssh
description: 通过 JumpServer 堡垒机在指定服务器上执行远程命令。用户预先配置好白名单服务器和认证信息，Agent 可直接调用。
---

# jump-ssh Skill

## 前置配置

**第一步：安装依赖**
```bash
pip install -r .agents/skills/jump-ssh/requirements.txt
```

**第二步：创建配置文件**
```bash
cp .agents/skills/jump-ssh/resources/config.example.yaml \
   .agents/skills/jump-ssh/resources/config.yaml
```

然后编辑 `config.yaml`，填入 JumpServer 账号密码，并配置允许访问的服务器白名单。

```yaml
jumpserver:
  host: "183.36.30.86"
  port: 2222
  user: "your-username"
  password: "your-password"

# 只有列在这里的服务器才能被访问
allowed_hosts:
  - name: "VM-4-13"
    match: "4.13"        # JumpServer 搜索关键词（IP 或主机名片段）
    default_workdir: "~/falsework" # 默认工作目录（可选），未指定 --workdir 时会进入此目录
```

> **安全提示**：`config.yaml` 已加入 `.gitignore`，不会被提交到代码仓库。

---

## 调用方式

脚本路径：`.agents/skills/jump-ssh/scripts/jump_ssh.py`

所有输出均为 JSON 格式，方便 Agent 解析。

### 1. 列出允许访问的服务器

```bash
python .agents/skills/jump-ssh/scripts/jump_ssh.py list
```

输出示例：
```json
{
  "success": true,
  "hosts": [
    {"name": "VM-4-13", "match": "4.13"},
    {"name": "jenkins-arm", "match": "192.168.4.25"}
  ]
}
```

### 2. 在指定服务器上执行命令

```bash
python .agents/skills/jump-ssh/scripts/jump_ssh.py exec \
  --host "VM-4-13" \
  --cmd "df -h"

# 指定工作目录（进入服务器后先 cd 到该目录，再执行命令）
python .agents/skills/jump-ssh/scripts/jump_ssh.py exec \
  --host "VM-4-13" \
  --workdir "/opt/myapp" \
  --cmd "ls && cat config.yaml"
```

输出示例：
```json
{
  "success": true,
  "host": "VM-4-13",
  "match": "4.13",
  "workdir": "/opt/myapp",
  "command": "cd /opt/myapp && ls && cat config.yaml",
  "output": "..."
}
```

### 3. 失败时的输出

```json
{
  "success": false,
  "error": "服务器 'unknown-host' 不在允许列表中。可用: ['VM-4-13', 'jenkins-arm']"
}
```

### 4. 指定配置文件路径（可选）

```bash
python .agents/skills/jump-ssh/scripts/jump_ssh.py \
  --config /custom/path/config.yaml \
  exec --host "VM-4-13" --cmd "uname -a"
```

### 5. 服务启停与部署（Service Management）

如果在目标服务器上配置了 `default_workdir`（如 `~/falsework`），且你需要执行代码部署或服务重启等操作，可以直接利用对应环境下的 `run.sh` 脚本来管理服务。
因为开启了 `default_workdir` 的支持，如果未传 `--workdir` 参数，它默认会先进入对应的路径。

```bash
# 启动服务
python .agents/skills/jump-ssh/scripts/jump_ssh.py exec \
  --host "VM-4-13" \
  --cmd "./run.sh start"

# 停止服务
python .agents/skills/jump-ssh/scripts/jump_ssh.py exec \
  --host "VM-4-13" \
  --cmd "./run.sh stop"

# 重启服务（通常在代码更新后使用）
python .agents/skills/jump-ssh/scripts/jump_ssh.py exec \
  --host "VM-4-13" \
  --cmd "./run.sh restart"
```

---

## 参数说明

| 参数 | 说明 |
|------|------|
| `--host` | 目标服务器名称，必须与 `allowed_hosts[].name` 完全匹配（不区分大小写） |
| `--cmd` | 在目标服务器执行的 shell 命令 |
| `--config` | 配置文件路径（可选，默认使用 `resources/config.yaml`） |

---

## 安全约束

- **白名单强制**：`--host` 必须在 `config.yaml` 的 `allowed_hosts` 中，否则直接报错
- **用户控制服务器列表**：Agent 无法自行发现和选择服务器，完全由用户决定哪些机器可访问
- **配置文件保密**：密码保存在本地 `config.yaml` 中，不暴露给 Agent
