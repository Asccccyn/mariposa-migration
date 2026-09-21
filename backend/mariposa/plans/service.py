"""计划（§11.1）：独立实体真源，记忆桶经链接引用；修改只改同一条实体。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..memory import service as memory
from ..errors import Forbidden, NotFound

STATES = {"planned", "active", "waiting", "blocked", "done", "cancelled"}
OPEN_STATES = {"active", "waiting", "blocked"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(payload: dict) -> str:
    return memory.canonical_hash(payload)


def create(principal_id: str, title: str, content: str | None = None,
           state: str = "planned", starts_at: str | None = None, due_at: str | None = None,
           date_start: str | None = None, date_end: str | None = None,
           timezone_name: str | None = None, all_day: bool = False,
           weight: str | None = None, link_memory_ids: list[str] | None = None) -> dict:
    if not title or not str(title).strip():
        raise Forbidden("plan title required")
    if state not in STATES:
        raise Forbidden(f"invalid state: {state}")
    pid = f"plan_{uuid.uuid4().hex[:10]}"
    now = _now()
    version_payload = {"title": title, "content": content, "state": state,
                       "starts_at": starts_at, "due_at": due_at, "date_start": date_start,
                       "date_end": date_end, "weight": weight}
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO plans(id, current_version_no, state, created_at, updated_at)"
                " VALUES(?,1,?,?,?)", (pid, state, now, now))
            conn.execute(
                "INSERT INTO plan_versions(plan_id, version_no, title, content, state,"
                " starts_at, due_at, date_start, date_end, timezone, all_day, weight,"
                " payload_hash, created_by, created_at)"
                " VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, title, content, state, starts_at, due_at, date_start, date_end,
                 timezone_name, int(all_day), weight, _hash(version_payload),
                 principal_id, now))
            for mid in link_memory_ids or []:
                exists = conn.execute(
                    "SELECT 1 FROM memories WHERE memory_id=?", (mid,)).fetchone()
                if not exists:
                    raise NotFound("linked memory not found", memory_id=mid)
                conn.execute(
                    "INSERT OR IGNORE INTO plan_memory_links(plan_id, memory_id)"
                    " VALUES(?,?)", (pid, mid))
            audit.record(conn, "plan.changed", principal_id, resource_id=pid,
                         resource_version=1, payload={"action": "create", "state": state})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"plan_id": pid, "version": 1, "state": state}


def _current(conn, plan_id: str):
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise NotFound("plan not found", plan_id=plan_id)
    v = conn.execute(
        "SELECT * FROM plan_versions WHERE plan_id=? AND version_no=?",
        (plan_id, row["current_version_no"])).fetchone()
    return row, v


def update(principal_id: str, plan_id: str, expected_version: int, **changes) -> dict:
    """版本冲突保护：expected_version != current -> VERSION_CONFLICT。"""
    with db.formal() as conn:
        row, v = _current(conn, plan_id)
        if row["current_version_no"] != expected_version:
            raise Forbidden("plan version conflict",
                            code="VERSION_CONFLICT",
                            expected=expected_version,
                            current=row["current_version_no"])
        new_state = changes.get("state", v["state"])
        if new_state not in STATES:
            raise Forbidden(f"invalid state: {new_state}")
        merged = {
            "title": changes.get("title", v["title"]),
            "content": changes.get("content", v["content"]),
            "state": new_state,
            "starts_at": changes.get("starts_at", v["starts_at"]),
            "due_at": changes.get("due_at", v["due_at"]),
            "date_start": changes.get("date_start", v["date_start"]),
            "date_end": changes.get("date_end", v["date_end"]),
            "weight": changes.get("weight", v["weight"]),
        }
        new_version = row["current_version_no"] + 1
        now = _now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO plan_versions(plan_id, version_no, title, content, state,"
                " starts_at, due_at, date_start, date_end, timezone, all_day, weight,"
                " payload_hash, created_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, new_version, merged["title"], merged["content"],
                 merged["state"], merged["starts_at"], merged["due_at"],
                 merged["date_start"], merged["date_end"], v["timezone"],
                 v["all_day"], merged["weight"], _hash(merged), principal_id, now))
            conn.execute(
                "UPDATE plans SET current_version_no=?, state=?, updated_at=? WHERE id=?",
                (new_version, merged["state"], now, plan_id))
            audit.record(conn, "plan.changed", principal_id, resource_id=plan_id,
                         resource_version=new_version,
                         payload={"action": "update", "state": merged["state"]})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"plan_id": plan_id, "version": new_version, "state": merged["state"]}


def get(conn, plan_id: str) -> dict:
    row, v = _current(conn, plan_id)
    return {"plan_id": plan_id, "version": row["current_version_no"],
            "title": v["title"], "content": v["content"], "state": v["state"],
            "starts_at": v["starts_at"], "due_at": v["due_at"],
            "date_start": v["date_start"], "date_end": v["date_end"],
            "all_day": bool(v["all_day"]), "weight": v["weight"]}


def list_plans(states: list[str] | None = None) -> list[dict]:
    with db.formal() as conn:
        if states:
            marks = ",".join("?" * len(states))
            rows = conn.execute(
                f"SELECT id FROM plans WHERE state IN ({marks}) ORDER BY updated_at DESC",
                tuple(states)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM plans ORDER BY updated_at DESC").fetchall()
        return [get(conn, r["id"]) for r in rows]


def bootstrap_plans(now_local_date, upcoming_days: int = 7) -> list[dict]:
    """§12.1：active/waiting/blocked 全取 + 7 日内 starts/due 的 planned + 逾期未完成。

    done/cancelled 排除；无日期 planned 不算临近。
    """
    from datetime import timedelta
    horizon = (now_local_date + timedelta(days=upcoming_days)).isoformat()
    today = now_local_date.isoformat()
    out = []
    for p in list_plans():
        if p["state"] in OPEN_STATES:
            out.append(p)
        elif p["state"] == "planned":
            anchor = p["starts_at"] or p["due_at"] or p["date_start"]
            if not anchor:
                continue
            day = anchor[:10]
            if today <= day <= horizon:
                out.append(p)
        elif p["state"] in ("done", "cancelled"):
            continue
    return out
