"""Raw 专项补查（v1.4 §7.1—7.2 / P3）：仅 words 证据不足时的授权查找。

与既有 raw.service.search 的区别：这里按本次允许范围（scope resolver）
分页游标扫描**全历史授权范围**，不先截最新 500 条就宣称全历史搜索；
返回扫描覆盖状态与 continuation——有预算而未查完时不能报告"从未说过"
（RAWX-02）。speaker 不从导入源角色猜测（RAWX-06）：没有正式参与者
映射前 speaker 返回 null，不把任意 user 自动当 qiaosheng。
"""
from __future__ import annotations

from .. import db
from ..retrieval import projection

_SCAN_BATCH = 500


def resolve_scope(plan: dict, principal) -> dict:
    """raw_scope_resolver：根据主体权限与计划明确条件生成可查范围。

    当前授权模型：raw 已收录消息对 owners（qiaosheng/jiaming）开放；
    worker 不可查。conversation 范围与日期来自明确条件——
    推测（inferred_hints）不参与范围决定。无权限时 allowed=False，
    由上层报告证据缺口，而不是静默缩小或扩大。
    """
    allowed = principal.principal_id in ("qiaosheng", "jiaming")
    ec = plan.get("explicit_constraints") or {}
    date_rng = ec.get("source_date") or ec.get("event_date")
    return {
        "allowed": allowed,
        "date_range": date_rng if isinstance(date_rng, dict) else None,
        "basis": "principal_binding+explicit_constraints",
    }


def scoped_search(query_terms: list[str], scope: dict,
                  limit: int = 20, cursor: tuple | None = None,
                  max_batches: int = 20) -> dict:
    """授权范围内的有界原文检索：分批游标 + 覆盖状态 + continuation。

    词面匹配沿用字符级归一化子串语义（与 raw.search 一致）；扫描顺序
    从最新往旧（与"补最近上下文"直觉一致），游标 (occurred_at, id) 稳定。
    """
    if not scope.get("allowed"):
        return {"hits": [], "coverage": "not_authorized",
                "scanned": 0, "continuation": None}
    tokens = [t.lower() for t in projection.tokenize(
        " ".join(query_terms)) if t]
    if not tokens:
        return {"hits": [], "coverage": "no_query_terms",
                "scanned": 0, "continuation": None}
    joined = " ".join(tokens)

    base_where = ["1=1"]
    base_params: list = []
    dr = scope.get("date_range") or {}
    if dr.get("from"):
        base_where.append("rm.occurred_at >= ?")
        base_params.append(dr["from"])
    if dr.get("to"):
        base_where.append("rm.occurred_at <= ?")
        base_params.append(dr["to"] + ("T23:59:59" if len(dr["to"]) == 10 else ""))

    with db.formal() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM raw_messages rm"
            f" WHERE {' AND '.join(base_where)}", base_params).fetchone()["n"]
        hits: list[dict] = []
        scanned = 0
        cur = cursor
        exhausted = False
        for _ in range(max_batches):
            w = list(base_where)
            p = list(base_params)
            if cur:
                w.append("(rm.occurred_at < ? OR"
                         " (rm.occurred_at = ? AND rm.id < ?))")
                p += [cur[0], cur[0], cur[1]]
            rows = conn.execute(
                "SELECT rm.id, rm.conversation_id, rm.role, rm.body,"
                " rm.occurred_at, rc.source_channel FROM raw_messages rm"
                " JOIN raw_conversations rc ON rc.id = rm.conversation_id"
                f" WHERE {' AND '.join(w)}"
                " ORDER BY rm.occurred_at DESC, rm.id DESC LIMIT ?",
                p + [_SCAN_BATCH]).fetchall()
            if not rows:
                exhausted = True
                break
            scanned += len(rows)
            last = rows[-1]
            cur = (last["occurred_at"], last["id"])
            if len(rows) < _SCAN_BATCH:
                exhausted = True
            for r in rows:
                normalized = projection.normalize_search_text(r["body"])
                if joined in normalized:
                    hits.append({
                        "resource_ref": f"raw_msg:{r['id']}",
                        "channel": "raw",
                        "message_id": r["id"],
                        "conversation_id": r["conversation_id"],
                        "role": r["role"],
                        "speaker": None,  # 无正式参与者映射前不猜测（RAWX-06）
                        "body_excerpt": r["body"][:600],
                        "occurred_at": r["occurred_at"],
                        "source_channel": r["source_channel"],
                        "matched_by": ["raw_keyword"],
                    })
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit or exhausted:
                break
        return {
            "hits": hits,
            "coverage": "complete_within_scope" if exhausted else "partial",
            "scanned": scanned,
            "total_in_scope": total,
            "continuation": ({"cursor": list(cur)}
                             if (cur and not exhausted) else None),
        }
