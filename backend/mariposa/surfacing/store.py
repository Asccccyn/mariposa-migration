"""surface 运行库读写：幂等作业、快照、回执、详情分页、判断台账。

只存有界运行数据（v1.4 §10.2 同 recall 运行库定位）：不保存长期
记忆正文正本；网络调用期间不持有写锁（表内容含对话/记忆投影，
保护级别同聊天库——不进普通日志/Git）。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from .. import config, db
from ..errors import IdempotencyConflict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


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

def record_judge_requests(operation_key: str, phase: str, provider: str,
                          request_count: int, est_cost_usd: float | None,
                          status: str) -> None:
    """每次真实付费出站/缓存命中分别记账（R32 分账：COUNT、judge、主模型
    阅读三本账——这里只记 judge）。"""
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT INTO surface_judge_ledger(operation_key, phase,"
            " provider, request_count, est_cost_usd, status, day,"
            " created_at) VALUES(?,?,?,?,?,?,?,?)",
            (operation_key, phase, provider, int(request_count),
             est_cost_usd, status, _day(), _now()))


def judge_usage_today() -> dict:
    """当日累计（请求计数对账用；费用估算缺失时如实施报 None）。"""
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(request_count),0) AS n,"
            " SUM(est_cost_usd) AS cost FROM surface_judge_ledger"
            " WHERE day=?", (_day(),)).fetchone()
    return {"requests": int(row["n"]),
            "est_cost_usd": (row["cost"] if row["cost"] is not None
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
