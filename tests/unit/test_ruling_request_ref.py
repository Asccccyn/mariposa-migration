"""裁定（2026-10-04 江乔生）：request_ref 幂等 + 线性接续回归。

- request_ref = 一次具体请求的幂等身份：同 ref 同 payload 重放原
  结果（start 回原 session、不重新领预算）；同 ref 异 payload
  REF_REUSE_MISMATCH。
- continue_request_ref 只表示接续点：每 session 消费一次，旧 ref
  再申领 burst 判 stale；同 request_ref 网络重试走幂等重放不受影响。
"""
from __future__ import annotations

import pytest

from mariposa import config as cfg
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, StaleOperation
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
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
        date_confidence="exact", original_title="rr",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False)


PLAN_A = {"original_request": "找窗帘", "channels": ["event"],
          "lexical_terms": ["窗帘"], "request_ref": "rr-alpha"}
PLAN_B = {"original_request": "改找别的", "channels": ["event"],
          "lexical_terms": ["别的"], "request_ref": "rr-alpha"}


class TestRequestRefIdempotency:

    def test_same_ref_same_payload_replays_original_session(self, actors):
        _hold(actors, "窗帘事件的正文")
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": PLAN_A}, None)["data"]["data"]
        sid = r1["recall_session_id"]
        r2 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": dict(PLAN_A)}, None)["data"]["data"]
        assert r2["recall_session_id"] == sid, \
            "同 request_ref 重放必须回原 session，不得新建"

    def test_same_ref_different_payload_rejected(self, actors):
        _hold(actors, "窗帘事件的正文")
        registry.invoke(actors["jiaming"], "memory.recall.start",
                        {"query_plan": PLAN_A}, None)
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.recall.start",
                            {"query_plan": PLAN_B}, None)
        assert ei.value.code == "REF_REUSE_MISMATCH"

    def test_replay_does_not_grant_new_budget(self, actors):
        """同 ref 重放不重新计算——session 的 rounds/burst 不变。"""
        from mariposa.recall import store
        _hold(actors, "预算重放正文")
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": PLAN_A}, None)["data"]["data"]
        sid = r1["recall_session_id"]
        before = store.get_session(sid)
        registry.invoke(actors["jiaming"], "memory.recall.start",
                        {"query_plan": dict(PLAN_A)}, None)
        after = store.get_session(sid)
        assert (before["current_revision"], before["bursts_used"],
                before["rounds_used"]) == \
            (after["current_revision"], after["bursts_used"],
             after["rounds_used"]), "重放不得产生新额度/新轮次"


class TestLinearContinuation:

    def test_old_continue_ref_is_stale(self, actors, monkeypatch):
        monkeypatch.setattr(cfg, "RECALL_BURST_ROUNDS", 1)
        _hold(actors, "线性接续正文")
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": {
                                 "original_request": "找窗帘",
                                 "channels": ["event"],
                                 "lexical_terms": ["窗帘"],
                                 "request_ref": "lc-start"}}, None)["data"]["data"]
        sid = r1["recall_session_id"]
        # burst1 轮次已用（BURST_ROUNDS=1）→ refine 需要 continue_ref
        r2 = registry.invoke(actors["jiaming"], "memory.recall.refine", {
            "session_id": sid,
            "query_plan": {"original_request": "再找窗帘",
                           "channels": ["event"],
                           "lexical_terms": ["窗帘"]},
            "continue_request_ref": "cont-one",
            "request_ref": "lc-r1"}, None)["data"]["data"]
        assert r2["revision"] == 2, "第一次接续应成功领 burst2"
        # burst2 也用尽后：旧 cont-one 再申领 → stale；新 ref 可续
        with pytest.raises(StaleOperation):
            registry.invoke(actors["jiaming"], "memory.recall.refine", {
                "session_id": sid,
                "query_plan": {"original_request": "三找窗帘",
                               "channels": ["event"],
                               "lexical_terms": ["窗帘"]},
                "continue_request_ref": "cont-one",
                "request_ref": "lc-r2-old"}, None)
        r3 = registry.invoke(actors["jiaming"], "memory.recall.refine", {
            "session_id": sid,
            "query_plan": {"original_request": "三找窗帘",
                           "channels": ["event"],
                           "lexical_terms": ["窗帘"]},
            "continue_request_ref": "cont-two",
            "request_ref": "lc-r2-new"}, None)["data"]["data"]
        assert r3["revision"] == 3

    def test_same_request_ref_retry_replays_not_double_consume(
            self, actors, monkeypatch):
        """同 request_ref 的网络重试走 operation 重放——不会二次
        消费 continue_ref，也不会再领一个 burst。"""
        from mariposa.recall import store
        monkeypatch.setattr(cfg, "RECALL_BURST_ROUNDS", 1)
        _hold(actors, "重试不双耗正文")
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": {
                                 "original_request": "找窗帘",
                                 "channels": ["event"],
                                 "lexical_terms": ["窗帘"],
                                 "request_ref": "rc-start"}}, None)["data"]["data"]
        sid = r1["recall_session_id"]
        args = {"session_id": sid,
                "query_plan": {"original_request": "再找窗帘",
                               "channels": ["event"],
                               "lexical_terms": ["窗帘"]},
                "continue_request_ref": "cont-x",
                "request_ref": "rc-r1"}
        r2 = registry.invoke(actors["jiaming"], "memory.recall.refine",
                             args, None)["data"]["data"]
        bursts_after_first = store.get_session(sid)["bursts_used"]
        r2b = registry.invoke(actors["jiaming"], "memory.recall.refine",
                              dict(args), None)["data"]["data"]
        assert r2b["revision"] == r2["revision"], "重试应重放原结果"
        assert store.get_session(sid)["bursts_used"] == bursts_after_first
