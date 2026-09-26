"""有限补查预算（v1.3 §14 / v1.4 §9.3）。

一个 burst = 原始表达 + 最多 2 个替代表达、初次检索 + 最多 2 轮自动补查。
真实用户后来的"再查一次"通过 refine 的显式继续请求申请新 burst，
保留同 session 的 rejected 与累计消耗；自动自循环、换窗口、反复 start
不产生新额度。达到总上限返回 BUDGET_EXHAUSTED，并交代范围与缺口。
"""
from __future__ import annotations

from .. import config, db
from ..errors import Forbidden

from . import store


def snapshot(session: dict) -> dict:
    return {
        "rounds_used": session["rounds_used"],
        "rounds_remaining_in_burst": _rounds_left_in_burst(session),
        "bursts_used": session["bursts_used"],
        "bursts_max": config.RECALL_SESSION_BURSTS_MAX,
        "rounds_max_total": (config.RECALL_SESSION_BURSTS_MAX *
                             config.RECALL_BURST_ROUNDS),
    }


def rounds_left_in_burst(session: dict) -> int:
    used_in_burst = session["rounds_used"] - (
        (session["current_burst"] - 1) * config.RECALL_BURST_ROUNDS)
    return max(0, config.RECALL_BURST_ROUNDS - used_in_burst)


_rounds_left_in_burst = rounds_left_in_burst  # 兼容别名


def require_round_available(session: dict) -> None:
    if session["rounds_used"] >= (config.RECALL_SESSION_BURSTS_MAX *
                                  config.RECALL_BURST_ROUNDS):
        raise Forbidden(
            "检索预算已耗尽（BUDGET_EXHAUSTED）；可提交 reject/refine/close"
            " 或读取已有状态，发起有成本的新检索需要新的用户继续请求",
            code="BUDGET_EXHAUSTED", session_id=session["session_id"],
            budget=snapshot(session))
    if _rounds_left_in_burst(session) <= 0:
        raise Forbidden(
            "当前 burst 检索轮次已用完；继续检索需要显式 refine 申请新 burst",
            code="BUDGET_EXHAUSTED", session_id=session["session_id"],
            budget=snapshot(session))


def consume_round(session_id: str, operation_id: str, revision: int,
                  burst_no: int, kind: str = "retrieve") -> None:
    """扣一个检索轮次（CAS：rounds_used+1）。"""
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE recall_sessions SET rounds_used=rounds_used+1,"
                " updated_at=datetime('now') WHERE session_id=?"
                " AND current_revision=?", (session_id, revision))
            if cur.rowcount != 1:
                raise Forbidden("session revision 冲突",
                                code="REVISION_CONFLICT",
                                session_id=session_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    store.record_attempt(session_id, operation_id, revision, burst_no, kind,
                         "completed")


def try_new_burst(session: dict, continue_request_ref: str | None) -> dict:
    """refine 显式继续 → 新 burst。

    新 burst 必须绑定新的用户请求引用：自动模型自循环、换 SDK 窗口、
    调不同工具名或反复 start 不能自动获得无界额度（v1.4 §9.3）。
    """
    if not continue_request_ref or not str(continue_request_ref).strip():
        raise Forbidden(
            "申请新 burst 需要 continue_request_ref（引用本轮真实用户"
            "继续请求，如新消息 id）", code="INVALID_ARGUMENT")
    if session["bursts_used"] >= config.RECALL_SESSION_BURSTS_MAX:
        raise Forbidden(
            f"已达每 session 最大 burst 数（{config.RECALL_SESSION_BURSTS_MAX}）",
            code="BUDGET_EXHAUSTED", session_id=session["session_id"],
            budget=snapshot(session))
    return {"current_burst": session["current_burst"] + 1,
            "bursts_used": session["bursts_used"] + 1}
