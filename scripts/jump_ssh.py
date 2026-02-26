#!/usr/bin/env python3
"""
jump-ssh: 通过 JumpServer 直连目标服务器执行命令

用法:
    python jump_ssh.py list                              # 列出白名单中的服务器
    python jump_ssh.py exec --host VM-4-13 --cmd "ls"  # 在指定服务器执行命令
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, List, Union

import paramiko
import yaml

# ─── 默认配置路径 ────────────────────────────────────────────────────────────
SKILL_DIR = Path(__file__).parent.parent
DEFAULT_CONFIG = SKILL_DIR / "resources" / "config.yaml"

# 目标机器 shell 提示符（正则）
PROMPT_SHELL = [r"\$\s*$", r"#\s*$"]

def fatal(msg: str):
    print(json.dumps({"success": False, "error": msg}), flush=True)
    sys.exit(1)

def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.exists():
        example = SKILL_DIR / "resources" / "config.example.yaml"
        fatal(f"配置文件不存在: {path}\n请参考 {example} 创建配置文件")
    with open(path, "r") as f:
        return yaml.safe_load(f)

def resolve_host(name: str, allowed_hosts: list) -> Optional[dict]:
    for h in allowed_hosts:
        if h["name"].lower() == name.lower():
            return h
    return None

# ─── 核心：Paramiko 会话包装器 (直连版) ──────────────────────────────────────
class ParamikoExpect:
    """A minimal expect-like wrapper around a paramiko Channel."""
    def __init__(self, chan: paramiko.Channel):
        self.chan = chan
        self.buffer = ""
        self.before = ""

    def expect(self, patterns: Union[str, List[str]], timeout: float = 15.0) -> int:
        self.chan.settimeout(timeout)
        if isinstance(patterns, str):
            patterns = [patterns]
        
        compiled_patterns = [re.compile(p) for p in patterns]
        
        start = time.time()
        while True:
            for idx, p in enumerate(compiled_patterns):
                match = p.search(self.buffer)
                if match:
                    self.before = self.buffer[:match.start()]
                    self.buffer = self.buffer[match.end():]
                    return idx

            if time.time() - start > timeout:
                raise TimeoutError(f"Timeout waiting for {patterns}. Buffer: {self.buffer}")

            try:
                chunk = self.chan.recv(4096)
                if not chunk:
                    raise EOFError("Channel closed by remote")
                self.buffer += chunk.decode('utf-8', 'replace')
            except paramiko.socket.timeout:
                raise TimeoutError(f"Socket timeout waiting for {patterns}. Buffer: {self.buffer}")

class ParamikoJumpSession:
    """
    使用 Paramiko 通过 JumpServer 直连目标服务器建立交互式命令会话。
    原理：认证用户名拼装为 "堡垒机用户@目标机器用户@目标机器IP"
    大大简化了原来繁琐的菜单交互过程。
    """
    def __init__(self, cfg: dict, target_ip: str, target_user: str):
        js = cfg["jumpserver"]
        self.host = js["host"]
        self.port = int(js["port"])
        self.js_user = js["user"]
        self.password = js.get("password")  # 可选，如果为空则 paramiko 默认尝试已加载的密钥
        
        # 组装直连 JumpServer 的混合用户名
        self.direct_user = f"{self.js_user}@{target_user}@{target_ip}"
        
        timeouts = cfg.get("timeout", {})
        self.t_connect = timeouts.get("connect", 15)
        self.t_expect = timeouts.get("expect", 15)
        self.t_cmd = timeouts.get("command", 60)
        
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.chan: Optional[paramiko.Channel] = None
        self.pexpect: Optional[ParamikoExpect] = None

    def connect(self):
        try:
            self.client.connect(
                hostname=self.host,
                port=self.port,
                username=self.direct_user, # 关键：使用直连模式的用户名
                password=self.password,
                timeout=self.t_connect,
                look_for_keys=True,        # 允许搜索本地 ~/.ssh/id_rsa 等私钥
                allow_agent=True           # 允许使用 ssh-agent
            )
            # 使用 invoke_shell 来激活 PTY 环境，兼容许多目标机器的环境及堡垒机的审计强制要求
            self.chan = self.client.invoke_shell(term='xterm', width=200, height=200)
            self.pexpect = ParamikoExpect(self.chan)
            
            # 由于是直连模式，连上后直接就是目标机器的 shell，不再有 Opt> 菜单
            time.sleep(1.0)
            self.chan.send("\r")
            self.pexpect.expect(PROMPT_SHELL, timeout=self.t_expect)
            
        except Exception as e:
            raise RuntimeError(f"JumpServer 直连到目标机器失败 (账户: {self.direct_user}): {e}")

    def close(self):
        if self.chan:
            self.chan.close()
        self.client.close()

    def select_and_exec(self, command: str) -> str:
        chan = self.chan
        px = self.pexpect

        # 1. 发送并执行命令
        ts = int(time.time())
        end_marker = f"JUMP_END_{ts}"
        part1 = "JUMP_END_"
        part2 = str(ts)
        full_cmd = f"{command}; echo '{part1}''{part2}'\r"
        
        chan.send(full_cmd)
        
        # 2. 等待回显标记
        px.expect(re.escape(end_marker), timeout=self.t_cmd)
        
        # 提取结果
        raw = px.before or ""
        return self._clean(raw)

    @staticmethod
    def _clean(raw: str) -> str:
        ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\[[0-9;]*m|\r")
        cleaned = ansi.sub("", raw)
        lines = [line for line in cleaned.split("\n") if line.strip()]
        if lines:
            # 第一行通常是命令回显（echo），跳过
            lines = lines[1:]
        return "\n".join(lines).strip()

# ─── 子命令处理 ──────────────────────────────────────────────────────────────
def cmd_list(cfg: dict):
    allowed = cfg.get("allowed_hosts", [])
    if not allowed:
        fatal("配置文件中 allowed_hosts 为空")
    result = []
    for h in allowed:
        info = {
            "name": h["name"], 
            "ip": h.get("ip", ""),
            "user": h.get("user", "root")
        }
        if "default_workdir" in h:
            info["default_workdir"] = h["default_workdir"]
        result.append(info)
    print(json.dumps({"success": True, "hosts": result}, ensure_ascii=False, indent=2))

def cmd_exec(cfg: dict, host_name: str, command: str, workdir: Optional[str] = None):
    allowed = cfg.get("allowed_hosts", [])
    host = resolve_host(host_name, allowed)
    if not host:
        available = [h["name"] for h in allowed]
        fatal(f"服务器 '{host_name}' 不在允许列表中。可用: {available}")

    target_ip = host.get("ip")
    if not target_ip:
        fatal(f"配置错误: '{host_name}' 缺少 ip 字段 (自系统升级直连模式后，强制要求提供 ip)")
        
    target_user = host.get("user", "root")

    if not workdir and "default_workdir" in host:
        workdir = host["default_workdir"]

    if workdir:
        command = f"cd {workdir} && {command}"

    session = ParamikoJumpSession(cfg, target_ip, target_user)
    try:
        session.connect()
        output = session.select_and_exec(command)  # 直接传递指令，跳过了菜单选择!
        print(json.dumps({
            "success": True,
            "host": host["name"],
            "ip": target_ip,
            "user": target_user,
            "workdir": workdir,
            "command": command,
            "output": output,
        }, ensure_ascii=False, indent=2))
    except TimeoutError as e:
        fatal(f"超时: {e}")
    except RuntimeError as e:
        fatal(f"执行失败: {e}")
    except Exception as e:
        fatal(f"未知错误: {type(e).__name__}: {e}")
    finally:
        session.close()

# ─── CLI 入口 ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="通过 JumpServer 在指定服务器执行命令")
    parser.add_argument("--config", help="配置文件路径（默认: resources/config.yaml）")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    subparsers.add_parser("list", help="列出白名单中允许访问的服务器")

    exec_parser = subparsers.add_parser("exec", help="在指定服务器上执行命令")
    exec_parser.add_argument("--host", required=True, help="目标服务器名称（来自白名单 name 字段）")
    exec_parser.add_argument("--cmd", required=True, help="要执行的 shell 命令")
    exec_parser.add_argument("--workdir", default=None, help="工作目录，进入服务器后先 cd 到该目录")

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.subcommand == "list":
        cmd_list(cfg)
    elif args.subcommand == "exec":
        cmd_exec(cfg, args.host, args.cmd, args.workdir)

if __name__ == "__main__":
    main()
