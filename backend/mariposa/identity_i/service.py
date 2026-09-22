"""I（spec_v2 R19 / §11）：周家明的正本，乔生只能提建议。

- I 仅周家明（jiaming）写；无情绪准入门槛（"平静时写"是自律，不是审核）；
- 保留版本；乔生的建议是待提议材料，绝不直接改正本；
- 旧 Self/Home 不自动改名成 I（V2-I-03），映射须显式确认。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound, VersionConflict
from ..memory.service import canonical_hash

DOC_ID = "i_main"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get() -> dict:
    with db.formal() as conn:
        doc = conn.execute("SELECT * FROM i_documents WHERE doc_id=?",
                           (DOC_ID,)).fetchone()
        if doc is None:
            return {"doc_id": DOC_ID, "content": None, "version": 0,
                    "note": "I 尚未落笔；由周家明写入"}
        v = conn.execute(
            "SELECT * FROM i_versions WHERE doc_id=? AND version_no=?",
            (DOC_ID, doc["current_version_no"])).fetchone()
        return {"doc_id": DOC_ID, "content": v["content"],
                "version": doc["current_version_no"],
                "authored_by": v["authored_by"], "updated_at": v["created_at"]}


def write(principal_id: str, content: str,
          expected_version: int | None = None) -> dict:
    """写 I 正本：仅周家明；乐观锁防并发覆盖。"""
    if principal_id != "jiaming":
        raise Forbidden("only jiaming writes the I document",
                        principal=principal_id)
    if not content or not str(content).strip():
        raise Forbidden("I content required", code="INVALID_ARGUMENT")
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            doc = conn.execute("SELECT * FROM i_documents WHERE doc_id=?",
                               (DOC_ID,)).fetchone()
            if doc is None:
                if expected_version not in (None, 0):
                    raise VersionConflict(
                        "I document does not exist yet",
                        expected=expected_version, current=0)
                conn.execute(
                    "INSERT INTO i_documents(doc_id, current_version_no,"
                    " updated_at) VALUES(?,1,?)", (DOC_ID, now))
                new_version = 1
            else:
                if expected_version is not None and \
                        doc["current_version_no"] != expected_version:
                    raise VersionConflict(
                        "I version moved",
                        expected=expected_version,
                        current=doc["current_version_no"])
                new_version = doc["current_version_no"] + 1
                conn.execute(
                    "UPDATE i_documents SET current_version_no=?, updated_at=?"
                    " WHERE doc_id=?", (new_version, now, DOC_ID))
            payload = {"content": content, "authored_by": principal_id}
            conn.execute(
                "INSERT INTO i_versions(doc_id, version_no, content,"
                " authored_by, payload_hash, created_at) VALUES(?,?,?,?,?,?)",
                (DOC_ID, new_version, str(content), principal_id,
                 canonical_hash(payload), now))
            audit.record(conn, "i.written", principal_id, resource_id=DOC_ID,
                         resource_version=new_version,
                         payload={"bytes": len(content)})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"doc_id": DOC_ID, "version": new_version}


def versions_read() -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT version_no, content, authored_by, payload_hash, created_at"
            " FROM i_versions WHERE doc_id=? ORDER BY version_no",
            (DOC_ID,)).fetchall()
    if not rows:
        raise NotFound("I document has no versions")
    return [dict(r) for r in rows]


def suggest(principal_id: str, content: str) -> dict:
    """乔生提建议：待提议材料，不改正本（V2-I-01）。"""
    if principal_id != "qiaosheng":
        raise Forbidden("only qiaosheng files I suggestions",
                        principal=principal_id)
    if not content or not str(content).strip():
        raise Forbidden("suggestion content required",
                        code="INVALID_ARGUMENT")
    sid = f"isg_{uuid.uuid4().hex[:10]}"
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO i_suggestions(suggestion_id, suggested_by, content,"
             " created_at) VALUES(?,?,?,?)",
            (sid, principal_id, str(content), _now()))
        audit.record(conn, "i.suggestion.filed", principal_id,
                     resource_id=sid, payload={"bytes": len(content)})
    return {"suggestion_id": sid, "status": "open",
            "note": "建议已登记；是否落笔由周家明决定"}


def suggestions_list(status: str | None = None) -> list[dict]:
    with db.formal() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM i_suggestions WHERE status=? ORDER BY created_at",
                (status,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM i_suggestions ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]
