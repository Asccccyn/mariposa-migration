"""信件与删除：沿用本地旧 Ombre 已核验行为（docs/legacy_behavior_matrix.md）。

继承的旧规格（main2.5 v2.17.11 源码核验）：
- 删除信 = 提交删除申请（confirm 语义），非直接物理删除
- 删除申请：reason 必填；同资源 pending_exists(409)；DAILY_LIMIT=10/天、
  LIFETIME_LIMIT=5/资源；withdraw 撤回；decide(approve/reject) 由 AI 侧审批，
  可带 ai_reason 与 expected_resource_id 乐观校验；目标不活跃 -> superseded
- 锁信：timed 锁 + unlock_date；读时过期锁归一化（normalize_expired_lock 语义）；
  列表 metadata-only，不返回正文
- 旧“测试桶豁免直删”通道不迁移：mariposa 不暴露无审批物理删除

mariposa 映射：decide approve + action=delete 才物理删除（有审批+限额+审计）；
archive -> memory.visibility=archived / letters.archived=1（对齐旧 bucket_mgr.archive）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import audit, config, db
from ..errors import (AlreadyDecided, DeleteBlocked, Forbidden,
                      LockedResource, NotFound)
from ..memory import service as memory
from ..retrieval import projection

DAILY_LIMIT = 10
LIFETIME_LIMIT = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(payload: dict) -> str:
    return memory.canonical_hash(payload)


def write_letter(principal, content: str, letter_date: str | None = None,
                 lock_type: str = "none", unlock_date: str | None = None) -> dict:
    principal_id = getattr(principal, "principal_id", principal)
    if not content or not str(content).strip():
        raise Forbidden("letter content required")
    lock_type, unlock_date = normalize_lock(lock_type, unlock_date)
    lid = f"lt_{uuid.uuid4().hex[:10]}"
    now = _now().isoformat()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO letters(id, current_version_no, author, letter_date,"
                " lock_type, unlock_date, created_at, updated_at)"
                " VALUES(?,1,?,?,?,?,?,?)",
                (lid, principal_id, letter_date or now[:10], lock_type, unlock_date,
                 now, now))
            conn.execute(
                "INSERT INTO letter_versions(letter_id, version_no, content, edited_by,"
                " payload_hash, created_at) VALUES(?,1,?,?,?,?)",
                (lid, str(content), principal_id,
                 _hash({"content": content, "v": 1}), now))
            audit.record(conn, "letter.written", principal_id, resource_id=lid,
                         resource_version=1,
                         payload={"lock_type": lock_type, "unlock_date": unlock_date})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"letter_id": lid, "version": 1, "lock_type": lock_type,
            "unlock_date": unlock_date}


def normalize_lock(lock_type: str, unlock_date: str | None) -> tuple[str, str | None]:
    """normalize_lock_type + normalize_unlock_date 的已核验子集。"""
    lock_type = (lock_type or "none").strip().lower()
    if lock_type not in ("none", "timed", "locked"):
        raise Forbidden(f"invalid lock_type: {lock_type}")
    if lock_type == "timed":
        if not unlock_date:
            raise Forbidden("timed lock requires unlock_date")
        try:
            datetime.fromisoformat(str(unlock_date).replace("Z", "+00:00"))
        except ValueError:
            raise Forbidden("unlock_date must be ISO-8601")
    else:
        unlock_date = None if lock_type == "none" else unlock_date
    return lock_type, unlock_date


def lock_state(conn, letter_row) -> dict:
    """读时归一化：过期 timed 锁视为 unlocked（normalize_expired_lock 语义）。"""
    if letter_row["lock_type"] != "timed":
        return {"locked": letter_row["lock_type"] == "locked",
                "lock_type": letter_row["lock_type"], "unlock_date": None}
    expired = str(letter_row["unlock_date"]) <= _now().isoformat()
    return {"locked": not expired, "lock_type": "timed",
            "unlock_date": letter_row["unlock_date"], "expired": expired}


def list_letters(author: str | None = None,
                 include_archived: bool = False) -> list[dict]:
    """metadata-only 列表：排序 letter_date/created 倒序；锁信不返回正文。

    旧系统语义（bucket_mgr.list_all(include_archive=False)）：默认不含归档信。
    """
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT * FROM letters ORDER BY COALESCE(letter_date, created_at) DESC"
        ).fetchall()
        out = []
        for r in rows:
            if r["archived"] and not include_archived:
                continue
            if author and r["author"] != author:
                continue
            out.append({
                "letter_id": r["id"], "author": r["author"],
                "letter_date": r["letter_date"], "version": r["current_version_no"],
                "archived": bool(r["archived"]),
                "lock": lock_state(conn, r),
            })
        return out


def read_letter(principal_id: str, letter_id: str) -> dict:
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM letters WHERE id=?", (letter_id,)).fetchone()
        if row is None:
            raise NotFound("letter not found", letter_id=letter_id)
        state = lock_state(conn, row)
        if state["locked"]:
            raise LockedResource(
                "letter is locked", letter_id=letter_id,
                unlock_date=state.get("unlock_date"))
        v = conn.execute(
            "SELECT * FROM letter_versions WHERE letter_id=? AND version_no=?",
            (letter_id, row["current_version_no"])).fetchone()
        return {"letter_id": letter_id, "author": row["author"],
                "letter_date": row["letter_date"],
                "version": row["current_version_no"],
                "archived": bool(row["archived"]),
                "content": v["content"], "lock": state}


def edit_letter(principal_id: str, letter_id: str, expected_version: int,
                content: str | None = None, lock_type: str | None = None,
                unlock_date: str | None = None) -> dict:
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM letters WHERE id=?", (letter_id,)).fetchone()
        if row is None:
            raise NotFound("letter not found", letter_id=letter_id)
        if row["author"] != principal_id:
            raise Forbidden("only the author may edit a letter")
        if row["current_version_no"] != expected_version:
            raise Forbidden("letter version conflict",
                            code="VERSION_CONFLICT",
                            expected=expected_version,
                            current=row["current_version_no"])
        state = lock_state(conn, row)
        if state["locked"] and lock_type is None:
            raise LockedResource("locked letter cannot be edited", letter_id=letter_id)
        new_lock, new_unlock = normalize_lock(
            lock_type or row["lock_type"],
            unlock_date if lock_type else row["unlock_date"])
        # 仅锁更新不改正文版本（旧系统 lock_update 独立于正文修订）
        new_version = row["current_version_no"] + 1 if content is not None \
            else row["current_version_no"]
        now = _now().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if content is not None:
                conn.execute(
                    "INSERT INTO letter_versions(letter_id, version_no, content,"
                    " edited_by, payload_hash, created_at) VALUES(?,?,?,?,?,?)",
                    (letter_id, new_version, str(content), principal_id,
                     _hash({"content": content, "v": new_version}), now))
            conn.execute(
                "UPDATE letters SET current_version_no=?, lock_type=?, unlock_date=?,"
                " updated_at=? WHERE id=?",
                (new_version, new_lock, new_unlock, now, letter_id))
            audit.record(conn, "letter.lock_update" if content is None else
                         "letter.edited", principal_id, resource_id=letter_id,
                         resource_version=new_version,
                         payload={"lock_type": new_lock})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"letter_id": letter_id, "version": new_version,
            "lock_type": new_lock, "unlock_date": new_unlock}


# ---------- 删除申请（旧 DeletionRequestStore 规格迁移） ----------

def deletion_submit(principal_id: str, resource_id: str, reason: str,
                    action: str = "delete", resource_kind: str = "memory") -> dict:
    """人类提交删除申请。reason 必填；限额与旧系统一致。"""
    if action not in ("archive", "delete"):
        raise Forbidden("action must be archive or delete")
    reason = str(reason or "").strip()
    if not reason:
        raise Forbidden("deletion reason is required", code="reason_required")
    with db.formal() as conn:
        kind = resource_kind
        if kind == "memory":
            target = conn.execute("SELECT memory_id, visibility FROM memories"
                                  " WHERE memory_id=?", (resource_id,)).fetchone()
        else:
            target = conn.execute("SELECT id FROM letters WHERE id=?",
                                  (resource_id,)).fetchone()
        if target is None:
            raise NotFound("target not found", resource_id=resource_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            related = conn.execute(
                "SELECT status FROM deletion_requests WHERE resource_id=?",
                (resource_id,)).fetchall()
            if any(r["status"] == "pending" for r in related):
                raise Forbidden("a deletion request is already pending",
                                code="pending_exists")
            if len(related) >= LIFETIME_LIMIT:
                raise Forbidden("resource lifetime deletion request limit reached",
                                code="lifetime_limit")
            local_date = _now().astimezone(ZoneInfo(config.RELATIONSHIP_TIMEZONE)) \
                .date().isoformat()
            today_count = conn.execute(
                "SELECT COUNT(*) AS c FROM deletion_requests WHERE local_date=?",
                (local_date,)).fetchone()["c"]
            if today_count >= DAILY_LIMIT:
                raise Forbidden("daily deletion request limit reached",
                                code="daily_limit")
            rid = f"del_{uuid.uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO deletion_requests(id, resource_id, resource_kind, action,"
                " human_reason, ai_reason, status, submitted_by, submitted_at, local_date)"
                " VALUES(?,?,?,?,?,'', 'pending', ?,?,?)",
                (rid, resource_id, kind, action, reason, principal_id,
                 _now().isoformat(), local_date))
            audit.record(conn, "deletion.requested", principal_id, resource_id=rid,
                         payload={"target": resource_id, "action": action})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"request_id": rid, "status": "pending", "action": action}


def deletion_withdraw(principal_id: str, resource_id: str) -> dict:
    """撤回该资源最近一条 pending。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM deletion_requests WHERE resource_id=? AND"
                " status='pending' ORDER BY submitted_at DESC LIMIT 1",
                (resource_id,)).fetchone()
            if row is None:
                raise NotFound("pending deletion request not found",
                               resource_id=resource_id)
            conn.execute(
                "UPDATE deletion_requests SET status='withdrawn', decided_at=?,"
                " decided_by=? WHERE id=?",
                (_now().isoformat(), principal_id, row["id"]))
            audit.record(conn, "deletion.withdrawn", principal_id,
                         resource_id=row["id"])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"request_id": row["id"], "status": "withdrawn"}


