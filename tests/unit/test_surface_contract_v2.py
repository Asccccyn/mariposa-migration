"""surface 准入合同 v2 合同测试（她 2026-10-20 P0 裁定）。

覆盖：facts-v2 探针（旧空真 PASS 全部翻正）/ 条件性必需维度 / admit 五终态
真值表与判定顺序 / 她加的三个回归场景 / 快照与合同指纹 / 交付按当前状态
重验 / CONFIRMED_DETERMINISTIC 本批无生产者。全部离线，零 API。
"""
from __future__ import annotations

import itertools

import pytest

from mariposa.surfacing import contract as C
from mariposa.surfacing import facts as F
from mariposa.surfacing import judge as surface_judge
from mariposa.surfacing import selection as sel
from mariposa.surfacing.models import surface_policy_snapshot
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


def _cand(ref, text="事件正文", date="2026-07-09", hyps=("h1",), **kw):
    mid = ref.split(":")[1]
    c = {"resource_ref": ref, "memory_id": mid, "content_version": "1",
         "memory_date": date, "date_confidence": "exact",
         "occurred_start": None, "occurred_end": None, "original_title": "t",
         "categories": [], "event_text": text, "body_hash": "h" * 64,
         "body_bytes": 99, "domain_versions": {"memory": "1"},
         "evidence": [{"evidence_kind": "authored_event", "field": "event_text",
                       "source_ref": f"memory:{mid}", "source_version": "1",
                       "snippet": text[:20]}],
         "rrf_score": 1.0, "hypotheses": list(hyps)}
    c.update(kw)
    return c


def _facts(time="not_required", t_req=False, part="not_required",
           p_req=False, evid="pass"):
    return {"time": {"status": time, "required": t_req},
            "participants": {"status": part, "required": p_req},
            "evidence": {"status": evid}}


# ------------------------------------------------------------------ facts-v2

class TestFactsProbes:
    """check_history.py 的五探针（审计分支全 pass）在 v2 的期望。"""

    def test_year_conflict_is_conflict(self):
        assert F.check_time({"memory_date": "2026-07-09"},
                            F.extract_dates("2025年7月9日婚礼")) == F.CONFLICT

    def test_iso_date_conflict_is_conflict(self):
        assert F.check_time({"memory_date": "2026-08-19"},
                            F.extract_dates("2026-07-09婚礼")) == F.CONFLICT

    def test_missing_participants_is_unknown_when_constrained(self):
        assert F.check_participants({}, ["老王"]) == F.UNKNOWN

    def test_fake_evidence_entry_is_unknown(self):
        assert F.check_evidence({"evidence": [{"kind": "not_evidence"}]}) == F.UNKNOWN
        assert F.check_evidence({"evidence": [{"evidence_kind": "authored_event"}]}) == F.UNKNOWN  # 无 source_ref

    def test_relative_date_only_is_not_required(self):
        assert F.extract_dates("去年婚礼") == []
        assert F.check_time({}, F.extract_dates("去年婚礼")) == F.NOT_REQUIRED

    def test_date_forms(self):
        d = lambda t: [c.as_dict() for c in F.extract_dates(t)]  # noqa: E731
        assert d("2025年7月9日") == [{"year": 2025, "month": 7, "day": 9}]
        assert d("二〇二五年七月九号") == [{"year": 2025, "month": 7, "day": 9}]
        assert d("七月九号我们在一起了") == [{"year": None, "month": 7, "day": 9}]
        assert d("7.9记忆库做成了") == [{"year": None, "month": 7, "day": 9}]
        assert d("去年7月9日") == [{"year": None, "month": 7, "day": 9}]
        assert d("2026-07-08") == [{"year": 2026, "month": 7, "day": 8}]
        assert d("v1.2 发布那次") == []          # 字母前缀不作日期
        assert d("12月31日") == [{"year": None, "month": 12, "day": 31}]

    def test_month_day_only_matches_any_year(self):
        assert F.check_time({"memory_date": "2026-07-09"},
                            F.extract_dates("7.9")) == F.PASS

    def test_occurred_range_priority_and_inconsistency(self):
        rng = {"occurred_start": "2026-07-08", "occurred_end": "2026-07-10"}
        assert F.check_time(rng, F.extract_dates("7月9日")) == F.PASS
        assert F.check_time(rng, F.extract_dates("8月19日")) == F.CONFLICT
        bad = {**rng, "memory_date": "2026-01-01"}      # 单日落在区间外
        assert F.check_time(bad, F.extract_dates("7月9日")) == F.UNKNOWN
        wide = {"occurred_start": "2023-01-01", "occurred_end": "2026-12-31"}
        assert F.check_time(wide, F.extract_dates("7月9日")) == F.UNKNOWN

    def test_candidate_without_date_under_constraint_is_unknown(self):
        assert F.check_time({"memory_date": None},
                            F.extract_dates("2025年7月9日")) == F.UNKNOWN

    def test_participants_extraction(self):
        assert F.extract_participants("和老王一起吃饭") == ["老王"]
        assert F.extract_participants("跟你一起去海边") == []       # 代词
        assert F.extract_participants("char，你记得吗") == []       # 称呼非约束
        assert F.extract_participants("我们俩一起") == []

    def test_evidence_rules(self):
        ok = {"content_version": "3", "evidence": [
            {"evidence_kind": "authored_event", "source_ref": "memory:m1",
             "source_version": "3"}]}
        assert F.check_evidence(ok) == F.PASS
        stale = {"content_version": "3", "evidence": [
            {"evidence_kind": "authored_event", "source_ref": "memory:m1",
             "source_version": "2"}]}
        assert F.check_evidence(stale) == F.UNKNOWN
        gap = {"evidence": [{"evidence_kind": "approved_summary",
                             "source_ref": "memory:m1",
                             "structured_value": {"legacy_content_gap": True}}]}
        assert F.check_evidence(gap) == F.UNKNOWN
        onlyfact = {"evidence": [{"evidence_kind": "structured_fact",
                                  "source_ref": "memory:m1"}]}
        assert F.check_evidence(onlyfact) == F.UNKNOWN
        assert F.check_evidence({"evidence": []}) == F.UNKNOWN


