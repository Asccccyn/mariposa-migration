"""传输幂等层（从 registry.py 拆出——2026-10-04 深耦合拆分批）。

职责单一：write 能力的 transport 幂等（原子 claim、终态等待、崩溃
恢复、request_ref 派生键、领域回据恢复面）。registry 保留注册与
分发；本模块不 import registry（避免环），依赖经参数/晚绑定传入。

兼容：registry 仍重导出本模块公共名（_transport_key/_payload_hash/
_DOMAIN_IDEMPOTENT_CAPS 等），既有调用方与测试不改。
"""
from __future__ import annotations

import json
import time as _time

from .. import db
from ..errors import (Busy, Forbidden, IdempotencyConflict,
                      MariposaError, NotFound, OutcomeUnknown,
                      StaleOperation)


from ..identity import Principal
from ..memory import service as memory
from ..recall import store as recall_store


def _payload_hash(arguments: dict) -> str:
    return memory.canonical_hash(arguments)


_IDEMPOTENCY_STALE_SECONDS = 60


def _transport_key(key: str) -> str:
    """RA-004（2026-10-02 复审 P1）：transport 幂等键统一无歧义编码。
    领域层 operation 键为 op:<key>；此前本层存裸字符串，客户端
    transport="op:k" 会与 body operation_id=k 的领域记录碰撞。两类
    键分别固定 t:/op: 前缀，任何输入字符串都不产生跨层相等。"""
    return f"t:{key}"


