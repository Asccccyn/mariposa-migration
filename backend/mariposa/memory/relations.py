"""桶间关系（§5.1/§8.1 + 规格 v2.0 收口）。

有向存储、显式反向查询，不复制镜像边。每条有效边有稳定
relation_id（纠错撤销/改绑的精确对象）；软删与 confidence='human'
无效字段已退役——纠错一律走 relation_corrections（§5）。
"""
from __future__ import annotations

import uuid
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
         reverse_label: str | None = None,
         conn=None) -> dict:
    """建立有效边；同端点同类型幂等（返回既有实例，不生成重复边）。"""
    if relation_type not in _RELATION_TYPES:
        raise Forbidden(f"relation_type must be one of {sorted(_RELATION_TYPES)}")
    if relation_type != "custom" and (custom_label or reverse_label):
        raise Forbidden("custom_label only for relation_type=custom")

    def _do(conn):
        for mid in (from_memory, to_memory):
            if not _exists(conn, mid):
                raise NotFound("memory not found", memory_id=mid)
        row = conn.execute(
            "SELECT relation_id FROM memory_relations"
            " WHERE from_memory=? AND to_memory=? AND relation_type=?",
            (from_memory, to_memory, relation_type)).fetchone()
        if row:
            return {"relation_id": row["relation_id"],
                    "from": from_memory, "to": to_memory,
                    "relation_type": relation_type,
                    "deduplicated": True}
        rid = f"rel_{uuid.uuid4().hex[:16]}"
        conn.execute(
            "INSERT INTO memory_relations(relation_id, from_memory,"
            " to_memory, relation_type, custom_label, reverse_label,"
            " created_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (rid, from_memory, to_memory, relation_type, custom_label,
             reverse_label, principal_id, _now()))
        return {"relation_id": rid, "from": from_memory, "to": to_memory,
                "relation_type": relation_type}
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


def correct(principal_id: str, relation_id: str,
            correction_action: str, note: str | None = None,
            replacement: dict | None = None, conn=None) -> dict:
    """纠错（§P-R02）：remove_wrong_binding / replace_wrong_binding。

    同一事务：登记纠错历史→删该条有效边→（改绑）创建新边实例。
    同 relation_id 重放的幂等由上层 atomic_write 承担。

    CB-006（2026-10-02 审计 P1）：replace 必须携带完整 replacement、
    remove 禁止携带——"替换"不得退化为无新关系的解绑。同端点替换
    （表有 UNIQUE(from,to,type)）在同事务内先删旧实例再建新实例：
    复用旧 relation_id 再删除会把替换变成删除，且回执指向已不存在
    的 replacement。
    """
    from ..relations.corrections import record_correction
    if correction_action not in ("remove_wrong_binding",
                                 "replace_wrong_binding"):
        raise Forbidden("correction_action must be remove_wrong_binding/"
                        "replace_wrong_binding")
    if correction_action == "remove_wrong_binding" and replacement:
        raise Forbidden("remove_wrong_binding 不接受 replacement（移除"
                        "语义；改绑请用 replace_wrong_binding）",
                        code="INVALID_ARGUMENT")
    if correction_action == "replace_wrong_binding" and not replacement:
        raise Forbidden("replace_wrong_binding 必须携带完整 replacement"
                        "（from_memory/to_memory/relation_type）——缺新"
                        "关系的替换即解绑", code="INVALID_ARGUMENT")

    def _do(conn):
        row = conn.execute(
            "SELECT * FROM memory_relations WHERE relation_id=?",
            (relation_id,)).fetchone()
        if row is None:
            raise NotFound("relation instance not found",
                           relation_id=relation_id)
        # 先删旧实例再 link 新实例：同端点唯一键让位（UNIQUE 约束），
        # 同事务保证原子；失败整体回滚，旧边不受损
        conn.execute("DELETE FROM memory_relations WHERE relation_id=?",
                     (relation_id,))
        replacement_id = None
        if replacement:
            rep = link(principal_id,
                       replacement.get("from_memory", ""),
                       replacement.get("to_memory", ""),
                       replacement.get("relation_type", ""),
                       replacement.get("custom_label"),
                       replacement.get("reverse_label"), conn=conn)
            replacement_id = rep["relation_id"]
        cid = record_correction(
            conn, domain="memory_relation",
            original_instance_id=row["relation_id"],
            endpoint_a=row["from_memory"], endpoint_b=row["to_memory"],
            original_meta={"relation_type": row["relation_type"],
                           "custom_label": row["custom_label"],
                           "reverse_label": row["reverse_label"]},
            original_created_by=row["created_by"],
            original_created_at=row["created_at"],
            corrected_by=principal_id, note=note,
            replacement_instance_id=replacement_id)
        return {"correction_id": cid, "removed_relation_id": relation_id,
                "replacement_relation_id": replacement_id,
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


def list_for(memory_id: str, direction: str = "both") -> list[dict]:
    """显式正/反查询；反向标注 reversed=true（§P-R03）。"""
    out: list[dict] = []
    with db.formal() as conn:
        if direction in ("out", "both"):
            rows = conn.execute(
                "SELECT * FROM memory_relations WHERE from_memory=?",
                (memory_id,)).fetchall()
            out += [{**dict(r), "reversed": False} for r in rows]
        if direction in ("in", "both"):
            rows = conn.execute(
                "SELECT * FROM memory_relations WHERE to_memory=?",
                (memory_id,)).fetchall()
            out += [{**dict(r), "reversed": True} for r in rows]
    return out


def related_ids(conn, memory_id: str) -> list[str]:
    """检索途径：双向邻居（matched_by=relation 候选集合）。"""
    ids: set[str] = set()
    for r in conn.execute(
            "SELECT from_memory, to_memory FROM memory_relations"
            " WHERE from_memory=? OR to_memory=?",
            (memory_id, memory_id)):
        ids.add(r["from_memory"] if r["from_memory"] != memory_id
                else r["to_memory"])
    return sorted(ids)


def _continuation_rows(conn, memory_id: str):
    return conn.execute(
        "SELECT from_memory, to_memory FROM memory_relations"
        " WHERE (from_memory=? OR to_memory=?)"
        " AND relation_type='continuation_of'",
        (memory_id, memory_id)).fetchall()


def trace(memory_id: str, max_depth: int = 5) -> dict:
    """沿 continuation_of 追事件链（§6.3）：全分支覆盖、双向可达、
    有环有界并披露截断（earlier=本桶指向的更早事件；later=指向本桶
    的更晚事件——方向文字不颠倒，§Q04）。"""
    earlier: list[str] = []
    later: list[str] = []
    seen = {memory_id}
    frontier = [memory_id]
    truncated = False
    depth = 0
    with db.formal() as conn:
        while frontier and depth < max(1, min(int(max_depth), 20)):
            depth += 1
            nxt: list[str] = []
            for cur in frontier:
                for r in _continuation_rows(conn, cur):
                    outgoing = r["from_memory"] == cur
                    other = r["to_memory"] if outgoing else r["from_memory"]
                    if other in seen:
                        continue
                    seen.add(other)
                    (earlier if outgoing else later).append(other)
                    nxt.append(other)
            frontier = nxt
        truncated = bool(frontier)  # 深度耗尽仍有 frontier=未查尽
    return {"memory_id": memory_id, "earlier": earlier, "later": later,
            "visited": len(seen), "truncated": truncated,
            "max_depth": max(1, min(int(max_depth), 20))}
