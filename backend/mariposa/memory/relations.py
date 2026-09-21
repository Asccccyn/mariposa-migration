"""关联（§5.1/§8.1）：单向存储、显式反向查询，不复制镜像关系。"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from ..errors import Forbidden, NotFound

_RELATION_TYPES = {"continuation_of", "related_to", "contradicts", "custom"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exists(conn, memory_id: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                             (memory_id,)).fetchone())


def link(principal_id: str, from_memory: str, to_memory: str,
         relation_type: str, custom_label: str | None = None,
         reverse_label: str | None = None) -> dict:
    if relation_type not in _RELATION_TYPES:
        raise Forbidden(f"relation_type must be one of {sorted(_RELATION_TYPES)}")
    if relation_type != "custom" and (custom_label or reverse_label):
        raise Forbidden("custom_label only for relation_type=custom")
    with db.formal() as conn:
        for mid in (from_memory, to_memory):
            if not _exists(conn, mid):
                raise NotFound("memory not found", memory_id=mid)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO memory_relations(from_memory, to_memory,"
                " relation_type, custom_label, reverse_label, confidence, active,"
                " version, created_by, created_at) VALUES(?,?,?,?,?,'human',1,1,?,?)",
                (from_memory, to_memory, relation_type, custom_label,
                 reverse_label, principal_id, _now()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"from": from_memory, "to": to_memory, "relation_type": relation_type}


def detach(principal_id: str, from_memory: str, to_memory: str,
           relation_type: str) -> dict:
    with db.formal() as conn:
        cur = conn.execute(
            "UPDATE memory_relations SET active=0, version=version+1"
            " WHERE from_memory=? AND to_memory=? AND relation_type=? AND active=1",
            (from_memory, to_memory, relation_type))
        if cur.rowcount == 0:
            raise NotFound("active relation not found")
    return {"detached": True}


def list_for(memory_id: str, direction: str = "both") -> list[dict]:
    """显式正/反查询；反向结果标注 reversed=true。"""
    out: list[dict] = []
    with db.formal() as conn:
        if direction in ("out", "both"):
            rows = conn.execute(
                "SELECT * FROM memory_relations WHERE from_memory=? AND active=1",
                (memory_id,)).fetchall()
            out += [{**dict(r), "reversed": False} for r in rows]
        if direction in ("in", "both"):
            rows = conn.execute(
                "SELECT * FROM memory_relations WHERE to_memory=? AND active=1",
                (memory_id,)).fetchall()
            out += [{**dict(r), "reversed": True} for r in rows]
    return out


def trace(memory_id: str, max_depth: int = 5) -> dict:
    """沿 continuation_of 追事件链（新进展连接旧经历，§9.3）。"""
    chain: list[str] = [memory_id]
    seen = {memory_id}
    with db.formal() as conn:
        cur = memory_id
        for _ in range(max_depth):
            row = conn.execute(
                "SELECT to_memory FROM memory_relations WHERE from_memory=?"
                " AND relation_type='continuation_of' AND active=1", (cur,)).fetchone()
            if not row or row["to_memory"] in seen:
                break
            cur = row["to_memory"]
            chain.append(cur)
            seen.add(cur)
    return {"chain": chain, "depth": len(chain) - 1}


def related_ids(conn, memory_id: str) -> list[str]:
    """检索途径：通过关联找到的桶（matched_by=relation 的候选集合）。"""
    ids: set[str] = set()
    for r in conn.execute(
            "SELECT from_memory, to_memory FROM memory_relations"
            " WHERE (from_memory=? OR to_memory=?) AND active=1",
            (memory_id, memory_id)):
        ids.add(r["from_memory"] if r["from_memory"] != memory_id else r["to_memory"])
    return sorted(ids)
