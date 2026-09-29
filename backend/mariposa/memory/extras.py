"""记忆扩展能力：pin/protect/anchor/update/versions.list（§17.3）。"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound, VersionConflict
from . import service as memory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_FLAGS = {"pin": "pinned", "protect": "protected", "anchor": "anchor"}


def set_flag(principal_id: str, memory_id: str, flag: str, value: bool) -> dict:
    col = _FLAGS.get(flag)
    if col is None:
        raise Forbidden(f"unknown flag: {flag}")
    with db.formal() as conn:
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                            (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"UPDATE memories SET {col}=?, updated_at=? WHERE memory_id=?",
                         (int(value), _now(), memory_id))
            audit.record(conn, f"memory.{flag}{'_set' if value else '_unset'}",
                         principal_id, resource_id=memory_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, flag: value}


def update_text(principal_id: str, memory_id: str, expected_version: int,
                text: str | None = None, why_remember: str | None = None,
                memory_date: str | None = None,
                date_confidence: str | None = None) -> dict:
    """修改桶正文：新版本，不就地覆盖（§4.5）。

    审计 F39：expected_version 校验在 BEGIN IMMEDIATE 写锁内执行——
    两个并发编辑同旧 revision，先提交者成功，后者得到结构化
    VERSION_CONFLICT，而不是裸 IntegrityError。
    审计 F04：正文/投影构造统一走 version_body / rebuild_full_projection，
    v2 记忆只改元数据不再让事件正文从检索消失。
    """
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            m = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                             (memory_id,)).fetchone()
            if m is None:
                raise NotFound("memory not found", memory_id=memory_id)
            if m["current_version_no"] != expected_version:
                raise VersionConflict("memory version moved",
                                      expected=expected_version,
                                      current=m["current_version_no"])
            v = conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? AND version_no=?",
                (memory_id, expected_version)).fetchone()
            # hold_text 列延续：v2 版本行（正文在 event_text）保持 NULL；
            # v1 旧形态行继续以 hold_text 承载正文。跨版本形态不漂移。
            if text is not None:
                new_text = None if v["event_text"] is not None else text
            else:
                new_text = None if v["event_text"] is not None else v["hold_text"]
            new_why = (why_remember if why_remember is not None
                       else v["why_remember"])
            if m["compression_state"] == "forgotten_summary":
                raise Forbidden(
                    "forgotten memory cannot be edited in place; restore first")
            new_version = expected_version + 1
            now = _now()
            # A01 + F04：未提供新正文时，经统一入口延续当前 revision 正文
            #（event_text 优先，v1 旧数据回退 hold_text）
            new_event = text if text is not None else memory.version_body(v)
            old_title = v["original_title"]
            old_schema = v["schema_version"] or 1
            payload = {"representation": "full", "hold_text": new_text,
                       "why_remember": new_why, "origin": "update"}
            conn.execute(
                "INSERT INTO memory_versions(memory_id, version_no, representation,"
                " hold_text, compressed_summary, why_remember, authored_by, confirmed_by,"
                " origin_kind, payload_hash, created_at, original_title,"
                " event_text, schema_version)"
                " VALUES(?,?,'full',?,NULL,?,?,NULL,'initial_hold',?,?,?,?,?)",
                (memory_id, new_version, new_text, new_why, principal_id,
                 memory.canonical_hash(payload), now, old_title, new_event,
                 old_schema))
            conn.execute(
                "UPDATE memories SET current_version_no=?, memory_date=?,"
                " date_confidence=?, updated_at=? WHERE memory_id=?",
                (new_version,
                 memory_date if memory_date is not None else m["memory_date"],
                 date_confidence or m["date_confidence"], now, memory_id))
            from ..retrieval import field_projection as _fp
            _fp.build_for_memory(conn, memory_id)
            memory.rebuild_full_projection(conn, memory_id)
            audit.record(conn, "memory.updated", principal_id,
                         resource_id=memory_id, resource_version=new_version)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "version": new_version}


def versions_list(memory_id: str) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT version_no, representation, origin_kind, authored_by,"
            " confirmed_by, payload_hash, created_at FROM memory_versions"
            " WHERE memory_id=? ORDER BY version_no", (memory_id,)).fetchall()
    if not rows:
        raise NotFound("memory not found", memory_id=memory_id)
    return [dict(r) for r in rows]
