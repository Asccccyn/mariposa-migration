"""日历（§11.2）：聚合视图，不复制正式内容。

CalendarItemProvider 模式：第一版接 memory 与 plan；diary/reminder 后续登记。
遗忘桶在日历中只显示批准摘要（preview_kind=forgotten_summary）。
"""
from __future__ import annotations

from typing import Callable

from .. import db
from ..errors import Forbidden
from ..memory import service as memory
from ..plans import service as plans


def _item(item_id: str, kind: str, resource_id: str, date_start: str, date_end: str,
          title: str, preview: str, preview_kind: str, status: str,
          resource_version: int) -> dict:
    return {
        "item_id": item_id, "kind": kind, "resource_id": resource_id,
        "date_start": date_start, "date_end": date_end,
        "time_precision": "day", "title": title,
        "preview_kind": preview_kind, "preview": preview,
        "status": status, "resource_version": resource_version,
        "deep_link": f"/{kind}s/{resource_id}",
    }


def memory_provider(conn) -> Callable[[str, str], list[dict]]:
    def list_range(start_date: str, end_date: str) -> list[dict]:
        rows = conn.execute(
            "SELECT memory_id, current_version_no, memory_date, compression_state,"
            " visibility FROM memories WHERE memory_date IS NOT NULL"
            " AND memory_date >= ? AND memory_date <= ?",
            (start_date, end_date),
        ).fetchall()
        items = []
        for r in rows:
            if r["visibility"] != "active":
                continue
            rep = memory.get(conn, r["memory_id"])
            items.append(_item(
                f"memory:{r['memory_id']}", "memory", r["memory_id"],
                r["memory_date"], r["memory_date"],
                (rep["text"][:24] + "…") if len(rep["text"]) > 24 else rep["text"],
                rep["text"], rep["representation"], r["visibility"],
                r["current_version_no"],
            ))
        return items
    return list_range


def plan_provider(conn) -> Callable[[str, str], list[dict]]:
    def list_range(start_date: str, end_date: str) -> list[dict]:
        items = []
        for p in plans.list_plans():
            if p["state"] in ("done", "cancelled") and not _within(
                    p.get("date_start"), p.get("date_end"), start_date, end_date):
                continue
            anchors: list[tuple[str, str]] = []
            if p.get("date_start") and p.get("date_end"):
                anchors.append((p["date_start"], p["date_end"]))
            else:
                for key in ("starts_at", "due_at"):
                    if p.get(key):
                        anchors.append((p[key][:10], p[key][:10]))
            for ds, de in anchors:
                if ds <= end_date and de >= start_date:
                    items.append(_item(
                        f"plan:{p['plan_id']}", "plan", p["plan_id"], ds, de,
                        p["title"], p["content"] or "", "plan_content",
                        p["state"], p["version"],
                    ))
        return items
    return list_range


def _within(a_start, a_end, b_start, b_end) -> bool:
    if not a_start:
        return False
    return a_start <= b_end and (a_end or a_start) >= b_start


PROVIDERS = {"memory": memory_provider, "plan": plan_provider}


def range_items(start_date: str, end_date: str,
                types: list[str] | None = None) -> dict:
    """date range 端点含端（§11.2：契约区分包含日期与 [start,end) 时间区间）。"""
    selected = types or list(PROVIDERS)
    unknown = set(selected) - set(PROVIDERS)
    if unknown:
        raise Forbidden(f"unknown calendar types: {sorted(unknown)}")
    with db.formal() as conn:
        items: list[dict] = []
        for t in selected:
            items.extend(PROVIDERS[t](conn)(start_date, end_date))
    items.sort(key=lambda x: (x["date_start"], x["kind"], x["item_id"]))
    return {"start_date": start_date, "end_date": end_date,
            "range_end_inclusive": True, "items": items, "counts": len(items)}


def day(date_str: str, types: list[str] | None = None) -> dict:
    return range_items(date_str, date_str, types)


def month(year: int, month: int, types: list[str] | None = None) -> dict:
    from calendar import monthrange
    last = monthrange(year, month)[1]
    return range_items(f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}",
                       types)
