"""独立 words 检索通道（v1.3 §7.2/§8 / v1.4 §7、P3）。

"我们的话"使用独立 words channel，不混入普通 event ranking：本模块
只读 memory_our_words 的派生索引（words_search_docs/words_fts，可重建），
与事件投影完全隔离；仅出现在 our_words 的词不会让 event-only 命中
（WORD-01，event 侧由投影构造层结构性保证）。

遗忘表示（§23 未决业务项）：forgotten memory 的原有 our_words 显式
检索保持 disabled / PENDING_OWNER_DECISION——索引重建物理排除遗忘行，
读取时再校验当前表示兜底；不借 raw 回退、relation 或旧缓存绕过
（RAWX-05）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .. import config, db
from ..errors import Forbidden
from ..errors import NotFound
from . import projection
from . import evidence as evidence_mod

WORDS_INDEX_META_KEY = "words_index_fingerprint"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(conn) -> str:
    """正表指纹：word_id/speaker/text/expression_kind 的稳定摘要。"""
    rows = conn.execute(
        "SELECT word_id, speaker, text, expression_kind, memory_id"
        " FROM memory_our_words ORDER BY word_id").fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['word_id']}|{r['speaker']}|{r['text']}|"
                 f"{r['expression_kind']}|{r['memory_id']}\n".encode())
    return h.hexdigest()


def rebuild_words_index(conn) -> int:
    """全量重建派生索引：只收录 active 且未遗忘（full）memory 的话语。

    绑定当前 memory 版本与表示；遗忘生效 / restore / 话语变更使指纹
    变化，下次读取触发重建（读取时校验兜底，v1.4 §7.3/§10.3）。
    """
    conn.execute("DELETE FROM words_fts")
    conn.execute("DELETE FROM words_search_docs")
    rows = conn.execute(
        "SELECT w.word_id, w.memory_id, w.text, m.current_version_no,"
        " m.compression_state FROM memory_our_words w"
        " JOIN memories m ON m.memory_id = w.memory_id"
        " WHERE m.visibility='active' AND m.compression_state='full'"
        " ORDER BY w.word_id").fetchall()
    now = _now()
    for r in rows:
        norm = projection.normalize_search_text(r["text"])
        conn.execute(
            "INSERT INTO words_search_docs(word_id, memory_id,"
            " memory_version_no, memory_compression_state, text_norm,"
            " text_hash, projection_version, built_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (r["word_id"], r["memory_id"], r["current_version_no"],
             r["compression_state"], norm,
             hashlib.sha256(r["text"].encode()).hexdigest(),
             config.WORDS_PROJECTION_VERSION, now))
        conn.execute("INSERT INTO words_fts(word_id, text_norm) VALUES(?,?)",
                     (r["word_id"], norm))
    fp = _fingerprint(conn)
    conn.execute(
        "INSERT INTO mariposa_db_meta(key, value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (WORDS_INDEX_META_KEY, json.dumps(
            {"fingerprint": fp, "built_at": now,
             "projection_version": config.WORDS_PROJECTION_VERSION})))
    return len(rows)


def ensure_index_current(conn) -> bool:
    """读取时校验：正表指纹与派生索引不一致即重建（含遗忘行清除）。"""
    meta = conn.execute(
        "SELECT value FROM mariposa_db_meta WHERE key=?",
        (WORDS_INDEX_META_KEY,)).fetchone()
    fp = _fingerprint(conn)
    if meta is None:
        rebuild_words_index(conn)
        return True
    try:
        recorded = json.loads(meta["value"]).get("fingerprint")
    except (ValueError, TypeError):
        recorded = None
    if recorded != fp:
        rebuild_words_index(conn)
        return True
    return False


def _g(row, key, default=None):
    """sqlite3.Row 与 dict 统一取值。"""
    try:
        return row[key] if key in row.keys() else default
    except AttributeError:
        return row.get(key, default)


def _source_ref_valid(conn, source_ref) -> bool:
    """来源仍有效。CB-039（2026-10-02 审计 P2）：现行 source_msg:<id>
    按 Source 层当前事实校验（存在且 published）——此前恒 False，真实
    已发布来源的 verbatim 也被降级 word_unverified；旧 raw 前缀随
    legacy 退役一律 invalid（不查已删除的表）。"""
    if not isinstance(source_ref, str) or \
            not source_ref.startswith("source_msg:"):
        return False
    if conn is None:
        return False
    msg_id = source_ref[len("source_msg:"):]
    row = conn.execute(
        "SELECT published FROM source_messages WHERE id=? OR"
        " provider_message_id=?", (msg_id, msg_id)).fetchone()
    return row is not None and bool(row["published"])


def _word_evidence(row, conn=None) -> list[dict]:
    """expression_kind → evidence_kind（含来源有效性校验，EVID-03）。

    CB-039：sparse 与 dense 共用本构造——证据等级取决于当前
    provenance（expression_kind + source_ref 当前事实 + 撤销换代），
    不取决于命中通道；来源被显式撤销（换代>0 且当前无来源）不是
    "从未有来源"，不签无外部来源核验路径的 word_verbatim。
    """
    kind = _g(row, "expression_kind")
    source_ref = _g(row, "source_ref")
    keys = row.keys() if hasattr(row, "keys") else ()
    version = str(_g(row, "memory_version_no",
                     _g(row, "current_version_no")) or "")
    gen = int(_g(row, "source_binding_version", 0) or 0)
    wid = _g(row, "word_id")
    text = _g(row, "text") or ""
    if kind == "verbatim":
        if not source_ref:
            if gen > 0:
                # 来源被显式撤销：保留撤销状态，不按"从未有来源"的
                # 逐字声明路径签 verified
                return [evidence_mod.make_evidence(
                    "word_unverified", "our_words.text", text,
                    f"our_word:{wid}", source_version=version)] + [
                    evidence_mod.make_evidence(
                        "structured_fact", "our_words.source_ref",
                        "", f"our_word:{wid}",
                        structured_value={
                            "source_ref_state": "revoked",
                            "source_binding_version": gen})]
            # 无来源绑定且从未纠错：正式话语记录本身即逐字声明
            return [evidence_mod.make_evidence(
                "word_verbatim", "our_words.text", text,
                f"our_word:{wid}", source_version=version)]
        if _source_ref_valid(conn, source_ref):
            return [evidence_mod.make_evidence(
                "word_verbatim", "our_words.text", text,
                f"our_word:{wid}", source_version=version)]
        # 来源版本变化/失效后不继续返回 verified word_verbatim；
        # 裁定（2026-10-04）：gap 细分——legacy raw 前缀/不可解析
        # 引用不得伪装成有效 provenance
        _state = ("legacy_raw_prefix"
                  if isinstance(source_ref, str)
                  and source_ref.startswith("raw_msg:")
                  else "invalid_or_missing")
        return [evidence_mod.make_evidence(
            "word_unverified", "our_words.text", text,
            f"our_word:{wid}", source_version=version)] + [
            evidence_mod.make_evidence(
                "structured_fact", "our_words.source_ref",
                "", f"our_word:{wid}",
                structured_value={"source_ref_state": _state})]
    if kind == "paraphrase":
        return [evidence_mod.make_evidence(
            "word_paraphrase", "our_words.text", text,
            f"our_word:{wid}", source_version=version)]
    return [evidence_mod.make_evidence(
        "word_unverified", "our_words.text", text,
        f"our_word:{wid}", source_version=version)]


def words_search(conn, plan: dict, limit: int | None = None) -> dict:
    """独立 words 通道检索：FTS 匹配话语正文，返回带证据分级的候选。

    返回 {hits, coverage, forgotten_visible, index_rebuilt}。speaker
    条件按正式 speaker 字段处理（WORD-03），不凭文本或角色猜。
    """
    from . import query_plan as qp

    limit = limit or config.RECALL_LEXICAL_K
    rebuilt = ensure_index_current(conn)
    terms_expr = qp.compile_terms(plan.get("lexical_terms") or [])
    phrases_expr = " AND ".join(
        qp.compile_phrase(p) for p in (plan.get("exact_phrases") or []) if p)
    parts: list[str] = []
    if phrases_expr:
        parts.append(phrases_expr)
    if terms_expr:
        parts.append(f"({terms_expr})")
    expr = " AND ".join(parts) if parts else None

    # 三轮复审#4：正/负说话人与日期条件统一走 source_scope（与 words
    # dense、raw Round2 同一语义；source_date_excluded 在此落地——
    # 此前稀疏路只过滤了 speaker_excluded，排除日期两条都漏出来）
    scope = qp.source_scope(plan)

    where = ["m.visibility='active'", "m.compression_state='full'"]
    params: list = []
    scope_where, scope_params = qp.source_scope_sql(scope, "w.speaker",
                                                    "m.memory_date")
    where += scope_where
    params += scope_params
    # CB-047：QueryPlan 的 memory 维度约束（categories/mood_tags 及
    # match 模式）同样作用于 words 池——schema 接受的过滤不得静默
    # 丢弃（与 event 通道同一 _pool_where 语义，m. 列在本 JOIN 可用）
    ec = plan.get("explicit_constraints") or {}
    mem_filters = {k: ec[k] for k in
                   ("categories", "mood_tags", "category_match",
                    "mood_match") if ec.get(k)}
    if mem_filters:
        from .search import _pool_where
        pool_where, pool_params = _pool_where(mem_filters)
        where += pool_where
        params += pool_params

    # 裁定（2026-10-04 江乔生）：S05/S06 覆盖 words——任何不在当前
    # 检索可见候选宇宙中的文档不得参与 BM25 corpus statistics/IDF。
    # SQLite FTS5 的 bm25() 用全库统计，追加 scope 外话语会翻转可见
    # 候选排名；改为与 event 通道同款 scoped_bm25（scope 池内存评分）。
    # FTS MATCH 仍作召回下限（短语词法），排序与命中判定以 scoped
    # 评分为准（term 组内相邻+有序，HYBRID-04 同步 F17）。
    from . import scoped_bm25
    pool_sql = ("SELECT w.word_id, w.memory_id, w.ordinal, w.speaker,"
                " w.text, w.expression_kind, w.source_ref,"
                " w.source_binding_version, m.memory_date,"
                " m.current_version_no"
                " FROM memory_our_words w"
                " JOIN memories m ON m.memory_id = w.memory_id")
    if expr:
        pool_sql += (" JOIN words_fts ON words_fts.word_id = w.word_id"
                     " JOIN words_search_docs d ON d.word_id = w.word_id")
        pool_sql += " WHERE words_fts MATCH ? AND " + " AND ".join(where)
        pool_params = [expr] + params
    else:
        pool_sql += " WHERE " + " AND ".join(where)
        pool_params = list(params)
    pool = conn.execute(pool_sql, pool_params).fetchall()

    term_groups, phrase_groups = qp.plan_token_groups(plan)
    scored_by_word: dict = {}
    if term_groups or phrase_groups:
        from . import projection as _proj
        docs = [{"owner": r["word_id"], "field": "our_words",
                 "tokens": scoped_bm25._doc_tokens(
                     " ".join(_proj.tokenize(r["text"] or "")))}
                for r in pool]
        for e in scoped_bm25.score_documents(docs, term_groups,
                                             phrase_groups):
            scored_by_word[e["owner"]] = e["score"]
        rows = sorted((r for r in pool if r["word_id"] in scored_by_word),
                      key=lambda r: (-scored_by_word[r["word_id"]],
                                     r["word_id"]))
    else:
        rows = sorted(pool,
                      key=lambda r: (r["memory_date"] or "", r["word_id"]),
                      reverse=True)
    rows = rows[:limit + 1]
    # CB-013（2026-10-02 审计 P1）：limit+1 探测出的越界行表达
    # has_more——此前直接丢弃并把 coverage 恒签 complete_within_scope，
    # 截断浏览窗口冒充当前 scope 完成（可提前升级 Raw 并漏掉后部
    # 已有逐字证据）。与 event 通道一致标 partial_topk_window。
    has_more = len(rows) > limit

    hits = []
    for r in rows[:limit]:
        excerpt_text, truncated = evidence_mod.excerpt(r["text"])
        hits.append({
            "resource_ref": f"our_word:{r['word_id']}",
            "candidate_ref": f"our_word:{r['word_id']}",
            "channel": "words",
            "representation": "active_words",
            "content_version": str(r["current_version_no"]),
            "representation_version": config.WORDS_PROJECTION_VERSION,
            "projection_version": config.WORDS_PROJECTION_VERSION,
            "memory_id": r["memory_id"],
            "word_id": r["word_id"],
            "ordinal": r["ordinal"],
            "speaker": r["speaker"],
            "expression_kind": r["expression_kind"],
            "memory_date": r["memory_date"],
            "matched_by": (["words_keyword"] if scored_by_word
                           else []),
            "matched_fields": ["our_words.text"],
            "excerpt": excerpt_text,
            "truncated": truncated,
            "evidence": _word_evidence(r, conn),
        })

    forgotten = conn.execute(
        "SELECT COUNT(*) AS n FROM memory_our_words w JOIN memories m"
        " ON m.memory_id = w.memory_id WHERE m.compression_state="
        " 'forgotten_summary'").fetchone()["n"]
    return {
        "hits": hits,
        "coverage": ("partial_topk_window" if has_more
                     else "complete_within_scope"),
        "has_more": has_more,
        "lexical_scorer": scoped_bm25.SCORER_VERSION,
        "forgotten_words_count": forgotten,
        "forgotten_recall": config.WORDS_FORGOTTEN_RECALL,
        "forgotten_decision_state": config.WORDS_FORGOTTEN_DECISION_STATE,
        "index_rebuilt": rebuilt,
    }


def get_word(word_id: str) -> dict:
    """memory.words.get：按 word_id 读取单条话语（当前表示校验）。"""
    with db.formal() as conn:
        row = conn.execute(
            "SELECT w.*, m.visibility, m.compression_state,"
            " m.current_version_no FROM memory_our_words w"
            " JOIN memories m ON m.memory_id = w.memory_id"
            " WHERE w.word_id=?", (word_id,)).fetchone()
        if row is None:
            raise NotFound("word not found", word_id=word_id)
        if row["visibility"] != "active":
            raise Forbidden("所属记忆当前不可见", code="INVALID_STATE",
                            word_id=word_id)
        if row["compression_state"] != "full":
            raise Forbidden(
                "所属记忆已遗忘；遗忘后的 words 显式检索保持 disabled"
                f"（{config.WORDS_FORGOTTEN_DECISION_STATE}）",
                code="WORDS_FORGOTTEN_DISABLED", word_id=word_id)
        d = dict(row)
        d.pop("visibility", None)
        d["content_role"] = "retrieved_memory"
        d["instruction_authority"] = "none"
        d["evidence"] = _word_evidence(row, conn)
    return d
