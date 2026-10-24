"""surface 严格选择与两阶段 judge 适配器（TypeSafe 有限问句归一）。

v2（她 2026-10-20 P0 裁定）对本文件的改动与理由：
- 选择只消费 contract.admit 的 outcome（_asm 带 outcome；旧 ambiguity_resolved
  检查项退役）；
- 同事件两个不同桶都确认 = 竞争 → 弃权（旧"稳定 ID 打破同分"场景改写）；
- 分组降级为分区：每假设一事件、不再出站 pair/distinct 问句；
- prepare 只问 same_occurrence 一问（旧断言 5 问改 1 问）；
- 证据 / 时间 / 人物由 facts 确定性给出：缺答场景里 evidence_sufficient
  不再是 None；
- 缓存：新会话未观测 resolved 模型前不读缓存（jev-latest 漂移防护），
  旧"跨会话零请求命中"断言改为"会话内命中 / 跨会话首批必出站 /
  模型漂移即 miss"。
"""
from __future__ import annotations

import pytest

from mariposa.surfacing import contract
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
    mid = ref.split(":")[1]
    entry = {
        "resource_ref": ref, "memory_id": mid,
        "content_version": "1", "memory_date": "2026-07-09",
        "date_confidence": "exact", "occurred_start": None,
        "occurred_end": None, "original_title": "t", "categories": [],
        "event_text": text, "body_hash": "h" * 64, "body_bytes": 99,
        "domain_versions": {"memory": "1"},
        # 生产形状（retrieval/evidence.make_evidence）：evidence_kind +
        # source_ref + source_version；facts-v2 据此判证据充分
        "evidence": [{"evidence_kind": "authored_event",
                      "field": "event_text", "source_ref": f"memory:{mid}",
                      "source_version": "1", "snippet": text[:20]}],
        "rrf_score": 1.0,
        "hypotheses": ["h1"],
    }
    entry.update(kw)
    return entry


_OUTCOME_FOR = {"confirmed": contract.CONFIRMED_SEMANTIC,
                "rejected": contract.NOT_SAME,
                "uncertain": contract.UNKNOWN}


def _asm(event, ref, verdict="confirmed", rank=0.99, outcome=None,
         reason=None):
    outcome = outcome or _OUTCOME_FOR[verdict]
    return {"event_id": event, "candidate_ref": ref, "verdict": verdict,
            "outcome": outcome,
            "checks": {"same_occurrence": verdict == "confirmed",
                       "participants_consistent": None,
                       "time_status_consistent": None,
                       "evidence_sufficient": True},
            "rank_signal": rank,
            "reason_code": reason or (
                "EXACT_MATCH" if verdict == "confirmed"
                else "NOT_SAME_OCCURRENCE" if verdict == "rejected"
                else "UNCLEAR_REFERENCE")}


