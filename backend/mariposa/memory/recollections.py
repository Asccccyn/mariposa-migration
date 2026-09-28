"""回忆（spec_v2 R09 / §6）：双方本人明确打开后才能写。

- 追加前必须持有该桶、该身份、有效（已确认、未过期版本）的查看回执；
- 默认 append-only，修订保存版本（supersedes 链），不做物理删除入口；
- 回忆不进检索索引；有内容后触发保留线索，暂停无疑点自动遗忘（D06）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound, ViewReceiptInvalid

_AUTHORS = ("jiaming", "qiaosheng")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append(principal, memory_id: str, receipt_id: str, text: str,
           keep_wide: bool = False) -> dict:
    """凭有效查看回执追加本人回忆（VIEW-01/02/03）。

    v1.7 §5.5：keep_wide=True 时在**同一事务**内为本次回忆登记作者
    「留」标记（memory_keeps）；默认 False，绝不因回忆非空自动留。
    keep 必须绑定本次新写入的回忆（recollection_id + version）。
    """
    author = principal.principal_id
    if author not in _AUTHORS:
        raise Forbidden("only the two owners may write recollections",
                        principal=author)
    if not text or not str(text).strip():
        raise Forbidden("recollection text required", code="INVALID_ARGUMENT")
    if not isinstance(keep_wide, bool):
        raise Forbidden("keep_wide must be a boolean", code="INVALID_ARGUMENT")
    now = _now()
    rid = f"rc_{uuid.uuid4().hex[:12]}"
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                                (memory_id,)).fetchone():
                raise NotFound("memory not found", memory_id=memory_id)
            r = conn.execute(
                "SELECT * FROM memory_view_receipts WHERE receipt_id=?",
                (receipt_id,)).fetchone()
            if (r is None or r["principal_id"] != author
                    or r["binding_id"] != principal.binding_id
                    or r["memory_id"] != memory_id):
                raise ViewReceiptInvalid(
                    "valid confirmed view receipt for this memory required",
                    receipt_id=receipt_id)
            if r["confirmed_at"] is None:
                raise ViewReceiptInvalid(
                    "view not confirmed yet; confirm before writing",
                    receipt_id=receipt_id)
            conn.execute(
                "INSERT INTO memory_recollections(recollection_id, memory_id,"
                " author, text, view_receipt, version, written_at)"
                " VALUES(?,?,?,?,?,1,?)",
                (rid, memory_id, author, str(text).strip(), receipt_id, now))
            keep_mark = None
            if keep_wide:
                # v1.7 §5.5：留 = 本次回忆写入时显式选择，同事务、指本条
                from . import keep as keep_mod
                keep_mark = keep_mod.register_in_txn(
                    conn, author, memory_id, rid, 1, now)
            audit.record(conn, "memory.recollection.appended", author,
                         resource_id=memory_id,
                         payload={"recollection_id": rid,
                                  "keep_wide": keep_wide,
                                  **({"keep_mark": keep_mark["mark_id"]}
                                     if keep_mark else {})})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    out = {"recollection_id": rid, "memory_id": memory_id,
           "author": author, "version": 1}
    if keep_mark:
        out["keep"] = keep_mark
    return out


def revise(principal, recollection_id: str, text: str) -> dict:
    """修订本人回忆：原话留底（supersedes 链 + 新版本行）。"""
    author = principal.principal_id
    if not text or not str(text).strip():
        raise Forbidden("recollection text required", code="INVALID_ARGUMENT")
    now = _now()
    rid = f"rc_{uuid.uuid4().hex[:12]}"
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            old = conn.execute(
                "SELECT * FROM memory_recollections WHERE recollection_id=?",
                (recollection_id,)).fetchone()
            if old is None:
                raise NotFound("recollection not found",
                               recollection_id=recollection_id)
            if old["author"] != author:
                raise Forbidden("recollections can only be revised by author",
                                recollection_id=recollection_id)
            conn.execute(
                "INSERT INTO memory_recollections(recollection_id, memory_id,"
                " author, text, view_receipt, supersedes, version, written_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (rid, old["memory_id"], author, str(text).strip(),
                 old["view_receipt"], recollection_id,
                 old["version"] + 1, now))
            audit.record(conn, "memory.recollection.revised", author,
                         resource_id=old["memory_id"],
                         payload={"recollection_id": rid,
                                  "supersedes": recollection_id})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"recollection_id": rid, "supersedes": recollection_id,
            "version": old["version"] + 1}


def list_for(memory_id: str, include_history: bool = False) -> list[dict]:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT recollection_id, memory_id, author, text, supersedes,"
            " version, written_at FROM memory_recollections WHERE memory_id=?"
            + ("" if include_history else
               " AND recollection_id NOT IN (SELECT supersedes FROM"
               " memory_recollections WHERE memory_id=? AND supersedes IS NOT NULL)")
            + " ORDER BY written_at",
            (memory_id,) if include_history else (memory_id, memory_id)
        ).fetchall()
    return [dict(r) for r in rows]


def current_retention_hint(memory_id: str) -> bool:
    """桶是否已有回忆内容（保留线索）。"""
    with db.formal() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM memory_recollections WHERE memory_id=? LIMIT 1",
            (memory_id,)).fetchone())