def _claim_idempotency(principal_id: str, capability: str, key: str,
                       payload_hash: str) -> bool:
    """认领语义：新记录插入即占位；对账后 failed 的记录可被原子转移回
    running 重新执行；completed/running 一律不占。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO idempotency_records(principal_id,"
                " capability, idempotency_key, payload_hash, status,"
                " result_ref, created_at)"
                " VALUES(?,?,?,?, 'running', NULL, datetime('now'))"
                " ON CONFLICT(principal_id, capability, idempotency_key)"
                " DO UPDATE SET status='running',"
                " payload_hash=excluded.payload_hash, result_ref=NULL,"
                " created_at=excluded.created_at"
                " WHERE idempotency_records.status='failed'",
                (principal_id, capability, key, payload_hash))
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _read_idempotency(principal_id: str, capability: str, key: str):
    with db.formal() as conn:
        return conn.execute(
            "SELECT * FROM idempotency_records WHERE principal_id=? AND"
            " capability=? AND idempotency_key=?",
            (principal_id, capability, key)).fetchone()


def _idempotency_stale(row) -> bool:
    from datetime import datetime as _dt, timezone as _tz
    try:
        created = _dt.fromisoformat(row["created_at"])
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=_tz.utc)
    return (_dt.now(_tz.utc) - created).total_seconds() > _IDEMPOTENCY_STALE_SECONDS


def _idempotent_invoke(principal: Principal, cap: Capability, arguments: dict,
                       key: str) -> dict:
    """原子 claim 幂等：先 INSERT 占位（status=running），占位成功者才执行副作用。

    并发同 key：仅一方能占位；另一方有界等待后读终态重放。
    崩溃窗口（§13.2）：running 残留超过阈值时不盲删盲重放——副作用是否
    已发生不明，返回 OUTCOME_UNKNOWN，由显式对账（maintenance.idempotency.
    reconcile）核实业务结果后才能放行重试。
    """
    ph = _payload_hash(arguments)
    tkey = _transport_key(key)
    if not _claim_idempotency(principal.principal_id, cap.name, tkey, ph):
        replay = _await_completion(principal, cap, tkey, ph, arguments)
        if replay is not None:
            return {"ok": True, "data": replay, "idempotent_replay": True}

    try:
        result = cap.handler(principal, arguments)
    except Exception:
        # CB-008：handler 异常（业务拒绝 MariposaError 或未预期错误）
        # 一律把本层占位置 failed 再抛——副作用在各服务事务内已回滚，
        # 残留 running 会把干净失败伪装成 OUTCOME_UNKNOWN 逼人工对账；
        # 真正的进程崩溃不经此处，由 stale→OUTCOME_UNKNOWN 兜底。
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE idempotency_records SET status='failed'"
                    " WHERE principal_id=? AND capability=? AND"
                    " idempotency_key=? AND status='running'",
                    (principal.principal_id, cap.name, tkey))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        raise
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE idempotency_records SET status='completed', result_ref=?"
                " WHERE principal_id=? AND capability=? AND idempotency_key=?"
                " AND status='running'",
                (json.dumps(result, ensure_ascii=False), principal.principal_id,
                 cap.name, tkey))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"ok": True, "data": result}


def _revalidate_replayed_response(principal: Principal, capability: str,
                                  replay: dict) -> None:
    """CB-016（2026-10-02 审计 P1）：幂等缓存的重放前资源重验。

    write 回执缓存的是完整响应——写幂等的"同 key 同结果"不等于
    "当前资源仍处于该状态"。memory.open 的缓存响应带完整正文与票据：
    memory 已更新/物理删除、票据行已消失或归属他人时，重放必须返回
    结构化 stale，不得重发旧正文或已失效票据（物理删除 ≠ 出站不可
    再取）。其他能力按需登记。
    """
    if capability != "memory.open":
        return
    mid = replay.get("memory_id")
    if not mid:
        return
    with db.formal() as conn:
        m = conn.execute(
            "SELECT visibility, current_version_no FROM memories"
            " WHERE memory_id=?", (mid,)).fetchone()
        rid = replay.get("view_receipt")
        r = (conn.execute(
            "SELECT principal_id, binding_id FROM memory_view_receipts"
            " WHERE receipt_id=?", (rid,)).fetchone() if rid else None)
    if m is None or m["visibility"] != "active":
        raise StaleOperation(
            "memory 已删除/不可见，旧 open 响应拒绝重放", memory_id=mid)
    if str(replay.get("version")) != str(m["current_version_no"]):
        raise StaleOperation(
            "memory 已更新到新版本，旧 open 响应拒绝重放",
            memory_id=mid, current_version=m["current_version_no"])
    if (r is None or r["principal_id"] != principal.principal_id
            or r["binding_id"] != principal.binding_id):
        raise StaleOperation(
            "查看票据已失效或不属于当前身份，旧 open 响应拒绝重放",
            memory_id=mid)


#: 走 relations.corrections.atomic_write 的能力（领域回执键
#: op:<operation_id>，与业务副作用同事务）——F23 崩溃窗口恢复面
#: MEM-05（2026-10-04 二批）：补齐全部带领域回执的纠错/删除入口——
#: word 来源纠错（atomic_write）、直删（atomic_write）、删除申请
#: （RA-019 op: 回执）、删除决定（2026-10-04 同事务回执）
_DOMAIN_IDEMPOTENT_CAPS = frozenset({
    "memory.relations.correct", "i.item.relations.correct",
    "source.binding.correct", "memory.our_words.source.correct",
    "memory.delete", "memory.deletion.request",
    "memory.deletion.decide",
    # RE-MEM-01（2026-10-04 复审）：plan 链接纠错同走 atomic_write
    "plan.memory.correct",
    # WP2（迁移 31，2026-10-05）：宿主自动化 hold 带 operation_id 时走
    # atomic_write——业务提交与完成回执同事务，t 层崩溃按领域回执恢复
    "memory.hold"})

#: MEM-07（2026-10-04 二批）：各能力的领域归一化 payload 构造——
#: 崩溃恢复必须比对领域 payload 身份，不得把另一项操作的结果缓存
#: 为本请求完成。与各 handler 传给 atomic_write/deletion 的 payload
#: 字段保持一致（改 handler 时同步改这里）。
_DOMAIN_PAYLOAD_BUILDERS = {
    "memory.relations.correct": lambda a: {
        "relation_id": a.get("relation_id"),
        "correction_action": a.get("correction_action"),
        "replacement": a.get("replacement"), "note": a.get("note")},
    "i.item.relations.correct": lambda a: {
        "relation_id": a.get("relation_id"),
        "correction_action": a.get("correction_action"),
        "replacement": a.get("replacement"), "note": a.get("note")},
    "source.binding.correct": lambda a: {
        "binding_id": a.get("binding_id"),
        "correction_action": a.get("correction_action"),
        "replacement": a.get("replacement"), "note": a.get("note")},
    "memory.our_words.source.correct": lambda a: {
        "word_id": a.get("word_id"),
        "expected_source_ref": a.get("expected_source_ref"),
        "expected_source_version": a.get("expected_source_version"),
        "correction_action": a.get("correction_action"),
        "replacement": a.get("replacement"), "note": a.get("note")},
    "memory.delete": lambda a: {"memory_id": a.get("memory_id")},
    # RE-MEM-03（2026-10-04 复审）：与领域侧同一归一化——request
    # 的 reason strip、decide 的 decision strip().lower()，否则同
    # header/body 的 crash 重试会被误报幂等冲突
    "memory.deletion.request": lambda a: {
        "memory_id": a.get("memory_id"),
        "reason": (a.get("reason") or "").strip()},
    "memory.deletion.decide": lambda a: {
        "request_id": a.get("request_id"),
        "decision": (a.get("decision") or "").strip().lower(),
        "rejection_reason": a.get("rejection_reason")},
    "plan.memory.correct": lambda a: {
        "link_id": a.get("link_id"),
        "correction_action": a.get("correction_action"),
        "replacement": a.get("replacement"), "note": a.get("note")},
    # WP2：与 registry._hold 传给 atomic_write 的 payload 逐字段一致
    # （改 handler 时同步改这里——MEM-07 同款纪律）。operation_id 是
    # 键本身，不进 payload
    "memory.hold": lambda a: hold_domain_payload(a),
}


def hold_domain_payload(a: dict) -> dict:
    """memory.hold 的领域归一化载荷（handler 与传输恢复共用一份）。"""
    return {
        "text": a.get("text"),
        "memory_date": a.get("memory_date"),
        "date_confidence": a.get("date_confidence", "unknown"),
        "raw_pending": bool(a.get("raw_pending", True)),
        "original_title": a.get("original_title"),
        "categories": a.get("categories"),
        "plan_ids": a.get("plan_ids"),
        "mood": a.get("mood"),
        "our_words": a.get("our_words"),
        "creation_mode": a.get("creation_mode"),
        "occurred_start": a.get("occurred_start"),
        "occurred_end": a.get("occurred_end"),
        "source_selections": a.get("source_selections"),
    }


def _recover_transport_from_domain(principal: Principal, cap: Capability,
                                   tkey: str, arguments: dict):
    """t 层 running 残留时按领域回执恢复外层（F23）。

    返回 (verdict, result)：completed=业务已落地（外层补 completed
    并重放领域结果）；not_executed=领域零痕迹（外层转 failed 放行
    重试）；None=不可判定（body 无 operation_id 或领域侧同在
    running）。不放松未知结果保护——非 atomic_write 能力不走此路。
    """
    op = arguments.get("operation_id")
    if not isinstance(op, str) or not op:
        return None, None
    # MEM-07：领域归一化 payload 身份——与领域侧回执的 payload_hash
    # 同一口径（json 排序 sha256），不一致即另一项操作，判冲突而非
    # 把它的结果缓存为本请求完成
    import hashlib as _hl
    import json as _json
    _builder = _DOMAIN_PAYLOAD_BUILDERS.get(cap.name)
    _ph = None
    if _builder is not None:
        _ph = _hl.sha256(_json.dumps(
            _builder(arguments), ensure_ascii=False, sort_keys=True,
            default=str).encode()).hexdigest()
    with db.formal() as conn:
        row = conn.execute(
            "SELECT status, result_ref, payload_hash FROM"
            " idempotency_records WHERE"
            " principal_id=? AND capability=? AND idempotency_key=?",
            (principal.principal_id, cap.name, f"op:{op}")).fetchone()
        if row is not None and _ph is not None and \
                row["payload_hash"] not in (None, _ph):
            from ..errors import IdempotencyConflict
            raise IdempotencyConflict(
                "领域回执属于另一次不同内容的操作（payload 身份不"
                "符）", operation_key=op)
        if row is not None and row["status"] == "completed":
            try:
                result = json.loads(row["result_ref"])
            except (ValueError, TypeError):
                return None, None
            # RE-MEM-02（2026-10-04 复审）：删除申请/决定的领域回执
            # 只存指针——按 request_id 重建正常结果字段
            # （deletion_get），不把裸指针当完成结果回放
            if cap.name in ("memory.deletion.request",
                            "memory.deletion.decide") and \
                    isinstance(result, dict) and result.get("request_id"):
                from ..deletion import service as _del_svc
                try:
                    result = _del_svc.deletion_get(result["request_id"])
                except Exception:
                    return None, None
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE idempotency_records SET status='completed',"
                    " result_ref=? WHERE principal_id=? AND capability=?"
                    " AND idempotency_key=? AND status='running'",
                    (json.dumps(result, ensure_ascii=False),
                     principal.principal_id, cap.name, tkey))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return "completed", result
        if row is None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE idempotency_records SET status='failed',"
                    " result_ref=NULL WHERE principal_id=? AND"
                    " capability=? AND idempotency_key=? AND"
                    " status='running'",
                    (principal.principal_id, cap.name, tkey))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return "not_executed", None
    return None, None


def _await_completion(principal: Principal, cap: Capability, key: str,
                      ph: str, arguments: dict | None = None) -> dict | None:
    """占位失败方：等待占位方终态。

    返回 dict = 已完成（调用方按幂等重放返回）；None = 记录消失或已由
    本方重新认领（调用方继续执行 handler）。异常：同键异内容冲突 /
    崩溃窗口 OUTCOME_UNKNOWN / 仍在进行 Busy。
    """
    import time as _time
    for _ in range(40):  # <=8s
        _time.sleep(0.2)
        row = _read_idempotency(principal.principal_id, cap.name, key)
        if row is None:
            break  # 记录消失，可重试 claim
        if row["payload_hash"] != ph:
            raise IdempotencyConflict(
                "same key with different payload",
                capability=cap.name, key=key)
        if row["status"] == "completed":
            replay = json.loads(row["result_ref"])
            # CB-016：重放的完整响应先过资源重验，失效结构化拒绝
            _revalidate_replayed_response(principal, cap.name, replay)
            return replay
    row = _read_idempotency(principal.principal_id, cap.name, key)
    if row is not None and row["status"] == "running":
        if _idempotency_stale(row):
            # F23（2026-10-03 审计 P2）：atomic_write 能力的崩溃窗口按
            # 领域回执恢复外层——领域记录与业务副作用同事务，completed
            # 即已落地（补写外层并重放同一结果），无记录即零痕迹（转
            # failed 放行重试）；不可判定才维持 OUTCOME_UNKNOWN
            if cap.name in _DOMAIN_IDEMPOTENT_CAPS:
                verdict, recovered = _recover_transport_from_domain(
                    principal, cap, key, arguments or {})
                if verdict == "completed":
                    return recovered
                if verdict == "not_executed":
                    if _claim_idempotency(principal.principal_id, cap.name,
                                          key, ph):
                        return None
            # 疑似崩溃残留：不盲目重放副作用（B02 崩溃窗口）
            raise OutcomeUnknown(
                "idempotent execution likely crashed mid-flight; "
                "verify business outcome and reconcile before retrying",
                capability=cap.name, key=key)
        raise Busy()
    if not _claim_idempotency(principal.principal_id, cap.name, key, ph):
        raise Busy()
    return None





# ---------- handlers：纯领域逻辑，不做传输层判断 ----------


def _with_operation_id(principal: Principal, a: dict, fn) -> dict:
    """RUNTIME-02：同 operation_id 重试不重复建 session/扣预算/排除候选。

    commit-at-end 模型：不写任何中间态；已完成 operation 直接重放
    （重放经 recall 域 guard 按当前 session/版本/可见性/phase 重校验，
    不原样返回旧正文——审计 F07）；未完成的同 key 重试从头重新计算。
    payload_hash 区分同 key 异请求。设为型动作（reject/accept/navigate/
    close）由 run_operation 在执行成功后补写响应记录（重复执行结果
    不变，崩溃窗口重放安全）。
    """
    op = a.get("operation_id")
    # 裁定（2026-10-04 江乔生，措辞修正二）：request_ref 在场时，
    # 幂等身份 = 主体 + capability/action + request_ref——operation_id
    # 只是 transport 回执身份、session_id 只是路由参数，改变任一都
    # 不得绕开同一逻辑请求的冲突检测（RECALL-01）。
    _plan = a.get("query_plan")
    _req_ref = a.get("request_ref")
    if not _req_ref and isinstance(_plan, dict):
        _req_ref = _plan.get("request_ref")
    from_req_ref = False
    if isinstance(_req_ref, str) and _req_ref.strip():
        op = f"reqref:{_req_ref.strip()}"
        from_req_ref = True
    if not op:
        return fn(principal, a)
    if from_req_ref:
        # request_ref 定义的逻辑身份不含 session_id——跨 session 的
        # 同 ref 重试必须回到原 operation（start 回原 session）
        key = f"{fn.__name__}:::{op}"
        # 逻辑载荷同样不含传输/路由字段：operation_id/session_id 变化
        # 不构成不同 payload（RECALL-01：不得借改字段绕开身份）
        _logical = {k: v for k, v in a.items()
                    if k not in ("operation_id", "session_id")}
        ph = memory.canonical_hash(_logical)
    else:
        key = f"{fn.__name__}:{a.get('session_id', 'new')}:{op}"
        ph = memory.canonical_hash(a)
    ctx = {"principal_id": principal.principal_id, "operation_key": key,
           "payload_hash": ph}
    try:
        from ..recall import service as recall_service  # 晚绑定防环
        return recall_store.run_operation(
            principal.principal_id, key,
            lambda: fn(principal, a, op_ctx=ctx),
            payload_hash=ph,
            replay_guard=lambda saved: recall_service.revalidate_replayed(
                fn.__name__, saved, a))
    except IdempotencyConflict:
        if from_req_ref:
            raise Forbidden(
                "request_ref 已绑定另一次不同内容的请求"
                "（REF_REUSE_MISMATCH）：一个 request_ref 只代表一次"
                "具体请求", code="REF_REUSE_MISMATCH",
                request_ref=_req_ref) from None
        raise


