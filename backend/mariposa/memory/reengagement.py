"""再次提起（§7.3）：与访问日志严格分开；证据消息原时刻回填。"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound
from . import service as memory

KINDS = {"chat_message", "explicit_recall", "plan_reference"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(principal_id: str, memory_id: str, evidence_kind: str,
           occurred_at: str, evidence_ref: str | None = None) -> dict:
    """记录一次真实"再次提起"：occurred_at 取证据原时刻（不按导入/记录时间）。"""
    if evidence_kind not in KINDS:
        raise Forbidden(f"evidence_kind must be one of {sorted(KINDS)}")
    try:
        datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    except ValueError:
        raise Forbidden("occurred_at must be ISO-8601")
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # P1-4：存在性检查在写锁内
            if not conn.execute(
                    "SELECT 1 FROM memories WHERE memory_id=?",
                    (memory_id,)).fetchone():
                raise NotFound("memory not found", memory_id=memory_id)
            conn.execute(
                "INSERT OR IGNORE INTO memory_reengagements(memory_id,"
                " evidence_kind, evidence_ref, occurred_at, recorded_at,"
                " recorded_by) VALUES(?,?,?,?,?,?)",
                (memory_id, evidence_kind, evidence_ref, occurred_at,
                 _now(), principal_id))
            audit.record(conn, "memory.reengaged", principal_id,
                         resource_id=memory_id,
                         payload={"evidence_kind": evidence_kind,
                                  "occurred_at": occurred_at})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": memory_id, "occurred_at": occurred_at}