def _target_active(conn, resource_id: str, kind: str) -> bool:
    if kind == "memory":
        r = conn.execute("SELECT visibility FROM memories WHERE memory_id=?",
                         (resource_id,)).fetchone()
        return bool(r) and r["visibility"] == "active"
    r = conn.execute("SELECT 1 FROM letters WHERE id=?", (resource_id,)).fetchone()
    return bool(r)


def deletion_decide(principal_id: str, request_id: str, decision: str,
                    ai_reason: str = "", expected_resource_id: str = "") -> dict:
    """AI 侧（周家明）审批；approve 执行 archive/delete。

    审计 F38：状态迁移在写事务内以 pending 为条件做 CAS——并发的
    approve/reject 只有一个能落定，另一个得到结构化 ALREADY_DECIDED；
    状态更新与删除副作用同一事务提交，数据库状态与实际副作用一致。
    """
    if principal_id != "jiaming":
        raise Forbidden("only jiaming decides deletion requests",
                        principal=principal_id)
    if decision not in ("approve", "reject"):
        raise Forbidden("decision must be approve or reject")
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM deletion_requests WHERE id=?",
                           (request_id,)).fetchone()
        if row is None or row["status"] != "pending":
            raise NotFound("pending deletion request not found", request_id=request_id)
        if expected_resource_id and expected_resource_id != row["resource_id"]:
            raise Forbidden("deletion request does not match resource_id",
                            code="bucket_mismatch")
        if not _target_active(conn, row["resource_id"], row["resource_kind"]):
            supersede_conflict = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE deletion_requests SET status='superseded',"
                    " decided_at=?, decided_by=? WHERE id=?"
                    " AND status='pending'",
                    (_now().isoformat(), principal_id, request_id))
                if cur.rowcount != 1:
                    # N12：CAS 输家——不在 try 内 raise（外层 except 的
                    # ROLLBACK 会因事务已结束再炸，掩盖真实错误）
                    supersede_conflict = AlreadyDecided(
                        "deletion request already decided by a concurrent "
                        "decision", request_id=request_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            if supersede_conflict is not None:
                raise supersede_conflict
            raise Forbidden("deletion request target is no longer active",
                            code="superseded")
        conn.execute("BEGIN IMMEDIATE")
        try:
            status = "approved" if decision == "approve" else "rejected"
            # CAS：只有仍处于 pending 的申请能被本次决定落定
            cur = conn.execute(
                "UPDATE deletion_requests SET status=?, ai_reason=?,"
                " decided_at=?, decided_by=? WHERE id=? AND status='pending'",
                (status, str(ai_reason or "").strip(), _now().isoformat(),
                 principal_id, request_id))
            if cur.rowcount != 1:
                raise AlreadyDecided(
                    "deletion request already decided by a concurrent "
                    "decision", request_id=request_id)
            if decision == "approve":
                _execute(conn, row, principal_id)
            audit.record(conn, f"deletion.{status}", principal_id,
                         resource_id=request_id,
                         payload={"target": row["resource_id"],
                                  "action": row["action"]})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"request_id": request_id, "decision": decision,
            "resource_id": row["resource_id"], "status": status}


