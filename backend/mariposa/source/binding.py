"""Semantic Source Binding（复核 v1.1 §4.3 强化）。

- 区间/偏移语义校验全部委托 query.validate_range（同 parent 路径、
  半开区间 code point 口径、整数校验）——绑定与读取同一套契约。
- 绑定固定证据版本：记录 start/end content_hash，读取时校验防漂移。
- Memory 引用原文不复制原文；一条 Memory 多 range（多行）不变。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

from .. import audit, config, db
from ..errors import Forbidden, NotFound
from . import query as source_query


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bind(principal_id: str, memory_id: str, conversation_id: str,
         start_message_id: str, end_message_id: str,
         start_char_offset: int | None = None,
         end_char_offset: int | None = None,
         confidence: str = "exact") -> dict:
    """把 memory 绑定到一个连续消息区间（可重复调用叠加多个 range）。"""
    if confidence not in ("exact", "high", "low"):
        raise Forbidden("confidence must be exact/high/low")
    with db.formal() as conn:
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                            (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)

    # 同一套区间契约（路径/偏移/发布可见性全部由 query 层校验）
    resolved = source_query.validate_range(
        conversation_id, start_message_id, end_message_id,
        start_char_offset, end_char_offset)

    binding_id = f"msb_{_uuid.uuid4().hex[:12]}"
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO memory_source_bindings(binding_id, memory_id,"
                " conversation_id, start_message_id, end_message_id,"
                " start_char_offset, end_char_offset, bind_confidence,"
                " start_content_hash, end_content_hash, created_by,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (binding_id, memory_id, resolved["conversation"]["id"],
                 resolved["start"]["id"], resolved["end"]["id"],
                 start_char_offset, end_char_offset, confidence,
                 resolved["start_content_hash"],
                 resolved["end_content_hash"], principal_id, _now()))
            audit.record(conn, "source.bound", principal_id,
                         resource_id=memory_id,
                         payload={"binding_id": binding_id,
                                  "conversation_id":
                                      resolved["conversation"]["id"],
                                  "start": resolved["start"]["id"],
                                  "end": resolved["end"]["id"],
                                  "confidence": confidence,
                                  "offset_convention":
                                      source_query.OFFSET_CONVENTION})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"binding_id": binding_id, "memory_id": memory_id,
            "conversation_id": resolved["conversation"]["id"],
            "start_message_id": resolved["start"]["id"],
            "end_message_id": resolved["end"]["id"],
            "start_content_hash": resolved["start_content_hash"],
            "end_content_hash": resolved["end_content_hash"],
            "offset_convention": source_query.OFFSET_CONVENTION,
            "confidence": confidence}


def ranges_of(memory_id: str) -> list[dict]:
    """memory 的全部有效绑定（revoked 留库不返回）。"""
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_source_bindings WHERE memory_id=? AND"
            " bind_confidence<>'revoked' ORDER BY created_at",
            (memory_id,)).fetchall()
    return [dict(r) for r in rows]


def open_for_memory(memory_id: str, include_content: bool = False) -> dict:
    """按绑定动态读取原文区间（裁切片段；版本漂移显式标注不静默）。"""
    ranges = ranges_of(memory_id)
    if not ranges:
        return {"memory_id": memory_id, "ranges": [], "source": "source_layer"}
    opened = []
    for rng in ranges:
        try:
            payload = source_query.open_range(
                rng["conversation_id"], rng["start_message_id"],
                rng["end_message_id"],
                start_char_offset=rng["start_char_offset"],
                end_char_offset=rng["end_char_offset"],
                include_content=include_content)
        except NotFound:
            opened.append({"binding_id": rng["binding_id"],
                           "status": "unresolvable",
                           "confidence": rng["bind_confidence"],
                           "bound_at": rng["created_at"]})
            continue
        # 版本漂移检测（当前行内容与绑定时 hash 不同 → 显式标注）
        drift = None
        for label, bound_hash, key in (
                ("start", rng["start_content_hash"],
                 "start_provider_message_id"),
                ("end", rng["end_content_hash"],
                 "end_provider_message_id")):
            current = _current_hash(rng["conversation_id"], payload[key])
            if bound_hash and current and current != bound_hash:
                drift = drift or {}
                drift[label] = {"bound": bound_hash, "current": current}
        opened.append({"binding_id": rng["binding_id"],
                       "confidence": rng["bind_confidence"],
                       "bound_at": rng["created_at"],
                       "version_drift": drift,
                       **payload})
    return {"memory_id": memory_id, "ranges": opened,
            "source": "source_layer"}


def _current_hash(conversation_row_id: str, provider_message_id: str):
    with db.formal() as conn:
        row = conn.execute(
            "SELECT content_hash FROM source_messages WHERE conversation_id=?"
            " AND provider_message_id=?",
            (conversation_row_id, provider_message_id)).fetchone()
    return row["content_hash"] if row else None


def revoke(principal_id: str, binding_id: str) -> dict:
    """撤销绑定：行保留（bind_confidence=revoked），留历史。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE memory_source_bindings SET bind_confidence='revoked'"
                " WHERE binding_id=? AND bind_confidence<>'revoked'",
                (binding_id,))
            if cur.rowcount == 0:
                raise NotFound("active source binding not found",
                               binding_id=binding_id)
            audit.record(conn, "source.binding.revoked", principal_id,
                         resource_id=binding_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"binding_id": binding_id, "status": "revoked"}


def reindex_search_docs(batch: int = 500) -> dict:
    """source_search_docs / source_fts 由 source_messages 游标分批重建。"""
    import hashlib
    from ..retrieval import projection
    n = 0
    last_id = ""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM source_fts")
            conn.execute("DELETE FROM source_search_docs")
            while True:
                rows = conn.execute(
                    "SELECT id, provider_message_id, text FROM"
                    " source_messages WHERE id>? AND text<>'' AND"
                    " normalized_sender IN ('human','assistant')"
                    " ORDER BY id LIMIT ?", (last_id, batch)).fetchall()
                if not rows:
                    break
                for r in rows:
                    text_norm = projection.normalize_search_text(r["text"])
                    conn.execute(
                        "INSERT INTO source_search_docs(message_id,"
                        " provider_message_id, text_norm, text_hash,"
                        " projection_version, built_at) VALUES(?,?,?,?,?,?)",
                        (r["id"], r["provider_message_id"], text_norm,
                         hashlib.sha256(r["text"].encode()).hexdigest(),
                         config.SOURCE_PROJECTION_VERSION, _now()))
                    conn.execute(
                        "INSERT INTO source_fts(message_id, text_norm)"
                        " VALUES(?,?)", (r["id"], text_norm))
                    n += 1
                last_id = rows[-1]["id"]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"rebuilt_docs": n, "batch_size": batch}
