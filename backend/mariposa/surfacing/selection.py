"""surface 严格选择 v2：一事件 0/1 个已准入最高桶（docs/03 + 她 1020 P0）。

- 只消费 contract.admit 的 outcome：CONFIRMED_* 才能当赢家；分数只排序
  已准入者，RRF/relevance 第一名不自动当通过（R35）。
- 竞争语义：**同分区**的 UNKNOWN 候选=可能是同一事件 → 保守弃权；
  REJECT_FACTS / NOT_SAME 已确知不是它 → 不阻断（旧合同里任何
  uncertain 都阻断，造成"确认了却选不出来"）。
- 同事件 ≥2 个不同桶都确认 = 竞争 → 弃权（不挑分高的）。
- 跨分区同一 resource_ref 的赢家合并为一条（分区只是竞争域，不证明
  独立事件；同一往事被拆成多个分区不得重复浮现）。
- 选定后又发现已覆盖/超预算/已修订/复核失败：本事件零注入，**不退到
  第二名**（她"不要放宽"硬规则 R34）。
"""
from __future__ import annotations

from .. import config
from . import contract


def choose_winners(groups: list[dict], assessments: list[dict],
                   pool: list[dict]) -> tuple[list[dict], dict]:
    """每个分区选 0/1 个赢家，再跨分区按 resource_ref 合并。

    返回 (winners, abstain)。winners 项 = pool 候选 + event_id + 判断
    依据（+merged_event_ids 若有合并）；abstain[event_id] = 原因码。
    （R07 2026-10-24：恒真 grouping_confirmed 门与缺 outcome 旧形状兼容
    分支退役——partition_hypotheses 是唯一生产构造方、恒一假设一分区，
    两类生产 assessment 均显式填 outcome。）
    """
    by_ref = {c["resource_ref"]: c for c in pool}
    abstain: dict[str, str] = {}
    winners: list[dict] = []
    for g in groups:
        ev = g["event_id"]
        assessed = [a for a in assessments if a["event_id"] == ev]
        if not assessed:
            abstain[ev] = "SURFACE_UNCERTAIN"
            continue
        pool_refs = {c["resource_ref"] for c in _event_pool(pool, g)}
        relevant = [a for a in assessed if a["candidate_ref"] in pool_refs]
        eligible = [a for a in relevant
                    if a["outcome"] in contract.CONFIRMED_OUTCOMES
                    and a.get("reason_code") != "TOPIC_CHANGED"]
        competing = [a for a in relevant if a["outcome"] == contract.UNKNOWN]
        if not eligible:
            reasons = {a.get("reason_code") for a in relevant}
            abstain[ev] = ("SURFACE_NOT_RELEVANT_NOW"
                           if relevant and reasons == {"TOPIC_CHANGED"}
                           else "SURFACE_UNCERTAIN")
            continue
        if competing:
            # 同分区仍有无法排除的候选：可能是同一事件 → 保守弃权
            abstain[ev] = "SURFACE_EVENT_AMBIGUOUS"
            continue
        if len({a["candidate_ref"] for a in eligible}) > 1:
            # 多个不同桶都被确认 = 竞争，不挑分高的（R34）
            abstain[ev] = "SURFACE_EVENT_AMBIGUOUS"
            continue

        def sort_key(a):
            cand = by_ref[a["candidate_ref"]]
            return (-float(a.get("rank_signal") or 0.0),
                    -_evidence_priority(cand),
                    -int(bool(cand.get("event_text"))),
                    cand["resource_ref"])
        best = sorted(eligible, key=sort_key)[0]
        cand = dict(by_ref[best["candidate_ref"]])
        cand["event_id"] = ev
        cand["judge_reason"] = best.get("reason_code")
        cand["judge_outcome"] = best["outcome"]
        cand["rank_signal"] = best.get("rank_signal")
        winners.append(cand)

    merged: list[dict] = []
    by_winner_ref: dict[str, dict] = {}
    for w in winners:
        ref = w["resource_ref"]
        if ref in by_winner_ref:
            by_winner_ref[ref].setdefault("merged_event_ids", []).append(
                w["event_id"])
            continue
        by_winner_ref[ref] = w
        merged.append(w)
    return merged, abstain


def _event_pool(pool: list[dict], group: dict) -> list[dict]:
    hyp = set(group.get("hypothesis_ids") or [])
    return [c for c in pool
            if hyp & set(c.get("hypotheses") or [])]


def _evidence_priority(cand: dict) -> int:
    """明确证据优先：authored_event > approved_summary > 无。

    生产证据条目键名是 evidence_kind（retrieval/evidence.make_evidence）；
    旧实现读 kind 使本并列打破从未生效——兼容两种键名。
    """
    ev = cand.get("evidence") or []
    kinds = {(e.get("evidence_kind") or e.get("kind"))
             for e in ev if isinstance(e, dict)}
    if "authored_event" in kinds:
        return 2
    if "approved_summary" in kinds:
        return 1
    return 0


def finalize_prepare(winners: list[dict], policy: dict) -> list[dict]:
    """prepare 出口过滤（不退补）：

    - 正文超 max_event_body_bytes → 该事件 skipped（SURFACE_BODY_TOO_
      LARGE），不改摘要/不换次优桶；
    - 正文为空 → 该事件 skipped。
    返回仍带 skipped_reason 字段的赢家列表（reason=None 表示入选）。
    """
    max_bytes = int(policy.get("max_event_body_bytes")
                    or config.SURFACE_MAX_EVENT_BODY_BYTES)
    for w in winners:
        body = w.get("event_text") or ""
        if not body:
            w["skipped_reason"] = "SURFACE_BODY_TOO_LARGE"
        elif len(body.encode("utf-8")) > max_bytes:
            w["skipped_reason"] = "SURFACE_BODY_TOO_LARGE"
        else:
            w["skipped_reason"] = None
    return winners
