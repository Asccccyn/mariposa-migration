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


def _valid_signal(rel) -> bool:
    """合法 relevance：数值（非 bool）、有限、在 provider 消毒约定
    值域 [-1,1] 内（JEV-03 sanitize_signal 同口径）。"""
    import math as _math
    if not isinstance(rel, (int, float)) or isinstance(rel, bool):
        return False
    v = float(rel)
    return _math.isfinite(v) and -1.0 <= v <= 1.0


def _hard_gate(c: dict, plan: dict, rejected: set[str],
               judge_required: bool = True) -> bool:
    """公共门（权限/版本/用户 rejected）与判断门分离（M03，
    MANUAL_HANDOFF_JUDGE_SWITCH_V1）：judge_required=False 只跳过
    判分有效性段——公共门仍逐条执行，不得整段 return True。"""
    ref = c["resource_ref"]
    # 用户明确 rejected：本 session 内不再作为普通候选正文返回
    if ref in rejected:
        return False
    if judge_required:
        # S10 出站硬门：未判断（无 judge）、evaluation_status 非
        # evaluated、缺项/NaN/越界/布尔分值——都不是低分，一律不得
        # 交付。低分 evaluated 候选合法（S11 rank_only），由 select
        # 正常处理。
        judge = c.get("judge") or {}
        if judge.get("evaluation_status") != "evaluated":
            return False
        if not _valid_signal(judge.get("relevance_signal")):
            return False
        # S18：判断必须绑定候选实际版本；候选已前进则旧判断失效。
        # 版本身份只在双方都非空时比对（raw 候选合法无版本，S14：
        # 不虚构版本；unknown 版本由上游标 stale）
        jv = judge.get("candidate_version")
        cv = c.get("content_version")
        if jv and cv and str(jv) != str(cv):
            return False
    # 证据等级不满足不是淘汰条件（RAWX-01：paraphrase 候选仍作为线索
    # 交付并标 evidence_requirement_met=false，触发后续 raw 补查判定）；
    # 硬门只管判断有效性/版本/用户 rejected/权限。
    return True


def select(candidates: list[dict], plan: dict, rejected: set[str],
           has_conflict: bool = False,
           unjudged_count: int = 0,
           judge_required: bool = True) -> dict:
    """返回 {delivered, delivery_action, conflicts, missing, truncated}。

    judge_required=False（人工关闭判断，judge_policy §3.1）：跳过判分
    硬门与判分排序（保留检索序+确定性同分键），交付=本次候选全集
    （不再套 DELIVERY_LIMIT——有界分页由 recall/paging 承担）。
    """
    limit = int(plan.get("delivery_limit", config.RECALL_DELIVERY_LIMIT))
    if not judge_required:
        limit = max(limit, len(candidates))
    gated = [c for c in candidates
             if _hard_gate(c, plan, rejected, judge_required=judge_required)]

    if judge_required:
        # 排序：judge 分（若有且可用）> RRF 序（不伪造支持分）
        def sort_key(c: dict):
            judge = (c.get("judge") or {})
            rel = judge.get("relevance_signal")
            if isinstance(rel, (int, float)):
                return (0, -rel, -c.get("rrf_score", 0))
            return (1, -c.get("rrf_score", 0))
    else:
        # 关闭模式：保留检索序，同分补确定性键（P06：顺序稳定、不漏
        # 不重）；不伪造 judge 分值，不用 provider 分数排序
        def sort_key(c: dict):
            return (-c.get("rrf_score", 0),
                    str(c.get("resource_ref") or ""))

    ordered = sorted(gated, key=sort_key)

    # 多样性：同一资源的重复片段不占满交付位；不同通道各自保留证据角色。
    # 关闭模式不做该压缩——候选全集是分页分母，同资源多段命中必须在集
    # 合内可见（P02：证据角色合并去重已由 fusion.dedupe_by_resource 完成；
    # 这里不得二次丢弃不同命中段）。
    delivered: list[dict] = []
    seen_resource: set[str] = set()
    conflicts: list[dict] = []
    for c in ordered:
        base_ref = c["resource_ref"].split("#")[0]
        if judge_required and base_ref in seen_resource:
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
    else:
        # S11 rank_only_until_calibrated：没有已批准的阈值 profile 前，
        # 一切交付（无论证据等级）都标 needs_validation；自动
        # confident_top1 仅在显式批准 profile 后启用
        action = ("confident_top1"
                  if (config.RECALL_AUTO_TOP1 and requirement_met
                      and len(delivered) == 1)
                  else "needs_validation")

    missing: list[str] = []
    if (plan.get("evidence_requirement") == "verbatim_required"
            and delivered and not requirement_met):
        missing.append("逐字原话证据尚未取得")
    if has_conflict:
        missing.append("存在未决冲突候选，需人工裁决或 refine")
    if unjudged_count:
        missing.append(f"{unjudged_count} 个后台候选未精排覆盖")

    return {"delivered": delivered, "delivery_action": action,
            "conflicts": conflicts, "missing": missing,
            "requirement_met": bool(requirement_met)}