class TestConditionalRequirement:
    """她的两个例子：有约束才必需。"""

    def test_trip_without_date_tolerates_unknown_time(self):
        cons = F.extract_constraints("那次旅行你记得吗")
        f = F.hard_checks(_cand("memory:a", date=None), cons)
        assert f["time"] == {"status": "not_required", "required": False}
        assert C.admit(C.SEM_CONFIRMED, f).outcome == C.CONFIRMED_SEMANTIC

    def test_explicit_date_must_be_verified(self):
        cons = F.extract_constraints("2025年7月9日我们在一起的事情")
        # 候选无可核验日期 → UNKNOWN 阻断（不是通过）
        f = F.hard_checks(_cand("memory:a", date=None), cons)
        adm = C.admit(C.SEM_CONFIRMED, f)
        assert (adm.outcome, adm.reason_code) == (C.UNKNOWN, "FACT_UNVERIFIED_TIME")
        # 明确矛盾 → REJECT_FACTS，哪怕语义确认
        f2 = F.hard_checks(_cand("memory:a", date="2026-07-09"), cons)
        adm2 = C.admit(C.SEM_CONFIRMED, f2)
        assert (adm2.outcome, adm2.reason_code) == (C.REJECT_FACTS, "HARD_CONFLICT_TIME")
        # 一致 → 通过
        f3 = F.hard_checks(_cand("memory:a", date="2025-07-09"), cons)
        assert C.admit(C.SEM_CONFIRMED, f3).outcome == C.CONFIRMED_SEMANTIC

    def test_named_companion_is_constraint_but_vocative_is_not(self):
        f = F.hard_checks(_cand("memory:a"),
                          F.extract_constraints("和老王一起去海边那次"))
        assert f["participants"] == {"status": "unknown", "required": True}
        assert C.admit(C.SEM_CONFIRMED, f).reason_code == "FACT_UNVERIFIED_PARTICIPANTS"
        f2 = F.hard_checks(_cand("memory:a"),
                           F.extract_constraints("char，你记得那次旅行吗"))
        assert f2["participants"]["required"] is False
        assert C.admit(C.SEM_CONFIRMED, f2).outcome == C.CONFIRMED_SEMANTIC


# ------------------------------------------------------------------- admit

