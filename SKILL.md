---
name: jump-ssh
version: "2.0.0"
display_name: "JumpServer 远程运维"
display_name_en: "JumpServer Remote Operations"
description: "通过 JumpServer 在用户配置的白名单服务器上执行命令。适用于远程排障和经授权的运维操作，支持密码、SSH 私钥和 SSH Agent 认证。"
description_zh: "通过 JumpServer 在用户配置的白名单服务器上执行命令。适用于远程排障和经授权的运维操作，支持密码、SSH 私钥和 SSH Agent 认证。"
description_en: "Run commands on user-allowlisted servers through JumpServer for remote troubleshooting and authorized operations, with password, SSH private-key, or SSH Agent authentication."
---

# Jump SSH

通用 JumpServer SSH 客户端。仅访问配置中已有的主机，不假设任何公司仓库、镜像仓库、部署脚本、集群或目录。

## 使用边界

- Python 3.10+；Windows、Linux、macOS 均支持 `doctor`、`list`、`exec`。
- 持久 `session-*` 仅支持 Linux、macOS、WSL。原生 Windows 使用 `exec`；可把相关命令合并到一次执行。
- 目标机器必须提供 POSIX shell，初始提示符以 `$` 或 `#` 结尾，并支持 `stty`、`printf`、`eval`。不支持目标 Windows PowerShell、交互式编辑器、自动 MFA/验证码输入。
- 默认使用 JumpServer 直连用户名 `堡垒机用户@目标用户@目标IP`。免密指 SSH 公钥认证，不是匿名登录；目标系统账号仍需在 JumpServer 授权。
- 先读本文件完成首次设置；详细认证、主机密钥、全部配置项和排错见 [configuration.md](references/configuration.md)。只有需要保留 shell 状态时才读 [sessions.md](references/sessions.md)。

## 首次安装与配置

以**当前加载的本文件所在目录**为根目录。以下默认路径仅适用于常规 Codex 安装；自定义安装时先设置 `AGENT_SKILL_DIR`。始终通过绝对脚本路径调用，不依赖当前项目目录。

### Windows PowerShell

```powershell
$SkillDir = if ($env:AGENT_SKILL_DIR) {
    $env:AGENT_SKILL_DIR
} elseif ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME "skills/jump-ssh"
} else {
    Join-Path $env:USERPROFILE ".codex/skills/jump-ssh"
}
python -m venv (Join-Path $SkillDir ".venv")
$PythonBin = Join-Path $SkillDir ".venv/Scripts/python.exe"
$Script = Join-Path $SkillDir "scripts/jump_ssh.py"
& $PythonBin -m pip install -r (Join-Path $SkillDir "requirements.txt")
$Config = Join-Path $SkillDir "resources/config.yaml"
if (-not (Test-Path -LiteralPath $Config)) {
    Copy-Item -LiteralPath (Join-Path $SkillDir "resources/config.example.yaml") -Destination $Config
}
```

### Bash（Linux / macOS / WSL）

```bash
SKILL_DIR="${AGENT_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/jump-ssh}"
python3 -m venv "$SKILL_DIR/.venv"
PYTHON_BIN="$SKILL_DIR/.venv/bin/python"
SCRIPT="$SKILL_DIR/scripts/jump_ssh.py"
"$PYTHON_BIN" -m pip install -r "$SKILL_DIR/requirements.txt"
if [ ! -e "$SKILL_DIR/resources/config.yaml" ]; then
  (umask 077; cp "$SKILL_DIR/resources/config.example.yaml" "$SKILL_DIR/resources/config.yaml")
fi
```

已有可用虚拟环境时复用，不重复创建。Linux 缺少 `venv` 时先安装发行版提供的 Python venv 组件。

接着由用户填写本地 `resources/config.yaml`（或指定外部配置）：

1. 设置 `jumpserver.host`、SSH 端口和 JumpServer 用户名；端口不是 Web 控制台的 HTTP/HTTPS 端口。
2. **密码方式**：设置 `auth_mode: password`，选择 `password_env` 或 `password`；**免密方式**：设置 `auth_mode: key`，使用本地私钥、默认密钥或 SSH Agent，不填写 password。具体操作见配置参考。
3. 在 `allowed_hosts` 中填写目标名称、地址、目标登录账号；这些不是 JumpServer 账号。
4. 从管理员处验证 JumpServer SSH 主机公钥指纹，并准备 `known_hosts`。不要为方便自动接受未知主机密钥。
5. 本地执行 `doctor` 和 `list`；用户授权连接后，再用 `exec --host dev-1 --cmd "printf 'connection-ok\n'; whoami; pwd"` 做只读连通性检查。把 `dev-1` 换成实际名称。

