"""mariposa 配置。

所有值可由环境变量覆盖；默认值来自《mariposa § 01 工程执行文档》§21，
均为【默认】初值，不是已冻结决策。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# 数据根（v1.4 §10.1）：Mac/Linux 必须显式 MARIPOSA_ROOT，不回退 Windows
# 默认路径——错误路径应启动失败，不静默在别处建一个空正式库。
# Windows 保留旧默认 D:\mariposa（既有生产根）。
_root_env = os.environ.get("MARIPOSA_ROOT")
if _root_env:
    PROJECT_ROOT = Path(_root_env)
elif sys.platform == "win32":
    PROJECT_ROOT = Path(r"D:\mariposa")
else:
    raise RuntimeError(
        "MARIPOSA_ROOT 未设置：非 Windows 环境必须显式指定数据根"
        "（v1.4 §10.1 fail-fast；测试由 conftest 自动设置临时隔离根）。")

RUNTIME_DIR = PROJECT_ROOT / "runtime"
FORMAL_DB = RUNTIME_DIR / "formal" / "mariposa.sqlite3"
WORKSPACE_DB = RUNTIME_DIR / "workspace" / "workspace.sqlite3"
# Recall Session 等短期但需跨重启的运行状态（v1.4 §10.2；TTL 清理有界，
# 与正式库/工作区库相互独立）。
RECALL_DB = RUNTIME_DIR / "recall" / "recall.sqlite3"
LOG_DIR = RUNTIME_DIR / "logs"
ENV_FILE = PROJECT_ROOT / ".env"

# 新建数据库必须显式允许（OPS-RECALL-01）：测试根由 conftest 打开；
# 生产首次建库/换根走显式 MARIPOSA_ALLOW_CREATE=1，防止错误路径静默
# 建空库。
ALLOW_DB_CREATE = _flag("MARIPOSA_ALLOW_CREATE")

HTTP_BIND = os.environ.get("MARIPOSA_BIND", "127.0.0.1")
HTTP_PORT = int(os.environ.get("MARIPOSA_PORT", "18780"))

RELATIONSHIP_TIMEZONE = os.environ.get("MARIPOSA_TZ", "Asia/Shanghai")

PROJECTION_REVISION = "retrieval_projection_v1"
POLICY_VERSION = "forget_policy_v1"

FORGET_IDLE_DAYS = int(os.environ.get("MARIPOSA_FORGET_IDLE_DAYS", "30"))
FORGET_SCHEDULE_ENABLED = False  # 初期无自动扫描；只能手动触发 scan
FORGET_SCAN_BATCH_SIZE = 20
FORGET_REJECT_COOLDOWN_DAYS = int(
    os.environ.get("MARIPOSA_FORGET_REJECT_COOLDOWN_DAYS", "30"))

SEMANTIC_PROVIDER = os.environ.get("MARIPOSA_SEMANTIC_PROVIDER", "")  # 空 = 未配置
QUOTE_SEMANTIC_AUTO_APPLY = os.environ.get(
    "MARIPOSA_QUOTE_SEMANTIC_AUTO_APPLY", "true").lower() in ("1", "true", "yes")

CONTRACT_VERSION = "1.0"

# ---------- 召回运行时（v1.4；初值为隔离评测保护参数，非业务终值） ----------
# 各开关默认关闭，完成隔离验收后分项启用（§15.1）。
RECALL_RUNTIME_ENABLED = _flag("MARIPOSA_RECALL_ENABLED")
RECALL_WORDS_ENABLED = _flag("MARIPOSA_WORDS_RECALL_ENABLED")
RECALL_RAW_FALLBACK_ENABLED = _flag("MARIPOSA_RAW_FALLBACK_ENABLED")
# 未决业务项（v1.3 §23）：遗忘后的 words 显式检索保持 disabled，
# decision_state=PENDING_OWNER_DECISION；确认前不得借任何旁路恢复。
WORDS_FORGOTTEN_RECALL = "disabled"
WORDS_FORGOTTEN_DECISION_STATE = "PENDING_OWNER_DECISION"

RECALL_POLICY_VERSION = "recall-v1.4-eval-1"
WORDS_PROJECTION_VERSION = "words-projection-v1"
RECALL_SESSION_TTL_HOURS = int(
    os.environ.get("MARIPOSA_RECALL_SESSION_TTL_HOURS", "24"))
RECALL_BURST_ROUNDS = 3                # 一个补查 burst：初次 + 最多 2 轮补查
RECALL_SESSION_BURSTS_MAX = 3
RECALL_ALTERNATE_PER_BURST = 2
RECALL_RELATION_HOPS = 1
RECALL_RELATION_NEIGHBOR_CAP = 10
RECALL_LEXICAL_K = 20
RECALL_DENSE_K = 20
RECALL_CANDIDATE_UNION_CAP = 60
RECALL_JUDGE_CANDIDATE_CAP = 40
RECALL_JUDGE_HTTP_ATTEMPTS = 40
RECALL_DELIVERY_LIMIT = 3
RECALL_AUTO_TOP1 = False                # 未校准前禁用"自动高置信单条"
RECALL_EXCERPT_CHARS = 600
RECALL_PACKET_TEXT_CHARS = 4000
RECALL_PACKET_BYTES_MAX = 24576
RECALL_RRF_K = 60
RECALL_TOKENIZER = "unavailable"        # 主模型 tokenizer 接入后替换

RECALL_JUDGE_PROVIDER = os.environ.get(
    "MARIPOSA_RECALL_JUDGE_PROVIDER", "disabled")  # disabled | typesafe_jev
RECALL_JUDGE_MODEL_ID = os.environ.get("MARIPOSA_RECALL_JUDGE_MODEL_ID", "")
RECALL_JUDGE_PROMPT_VERSION = "mariposa-relevance-v1"
RECALL_JUDGE_CONCURRENCY = 2
RECALL_JUDGE_TIMEOUT_MS = int(
    os.environ.get("MARIPOSA_RECALL_JUDGE_TIMEOUT_MS", "5000"))
RECALL_JUDGE_ROUND_DEADLINE_MS = 20000


def ensure_dirs() -> None:
    for p in (RUNTIME_DIR, FORMAL_DB.parent, WORKSPACE_DB.parent,
              RECALL_DB.parent, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)
