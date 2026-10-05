#!/usr/bin/env python3
"""设置 mariposa 登录密码（OAuth 动态授权，2026-10-05）。

两个密码各映射一个身份：
  1. 网页登录密码 → qiaosheng（江乔生）
  2. MCP 密码     → jiaming（周家明 MCP 连接时输入）

密码经 getpass 输入（不回显、不落屏、不落命令行），库内只存
PBKDF2-SHA256 慢哈希。重复运行即改密。
用法：MARIPOSA_ROOT=<生产根> .venv/bin/python scripts/set_passwords.py
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))

from mariposa import oauth  # noqa: E402
from mariposa import schema  # noqa: E402

schema.migrate()
schema.migrate_runtime()

PROMPTS = [
    ("qiaosheng", "网页登录密码（你自己，qiaosheng）"),
    ("jiaming", "MCP 密码（周家明 MCP 连接，jiaming）"),
]
for principal_id, label in PROMPTS:
    while True:
        pw = getpass.getpass(f"设置{label}：")
        if len(pw) < 8:
            print("至少 8 个字符，请重试")
            continue
        pw2 = getpass.getpass("再输一遍确认：")
        if pw != pw2:
            print("两次不一致，请重试")
            continue
        break
    oauth.set_password(principal_id, pw)
    print(f"✓ {principal_id} 密码已保存（哈希落库）")
print("完成。网页请用登录密码，MCP 客户端连接时输入 MCP 密码。")
