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

from .. import config
from . import projection

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_DIM = 512
# S07/WP03：向量身份绑定语料 generation——corpus 规则升级时旧向量
# 整体失效重嵌（本值进入 memory_embeddings.model 身份与校验）
CORPUS_GENERATION = "eventbody-v1"
# 阈值实测分布（bge-small-zh + query 前缀 + 双侧投影规范化，ONNX 确定性推理）：
#   有效语义改写 >= 0.535；跨话题泛化误召回 <= 0.504；无关 < 0.40
# 阈值版本化可配（policy 变更入审计）；返回始终带 score 供调用方再筛。
MODEL_KEY = f"{MODEL_NAME}|{CORPUS_GENERATION}"
COSINE_THRESHOLD = float(os.environ.get("MARIPOSA_SEMANTIC_THRESHOLD", "0.51"))
REINDEX_BUDGET = int(os.environ.get("MARIPOSA_SEMANTIC_REINDEX_BUDGET", "20"))
RELATIVE_WINDOW = 0.06
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


def _np():
    import numpy  # 懒加载：核心包不强依赖（provider 未配置时无需 numpy）
    return numpy


def embed(texts: list[str]):
    np = _np()
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
        " AND projection_hash=?", (memory_id, MODEL_KEY, row["search_text_hash"])
    ).fetchone()
    if cached:
        return True
    text_row = conn.execute(
        "SELECT whitelist_body FROM retrieval_documents"
        " WHERE memory_id=?", (memory_id,)).fetchone()
    # 2026-09-30 裁定（S07）：语料=事件正文；无正文即标 gap——删除旧
    # 向量、不建新向量，禁止回退到混有禁检字段的旧整桶投影
    if text_row is None or not text_row["whitelist_body"]:
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?",
                     (memory_id,))
        return False
    vec = embed([text_row["whitelist_body"]])[0]
    from datetime import datetime, timezone
    conn.execute(
        "INSERT OR REPLACE INTO memory_embeddings(memory_id, model, dim,"
        " projection_hash, vector, created_at) VALUES(?,?,?,?,?,?)",
        (memory_id, MODEL_KEY, MODEL_DIM, row["search_text_hash"],
         vec.tobytes(), datetime.now(timezone.utc).isoformat()))
    return True


def semantic_search(conn: sqlite3.Connection, query: str, limit: int = 20,
                    extra_where: list[str] | None = None,
                    extra_params: list | None = None) -> list[dict]:
    """在有效投影集合上做精确余弦 top-k；返回带 matched_by 的命中。

    v1.4 §5.2：extra_where/extra_params 让调用方把权限、日期、分类与
    session 排除等过滤**前置**到候选池与评分阶段——不允许先全库 Top-K
    再过滤（范围内正确候选可能已被别的池挤掉）。不传时行为与旧接口
    一致（兼容 memory.search）。
    """
    if config.SEMANTIC_PROVIDER != "local_bge_zh":
        return []
    ensure_schema(conn)
    scope = list(extra_where or [])
    sp = list(extra_params or [])
    pool_sql = ("SELECT rd.memory_id, rd.search_text, rd.search_text_hash,"
                " rd.projection_kind, m.compression_state FROM retrieval_documents rd"
                " JOIN memories m ON m.memory_id = rd.memory_id"
                " WHERE m.visibility='active'")
    if scope:
        pool_sql += " AND " + " AND ".join(scope)
    rows = conn.execute(pool_sql, sp).fetchall()
    if not rows:
        return []
    # 补齐缺失/失效向量（迟到向量原则：hash 校验后才可安装）。
    # 查询路径限流：每次最多补 REINDEX_BUDGET 个，其余下次查询继续
    # （首次冷启动全量预热走 maintenance.semantic.warmup）。
    budget = REINDEX_BUDGET
    pending = 0
    for r in rows:
        valid = conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_id=? AND model=?"
            " AND projection_hash=?",
            (r["memory_id"], MODEL_KEY, r["search_text_hash"])).fetchone()
        if not valid:
            if budget > 0:
                reindex(conn, r["memory_id"])
                budget -= 1
            else:
                pending += 1
    from . import projection as _pj
    np = _np()
    qvec = embed([QUERY_PREFIX + _pj.normalize_search_text(query)])[0]
    scored = []
    score_sql = ("SELECT e.memory_id, e.vector, e.projection_hash,"
                 " rd.search_text_hash, rd.projection_kind,"
                 " m.compression_state, rd.whitelist_body,"
                 " m.current_version_no FROM memory_embeddings e"
                 " JOIN retrieval_documents rd ON rd.memory_id = e.memory_id"
                 " JOIN memories m ON m.memory_id = e.memory_id"
                 " WHERE e.model=? AND m.visibility='active'"
                 " AND e.projection_hash = rd.search_text_hash")  # 仅有效投影向量
    if scope:
        score_sql += " AND " + " AND ".join(scope)
    for r in conn.execute(score_sql, [MODEL_KEY] + sp):
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
                "whitelist_body": r["whitelist_body"],
                "content_version": str(r["current_version_no"]),
            })
    scored.sort(key=lambda x: -x["score"])
    # 相对间距窗：只保留与最优结果显著同层（>= best - RELATIVE_WINDOW）的命中，
    # 防同质语料下泛化误召回挤满候选（与绝对阈值双重过滤）。
    if scored:
        best = scored[0]["score"]
        scored = [x for x in scored if x["score"] >= best - RELATIVE_WINDOW]
    out = scored[:limit]
    if pending:
        # S07/WP03：未就绪向量计入 pending 并显式外露——不得同时
        # 报告 dense 全覆盖（调用方写入 coverage）
        return out + [{"__pending_vectors__": pending}]
    return out


def warmup(conn: sqlite3.Connection) -> dict:
    """全量预热有效投影向量（冷启动/重建后调用一次）。"""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT rd.memory_id, rd.search_text_hash FROM retrieval_documents rd"
        " JOIN memories m ON m.memory_id = rd.memory_id"
        " WHERE m.visibility='active'").fetchall()
    done, skipped = 0, 0
    for r in rows:
        valid = conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_id=? AND model=?"
            " AND projection_hash=?",
            (r["memory_id"], MODEL_KEY, r["search_text_hash"])).fetchone()
        if valid:
            skipped += 1
        else:
            reindex(conn, r["memory_id"])
            done += 1
    return {"warmed": done, "already_ok": skipped, "total": len(rows)}
