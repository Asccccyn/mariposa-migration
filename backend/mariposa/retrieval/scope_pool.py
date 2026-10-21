"""scope 候选池 keyset 取尽（R09 拆环共享正本）：旧搜索与 recall 共用，
不再从 recall.pipeline 反取私有 helper。"""
from __future__ import annotations

from .. import config
from ..errors import Forbidden

def fetch_all(conn, where: list[str], params: list,
                    batch: int = 500) -> tuple[list[str], bool]:
    """S19（四轮复审前提前收口）：scope 候选池 keyset 分页取尽。

    旧实现的 `LIMIT 2000` 会让第 2001+ 个作用域桶永远不被检索、
    coverage 却仍签 complete_within_scope（林石见两轮如实申报的
    已知未闭项）。现在分页迭代到取尽；RECALL_POOL_MAX_BUCKETS 是
    防失控安全阀，触顶前先探针确认还有下一桶才标 truncated=True
    ——调用方对 truncated 池不得再签 complete。返回 (ids, truncated)。
    """
    cap = max(1, config.RECALL_POOL_MAX_BUCKETS)
    w = list(where or []) + ["m.memory_id > ?"]
    cond = "WHERE " + " AND ".join(w)
    base = list(params or [])
    ids: list[str] = []
    cursor = ""
    truncated = False
    while True:
        rows = conn.execute(
            f"SELECT m.memory_id FROM memories m {cond}"
            " ORDER BY m.memory_id LIMIT ?",
            base + [cursor, batch]).fetchall()
        if not rows:
            break
        ids.extend(r["memory_id"] for r in rows)
        if len(ids) >= cap:
            del ids[cap:]
            more = conn.execute(
                f"SELECT 1 FROM memories m {cond} LIMIT 1",
                base + [ids[-1]]).fetchone()
            truncated = more is not None
            break
        if len(rows) < batch:
            break
        cursor = ids[-1]
    return ids, truncated


# ---------------------------------------------------------------- Round 2


def _pool_where(filters: dict) -> tuple[list[str], list]:
    """结构化筛选 → (where 片段, 参数)。跨维度 AND；同维度默认 any（D04）。"""
    where = ["m.visibility='active'"]
    params: list = []

    cats = [c for c in (filters.get("categories") or []) if c]
    if cats:
        marks = ",".join("?" * len(cats))
        if filters.get("category_match", "any") == "all":
            where.append(
                f"m.memory_id IN (SELECT memory_id FROM memory_categories"
                f" WHERE category IN ({marks})"
                f" GROUP BY memory_id HAVING COUNT(DISTINCT category)=?)")
            params += cats + [len(set(cats))]
        else:
            where.append(
                f"EXISTS(SELECT 1 FROM memory_categories c WHERE"
                f" c.memory_id=m.memory_id AND c.category IN ({marks}))")
            params += cats

    tags = [t for t in (filters.get("mood_tags") or []) if t]
    if tags:
        # 2026-10-05 冻结裁定（当晚修订）：mood_tags 只按大类过滤
        # （与 by_emotion 同语义）；子心情在 mood_note，不做筛选键
        from ..memory.service import MOOD_CATEGORIES
        bad = [t for t in tags if t not in MOOD_CATEGORIES]
        if bad:
            raise Forbidden(
                f"mood_tags 只能是大类（固定 {len(MOOD_CATEGORIES)} 个）："
                + "、".join(MOOD_CATEGORIES),
                code="MOOD_CATEGORY_REQUIRED", got=bad)
        tags = list(dict.fromkeys(tags))
        marks = ",".join("?" * len(tags))
        if filters.get("mood_match", "any") == "all":
            where.append(
                f"m.memory_id IN (SELECT memory_id FROM memory_mood_tags"
                f" WHERE tag IN ({marks})"
                f" GROUP BY memory_id HAVING COUNT(DISTINCT tag)=?)")
            params += tags + [len(set(tags))]
        else:
            where.append(
                f"EXISTS(SELECT 1 FROM memory_mood_tags t WHERE"
                f" t.memory_id=m.memory_id AND t.tag IN ({marks}))")
            params += tags

    dr = filters.get("event_date") or {}
    if dr.get("from") or dr.get("to"):
        # SEARCH-03：按事件发生时间筛（memory_date 或 occurred 区间重叠），
        # 不按 hold/回执/入库时间。occurred 存 ISO 时刻，比较按日期前缀。
        cond = []
        cp: list = []
        if dr.get("from"):
            cond.append("m.memory_date >= ?")
            cp.append(dr["from"])
        if dr.get("to"):
            cond.append("m.memory_date <= ?")
            cp.append(dr["to"])
        base = "(" + " AND ".join(cond) + ")"
        overlap = ("(m.occurred_start IS NOT NULL AND"
                   " substr(m.occurred_start,1,10) <= ?")
        op: list = [dr.get("to", "9999-12-31")]
        if dr.get("from"):
            overlap += (" AND (m.occurred_end IS NULL OR"
                        " substr(m.occurred_end,1,10) >= ?)")
            op.append(dr["from"])
        overlap += ")"
        where.append(f"({base} OR {overlap})")
        params += cp + op
    return where, params