def _blocking_references(conn, rid: str) -> dict[str, int]:
    """阻止物理删除的正式跨域引用（审计 F34）。

    I revision 关系与 Source 绑定是跨域历史引用：物理删除 memory 会
    破坏其完整性。返回 {引用类型: 行数}；空 dict = 无引用可删。
    """
    out: dict[str, int] = {}
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM i_revision_memory_relations"
        " WHERE memory_id=?", (rid,)).fetchone()["c"]
    if n:
        out["i_revision_relations"] = n
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM memory_source_bindings"
        " WHERE memory_id=?", (rid,)).fetchone()["c"]
    if n:
        out["source_bindings"] = n
    return out


def _execute(conn, row, actor: str) -> None:
    """approve 后执行：archive -> visibility/archived；delete -> 物理删除+清派生。"""
    rid, kind = row["resource_id"], row["resource_kind"]
    if row["action"] == "archive":
        if kind == "memory":
            conn.execute("UPDATE memories SET visibility='archived',"
                         " updated_at=? WHERE memory_id=?",
                         (_now().isoformat(), rid))
            projection.remove(conn, rid)  # 归档即无默认投影
        else:
            # 对齐旧 bucket_mgr.archive：真实归档状态，列表默认不可见，正文保留
            conn.execute("UPDATE letters SET archived=1, updated_at=? WHERE id=?",
                         (_now().isoformat(), rid))
        return
    # delete：物理删除（有审批+限额+审计门槛；继承旧 HumanDeleteExecutor 语义）
    if kind == "memory":
        # 审计 F34：存在 I revision 关系 / Source 绑用的记忆不做物理删除，
        # 结构化拒绝（引用完整性优先），调用方可改走 archive。
        refs = _blocking_references(conn, rid)
        if refs:
            raise DeleteBlocked(
                "memory 被正式跨域引用，不允许物理删除；请先解除引用或"
                "改用 archive", memory_id=rid, references=refs)
        # v2 分层子表全清（B08：v1 清单不含分类/心情/话语/回忆/keep 行，
        # v2 桶会 FK 失败）
        for table in ("memory_raw_refs", "memory_categories", "memory_tags",
                      "memory_moods", "memory_mood_tags", "memory_our_words",
                      "memory_recollections", "memory_view_receipts",
                      "memory_reengagements", "memory_meanings",
                      "memory_keeps", "field_search_docs", "field_fts",
                      "search_fts", "retrieval_documents", "memory_versions"):
            conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (rid,))
        conn.execute("DELETE FROM memory_relations WHERE from_memory=?"
                     " OR to_memory=?", (rid, rid))
        conn.execute("DELETE FROM plan_memory_links WHERE memory_id=?", (rid,))
        conn.execute("DELETE FROM memories WHERE memory_id=?", (rid,))
    else:
        conn.execute("DELETE FROM letter_versions WHERE letter_id=?", (rid,))
        conn.execute("DELETE FROM letters WHERE id=?", (rid,))


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def deletion_list(status: str | None = None) -> list[dict]:
    with db.formal() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM deletion_requests WHERE status=?"
                " ORDER BY submitted_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM deletion_requests ORDER BY submitted_at DESC").fetchall()
    return [dict(r) for r in rows]
