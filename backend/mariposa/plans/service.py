"""计划（§11.1 / spec_v2 R12、D05、§7.2）：独立实体真源。

计划与事件桶彼此独立；完成/放弃从对应状态变更所在自然日起固定 20 个
自然日到期；**任何查看/阅读都不续期、不改 terminal_revision**；只有明确
把终结状态改回活跃（重启执行）才取消旧终结周期并 bump terminal_revision。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import audit, config, db
from ..memory import retention as retention_mod
from ..memory import service as memory
from ..errors import Forbidden, NotFound

STATES = {"planned", "active", "waiting", "blocked", "done", "cancelled"}
OPEN_STATES = {"active", "waiting", "blocked"}
TERMINAL_STATES = {"done": "completed_at", "cancelled": "abandoned_at"}
#: plan 终结后的固定到期自然日数（S4 确认；不按小时累计）
TERMINAL_FORGET_DAYS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(payload: dict) -> str:
    return memory.canonical_hash(payload)


def _tz_of(v) -> str:
    return v["timezone"] or config.RELATIONSHIP_TIMEZONE


def _terminal_anchors(now: str, tzname: str) -> dict:
    """终结自然日 + 固定 20 自然日到期（起算日第 0 日）。"""
    t_date = retention_mod.local_date(now, tzname)
    due = retention_mod.due_from_basis(t_date, TERMINAL_FORGET_DAYS)
    return {"terminal_date": t_date.isoformat(),
            "due_date": due.isoformat(),
            "next_due_at": retention_mod.start_of_local_day_utc(due, tzname)}


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
    tzname = timezone_name or config.RELATIONSHIP_TIMEZONE
    anchors = _terminal_anchors(now, tzname) if state in TERMINAL_STATES else {}
    version_payload = {"title": title, "content": content, "state": state,
                       "starts_at": starts_at, "due_at": due_at, "date_start": date_start,
                       "date_end": date_end, "weight": weight}
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO plans(id, current_version_no, state, created_at,"
                " updated_at, completed_at, terminal_date, due_date,"
                " policy_timezone, terminal_revision)"
                " VALUES(?,1,?,?,?,?,?,?,?,?)",
                (pid, state, now, now,
                 now if state == "done" else None,
                 anchors.get("terminal_date"), anchors.get("due_date"),
                 tzname, 1 if state in TERMINAL_STATES else 0))
            if state == "cancelled":
                conn.execute(
                    "UPDATE plans SET completed_at=NULL, abandoned_at=? WHERE id=?",
                    (now, pid))
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
            if state in TERMINAL_STATES:
                # 直建终态计划同样进入到期队列（与 update 终结路径一致）
                from ..workspace import due_queue
                due_queue.replace_pending(conn, "plan", pid,
                                          anchors.get("due_date"))
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
    """版本冲突保护：expected_version != current -> VERSION_CONFLICT。

    终结锚点规则（S4/D05/§7.2）：
    - 活跃→done/cancelled：落对应终结时刻，due=终结自然日+20，terminal_revision+1；
    - 终结→同终结状态重复保存：锚点不动（PLAN-11）；
    - 终结→活跃（明确重启执行）：terminal_revision+1，清除旧终结锚点；
    - 任何内容/标题修改不触碰终结锚点；阅读走 get()，永不写这些字段。
    """
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
        tzname = _tz_of(v)
        old_state = v["state"]
        was_terminal = old_state in TERMINAL_STATES
        now_terminal = new_state in TERMINAL_STATES
        plans_update = {"state": new_state}
        if not was_terminal and now_terminal:
            anchors = _terminal_anchors(now, tzname)
            plans_update.update({
                TERMINAL_STATES[new_state]: now,
                "terminal_date": anchors["terminal_date"],
                "due_date": anchors["due_date"],
                "policy_timezone": tzname,
                "terminal_revision": row["terminal_revision"] + 1,
            })
        elif was_terminal and not now_terminal:
            # 明确重启执行：取消旧终结周期；只有状态更新能到这里
            plans_update.update({
                "completed_at": None, "abandoned_at": None,
                "terminal_date": None, "due_date": None,
                "terminal_revision": row["terminal_revision"] + 1,
            })
        # was_terminal and now_terminal：锚点保持不变（重复保存/换终结原因
        # 都不重写终结时刻；PLAN-11）
        sets = ", ".join(f"{k}=?" for k in plans_update)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 队列是 plans 锚点的派生物，必须与锚点同事务落库，
            # 否则 plans 回滚后队列与真源不一致
            from ..workspace import due_queue
            if not was_terminal and now_terminal:
                due_queue.replace_pending(conn, "plan", plan_id,
                                          plans_update.get("due_date"))
            elif was_terminal and not now_terminal:
                due_queue.replace_pending(conn, "plan", plan_id, None)
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
                f"UPDATE plans SET current_version_no=?, updated_at=?, {sets}"
                " WHERE id=?",
                (new_version, now, *plans_update.values(), plan_id))
            audit.record(conn, "plan.changed", principal_id, resource_id=plan_id,
                         resource_version=new_version,
                         payload={"action": "update", "state": new_state,
                                  "terminal_transition":
                                   f"{old_state}->{new_state}"
                                   if (was_terminal or now_terminal) else None})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"plan_id": plan_id, "version": new_version, "state": merged["state"]}


def get(conn, plan_id: str) -> dict:
    """读取计划详情：纯只读，不写任何终结字段（plan 阅读永不续期）。"""
    row, v = _current(conn, plan_id)
    return {"plan_id": plan_id, "version": row["current_version_no"],
            "title": v["title"], "content": v["content"], "state": v["state"],
            "starts_at": v["starts_at"], "due_at": v["due_at"],
            "date_start": v["date_start"], "date_end": v["date_end"],
            "all_day": bool(v["all_day"]), "weight": v["weight"],
            "completed_at": row["completed_at"],
            "abandoned_at": row["abandoned_at"],
            "terminal_date": row["terminal_date"],
            "due_date": row["due_date"],
            "terminal_revision": row["terminal_revision"],
            "forgetting_note": "完成/放弃后固定20自然日；查看不续期"}


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


def bootstrap_plans(now_local_date, upcoming_days: int = 3) -> list[dict]:
    """v2 开窗计划池（BOOT-08/PLAN-06）。

    - active/waiting/blocked（进行中/需执行）全取；
    - planned：按业务日期差 0..upcoming_days 日纳入（含恰好边界，按日期
      不按小时），逾期未完成（anchor < today）也单列保留，不消失；
    - done/cancelled 不进开窗（终结周期由到期队列管理，与阅读无关）。
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
            if day <= horizon:  # 含逾期（< today）与 0..3 日临近
                out.append(p)
    return out
