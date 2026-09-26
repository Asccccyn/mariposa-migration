"""Session 状态机与动作语义（v1.3 §3.2/§5 / v1.4 §9.2）。

查询纠正不修改正式记忆：reject 只改本 session 临时排除；refine 只
推进 revision；close 只结束本次检索运行。所有动作禁止的副作用在
service 层由「不写正式库」结构性保证。
"""
from __future__ import annotations

from ..errors import Forbidden

from .models import ACTIVE_STATUSES
from . import store

#: 动作 → 允许的前置状态（终态 EXPIRED/RESOLVED/CANCELLED 不可再动；
#: STALE_RETRY_REQUIRED 需先重读校验，只允许 status/close）。
ACTION_PRECONDITIONS = {
    "refine": ("ACTIVE", "AMBIGUOUS", "CONFLICT", "DEGRADED",
               "BUDGET_EXHAUSTED"),
    "reject": ACTIVE_STATUSES,
    "accept": ACTIVE_STATUSES,
    "navigate": ("ACTIVE", "AMBIGUOUS", "CONFLICT", "DEGRADED"),
    "status": ACTIVE_STATUSES + ("STALE_RETRY_REQUIRED", "EXPIRED",
                                 "RESOLVED", "CANCELLED"),
    "close": ACTIVE_STATUSES + ("STALE_RETRY_REQUIRED",),
    "evidence": ("ACTIVE", "AMBIGUOUS", "CONFLICT", "DEGRADED"),
}


def require_action(session: dict, action: str) -> None:
    allowed = ACTION_PRECONDITIONS.get(action)
    if allowed is None:
        raise Forbidden(f"未知 recall 动作 {action}", code="INVALID_ARGUMENT")
    if session["status"] not in allowed:
        raise Forbidden(
            f"动作 {action} 不允许在状态 {session['status']} 下执行",
            code="INVALID_STATE", session_id=session["session_id"],
            status=session["status"], allowed=list(allowed))


def derive_status(search_status: str, candidates_delivered: int,
                  has_conflict: bool) -> str:
    """检索结果主状态 → session 主状态（v1.4 §8.4）。

    degraded_reasons 表达并存的降级因素，不都升为 DEGRADED 主状态；
    只有全部请求通路不可用时才整体 DEGRADED。
    """
    if has_conflict:
        return "CONFLICT"
    if candidates_delivered > 1:
        return "AMBIGUOUS"
    if search_status == "FOUND" and candidates_delivered == 1:
        return "ACTIVE"  # 单条领先仍需周家明判断，不自动 RESOLVED
    if search_status in ("NO_MATCH_OBSERVED",):
        return "ACTIVE"   # 无匹配是检索结果状态，session 保持可纠正
    if search_status == "BUDGET_EXHAUSTED":
        return "BUDGET_EXHAUSTED"
    if search_status in ("UNAVAILABLE", "DEGRADED"):
        return "DEGRADED" if candidates_delivered == 0 else "ACTIVE"
    return "ACTIVE"


#: reject_target 语义（v1.4 §9.2）：拒绝整件事排除事件资源及其重复表示；
#: 只拒某句话排除该 word/source 选区，不升级为永久排除整类记忆。
REJECT_TARGET_EXPANSION = {
    "candidate": None,          # 仅该候选卡
    "event": ("event",),        # 同 resource_ref 的全部事件表示
    "word": ("word",),          # 单句话语
    "source_selection": ("source_selection",),
}


def reject(candidate: dict, reject_target: str) -> dict:
    """把候选标为 session-local rejected；返回更新后的候选引用记录。"""
    from .models import REJECT_TARGETS
    if reject_target not in REJECT_TARGETS:
        raise Forbidden(f"reject_target 必须是 {list(REJECT_TARGETS)}",
                        code="INVALID_ARGUMENT")
    store.set_candidate_state(
        candidate["session_id"], candidate["candidate_ref"], "rejected",
        reject_target=reject_target)
    return {"candidate_ref": candidate["candidate_ref"],
            "resource_ref": candidate["resource_ref"],
            "reject_target": reject_target, "scope": "session_local"}
