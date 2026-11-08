# 持久终端

仅在需要保留 `cd`、变量或 shell 上下文时使用。要求 Linux、macOS 或 WSL 的 Unix domain socket 支持；Windows 原生客户端明确拒绝此模式，不会创建不可用的后台进程。

沿用 `SKILL.md` 中的 Bash 变量；自定义配置需在**每次调用**时使用相同的 `--config`。

```bash
"$PYTHON_BIN" "$SCRIPT" session-start --host "dev-1"
# 从 JSON 读取 session_id，替换以下占位值
"$PYTHON_BIN" "$SCRIPT" session-exec --session "SESSION_ID" --cmd "cd /tmp && export CHECK_VALUE=ready"
"$PYTHON_BIN" "$SCRIPT" session-exec --session "SESSION_ID" --cmd 'printf "%s\n" "$CHECK_VALUE"; pwd'
"$PYTHON_BIN" "$SCRIPT" session-list
"$PYTHON_BIN" "$SCRIPT" session-close --session "SESSION_ID"
```

## 生命周期

- 一个配置对应一个本地 daemon；首次 `session-start` 启动。`session-list` 不会为不存在的 daemon 启动后台进程。
- socket 位于 `~/.cache/jump-ssh/`；目录限当前用户访问，新 socket 使用限制性 umask。
- 不并发发送命令。同一 daemon 的会话操作串行处理，终端命令顺序必须由调用者保证。
- 用完显式 `session-close`。空闲超过 `timeout.idle` 的会话自动关闭；没有会话且无请求约 10 秒后 daemon 退出。
- 超时或不完整结果使该 SSH 会话关闭，后续不能安全复用。远端后台进程可能存活，禁止盲目重试。
- 修改配置后旧 daemon 会回收所有会话并退出；客户端与 daemon 还会校验配置文件指纹，拒绝在旧配置上发起新操作。等待正在执行的命令结束，再重新 `session-start`。
- daemon 继承启动时的环境变量。修改密码环境变量或 Agent 环境时，先关闭全部会话、等待 daemon 退出，再启动。文件指纹不检测进程环境变量变化。
- 若 CLI 中断，daemon 可能继续执行当次命令；先查询状态，不能以本地退出判断远端已停止。

## 限制与恢复

daemon 是当前用户权限下的本地便利工具，不是隔离多用户的服务。与同一操作系统账号下的其他进程共享信任边界。

不要在会话中 `exit`、`exec` 替换 shell、启用 `set -e`、改变终端回显/提示符或启动交互式编辑器。这会破坏命令结果协议。对于独立命令，优先用 `exec`。

socket 不响应时不会自动删除并覆盖，因为旧 daemon 可能仍在工作。先确认相关进程已退出且命令状态明确，再删除报错对应的单个陈旧 socket。不要删除整个缓存目录或结束不相关进程。

本地 IPC 和 SSH 等待均有有限超时；IPC 预算覆盖连接、初始化与执行阶段，但不是排队服务的无限等待。调用者必须等待上一条操作返回再发下一条。
