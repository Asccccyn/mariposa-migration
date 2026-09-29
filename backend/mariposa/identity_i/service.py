"""I：周家明自行维护的当前自我 + 可按需查阅的版本历史。

- 只有周家明能新增/修改/恢复 I；乔生只能提建议；
- 每个 I 条目只有最新 revision 生效；旧 revision 默认不注入、不进普通召回；
- 当前态只暴露 has_history/history_count，历史正文须显式读取；
- restore 也创建新 revision，绝不把 current 指针直接倒回旧版；
- 修改可带理由、参考旧 revision，并可绑定记忆桶供之后沿 relation 回看；
- 旧 Self/Home 不自动改名成 I；既有正式 I 只安全迁成 i_main 条目。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound, VersionConflict
from ..memory.service import canonical_hash

DOC_ID = "i_main"
_RELATION_TYPES = {
    "changed_because_of", "clarified_by", "informed_by", "related"
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_jiaming(principal_id: str) -> None:
    if principal_id != "jiaming":
        raise Forbidden("only jiaming writes I", principal=principal_id)


def _require_content(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        raise Forbidden("I content required", code="INVALID_ARGUMENT")
    return text


def _item_row(conn, item_id: str):
    row = conn.execute(
        "SELECT * FROM i_items WHERE item_id=?", (item_id,)
    ).fetchone()
    if row is None:
        raise NotFound("I item not found", item_id=item_id)
    return row


def _revision_row(conn, item_id: str, revision: int):
    row = conn.execute(
        "SELECT * FROM i_item_revisions WHERE item_id=? AND revision=?",
        (item_id, revision),
    ).fetchone()
    if row is None:
        raise NotFound("I revision not found", item_id=item_id,
                       revision=revision)
    return row


def _current_items(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT i.item_id, i.position, i.current_revision, i.updated_at,"
        " r.content, r.authored_by"
        " FROM i_items i JOIN i_item_revisions r"
        " ON r.item_id=i.item_id AND r.revision=i.current_revision"
        " ORDER BY i.position, i.item_id"
    ).fetchall()
    out = []
    for row in rows:
        rev = int(row["current_revision"])
        out.append({
            "item_id": row["item_id"],
            "position": row["position"],
            "content": row["content"],
            "revision": rev,
            "authored_by": row["authored_by"],
            "updated_at": row["updated_at"],
            "has_history": rev > 1,
            "history_count": max(0, rev - 1),
            "history_hint": (
                "有历史版本；需要时调用 i.item.history 主动查阅"
                if rev > 1 else None
            ),
        })
    return out


def _render_current(items: list[dict]) -> str | None:
    return "\n\n".join(i["content"] for i in items) if items else None


def _validate_relations(conn, relations: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for rel in relations or []:
        if not isinstance(rel, dict):
            raise Forbidden("I relation must be an object",
                            code="INVALID_ARGUMENT")
        memory_id = str(rel.get("memory_id", "")).strip()
        relation_type = str(rel.get("relation_type", "related")).strip() or "related"
        if not memory_id:
            raise Forbidden("memory_id required for I relation",
                            code="INVALID_ARGUMENT")
        if relation_type not in _RELATION_TYPES:
            raise Forbidden("invalid I relation_type", code="INVALID_ARGUMENT",
                            relation_type=relation_type)
        if conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                        (memory_id,)).fetchone() is None:
            raise NotFound("memory for I relation not found", memory_id=memory_id)
        key = (memory_id, relation_type)
        if key not in seen:
            seen.add(key)
            out.append({"memory_id": memory_id,
                        "relation_type": relation_type})
    return out


def _insert_relations(conn, item_id: str, revision: int,
                      relations: list[dict], now: str) -> None:
    for rel in relations:
        conn.execute(
            "INSERT INTO i_revision_memory_relations("
            " item_id, revision, memory_id, relation_type, created_at)"
            " VALUES(?,?,?,?,?)",
            (item_id, revision, rel["memory_id"], rel["relation_type"], now),
        )


def _relations_for(conn, item_id: str, revision: int) -> list[dict]:
    rows = conn.execute(
        "SELECT memory_id, relation_type, created_at"
        " FROM i_revision_memory_relations"
        " WHERE item_id=? AND revision=?"
        " ORDER BY relation_type, memory_id",
        (item_id, revision),
    ).fetchall()
    return [dict(r) for r in rows]


def _append_document_snapshot(conn, principal_id: str, now: str) -> int:
    """兼容旧 i.get/i.versions：条目变化后追加一份当前整体快照。"""
    content = _render_current(_current_items(conn))
    if content is None:
        raise Forbidden("cannot snapshot empty I", code="INVALID_ARGUMENT")
    doc = conn.execute("SELECT * FROM i_documents WHERE doc_id=?",
                       (DOC_ID,)).fetchone()
    if doc is None:
        version = 1
        conn.execute(
            "INSERT INTO i_documents(doc_id, current_version_no, updated_at)"
            " VALUES(?,?,?)", (DOC_ID, version, now))
    else:
        version = int(doc["current_version_no"]) + 1
        conn.execute(
            "UPDATE i_documents SET current_version_no=?, updated_at=?"
            " WHERE doc_id=?", (version, now, DOC_ID))
    payload = {"content": content, "authored_by": principal_id}
    conn.execute(
        "INSERT INTO i_versions(doc_id, version_no, content, authored_by,"
        " payload_hash, created_at) VALUES(?,?,?,?,?,?)",
        (DOC_ID, version, content, principal_id,
         canonical_hash(payload), now))
    return version


def get() -> dict:
    with db.formal() as conn:
        items = _current_items(conn)
        doc = conn.execute("SELECT * FROM i_documents WHERE doc_id=?",
                           (DOC_ID,)).fetchone()
        if not items:
            return {"doc_id": DOC_ID, "content": None, "version": 0,
                    "items": [], "item_count": 0,
                    "note": "I 尚未落笔；由周家明写入"}
        return {
            "doc_id": DOC_ID,
            "content": _render_current(items),
            "version": int(doc["current_version_no"]) if doc else 0,
            "items": items,
            "item_count": len(items),
            "note": None,
        }


def write(principal_id: str, content: str,
          expected_version: int | None = None) -> dict:
    """旧整篇 I API：只在单条 i_main 模式保留兼容。"""
    _require_jiaming(principal_id)
    text = _require_content(content)
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            items = _current_items(conn)
            doc = conn.execute("SELECT * FROM i_documents WHERE doc_id=?",
                               (DOC_ID,)).fetchone()
            current_doc_version = int(doc["current_version_no"]) if doc else 0
            if expected_version is not None and \
                    int(expected_version) != current_doc_version:
                raise VersionConflict("I version moved",
                                      expected=expected_version,
                                      current=current_doc_version)
            if not items:
                conn.execute(
                    "INSERT INTO i_items(item_id, position, current_revision,"
                    " created_at, updated_at) VALUES(?,0,1,?,?)",
                    (DOC_ID, now, now))
                payload = {"content": text, "change_type": "create"}
                conn.execute(
                    "INSERT INTO i_item_revisions("
                    " item_id, revision, content, authored_by, change_type,"
                    " based_on_revision, restored_from_revision,"
                    " informed_by_revision, change_reason, payload_hash, created_at)"
                    " VALUES(?,1,?,?, 'create',NULL,NULL,NULL,NULL,?,?)",
                    (DOC_ID, text, principal_id,
                     canonical_hash(payload), now))
            else:
                if len(items) != 1 or items[0]["item_id"] != DOC_ID:
                    raise Forbidden(
                        "I is in item mode; use i.item.create/revise/restore",
                        code="I_ITEM_MODE_REQUIRED")
                current = int(items[0]["revision"])
                new_revision = current + 1
                payload = {"content": text, "change_type": "revise",
                           "based_on_revision": current}
                conn.execute(
                    "INSERT INTO i_item_revisions("
                    " item_id, revision, content, authored_by, change_type,"
                    " based_on_revision, restored_from_revision,"
                    " informed_by_revision, change_reason, payload_hash, created_at)"
                    " VALUES(?,?,?,?, 'revise', ?,NULL,NULL,?,?,?)",
                    (DOC_ID, new_revision, text, principal_id, current,
                     "compat i.write", canonical_hash(payload), now))
                conn.execute(
                    "UPDATE i_items SET current_revision=?, updated_at=?"
                    " WHERE item_id=?", (new_revision, now, DOC_ID))
            new_version = _append_document_snapshot(conn, principal_id, now)
            audit.record(conn, "i.written", principal_id, resource_id=DOC_ID,
                         resource_version=new_version,
                         payload={"bytes": len(text), "compat": True})
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


def items_list() -> list[dict]:
    """列出当前生效 I 条目；不带任何旧 revision 正文。"""
    with db.formal() as conn:
        return _current_items(conn)


def item_get(item_id: str) -> dict:
    """读取单条当前 I；历史只以 has_history 指针存在。"""
    with db.formal() as conn:
        row = _item_row(conn, item_id)
        rev = _revision_row(conn, item_id, int(row["current_revision"]))
        current = int(row["current_revision"])
        return {
            "item_id": item_id,
            "position": row["position"],
            "content": rev["content"],
            "revision": current,
            "authored_by": rev["authored_by"],
            "updated_at": row["updated_at"],
            "has_history": current > 1,
            "history_count": max(0, current - 1),
            "history_hint": (
                "有历史版本；需要时调用 i.item.history 主动查阅"
                if current > 1 else None
            ),
        }


def item_create(principal_id: str, content: str,
                change_reason: str | None = None,
                relations: list[dict] | None = None) -> dict:
    """新增是一条全新的 I_ITEM，不伪装成既有条目的修改。"""
    _require_jiaming(principal_id)
    text = _require_content(content)
    item_id = f"i_{uuid.uuid4().hex[:10]}"
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            safe_relations = _validate_relations(conn, relations)
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM i_items"
            ).fetchone()["p"]
            conn.execute(
                "INSERT INTO i_items(item_id, position, current_revision,"
                " created_at, updated_at) VALUES(?,?,1,?,?)",
                (item_id, position, now, now))
            payload = {"content": text, "change_type": "create",
                       "change_reason": change_reason}
            conn.execute(
                "INSERT INTO i_item_revisions("
                " item_id, revision, content, authored_by, change_type,"
                " based_on_revision, restored_from_revision, informed_by_revision,"
                " change_reason, payload_hash, created_at)"
                " VALUES(?,1,?,?, 'create',NULL,NULL,NULL,?,?,?)",
                (item_id, text, principal_id, change_reason,
                 canonical_hash(payload), now))
            _insert_relations(conn, item_id, 1, safe_relations, now)
            doc_version = _append_document_snapshot(conn, principal_id, now)
            audit.record(
                conn, "i.item.created", principal_id,
                resource_id=item_id, resource_version=1,
                payload={"doc_version": doc_version,
                         "relation_count": len(safe_relations)})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"item_id": item_id, "revision": 1,
            "doc_version": doc_version, "has_history": False}


def item_history(item_id: str) -> dict:
    """显式读取历史；旧 I 正文只通过这个窄通道进入上下文。"""
    with db.formal() as conn:
        item = _item_row(conn, item_id)
        current = int(item["current_revision"])
        rows = conn.execute(
            "SELECT * FROM i_item_revisions WHERE item_id=? ORDER BY revision",
            (item_id,)).fetchall()
        revisions = []
        for row in rows:
            data = dict(row)
            data["is_current"] = int(row["revision"]) == current
            data["relations"] = _relations_for(
                conn, item_id, int(row["revision"]))
            revisions.append(data)
    return {"item_id": item_id, "current_revision": current,
            "revisions": revisions}


def item_revise(principal_id: str, item_id: str, content: str,
                expected_revision: int, change_reason: str | None = None,
                informed_by_revision: int | None = None,
                relations: list[dict] | None = None) -> dict:
    """修改当前 I：生成新 revision，可注明由某个旧 revision 重新启发。"""
    _require_jiaming(principal_id)
    text = _require_content(content)
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            item = _item_row(conn, item_id)
            current = int(item["current_revision"])
            if current != int(expected_revision):
                raise VersionConflict("I item revision moved",
                                      expected=expected_revision,
                                      current=current)
            informed = None
            if informed_by_revision is not None:
                informed = int(informed_by_revision)
                if informed >= current:
                    raise Forbidden(
                        "informed_by_revision must be an older revision",
                        code="INVALID_ARGUMENT")
                _revision_row(conn, item_id, informed)
            safe_relations = _validate_relations(conn, relations)
            new_revision = current + 1
            payload = {
                "content": text,
                "change_type": "revise",
                "based_on_revision": current,
                "informed_by_revision": informed,
                "change_reason": change_reason,
            }
            conn.execute(
                "INSERT INTO i_item_revisions("
                " item_id, revision, content, authored_by, change_type,"
                " based_on_revision, restored_from_revision, informed_by_revision,"
                " change_reason, payload_hash, created_at)"
                " VALUES(?,?,?,?, 'revise', ?,NULL,?,?,?,?)",
                (item_id, new_revision, text, principal_id, current,
                 informed, change_reason, canonical_hash(payload), now))
            _insert_relations(conn, item_id, new_revision, safe_relations, now)
            conn.execute(
                "UPDATE i_items SET current_revision=?, updated_at=?"
                " WHERE item_id=?", (new_revision, now, item_id))
            doc_version = _append_document_snapshot(conn, principal_id, now)
            audit.record(
                conn, "i.item.revised", principal_id,
                resource_id=item_id, resource_version=new_revision,
                payload={"based_on_revision": current,
                         "informed_by_revision": informed,
                         "doc_version": doc_version,
                         "relation_count": len(safe_relations)})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"item_id": item_id, "revision": new_revision,
            "doc_version": doc_version, "has_history": True}


def item_restore(principal_id: str, item_id: str, restore_revision: int,
                 expected_revision: int, change_reason: str | None = None,
                 relations: list[dict] | None = None) -> dict:
    """重新认可旧 I：复制旧内容成为一个新的当前 revision。"""
    _require_jiaming(principal_id)
    now = _now()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            item = _item_row(conn, item_id)
            current = int(item["current_revision"])
            if current != int(expected_revision):
                raise VersionConflict("I item revision moved",
                                      expected=expected_revision,
                                      current=current)
            target_no = int(restore_revision)
            if target_no >= current:
                raise Forbidden("restore_revision must be an older revision",
                                code="INVALID_ARGUMENT")
            target = _revision_row(conn, item_id, target_no)
            safe_relations = _validate_relations(conn, relations)
            new_revision = current + 1
            payload = {
                "content": target["content"],
                "change_type": "restore",
                "based_on_revision": current,
                "restored_from_revision": target_no,
                "change_reason": change_reason,
            }
            conn.execute(
                "INSERT INTO i_item_revisions("
                " item_id, revision, content, authored_by, change_type,"
                " based_on_revision, restored_from_revision, informed_by_revision,"
                " change_reason, payload_hash, created_at)"
                " VALUES(?,?,?,?, 'restore', ?,?,NULL,?,?,?)",
                (item_id, new_revision, target["content"], principal_id,
                 current, target_no, change_reason,
                 canonical_hash(payload), now))
            _insert_relations(conn, item_id, new_revision, safe_relations, now)
            conn.execute(
                "UPDATE i_items SET current_revision=?, updated_at=?"
                " WHERE item_id=?", (new_revision, now, item_id))
            doc_version = _append_document_snapshot(conn, principal_id, now)
            audit.record(
                conn, "i.item.restored", principal_id,
                resource_id=item_id, resource_version=new_revision,
                payload={"based_on_revision": current,
                         "restored_from_revision": target_no,
                         "doc_version": doc_version,
                         "relation_count": len(safe_relations)})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"item_id": item_id, "revision": new_revision,
            "restored_from_revision": target_no,
            "doc_version": doc_version, "has_history": True}


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
