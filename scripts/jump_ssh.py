#!/usr/bin/env python3
"""
jump-ssh: 通过 JumpServer 堡垒机在指定服务器上执行命令

用法:
    python jump_ssh.py list                              # 列出白名单中的服务器
    python jump_ssh.py exec --host VM-4-13 --cmd "ls"  # 在指定服务器执行命令
    python jump_ssh.py exec --host VM-4-13 --cmd "ls" --workdir /opt/app
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pexpect
import yaml

# ─── 默认配置路径 ────────────────────────────────────────────────────────────
SKILL_DIR = Path(__file__).parent.parent
DEFAULT_CONFIG = SKILL_DIR / "resources" / "config.yaml"

# JumpServer 交互提示符
PROMPT_OPT = "Opt>"                          # 主菜单提示符
PROMPT_HOST = "[Host]>"                      # 多结果选择提示符
PROMPT_SEARCH = "Search:"                    # 资产列表搜索提示符
PROMPT_SHELL = [r"\$\s*$", r"#\s*$"]        # 目标机器 shell 提示符（正则）
PROMPT_PASSWORD = "password:"


def fatal(msg: str):
    print(json.dumps({"success": False, "error": msg}), flush=True)
    sys.exit(1)


# ─── 配置加载 ────────────────────────────────────────────────────────────────
def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.exists():
        example = SKILL_DIR / "resources" / "config.example.yaml"
        fatal(f"配置文件不存在: {path}\n请参考 {example} 创建配置文件")
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ─── 白名单校验 ──────────────────────────────────────────────────────────────
def resolve_host(name: str, allowed_hosts: list) -> Optional[dict]:
    for h in allowed_hosts:
        if h["name"].lower() == name.lower() or h["match"].lower() == name.lower():
            return h
    return None


# ─── 核心：pexpect SSH 会话 ──────────────────────────────────────────────────
class JumpSession:
    """
    使用 pexpect 驱动系统 ssh 命令，自动化 JumpServer 交互式菜单。
    直接调用本机 ssh，终端环境天然正确，无 paramiko PTY 兼容问题。
    """

    def __init__(self, cfg: dict):
        js = cfg["jumpserver"]
        self.host = js["host"]
        self.port = int(js["port"])
        self.user = js["user"]
        self.password = js["password"]
        self.t_connect = cfg.get("timeout", {}).get("connect", 15)
        self.t_expect = cfg.get("timeout", {}).get("expect", 15)
        self.t_cmd = cfg.get("timeout", {}).get("command", 60)
        self._child: Optional[pexpect.spawn] = None

    def connect(self):
        """启动 ssh 进程并登录 JumpServer"""
        ssh_cmd = (
            f"ssh -p {self.port} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout={self.t_connect} "
            f"{self.user}@{self.host}"
        )
        self._child = pexpect.spawn(
            ssh_cmd,
            encoding="utf-8",
            codec_errors="replace",
            timeout=self.t_connect,
            dimensions=(50, 220),
        )

        # 处理密码认证
        idx = self._child.expect([PROMPT_PASSWORD, PROMPT_OPT, pexpect.TIMEOUT, pexpect.EOF],
                                  timeout=self.t_connect)
        if idx == 0:
            self._child.sendline(self.password)
            self._child.expect(PROMPT_OPT, timeout=self.t_expect)
        elif idx == 1:
            pass  # 已经到菜单了（key 认证）
        else:
            raise RuntimeError(f"SSH 连接失败，输出:\n{self._child.before}")

        # JumpServer 在显示 Opt> 后会做一次 ANSI 擦除并重绘（\x1b[5D\x1b[KOpt>）
        # 等待这个双重渲染稳定，避免在 JumpServer 还没就绪时就发送命令
        self._drain_prompt()

    def _drain_prompt(self):
        """
        消耗掉 JumpServer 的双重 Opt> 渲染，确保提示符完全稳定。
        JumpServer 输出 Opt> 后会用 ANSI 码擦除并重绘，第一个 expect("Opt>")
        只匹配了第一次，这里消耗掉后续可能的重绘。
        """
        child = self._child
        # 短暂等待，消耗剩余的 ANSI 输出（如果有）
        try:
            child.expect(PROMPT_OPT, timeout=1.5)  # 消耗第二次 Opt>
        except pexpect.TIMEOUT:
            pass  # 只有一次渲染也没关系
        time.sleep(0.3)  # 额外 buffer，确保服务端稳定

    def close(self):
        if self._child:
            try:
                self._child.close(force=True)
            except Exception:
                pass

    def select_and_exec(self, match_keyword: str, command: str) -> str:
        """
        在 JumpServer 搜索目标机器，连接后执行命令，返回命令输出。
        """
        child = self._child

        # 步骤1: 在稳定的 Opt> 发送搜索关键词（使用 send 而非 sendline，JumpServer 单字符模式）
        child.send(match_keyword + "\r")

        # 步骤2: 等待结果 —— 可能直连进去($/#)，可能需要选择([Host]>)，可能多个结果(Search:)
        shell_patterns = [r"\$\s", r"#\s"]
        patterns = [PROMPT_HOST, PROMPT_SEARCH, PROMPT_OPT] + shell_patterns
        idx = child.expect(patterns, timeout=self.t_expect)

        if idx == 0:
            # [Host]> 有多个匹配，选第一个
            child.send("1\r")
            idx2 = child.expect(shell_patterns + [pexpect.TIMEOUT], timeout=self.t_expect)
            if idx2 >= len(shell_patterns):
                raise RuntimeError(f"选择机器后未进入 shell，输出:\n{child.before}")
        elif idx == 1:
            # Search: 在资产列表的搜索框，再发一次关键词
            child.send(match_keyword + "\r")
            idx3 = child.expect(shell_patterns + [PROMPT_HOST, pexpect.TIMEOUT], timeout=self.t_expect)
            if idx3 == len(shell_patterns):   # [Host]>
                child.send("1\r")
                child.expect(shell_patterns, timeout=self.t_expect)
            elif idx3 > len(shell_patterns):
                raise RuntimeError(f"搜索后超时，输出:\n{child.before}")
        elif idx == 2:
            # 回到 Opt>，说明没找到
            raise RuntimeError(f"JumpServer 未找到匹配 '{match_keyword}' 的机器，请检查 match 关键词")
        # idx >= 3：直接进入 shell

        # 步骤3: 等待 shell 完全就绪（有时连接成功后还有欢迎信息，等其稳定）
        time.sleep(1.0)
        child.sendline("")   # 发一个空行触发提示符刷新
        # 等待出现 shell 提示符，flush 掉欢迎信息
        child.expect([r"\$\s*\r?\n", r"#\s*\r?\n", pexpect.TIMEOUT], timeout=8)

        # 步骤4: 执行命令，用唯一标记包裹
        # 使用经典技巧：发送 "echo 'JUMP_END_''12345'" 
        # 这样回显行里没有完整的连续字符串，而实际输出会拼成 "JUMP_END_12345"
        ts = int(time.time())
        end_marker = f"JUMP_END_{ts}"
        # 拆分为两部分
        part1 = "JUMP_END_"
        part2 = str(ts)
        full_cmd = f"{command}; echo '{part1}''{part2}'"
        child.sendline(full_cmd)

        # 步骤5: 等待命令完成（匹配唯一的真实输出）
        child.expect(re.escape(end_marker), timeout=self.t_cmd)

        # 步骤6: 提取输出（before 是 end_marker 之前的内容）
        raw = child.before or ""
        return self._clean(raw)


    @staticmethod
    def _clean(raw: str) -> str:
        """清理 ANSI 转义码和回车符"""
        ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\[[0-9;]*m|\r")
        cleaned = ansi.sub("", raw)
        # 去掉空行，strip 首尾空白
        lines = [line for line in cleaned.split("\n") if line.strip()]
        # 第一行通常是命令回显（echo），跳过
        if lines:
            lines = lines[1:]
        return "\n".join(lines).strip()


# ─── 子命令处理 ──────────────────────────────────────────────────────────────

def cmd_list(cfg: dict):
    allowed = cfg.get("allowed_hosts", [])
    if not allowed:
        fatal("配置文件中 allowed_hosts 为空")
    result = []
    for h in allowed:
        info = {"name": h["name"], "match": h["match"]}
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

    if not workdir and "default_workdir" in host:
        workdir = host["default_workdir"]

    if workdir:
        command = f"cd {workdir} && {command}"

    session = JumpSession(cfg)
    try:
        session.connect()
        output = session.select_and_exec(host["match"], command)
        print(json.dumps({
            "success": True,
            "host": host["name"],
            "match": host["match"],
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
