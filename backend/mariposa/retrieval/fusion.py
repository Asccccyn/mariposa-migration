"""排名融合与去重（v1.4 §5.3）。

RRF：各 family（lexical/dense）先在族内合并同资源生成唯一 1-based
排名，再按 rrf = Σ w/(k + rank) 融合；不直接相加 BM25 原始分（越小越
相关）与余弦值（越大越相关）。同一资源命中多个分类/alternate 不重复
叠票。跨通道（event/words/raw）不比较未校准的原始分数。
"""
from __future__ import annotations

from .. import config


def family_rank(hits: list[dict], key: str = "resource_ref") -> list[dict]:
    """族内去重并生成唯一排名（1-based，按入参顺序=原排序）。

    同资源多次出现（多分类命中、多 alternate 命中）保留最靠前的排名，
    合并命中通路，不叠票。
    """
    seen: dict[str, dict] = {}
    for h in hits:
        ref = h.get(key)
        if ref in seen:
            prev = seen[ref]
            prev["matched_by"] = list(dict.fromkeys(
                prev.get("matched_by", []) + h.get("matched_by", [])))
            continue
        item = dict(h)
        item["matched_by"] = list(h.get("matched_by", []))
        seen[ref] = item
    ranked = list(seen.values())
    for i, item in enumerate(ranked, start=1):
        item["family_rank"] = i
    return ranked


def rrf_fuse(families: dict[str, list[dict]],
             weights: dict[str, float] | None = None,
             k: int | None = None) -> list[dict]:
    """多 family RRF 融合；未出现在该 family 的资源贡献 0。"""
    k = k if k is not None else config.RECALL_RRF_K
    weights = weights or {}
    scores: dict[str, dict] = {}
    for name, ranked in families.items():
        w = float(weights.get(name, 1.0))
        for item in ranked:
            ref = item["resource_ref"]
            entry = scores.setdefault(ref, {
                "resource_ref": ref, "rrf_score": 0.0,
                "families": [], "matched_by": [], "hit": dict(item)})
            entry["rrf_score"] += w / (k + item["family_rank"])
            entry["families"].append(name)
            entry["matched_by"] = list(dict.fromkeys(
                entry["matched_by"] + item.get("matched_by", [])))
            # 保留第一个见到的通道内容（族顺序即优先级）
            for f in ("channel", "representation", "content_version",
                      "representation_version", "memory_id", "word_id",
                      "expression_kind", "speaker", "source_ref",
                      "memory_date", "score", "excerpt", "truncated",
                      "matched_fields", "evidence"):
                if f in item and f not in entry["hit"]:
                    entry["hit"][f] = item[f]
    fused = sorted(scores.values(), key=lambda x: -x["rrf_score"])
    out = []
    for e in fused:
        merged = dict(e["hit"])
        merged["resource_ref"] = e["resource_ref"]
        merged["rrf_score"] = round(e["rrf_score"], 6)
        merged["rank_families"] = e["families"]
        merged["matched_by"] = e["matched_by"]
        out.append(merged)
    return out


def dedupe_by_resource(candidates: list[dict],
                       union_cap: int | None = None) -> list[dict]:
    """跨家族/通道去重：资源稳定 ID 唯一（多分类记忆只计一个资源）。"""
    union_cap = union_cap or config.RECALL_CANDIDATE_UNION_CAP
    seen: set[str] = set()
    out = []
    for c in candidates:
        ref = c["resource_ref"]
        if ref in seen:
            continue
        seen.add(ref)
        out.append(c)
        if len(out) >= union_cap:
            break
    return out


# ---------- 作用域内词法检索（HYBRID-06：隔离统计） ----------

def _phrase_in(phrase_tokens: list[str], doc_tokens_joined: str) -> bool:
    if not phrase_tokens:
        return False
    return " ".join(phrase_tokens) in doc_tokens_joined


def scoped_lexical_search(conn, where: list[str], params: list,
                          terms: list[list[str]],
                          phrases: list[list[str]],
                          select_extra: str = "",
                          pool_cap: int = 2000) -> dict:
    """作用域内 BM25 风格词法检索（v1.4 §5.2）。

    候选池先按 where 过滤（权限/日期/分类/负向条件前置），打分的词频
    统计只来自池内文档——加入无权语料不改变本 scope 的可见排名与计数，
    不只是"结果最后被过滤"（HYBRID-06）。terms=OR 组（任一命中），
    phrases=AND 组（全部连续命中）。返回 {rows, pool_size, capped}。
    """
    pool = conn.execute(
        "SELECT m.memory_id, m.memory_date, m.compression_state,"
        " m.current_version_no, rd.projection_kind, rd.search_text,"
        f" rd.whitelist_body{select_extra}"
        " FROM memories m JOIN retrieval_documents rd"
        " ON rd.memory_id = m.memory_id"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY m.memory_date DESC LIMIT ?",
        params + [pool_cap]).fetchall()
    docs = []
    for r in pool:
        toks = r["search_text"].split()
        docs.append({"row": r, "tf": {}, "n": len(toks),
                     "joined": r["search_text"]})
    # 池内词频文档频率（df）→ IDF
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d["joined"].split()):
            df[t] = df.get(t, 0) + 1
    n_docs = len(docs)
    k1, b = 1.5, 0.75
    avgdl = (sum(d["n"] for d in docs) / n_docs) if n_docs else 0.0

    def score_doc(d: dict, query_tokens: list[str]) -> float:
        s = 0.0
        counts: dict[str, int] = {}
        for t in d["joined"].split():
            counts[t] = counts.get(t, 0) + 1
        for qt in query_tokens:
            tf = counts.get(qt, 0)
            if not tf:
                continue
            idf = ((n_docs - df.get(qt, 0) + 0.5) /
                   (df.get(qt, 0) + 0.5) + 1.0)
            denom = tf + k1 * (1 - b + b * (d["n"] / avgdl if avgdl else 0))
            s += idf * (tf * (k1 + 1)) / denom
        return s

    hits = []
    for d in docs:
        joined = d["joined"]
        if phrases and not all(_phrase_in(p, joined) for p in phrases):
            continue
        if terms and not any(_phrase_in(t, joined) for t in terms):
            continue
        if not phrases and not terms:
            continue  # 空表达由调用方走浏览
        qtoks = [t for grp in phrases for t in grp] + \
                [t for grp in terms for t in grp]
        hits.append({"row": d["row"],
                     "scoped_score": round(score_doc(d, qtoks), 6)})
    hits.sort(key=lambda h: (-h["scoped_score"],
                             h["row"]["memory_date"] or "",
                             h["row"]["memory_id"]))
    return {"rows": hits, "pool_size": n_docs,
            "capped": n_docs >= pool_cap}
