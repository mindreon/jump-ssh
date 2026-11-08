# 配置与排错

适用于首次安装、切换认证方式或排查连接失败。配置文件默认是技能内的 `resources/config.yaml`；使用 `--config` 可将真实配置放在包外，避免升级覆盖。

## 密码登录

```yaml
jumpserver:
  host: "jump.example.com"
  port: 2222
  user: "your-jumpserver-user"
  auth_mode: "password"
  password_env: "JUMP_SSH_PASSWORD"
allowed_hosts:
  - name: "dev-1"
    ip: "192.0.2.10"
    user: "deploy"
```

`password_env` 的值是**变量名**，不是密码本身。先在将运行脚本的同一环境中设置变量；不要将实际密码粘贴到聊天、命令记录或文档中。

Bash 可交互读取，避免写入历史：

```bash
read -rsp 'JumpServer password: ' JUMP_SSH_PASSWORD; printf '\n'
export JUMP_SSH_PASSWORD
```

PowerShell：

```powershell
$Secret = Read-Host "JumpServer password" -AsSecureString
$Credential = [System.Management.Automation.PSCredential]::new("unused", $Secret)
$env:JUMP_SSH_PASSWORD = $Credential.GetNetworkCredential().Password
Remove-Variable Secret, Credential
```

也可删除 `password_env`，改为 `password: "仅在本地填写实际密码"`。两者不能同时配置。环境变量和 Python 进程内的密码仍是明文；它们避免密码进入配置仓库，并非秘密保险库。

## 免密登录（公钥认证）

前提：JumpServer 已为该用户授权公钥，本地持有对应私钥或 SSH Agent。它不是在目标机器配置 root 免密，也不是只删除 password 就能跳过身份校验。

```yaml
jumpserver:
  host: "jump.example.com"
  port: 2222
  user: "your-jumpserver-user"
  auth_mode: "key"
  private_key: "~/.ssh/id_ed25519"
  allow_agent: false
  look_for_keys: false
allowed_hosts:
  - name: "dev-1"
    ip: "192.0.2.10"
    user: "deploy"
```

两种替代方案：

- 自动发现默认私钥：省略 `private_key`，保留 `look_for_keys: true`，Paramiko 查找用户 `.ssh` 下支持的常用密钥文件。
- SSH Agent：省略 `private_key`，设置 `allow_agent: true`、`look_for_keys: false`。先用 `ssh-add -l` 确认当前环境中的 Agent 持有已授权密钥。不同 Windows Agent 实现的兼容性取决于本机环境。

加密私钥需要 `key_passphrase_env: "JUMP_SSH_KEY_PASSPHRASE"`，或预先加载进 SSH Agent。设置环境变量的方法与密码相同。`key_passphrase` 是本地明文替代项，不可与对应 `_env` 同时设置。私钥内容不应写入 YAML。

推荐显式指定 `auth_mode`。兼容旧配置时省略或写 `auto`：存在 password/password_env 就选 password，否则选 key。不会尝试把登录密码当成私钥口令，也不会在失败后悄悄切换认证模式。

## 校验 JumpServer 身份

认证用户的私钥与服务器的 host key 是两种不同的密钥。客户端必须预先信任 **JumpServer SSH 网关**的 host key，而不是目标机器的 host key。

- 默认读取当前用户 `~/.ssh/known_hosts`；可用 `jumpserver.known_hosts` 指定独立文件。
- 22 端口条目通常为 `jump.example.com`；其他端口通常为 `[jump.example.com]:2222`。
- 请管理员通过可信渠道提供已验证的公钥条目或 SHA256 指纹。也可由用户手动执行 `ssh -p 2222 your-jumpserver-user@jump.example.com`，在确认显示的指纹与管理员提供的一致后接受并退出，不进入业务服务器执行操作。
- `ssh-keyscan` 只能采集网络提供的密钥，不能证明其身份；不要未经核验就将采集结果写入信任文件。
- 主机密钥不匹配时停止，联系管理员核实轮换；不要自动删除旧条目或禁用校验。

`doctor` 检查本地是否存在匹配网关地址/端口的信任条目，但不进行网络指纹比对。显式配置 `private_key` 时也会离线解析私钥并检查解密口令，成功后输出 `private_key_checked: true`。不会检查未显式配置的默认私钥或 Agent 持有的密钥；后续 SSH 握手才证明远端授权。

## 完整配置字段

所有本地文件路径中的 `~` 展开为**本地用户**主目录；相对路径按**配置文件目录**解析。环境变量只通过 `_env` 字段显式读取，不会在普通字符串里插值。Windows 路径建议使用 `/`，例如 `C:/Users/you/.ssh/id_ed25519`。

