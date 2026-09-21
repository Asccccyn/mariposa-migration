"""mariposa 配置。

所有值可由环境变量覆盖；默认值来自《mariposa § 01 工程执行文档》§21，
均为【默认】初值，不是已冻结决策。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("MARIPOSA_ROOT", r"D:\mariposa"))

RUNTIME_DIR = PROJECT_ROOT / "runtime"
FORMAL_DB = RUNTIME_DIR / "formal" / "mariposa.sqlite3"
WORKSPACE_DB = RUNTIME_DIR / "workspace" / "workspace.sqlite3"
LOG_DIR = RUNTIME_DIR / "logs"
ENV_FILE = PROJECT_ROOT / ".env"

HTTP_BIND = os.environ.get("MARIPOSA_BIND", "127.0.0.1")
HTTP_PORT = int(os.environ.get("MARIPOSA_PORT", "18780"))

RELATIONSHIP_TIMEZONE = os.environ.get("MARIPOSA_TZ", "Asia/Shanghai")

PROJECTION_REVISION = "retrieval_projection_v1"
POLICY_VERSION = "forget_policy_v1"

FORGET_IDLE_DAYS = int(os.environ.get("MARIPOSA_FORGET_IDLE_DAYS", "30"))
FORGET_SCHEDULE_ENABLED = False  # 初期无自动扫描；只能手动触发 scan
FORGET_SCAN_BATCH_SIZE = 20

SEMANTIC_PROVIDER = os.environ.get("MARIPOSA_SEMANTIC_PROVIDER", "")  # 空 = 未配置
QUOTE_SEMANTIC_AUTO_APPLY = os.environ.get(
    "MARIPOSA_QUOTE_SEMANTIC_AUTO_APPLY", "true").lower() in ("1", "true", "yes")

CONTRACT_VERSION = "1.0"


def ensure_dirs() -> None:
    for p in (RUNTIME_DIR, FORMAL_DB.parent, WORKSPACE_DB.parent, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)
