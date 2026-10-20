"""surface 候选池（P2 分级检索，她 2026-10-20 二批复）。

不复制第二套 BM25/Dense（docs/02）：Tier1 结构化日期走 memories 日期索引，
Tier2 词法复用 recall.pipeline.round1_lexical_hits（真实 scoped BM25），
Tier3 Dense 复用 retrieval.candidate_service.dense_tail——只在确定性准入
失败后、模型已常驻、预算允许时按 reindex_budget=0 补查。

旧的"每假设整句语义查询 + 无条件 Dense"构建器已退役：它让 Dense 对每个
假设无条件运行，且日期从不作为结构化条件。
"""
from __future__ import annotations

import hashlib

from .. import config
from ..memory import service as mem_service
from ..retrieval import candidate_service


def domain_version_vector(conn, memory_id: str) -> dict:
    """跨栏目变更检测向量（R23）：正文版本 ≠ 各域版本，分别取指纹。

    指纹是确定性变更检测（count/max 聚合），不是语义版本号；不宣传
    跨服务全局原子。
    """

    def _one(sql: str, params: tuple) -> str:
        row = conn.execute(sql, params).fetchone()
        return "|".join(str(v) for v in tuple(row))

    return {
        "memory": _one(
            "SELECT current_version_no, updated_at FROM memories"
            " WHERE memory_id=?", (memory_id,)),
        "words": _one(
            "SELECT COUNT(*), COALESCE(MAX(ordinal),0),"
            " COALESCE(MAX(created_at),'') FROM memory_our_words"
            " WHERE memory_id=?", (memory_id,)),
        "recollections": _one(
            "SELECT COUNT(*), COALESCE(MAX(version),0),"
            " COALESCE(MAX(written_at),'') FROM memory_recollections"
            " WHERE memory_id=?", (memory_id,)),
        "keeps": _one(
            "SELECT COUNT(*), COALESCE(SUM(revoked_at IS NULL),0)"
            " FROM memory_keeps WHERE memory_id=?", (memory_id,)),
        "categories": _one(
            "SELECT COUNT(*) FROM memory_categories WHERE memory_id=?",
            (memory_id,)),
    }


def memory_current_state(conn, memory_id: str) -> dict:
    """check/detail/validate 复核用：当前正文+日期（单日与区间）+版本向量。"""
    full = mem_service.get(conn, memory_id)
    return {
        "memory_id": memory_id,
        "content_version": str(full.get("version") or ""),
        "memory_date": full.get("memory_date"),
        "date_confidence": full.get("date_confidence"),
        "occurred_start": full.get("occurred_start"),
        "occurred_end": full.get("occurred_end"),
        "event_text": full.get("text") or "",
        "domain_versions": domain_version_vector(conn, memory_id),
    }


# ======================================================================
# P2 分级检索（她 2026-10-20 二批复）：结构化日期 → 全作用域词法 →
# （确定性失败后、按预算）Dense。每个分区独立；唯一性只在未截断的
# 日期集上证明；召回命中与强事件身份严格分开（anchors.py）。
# ======================================================================

def _entry_from_memory(conn, memory_id: str, hyp_id: str,
                       card: dict | None = None) -> dict | None:
    """由 memory_id 构造池条目（完整正文、日期、证据、域版本）。"""
    try:
        full = mem_service.get(conn, memory_id)
    except Exception:
        return None
    if full.get("visibility") not in (None, "active"):
        return None
    text = full.get("text") or ""
    c = card or {
        "channel": "event", "memory_id": memory_id,
        "representation": full.get("representation") or "full",
        "content_version": str(full.get("version") or ""),
        "_row": {"whitelist_body": text, "compression_state": "full"},
    }
    if not c.get("evidence"):
        candidate_service.attach_event_evidence(conn, [c])
    return {
        "resource_ref": f"memory:{memory_id}",
        "memory_id": memory_id,
        "content_version": str(full.get("version") or ""),
        "memory_date": full.get("memory_date"),
        "date_confidence": full.get("date_confidence"),
        "occurred_start": full.get("occurred_start"),
        "occurred_end": full.get("occurred_end"),
        "original_title": full.get("original_title"),
        "categories": full.get("categories") or [],
        "event_text": text,
        "body_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "body_bytes": len(text.encode("utf-8")),
        "domain_versions": domain_version_vector(conn, memory_id),
        "evidence": c.get("evidence") or [],
        "rrf_score": 0.0,
        "hypotheses": [hyp_id],
        "tier_hits": [],
        "recall_hit": False,
        "strong_match": None,
    }


