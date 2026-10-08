"""Jev 派生缓存。

只持久化可重建的标量判断/feature 与版本指纹，不保存记忆正文。
缓存不是正式 memory 真源，删除缓存不会删除或修改任何记忆。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from ... import config, db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def rerank_identity(*, query_projection: dict, candidate_projection: dict,
                    candidate_ref: str, candidate_version: str | None,
                    representation_version: str | None,
                    projection_version: str | None,
                    requested_model: str, prompt_version: str,
                    policy_version: str, schema_version: str,
                    namespace: str = "jev") -> dict:
    """namespace：判断缓存按 provider 分键（WP6 J12——Jev 与 Codex 的分数
    缓存不互相命中；同表异键，删除缓存互不影响）。"""
    qfp = fingerprint(query_projection)
    cfp = fingerprint(candidate_projection)
    parts = {
        "namespace": namespace,
        "query_fingerprint": qfp,
        "candidate_fingerprint": cfp,
        "candidate_ref": candidate_ref,
        "candidate_version": candidate_version or "",
        "representation_version": representation_version or "",
        "projection_version": projection_version or "",
        "requested_model": requested_model,
        "prompt_version": prompt_version,
        "policy_version": policy_version,
        "schema_version": schema_version,
    }
    return {**parts, "cache_key": fingerprint(parts)}


def get_rerank(identity: dict) -> dict | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(
        hours=config.RECALL_JUDGE_CACHE_TTL_HOURS)).isoformat()
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM jev_rerank_cache WHERE cache_key=?"
            " AND updated_at>=?",
            (identity["cache_key"], cutoff)).fetchone()
        if row is None:
            return None
        now = _now()
        conn.execute(
            "UPDATE jev_rerank_cache SET last_used_at=? WHERE cache_key=?",
            (now, identity["cache_key"]))
        return dict(row)


def put_rerank(identity: dict, *, relevance_signal: float,
               evaluation_status: str, resolved_model: str | None = None,
               provider_receipt_id: str | None = None) -> None:
    now = _now()
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT INTO jev_rerank_cache("
            " cache_key,query_fingerprint,candidate_fingerprint,candidate_ref,"
            " candidate_version,representation_version,projection_version,"
            " requested_model,resolved_model,prompt_version,policy_version,"
            " schema_version,relevance_signal,evaluation_status,"
            " provider_receipt_id,created_at,updated_at,last_used_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(cache_key) DO UPDATE SET"
            " relevance_signal=excluded.relevance_signal,"
            " evaluation_status=excluded.evaluation_status,"
            " resolved_model=excluded.resolved_model,"
            " provider_receipt_id=excluded.provider_receipt_id,"
            " updated_at=excluded.updated_at,last_used_at=excluded.last_used_at",
            (
                identity["cache_key"], identity["query_fingerprint"],
                identity["candidate_fingerprint"], identity["candidate_ref"],
                identity["candidate_version"],
                identity["representation_version"],
                identity["projection_version"], identity["requested_model"],
                resolved_model, identity["prompt_version"],
                identity["policy_version"], identity["schema_version"],
                relevance_signal, evaluation_status, provider_receipt_id,
                now, now, now,
            ))


def feature_identity(*, resource_ref: str, content_version: str | None,
                     projection_version: str | None, feature_name: str,
                     feature_schema_version: str, requested_model: str) -> dict:
    parts = {
        "resource_ref": resource_ref,
        "content_version": content_version or "",
        "projection_version": projection_version or "",
        "feature_name": feature_name,
        "feature_schema_version": feature_schema_version,
        "requested_model": requested_model,
    }
    return {**parts, "feature_key": fingerprint(parts)}


def get_feature(identity: dict) -> dict | None:
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM jev_feature_cache WHERE feature_key=?",
            (identity["feature_key"],)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jev_feature_cache SET last_used_at=? WHERE feature_key=?",
            (_now(), identity["feature_key"]))
        out = dict(row)
        out["feature_value"] = json.loads(out["feature_value"])
        return out


def put_feature(identity: dict, *, feature_value,
                resolved_model: str | None = None,
                provider_receipt_id: str | None = None) -> None:
    now = _now()
    with db.recall_runtime() as conn:
        conn.execute(
            "INSERT INTO jev_feature_cache("
            " feature_key,resource_ref,content_version,projection_version,"
            " feature_name,feature_schema_version,requested_model,"
            " resolved_model,feature_value,provider_receipt_id,"
            " created_at,updated_at,last_used_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(feature_key) DO UPDATE SET"
            " resolved_model=excluded.resolved_model,"
            " feature_value=excluded.feature_value,"
            " provider_receipt_id=excluded.provider_receipt_id,"
            " updated_at=excluded.updated_at,last_used_at=excluded.last_used_at",
            (
                identity["feature_key"], identity["resource_ref"],
                identity["content_version"], identity["projection_version"],
                identity["feature_name"], identity["feature_schema_version"],
                identity["requested_model"], resolved_model,
                _canonical(feature_value), provider_receipt_id, now, now, now,
            ))


def prune_rerank(*, max_rows: int = 50000) -> int:
    """有界 LRU 清理；只清 derived cache，不碰 session / 正式记忆。"""
    max_rows = max(0, int(max_rows))
    with db.recall_runtime() as conn:
        count = conn.execute(
            "SELECT COUNT(*) n FROM jev_rerank_cache").fetchone()["n"]
        excess = max(0, count - max_rows)
        if not excess:
            return 0
        keys = conn.execute(
            "SELECT cache_key FROM jev_rerank_cache"
            " ORDER BY last_used_at ASC LIMIT ?", (excess,)).fetchall()
        for row in keys:
            conn.execute("DELETE FROM jev_rerank_cache WHERE cache_key=?",
                         (row["cache_key"],))
        return len(keys)
