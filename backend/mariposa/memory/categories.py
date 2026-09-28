"""九分类平行多选（v1.7 §3.2：daily/milestone/sad/sweet/date/plan/sex/anniversary/reloplay）。

同一桶可同时属于多个分类（平行，无主副）；一个分类只存一行。
期限取桶内所有分类的最长自然日周期；重大转折/纪念为永久类，
含永久类的桶不进入自动遗忘。'plan' 分类本身没有独立期限：计划资源
由 plan 终结周期管理，纯 plan 分类桶标记 plan_managed，不自动压缩。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from ..errors import Forbidden, NotFound

CATEGORIES = ("daily", "milestone", "sad", "sweet", "date", "plan",
              "sex", "anniversary", "reloplay")

LABELS = {
    "daily": "日常",
    "milestone": "重大转折",
    "sad": "伤心的事",
    "sweet": "甜蜜",
    "date": "约会",
    "plan": "plan",
    "sex": "做爱",
    "anniversary": "纪念",
}

#: 自然日周期；None = 永久（不自动遗忘）。plan 无独立期限（plan_managed）。
PERMANENT = {"milestone", "anniversary"}


def validate(categories: list[str]) -> list[str]:
    if not isinstance(categories, list) or not categories:
        raise Forbidden("categories must be a non-empty list",
                        code="INVALID_ARGUMENT")
    seen = []
    for c in categories:
        if c not in CATEGORIES:
            raise Forbidden(f"unknown category: {c}",
                            code="INVALID_ARGUMENT", category=c)
        if c not in seen:
            seen.append(c)
    return seen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def replace(conn, memory_id: str, categories: list[str], added_by: str) -> None:
    """整组替换（hold 时写入；必须在正式库事务内调用）。"""
    cats = validate(categories)
    conn.execute("DELETE FROM memory_categories WHERE memory_id=?", (memory_id,))
    now = _now()
    for c in cats:
        conn.execute(
            "INSERT INTO memory_categories(memory_id, category, added_by,"
            " created_at) VALUES(?,?,?,?)",
            (memory_id, c, added_by, now))


def add(conn, memory_id: str, categories: list[str], added_by: str) -> None:
    """追加分类（不删除已有；桶内同分类只存一次）。"""
    cats = validate(categories)
    now = _now()
    for c in cats:
        conn.execute(
            "INSERT OR IGNORE INTO memory_categories(memory_id, category,"
            " added_by, created_at) VALUES(?,?,?,?)",
            (memory_id, c, added_by, now))


def remove(conn, memory_id: str, category: str, removed_by: str) -> None:
    if category not in CATEGORIES:
        raise Forbidden(f"unknown category: {category}",
                        code="INVALID_ARGUMENT", category=category)
    cur = conn.execute(
        "DELETE FROM memory_categories WHERE memory_id=? AND category=?",
        (memory_id, category))
    if cur.rowcount == 0:
        raise NotFound("category not set on memory",
                       memory_id=memory_id, category=category)


def list_of(conn, memory_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT category FROM memory_categories WHERE memory_id=?"
        " ORDER BY rowid", (memory_id,)).fetchall()
    return [r["category"] for r in rows]



