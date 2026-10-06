"""Memory 删除（规格 v2.0 §7 重写）。

两条正式路径共享同一个五域关系硬门（P-D04）：
- 人类申请：qiaosheng 申请（理由必填；5 次/桶一生 + 10 次/上海自然日，
  P-D03 申请口径）→ jiaming 决定（reject 必填理由；approve 不要求）。
- 机器直删：jiaming 直接真删除，无申请/配额/理由审计（P-D02）。

Memory archive 已退役（P-A01）：无 action 参数、无 archived 写路径。
删除不级联任何有效关系——有有效跨资源关系一律结构化拒绝（P-R04）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import audit, db
from ..errors import AlreadyDecided, DeleteBlocked, Forbidden, NotFound
from ..retrieval import projection

LIFETIME_PER_MEMORY = 5    # 人类申请：每桶一生成功提交数（P-D03）
DAILY_LIMIT = 10           # 人类申请：每上海自然日提交数
_SUB_TABLES = ("memory_categories", "memory_tags", "memory_moods",
               "memory_mood_tags", "memory_our_words", "memory_recollections",
               "memory_view_receipts", "memory_reengagements", "memory_keeps",
               "field_search_docs", "field_fts", "search_fts",
               "retrieval_documents", "memory_versions",
               "memory_embeddings")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


def _shanghai_date() -> str:
    return _now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


# ---------------------------------------------------------------- 硬门

def blocking_relations(conn, memory_id: str) -> dict[str, int]:
    """五域有效关系硬门（§7.4）——只在写事务内调用。

    ① 桶间入边/出边；② 任意 I 修订→本桶；③ 本桶有效 Source 区间绑定；
    ④ 本桶与任意 Plan 的成员绑定（含终态 Plan）；⑤ 本桶 our_words 的
    非空 Source 来源引用（不可解析的旧引用=需要纠错，不放行删除）。
    """
    out: dict[str, int] = {}
    n = conn.execute(
        "SELECT COUNT(*) c FROM memory_relations"
        " WHERE from_memory=? OR to_memory=?",
        (memory_id, memory_id)).fetchone()["c"]
    if n:
        out["memory_relations"] = n
    n = conn.execute(
        "SELECT COUNT(*) c FROM i_revision_memory_relations"
        " WHERE memory_id=?", (memory_id,)).fetchone()["c"]
    if n:
        out["i_revision_relations"] = n
    n = conn.execute(
        "SELECT COUNT(*) c FROM memory_source_bindings"
        " WHERE memory_id=?", (memory_id,)).fetchone()["c"]
    if n:
        out["source_bindings"] = n
    n = conn.execute(
        "SELECT COUNT(*) c FROM plan_memory_links"
        " WHERE memory_id=?", (memory_id,)).fetchone()["c"]
    if n:
        out["plan_links"] = n
    # 裁定（2026-10-04）：dangling provenance 不算有效引用——只有
    # 解析到当前已发布消息的 source_msg: 才进硬门计数；legacy
    # raw_msg:/不存在的引用标 gap 但不得阻止删除
    n = conn.execute(
        "SELECT COUNT(*) c FROM memory_our_words w"
        " WHERE w.memory_id=? AND w.source_ref IS NOT NULL"
        " AND w.source_ref <> '' AND w.source_ref LIKE 'source_msg:%'"
        " AND EXISTS (SELECT 1 FROM source_messages m WHERE"
        " (m.id = substr(w.source_ref, 12) OR"
        "  m.provider_message_id = substr(w.source_ref, 12))"
        " AND m.published=1)", (memory_id,)).fetchone()["c"]
    if n:
        out["word_sources"] = n
    return out


def _gate_or_raise(conn, memory_id: str) -> None:
    refs = blocking_relations(conn, memory_id)
    if refs:
        raise DeleteBlocked(
            "memory 存在有效跨资源关系，不能删除；请核对并纠正真正"
            "错误的绑定后再删除", memory_id=memory_id, references=refs)


# ---------------------------------------------------------------- 物理删除

def _execute_delete(conn, memory_id: str, actor: str) -> None:
    """真删除（关系硬门通过后）：当前资源+从属子记录+派生索引。
    不删除申请历史/纠错历史（不随桶级联，§7.5）；不触碰其它桶、
    Plan、I 修订正文、Source 母本与消息。
    """
    _gate_or_raise(conn, memory_id)
    projection.remove(conn, memory_id)
    # F09：向量派生表由检索侧惰性建表——未启用过 dense 的库上删除
    # 也要能跑，先幂等确保 schema 存在再清理
    from ..retrieval import semantic as _sem, words_semantic as _wsem
    _sem.ensure_schema(conn)
    _wsem.ensure_schema(conn)
    # RA-020（2026-10-02 复审 P2）：先取 word IDs——our_words 删除后
    # words 派生表（words_search_docs/words_fts）按 word_id 同事务清理
    #（此前正文副本留存到下次全量重建）
    word_ids = [r["word_id"] for r in conn.execute(
        "SELECT word_id FROM memory_our_words WHERE memory_id=?",
        (memory_id,))]
    for table in _SUB_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (memory_id,))
    for wid in word_ids:
        conn.execute("DELETE FROM words_search_docs WHERE word_id=?",
                     (wid,))
        conn.execute("DELETE FROM words_fts WHERE word_id=?", (wid,))
        # F09（2026-10-03 审计 P2）：words 向量派生索引同事务真删除
        conn.execute("DELETE FROM word_embeddings WHERE word_id=?", (wid,))
    conn.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
    audit.record(conn, "memory.deleted", actor,
                 resource_id=memory_id, resource_version=0,
                 payload={"path": "physical_delete"})


# ---------------------------------------------------------------- 人类申请

def deletion_request(principal_id: str, memory_id: str, reason: str,
                     operation_key: str | None = None) -> dict:
    """人类申请（仅 qiaosheng；P-D01/P-D03）。

    幂等：同 operation_key 重放返回同一申请（不重复计数）。申请与
    配额检查在写锁内完成；失败回滚不产生申请。
    """
    if principal_id != "qiaosheng":
        raise Forbidden("删除申请仅江乔生（人类路径）可用；周家明维护"
                        "删除走 memory.delete", code="OWNER_MISMATCH")
    reason = (reason or "").strip()
    if not reason:
        raise Forbidden("申请理由必填（P-D01）", code="INVALID_ARGUMENT")
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # RA-019（2026-10-02 复审 P2）：operation 幂等移入写锁——
            # BEGIN 内先查 completed+payload_hash（同 key 同载荷重放/
            # 异载荷冲突），并发同键第二请求不再只看 pending 抛
            # CONFLICT 而是拿到相同回执
            if operation_key:
                import json as _json
                import hashlib as _hl
                ph = _hl.sha256(_json.dumps(
                    {"memory_id": memory_id, "reason": reason},
                    ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                prior = conn.execute(
                    "SELECT payload_hash, result_ref FROM"
                    " idempotency_records WHERE principal_id=? AND"
                    " capability='memory.deletion.request' AND"
                    " idempotency_key=?",
                    (principal_id, f"op:{operation_key}")).fetchone()
                if prior is not None:
                    if prior["payload_hash"] not in (None, ph):
                        from ..errors import IdempotencyConflict
                        raise IdempotencyConflict(
                            "same operation key with different payload",
                            operation_key=operation_key)
                    saved = _json.loads(prior["result_ref"])
                    if saved.get("memory_id") == memory_id:
                        try:
                            out = deletion_get(saved["request_id"])
                            out["idempotent_replay"] = True
                            conn.execute("COMMIT")
                            return out
                        except NotFound:
                            # F-J-26（联合审计 2026-10-06）：回执指向的申请不存在
                            # （现行合同 deletion_requests 行不删除，理论不可达）——
                            # 落穿会在下方 INSERT 撞同键 UNIQUE 变裸 500，改结构化
                            # 冲突；若未来裁定允许换发，先清旧记录再重建
                            from ..errors import IdempotencyConflict
                            raise IdempotencyConflict(
                                "幂等回执指向的申请不存在（记录陈旧）",
                                operation_key=operation_key)
                    else:
                        # F-J-26：同键指向不同桶（legacy 无 payload_hash 行
                        # 可落到此）——结构化冲突，不落穿撞键
                        from ..errors import IdempotencyConflict
                        raise IdempotencyConflict(
                            "same operation key with different memory target",
                            operation_key=operation_key)
            if not conn.execute(
                    "SELECT 1 FROM memories WHERE memory_id=?",
                    (memory_id,)).fetchone():
                raise NotFound("memory not found", memory_id=memory_id)
            pending = conn.execute(
                "SELECT request_id FROM deletion_requests"
                " WHERE memory_id=? AND status='pending'",
                (memory_id,)).fetchone()
            if pending:
                raise Forbidden("该桶已有 pending 申请（同桶最多一条）",
                                code="CONFLICT", request_id=pending[0])
            lifetime = conn.execute(
                "SELECT COUNT(*) c FROM deletion_requests"
                " WHERE memory_id=? AND submitted_by='qiaosheng'",
                (memory_id,)).fetchone()["c"]
            if lifetime >= LIFETIME_PER_MEMORY:
                raise Forbidden(
                    f"该桶人类删除申请已达一生上限 {LIFETIME_PER_MEMORY} 次",
                    code="QUOTA_EXCEEDED", scope="lifetime",
                    limit=LIFETIME_PER_MEMORY)
            local_date = _shanghai_date()
            daily = conn.execute(
                "SELECT COUNT(*) c FROM deletion_requests"
                " WHERE submitted_by='qiaosheng' AND submitted_local_date=?",
                (local_date,)).fetchone()["c"]
            if daily >= DAILY_LIMIT:
                raise Forbidden(
                    f"今日（上海自然日）申请已达上限 {DAILY_LIMIT} 次",
                    code="QUOTA_EXCEEDED", scope="daily", limit=DAILY_LIMIT)
            rid = f"dr_{uuid.uuid4().hex[:14]}"
            conn.execute(
                "INSERT INTO deletion_requests(request_id, memory_id,"
                " human_reason, status, submitted_by, submitted_local_date,"
                " created_at) VALUES(?,?,?,'pending',?,?,?)",
                (rid, memory_id, reason, principal_id, local_date, _iso()))
            # P1-04（2026-10-05 审计）：申请落库同事务写审计——敏感
            # 生命周期动作（申请/撤回/驳回）此前全部无审计痕迹，
            # 违反 §19"审计与正式写入同一事务"
            audit.record(conn, "deletion.requested", principal_id,
                         resource_id=memory_id,
                         payload={"request_id": rid,
                                  "reason": reason[:200]})
            if operation_key:
                # 同事务存完成回执：同 key 重放返回本申请，不重复计数
                # （CB-008：op: 前缀与 transport 幂等键空间隔离）
                import json as _json
                import hashlib as _hl
                ph = _hl.sha256(_json.dumps(
                    {"memory_id": memory_id, "reason": reason},
                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                conn.execute(
                    "INSERT INTO idempotency_records(principal_id,"
                    " capability, idempotency_key, payload_hash, status,"
                    " result_ref, created_at) VALUES(?,?,?,?,?,?,?)",
                    (principal_id, "memory.deletion.request",
                     f"op:{operation_key}",
                     ph, "completed",
                     _json.dumps({"memory_id": memory_id,
                                  "request_id": rid},
                                 ensure_ascii=False), _iso()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return deletion_get(rid)


def deletion_withdraw(principal_id: str, request_id: str) -> dict:
    """撤回自己的 pending（不返还已提交次数，P-D03）。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM deletion_requests WHERE request_id=?",
                (request_id,)).fetchone()
            if row is None:
                raise NotFound("deletion request not found",
                               request_id=request_id)
            if row["submitted_by"] != principal_id:
                raise Forbidden("只能撤回本人的申请", code="OWNER_MISMATCH")
            if row["status"] != "pending":
                raise AlreadyDecided("只有 pending 申请可撤回",
                                      status=row["status"])
            conn.execute(
                "UPDATE deletion_requests SET status='withdrawn',"
                " decided_at=? WHERE request_id=? AND status='pending'",
                (_iso(), request_id))
            # P1-04（2026-10-05 审计）：撤回同事务留痕
            audit.record(conn, "deletion.withdrawn", principal_id,
                         resource_id=row["memory_id"],
                         payload={"request_id": request_id})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return deletion_get(request_id)


