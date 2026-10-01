"""scope 内真 BM25（recall-closure S06 / WP03）。

`scope-stage-bm25-v1`：在**当前获准字段/片段**的 token 文档集合上
执行标准 BM25（对数 IDF，k1=1.5、b=0.75 为版本化工程参数）：

    IDF(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
    score(d,q) = Σ IDF(t) * tf(t,d)*(k1+1) /
                 (tf(t,d) + k1*(1 - b + b*len(d)/avgdl))

N/df/avgdl 只来自授权＋阶段过滤后的文档集合——不让隐藏、撤权或
本阶段禁用字段改变可见资源排名（S05/S06）。SQLite bm25() 的统计
基于整个 FTS 表，WHERE 不改变其 IDF，故评分在本模块内完成；
field_fts 仅用于候选定位验证，不作为排序分来源。

查询语义（S04）：terms 组间 OR、组内 token 连续；exact_phrases 组间
AND、组内逐字顺序；重复 query token 先去重；空范围零结果不造分。
"""
from __future__ import annotations

import math

K1 = 1.5
B = 0.75
SCORER_VERSION = "scope-stage-bm25-v1"


def _doc_tokens(text_norm: str) -> list[str]:
    return [t for t in text_norm.split(" ") if t]


def _contains_seq(doc: list[str], seq: list[str]) -> bool:
    """连续子序列检查（词内顺序/连续性，S04）。"""
    if not seq or len(doc) < len(seq):
        return False
    n = len(seq)
    for i in range(len(doc) - n + 1):
        if doc[i:i + n] == seq:
            return True
    return False


def score_documents(
        docs: list[dict],
        term_groups: list[list[str]],
        phrases: list[list[str]]) -> list[dict]:
    """对 (owner_id, field) 文档执行 scope BM25。

    docs: [{"owner": memory_id, "field": field_kind, "tokens": [...]}]
    term_groups: OR 组（组内连续）；phrases: AND（组内连续）
    返回按 owner 聚合的命中：[{"owner", "fields", "score", "hits"}]，
    score = 该 owner 命中文档的最大 BM25 分（S06：不累加选票），
    顺序按分数降序、稳定 owner id。
    """
    if not docs:
        return []
    # 组装去重后的查询 token 集（S06：重复 query token 先去重）
    q_tokens: list[str] = []
    for g in term_groups:
        for t in g:
            if t not in q_tokens:
                q_tokens.append(t)
    for g in phrases:
        for t in g:
            if t not in q_tokens:
                q_tokens.append(t)
    if not q_tokens:
        return []

    N = len(docs)
    avgdl = sum(len(d["tokens"]) for d in docs) / N or 1.0
    df: dict[str, int] = {}
    for d in docs:
        seen = set(d["tokens"])
        for t in q_tokens:
            if t in seen:
                df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + (N - dfc + 0.5) / (dfc + 0.5))
           for t, dfc in df.items()}

    by_owner: dict[str, dict] = {}
    for d in docs:
        toks = d["tokens"]
        # phrases：AND 语义——任一 phrase 不满足即该文档不命中
        ok_phrases = all(_contains_seq(toks, p) for p in phrases) if \
            phrases else True
        # 复审#7：term group 整组连续命中才计分（S04：同一个 term 内
        # 保持顺序与连续；组间 OR）——"小路灯"不再被只有"小"的文档
        # 命中；exact_phrase AND (terms OR) 语义由两段共同保证
        doc_set = set(toks)
        matched_terms = []
        if term_groups:
            for g in term_groups:
                g2 = [t for t in g if t]
                if not g2:
                    continue
                if _contains_seq(toks, g2):
                    matched_terms.extend(
                        t for t in g2 if t not in matched_terms)
        else:
            matched_terms = [t for t in q_tokens if t in doc_set]
        if not ok_phrases or not matched_terms:
            continue
        tf = {t: toks.count(t) for t in matched_terms}
        dl = len(toks)
        score = 0.0
        for t in matched_terms:
            denom = tf[t] + K1 * (1 - B + B * dl / avgdl)
            score += idf[t] * tf[t] * (K1 + 1) / denom
        entry = by_owner.setdefault(
            d["owner"], {"owner": d["owner"], "fields": [],
                         "score": 0.0, "hits": {},
                         "excerpt_by_field": {}})
        entry["fields"].append(d["field"])
        entry["hits"][d["field"]] = round(score, 6)
        entry["score"] = max(entry["score"], round(score, 6))
        # 命中窗原文（复审#1）：围绕首个命中 token 的 ±窗口——真实
        # matched_excerpt 由检索层产生，Jev 不再猜内容
        if d["field"] not in entry["excerpt_by_field"]:
            win = _hit_window(d["tokens"], q_tokens)
            if win is not None:
                entry["excerpt_by_field"][d["field"]] = win
    out = sorted(by_owner.values(),
                 key=lambda e: (-e["score"], e["owner"]))
    for e in out:
        e["fields"] = sorted(set(e["fields"]))
    return out


def _hit_window(tokens: list[str], q_tokens: list[str],
                width: int = 120) -> str | None:
    """首个命中 token 周围的有界窗（token 空格拼接）。"""
    for i, t in enumerate(tokens):
        if t in q_tokens:
            lo = max(0, i - width // 3)
            hi = min(len(tokens), lo + width)
            return " ".join(tokens[lo:hi])
    return None
