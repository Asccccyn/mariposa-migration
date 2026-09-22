"""测试环境：必须在 import mariposa 之前固定 MARIPOSA_ROOT。

保险丝（B06）：测试会清空数据库；外部传入的 MARIPOSA_ROOT 若不在允许的
测试根目录内则 fail closed，防止误把业务/生产库当测试库清掉。
路径比较基于规范化真实路径（realpath），不做字符串前缀判断。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _real(path) -> Path:
    return Path(os.path.realpath(str(path)))


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


#: 允许的测试根目录（fail closed 白名单）：
#: 1) 系统临时目录（默认）；2) 项目内 pytest basetemp；
#: 3) runtime/verification 取证隔离目录（§16.1 模板）；
#: 4) MARIPOSA_TEST_ROOTS 显式白名单（分号分隔的规范化路径）。
def _test_root_allowed(raw_root: str) -> tuple[bool, str]:
    root = _real(raw_root)
    candidates = [
        (_real(tempfile.gettempdir()), "system temp dir"),
        (_real(_REPO_ROOT / ".pytest_tmp"), "project pytest basetemp"),
        (_real(_REPO_ROOT / "runtime" / "verification"),
         "runtime/verification evidence dir"),
    ]
    for base, why in candidates:
        if _is_within(root, base):
            return True, why
    for entry in os.environ.get("MARIPOSA_TEST_ROOTS", "").split(";"):
        if entry.strip() and root == _real(entry.strip()):
            return True, "MARIPOSA_TEST_ROOTS allowlist"
    return False, ""


def _enforce_test_root_fuse() -> None:
    raw = os.environ.get("MARIPOSA_ROOT")
    if not raw:
        return
    ok, why = _test_root_allowed(raw)
    if ok:
        return
    raise RuntimeError(
        f"MARIPOSA_ROOT={raw} 拒绝用作测试根目录（fail closed）："
        "测试会清空该目录下的数据库。允许的根：系统临时目录、"
        ".pytest_tmp、runtime/verification、或 MARIPOSA_TEST_ROOTS 白名单"
        "（分号分隔）。业务/生产库绝不能作为测试根。")


_enforce_test_root_fuse()

_TEST_ROOT = os.environ.setdefault(
    "MARIPOSA_ROOT", os.path.join(tempfile.mkdtemp(prefix="mariposa-test-"))
)

import pytest  # noqa: E402

from mariposa import db, schema  # noqa: E402
from mariposa.identity import service as identity  # noqa: E402

TOKENS = {"qiaosheng": "tok-q", "jiaming": "tok-j", "worker": "tok-w",
          "linshijian": "tok-l"}

FORMAL_TABLES = [
    # 子表在前，父表在后
    "i_suggestions", "i_versions", "i_documents",
    "review_delegations",
    "memory_summary_versions", "memory_retention",
    "memory_recollections", "memory_view_receipts", "memory_our_words",
    "memory_mood_tags", "memory_moods", "memory_categories",
    "forgetting_due_queue",
    "anniversary_occurrences", "anniversary_definitions",
    "audit_events", "events_outbox", "idempotency_records", "proposal_resolutions",
    "proposal_envelopes", "search_fts", "retrieval_documents", "memory_versions",
    "memories", "client_bindings", "principals",
    "raw_messages", "raw_conversations",
    "quote_versions", "quotes",
    "handoffs",
    "plan_memory_links", "plan_versions", "plans",
    "activity_events",
    "letter_versions", "letters", "deletion_requests",
    "home_versions", "home", "self_versions", "self_entries",
    "diary_versions", "diary_entries", "memory_tags", "bootstrap_snapshots",
    "memory_relations", "memory_raw_refs", "provisional_sources",
    "reminders", "media_objects", "moment_reactions", "moment_comments",
    "moment_versions", "moments", "rejection_suppression",
    "memory_meanings", "stickers", "import_jobs",
    "memory_reengagements", "migration_id_map",
]
WORKSPACE_TABLES = [
    "v2_proposal_versions", "v2_review_items",
    "workspace_audit", "worker_runs", "proposal_versions", "work_items",
    "workspace_task_leases",
]


def reset_all() -> None:
    schema.migrate()
    with db.formal() as c:
        c.execute("PRAGMA foreign_keys=OFF")
        for t in FORMAL_TABLES:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys=ON")
    with db.workspace() as c:
        c.execute("PRAGMA foreign_keys=OFF")
        for t in WORKSPACE_TABLES:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys=ON")
    identity.seed(TOKENS)


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "binding_qiaosheng"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "binding_jiaming"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "binding_worker"),
    }
