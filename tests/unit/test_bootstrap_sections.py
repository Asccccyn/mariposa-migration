"""复审关卡 5：bootstrap memory/plans 真分页（数量上限显式 + cursor 续取）。"""
from __future__ import annotations

import pytest
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone

from mariposa.bootstrap import service as bootstrap
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.plans import service as plans
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
    }


class TestMemoryDaysPaging:
    def test_section_limit_and_cursor(self, actors):
        today = datetime.now(timezone.utc).astimezone(
            ZoneInfo("Asia/Shanghai")).date()
        ids = set()
        for i in range(60):  # 窗口内 60 桶 > 段上限 50
            h = memory.hold(actors["jiaming"], text=f"分页桶 {i}",
                            memory_date=today.isoformat())
            ids.add(h["memory_id"])
        first = bootstrap.get("jiaming", "cc", "cc")
        md = first["memory_days"]
        assert md["section_limit"] == 50 and md["count"] == 50
        assert md["total_in_window"] == 60
        assert md["next_cursor"] is not None
        got = {m["memory_id"] for m in md["items"]}
        page2 = bootstrap.next_page("jiaming", "cc", first["snapshot_id"],
                                    md["next_cursor"], section="memory_days")
        assert page2["count"] == 10
        got |= {m["memory_id"] for m in page2["items"]}
        assert got == ids  # 全量可达，无重叠无丢失
        assert page2["next_cursor"] is None

    def test_no_cursor_when_small(self, actors):
        first = bootstrap.get("jiaming", "cc", "cc")
        assert first["memory_days"]["next_cursor"] is None
        assert first["plans"]["next_cursor"]["plans_offset"] is None


class TestPlansPaging:
    def test_plans_offset_cursor(self, actors):
        # open 状态计划全量进 bootstrap：造 55 个 active
        for i in range(55):
            plans.create("jiaming", title=f"分页计划 {i}", state="active")
        first = bootstrap.get("jiaming", "cc", "cc")
        pl = first["plans"]
        assert pl["count"] == 50 and pl["total"] == 55
        page2 = bootstrap.next_page("jiaming", "cc", first["snapshot_id"],
                                    {"plans_offset": 50}, section="plans")
        assert page2["count"] == 5
        assert page2["next_cursor"]["plans_offset"] is None

    def test_unknown_section_rejected(self, actors):
        first = bootstrap.get("jiaming", "cc", "cc")
        with pytest.raises(Forbidden):
            bootstrap.next_page("jiaming", "cc", first["snapshot_id"],
                                {}, section="exec.sql")