class TestAdmit:
    def test_semantic_three_bands(self):
        assert C.semantic_outcome(0.98, 0.98, 0.02) == C.SEM_CONFIRMED
        assert C.semantic_outcome(0.97, 0.98, 0.02) == C.SEM_UNKNOWN
        assert C.semantic_outcome(0.02, 0.98, 0.02) == C.SEM_NOT_SAME
        assert C.semantic_outcome(None, 0.98, 0.02) == C.SEM_UNKNOWN
        assert C.semantic_outcome(1.5, 0.98, 0.02) == C.SEM_UNKNOWN

    def test_conflict_beats_high_semantic(self):
        adm = C.admit(C.SEM_CONFIRMED, _facts(time="conflict", t_req=True))
        assert adm.outcome == C.REJECT_FACTS and not adm.deliverable

    def test_not_same_beats_fact_unknown(self):
        adm = C.admit(C.SEM_NOT_SAME, _facts(time="unknown", t_req=True))
        assert adm.outcome == C.NOT_SAME

    def test_unknown_reasons(self):
        assert C.admit(C.SEM_CONFIRMED, _facts(time="unknown", t_req=True)
                       ).reason_code == "FACT_UNVERIFIED_TIME"
        assert C.admit(C.SEM_CONFIRMED, _facts(part="unknown", p_req=True)
                       ).reason_code == "FACT_UNVERIFIED_PARTICIPANTS"
        assert C.admit(C.SEM_CONFIRMED, _facts(evid="unknown")
                       ).reason_code == "INSUFFICIENT_EVIDENCE"
        assert C.admit(C.SEM_UNKNOWN, _facts()).reason_code == "UNCLEAR_REFERENCE"

    def test_unrequired_unknown_tolerated(self):
        adm = C.admit(C.SEM_CONFIRMED, _facts(time="unknown", part="unknown"))
        assert adm.outcome == C.CONFIRMED_SEMANTIC and adm.deliverable

    def test_delivery_requires_relevance(self):
        base = C.admit(C.SEM_CONFIRMED, _facts(), require_relevance=True)
        assert base.outcome == C.CONFIRMED_SEMANTIC and not base.deliverable
        no = C.admit(C.SEM_CONFIRMED, _facts(), relevant_now=False,
                     require_relevance=True)
        assert (no.deliverable, no.reason_code) == (False, "TOPIC_CHANGED")
        yes = C.admit(C.SEM_CONFIRMED, _facts(), relevant_now=True,
                      require_relevance=True)
        assert yes.deliverable

    def test_verdict_projection(self):
        assert C.verdict_for(C.CONFIRMED_SEMANTIC) == "confirmed"
        assert C.verdict_for(C.REJECT_FACTS) == "rejected"
        assert C.verdict_for(C.NOT_SAME) == "rejected"
        assert C.verdict_for(C.UNKNOWN) == "uncertain"

    def test_no_producer_for_deterministic_outcome(self):
        """语义路径 admit() 对任何输入都不产生 CONFIRMED_DETERMINISTIC——
        它的唯一生产者是 P2 的 contract.admit_deterministic（见
        test_surface_tiered_retrieval）。"""
        assert callable(C.admit_deterministic)
        statuses = ("pass", "conflict", "unknown", "not_required")
        seen = set()
        for sem, t, p, e, req_t, req_p in itertools.product(
                (C.SEM_CONFIRMED, C.SEM_NOT_SAME, C.SEM_UNKNOWN),
                statuses, statuses, ("pass", "unknown"), (True, False),
                (True, False)):
            seen.add(C.admit(sem, _facts(t, req_t, p, req_p, e)).outcome)
        assert C.CONFIRMED_DETERMINISTIC not in seen
        assert seen == {C.REJECT_FACTS, C.NOT_SAME, C.UNKNOWN, C.CONFIRMED_SEMANTIC}


# ----------------------------------------------------- 她加的三个回归场景

class _Transport:
    def __init__(self, per_ref: dict, default=0.5, model="jev-fake"):
        self.per_ref, self.default, self.model = per_ref, default, model
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        out = {}
        for k in payload["questions"]:
            ref = k.split("@")[0]
            out[k] = {"noul": self.per_ref.get(ref, self.default)}
        return {"answers": out, "model": self.model}


