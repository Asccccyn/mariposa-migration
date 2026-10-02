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


def _word_evidence(row) -> list[dict]:
    """expression_kind → evidence_kind（含来源有效性校验，EVID-03）。"""
    kind = row["expression_kind"]
    source_ref = row["source_ref"]
    keys = row.keys()
    version = str(row["memory_version_no"] if "memory_version_no" in keys
                  else row["current_version_no"])
    if kind == "verbatim":
        if not source_ref:
            # 无 raw 绑定的 verbatim：正式话语记录本身即逐字声明，
            # 无外部来源需要核验
            return [evidence_mod.make_evidence(
                "word_verbatim", "our_words.text", row["text"],
                f"our_word:{row['word_id']}",
                source_version=version)]
        valid = _source_ref_valid(source_ref)
        if valid:
            return [evidence_mod.make_evidence(
                "word_verbatim", "our_words.text", row["text"],
                f"our_word:{row['word_id']}",
                source_version=version)]
        # 来源版本变化/失效后不继续返回 verified word_verbatim
        return [evidence_mod.make_evidence(
            "word_unverified", "our_words.text", row["text"],
            f"our_word:{row['word_id']}",
            source_version=version)] + [
            evidence_mod.make_evidence(
                "structured_fact", "our_words.source_ref",
                "", f"our_word:{row['word_id']}",
                structured_value={"source_ref_state": "invalid_or_missing"})]
    if kind == "paraphrase":
        return [evidence_mod.make_evidence(
            "word_paraphrase", "our_words.text", row["text"],
            f"our_word:{row['word_id']}",
            source_version=version)]
    return [evidence_mod.make_evidence(
        "word_unverified", "our_words.text", row["text"],
        f"our_word:{row['word_id']}",
        source_version=version)]


def _source_ref_valid(source_ref: str) -> bool:
    """来源仍有效（D13，2026-10-01）：旧 raw_msg:/raw_binding: 前缀随
    legacy raw 表退役——一律 invalid（降级 word_unverified），不查已
    删除的表；现行 source_msg: 前缀由 Source 层校验。"""
    return False


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

    sql = ("SELECT w.word_id, w.memory_id, w.ordinal, w.speaker, w.text,"
           " w.expression_kind, w.source_ref, m.memory_date,"
           " m.current_version_no, bm25(words_fts) AS rank"
           " FROM words_fts"
           " JOIN words_search_docs d ON d.word_id = words_fts.word_id"
           " JOIN memory_our_words w ON w.word_id = d.word_id"
           " JOIN memories m ON m.memory_id = w.memory_id")
    if expr:
        sql += " WHERE words_fts MATCH ? AND " + " AND ".join(where)
        params = [expr] + params
        sql += " ORDER BY rank, w.word_id LIMIT ?"
    else:
        sql += " WHERE " + " AND ".join(where) + \
            " ORDER BY m.memory_date DESC, w.word_id LIMIT ?"
    rows = conn.execute(sql, params + [limit + 1]).fetchall()

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
            "matched_by": ["words_keyword"],
            "matched_fields": ["our_words.text"],
            "excerpt": excerpt_text,
            "truncated": truncated,
            "evidence": _word_evidence(r),
        })

    forgotten = conn.execute(
        "SELECT COUNT(*) AS n FROM memory_our_words w JOIN memories m"
        " ON m.memory_id = w.memory_id WHERE m.compression_state="
        " 'forgotten_summary'").fetchone()["n"]
    return {
        "hits": hits,
        "coverage": "complete_within_scope",
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
    d["evidence"] = _word_evidence(row)
    return d
