"""历史真实 JEV 分数经 v2 合同离线重放（零 API；她 2026-10-20 P0 验收项）。

数据源：audit-verify-20261019 的 run4/4b/4c/4d 七份产物中的四份新合同批
（resolved jev-1.13.0），wire[].raw_answers 里的真实 same_occurrence /
relevant_now noul；候选与原话取自评估脚本模块级 SCENARIOS（import 惰性，
无网络）。本测试**不发起任何请求**；文件不在本机则 skip。

口径：
- .98（生产默认）→ 任何候选不得 CONFIRMED、零赢家（默认值保守=不自动确认）；
- .45（run4 记录的实验阈值，**replay-only，不是生产校准**）→ 合同一致性：
  S01 恰 1 赢家、S05（.72 但 7.9 vs 8/19）REJECT_FACTS、S13 REJECT 竞争者
  不阻断、S03 两事件各自胜出、S11 同分区 UNKNOWN 竞争者保守弃权；
- 非 vacuous：按 SCENARIOS.expect 的每个候选逐项核，缺项即失败；
- 与旧 oracle 的已知差异只允许一处：S04（原话给了 7.9、候选无日期）由她
  1020 修订（有约束必须核验）从 confirmed 变 UNKNOWN。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from mariposa.surfacing import contract as C
from mariposa.surfacing import facts as F
from mariposa.surfacing import selection as sel
from mariposa.surfacing.models import surface_policy_snapshot

AUDIT = Path("/Users/owner/Projects/devspace-workspace/"
             "audit-verify-20261019")
EVAL_SCRIPT = AUDIT / "mariposa/scripts/jev_real_eval_20261019.py"
RUNS = ("jev_real_eval_run4.json", "jev_real_eval_run4b.json",
        "jev_real_eval_run4c.json", "jev_real_eval_run4d.json")

pytestmark = pytest.mark.skipif(
    not EVAL_SCRIPT.exists() or not all((AUDIT / r).exists() for r in RUNS),
    reason="历史 JEV 证据不在本机（audit-verify-20261019）")

REPLAY_THRESHOLD = 0.45     # run4 实验值；仅重放，不是生产校准


def _scenarios() -> list[dict]:
    spec = importlib.util.spec_from_file_location("_jev_eval_hist", EVAL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 模块级只有常量与函数定义
    return mod.SCENARIOS


def _scores() -> tuple[dict, dict, set]:
    same, relevant, models = {}, {}, set()
    for name in RUNS:                      # 后批覆盖前批（补跑成功者为准）
        d = json.loads((AUDIT / name).read_text())
        for w in d.get("wire") or []:
            if w.get("resolved_model"):
                models.add(w["resolved_model"])
            for k, v in (w.get("raw_answers") or {}).items():
                if not (isinstance(v, dict) and v.get("noul") is not None):
                    continue
                ref = k.split("@")[0]
                if k.endswith("_same_occurrence") and ref.startswith("memory:"):
                    same[ref] = float(v["noul"])
                elif k.endswith("_relevant_now"):
                    relevant[ref] = float(v["noul"])
    return same, relevant, models


def _production_shape(c: dict) -> dict:
    """合成池用 {kind} 旧形状；按生产 make_evidence 形状补 evidence_kind /
    source_ref / source_version（候选 content_version=1）。"""
    c = dict(c)
    c["evidence"] = [{"evidence_kind": e.get("evidence_kind") or e.get("kind"),
                      "field": "event_text",
                      "source_ref": f"memory:{c['memory_id']}",
                      "source_version": c.get("content_version") or "1"}
                     for e in c.get("evidence") or [] if isinstance(e, dict)]
    return c


def _replay(thr: float):
    policy = surface_policy_snapshot()
    risk = float(policy["risk_threshold"])
    same, relevant, _ = _scores()
    out = {}
    for sc in _scenarios():
        text = sc["source"]
        pool = [_production_shape(c) for c in sc["pool"]]
        if sc.get("phase") == "delivery":
            # 直调 delivery 合同：语义 + relevant_now（S17 换题）
            res = {}
            for c in pool:
                sem = C.semantic_outcome(same.get(c["resource_ref"]), thr, risk)
                rn = relevant.get(c["resource_ref"])
                rel = (True if rn is not None and rn >= policy["positive_threshold"]
                       else False if rn is not None and rn <= risk else None)
                adm = C.admit(sem, F.hard_checks(
                    c, F.extract_constraints(text)), relevant_now=rel,
                    require_relevance=True)
                res[c["resource_ref"]] = adm
            out[sc["id"]] = {"delivery": res}
            continue
        groups, assessments = [], []
        for i, h in enumerate(sc["hyps"]):
            span = text[h["start_cp"]:h["end_cp"]] or text
            groups.append({"event_id": f"ev_{i + 1}",
                           "hypothesis_ids": [h["hypothesis_id"]],
                           "grouping_confirmed": True,
                           "constraints": F.extract_constraints(span)})
        for g in groups:
            for c in pool:
                if g["hypothesis_ids"][0] not in c["hypotheses"]:
                    continue
                noul = same.get(c["resource_ref"])
                sem = C.semantic_outcome(noul, thr, risk)
                adm = C.admit(sem, F.hard_checks(c, g["constraints"]))
                assessments.append({
                    "event_id": g["event_id"],
                    "candidate_ref": c["resource_ref"],
                    "outcome": adm.outcome, "checks": {},
                    "rank_signal": noul, "reason_code": adm.reason_code})
        winners, abstain = sel.choose_winners(groups, assessments, pool)
        out[sc["id"]] = {
            "outcomes": {a["candidate_ref"]: (a["outcome"], a["reason_code"])
                         for a in assessments},
            "winners": [w["resource_ref"] for w in winners],
            "abstain": abstain,
            "expect": sc["expect"]}
    return out


def _non_vacuous_check(result: dict):
    """每个 expect 候选必须有 outcome（旧 harness 的 ok 对空 got 为 vacuous true）。"""
    missing = []
    for sid, r in result.items():
        if "delivery" in r:
            continue
        for ref in r["expect"]:
            if ref not in r["outcomes"]:
                missing.append((sid, ref))
    assert missing == [], missing


def test_history_scores_come_from_one_resolved_model():
    same, relevant, models = _scores()
    assert models == {"jev-1.13.0"}
    # 真例/负例分数带（纠正后的口径）：负例含 .72（S05 日期冲突）
    assert same["memory:a5"] == pytest.approx(0.72, abs=0.01)
    assert 0.5 <= same["memory:a1"] <= 0.72
    assert relevant["memory:a17"] <= 0.05


def test_default_threshold_confirms_nothing_on_history():
    result = _replay(0.98)
    _non_vacuous_check(result)
    for sid, r in result.items():
        if "delivery" in r:
            assert all(not a.deliverable for a in r["delivery"].values()), sid
            continue
        assert r["winners"] == [], sid
        assert all(o not in C.CONFIRMED_OUTCOMES
                   for o, _ in r["outcomes"].values()), sid


def test_replay_threshold_contract_consistency():
    result = _replay(REPLAY_THRESHOLD)
    _non_vacuous_check(result)

    # 零误确认：所有 expect=not_confirmed 的候选都不得 CONFIRMED
    mis_confirm = [(sid, ref) for sid, r in result.items() if "outcomes" in r
                   for ref, want in r["expect"].items()
                   if want == "not_confirmed"
                   and r["outcomes"][ref][0] in C.CONFIRMED_OUTCOMES]
    assert mis_confirm == []

    # 期望 confirmed 却未确认：只允许 S04（她 1020 修订：有日期约束必须核验）
    not_confirmed_but_expected = sorted(
        (sid, ref) for sid, r in result.items() if "outcomes" in r
        for ref, want in r["expect"].items()
        if want == "confirmed" and r["outcomes"][ref][0] not in C.CONFIRMED_OUTCOMES)
    assert not_confirmed_but_expected == [("S04-missing-year", "memory:a4")]
    assert result["S04-missing-year"]["outcomes"]["memory:a4"] == (
        C.UNKNOWN, "FACT_UNVERIFIED_TIME")

    # S01：.69 + 7.9↔2026-07-09 PASS + 证据 PASS → 恰 1 赢家（旧合同零赢家）
    s01 = result["S01-exact-hit"]
    assert s01["outcomes"]["memory:a1"] == (C.CONFIRMED_SEMANTIC, "EXACT_MATCH")
    assert s01["winners"] == ["memory:a1"] and s01["abstain"] == {}

    # S05：语义 .72 高于多数真例，但 7.9 vs 2026-08-19 → REJECT_FACTS
    s05 = result["S05-date-conflict"]
    assert s05["outcomes"]["memory:a5"] == (C.REJECT_FACTS, "HARD_CONFLICT_TIME")
    assert s05["winners"] == []

    # S12：语义 .03（中间带 UNKNOWN）但日期冲突优先 → REJECT_FACTS
    assert result["S12-same-topic-different-day"]["outcomes"]["memory:b12"][0] == C.REJECT_FACTS

    # S13：b13（7/10）REJECT_FACTS 不阻断 a13 → a13 胜出（回归 3a）
    s13 = result["S13-multi-similar-one-accurate"]
    assert s13["outcomes"]["memory:b13"][0] == C.REJECT_FACTS
    assert s13["winners"] == ["memory:a13"]

    # S03：两事件分区各自确认、各出一赢家（回归 1）
    s03 = result["S03-two-events-same-day"]
    assert sorted(s03["winners"]) == ["memory:a3", "memory:b3"]

    # S11：a11 确认，但 b11（无日期、原话有 7.9）UNKNOWN 同分区竞争 → 保守弃权
    s11 = result["S11-partial-reliable"]
    assert s11["outcomes"]["memory:a11"][0] == C.CONFIRMED_SEMANTIC
    assert s11["outcomes"]["memory:b11"] == (C.UNKNOWN, "FACT_UNVERIFIED_TIME")
    assert s11["winners"] == [] and s11["abstain"] == {"ev_1": "SURFACE_EVENT_AMBIGUOUS"}

    # S16 中间带 → UNKNOWN（期望 uncertain）
    assert result["S16-middle-band-vague"]["outcomes"]["memory:a16"][0] == C.UNKNOWN

    # S17 delivery：换题（relevant_now .02）→ 不可交付
    assert all(not a.deliverable
               for a in result["S17-delivery-topic-change"]["delivery"].values())

    # 其余场景零赢家
    others = {sid for sid, r in result.items() if "winners" in r
              and r["winners"]} - {"S01-exact-hit", "S03-two-events-same-day",
                                    "S13-multi-similar-one-accurate"}
    assert others == set()
