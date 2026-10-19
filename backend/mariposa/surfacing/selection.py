"""surface 严格选择：一事件 0/1 个已确认最高桶（docs/03）。

- 只有严格通过的候选参与赢家选择；评分只排序已通过者，RRF/relevance
  第一名不自动当通过（R35）。
- 同事件唯一最高者；等价桶并列按 明确证据优先 → 当前完整正文 →
  稳定 ID 打破同分（不是多送一条）。
- 分立发生但 judge 无法区分 → 该事件零桶（abstain）。
- 选定后又发现已覆盖/超预算/已修订/复核失败：本事件零注入，**不退到
  第二名**（她"不要放宽"硬规则 R34）。
"""
from __future__ import annotations

from .. import config


def _valid_rank(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and float(v) == float(v) and 0.0 <= float(v) <= 1.0)


def choose_winners(groups: list[dict], assessments: list[dict],
                   pool: list[dict]) -> tuple[list[dict], dict]:
    """每个已确认事件组选一个赢家。

    返回 (winners, abstain)。winners 项 = pool 候选 + event_id + 判断
    依据；abstain[event_id] = 原因码（零桶事件）。
    """
    by_ref = {c["resource_ref"]: c for c in pool}
    abstain: dict[str, str] = {}
    winners: list[dict] = []
    for g in groups:
        ev = g["event_id"]
        if not g.get("grouping_confirmed", False):
            abstain[ev] = "SURFACE_EVENT_AMBIGUOUS"
            continue
        assessed = [a for a in assessments if a["event_id"] == ev]
        if not assessed:
            abstain[ev] = "SURFACE_UNCERTAIN"
            continue
        relevant = [a for a in assessed if a["candidate_ref"] in
                    {c["resource_ref"] for c in _event_pool(pool, g)}]
        confirmed = [a for a in relevant
                     if a.get("verdict") == "confirmed"
                     and all(a.get("checks", {}).get(k) is True for k in (
                         "same_occurrence", "participants_consistent",
                         "time_status_consistent", "evidence_sufficient",
                         "ambiguity_resolved"))]
        if not confirmed:
            reasons = {a.get("reason_code") for a in relevant}
            abstain[ev] = ("SURFACE_NOT_RELEVANT_NOW"
                           if reasons == {"TOPIC_CHANGED"} else
                           "SURFACE_EVENT_AMBIGUOUS" if "AMBIGUOUS_EVENT"
                           in reasons else "SURFACE_UNCERTAIN")
            continue
        # 未判完/不确定的同事件竞争候选压低置信：有 plausible 竞争者
        # 未严格通过 → 弃权该事件（reference.choose_winners 同语义：
        # unjudged or ambiguous competitor suppresses event——验收优先
        # 阻止误注入，允许明确记录漏召/弃权）
        unresolved = [a for a in relevant if a.get("verdict") == "uncertain"]
        if unresolved:
            abstain[ev] = "SURFACE_EVENT_AMBIGUOUS"
            continue

        def sort_key(a):
            cand = by_ref[a["candidate_ref"]]
            return (-float(a.get("rank_signal") or 0.0),
                    -_evidence_priority(cand),
                    -int(bool(cand.get("event_text"))),
                    cand["resource_ref"])
        best = sorted(confirmed, key=sort_key)[0]
        cand = dict(by_ref[best["candidate_ref"]])
        cand["event_id"] = ev
        cand["judge_reason"] = best.get("reason_code")
        cand["rank_signal"] = best.get("rank_signal")
        winners.append(cand)
    return winners, abstain


def _event_pool(pool: list[dict], group: dict) -> list[dict]:
    hyp = set(group.get("hypothesis_ids") or [])
    return [c for c in pool
            if hyp & set(c.get("hypotheses") or [])]


def _evidence_priority(cand: dict) -> int:
    """明确证据优先：authored_event > approved_summary > 无。"""
    ev = cand.get("evidence") or []
    kinds = {e.get("kind") for e in ev if isinstance(e, dict)}
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
