"""业务日时间纯函数（v1.7：自遗忘模块退役中抽出，供 plans/bootstrap 复用）。

按共同业务时区（config.RELATIONSHIP_TIMEZONE）计算自然日；
本模块只做时间换算，不含任何遗忘/到期策略。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config


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
    return local_date(datetime.now(timezone.utc), tzname)


def start_of_local_day_utc(day: date, tzname: str | None = None) -> str:
    """业务时区某自然日 00:00 对应的 UTC ISO 时间。"""
    tz = ZoneInfo(tzname or config.RELATIONSHIP_TIMEZONE)
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(
        timezone.utc).isoformat()
