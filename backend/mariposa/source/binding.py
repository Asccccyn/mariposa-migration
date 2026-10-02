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
         confidence: str = "exact", conn=None) -> dict:
    """把 memory 绑定到一个连续消息区间（可重复调用叠加多个 range）。

    conn 由纠错改绑传入：插入与纠错同事务（§5.4 原子操作）。
    """
    if confidence not in ("exact", "high", "low"):
        raise Forbidden("confidence must be exact/high/low")

    def _insert(conn):
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                            (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        # 同一套区间契约（路径/偏移/发布可见性全部由 query 层校验）
        resolved = source_query.validate_range(
            conversation_id, start_message_id, end_message_id,
            start_char_offset, end_char_offset)
        binding_id = f"msb_{_uuid.uuid4().hex[:12]}"
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
        return {"binding_id": binding_id, "memory_id": memory_id,
                "conversation_id": resolved["conversation"]["id"],
                "start_message_id": resolved["start"]["id"],
                "end_message_id": resolved["end"]["id"],
                "start_content_hash": resolved["start_content_hash"],
                "end_content_hash": resolved["end_content_hash"],
                "offset_convention": source_query.OFFSET_CONVENTION,
                "confidence": confidence}

    if conn is not None:
        return _insert(conn)
    with db.formal() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            out = _insert(c)
            c.execute("COMMIT")
            return out
        except Exception:
            c.execute("ROLLBACK")
            raise


def ranges_of(memory_id: str) -> list[dict]:
    """memory 的全部有效绑定（全部行均有效——纠错历史在别处，§5.2）。"""
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_source_bindings WHERE memory_id=?"
            " ORDER BY created_at",
            (memory_id,)).fetchall()
    return [dict(r) for r in rows]


def memories_referencing(message_id: str) -> list[dict]:
    """§6.1 Source 反查：这段原文消息被哪些绑定区间实际覆盖。

    精确判定按消息序范围（start..end 区间含该消息），不把"同
    conversation"当"同一段"；返回绑定身份与范围供两端继续追查。
    """
    with db.formal() as conn:
        msg = conn.execute(
            "SELECT conversation_id, sequence, provider FROM"
            " source_messages WHERE id=? OR provider_message_id=?",
            (message_id, message_id)).fetchone()
        if msg is None:
            return []
        seqs = conn.execute(
            "SELECT sequence FROM source_messages WHERE conversation_id=?"
            " AND (id=? OR provider_message_id=?)",
            (msg["conversation_id"], message_id, message_id)).fetchall()
        seq_set = {r["sequence"] for r in seqs}
        rows = conn.execute(
            "SELECT b.*, s.sequence AS start_seq, e.sequence AS end_seq"
            " FROM memory_source_bindings b"
            " JOIN source_messages s ON s.id=b.start_message_id"
            " JOIN source_messages e ON e.id=b.end_message_id"
            " WHERE b.conversation_id=?", (msg["conversation_id"],)).fetchall()
    out = []
    for r in rows:
        # 同 sequence 组按稳定消息身份精确分辨（§6.1）
        if any(r["start_seq"] <= q <= r["end_seq"] for q in seq_set):
            d = dict(r)
            d.pop("start_seq", None)
            d.pop("end_seq", None)
            out.append(d)
    return out


def correct(principal_id: str, binding_id: str,
            correction_action: str, note: str | None = None,
            replacement: dict | None = None, conn=None) -> dict:
    """§P-R02 纠错：撤销/改绑错误区间绑定（替代旧 revoke）。

    同一事务：登记纠错历史→删该条有效绑定→（改绑）经完整区间校验
    创建新绑定。不删除/改写原文母本或消息（§5.5 Source）。
    """
    from ..relations.corrections import record_correction
    if correction_action not in ("remove_wrong_binding",
                                 "replace_wrong_binding"):
        raise Forbidden("correction_action must be remove_wrong_binding/"
                        "replace_wrong_binding")
    def _do(conn):
        row = conn.execute(
            "SELECT * FROM memory_source_bindings WHERE binding_id=?",
            (binding_id,)).fetchone()
        if row is None:
            raise NotFound("source binding not found",
                           binding_id=binding_id)
        replacement_id = None
        if replacement:
            rep = bind(principal_id,
                       replacement.get("memory_id", row["memory_id"]),
                       replacement.get("conversation_id", ""),
                       replacement.get("start_message_id", ""),
                       replacement.get("end_message_id", ""),
                       start_char_offset=replacement.get(
                           "start_char_offset"),
                       end_char_offset=replacement.get(
                           "end_char_offset"),
                       confidence=replacement.get("confidence", "exact"),
                       conn=conn)
            replacement_id = rep.get("binding_id", binding_id)
        cid = record_correction(
            conn, domain="source_binding",
            original_instance_id=binding_id,
            endpoint_a=row["memory_id"],
            endpoint_b=row["conversation_id"],
            original_meta={
                "start_message_id": row["start_message_id"],
                "end_message_id": row["end_message_id"],
                "start_char_offset": row["start_char_offset"],
                "end_char_offset": row["end_char_offset"],
                "bind_confidence": row["bind_confidence"]},
            original_created_by=row["created_by"],
            original_created_at=row["created_at"],
            corrected_by=principal_id, note=note,
            replacement_instance_id=replacement_id)
        conn.execute(
            "DELETE FROM memory_source_bindings WHERE binding_id=?",
            (binding_id,))
        audit.record(conn, "source.binding.corrected", principal_id,
                     resource_id=binding_id,
                     payload={"correction_id": cid})
        return {"correction_id": cid, "removed_binding_id": binding_id,
                "replacement_binding_id": replacement_id,
                "action": correction_action}

    if conn is not None:
        return _do(conn)
    with db.formal() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            out = _do(c)
            c.execute("COMMIT")
            return out
        except Exception:
            c.execute("ROLLBACK")
            raise


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
