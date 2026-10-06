"""words 专项语义检索（recall-closure S08/WP05）。

- 索引父单位 = 稳定 word_id（一条完整 authored our_word 一个向量；
  不把所有话拼一个向量，不塞进 event 向量空间）；
- 向量身份绑定：词文本 hash + speaker/expression_kind/source_ref/
  所属 memory 版本 + 语料 generation（wordbody-v1）——任一变化失效；
- 阈值独立于 event（待专项评测校准，不复制 event 经验值）；
- provider 未配置显式不可用，不伪造相似度（HYBRID-07 同源原则）。
"""
from __future__ import annotations

import sqlite3

from .. import config
from . import projection

try:  # 与 semantic.py 同源复用（同一模型/预处理）
    from .semantic import MODEL_NAME, embed, _cache_path  # noqa: F401
except ImportError:  # pragma: no cover
    embed = None

CORPUS_GENERATION = "wordbody-v1"


def _active_model_key() -> str:
    """复审#6：model key 绑定实际 provider 模型身份——BGE/Qwen 换代
    时旧向量整体失效，512/2560 维缓存不混用。"""
    from .. import config
    from .semantic import (LOCAL_PROVIDERS, MODEL_NAME, QWEN_MODEL_NAME)
    if config.SEMANTIC_PROVIDER == "qwen3e4b":
        return f"{QWEN_MODEL_NAME}|{CORPUS_GENERATION}"
    return f"{MODEL_NAME}|{CORPUS_GENERATION}"


def _active_dim() -> int:
    from .. import config
    from .semantic import QWEN_MODEL_DIM, MODEL_DIM
    return (QWEN_MODEL_DIM
            if config.SEMANTIC_PROVIDER == "qwen3e4b" else MODEL_DIM)


def _embed_texts(texts: list[str]):
    """按当前 provider 编码文档侧（无 instruct）。"""
    from .. import config
    if config.SEMANTIC_PROVIDER == "qwen3e4b":
        from . import qwen_embed
        return qwen_embed.embed(list(texts))
    return embed(list(texts))


def _embed_query(query: str):
    """查询侧：BGE 前缀 / Qwen 官方 instruct。"""
    from .. import config
    if config.SEMANTIC_PROVIDER == "qwen3e4b":
        from . import qwen_embed
        return qwen_embed.embed_query(query)
    from . import projection as _pj
    from .semantic import QUERY_PREFIX
    return embed([QUERY_PREFIX + _pj.normalize_search_text(query)])[0]


def _provider_active() -> bool:
    from .. import config
    from .semantic import LOCAL_PROVIDERS
    return (config.SEMANTIC_PROVIDER in LOCAL_PROVIDERS
            and embed is not None)
#: S08：words 相似度阈值独立配置——当前为工程初值，专项评测后校准
WORDS_COSINE_THRESHOLD = float(
    __import__("os").environ.get("MARIPOSA_WORDS_SEMANTIC_THRESHOLD",
                                 "0.51"))
WORDS_REINDEX_BUDGET = int(
    __import__("os").environ.get("MARIPOSA_WORDS_REINDEX_BUDGET", "20"))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
