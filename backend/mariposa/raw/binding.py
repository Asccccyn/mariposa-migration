"""原文后补绑定（§9.2）与去重审阅（§9.3）。

- 周家明可 raw_pending 先写 Hold，附现场片段入 provisional_sources
- 导入原文后按（时间/会话范围）绑定：高置信只建结构引用，不改 Hold 内容
- 同一 source_hash + 相同消息范围已绑其他桶 -> DEDUPE_NEEDS_REVIEW，不自动认领
- 绑定可撤销留历史
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound
from ..memory import service as memory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def report_fragment(principal_id: str, fragment: str,
                    memory_id: str | None = None) -> dict:
    """周家明现场复述片段：不是后台候选，也不是已验证原文。"""
    if principal_id != "jiaming":
        raise Forbidden("only jiaming reports provisional fragments",
                        principal=principal_id)
    if not fragment or not str(fragment).strip():
        raise Forbidden("fragment required")
    fid = f"prov_{uuid.uuid4().hex[:10]}"
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO provisional_sources(id, memory_id, reported_by, reported_at,"
            " fragment, status) VALUES(?,?,?,?,?,'unbound')",
            (fid, memory_id, principal_id, _now(), str(fragment)))
    return {"provisional_id": fid, "bound_to": memory_id}


def bind(principal_id: str, memory_id: str, conversation_id: str,
         message_from: str, message_to: str,
         confidence: str = "high") -> dict:
    """把原文范围绑定到桶；不改 Hold 内容；绑定到不同主体的来源不能自动认领。"""
    if confidence not in ("exact", "high", "low"):
        raise Forbidden("confidence must be exact/high/low")
    source_hash = hashlib.sha256(
        f"{conversation_id}:{message_from}:{message_to}".encode()).hexdigest()
    with db.formal() as conn:
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                            (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        msgs = conn.execute(
            "SELECT COUNT(*) AS c FROM raw_messages WHERE conversation_id=?"
            " AND source_message_id>=? AND source_message_id<=?",
            (conversation_id, message_from, message_to)).fetchone()["c"]
        if msgs == 0:
            raise NotFound("no raw messages in range",
                           conversation_id=conversation_id)
        # 去重：同一范围已绑到别的桶
        dup = conn.execute(
            "SELECT memory_id FROM memory_raw_refs WHERE conversation_id=?"
            " AND message_from=? AND message_to=? AND memory_id<>?"
            " AND bind_confidence<>'revoked'",
            (conversation_id, message_from, message_to, memory_id)).fetchone()
        if dup:
            raise Forbidden(
                "same source range already bound to another memory",
                code="DEDUPE_NEEDS_REVIEW", other_memory=dup["memory_id"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO memory_raw_refs(memory_id, conversation_id,"
                " message_from, message_to, source_hash, bind_confidence, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (memory_id, conversation_id, message_from, message_to,
                 source_hash, confidence, _now()))
            conn.execute(
                "UPDATE memories SET source_state='bound', updated_at=?"
                " WHERE memory_id=?", (_now(), memory_id))
            conn.execute(
                "UPDATE provisional_sources SET status='bound' WHERE memory_id=?",
                (memory_id,))
            audit.record(conn, "raw.bound", principal_id, resource_id=memory_id,
                         payload={"conversation_id": conversation_id,
                                  "range": [message_from, message_to],
                                  "confidence": confidence})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "source_state": "bound",
            "source_hash": source_hash, "messages": msgs}


def revoke(principal_id: str, memory_id: str, conversation_id: str) -> dict:
    """绑定可撤销留历史：行保留，confidence=revoked，桶回 raw_pending。"""
    with db.formal() as conn:
        cur = conn.execute(
            "UPDATE memory_raw_refs SET bind_confidence='revoked'"
            " WHERE memory_id=? AND conversation_id=? AND bind_confidence<>'revoked'",
            (memory_id, conversation_id))
        if cur.rowcount == 0:
            raise NotFound("active binding not found")
        conn.execute("UPDATE memories SET source_state='raw_pending', updated_at=?"
                     " WHERE memory_id=?", (_now(), memory_id))
    return {"memory_id": memory_id, "source_state": "raw_pending"}


def refs_of(memory_id: str) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT conversation_id, message_from, message_to, source_hash,"
            " bind_confidence, created_at FROM memory_raw_refs WHERE memory_id=?",
            (memory_id,)).fetchall()
    return [dict(r) for r in rows]
