"""WP06：burst 真实计数与输出预算（S16/S17）。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import budget, service as recall_service, store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold(principal, text):
    return memory.hold(principal, text=text, memory_date="2026-09-25",
                       date_confidence="exact", original_title="wp06",
                       categories=["daily"],
                       creation_mode="contemporaneous", raw_pending=False)


class TestBurstRealCounting:
    def test_early_burst_not_squeezed(self, actors):
        """S17：提前开启的新 burst 按自身记录计数——burst1 只用了
        1 轮时，burst2 仍有完整轮数（公式推断会虚增已用）。"""
        from mariposa import config as cfg
        p = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-auto-test_c-6","query_plan": {
                                "original_request": "查",
                                "channels": ["event"],
                                "lexical_terms": ["灯火"]}}, None)
        sid = p["data"]["data"]["recall_session_id"]
        # 显式 continue 开新 burst（burst1 仅 1 轮）——ref 用服务端
        # 随 start 签发的接续引用（RECALL-02）
        r2 = registry.invoke(actors["jiaming"], "memory.recall.refine",
                             { "operation_id": "op-auto-test_c-5","session_id": sid,
                              "continue_request_ref": p["data"]["data"][
                                  "continuation"]["continue_request_ref"],
                              "query_plan": {
                                  "original_request": "再查",
                                  "channels": ["event"],
                                  "lexical_terms": ["灯火"]}}, None)
        assert r2["data"]["data"]["revision"] == 2
        session = store.require_session(sid)
        assert session["current_burst"] == 2
        with db.recall_runtime() as conn:
            used_b2 = conn.execute(
                "SELECT COUNT(*) c FROM recall_rounds WHERE session_id=?"
                " AND burst_no=2", (sid,)).fetchone()["c"]
        assert used_b2 == 1
        # 剩余 = 上限 - burst2 真实已用（公式法会算成 上限-2）
        left = budget.rounds_left_in_burst(session)
        assert left == cfg.RECALL_BURST_ROUNDS - 1

    def test_burst_two_acceptance(self, actors, monkeypatch):
        """00 指令：新隔离会话按原 v1.7 每 burst 2 轮验收（生产
        数值切换单独呈报，config 默认不动）。"""
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "RECALL_BURST_ROUNDS", 2)
        hold(actors["jiaming"], "验收场景正文台灯")
        p = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-auto-test_c-4","query_plan": {
                                "original_request": "查台灯",
                                "channels": ["event"],
                                "lexical_terms": ["台灯"]}}, None)
        sid = p["data"]["data"]["recall_session_id"]
        r2 = registry.invoke(actors["jiaming"], "memory.recall.refine",
                             { "operation_id": "op-auto-test_c-3","session_id": sid,
                              "query_plan": {
                                  "original_request": "再查",
                                  "channels": ["event"],
                                  "lexical_terms": ["台灯"]}}, None)
        assert r2["data"]["data"]["status"] != "BUDGET_EXHAUSTED"
        # burst2（上限 2）已用 2 轮：第三次 refine 无 continue → 拒
        r3 = registry.invoke(actors["jiaming"], "memory.recall.refine",
                             { "operation_id": "op-auto-test_c-2","session_id": sid,
                              "query_plan": {
                                  "original_request": "三查",
                                  "channels": ["event"],
                                  "lexical_terms": ["台灯"]}}, None)
        assert r3["data"]["data"]["status"] == "BUDGET_EXHAUSTED"


class TestOutputBudget:
    def test_json_bytes_actually_enforced(self, actors):
        """S16：完整 packet UTF-8 序列化 ≤24576 字节实际生效。"""
        import json
        hold(actors["jiaming"], "预算" * 900)  # 长正文
        p = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-auto-test_c-1","query_plan": {
                                "original_request": "查预算",
                                "channels": ["event"],
                                "lexical_terms": ["预算"]}}, None)
        packet = p["data"]["data"]
        blob = json.dumps(packet, ensure_ascii=False).encode("utf-8")
        assert len(blob) <= 24576, \
            f"S16：packet 序列化超限 {len(blob)}"
        limits = packet["budget"]["output_limits"]
        assert limits["json_bytes"] == 24576

    def test_snippet_window_600(self, actors):
        hold(actors["jiaming"], "长文" * 500 + "结尾标记词" + "尾" * 50)
        p = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-auto-test_c-0","query_plan": {
                                "original_request": "查长文",
                                "channels": ["event"],
                                "lexical_terms": ["长文"]}}, None)
        for c in p["data"]["data"]["candidates"]:
            for ev in c.get("evidence") or []:
                if isinstance(ev.get("snippet"), str):
                    assert len(ev["snippet"]) <= 600, \
                        "S16：单候选文本 ≤600 code points"