CREATE TABLE IF NOT EXISTS word_embeddings(
  word_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  word_fingerprint TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL
)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_word_embeddings_model
  ON word_embeddings(model)""")


def _word_fingerprint(text: str, speaker, expression_kind, source_ref,
                      memory_version) -> str:
    import hashlib
    import json as _json
    basis = _json.dumps({
        "text": text, "speaker": speaker,
        "expression_kind": expression_kind,
        "source_ref": source_ref,
        "memory_version": str(memory_version or ""),
        "corpus": CORPUS_GENERATION,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def reindex_word(conn, word_id: str, text: str, speaker, expression_kind,
                 source_ref, memory_version) -> bool:
    """按当前词身份重建向量；无文本/无 provider 清理并返回 False。"""
    ensure_schema(conn)
    fp = _word_fingerprint(text, speaker, expression_kind, source_ref,
                           memory_version)
    cached = conn.execute(
        "SELECT 1 FROM word_embeddings WHERE word_id=? AND model=?"
        " AND word_fingerprint=?", (word_id, _active_model_key(), fp)
    ).fetchone()
    if cached:
        return True
    conn.execute("DELETE FROM word_embeddings WHERE word_id=?", (word_id,))
    if not text or not text.strip() or not _provider_active():
        return False
    from datetime import datetime, timezone
    vec = _embed_texts([text])[0]
    import numpy as _np
    vec = _np.asarray(list(vec), dtype=_np.float32) if not hasattr(
        vec, "tobytes") else vec
    conn.execute(
        "INSERT INTO word_embeddings(word_id, model, word_fingerprint,"
        " dim, vector, created_at) VALUES(?,?,?,?,?,?)",
        (word_id, _active_model_key(), fp, _active_dim(),
         vec.tobytes(),
         datetime.now(timezone.utc).isoformat()))
    return True


def words_semantic_search(conn, query: str, limit: int = 20,
                          extra_where: list[str] | None = None,
                          extra_params: list[str] | None = None) -> dict:
    """可见 words 上余弦检索。候选定位复用 words_search_docs 池
    （speaker/日期等条件由调用方 SQL 前置），返回 word 命中卡。

    全量审计 P1-04：不再用裸 list 表达"结果"与"运行状态"两种语义
    ——返回 {"hits", "pending_vectors", "provider_active"}：
    provider_active=True + 0 hits = 正常搜完零命中（complete）；
    provider_active=False = provider 未配置（unavailable）；
    pending_vectors>0 = 部分向量未就绪（partial）。"""
    if not _provider_active() or not (query or "").strip():
        return {"hits": [], "pending_vectors": 0,
                "provider_active": False}
    ensure_schema(conn)
    scope = list(extra_where or [])
    sp = list(extra_params or [])
    pool_sql = ("SELECT w.word_id, w.text, w.speaker,"
                " w.expression_kind, w.source_ref,"
                " w.source_binding_version, w.memory_id,"
                " m.current_version_no FROM memory_our_words w"
                " JOIN memories m ON m.memory_id = w.memory_id"
                " WHERE m.visibility='active'"
                " AND m.compression_state='full'")
    if scope:
        pool_sql += " AND " + " AND ".join(scope)
    rows = conn.execute(pool_sql, sp).fetchall()
    if not rows:
        return {"hits": [], "pending_vectors": 0,
                "provider_active": True}
    budget = WORDS_REINDEX_BUDGET
    pending = 0
    for r in rows:
        fp = _word_fingerprint(r["text"], r["speaker"],
                               r["expression_kind"], r["source_ref"],
                               r["current_version_no"])
        valid = conn.execute(
            "SELECT 1 FROM word_embeddings WHERE word_id=? AND model=?"
            " AND word_fingerprint=?",
            (r["word_id"], _active_model_key(), fp)).fetchone()
        if not valid:
            if budget > 0:
                reindex_word(conn, r["word_id"], r["text"], r["speaker"],
                             r["expression_kind"], r["source_ref"],
                             r["current_version_no"])
                budget -= 1
            else:
                pending += 1
    import numpy as np
    qvec = _embed_query(query)
    scored = []
    for r in rows:
        fp = _word_fingerprint(r["text"], r["speaker"],
                               r["expression_kind"], r["source_ref"],
                               r["current_version_no"])
        vrow = conn.execute(
            "SELECT vector FROM word_embeddings WHERE word_id=? AND"
            " model=? AND word_fingerprint=?",
            (r["word_id"], _active_model_key(), fp)).fetchone()
        if vrow is None:
            continue
        v = np.frombuffer(vrow["vector"], dtype=np.float32)
        score = float(qvec @ v / (np.linalg.norm(qvec)
                                  * np.linalg.norm(v)))
        if score >= WORDS_COSINE_THRESHOLD:
            scored.append({
                "word_id": r["word_id"], "memory_id": r["memory_id"],
                "speaker": r["speaker"],
                "expression_kind": r["expression_kind"],
                "source_ref": r["source_ref"],
                "source_binding_version": int(
                    r["source_binding_version"] or 0),
                "text": r["text"],
                "matched_by": ["semantic"],
                "score": round(score, 4),
            })
    scored.sort(key=lambda x: -x["score"])
    return {"hits": scored[:limit], "pending_vectors": pending,
            "provider_active": True}

