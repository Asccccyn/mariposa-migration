"""她的话 · 后台语义校对受控管线（§10.1）。

- 双步判定：classify 与 verify 两次独立调用（可同 provider 不同调用），
  两步一致才可能 material_conflict；不一致 -> uncertain 挂起。
- provider 未配置：run 一律 suspended，不产生任何 quote 写动作（不伪造接通）。
- auto_apply=true 且证据明确时才执行狭窄修正：新增 quote version，
  记录原文证据与原因；原复述版本保留。
- 撤下（withdrawn）的 quote 任何校对不得复活。
- provider 注入点：get_classifier()；生产从配置读取，测试可替换。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Protocol

from .. import audit, config, db
from ..errors import Forbidden, NotFound
from . import service as quotes


class SemanticClassifier(Protocol):
    def classify(self, quote_text: str, source_text: str) -> dict:
        """返回 {"label": equivalent|material_conflict|uncertain|no_source,
        "confidence": float, "reason": str}。"""
        ...


_CONFIGURED: SemanticClassifier | None = None


def get_classifier() -> SemanticClassifier | None:
    """生产：SEMANTIC_PROVIDER 配置时才有实例；当前无真实实现 -> None。"""
    return _CONFIGURED if _CONFIGURED else (
        None if not config.SEMANTIC_PROVIDER else None)


def set_classifier_for_testing(clf: SemanticClassifier | None) -> None:
    """仅测试注入用；不改变生产行为。"""
    global _CONFIGURED
    _CONFIGURED = clf


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_record(quote_id: str, classification: dict, state: str) -> str:
    item_id = f"qsr_{uuid.uuid4().hex[:10]}"
    payload = {"quote_id": quote_id, "review_kind": "quote_semantic_review",
               **classification}
    now = _now()
    with db.workspace() as wconn:
        wconn.execute(
            "INSERT INTO work_items(item_id, item_type, target_memory_id, state,"
            " current_revision, created_by, created_at, updated_at, resolution_note)"
            " VALUES(?, 'quote_semantic_review', ?, ?, 1, 'semantic_review', ?, ?, ?)",
            (item_id, quote_id, state, now, now,
             json.dumps(payload, ensure_ascii=False)))
        wconn.execute(
            "INSERT INTO proposal_versions(proposal_id, revision, payload,"
            " payload_hash, created_by, submitted_at) VALUES(?,1,?,?,?,NULL)",
            (item_id, json.dumps(payload, ensure_ascii=False),
             quotes._hash(payload) if hasattr(quotes, "_hash") else "", "semantic_review"))
    return item_id


def run_review(quote_id: str, raw_text: str | None = None) -> dict:
    """对一条 quote 执行校对。raw_text 可由调用方（受控 job）提供原文证据。"""
    with db.formal() as conn:
        row = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if row is None:
        raise NotFound("quote not found", quote_id=quote_id)
    if row["withdrawn"]:
        raise Forbidden("withdrawn quotes cannot be revived by review",
                        quote_id=quote_id)

    v = _current_quote_text(quote_id)

    classifier = get_classifier()
    if classifier is None:
        return {"status": "suspended",
                "reason": "semantic_provider_unavailable",
                "quote_id": quote_id,
                "note": "provider 未配置；不产生任何写动作"}

    if raw_text is None:
        item = _workspace_record(quote_id, {"label": "no_source",
                                            "confidence": 1.0,
                                            "reason": "raw 证据缺失"}, "rejected")
        return {"status": "no_source", "quote_id": quote_id,
                "workspace_item": item}

    step1 = classifier.classify(v, raw_text)
    step2 = classifier.classify(raw_text, v)  # 反向独立调用（双步判定）
    label1, label2 = step1.get("label"), step2.get("label")

    if label1 != label2 or label1 == "uncertain":
        item = _workspace_record(quote_id, {"label": "uncertain",
                                            "step1": step1, "step2": step2},
                                 "deferred")
        return {"status": "uncertain", "quote_id": quote_id,
                "workspace_item": item}

    if label1 in ("equivalent", "no_source"):
        item = _workspace_record(quote_id, {"label": label1, "step1": step1,
                                            "step2": step2}, "rejected")
        return {"status": label1, "quote_id": quote_id, "workspace_item": item}

    # material_conflict：双步一致
    if not config.QUOTE_SEMANTIC_AUTO_APPLY:
        item = _workspace_record(quote_id, {"label": "material_conflict",
                                            "step1": step1, "step2": step2,
                                            "proposed_text": raw_text},
                                 "submitted")
        return {"status": "pending_manual_apply", "quote_id": quote_id,
                "workspace_item": item}

    return _apply_correction(quote_id, raw_text, step1, step2)


def _current_quote_text(quote_id: str) -> str:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT v.text FROM quotes q JOIN quote_versions v"
            " ON v.quote_id = q.id AND v.version_no = q.current_version_no"
            " WHERE q.id=?", (quote_id,)).fetchone()
    return row["text"]


def _apply_correction(quote_id: str, source_text: str, step1: dict, step2: dict) -> dict:
    """狭窄修正：只改“她表达了什么”；新版本 + 证据 + 审计；旧版本保留。"""
    with db.formal() as conn:
        row = conn.execute("SELECT current_version_no FROM quotes WHERE id=?",
                           (quote_id,)).fetchone()
        new_version = row["current_version_no"] + 1
        now = _now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO quote_versions(quote_id, version_no, text,"
                " semantic_status, raw_ref, created_by, created_at)"
                " VALUES(?,?,?,'material_conflict','semantic_review','semantic_review',?)",
                (quote_id, new_version, source_text, now))
            conn.execute("UPDATE quotes SET current_version_no=?, updated_at=?"
                         " WHERE id=?", (new_version, now, quote_id))
            audit.record(
                conn, "quote.semantic_corrected", "semantic_review",
                resource_id=quote_id, resource_version=new_version,
                payload={"reason": (step1.get("reason") or "")[:200],
                         "confidence": step1.get("confidence")})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"status": "material_conflict_applied", "quote_id": quote_id,
            "new_version": new_version}


def reviews_list(states: list[str] | None = None) -> list[dict]:
    q = ("SELECT item_id, target_memory_id, state, created_at, resolution_note"
         " FROM work_items WHERE item_type='quote_semantic_review'")
    params: tuple = ()
    if states:
        q += " AND state IN (" + ",".join("?" * len(states)) + ")"
        params = tuple(states)
    q += " ORDER BY created_at DESC"
    with db.workspace() as wconn:
        rows = wconn.execute(q, params).fetchall()
    out = []
    for r in rows:
        entry = dict(r)
        entry["quote_id"] = r["target_memory_id"]
        try:
            entry["classification"] = json.loads(r["resolution_note"] or "{}")
        except json.JSONDecodeError:
            entry["classification"] = {}
        out.append(entry)
    return out