def _min_year(conn) -> int | None:
    row = conn.execute(
        "SELECT MIN(substr(memory_date,1,4)) AS y FROM memories"
        " WHERE visibility='active' AND memory_date IS NOT NULL").fetchone()
    row2 = conn.execute(
        "SELECT MIN(substr(occurred_start,1,4)) AS y FROM memories"
        " WHERE visibility='active' AND occurred_start IS NOT NULL").fetchone()
    years = [int(r["y"]) for r in (row, row2)
             if r and r["y"] and str(r["y"]).isdigit()]
    return min(years) if years else None


def _iso(y: int, m: int, d: int) -> str | None:
    import datetime as _dt
    try:
        return _dt.date(y, m, d).isoformat()
    except ValueError:
        return None


DATE_ARM_SQL = (
    "SELECT m.memory_id FROM memories m INDEXED BY idx_memories_date"
    " WHERE m.visibility='active' AND m.memory_date >= ? AND m.memory_date <= ?"
    " AND m.memory_id > ? ORDER BY m.memory_id LIMIT ?")
OCCURRED_ARM_SQL = (
    "SELECT m.memory_id FROM memories m"
    " WHERE m.visibility='active' AND m.occurred_start IS NOT NULL"
    " AND substr(m.occurred_start,1,10) <= ?"
    " AND (m.occurred_end IS NULL OR substr(m.occurred_end,1,10) >= ?)"
    " AND m.memory_id > ? ORDER BY m.memory_id LIMIT ?")


def _keyset_ids(conn, sql: str, params: list, batch: int = 500) -> tuple[list[str], bool]:
    """与 recall.pipeline._scope_pool_ids 同语义的 keyset 取尽（安全阀
    RECALL_POOL_MAX_BUCKETS；触顶前探针确认还有下一桶才标 truncated）。
    memory_date 臂显式 INDEXED BY idx_memories_date——无统计信息时规划器
    会偏向 (visibility) 索引后全扫，这里不依赖规划器猜测。"""
    cap = max(1, config.RECALL_POOL_MAX_BUCKETS)
    ids: list[str] = []
    cursor = ""
    truncated = False
    while True:
        rows = conn.execute(sql, params + [cursor, batch]).fetchall()
        if not rows:
            break
        ids.extend(r["memory_id"] for r in rows)
        if len(ids) >= cap:
            del ids[cap:]
            more = conn.execute(sql, params + [ids[-1], 1]).fetchone()
            truncated = more is not None
            break
        if len(rows) < batch:
            break
        cursor = ids[-1]
    return ids, truncated


def date_set(conn, dates: list[dict], now_year: int) -> dict:
    """Tier1：显式日期约束的**完整**候选枚举（不补年份）。

    有年份 → 该日；无年份 → 作用域内 [min 年, now 年] 逐年同月日
    （跨年候选全部纳入竞争）。两条谓词分开查：memory_date 精确日走
    idx_memories_date（显式 INDEXED BY）；occurred 区间重叠无索引（只扫
    occurred_start 非空的行，如实记 scan）。返回 {ids, truncated, ran, days}。
    """
    if not dates:
        return {"ids": [], "truncated": False, "ran": False, "days": []}
    days: list[str] = []
    lo = _min_year(conn)
    for d in dates:
        if d.get("year"):
            iso = _iso(int(d["year"]), int(d["month"]), int(d["day"]))
            if iso:
                days.append(iso)
        elif lo is not None:
            for y in range(lo, now_year + 1):
                iso = _iso(y, int(d["month"]), int(d["day"]))
                if iso:
                    days.append(iso)
    days = list(dict.fromkeys(days))
    ids: list[str] = []
    seen: set[str] = set()
    truncated = False
    for day in days:
        for sql in (DATE_ARM_SQL, OCCURRED_ARM_SQL):
            part, tr = _keyset_ids(conn, sql, [day, day])
            truncated = truncated or tr
            for mid in part:
                if mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
    return {"ids": ids, "truncated": truncated, "ran": True, "days": days}