| 字段 | 必填 / 默认 | 语义 |
|---|---|---|
| `jumpserver.host` | 必填 | 网关 DNS 名或 IP，不含协议、用户名或路径；不读取 OpenSSH Host 别名 |
| `jumpserver.port` | `2222` | 网关 SSH 端口，整数 1–65535 |
| `jumpserver.user` | 必填 | JumpServer 平台用户，不是目标系统账号 |
| `jumpserver.auth_mode` | `auto` | `password`、`key`、`auto` |
| `jumpserver.password` / `password_env` | 密码模式二选一 | 本地密码或环境变量名；空值报错 |
| `jumpserver.private_key` | 可选 | 本地私钥文件；`identity_file` 是旧别名，二者不能并用 |
| `jumpserver.key_passphrase` / `key_passphrase_env` | 可选，二选一 | 加密私钥口令或环境变量名 |
| `jumpserver.allow_agent` | `true` | key 模式是否允许使用本地 SSH Agent；密码模式忽略 |
| `jumpserver.look_for_keys` | `true` | key 模式是否搜索默认私钥；密码模式忽略 |
| `jumpserver.known_hosts` | `~/.ssh/known_hosts` | 已验证的网关 host key 文件 |
| `allowed_hosts` | 必填非空列表 | 只能通过这些已授权主机发起连接 |
| `allowed_hosts[].name` | 必填 | `--host` 对应的本地别名，忽略大小写且不能重复 |
| `allowed_hosts[].ip` | 必填 | JumpServer 中可路由的目标 IP 或主机名；字段名虽叫 ip，也可填主机名 |
| `allowed_hosts[].user` | 必填 | JumpServer 授权的目标系统登录账号，不默认使用 root |
| `allowed_hosts[].default_workdir` | 可选 | 远端绝对目录或 `~/...`；由 `--workdir` 覆盖，省略则保留登录目录 |
| `allowed_hosts[].selector` | 可选 | 菜单兼容模式，见下一节；旧别名 `asset_id`、`node` 三选一 |
| `timeout.connect` | `15` 秒 | TCP 建连阶段 |
| `timeout.expect` | `15` 秒 | SSH banner、认证、通道和初始提示符等各等待阶段 |
| `timeout.command` | `60` 秒 | 单条远端命令等待；不等于 CLI 全程上限 |
| `timeout.idle` | `900` 秒 | 持久会话空闲回收时间，不中断正在执行的命令 |

所有 timeout 均为大于 0 的有限数字。不要把超时设为无限以隐藏卡住的交互。长任务应按实际需求有限增加 command 超时，或使用目标环境已有的受管任务系统。

## 菜单兼容模式（可选）

默认直连不使用菜单。如果某网关只支持 `Opt>` 菜单，可为某个白名单主机显式设置 `selector`。此时先使用 JumpServer 用户名登录，等待 `Opt>`，发送 `:<selector>`，再等待目标 shell 的 `$`/`#` 提示符。

这是已有行为的兼容入口，不是通用菜单自动化；不会搜索资产、猜测选项或处理额外账号选择/MFA。不同网关版本的菜单可能不兼容。selector 必须由用户确认唯一对应当前主机与账号；脚本不能从菜单内容证明这一映射。若不能保证映射，使用直连方式而不是此模式。

## 错误与排查

| 现象 | 检查方式 |
|---|---|
| 缺少模块 | 用运行脚本的同一个 Python 执行 `-m pip install -r requirements.txt` |
| YAML 或配置字段错误 | `doctor` 按字段名或行列号定位；错误不会回显 YAML 中的秘密 |
| 缺少 host key / 不匹配 | 核验网关地址、端口和已验证的 known_hosts 条目，不自动忽略错误 |
| Authentication failed | 核对平台用户名、选用的认证模式、公钥授权/密码和网关认证策略 |
| 免密仍失败 | 本地是否实际有私钥或 Agent；公钥是否配置给正确的 JumpServer 用户；目标资产账号是否已授权 |
| 无法到达提示符 | 是否支持直连，是否出现 MFA/菜单/强制改密，目标 shell 是否是受支持的 POSIX shell |
| 目录初始化失败 | `default_workdir` 是否真实存在，目标账号是否有访问权限 |
| 超时 / 断线 | JSON 的失败不是“远端已经取消”；先确认远端操作状态，再决定下一步 |
| 原生 Windows session 报错 | 使用 `exec` 或在 WSL 内单独安装并配置技能 |

远端请求结果和任何 stderr 可能含业务数据。异常会对已知密码/私钥口令做替换，但用户仍不应执行输出秘密的命令。