`doctor` 不连接服务器，不能证明远端账号授权、公钥部署或网络可用。

## 常用调用

以下沿用 Bash 初始化变量；PowerShell 把 `"$PYTHON_BIN" "$SCRIPT"` 替换为 `& $PythonBin $Script`，其余参数相同。

```bash
# 离线检查：配置、依赖、主机公钥、平台支持
"$PYTHON_BIN" "$SCRIPT" doctor

# 仅列出已有白名单，不搜索或发现远端服务器
"$PYTHON_BIN" "$SCRIPT" list

# 每次建立并关闭一个 SSH 连接
"$PYTHON_BIN" "$SCRIPT" exec --host "dev-1" --cmd "df -h"

# --workdir 优先于主机 default_workdir；允许绝对路径或 ~/ 相对远端主目录
"$PYTHON_BIN" "$SCRIPT" exec --host "dev-1" --workdir "/opt/app" --cmd "pwd && ls -la"

# --config 是全局参数，必须放在子命令前
"$PYTHON_BIN" "$SCRIPT" --config "/path/to/config.yaml" doctor
```

远端命令为单个字符串，由远端 POSIX shell 解释。PowerShell 中含 `$` 的远端命令优先使用单引号，避免在本地展开。含密码或 token 的命令不要直接放在 CLI 参数中。

## 结果与耗时

业务命令向 stdout 返回一个 JSON 对象；`--help` 和参数语法错误为普通文本。

- `success`：只有操作成功且远端退出码为 0 才为 true。
- `output`：远端 PTY 文本，stdout/stderr 合并；ANSI 控制序列被去除，不适合二进制或逐字节传输。
- `exit_code`：远端命令退出码；连接失败、超时、结果未知时不伪造为 0。
- `elapsed_seconds`：当前 CLI 调用总耗时（包含配置、连接或本地 IPC 等待），以单调时钟计时。
- CLI 退出码：成功 0，业务失败 1，参数错误 2。既检查 CLI 状态，也检查 JSON；不要仅凭“拿到输出”判断成功。
- `timeout.connect`、`timeout.expect`、`timeout.command` 分别限制各连接、握手/提示符、命令阶段，不是同一个总时限。完整调用可能叠加多个阶段。
- 超时会关闭对应 SSH 会话，但远端子进程可能仍在运行。不得自动重试部署、删除、重启等操作；先检查实际状态。

## 运维与安全

- 仅按用户请求执行操作；白名单是路由约束，不是 shell 沙箱，不能防止获准主机上的命令再次访问网络。
- 部署与服务管理依照目标项目的实际说明，不默认执行 `git pull`、构建推送、重启或特定脚本。
- 凭据留在本地配置、私钥或环境变量中，不要求用户在聊天中粘贴。Windows 限制配置 ACL，POSIX 使用 `chmod 600`。
- 不输出配置文件全文；工具会隐藏异常中的已知认证秘密，但远端命令自己打印的敏感内容仍需避免。
- 分发时仅包含 `SKILL.md`、`agents/`、`assets/`、`references/`、`scripts/jump_ssh.py`、`requirements.txt`、`resources/config.example.yaml`；可附 `tests/`。不要打包真实配置、密钥、`.env`、`.git`、虚拟环境或缓存。

## 旧版迁移

2.0.0 移除绑定外部项目的 `woodpecker-verify` 和固定发布流程，避免安装技能后仍依赖未随包提供的内部代码。需要 CI 或 Kubernetes 检查时，根据用户环境使用已有工具或通过 `exec` 调用已安装的命令，不自动安装集群工具。

所有平台统一使用 Paramiko；不读取 OpenSSH 的 `~/.ssh/config`。原先依赖 SSH Host 别名、IdentityFile 或跳板设置时，在 YAML 填写真实地址、端口和 `private_key`。保持 `identity_file`、`asset_id`、`node` 的配置别名，详见配置参考。

默认严格校验主机公钥，不再忽略 host key 错误，也不强行开启过时算法。旧的持久会话需先在旧版本关闭，新版不复用旧协议会话。已有真实配置不会自动改写。
