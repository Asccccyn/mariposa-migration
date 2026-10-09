"""运维能力：outbox 消费、活动查询、提醒到期（§16.1/§19.2/§17.3）。"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from ..errors import Forbidden

#: P2-01（2026-10-05 审计）：幂等对账新鲜度下限——低于该时长的 running
#: 记录一律拒绝判 failed（并发中的真实执行最常见，见函数注释）
MIN_RECONCILE_STALE_S = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def outbox_drain(limit: int = 100) -> dict:
    """至少一次投递 + 幂等消费的第一版消费者：逐条标记 processed。

    异步下游接入点在此注册。WP-05（D05a）：events_outbox 已停写且
    无注册消费者——drain 显式返回 no_consumers_registered，历史行
    只读不动（audit_events 是单一正本）。
    """
    with db.formal() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM events_outbox WHERE processed=0"
        ).fetchone()["c"]
    return {"drained": 0, "no_consumers_registered": True,
            "historical_pending_readonly": pending}


def outbox_status() -> dict:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS pending FROM events_outbox WHERE processed=0"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) AS t FROM events_outbox").fetchone()["t"]
    return {"pending": row["pending"], "total": total}


def activity_list(limit: int = 50, event_type: str | None = None) -> list[dict]:
    """审计查询（管理接口；不参与记忆召回，§19.3）。"""
    with db.formal() as conn:
        if event_type:
            rows = conn.execute(
                "SELECT event_id, event_type, occurred_at, actor_principal,"
                " entry_source, resource_id, resource_version FROM audit_events"
                " WHERE event_type=? ORDER BY occurred_at DESC LIMIT ?",
                (event_type, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT event_id, event_type, occurred_at, actor_principal,"
                " entry_source, resource_id, resource_version FROM audit_events"
                " ORDER BY occurred_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]





def idempotency_reconcile(principal_id: str, record_principal: str,
                          capability: str, idempotency_key: str,
                          stale_seconds: int = 60) -> dict:
    """崩溃窗口对账（§13.2）：把疑似中途崩溃的 running 幂等记录显式标记 failed。

    只有 failed 之后同 key 重试才能重新占位执行。调用方必须先核实业务结果
    （副作用可能已发生）；本工具只清除占位，不伪造结果。

    P2-01（2026-10-05 审计）：新鲜度下限服务端钉死——此前 stale_seconds
    完全由调用方给（schema 最小 1），把一条真实并发中的写占位判成
    failed 即可让同 key 重执行双写副作用；跨主体对账 + 无审计同理
    收口：对账动作与 UPDATE 同事务落 audit。
    """
    from ..errors import NotFound as _NF
    from datetime import datetime as _dt
    from ..capabilities import registry as _reg
    from ..audit import service as _audit
    stale_seconds = max(int(stale_seconds or 0), MIN_RECONCILE_STALE_S)
    with db.formal() as conn:
        # RA-004 后 transport 幂等记录统一存 t: 前缀键；对账入口收
        # 的是调用方原始 key，必须先映射再查（裸键回退仅服务迁移前
        # 旧行）。否则真实崩溃留下的 t: running 记录永远无法经公开
        # 入口对账清除（审计 2026-10-03：红测 fixture 种裸键掩盖了
        # 这一断层）
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE principal_id=? AND"
            " capability=? AND idempotency_key=?",
            (record_principal, capability,
             _reg._transport_key(idempotency_key))).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM idempotency_records WHERE principal_id=? AND"
                " capability=? AND idempotency_key=?",
                (record_principal, capability, idempotency_key)).fetchone()
        if row is None:
            raise _NF("idempotency record not found",
                      principal=record_principal, capability=capability,
                      key=idempotency_key)
        if row["status"] != "running":
            return {"reconciled": False, "status": row["status"],
                    "note": "record is not running; nothing to reconcile"}
        created = _dt.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age <= stale_seconds:
            return {"reconciled": False, "status": "running",
                    "age_seconds": int(age),
                    "note": "record still fresh; concurrent execution may be"
                            " in flight; refuse to reconcile"}
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE idempotency_records SET status='failed', result_ref=?"
                " WHERE principal_id=? AND capability=? AND idempotency_key=?"
                " AND status='running'",
                (None, record_principal, capability,
                 row["idempotency_key"]))
            if cur.rowcount:
                _audit.record(conn, "maintenance.idempotency.reconciled",
                              principal_id,
                              resource_id=f"{record_principal}:{capability}"
                                          f":{row['idempotency_key']}",
                              payload={"age_seconds": int(age),
                                       "stale_floor": MIN_RECONCILE_STALE_S})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"reconciled": True, "status": "failed",
            "age_seconds": int(age),
            "reconciled_by": principal_id,
            "note": "占位已清除；副作用是否已发生须由调用方核实业务状态，"
                    "确认后同 key 重试将重新执行"}


def jobs_status() -> dict:
    """维护任务状态总览：outbox 待处理、导入任务。

    CB-051（2026-10-02 审计 P2）：Workspace Tasks/Lease 已退役——
    不再查询已删除的 workspace_task_leases/work_items（此前该状态页
    在 fresh schema 上直接 no such table 崩溃）。"""
    with db.formal() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM events_outbox WHERE processed=0"
        ).fetchone()["c"]
        imports = conn.execute(
            "SELECT status, COUNT(*) AS c FROM import_jobs GROUP BY status"
        ).fetchall()
    return {"outbox_pending": pending,
            "import_jobs": {r["status"]: r["c"] for r in imports}}
