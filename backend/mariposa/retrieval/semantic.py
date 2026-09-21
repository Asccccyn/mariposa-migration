"""语义检索：本地真实 provider（ONNX bge-small-zh-v1.5，512 维）。

规则（§8.3）：
- 仅对**当前有效投影**建立/使用向量；projection_hash 与投影不一致即失效，
  不存在"旧正文向量残留命中"的通道；
- 遗忘桶的向量由批准摘要生成（旧正文向量在投影替换时失效）；
- provider 未配置时全部路径显式 degraded，不伪造相似度。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import numpy as np

from .. import config
from . import projection

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_DIM = 512
# 阈值实测分布（bge-small-zh + query 前缀 + 双侧投影规范化，ONNX 确定性推理）：
#   有效语义改写 >= 0.535；跨话题泛化误召回 <= 0.504；无关 < 0.40
# 阈值版本化可配（policy 变更入审计）；返回始终带 score 供调用方再筛。
COSINE_THRESHOLD = float(os.environ.get("MARIPOSA_SEMANTIC_THRESHOLD", "0.51"))
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："  # BGE 官方检索用法

_provider = None


def _cache_path() -> str:
    env = os.environ.get("FASTEMBED_CACHE_PATH")
    if env:
        return env
    return str(config.RUNTIME_DIR / "models")


def get_provider():
    """返回本地 ONNX provider；SEMANTIC_PROVIDER 未配置时 None。"""
    global _provider
    if config.SEMANTIC_PROVIDER != "local_bge_zh":
        return None
    if _provider is None:
        from fastembed import TextEmbedding
        _provider = TextEmbedding(MODEL_NAME, cache_dir=_cache_path())
    return _provider


def embed(texts: list[str]) -> list[np.ndarray]:
    p = get_provider()
    if p is None:
        raise RuntimeError("semantic provider not configured")
    return [np.asarray(v, dtype=np.float32) for v in p.embed(texts)]


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表（与 schema.py v10 等效；供旧库在线升级）。"""
    conn.execute("""
CREATE TABLE IF NOT EXISTS memory_embeddings(
  memory_id TEXT NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  projection_hash TEXT NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, model)
)""")


def reindex(conn: sqlite3.Connection, memory_id: str) -> bool:
    """按当前有效投影重建该桶向量；无投影/无 provider 时清理其向量。"""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT search_text_hash FROM retrieval_documents WHERE memory_id=?",
        (memory_id,)).fetchone()
    if config.SEMANTIC_PROVIDER != "local_bge_zh":
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
        return False
    if row is None:
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory_id,))
        return False
    # 命中缓存（同投影 hash 已有向量）则不重算
    cached = conn.execute(
        "SELECT 1 FROM memory_embeddings WHERE memory_id=? AND model=?"
        " AND projection_hash=?", (memory_id, MODEL_NAME, row["search_text_hash"])
    ).fetchone()
    if cached:
        return True
    text_row = conn.execute(
        "SELECT search_text FROM retrieval_documents WHERE memory_id=?",
        (memory_id,)).fetchone()
    vec = embed([text_row["search_text"]])[0]
    from datetime import datetime, timezone
    conn.execute(
        "INSERT OR REPLACE INTO memory_embeddings(memory_id, model, dim,"
        " projection_hash, vector, created_at) VALUES(?,?,?,?,?,?)",
        (memory_id, MODEL_NAME, MODEL_DIM, row["search_text_hash"],
         vec.tobytes(), datetime.now(timezone.utc).isoformat()))
    return True


def semantic_search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """在有效投影集合上做精确余弦 top-k；返回带 matched_by 的命中。"""
    if config.SEMANTIC_PROVIDER != "local_bge_zh":
        return []
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT rd.memory_id, rd.search_text, rd.search_text_hash,"
        " rd.projection_kind, m.compression_state FROM retrieval_documents rd"
        " JOIN memories m ON m.memory_id = rd.memory_id"
        " WHERE m.visibility='active'").fetchall()
    if not rows:
        return []
    # 补齐缺失/失效向量（迟到向量原则：hash 校验后才可安装）
    for r in rows:
        valid = conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_id=? AND model=?"
            " AND projection_hash=?",
            (r["memory_id"], MODEL_NAME, r["search_text_hash"])).fetchone()
        if not valid:
            reindex(conn, r["memory_id"])
    from . import projection as _pj
    qvec = embed([QUERY_PREFIX + _pj.normalize_search_text(query)])[0]
    scored = []
    for r in conn.execute(
            "SELECT e.memory_id, e.vector, e.projection_hash, rd.search_text_hash,"
            " rd.projection_kind, m.compression_state FROM memory_embeddings e"
            " JOIN retrieval_documents rd ON rd.memory_id = e.memory_id"
            " JOIN memories m ON m.memory_id = e.memory_id"
            " WHERE e.model=? AND m.visibility='active'"
            " AND e.projection_hash = rd.search_text_hash",  # 仅有效投影向量
            (MODEL_NAME,)):
        v = np.frombuffer(r["vector"], dtype=np.float32)
        score = float(qvec @ v / (np.linalg.norm(qvec) * np.linalg.norm(v)))
        if score >= COSINE_THRESHOLD:
            scored.append({
                "memory_id": r["memory_id"],
                "matched_by": ("summary_semantic"
                               if r["compression_state"] == "forgotten_summary"
                               else "semantic"),
                "projection_kind": r["projection_kind"],
                "score": round(score, 4),
            })
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]
