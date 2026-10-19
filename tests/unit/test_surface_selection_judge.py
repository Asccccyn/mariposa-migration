"""surface 严格选择与两阶段 judge 适配器（TypeSafe 有限问句归一）。"""
from __future__ import annotations

import json

import pytest

from mariposa import config
from mariposa.surfacing import judge as surface_judge
from mariposa.surfacing import selection as sel
from mariposa.surfacing.models import SurfaceBlocked, surface_policy_snapshot
from tests.conftest import reset_all


@pytest.fixture(autouse=True)
def _env():
    reset_all()
    surface_judge.clear_transport_override()
    yield
    surface_judge.clear_transport_override()


def _policy(**kw):
    p = surface_policy_snapshot()
    p.update(kw)
    return p


def _pool_entry(ref="memory:m1", text="事件正文", **kw):
    entry = {
        "resource_ref": ref, "memory_id": ref.split(":")[1],
        "content_version": "1", "memory_date": "2026-07-09",
        "date_confidence": "exact", "occurred_start": None,
        "occurred_end": None, "original_title": "t", "categories": [],
        "event_text": text, "body_hash": "h" * 64, "body_bytes": 99,
        "domain_versions": {"memory": "1"}, "evidence": [
            {"kind": "authored_event"}], "rrf_score": 1.0,
        "hypotheses": ["h1"],
    }
    entry.update(kw)
    return entry


def _asm(event, ref, verdict="confirmed", rank=0.99, checks=None):
    base = {"same_occurrence": True, "participants_consistent": True,
            "time_status_consistent": True, "evidence_sufficient": True,
            "ambiguity_resolved": True, "relevant_now": True}
    if checks:
        base.update(checks)
    return {"event_id": event, "candidate_ref": ref, "verdict": verdict,
            "checks": base, "rank_signal": rank,
            "reason_code": "EXACT_MATCH" if verdict == "confirmed"
            else "INSUFFICIENT_EVIDENCE"}


class TestStrictSelection:
    def test_one_winner_per_event_no_runnerup(self):
        pool = [_pool_entry("memory:a", hypotheses=["h1"]),
                _pool_entry("memory:b", hypotheses=["h1"])]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"],
                   "grouping_confirmed": True}]
        # a 确认、b 失败 → 只选 a；不给 b 补位
        asm = [_asm("ev_1", "memory:a"),
               _asm("ev_1", "memory:b", verdict="rejected")]
        winners, abstain = sel.choose_winners(groups, asm, pool)
        assert [w["memory_id"] for w in winners] == ["a"]
        assert abstain == {}

    def test_winner_failed_no_fallback(self):
        pool = [_pool_entry("memory:a"), _pool_entry("memory:b")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"],
                   "grouping_confirmed": True}]
        asm = [_asm("ev_1", "memory:a", verdict="rejected"),
               _asm("ev_1", "memory:b")]   # b 其实过了——但 a 才是最高
        # rank 高者失败：另一确认者是否补位？——规则：只有确认者参与
        # 排序，b 是确认者且无更高确认者 → b 胜（这里 b 合法胜出）
        winners, _ = sel.choose_winners(groups, asm, pool)
        assert [w["memory_id"] for w in winners] == ["b"]

    def test_uncertain_competitor_suppresses_event(self):
        pool = [_pool_entry("memory:a"), _pool_entry("memory:b")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"],
                   "grouping_confirmed": True}]
        asm = [_asm("ev_1", "memory:a"),
               _asm("ev_1", "memory:b", verdict="uncertain")]
        winners, abstain = sel.choose_winners(groups, asm, pool)
        assert winners == []
        assert abstain["ev_1"] == "SURFACE_EVENT_AMBIGUOUS"

    def test_unconfirmed_grouping_abstains(self):
        pool = [_pool_entry("memory:a", hypotheses=["h1", "h2"])]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1", "h2"],
                   "grouping_confirmed": False}]
        winners, abstain = sel.choose_winners(
            groups, [_asm("ev_1", "memory:a")], pool)
        assert winners == []
        assert abstain["ev_1"] == "SURFACE_EVENT_AMBIGUOUS"

    def test_rank_not_confirmation(self):
        """分数不是确认：verdict 非 confirmed 的最高分不入选。"""
        pool = [_pool_entry("memory:a"), _pool_entry("memory:b")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"],
                   "grouping_confirmed": True}]
        asm = [_asm("ev_1", "memory:a", verdict="uncertain", rank=1.0),
               _asm("ev_1", "memory:b", verdict="uncertain", rank=0.1)]
        winners, abstain = sel.choose_winners(groups, asm, pool)
        assert winners == []

    def test_tie_break_stable_by_ref(self):
        pool = [_pool_entry("memory:zz"), _pool_entry("memory:aa")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"],
                   "grouping_confirmed": True}]
        asm = [_asm("ev_1", "memory:zz", rank=0.99),
               _asm("ev_1", "memory:aa", rank=0.99)]
        winners, _ = sel.choose_winners(groups, asm, pool)
        assert winners[0]["memory_id"] == "aa"   # 稳定 ID 打破同分

    def test_finalize_prepare_skips_oversize_body(self):
        big = "字" * 30000
        winners = [{"event_id": "ev_1", "event_text": big,
                    "resource_ref": "memory:a"},
                   {"event_id": "ev_2", "event_text": "ok",
                    "resource_ref": "memory:b"}]
        out = sel.finalize_prepare(winners, _policy())
        assert out[0]["skipped_reason"] == "SURFACE_BODY_TOO_LARGE"
        assert out[1]["skipped_reason"] is None


