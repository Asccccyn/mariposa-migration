"""surface 运行库读写：幂等作业、快照、回执、详情分页、判断台账。

只存有界运行数据（v1.4 §10.2 同 recall 运行库定位）：不保存长期
记忆正文正本；网络调用期间不持有写锁（表内容含对话/记忆投影，
保护级别同聊天库——不进普通日志/Git）。
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, db
from ..errors import IdempotencyConflict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _day_bounds() -> tuple[str, str]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start.astimezone(timezone.utc).isoformat(),
            (start + timedelta(days=1)).astimezone(timezone.utc).isoformat())


def _expires(seconds: int) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=max(0, int(seconds)))).isoformat()


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


# --------------------------------------------------------------- 幂等作业

def claim_operation(principal_id: str, operation_key: str,
                    phash: str | None) -> dict | None:
    """阶段 A：同 op 已有完成/未知结果则返回之（重放不重跑付费 judge）。

    返回 None = 本事务应继续执行。payload 不一致 → SURFACE_IDEMPOTENCY_
    CONFLICT（结构化拒绝，不静默重跑）。
    """
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM surface_operations WHERE principal_id=?"
                " AND operation_key=?",
                (principal_id, operation_key)).fetchone()
            if row is not None:
                if phash is not None and row["payload_hash"] not in (
                        None, phash):
                    raise IdempotencyConflict(
                        "same operation key with different payload",
                        operation_key=operation_key)
                out = dict(row)
                if out.get("result_json"):
                    out["result"] = json.loads(out["result_json"])
                return out
            conn.execute(
                "INSERT INTO surface_operations(principal_id, operation_key,"
                " payload_hash, status, result_json, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (principal_id, operation_key, phash, "running", None,
                 _now(), _now()))
            conn.execute("COMMIT")
            return None
        except IdempotencyConflict:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_unknown(principal_id: str, operation_key: str) -> None:
    """付费已出站而结果未知：保留预占、标 unknown，不假装未执行。"""
    with db.recall_runtime() as conn:
        conn.execute(
            "UPDATE surface_operations SET status='unknown', updated_at=?"
            " WHERE principal_id=? AND operation_key=?",
            (_now(), principal_id, operation_key))


def complete_operation(principal_id: str, operation_key: str,
                       result: dict) -> None:
    now = _now()
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT INTO surface_operations(principal_id, operation_key,"
            " payload_hash, status, result_json, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(principal_id, operation_key) DO UPDATE SET"
            " status='completed', result_json=excluded.result_json,"
            " updated_at=excluded.updated_at",
            (principal_id, operation_key, None, "completed",
             json.dumps(result, ensure_ascii=False), now, now))


# --------------------------------------------------------------- prepare 快照

def new_surface_run(operation_key: str, conversation_scope: str,
                    status: str, snapshot: dict, coverage: dict,
                    policy_revision: int, prompt_revision: str,
                    reason_code: str | None = None) -> str:
    run_id = "sfr_" + uuid.uuid4().hex
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT INTO surface_runs(surface_run_id, operation_key,"
            " conversation_scope, status, snapshot_json, coverage_json,"
            " policy_revision, prompt_revision, reason_code, created_at,"
            " expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, operation_key, conversation_scope, status,
             json.dumps(snapshot, ensure_ascii=False),
             json.dumps(coverage, ensure_ascii=False),
             policy_revision, prompt_revision, reason_code, _now(),
             _expires(config.SURFACE_CANDIDATE_TTL_SECONDS)))
    return run_id


def load_surface_run(surface_run_id: str) -> dict | None:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM surface_runs WHERE surface_run_id=?",
            (surface_run_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["snapshot"] = json.loads(out.pop("snapshot_json"))
    out["coverage"] = json.loads(out.pop("coverage_json"))
    return out


# --------------------------------------------------------------- 验证回执

def save_validation_receipt(surface_run_id: str, conversation_scope: str,
                            target_message_id: str, target_revision: int,
                            window_id: str, content_hash: str,
                            items: list[dict], policy_revision: int,
                            status: str, ttl_seconds: int) -> str:
    receipt_id = "svr_" + secrets.token_hex(16)
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT INTO surface_validation_receipts(receipt_id,"
            " surface_run_id, conversation_scope, target_message_id,"
            " target_revision, window_id, content_hash, items_json,"
            " policy_revision, status, as_of, expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, surface_run_id, conversation_scope,
             target_message_id, target_revision, window_id, content_hash,
             json.dumps(items, ensure_ascii=False), policy_revision,
             status, _now(), _expires(ttl_seconds)))
    return receipt_id


def load_validation_receipt(receipt_id: str) -> dict | None:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM surface_validation_receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["items"] = json.loads(out.pop("items_json"))
    return out


def consume_validation_receipt(receipt_id: str, status: str) -> None:
    """回执一次性消费语义：consumed/missed 后不再可用（check 复核时改写）。"""
    with db.recall_runtime() as conn:
        conn.execute(
            "UPDATE surface_validation_receipts SET status=?, as_of=?"
            " WHERE receipt_id=?", (status, _now(), receipt_id))


# --------------------------------------------------------------- 详情快照

def save_detail_snapshot(detail_ref: str, memory_id: str,
                         memory_version: str, domain_versions: dict,
                         conversation_scope: str, principal_id: str,
                         surfaced_version: str | None,
                         changed_since_surface: bool, pages: list[dict],
                         section_status: dict, complete: bool) -> str:
    snapshot_id = "sds_" + uuid.uuid4().hex
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT INTO surface_detail_snapshots(snapshot_id, detail_ref,"
            " memory_id, memory_version, domain_versions_json,"
            " conversation_scope, principal_id, surfaced_version,"
            " changed_since_surface, pages_json, section_status_json,"
            " complete, created_at, expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, detail_ref, memory_id, memory_version,
             json.dumps(domain_versions, ensure_ascii=False),
             conversation_scope, principal_id, surfaced_version,
             1 if changed_since_surface else 0,
             json.dumps(pages, ensure_ascii=False),
             json.dumps(section_status, ensure_ascii=False),
             1 if complete else 0, _now(),
             _expires(config.SURFACE_CANDIDATE_TTL_SECONDS)))
    return snapshot_id


def load_detail_snapshot_by_ref(detail_ref: str) -> dict | None:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM surface_detail_snapshots WHERE detail_ref=?"
            " ORDER BY created_at DESC LIMIT 1",
            (detail_ref,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["domain_versions"] = json.loads(out.pop("domain_versions_json"))
    out["pages"] = json.loads(out.pop("pages_json"))
    out["section_status"] = json.loads(out.pop("section_status_json"))
    out["changed_since_surface"] = bool(out.pop("changed_since_surface"))
    out["complete"] = bool(out.pop("complete"))
    return out


# --------------------------------------------------------------- 判断台账

def reserve_judge_request(operation_key: str, phase: str, provider: str,
                          requested_model: str, policy: dict) -> int:
    """Atomic admission immediately before each HTTP attempt; never send first.

    A full default-context reservation bounds cost without guessing tokenization.
    Unknown outcomes retain it. Legacy unknown-cost rows block further spending
    for their business day until reconciled; history itself is not rewritten.
    """
    from .models import SurfaceBlocked
    price = config.SURFACE_JEV_PRICE_SNAPSHOT
    model = price["aliases"].get(requested_model, requested_model)
    rate = price["models"].get(model)
    if provider != "typesafe_jev" or rate is None:
        raise SurfaceBlocked("SURFACE_PRICE_UNAVAILABLE")
    cap = policy.get("daily_judge_cost_cap_usd")
    if cap is None or not math.isfinite(float(cap)) or float(cap) <= 0:
        raise SurfaceBlocked("SURFACE_BUDGET_UNCONFIGURED")
    reserve = int(price["max_input_tokens"]) * float(rate) / 1_000_000
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            day_start, day_end = _day_bounds()
            row = conn.execute(
                "SELECT COALESCE(SUM(request_count),0) n, COALESCE(SUM(est_cost_usd),0) cost,"
                " SUM(CASE WHEN request_count>0 AND est_cost_usd IS NULL THEN 1 ELSE 0 END) unknown"
                " FROM surface_judge_ledger WHERE created_at>=? AND created_at<?",
                (day_start, day_end)).fetchone()
            if row["unknown"]:
                raise SurfaceBlocked("SURFACE_COST_UNKNOWN")
            if (int(row["n"]) >= int(policy["daily_judge_request_cap"])
                    or float(row["cost"]) + reserve > float(cap) + 1e-12):
                raise SurfaceBlocked("SURFACE_BUDGET_EXHAUSTED")
            # An alias resolving outside the priced set is not silently allowed
            # to continue spending. Updating the public snapshot can clear this.
            drifted = conn.execute("SELECT resolved_model FROM surface_judge_ledger"
                " WHERE requested_model=? AND cost_basis='unknown_model'", (requested_model,)).fetchall()
            if any(r["resolved_model"] not in price["models"] for r in drifted):
                raise SurfaceBlocked("SURFACE_PRICE_UNAVAILABLE")
            cur = conn.execute("INSERT INTO surface_judge_ledger(operation_key,phase,provider,request_count,"
                "est_cost_usd,status,day,created_at,requested_model,reserved_cost_usd,cost_basis)"
                " VALUES(?,?,?,1,?,'reserved',?,?,?,?,?)",
                (operation_key,phase,provider,reserve,_day(),_now(),requested_model,reserve,
                 price["revision"] + ":context_ceiling_reservation"))
            conn.execute("COMMIT")
            return int(cur.lastrowid)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def settle_judge_request(ledger_id: int, response: dict | None, *, offline=False) -> bool:
    """Use official price × returned input usage as an estimate, never a bill.

    False means unknown model/invalid usage; the original reservation survives.
    Output is free for the explicitly priced models. Fake calls are marked as
    offline and retain a simulated bound rather than fabricate supplier usage.
    """
    price = config.SURFACE_JEV_PRICE_SNAPSHOT
    body = response if isinstance(response, dict) else {}
    model = body.get("model")
    usage = body.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    rate = price["models"].get(model) if isinstance(model, str) else None
    valid = rate is not None and type(tokens) is int and 0 <= tokens <= price["max_input_tokens"]
    with db.recall_runtime() as conn:
        row = conn.execute("SELECT status FROM surface_judge_ledger WHERE ledger_id=?", (ledger_id,)).fetchone()
        if row is None or row["status"] != "reserved":
            return valid
        if valid and not offline:
            conn.execute("UPDATE surface_judge_ledger SET est_cost_usd=?,status='completed',"
                         "resolved_model=?,input_tokens=?,cost_basis=? WHERE ledger_id=? AND status='reserved'",
                         (tokens * rate / 1_000_000,model,tokens,price["revision"] + ":usage_estimate",ledger_id))
        else:
            basis = "offline_transport" if offline else "unknown_model" if model and rate is None else "unknown_outcome_or_usage"
            conn.execute("UPDATE surface_judge_ledger SET status='unknown',resolved_model=?,cost_basis=?"
                         " WHERE ledger_id=? AND status='reserved'", (model,basis,ledger_id))
    return valid

def record_judge_requests(operation_key: str, phase: str, provider: str,
                          request_count: int, est_cost_usd: float | None,
                          status: str) -> None:
    """阶段收口；已有逐 HTTP 预占时只收未知状态，绝不再加请求计数。

    零请求或旧调用路径仍写阶段摘要；这里只记 Judge，不混主模型阅读。
    """
    with db.recall_runtime() as conn:
        reserved = conn.execute("SELECT COUNT(*) n FROM surface_judge_ledger WHERE operation_key=?"
                                " AND phase=? AND reserved_cost_usd IS NOT NULL", (operation_key,phase)).fetchone()["n"]
        if reserved:
            # Per-attempt reservations already account for requests. A stage
            # summary must not add them again or release an unknown outcome.
            conn.execute("UPDATE surface_judge_ledger SET status='unknown',cost_basis='unknown_outcome_or_usage'"
                         " WHERE operation_key=? AND phase=? AND status='reserved'", (operation_key,phase))
            return
        conn.execute(
            "INSERT INTO surface_judge_ledger(operation_key, phase,"
            " provider, request_count, est_cost_usd, status, day,"
            " created_at) VALUES(?,?,?,?,?,?,?,?)",
            (operation_key, phase, provider, int(request_count),
             est_cost_usd, status, _day(), _now()))


def judge_usage_today() -> dict:
    """当日累计（请求计数对账用；费用估算缺失时如实施报 None）。"""
    with db.recall_runtime() as conn:
        day_start, day_end = _day_bounds()
        row = conn.execute(
            "SELECT COALESCE(SUM(request_count),0) AS n,"
            " SUM(est_cost_usd) AS cost,"
            " SUM(CASE WHEN request_count>0 AND est_cost_usd IS NULL THEN 1 ELSE 0 END) AS unknown"
            " FROM surface_judge_ledger WHERE created_at>=? AND created_at<?", (day_start,day_end)).fetchone()
    return {"requests": int(row["n"]),
            "est_cost_usd": (row["cost"] if row["cost"] is not None and not row["unknown"]
                             else None)}


# --------------------------------------------------------------- 清理

def purge_expired(limit: int = 200) -> int:
    now = _now()
    removed = 0
    with db.recall_runtime() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in ("surface_runs", "surface_validation_receipts",
                          "surface_detail_snapshots"):
                rows = conn.execute(
                    f"SELECT rowid FROM {table} WHERE expires_at < ?"
                    f" LIMIT ?", (now, limit)).fetchall()
                if rows:
                    conn.execute(
                        f"DELETE FROM {table} WHERE expires_at < ?"
                        f" AND rowid IN ({','.join('?' * len(rows))})",
                        (now, *[r["rowid"] for r in rows]))
                    removed += len(rows)
            # operation/台账行：双倍 TTL 后清理（同 recall F07 语义）
            cutoff = (datetime.now(timezone.utc) - timedelta(
                seconds=config.SURFACE_CANDIDATE_TTL_SECONDS * 2)).isoformat()
            stale = conn.execute(
                "SELECT principal_id, operation_key FROM surface_operations"
                " WHERE updated_at < ? AND status != 'running' LIMIT ?",
                (cutoff, limit)).fetchall()
            for r in stale:
                conn.execute(
                    "DELETE FROM surface_operations WHERE principal_id=?"
                    " AND operation_key=?",
                    (r["principal_id"], r["operation_key"]))
            removed += len(stale)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return removed


def reset_for_tests() -> None:
    """conftest 清理链挂点：surface 运行表全清（测试隔离根内）。"""
    with db.recall_runtime() as conn:
        for table in ("surface_operations", "surface_runs",
                      "surface_validation_receipts",
                      "surface_detail_snapshots", "surface_judge_ledger"):
            conn.execute(f"DELETE FROM {table}")