def _run(source_text, hyps, pool, per_ref, thr=0.45):
    surface_judge.set_transport_for_tests(_Transport(per_ref))
    sj = surface_judge.SurfaceJudge(
        "typesafe_jev", _policy(same_occurrence_threshold=thr))
    src = {"message_id": "m", "revision": 1, "text": source_text,
           "body_hash": "b" * 64}
    dec = sj.judge_prepare(src, [], hyps, pool)
    winners, abstain = sel.choose_winners(
        [{"event_id": g.event_id, "hypothesis_ids": g.hypothesis_ids,
          "grouping_confirmed": g.grouping_confirmed} for g in dec.event_groups],
        [{"event_id": a.event_id, "candidate_ref": a.candidate_ref,
          "verdict": a.verdict, "outcome": a.outcome, "checks": a.checks,
          "rank_signal": a.rank_signal, "reason_code": a.reason_code}
         for a in dec.assessments], pool)
    return dec, winners, abstain


def _h(hid, s, e):
    return {"hypothesis_id": hid, "start_cp": s, "end_cp": e,
            "inherited_date_span": None}


class TestRegressionScenarios:
    def test_two_events_in_one_sentence_judged_independently(self):
        """回归 1：一句话两件事分区各判，不互相污染。"""
        text = "7.9记忆库做成了，也是我们的结婚纪念日"
        hyps = [_h("h1", 0, 9), _h("h2", 9, 19)]
        pool = [_cand("memory:a3", "2026年7月9日，我们把记忆库建成了", hyps=("h1",)),
                _cand("memory:b3", "2026年7月9日是我们的结婚纪念日", hyps=("h2",))]
        dec, winners, abstain = _run(text, hyps, pool,
                                     {"memory:a3": 0.71, "memory:b3": 0.48})
        assert [g.constraints["dates"] for g in dec.event_groups] == [
            [{"year": None, "month": 7, "day": 9}], []]
        assert sorted(w["memory_id"] for w in winners) == ["a3", "b3"]
        assert abstain == {}
        # h2 的候选不受 h1 日期约束：b3 时间维度 not_required
        b3 = next(a for a in dec.assessments if a.candidate_ref == "memory:b3")
        assert b3.facts["time"]["required"] is False

    def test_same_ref_across_partitions_merges_to_one(self):
        """回归 2：同桶被两个假设命中/在多个分区胜出 → 只浮现一条。"""
        text = "那天我们去了海边。玩得很开心"
        hyps = [_h("h1", 0, 8), _h("h2", 9, 14)]
        pool = [_cand("memory:sea", "2026年7月1日我们一起去了海边",
                      date="2026-07-01", hyps=("h1", "h2"))]
        dec, winners, abstain = _run(text, hyps, pool, {"memory:sea": 0.9})
        assert len(dec.event_groups) == 2
        assert len(winners) == 1
        assert winners[0]["memory_id"] == "sea"
        assert winners[0]["merged_event_ids"] == ["ev_2"]
        assert abstain == {}

    def test_confirmed_not_blocked_by_not_same_or_rejected_competitor(self):
        """回归 3a：无关竞争者（NOT_SAME / REJECT_FACTS）不阻断正确记忆。"""
        text = "7.9记忆库做成了"
        hyps = [_h("h1", 0, 8)]
        pool = [_cand("memory:a13", "2026年7月9日，我们把记忆库建成了"),
                _cand("memory:b13", "7月10日，我们把记忆库建成了", date="2026-07-10"),
                _cand("memory:c13", "完全无关的一天", date="2026-07-09")]
        dec, winners, abstain = _run(
            text, hyps, pool,
            {"memory:a13": 0.51, "memory:b13": 0.17, "memory:c13": 0.01})
        by = {a.candidate_ref: a.outcome for a in dec.assessments}
        assert by == {"memory:a13": C.CONFIRMED_SEMANTIC,
                      "memory:b13": C.REJECT_FACTS,     # 7.9 vs 07-10
                      "memory:c13": C.NOT_SAME}         # ≤ risk
        assert [w["memory_id"] for w in winners] == ["a13"]
        assert abstain == {}

    def test_confirmed_blocked_by_competing_unknown_same_partition(self):
        """回归 3b：同分区 UNKNOWN 可能是同一事件 → 保守弃权。"""
        text = "7.9记忆库做成了"
        hyps = [_h("h1", 0, 8)]
        pool = [_cand("memory:a11", "2026年7月9日，我们把记忆库建成了"),
                _cand("memory:b11", "我们好像做了什么东西，大概七月初", date=None)]
        dec, winners, abstain = _run(text, hyps, pool,
                                     {"memory:a11": 0.64, "memory:b11": 0.18})
        b11 = next(a for a in dec.assessments if a.candidate_ref == "memory:b11")
        assert (b11.outcome, b11.reason_code) == (C.UNKNOWN, "FACT_UNVERIFIED_TIME")
        assert winners == []
        assert abstain == {"ev_1": "SURFACE_EVENT_AMBIGUOUS"}

    def test_unknown_in_other_partition_does_not_block(self):
        """回归 3c：别的分区里的 UNKNOWN 与本分区无关。"""
        text = "7.9记忆库做成了。还有别的事"
        hyps = [_h("h1", 0, 8), _h("h2", 9, 14)]
        pool = [_cand("memory:a", "2026年7月9日，我们把记忆库建成了", hyps=("h1",)),
                _cand("memory:x", "模糊的另一件事", hyps=("h2",))]
        dec, winners, abstain = _run(text, hyps, pool,
                                     {"memory:a": 0.9, "memory:x": 0.3})
        assert [w["memory_id"] for w in winners] == ["a"]
        assert abstain == {"ev_2": "SURFACE_UNCERTAIN"}

    def test_default_threshold_confirms_nothing(self):
        """默认 .98 下历史真例分数（.51–.72）全部 UNKNOWN → 零赢家。"""
        text = "7.9记忆库做成了"
        pool = [_cand("memory:a1", "2026年7月9日，我们把记忆库建成了")]
        dec, winners, _ = _run(text, [_h("h1", 0, 8)], pool,
                               {"memory:a1": 0.72}, thr=0.98)
        assert dec.assessments[0].outcome == C.UNKNOWN
        assert winners == []