def lexical_hits(conn, span: str, hyp_id: str, terms: list[str]) -> dict:
    """Tier2：全作用域（active）BM25 召回——**不**按日期预筛，以便发现
    无日期但强身份的竞争候选。返回 {cards_by_id, coverage, ran}。"""
    if not terms:
        return {"cards_by_id": {}, "coverage": "not_requested", "ran": False}
    from ..recall import pipeline as _pl
    plan = {
        "request_ref": f"surface:{hyp_id}", "original_request": span,
        "intent": "recall_event", "channels": ["event"],
        "lexical_terms": list(terms), "exact_phrases": [],
        "explicit_constraints": {}, "explicit_negative_constraints": {},
        "resolved_references": [], "inferred_hints": [],
        "alternate_queries": [], "unknowns": [],
        "temporal_axis": "event_time", "evidence_requirement": "any",
        "raw_fallback": "off",
    }
    cov: dict = {}
    cards = _pl.round1_lexical_hits(conn, plan, set(), cov)
    by_id = {}
    for c in cards:
        by_id.setdefault(c["memory_id"], c)
    return {"cards_by_id": by_id,
            "coverage": cov.get("event") or "unknown", "ran": True}


def tiered_partition_retrieval(conn, span: str, hyp_id: str,
                               constraints: dict, now_year: int) -> dict:
    """Tier0–2：约束/召回词项 → 日期集 → 词法；每候选记 tier_hits /
    recall_hit / strong_match。Dense 不在此处（确定性失败后按预算补）。"""
    from . import anchors
    terms = anchors.recall_terms(span)
    t1 = date_set(conn, constraints.get("dates") or [], now_year)
    t2 = lexical_hits(conn, span, hyp_id, terms)
    entries: dict[str, dict] = {}
    for mid in t1["ids"]:
        e = _entry_from_memory(conn, mid, hyp_id)
        if e is not None:
            e["tier_hits"].append("date")
            entries[mid] = e
    for mid, card in t2["cards_by_id"].items():
        e = entries.get(mid) or _entry_from_memory(conn, mid, hyp_id, card)
        if e is None:
            continue
        e["tier_hits"].append("lexical")
        e["recall_hit"] = True
        e["matched_fields"] = list(card.get("matched_fields") or [])
        entries[mid] = e
    for e in entries.values():
        e["strong_match"] = anchors.strong_identity_match(span, e)
    return {
        "recall_terms": terms,
        "tiers": {
            "date": {"ran": t1["ran"], "size": len(t1["ids"]),
                     "truncated": t1["truncated"], "days": t1["days"]},
            "lexical": {"ran": t2["ran"], "coverage": t2["coverage"],
                        "hits": len(t2["cards_by_id"])},
            "dense": {"status": "not_requested", "hits": 0},
        },
        "completeness": {
            "date_set_size": len(t1["ids"]),
            "date_set_truncated": t1["truncated"],
            "lexical_coverage": (t2["coverage"] if t2["ran"]
                                 else "complete_within_scope" if not terms
                                 else "unknown"),
        },
        "candidates": list(entries.values()),
    }


def dense_supplement(conn, span: str, hyp_id: str, existing: dict[str, dict],
                     budget_left: int) -> tuple[list[dict], dict]:
    """Tier3：确定性失败后按预算补 Dense。条件：provider 配置 ∧ 本进程
    已常驻（semantic.dense_ready）∧ 预算 >0；reindex_budget=0。Dense 候选
    只进语义路径（相似度不是身份）。空结果不等于"库里没有"——状态由
    调用方写入 coverage。"""
    from ..retrieval import semantic
    from ..retrieval import query_plan as qp
    from ..retrieval.semantic import LOCAL_PROVIDERS
    status = {"status": "not_requested", "hits": 0, "pending": 0}
    if config.SEMANTIC_PROVIDER not in LOCAL_PROVIDERS:
        status["status"] = "unavailable"
        return [], status
    if budget_left <= 0:
        status["status"] = "skipped_budget"
        return [], status
    if not semantic.dense_ready():
        status["status"] = "skipped_cold"
        return [], status
    where, params, _, _ = qp.AllowedScope.for_plan({"explicit_constraints": {}})
    cov: dict = {}
    degraded: list[str] = []
    try:
        cards = candidate_service.dense_tail(
            conn, {"semantic_query": span}, [], [], where, params, set(),
            cov, degraded, reindex_budget=0)
    except Exception:
        status["status"] = "provider_error"
        return [], status
    status["status"] = cov.get("dense_event") or "complete_within_scope"
    status["pending"] = int(cov.get("dense_pending_vectors") or 0)
    out: list[dict] = []
    for c in cards:
        mid = c["memory_id"]
        if mid in existing:
            existing[mid]["tier_hits"].append("dense")
            continue
        e = _entry_from_memory(conn, mid, hyp_id, c)
        if e is None:
            continue
        e["tier_hits"].append("dense")
        e["dense_score"] = c.get("score")
        out.append(e)
    status["hits"] = len(cards)
    return out, status
