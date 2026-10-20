"""surface 候选池：复用主动 recall 交付截断之前的候选生成接缝。

不复制第二套 BM25/Dense（docs/02）：每条事件假设构造一个查询计划，
走 retrieval.candidate_service.event_candidates（与主动 recall 同一
函数），多假设独立召回后融合去重；候选不受旧 0～3 卡片截断
（RECALL_DELIVERY_LIMIT 只作用于主动交付，不进本路径）。
"""
from __future__ import annotations

import hashlib
import re

from .. import config, db
from ..memory import service as mem_service
from ..retrieval import candidate_service
from ..retrieval import fusion

# 日期线索（检索假设用，不是事件事实）：数字日期/中文年月日。
_DATE_PATTERNS = (
    re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"),
    re.compile(r"\d{1,2}[-/.]\d{1,2}"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}[日号]?"),
    re.compile(r"\d{1,2}月\d{1,2}[日号]"),
)


def _extract_date_phrases(text: str) -> list[str]:
    out: list[str] = []
    for pat in _DATE_PATTERNS:
        out.extend(m.group(0) for m in pat.finditer(text))
    return list(dict.fromkeys(out))


def _hypothesis_query_plans(source_text: str, hypotheses: list[dict],
                            policy: dict) -> list[tuple[str, dict]]:
    """每假设一个查询计划（≤max_queries；超限整次拒绝由调用方处理）。

    lexical_terms 留空（无 tokenizer 不伪造词法命中）；exact_phrases
    携带跨度内显式日期；semantic_query=原话跨度全文——短句同样可语义
    检索，不用长度/回忆词表做唯一门（R09）。
    """
    plans: list[tuple[str, dict]] = []
    for h in hypotheses:
        span = source_text[h["start_cp"]:h["end_cp"]]
        if not span.strip():
            continue
        plans.append((h["hypothesis_id"], {
            "request_ref": f"surface:{h['hypothesis_id']}",
            "original_request": span,
            "intent": "recall_event",
            "channels": ["event"],
            "semantic_query": span,
            "lexical_terms": [],
            "exact_phrases": _extract_date_phrases(span),
            "explicit_constraints": {},
            "explicit_negative_constraints": {},
            "resolved_references": [],
            "inferred_hints": [],
            "alternate_queries": [],
            "unknowns": [],
            "temporal_axis": "event_time",
            "evidence_requirement": "any",
            "raw_fallback": "off",
        }))
    return plans[:policy["max_queries"]]


def _rrf_score(ranks: list[int], k: int = 60) -> float:
    return sum(1.0 / (k + r) for r in ranks)


def build_candidate_pool(conn, source_text: str,
                         hypotheses: list[dict],
                         policy: dict) -> tuple[list[dict], dict]:
    """授权候选池：多假设召回 → RRF 融合 → 去重 → 有限 cap。

    返回 (pool, coverage)。pool 每项为 surface 候选（含 memory.get 当前
    完整事件正文——judge 必须读全文判断，不得拿标题/片段代替）。
    """
    coverage: dict = {"queries": 0, "event": "none", "dense_event": "none",
                      "union_cap": policy["candidate_union_cap"],
                      "per_hypothesis_cap": policy["per_hypothesis_cap"]}
    degraded: list[str] = []
    plans = _hypothesis_query_plans(source_text, hypotheses, policy)

    # ranks[resource_ref] = 各查询中的名次（1-based）；
    # membership[resource_ref] = 命中它的假设集合（judge 按事件分候选）
    ranks: dict[str, list[int]] = {}
    membership: dict[str, set[str]] = {}
    first_seen: dict[str, dict] = {}
    for hyp_id, plan in plans:
        cov: dict = {}
        cards = candidate_service.event_candidates(
            conn, plan, set(), cov, degraded)
        coverage["queries"] += 1
        for key in ("event", "dense_event"):
            val = cov.get(key)
            if val and val not in ("none",):
                # 不把 partial 覆盖成 complete：取最保守值
                cur = coverage.get(key)
                if cur in (None, "none", "complete_within_scope"):
                    coverage[key] = val
        ordered = sorted(
            cards, key=lambda c: -(c.get("score") or 0)) or cards
        for pos, card in enumerate(ordered, start=1):
            ref = card["resource_ref"]
            ranks.setdefault(ref, []).append(pos)
            membership.setdefault(ref, set()).add(hyp_id)
            first_seen.setdefault(ref, card)

    if not ranks:
        coverage["pool_size"] = 0
        return [], coverage

    fused = sorted(ranks, key=lambda ref: (-_rrf_score(ranks[ref]), ref))
    fused = fused[: policy["candidate_union_cap"]]
    cards = [first_seen[ref] for ref in fused]
    candidate_service.attach_event_evidence(conn, cards)
    deduped = fusion.dedupe_by_resource(cards) if cards else []

    pool: list[dict] = []
    for card in deduped:
        mid = card["memory_id"]
        full = mem_service.get(conn, mid)
        text = full.get("text") or ""
        entry = {
            "resource_ref": card["resource_ref"],
            "memory_id": mid,
            "content_version": str(card.get("content_version") or
                                   full.get("version") or ""),
            "memory_date": full.get("memory_date"),
            "date_confidence": full.get("date_confidence"),
            "occurred_start": full.get("occurred_start"),
            "occurred_end": full.get("occurred_end"),
            "original_title": full.get("original_title"),
            "categories": full.get("categories") or [],
            "event_text": text,
            "body_hash": hashlib.sha256(
                text.encode("utf-8")).hexdigest(),
            "body_bytes": len(text.encode("utf-8")),
            "domain_versions": domain_version_vector(conn, mid),
            "evidence": card.get("evidence") or [],
            "rrf_score": _rrf_score(ranks[card["resource_ref"]]),
            "hypotheses": sorted(membership[card["resource_ref"]]),
        }
        pool.append(entry)
    coverage["pool_size"] = len(pool)
    if degraded:
        coverage["degraded"] = sorted(set(degraded))
    return pool, coverage


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
