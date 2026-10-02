"""WP00 负向探针：selection 出站硬门（recall-closure-20260930）。

包内合成探针复现的三个缺陷的应用级钉子：
- unjudged 候选不得进入交付（S10：未判断不能填满前三）
- invalid 但携带数字 relevance 的候选不得进入交付（S10：缺项/错误
  type/格式错不是低分）
- 低分 evaluated 候检按分排序交付并标 needs_validation（S11
  rank_only_until_calibrated 正向钉子，不是缺陷）
- provider 全失败/未配置：搜索候选正文不得直出（S10）
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.retrieval import selection
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _card(ref, judge=None, rrf=0.5, evidence=None):
    return {
        "resource_ref": ref, "candidate_ref": ref,
        "channel": "event", "representation": "full",
        "content_version": "1", "representation_version": "1",
        "matched_by": ["keyword"], "matched_fields": ["event_text"],
        "rrf_score": rrf, "judge": judge,
        "evidence": evidence or [],
    }


class TestSelectionHardGate:
    """对 selection.select 的直接探针（当前红 → WP01 转绿）。"""

    def test_unjudged_candidate_must_not_deliver(self):
        plan = {"original_request": "x", "channels": ["event"]}
        out = selection.select([_card("memory:a", judge=None)], plan,
                               set())
        assert not out["delivered"], \
            "S10：未判断候选不得填满交付位"

    def test_unavailable_judge_must_not_deliver(self):
        plan = {"original_request": "x", "channels": ["event"]}
        j = {"evaluation_status": "unavailable", "relevance_signal": None}
        out = selection.select([_card("memory:a", judge=j)], plan, set())
        assert not out["delivered"], "S10：judge unavailable 不得交付"

    def test_invalid_with_numeric_score_must_not_deliver(self):
        plan = {"original_request": "x", "channels": ["event"]}
        j = {"evaluation_status": "invalid", "relevance_signal": 0.99}
        out = selection.select([_card("memory:a", judge=j)], plan, set())
        assert not out["delivered"], \
            "S10：invalid 即使带数字分也不是低分，不得交付"

    def test_nan_and_out_of_range_scores_are_not_low_scores(self):
        """非法分值（NaN/inf/越界/布尔）不是低分；合法负分（-0.5，
        JEV-03 值域内）是 evaluated，按分排序垫底交付。"""
        plan = {"original_request": "x", "channels": ["event"]}
        for bad in (float("nan"), float("inf"), 1.7, True):
            j = {"evaluation_status": "evaluated", "relevance_signal": bad}
            out = selection.select([_card("memory:a", judge=j)], plan,
                                   set())
            assert not out["delivered"], f"S10：非法分值 {bad!r} 不得交付"
        j_neg = {"evaluation_status": "evaluated",
                 "relevance_signal": -0.5}
        out = selection.select(
            [_card("memory:neg", judge=j_neg, rrf=0.9)], plan, set())
        assert len(out["delivered"]) == 1, "合法负分不删除（rank_only）"

    def test_low_score_evaluated_delivers_rank_only(self):
        """S11 正向钉子：低分 evaluated 候选按分排序交付，标
        needs_validation；不得自动删除或冒充高分。"""
        plan = {"original_request": "x", "channels": ["event"]}
        j_low = {"evaluation_status": "evaluated", "relevance_signal": 0.01}
        j_mid = {"evaluation_status": "evaluated", "relevance_signal": 0.4}
        out = selection.select(
            [_card("memory:low", judge=j_low, rrf=0.9),
             _card("memory:mid", judge=j_mid, rrf=0.1)], plan, set())
        assert [c["resource_ref"] for c in out["delivered"]] == \
            ["memory:mid", "memory:low"], "按 judge 分排序（高在前）"
        assert out["delivery_action"] == "needs_validation"

    def test_judge_version_mismatch_must_not_deliver(self):
        """S18/WP01：judge 判断与候选实际版本不一致时不得出站。"""
        plan = {"original_request": "x", "channels": ["event"]}
        j = {"evaluation_status": "evaluated", "relevance_signal": 0.9,
             "candidate_version": "1"}
        stale = _card("memory:a", judge=j)
        stale["content_version"] = "2"  # 候选已前进到新版本
        out = selection.select([stale], plan, set())
        assert not out["delivered"], "旧版本判断不得出站"


class TestProviderFailureNoBodyLeak:
    def test_judge_unconfigured_returns_no_body(self, actors,
                                                monkeypatch):
        """S10：Jev 未配置/全失败时，搜索候选正文不得直出。"""
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "RECALL_JUDGE_PROVIDER", "disabled")
        from mariposa.memory import service as msvc
        msvc.hold(actors["jiaming"], text="禁出正文的海雾鸥影",
                  memory_date="2026-09-25", date_confidence="exact",
                  original_title="禁出", categories=["daily"],
                  creation_mode="contemporaneous", raw_pending=False)
        # judge provider 未配置（默认 DisabledJudge → unavailable）
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-auto-test_c-0","query_plan": {
                                "original_request": "海雾鸥影",
                                "channels": ["event"],
                                "lexical_terms": ["海雾鸥影"]}}, None)
        packet = r["data"]["candidates"] if "candidates" in r["data"] \
            else r["data"]["data"]["candidates"]
        assert packet == [], \
            "S10：Jev 不可用时搜索候选正文不得直出"
        # 结构化降级必须可见
        top = r["data"] if "candidates" in r["data"] else r["data"]["data"]
        assert (top.get("coverage", {}).get("judge") in
                ("not_configured", "unavailable")
                or "judge_" in " ".join(top.get("degraded_reasons") or [])), \
            "降级原因必须结构化呈现"
