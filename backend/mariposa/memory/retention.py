"""留存与到期：自然日语义（spec_v2 R03/R10/R11、D01/D02、§7.1）。

- 按共同业务时区的自然日计数：起算日记为第 0 日，due_date = basis_date + N；
  到期日在该时区日界起具备候选资格，不是累计 N×24 小时。
- basis = max(首次 hold 时刻, 最近明确打开确认时刻)；UTC 原时刻留作审计。
- 永久类（重大转折/纪念）与确定留不进入自动到期；迁移缺 held_at 的桶
  标 date_gap，暂停自动遗忘，不用事件日期或迁移时刻填充。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, db
from ..errors import NotFound
from . import categories as categories_mod

POLICY_VERSION = "forget_policy_v2"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_ts(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def local_date(ts: datetime | str, tzname: str | None = None) -> date:
    tz = ZoneInfo(tzname or config.RELATIONSHIP_TIMEZONE)
    if isinstance(ts, str):
        ts = parse_ts(ts)
    return ts.astimezone(tz).date()


def business_today(tzname: str | None = None) -> date:
    return local_date(_now(), tzname)


def due_from_basis(basis_date: date, period_days: int) -> date:
    """自然日加法：起算日第 0 日，跨月/跨年/闰日按日期推进（RET-14）。"""
    return basis_date + timedelta(days=period_days)


def start_of_local_day_utc(d: date, tzname: str | None = None) -> str:
    """到期日所在业务时区日界的 UTC 时刻（仅调度派生值）。"""
    tz = ZoneInfo(tzname or config.RELATIONSHIP_TIMEZONE)
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(
        timezone.utc).isoformat()


def compute_row(categories: list[str], basis_at: str | None,
                tzname: str | None = None) -> dict:
    """根据分类与起算时刻推导 retention 字段（不落库）。"""
    tz = tzname or config.RELATIONSHIP_TIMEZONE
    kind = categories_mod.classify(categories)
    row = {
        "policy_version": POLICY_VERSION,
        "policy_timezone": tz,
        "basis_at": basis_at,
        "basis_date": local_date(basis_at, tz).isoformat() if basis_at else None,
        "due_date": None,
        "next_due_at": None,
        "status": None,
    }
    if basis_at is None:
        # 日期缺口：不得用事件日期/迁移时刻填充（D01/§7.1）
        row["status"] = "date_gap"
        return row
    if kind == "permanent":
        row["status"] = "excluded"
        row["permanent_reason"] = "permanent_category"
        return row
    if kind == "plan_managed":
        row["status"] = "plan_managed"
        return row
    if kind == "uncategorized":
        row["status"] = "excluded"  # 未分类：无期限依据，见 DECISIONS
        row["permanent_reason"] = "uncategorized"
        return row
    period = categories_mod.period_days(categories)
    due = due_from_basis(local_date(basis_at, tz), period)
    row["due_date"] = due.isoformat()
    row["next_due_at"] = start_of_local_day_utc(due, tz)
    row["status"] = "active"
    return row


def create_for_memory(conn, memory_id: str, categories: list[str],
                      held_at: str | None, tzname: str | None = None) -> None:
    """hold v2 时建立 retention 行（正式库事务内调用）。"""
    row = compute_row(categories, held_at, tzname)
    conn.execute(
        "INSERT OR REPLACE INTO memory_retention(memory_id, policy_version,"
        " policy_timezone, basis_at, basis_date, due_date, next_due_at,"
        " last_explicit_open_at, view_revision, retention_revision,"
        " permanent_reason, status)"
        " VALUES(?,?,?,?,?,?,?,NULL,0,0,?,?)",
        (memory_id, row["policy_version"], row["policy_timezone"],
         row["basis_at"], row["basis_date"], row["due_date"],
         row["next_due_at"], row.get("permanent_reason"), row["status"]))
    _sync_queue(conn, memory_id, row)


def _sync_queue(conn, memory_id: str, row: dict) -> None:
    """到期队列与 retention 行保持一致（派生值；真源仍是本表）。"""
    from ..workspace import due_queue
    if row["status"] == "active":
        due_queue.replace_pending(conn, "memory", memory_id, row["due_date"])
    else:
        due_queue.replace_pending(conn, "memory", memory_id, None)


def get(conn, memory_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM memory_retention WHERE memory_id=?",
        (memory_id,)).fetchone()
    return dict(row) if row else None


def recompute(conn, memory_id: str, tzname: str | None = None) -> dict:
    """分类变化后按当前分类重算（保持原 basis，除非永久化/保留）。"""
    r = get(conn, memory_id)
    if r is None:
        raise NotFound("retention row missing", memory_id=memory_id)
    if r["status"] == "retained":
        return r  # 确定留是终局，不因重算退出（R15）
    cats = categories_mod.list_of(conn, memory_id)
    row = compute_row(cats, r["basis_at"], tzname or r["policy_timezone"])
    conn.execute(
        "UPDATE memory_retention SET due_date=?, next_due_at=?, status=?,"
        " permanent_reason=? WHERE memory_id=?",
        (row["due_date"], row["next_due_at"], row["status"],
         row.get("permanent_reason"), memory_id))
    _sync_queue(conn, memory_id, row)
    return row


def register_explicit_open(conn, memory_id: str,
                           opened_at: str | None = None) -> dict:
    """明确打开确认后的续期（R10）：普通桶按本次打开所在自然日重算期限。

    仅作用于 status='active' 的普通桶；同自然日多次打开 due_date 不变
    （RET-15）；永久/保留/plan_managed/日期缺口打开不产生期限变化。
    调用方必须在正式库事务内、且已核验回执。
    """
    r = get(conn, memory_id)
    if r is None:
        raise NotFound("retention row missing", memory_id=memory_id)
    now = opened_at or iso(_now())
    if r["status"] != "active":
        return {**r, "renewed": False,
                "reason": f"status={r['status']} 不按打开续期"}
    cats = categories_mod.list_of(conn, memory_id)
    row = compute_row(cats, now, r["policy_timezone"])
    new_due = row["due_date"]
    same_day = r["due_date"] == new_due
    conn.execute(
        "UPDATE memory_retention SET last_explicit_open_at=?, basis_at=?,"
        " basis_date=?, due_date=?, next_due_at=?,"
        " retention_revision=retention_revision+1 WHERE memory_id=?",
        (now, now, row["basis_date"], new_due, row["next_due_at"], memory_id))
    _sync_queue(conn, memory_id, row)
    out = get(conn, memory_id)
    out["renewed"] = True
    out["same_day"] = same_day
    return out


def mark_retained(conn, memory_id: str, reason: str,
                  decided_by: str) -> dict:
    """确定留（R15）：正式终局，不再安排周期复审。"""
    conn.execute(
        "UPDATE memory_retention SET status='retained', due_date=NULL,"
        " next_due_at=NULL, permanent_reason=?,"
        " retention_revision=retention_revision+1 WHERE memory_id=?",
        (f"{reason} (by {decided_by})", memory_id))
    return get(conn, memory_id)


def set_retain_hint(conn, memory_id: str, hint: str) -> None:
    """保留线索（D06）：暂停自动压缩，不等于永久保留。"""
    conn.execute(
        "UPDATE memory_retention SET retain_hint=? WHERE memory_id=?",
        (hint, memory_id))


def is_due(conn, memory_id: str, today: date | None = None) -> bool:
    r = get(conn, memory_id)
    if r is None or r["status"] != "active" or not r["due_date"]:
        return False
    t = today or business_today(r["policy_timezone"])
    return t >= date.fromisoformat(r["due_date"])


def bump_view_revision(conn, memory_id: str) -> None:
    conn.execute(
        "UPDATE memory_retention SET view_revision=view_revision+1"
        " WHERE memory_id=?", (memory_id,))
