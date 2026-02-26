#!/usr/bin/env python3
"""debug v3: 打印 end_marker 之前的原始 before 内容"""
import pexpect, yaml, time, re, sys
from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent
cfg = yaml.safe_load(open(SKILL_DIR / "resources" / "config.yaml"))
js = cfg["jumpserver"]

ssh_cmd = (f"ssh -p {js['port']} -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
           f"{js['user']}@{js['host']}")
child = pexpect.spawn(ssh_cmd, encoding="utf-8", codec_errors="replace",
                      timeout=30, dimensions=(50, 220))

# 登录
child.expect("password:", timeout=20)
child.sendline(js["password"])
child.expect("Opt>", timeout=15)
# drain 双重 Opt>
try: child.expect("Opt>", timeout=1.5)
except pexpect.TIMEOUT: pass
time.sleep(0.3)

# 搜索
child.send("4.13\r")
idx = child.expect([r"\$\s", r"#\s", "Opt>", pexpect.TIMEOUT], timeout=15)
print(f"after search: idx={idx}, before={repr(child.before[-500:])}", file=sys.stderr)

# 如果进了 shell
if idx in (0, 1):
    time.sleep(1.0)
    child.sendline("")
    child.expect([r"\$\s*\r?\n", r"#\s*\r?\n", pexpect.TIMEOUT], timeout=8)

    end_marker = f"__JUMP_END_{int(time.time())}__"
    cmd = "kubectl get namespace"
    child.sendline(f"{cmd}; echo '{end_marker}'")
    child.expect(re.escape(end_marker), timeout=30)

    raw = child.before or ""
    print(f"\n===RAW BEFORE (repr)===\n{repr(raw[:2000])}", file=sys.stderr)
    print(f"\n===RAW BEFORE (text)===\n{raw[:2000]}", file=sys.stderr)
else:
    print(f"没有进入 shell，idx={idx}", file=sys.stderr)

child.close()
