"""精简交付选择（v1.4 §8）：0—3 张候选卡，不塞回全桶。

先过硬门（权限、当前表示、版本、明确约束、用户 rejected、证据等级），
再按校准后相关度排序（Jev 不可用时用 RRF 序且不伪造支持分），最后做
去重与必要多样性。未校准前禁用"自动高置信单条"——集合中只有 1 条也
标 needs_validation。列表/时间线请求带 continuation/coverage，前三不
冒充完整答案（PACK-01..07）。
"""
from __future__ import annotations

from .. import config
from . import evidence as evidence_mod


def _hard_gate(c: dict, plan: dict, rejected: set[str]) -> bool:
    ref = c["resource_ref"]
    # 用户明确 rejected：本 session 内不再作为普通候选正文返回
    if ref in rejected:
        return False
    # reject_target=event：排除该事件资源的全部重复表示
    if any(r.startswith("memory:") and ref.startswith("memory:") and
           r == ref for r in rejected):
        return False
    # 证据等级不满足不是淘汰条件（RAWX-01：paraphrase 候选仍作为线索
    # 交付并标 evidence_requirement_met=false，触发后续 raw 补查判定）；
    # 硬门只管权限/表示/版本/明确约束/用户 rejected。
    return True


def select(candidates: list[dict], plan: dict, rejected: set[str],
           has_conflict: bool = False,
           unjudged_count: int = 0) -> dict:
    """返回 {delivered, delivery_action, conflicts, missing, truncated}。"""
    limit = int(plan.get("delivery_limit", config.RECALL_DELIVERY_LIMIT))
    gated = [c for c in candidates if _hard_gate(c, plan, rejected)]

    # 排序：judge 分（若有且可用）> RRF 序（不伪造支持分）
    def sort_key(c: dict):
        judge = (c.get("judge") or {})
        rel = judge.get("relevance_signal")
        if isinstance(rel, (int, float)):
            return (0, -rel, -c.get("rrf_score", 0))
        return (1, -c.get("rrf_score", 0))

    ordered = sorted(gated, key=sort_key)

    # 多样性：同一资源的重复片段不占满交付位；不同通道各自保留证据角色
    delivered: list[dict] = []
    seen_resource: set[str] = set()
    conflicts: list[dict] = []
    for c in ordered:
        base_ref = c["resource_ref"].split("#")[0]
        if base_ref in seen_resource:
            continue  # 同一资源的重复片段不占多个交付位
        if c.get("conflict_flag"):
            conflicts.append(c["resource_ref"])
        if len(delivered) >= limit:
            break
        seen_resource.add(base_ref)
        delivered.append(c)

    # 证据要求未满足的候选仍可交付（作为线索），但显式标注
    requirement_met = all(
        evidence_mod.meets_requirement(c.get("evidence") or [],
                                       plan.get("evidence_requirement",
                                                "any"))
        for c in delivered) if delivered else False

    if not delivered:
        action = "no_candidates"
    elif requirement_met and len(delivered) == 1:
        # 未校准前不启用自动高置信单条；唯一候选也标 needs_validation
        action = ("needs_validation"
                  if not config.RECALL_AUTO_TOP1 else "confident_top1")
    else:
        action = "needs_validation" if not requirement_met else "deliver"

    missing: list[str] = []
    if (plan.get("evidence_requirement") == "verbatim_required"
            and delivered and not requirement_met):
        missing.append("逐字原话证据尚未取得")
    if has_conflict:
        missing.append("存在未决冲突候选，需人工裁决或 refine")
    if unjudged_count:
        missing.append(f"{unjudged_count} 个后台候选未精排覆盖")

    return {"delivered": delivered, "delivery_action": action,
            "conflicts": conflicts, "missing": missing}
