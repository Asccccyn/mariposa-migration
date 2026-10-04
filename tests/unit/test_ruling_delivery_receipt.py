"""裁定（2026-10-04 江乔生）：EXPLICIT_REJECT_AFTER_DELIVERY 的
"delivery" = 真实进入过模型侧可见的出站交付包。

- 拒绝真实交付过的候选 → 理由成立（回执绑定 delivered revision）；
- 拒绝仅内部 seen、从未出站的候选 → 理由不成立（gate 拒绝 Round2）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text):
    return memory.hold(
        actors["jiaming"], text=text, memory_date="2026-09-20",
        date_confidence="exact", original_title="dr",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False)


def _start(actors, terms, op):
    return registry.invoke(actors["jiaming"], "memory.recall.start",
                           {"query_plan": {
                               "original_request": "查话",
                               "channels": ["event"],
                               "lexical_terms": list(terms)},
                            "operation_id": op},
                           None)["data"]["data"]


class TestDeliveryReceiptSemantics:

    def test_reject_delivered_candidate_supports_reason(self, actors):
        _hold(actors, "交付拒绝理由正文话")
        p = _start(actors, ["正文话"], "dr-del-1")
        sid = p["recall_session_id"]
        cand = p["candidates"][0]
        refs = {r["resource_ref"] for r in store.list_receipts(sid)}
        assert cand["resource_ref"] in refs, \
            "前置：交付包候选必须有出站回执"
        with db.recall_runtime() as conn:
            rev = conn.execute(
                "SELECT revision FROM recall_receipts WHERE"
                " session_id=? AND resource_ref=?",
                (sid, cand["resource_ref"])).fetchone()["revision"]
        assert rev == 1, "回执必须绑定出站轮 revision"
        registry.invoke(actors["jiaming"], "memory.recall.reject", {
            "session_id": sid, "operation_id": "dr-rej-1",
            "resource_ref": cand["resource_ref"],
            "reject_target": "candidate"}, None)
        # Round2 以 EXPLICIT_REJECT_AFTER_DELIVERY 进入：理由有事实
        # 支撑（门在 raw 授权之前的 reason_fact_supported 已核过；
        # 本用例断言 gate 层判定，走 round2 真实入口会因无 raw 授权
        # 停在授权门——直接核内部 gate）
        from mariposa.recall import service as svc
        gate = svc._reason_fact_supported(
            sid, "EXPLICIT_REJECT_AFTER_DELIVERY", {})
        assert gate is True

    def test_reject_undelivered_seen_does_not_support(self, actors):
        _hold(actors, "内部seen拒绝理由正文话")
        p = _start(actors, ["正文话"], "dr-seen-1")
        sid = p["recall_session_id"]
        # 伪造一条只进内部池、从未出站的 rejected 候选
        with db.recall_runtime() as conn:
            conn.execute(
                "INSERT INTO recall_candidates(session_id,"
                " candidate_ref, resource_ref, channel, representation,"
                " state, first_seen_revision, updated_at)"
                " VALUES(?, 'cand-never-out', 'memory:never-out',"
                " 'event', 'full', 'rejected', 1, datetime('now'))",
                (sid,))
        from mariposa.recall import service as svc
        gate = svc._reason_fact_supported(
            sid, "EXPLICIT_REJECT_AFTER_DELIVERY", {})
        assert gate is False, "内部 seen 的拒绝不得背书该理由"