class TestStrictSelection:
    def test_one_winner_per_event_no_runnerup(self):
        pool = [_pool_entry("memory:a", hypotheses=["h1"]),
                _pool_entry("memory:b", hypotheses=["h1"])]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"]}]
        # a 确认、b 被明确否定（NOT_SAME）→ 只选 a；否定者不阻断
        asm = [_asm("ev_1", "memory:a"),
               _asm("ev_1", "memory:b", verdict="rejected")]
        winners, abstain = sel.choose_winners(groups, asm, pool)
        assert [w["memory_id"] for w in winners] == ["a"]
        assert abstain == {}

    def test_winner_failed_no_fallback(self):
        pool = [_pool_entry("memory:a"), _pool_entry("memory:b")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"]}]
        asm = [_asm("ev_1", "memory:a", verdict="rejected"),
               _asm("ev_1", "memory:b")]
        winners, _ = sel.choose_winners(groups, asm, pool)
        assert [w["memory_id"] for w in winners] == ["b"]

    def test_uncertain_competitor_suppresses_event(self):
        """同分区 UNKNOWN = 可能是同一事件的竞争候选 → 保守弃权。"""
        pool = [_pool_entry("memory:a"), _pool_entry("memory:b")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"]}]
        asm = [_asm("ev_1", "memory:a"),
               _asm("ev_1", "memory:b", verdict="uncertain")]
        winners, abstain = sel.choose_winners(groups, asm, pool)
        assert winners == []
        assert abstain["ev_1"] == "SURFACE_EVENT_AMBIGUOUS"

    def test_rank_not_confirmation(self):
        """分数不是确认：outcome 非 CONFIRMED 的最高分不入选。"""
        pool = [_pool_entry("memory:a"), _pool_entry("memory:b")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"]}]
        asm = [_asm("ev_1", "memory:a", verdict="uncertain", rank=1.0),
               _asm("ev_1", "memory:b", verdict="uncertain", rank=0.1)]
        winners, abstain = sel.choose_winners(groups, asm, pool)
        assert winners == []

    def test_two_confirmed_distinct_refs_is_competition(self):
        """v2：同事件两个不同桶都确认 = 竞争 → 弃权，不挑分高/ID 小者。"""
        pool = [_pool_entry("memory:zz"), _pool_entry("memory:aa")]
        groups = [{"event_id": "ev_1", "hypothesis_ids": ["h1"]}]
        asm = [_asm("ev_1", "memory:zz", rank=0.99),
               _asm("ev_1", "memory:aa", rank=0.99)]
        winners, abstain = sel.choose_winners(groups, asm, pool)
        assert winners == []
        assert abstain["ev_1"] == "SURFACE_EVENT_AMBIGUOUS"

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
    """全部问句答 value（overrides 可按键覆盖）；响应体带 resolved model。"""

    def __init__(self, value=0.99, overrides=None, model="jev-fake-1"):
        self.calls: list[dict] = []
        self.value = value
        self.overrides = overrides or {}
        self.model = model

    def __call__(self, payload):
        self.calls.append(payload)
        answers = {}
        for k in payload["questions"]:
            answers[k] = {"noul": self.overrides.get(k, self.value)}
        return {"answers": answers, "model": self.model}


_HYP = [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
         "inherited_date_span": None}]
_SOURCE = {"message_id": "m", "revision": 1, "text": "7.9记忆库做成了",
           "body_hash": "b" * 64}


class TestSurfaceJudge:
    def test_profile_unsupported_for_non_typesafe(self):
        with pytest.raises(SurfaceBlocked) as ei:
            surface_judge.SurfaceJudge("codex_sdk", _policy())
        assert ei.value.code == "SURFACE_PROFILE_UNSUPPORTED"

    def test_prepare_confirmed_flow_and_question_shape(self):
        transport = FakeTransport(0.99)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        decision = sj.judge_prepare(_SOURCE, [], _HYP,
                                    [_pool_entry("memory:m1")])
        assert decision.phase == "prepare"
        a = decision.assessments[0]
        assert a.outcome == contract.CONFIRMED_SEMANTIC
        assert a.strictly_confirmed()
        assert a.resolved_model == "jev-fake-1"
        assert decision.resolved_model == "jev-fake-1"
        # v2 有限问句：prepare 只问 same_occurrence 一问，无自由生成
        q = transport.calls[0]["questions"]
        assert list(q) == ["c0_same_occurrence"]
        assert all(v["type"] == "noul" for v in q.values())
        # 请求显式携带 requested 模型别名（与 recall 路径一致）
        assert transport.calls[0]["model"] == sj.model_id
        # 出站候选带完整正文（judge 必须读全文）
        state = transport.calls[0]["state"]
        assert state["candidates"][0]["text"] == "事件正文"
        assert "mood" not in state["candidates"][0]
        assert "our_words" not in state["candidates"][0]
        # 事实由代码给出：7.9 ↔ memory_date 2026-07-09 PASS，证据 PASS
        assert a.facts["time"] == {"status": "pass", "required": True}
        assert a.facts["evidence"]["status"] == "pass"

    def test_middle_band_is_uncertain(self):
        surface_judge.set_transport_for_tests(FakeTransport(0.5))
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m", "revision": 1, "text": "那件事呢",
                  "body_hash": "b" * 64}
        decision = sj.judge_prepare(
            source, [],
            [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 4,
              "inherited_date_span": None}], [_pool_entry()])
        a = decision.assessments[0]
        assert a.verdict == "uncertain"
        assert a.outcome == contract.UNKNOWN
        assert a.semantic == contract.SEM_UNKNOWN
        assert not a.strictly_confirmed()

    def test_low_band_is_not_same_not_unknown(self):
        """≤ risk 门=明确否定 NOT_SAME（不阻断同分区赢家）。"""
        surface_judge.set_transport_for_tests(FakeTransport(0.01))
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        decision = sj.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        a = decision.assessments[0]
        assert a.outcome == contract.NOT_SAME
        assert a.verdict == "rejected"
        assert a.reason_code == "NOT_SAME_OCCURRENCE"

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
        assert a.outcome == contract.UNKNOWN
        assert a.checks["same_occurrence"] is None
        # 时间/人物：消息无约束 → not_required（映射 None）；证据是确定性
        # 校验，不随缺答变 None
        assert a.checks["time_status_consistent"] is None
        assert a.checks["participants_consistent"] is None
        assert a.checks["evidence_sufficient"] is True

    def test_partition_one_event_per_hypothesis_no_pair_questions(self):
        """分组降级为分区：每假设一事件、恒 confirmed、零 pair/distinct 出站。"""
        transport = FakeTransport(0.5)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        source = {"message_id": "m", "revision": 1,
                  "text": "7.9结婚纪念日，也是记忆库完成", "body_hash": "b" * 64}
        hyps = [{"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
                 "inherited_date_span": None},
                {"hypothesis_id": "h2", "start_cp": 9, "end_cp": len(source["text"]),
                 "inherited_date_span": None}]
        pool = [_pool_entry("memory:m1", hypotheses=["h1", "h2"])]
        decision = sj.judge_prepare(source, [], hyps, pool)
        assert len(decision.event_groups) == 2
        assert all(len(g.hypothesis_ids) == 1
                   for g in decision.event_groups)
        # 分区各自携带用户原话约束：h1 有 7.9 日期，h2 没有
        assert decision.event_groups[0].constraints["dates"] == [
            {"year": None, "month": 7, "day": 9}]
        assert decision.event_groups[1].constraints["dates"] == []
        for call in transport.calls:
            assert not any(k.startswith("hypothesis_")
                           for k in call["questions"])

    def test_delivery_relevance_gate(self):
        # relevant_now=0.01 → 拒绝（换题）
        calls = []

        def transport_fn(payload):
            calls.append(payload)
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
        assert a.reason_code == "TOPIC_CHANGED"
        assert a.checks["relevant_now"] is False
        assert not a.strictly_confirmed()
        # delivery 两问：same_occurrence + relevant_now
        assert sorted(calls[0]["questions"]) == sorted([
            "c0_same_occurrence", "c0_relevant_now"])

    def test_request_byte_gate(self):
        transport = FakeTransport(0.99)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge(
            "typesafe_jev", _policy(max_judge_request_bytes=64))
        decision = sj.judge_prepare(
            _SOURCE, [], _HYP, [_pool_entry(text="超长" * 500)])
        # 单候选超判断字节预算：无法完整判断 → unknown（不截正文、
        # 不拿标题代替），且没有任何出站请求
        assert decision.assessments[0].verdict == "uncertain"
        assert decision.assessments[0].outcome == contract.UNKNOWN
        assert transport.calls == []

    # ------------------------------------------------------------ 缓存

    def test_cache_hit_within_session_after_resolved_observed(self):
        transport = FakeTransport(0.99)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        sj.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        n1 = sj.request_count
        sj.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        assert n1 == 1 and sj.request_count == 1   # 同会话第二次零出站
        assert sj.cache_hits == 1

    def test_new_session_first_batch_always_live(self):
        """跨会话：未观测 resolved 模型前不读缓存（jev-latest 漂移防护）。"""
        transport = FakeTransport(0.99)
        surface_judge.set_transport_for_tests(transport)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        sj.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        sj2 = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        sj2.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        assert sj2.request_count == 1
        # 观测到同一 resolved 后，再问同一候选即命中
        sj2.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        assert sj2.request_count == 1 and sj2.cache_hits == 1

    def test_cache_miss_on_resolved_model_drift(self):
        """缓存行 resolved=1.13 而本会话观测 1.14 → miss，不冒充新模型。"""
        surface_judge.set_transport_for_tests(FakeTransport(0.99, model="jev-1.13.0"))
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        sj.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        t2 = FakeTransport(0.99, model="jev-1.14.0")
        surface_judge.set_transport_for_tests(t2)
        sj2 = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        other = _pool_entry("memory:other")
        sj2.judge_prepare(_SOURCE, [], _HYP, [other])      # 观测 1.14
        sj2.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        assert sj2.cache_hits == 0
        assert sj2.request_count == 2
        # 会话内再次问 m1 → 命中的是 1.14 新写入的行
        sj2.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
        assert sj2.cache_hits == 1

    def test_resolved_drift_within_session_is_unknown_outcome(self):
        models = iter(["jev-1.13.0", "jev-1.14.0"])

        def transport_fn(payload):
            return {"answers": {k: {"noul": 0.99} for k in payload["questions"]},
                    "model": next(models)}
        surface_judge.set_transport_for_tests(transport_fn)
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        sj.judge_prepare(_SOURCE, [], _HYP, [_pool_entry("memory:a")])
        # Drift is a provider-wide unknown outcome: stop the remaining pool,
        # rather than pay for later candidates under an unverifiable session.
        with pytest.raises(SurfaceBlocked, match="SURFACE_UNKNOWN_OUTCOME"):
            sj.judge_prepare(_SOURCE, [], _HYP, [_pool_entry("memory:b"), _pool_entry("memory:c")])
        assert sj.request_count == 2

    def test_contract_fingerprint_changes_cache_key(self):
        sj = surface_judge.SurfaceJudge("typesafe_jev", _policy())
        sj.resolved_model = "x"
        k1 = sj._identity("prepare", "q", "ctx", "fp")["feature_key"]
        sj2 = surface_judge.SurfaceJudge(
            "typesafe_jev", _policy(same_occurrence_threshold=0.45))
        sj2.resolved_model = "x"
        k2 = sj2._identity("prepare", "q", "ctx", "fp")["feature_key"]
        assert k1 != k2
        assert sj.contract_fingerprint != sj2.contract_fingerprint
