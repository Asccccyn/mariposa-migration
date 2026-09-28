"""作者「留」（v1.7 §5.5）：回忆写入时显式选择，谁留谁撤。

- 唯一入口：recollection.append(keep_wide=True) 在同一正式库事务内
  登记标记；keep 绑定本次新写的回忆 ID+版本，理由就是回忆本身
  （不另设 reason 字段，不进任何索引/Jev/标签推导）。
- 两位作者独立标记 OR 生效；撤自己的不影响对方的；全部撤销后按真实
  basis 重新计算（撤销不重置年龄、不写死 CORE）。
- 后台/工具模型无权留/撤（调用方必须核验 principal ∈ 两位作者）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound

_OWNERS = ("qiaosheng", "jiaming")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_in_txn(conn, owner: str, memory_id: str,
                    recollection_id: str, recollection_version: int,
                    now: str) -> dict:
    """在调用方事务内登记 keep（不自己开事务；幂等去重由重复操作幂等层管）。"""
    if owner not in _OWNERS:
        raise Forbidden("only the two owners may keep", principal=owner)
    if not recollection_id or recollection_version < 1:
        raise Forbidden("keep must bind a concrete recollection version",
                        code="KEEP_REQUIRES_OWN_NEW_RECOLLECTION")
    mark_id = f"mk_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO memory_keeps(mark_id, memory_id, owner,"
        " recollection_id, recollection_version, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (mark_id, memory_id, owner, recollection_id,
         int(recollection_version), now))
    audit.record(conn, "memory.keep.registered", owner,
                 resource_id=memory_id,
                 payload={"mark_id": mark_id,
                          "recollection_id": recollection_id,
                          "recollection_version": recollection_version})
    return {"mark_id": mark_id, "memory_id": memory_id, "owner": owner,
            "recollection_id": recollection_id,
            "recollection_version": int(recollection_version)}


def revoke(principal, mark_id: str) -> dict:
    """撤销本人 keep（幂等：重复撤销不刷新时间戳）。"""
    actor = principal.principal_id
    if actor not in _OWNERS:
        raise Forbidden("only the two owners may revoke a keep",
                        principal=actor)
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM memory_keeps WHERE mark_id=?",
                (mark_id,)).fetchone()
            if row is None:
                raise NotFound("keep mark not found", mark_id=mark_id)
            if row["owner"] != actor:
                raise Forbidden("only the creator can revoke their own keep",
                                mark_id=mark_id, owner=row["owner"])
            if row["revoked_at"] is not None:
                conn.execute("COMMIT")
                return {"mark_id": mark_id, "status": "revoked",
                        "revoked_at": row["revoked_at"],
                        "idempotent_replay": True}
            now = _now()
            conn.execute(
                "UPDATE memory_keeps SET revoked_at=? WHERE mark_id=?",
                (now, mark_id))
            audit.record(conn, "memory.keep.revoked", actor,
                         resource_id=row["memory_id"],
                         payload={"mark_id": mark_id})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"mark_id": mark_id, "status": "revoked", "revoked_at": now}


def active_keepers(memory_id: str) -> list[str]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT owner FROM memory_keeps WHERE memory_id=?"
            " AND revoked_at IS NULL", (memory_id,)).fetchall()
    return [r["owner"] for r in rows]


def marks_of(memory_id: str) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_keeps WHERE memory_id=? ORDER BY created_at",
            (memory_id,)).fetchall()
    return [dict(r) for r in rows]
