"""关系纠错历史与领域原子写（规格 v2.0 §5/§8）。

四套专业关系表保存**当前有效**关系；已纠正的旧绑定移入
relation_corrections——它不是通用关系真源，不参与 phase，不是
图谱表，不保存被删正文。

`atomic_write` 是本轮写能力（纠错/删除）共用的幂等适配：同一
事务内查完成记录→执行业务→存完成回执→提交，不出现"业务已
落地而重试 OUTCOME_UNKNOWN"（§8.2）。复用既有正式库幂等表
（idempotency_records）的 completed 行；不建业务进行中队列。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..errors import Forbidden, IdempotencyConflict

DOMAINS = ("memory_relation", "i_revision_relation", "source_binding",
           "plan_link", "word_source")
REASON = "binding_error"  # 唯一纠错类别（服务端固定，§P-R02）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_correction(conn, *, domain: str, original_instance_id: str,
                      endpoint_a: str, endpoint_b: str | None,
                      original_meta: dict | None,
                      original_created_by: str | None,
                      original_created_at: str | None,
                      corrected_by: str, note: str | None = None,
                      replacement_instance_id: str | None = None) -> str:
    """在调用方事务内登记纠错历史（§5.3 最小内容；legacy 未知留空）。"""
    if domain not in DOMAINS:
        raise Forbidden(f"unknown correction domain: {domain}")
    cid = f"corr_{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO relation_corrections(correction_id, domain,"
        " original_instance_id, endpoint_a, endpoint_b, original_meta,"
        " original_created_by, original_created_at, corrected_by,"
        " corrected_at, reason_code, note, replacement_instance_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, domain, original_instance_id, endpoint_a, endpoint_b,
         json.dumps(original_meta or {}, ensure_ascii=False),
         original_created_by, original_created_at, corrected_by, _now(),
         REASON, note, replacement_instance_id))
    return cid


def list_corrections(*, endpoint: str | None = None,
                     instance_id: str | None = None,
                     domain: str | None = None, limit: int = 50,
                     offset: int = 0) -> dict:
    """纠错历史只读（不混入有效关系）。目标已删除如实返回原始身份。"""
    where, params = [], []
    if endpoint:
        where.append("(endpoint_a=? OR endpoint_b=?)")
        params += [endpoint, endpoint]
    if instance_id:
        where.append("original_instance_id=?")
        params.append(instance_id)
    if domain:
        where.append("domain=?")
        params.append(domain)
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    with db.formal() as conn:
        rows = conn.execute(
            f"SELECT * FROM relation_corrections {cond}"
            " ORDER BY corrected_at DESC LIMIT ? OFFSET ?",
            params + [max(1, min(int(limit), 200)), max(0, int(offset))]
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) c FROM relation_corrections {cond}",
            params).fetchone()["c"]
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["original_meta"] = json.loads(d.get("original_meta") or "{}")
        except (ValueError, TypeError):
            d["original_meta"] = {}
        out.append(d)
    return {"corrections": out, "total": total,
            "limit": max(1, min(int(limit), 200)), "offset": int(offset)}


def _result_ref(result) -> str:
    """完成回执引用：结果 JSON 直接存 idempotency_records.result_ref。"""
    return json.dumps(result, ensure_ascii=False, default=str)


def _load_result(ref: str):
    try:
        return json.loads(ref)
    except (ValueError, TypeError):
        return {"ok": True, "replay_ref": ref}


def atomic_write(principal_id: str, capability: str, operation_key: str,
                 payload, fn):
    """领域原子写（§8.2）：单事务完成查重→业务→回执。

    fn(conn) 在 BEGIN IMMEDIATE 事务内执行，返回值作为完成回执存入
    idempotency_records（formal 库既有表，PR(principal_id,key) 唯一）。
    同 key 同 payload → 重放回执；同 key 异 payload → 结构化冲突；
    失败 → 整体回滚零痕迹。

    CB-008（2026-10-02 审计 P1）：记录键加 `op:` 前缀——transport 层
    幂等（registry._idempotent_invoke 的裸键）与领域 operation 键此前
    共用 (principal, capability, key) 空间，客户端把 transport key 与
    body operation_id 设成同一字符串时两层互相占坑（外层 running 与
    内层 completed 撞 UNIQUE / payload 口径不同误报冲突）。命名空间
    隔离后同字符串双键是合法调用；只重放本层记录。
    """
    import hashlib
    ph = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                   sort_keys=True, default=str)
                        .encode()).hexdigest()
    record_key = f"op:{operation_key}"
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT result_ref, payload_hash FROM idempotency_records"
                " WHERE principal_id=? AND capability=? AND"
                " idempotency_key=?",
                (principal_id, capability, record_key)).fetchone()
            if row is not None:
                if row["payload_hash"] not in (None, ph):
                    raise IdempotencyConflict(
                        "same operation key with different payload",
                        operation_key=operation_key)
                conn.execute("COMMIT")
                out = _load_result(row["result_ref"])
                out["idempotent_replay"] = True
                return out
            result = fn(conn)
            conn.execute(
                "INSERT INTO idempotency_records(principal_id, capability,"
                " idempotency_key, payload_hash, status, result_ref,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                (principal_id, capability, record_key, ph, "completed",
                 _result_ref(result), _now()))
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