def deletion_decide(principal_id: str, request_id: str, decision: str,
                    rejection_reason: str | None = None,
                    operation_key: str | None = None) -> dict:
    """周家明决定（P-D01）：approve 真删除（无理由要求；关系硬门通过
    才落 approved，被拦回滚保 pending）；reject 必填非空理由。

    裁定（2026-10-04）：破坏性决定的业务变更与 operation 回执同一
    事务（同 deletion_request/RA-019 模式）——同 key 同载荷网络重试
    重放同一决定结果，不重复执行删除。
    """
    if principal_id != "jiaming":
        raise Forbidden("删除决定仅周家明", code="OWNER_MISMATCH")
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise Forbidden("decision must be approve/reject",
                        code="INVALID_ARGUMENT")
    if decision == "reject" and not (rejection_reason or "").strip():
        raise Forbidden("拒绝必须说明理由（P-D01）", code="INVALID_ARGUMENT")
    import json as _json
    import hashlib as _hl
    ph = None
    if operation_key:
        ph = _hl.sha256(_json.dumps(
            {"request_id": request_id, "decision": decision,
             "rejection_reason": rejection_reason},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if operation_key:
                prior = conn.execute(
                    "SELECT payload_hash, result_ref FROM"
                    " idempotency_records WHERE principal_id=? AND"
                    " capability='memory.deletion.decide' AND"
                    " idempotency_key=?",
                    (principal_id, f"op:{operation_key}")).fetchone()
                if prior is not None:
                    if prior["payload_hash"] not in (None, ph):
                        from ..errors import IdempotencyConflict
                        raise IdempotencyConflict(
                            "same operation key with different payload",
                            operation_key=operation_key)
                    try:
                        out = deletion_get(request_id)
                        out["idempotent_replay"] = True
                        conn.execute("COMMIT")
                        return out
                    except NotFound:
                        pass  # 回执指向的申请已不存在：走正常决定路径
            row = conn.execute(
                "SELECT * FROM deletion_requests WHERE request_id=?",
                (request_id,)).fetchone()
            if row is None:
                raise NotFound("deletion request not found",
                               request_id=request_id)
            if row["status"] != "pending":
                raise AlreadyDecided("申请已决定", status=row["status"])
            if decision == "reject":
                conn.execute(
                    "UPDATE deletion_requests SET status='rejected',"
                    " rejection_reason=?, decided_at=? WHERE request_id=?",
                    (rejection_reason.strip(), _iso(), request_id))
                # P1-04（2026-10-05 审计）：驳回同事务留痕——此前只有
                # approve 侧经 memory.deleted 间接可见，拒绝这一敏感
                # 决策在审计流水里查不到
                audit.record(conn, "deletion.rejected", principal_id,
                             resource_id=row["memory_id"],
                             payload={"request_id": request_id,
                                      "reason": rejection_reason[:200]})
            else:
                # 技术拦截（关系硬门）不是主观拒绝：抛出→回滚→保 pending
                _execute_delete(conn, row["memory_id"], principal_id)
                conn.execute(
                    "UPDATE deletion_requests SET status='approved',"
                    " decided_at=? WHERE request_id=?",
                    (_iso(), request_id))
            if operation_key:
                # 决定与回执同事务：崩溃/重试后同 key 恢复同一结果
                conn.execute(
                    "INSERT INTO idempotency_records(principal_id,"
                    " capability, idempotency_key, payload_hash, status,"
                    " result_ref, created_at) VALUES(?,?,?,?,?,?,?)",
                    (principal_id, "memory.deletion.decide",
                     f"op:{operation_key}", ph, "completed",
                     _json.dumps({"request_id": request_id},
                                 ensure_ascii=False), _iso()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return deletion_get(request_id)


# ---------------------------------------------------------------- 机器直删

def direct_delete(principal_id: str, memory_id: str, conn=None) -> dict:
    """周家明直删（P-D02）：无申请/理由/配额/审批审计。

    关系硬门同样生效（P-D04）；pending 人类申请置 superseded（客观
    状态，不伪造拒绝理由）；既有拒绝决定原样保留。conn 由 atomic_write
    注入（幂等回执与删除同事务，§8.2）。
    """
    if principal_id != "jiaming":
        raise Forbidden("直删仅周家明（认证 jiaming 主体）",
                        code="OWNER_MISMATCH")

    def _do(conn):
        if not conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?",
                (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        _execute_delete(conn, memory_id, principal_id)
        conn.execute(
            "UPDATE deletion_requests SET status='superseded',"
            " decided_at=? WHERE memory_id=? AND status='pending'",
            (_iso(), memory_id))
        return {"memory_id": memory_id, "deleted": True, "path": "direct"}

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


# ---------------------------------------------------------------- 读取

def deletion_get(request_id: str) -> dict:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM deletion_requests WHERE request_id=?",
            (request_id,)).fetchone()
    if row is None:
        raise NotFound("deletion request not found", request_id=request_id)
    return dict(row)


def deletion_list(status: str | None = None,
                  memory_id: str | None = None) -> list[dict]:
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if memory_id:
        where.append("memory_id=?")
        params.append(memory_id)
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    with db.formal() as conn:
        rows = conn.execute(
            f"SELECT * FROM deletion_requests {cond}"
            " ORDER BY created_at DESC LIMIT 200", params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 兼容垫片

def deletion_submit(principal_id: str, resource_id: str, reason: str,
                    action: str = "delete", resource_kind: str = "memory",
                    operation_key: str | None = None) -> dict:
    """旧签名垫片：忽略已退役的 action/resource_kind（§3.1.3）。"""
    return deletion_request(principal_id, resource_id, reason,
                            operation_key=operation_key)
