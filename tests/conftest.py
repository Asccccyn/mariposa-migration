"""测试环境：必须在 import mariposa 之前固定 MARIPOSA_ROOT。"""
from __future__ import annotations

import os
import tempfile

_TEST_ROOT = os.environ.setdefault(
    "MARIPOSA_ROOT", os.path.join(tempfile.mkdtemp(prefix="mariposa-test-"))
)

import pytest  # noqa: E402

from mariposa import db, schema  # noqa: E402
from mariposa.identity import service as identity  # noqa: E402

TOKENS = {"qiaosheng": "tok-q", "jiaming": "tok-j", "worker": "tok-w"}

FORMAL_TABLES = [
    # 子表在前，父表在后
    "audit_events", "events_outbox", "idempotency_records", "proposal_resolutions",
    "proposal_envelopes", "search_fts", "retrieval_documents", "memory_versions",
    "memories", "client_bindings", "principals",
    "raw_messages", "raw_conversations",
    "quote_versions", "quotes",
    "handoffs",
    "plan_memory_links", "plan_versions", "plans",
    "activity_events",
]
WORKSPACE_TABLES = [
    "workspace_audit", "worker_runs", "proposal_versions", "work_items",
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
