"""朋友圈最小版（§16.3）：自有实体；kind=post 与 group_archive 分开。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound

_COMMENTS: str = "moments_comments"  # v7 表


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def post(principal_id: str, content: str, media_hash: str | None = None) -> dict:
    if not content or not str(content).strip():
        raise Forbidden("moment content required")
    mid = f"mo_{uuid.uuid4().hex[:10]}"
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO moments(id, current_version_no, author, kind,"
                " visibility, created_at, updated_at) VALUES(?,1,?,'post','normal',?,?)",
                (mid, principal_id, now, now))
            conn.execute(
                "INSERT INTO moment_versions(moment_id, version_no, content,"
                " media_hash, edited_by, created_at) VALUES(?,1,?,?,?,?)",
                (mid, str(content), media_hash, principal_id, now))
            audit.record(conn, "moment.posted", principal_id, resource_id=mid,
                         resource_version=1,
                         payload={"kind": "post", "has_media": bool(media_hash)})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"moment_id": mid, "kind": "post", "version": 1}


def list_moments(kind: str = "post", limit: int = 50) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT m.id, m.author, m.kind, m.created_at, v.content, v.media_hash"
            " FROM moments m JOIN moment_versions v"
            " ON v.moment_id = m.id AND v.version_no = m.current_version_no"
            " WHERE m.kind=? ORDER BY m.created_at DESC LIMIT ?",
            (kind, limit)).fetchall()
    return [dict(r) for r in rows]


def comment(principal_id: str, moment_id: str, content: str) -> dict:
    if not content or not str(content).strip():
        raise Forbidden("comment content required")
    cid = f"mc_{uuid.uuid4().hex[:10]}"
    with db.formal() as conn:
        if not conn.execute("SELECT 1 FROM moments WHERE id=?",
                            (moment_id,)).fetchone():
            raise NotFound("moment not found", moment_id=moment_id)
        conn.execute(
            "INSERT INTO moment_comments(id, moment_id, author, content, created_at)"
            " VALUES(?,?,?,?,?)", (cid, moment_id, principal_id, str(content), _now()))
    return {"comment_id": cid}


def react(principal_id: str, moment_id: str, reaction: str) -> dict:
    if not reaction or len(reaction) > 16:
        raise Forbidden("reaction 1..16 chars")
    with db.formal() as conn:
        if not conn.execute("SELECT 1 FROM moments WHERE id=?",
                            (moment_id,)).fetchone():
            raise NotFound("moment not found", moment_id=moment_id)
        conn.execute(
            "INSERT OR REPLACE INTO moment_reactions(moment_id, principal,"
            " reaction, created_at) VALUES(?,?,?,?)",
            (moment_id, principal_id, reaction, _now()))
    return {"moment_id": moment_id, "reaction": reaction}
