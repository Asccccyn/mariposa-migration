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
    """修改桶正文：新版本，不就地覆盖（§4.5）。"""
    with db.formal() as conn:
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
        new_text = text if text is not None else v["hold_text"]
        new_why = why_remember if why_remember is not None else v["why_remember"]
        if m["compression_state"] == "forgotten_summary":
            raise Forbidden("forgotten memory cannot be edited in place; restore first")
        new_version = expected_version + 1
        now = _now()
        payload = {"representation": "full", "hold_text": new_text,
                   "why_remember": new_why, "origin": "update"}
        from ..retrieval import projection
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO memory_versions(memory_id, version_no, representation,"
                " hold_text, compressed_summary, why_remember, authored_by, confirmed_by,"
                " origin_kind, payload_hash, created_at)"
                " VALUES(?,?,'full',?,NULL,?,?,NULL,'initial_hold',?,?)",
                (memory_id, new_version, new_text, new_why, principal_id,
                 memory.canonical_hash(payload), now))
            conn.execute(
                "UPDATE memories SET current_version_no=?, memory_date=?,"
                " date_confidence=?, updated_at=? WHERE memory_id=?",
                (new_version,
                 memory_date if memory_date is not None else m["memory_date"],
                 date_confidence or m["date_confidence"], now, memory_id))
            projection.upsert(conn, memory_id, new_version, "full",
                              projection.build_full(new_text or "", new_why))
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