# ----------------------------------------------------- 快照 / 指纹 / 交付重验

class TestContractIdentity:
    def test_policy_snapshot_carries_contract_fields(self):
        p = surface_policy_snapshot()
        assert p["same_occurrence_threshold"] == 0.98
        assert p["facts_rules_version"] == "facts-v2"
        assert p["judge_schema_version"] == "surface-judge-v2"
        assert p["prompt_prepare"] == "surface_prepare_v2"
        assert p["policy_revision"] == 2

    def test_fingerprint_sensitive_to_every_contract_field(self):
        base = C.contract_fingerprint(_policy(), "surface:typesafe_jev:jev-latest")
        for k, v in (("same_occurrence_threshold", 0.45),
                     ("facts_rules_version", "facts-v3"),
                     ("prompt_prepare", "surface_prepare_v3"),
                     ("risk_threshold", 0.05), ("policy_revision", 3),
                     ("judge_schema_version", "surface-judge-v3")):
            assert C.contract_fingerprint(_policy(**{k: v}),
                                          "surface:typesafe_jev:jev-latest") != base, k
        assert C.contract_fingerprint(_policy(), "surface:typesafe_jev:other") != base

    def test_delivery_readmits_on_current_state(self):
        """service.validate 的组合：快照赢家 + 当前状态 → 同一份合同重验。"""
        constraints = F.extract_constraints("2025年7月9日我们在一起的事")
        snapshot_winner = _cand("memory:a", date="2025-07-09")
        # 当前状态日期被改成 2026 → 冲突 → 不可交付（哪怕语义仍确认）
        cur = {"content_version": "1", "memory_date": "2026-07-09",
               "occurred_start": None, "occurred_end": None}
        f_now = F.hard_checks({**snapshot_winner, **cur}, constraints)
        adm = C.admit(C.SEM_CONFIRMED, f_now, relevant_now=True,
                      require_relevance=True)
        assert (adm.deliverable, adm.reason_code) == (False, "HARD_CONFLICT_TIME")
        # 版本前进使证据 source_version 失配 → 证据 UNKNOWN → 不可交付
        cur2 = {**cur, "memory_date": "2025-07-09", "content_version": "2"}
        adm2 = C.admit(C.SEM_CONFIRMED,
                       F.hard_checks({**snapshot_winner, **cur2}, constraints),
                       relevant_now=True, require_relevance=True)
        assert (adm2.deliverable, adm2.reason_code) == (False, "INSUFFICIENT_EVIDENCE")