class FakeTransport:
    """正向问句答 value；反向问句（ambiguity_risk）答 risk_value。"""

    def __init__(self, value=0.99, overrides=None, risk_value=0.01):
        self.calls: list[dict] = []
        self.value = value
        self.risk_value = risk_value
        self.overrides = overrides or {}

    def __call__(self, payload):
        self.calls.append(payload)
        answers = {}
        for k in payload["questions"]:
            v = self.overrides.get(
                k, self.risk_value if "ambiguity_risk" in k else self.value)
            answers[k] = {"noul": v}
        return {"answers": answers, "model": "fake"}


class TestSurfaceJudge:
    def test_profile_unsupported_for_non_typesafe(self):
        with pytest.raises(SurfaceBlocked) as ei:
            surface_judge.SurfaceJudge("codex_sdk", _policy())
        assert ei.value.code == "SURFACE_PROFILE_UNSUPPORTED"

    def test_prepare_confirmed_flow_and_question_shape(self):
        transport = FakeTransport(0.99)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m", "revision": 1, "text": "7.9记忆库做成了",
                  "body_hash": "b" * 64}
        hyp = [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
                "inherited_date_span": None}]
        pool = [_pool_entry("memory:m1")]
        decision = sj.judge_prepare(source, [], hyp, pool)
        assert decision.phase == "prepare"
        assert decision.assessments[0].strictly_confirmed()
        # 有限问句：五项检查问句全部出站，无自由生成
        q = transport.calls[0]["questions"]
        assert len(q) == 5
        assert all(v["type"] == "noul" for v in q.values())
        # 出站候选带完整正文（judge 必须读全文）
        state = transport.calls[0]["state"]
        assert state["candidates"][0]["event_text"] == "事件正文"
        # 无 mood/words/raw 出站（初次判断不带这些域）
        assert "mood" not in state["candidates"][0]
        assert "our_words" not in state["candidates"][0]

    def test_middle_band_is_uncertain(self):
        transport = FakeTransport(0.5)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m", "revision": 1, "text": "那件事呢",
                  "body_hash": "b" * 64}
        decision = sj.judge_prepare(
            source, [],
            [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 4,
              "inherited_date_span": None}], [_pool_entry()])
        assert decision.assessments[0].verdict == "uncertain"
        assert not decision.assessments[0].strictly_confirmed()

    def test_missing_answer_is_unknown_not_zero(self):
        transport = lambda payload: {"answers": {}}   # 全缺答
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m", "revision": 1, "text": "x",
                  "body_hash": "b" * 64}
        decision = sj.judge_prepare(
            source, [],
            [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 1,
              "inherited_date_span": None}], [_pool_entry()])
        a = decision.assessments[0]
        assert a.verdict == "uncertain"
        assert all(v is None for v in a.checks.values())

    def test_group_merge_only_on_confirmed_pair(self):
        # h1/h2 共享日期 → 被提议合并；答 0.5（中间带）→ 不合并、不确认
        surface_judge.set_transport_for_tests(FakeTransport(0.5))
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m", "revision": 1,
                  "text": "7.9结婚纪念日，也是记忆库完成", "body_hash": "b" * 64}
        hyps = [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
                 "inherited_date_span": None},
                {"hypothesis_id": "h2", "start_cp": 9, "end_cp": 18,
                 "inherited_date_span": None}]
        pool = [_pool_entry("memory:m1", hypotheses=["h1", "h2"])]
        decision = sj.judge_prepare(source, [], hyps, pool)
        assert len(decision.event_groups) == 2   # 未确认 → 分立
        assert all(not g.grouping_confirmed or len(g.hypothesis_ids) == 1
                   for g in decision.event_groups)

    def test_delivery_relevance_gate(self):
        # relevant_now=0.01 → 拒绝（换题）
        def transport_fn(payload):
            answers = {}
            for k in payload["questions"]:
                v = 0.01 if k.endswith("relevant_now") else 0.99
                answers[k] = {"noul": v}
            return {"answers": answers}
        surface_judge.set_transport_for_tests(transport_fn)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m1", "revision": 1, "text": "7.9呢",
                  "body_hash": "b" * 64}
        target = {"message_id": "m2", "revision": 1, "text": "今天吃什么",
                  "body_hash": "c" * 64}
        winners = [_pool_entry("memory:m1", event_id="ev_1")]
        decision = sj.judge_delivery(source, [], target, [], winners)
        a = decision.assessments[0]
        assert a.verdict == "rejected"
        assert a.checks["relevant_now"] is False

    def test_request_byte_gate(self):
        transport = FakeTransport(0.99)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge(
            "typesafe_jev", _policy(max_judge_request_bytes=64))
        source = {"message_id": "m", "revision": 1, "text": "7.9记忆库做成了",
                  "body_hash": "b" * 64}
        decision = sj.judge_prepare(
            source, [],
            [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
              "inherited_date_span": None}],
            [_pool_entry(text="超长" * 500)])
        # 单候选超判断字节预算：无法完整判断 → uncertain（不截正文、
        # 不拿标题代替），且没有任何出站请求
        assert decision.assessments[0].verdict == "uncertain"
        assert transport.calls == []

    def test_cache_hit_no_second_paid_request(self):
        surface_judge.set_transport_for_tests(FakeTransport(0.99))
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m", "revision": 1, "text": "7.9记忆库做成了",
                  "body_hash": "b" * 64}
        hyp = [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
                "inherited_date_span": None}]
        sj.judge_prepare(source, [], hyp, [_pool_entry()])
        n1 = sj.request_count
        sj2 = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        sj2.judge_prepare(source, [], hyp, [_pool_entry()])
        assert sj2.request_count == 0    # 缓存命中，不重复付费
        assert n1 >= 1
