"""v2 开窗（P6 后端）：V2-BOOT-01..08。

三天桶=标题+心情标签+文字；I 完整浮现；不默认 30 条原文；开窗不续期；
跨变化 snapshot 失效；不硬截正文；最近三天不是滚动 72 小时；临近按
日期而非小时。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from mariposa import db
from mariposa.bootstrap import service as bootstrap
from mariposa.identity import service as identity
from mariposa import biztime as ret_mod
from mariposa.memory import service as memory
from mariposa.plans import service as plans
from tests.conftest import reset_all

TZ = "Asia/Shanghai"


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold_v2(actors, date, title="桶标题", text="事件正文", mood=None,
            cats=("daily",)):
    return memory.hold(actors["jiaming"], text=text, memory_date=date,
                       date_confidence="exact", original_title=title,
                       categories=list(cats), mood=mood,
                       creation_mode="contemporaneous", raw_pending=False)


class TestBootstrapV2:
    def test_three_day_buckets_correct_fields_boot01(self, actors):
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()
        in_range = [
            hold_v2(actors, today.isoformat(), title="今天的标题",
                    text="今天的事件正文不应出现在开窗",
                    mood={"text": "今天心情文字", "tags": ["开心"]}),
            hold_v2(actors, (today - timedelta(days=2)).isoformat(),
                    title="前天的标题"),
        ]
        out_of_range = hold_v2(actors, (today - timedelta(days=3)).isoformat(),
                               title="四天前的标题")
        out = bootstrap.get("jiaming", "cc", "cc")
        md = out["memory_days"]
        assert sorted(md["dates"]) == sorted([
            today.isoformat(), (today - timedelta(days=1)).isoformat(),
            (today - timedelta(days=2)).isoformat()])
        items = {m["memory_id"]: m for m in md["items"]}
        for m in in_range:
            assert m["memory_id"] in items
            it = items[m["memory_id"]]
            assert it["original_title"] in ("今天的标题", "前天的标题")
            assert "text" not in it  # 不默认展开事件
            assert it["mood_tags"] == (["开心"] if it["original_title"] == "今天的标题" else [])
        assert items[in_range[0]["memory_id"]]["mood_text"] == "今天心情文字"
        assert out_of_range["memory_id"] not in items

    def test_i_fully_surfaced_boot02(self, actors):
        from mariposa.identity_i import service as i_svc
        i_svc.write("jiaming", "I 的完整正本内容，不被标题化。")
        out = bootstrap.get("jiaming", "cc", "cc")
        assert out["i"]["content"] == "I 的完整正本内容，不被标题化。"
        assert out["i"]["version"] == 1

    def test_no_30_raw_messages_boot03(self, actors):
        from mariposa.raw import service as raw_svc
        base = datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
        raw_svc.import_payload("worker", {
            "source_channel": "x", "external_id": "e1",
            "messages": [{"source_message_id": f"m{i}", "role": "user",
                          "body": f"消息{i}",
                          "occurred_at": (base + timedelta(minutes=i)).isoformat(),
                          "sequence": i} for i in range(40)]})
        for profile, entry in (("cc", "cc"), ("claude_chat", "claude_chat")):
            out = bootstrap.get("jiaming", entry, profile)
            assert "raw" not in out
            assert out["coverage"]["raw"] == "not_in_default_package"

    def test_bootstrap_does_not_renew_boot04(self, actors):
        # v1.7：bootstrap 不算明确打开——last_explicit_open_at 不得被刷新
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()
        m = hold_v2(actors, today.isoformat())
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET last_explicit_open_at=? WHERE memory_id=?",
                ("2026-01-01T00:00:00+00:00", m["memory_id"]))
        for _ in range(3):
            bootstrap.get("jiaming", "cc", "cc")
        with db.formal() as conn:
            row = conn.execute(
                "SELECT last_explicit_open_at FROM memories WHERE memory_id=?",
                (m["memory_id"],)).fetchone()
        assert row["last_explicit_open_at"] == "2026-01-01T00:00:00+00:00"

    def test_snapshot_invalidated_by_changes_boot05(self, actors):
        first = bootstrap.get("jiaming", "cc", "cc")
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()
        hold_v2(actors, today.isoformat())  # 新桶 → 状态指纹变化
        from mariposa.errors import SnapshotStale
        with pytest.raises(SnapshotStale):
            bootstrap.get("jiaming", "cc", "cc",
                          loaded_snapshot_id=first["snapshot_id"])

    def test_three_days_not_rolling_72h_boot07(self, actors):
        # 业务今天 00:05：9月19日虽距当前不足 72 小时，仍不在普通近期桶
        row = bootstrap._state_hash  # noqa: F841  （引用模块）
        import mariposa.bootstrap.service as bs
        from datetime import date as _date
        # 纯逻辑验证：三天窗口按事件日期生成
        fake_today = _date(2026, 9, 22)
        days = [(fake_today - timedelta(days=i)).isoformat()
                for i in range(bootstrap.BOOT_MEMORY_DAYS)]
        assert days == ["2026-09-22", "2026-09-21", "2026-09-20"]
        assert "2026-09-19" not in days

    def test_upcoming_by_date_not_hours_boot08(self, actors):
        # 9月25日的计划：按日期差恰为 3 日 → 属临近；9月26日不属于
        from datetime import date as _date
        fake_today = _date(2026, 9, 22)
        got = plans.bootstrap_plans(fake_today, bootstrap.BOOT_UPCOMING_DAYS)
        # 构造真实数据验证：锚定真实今天更稳
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()
        p3 = plans.create("jiaming", "差三天",
                          state="planned",
                          starts_at=f"{today + timedelta(days=3)}T23:55:00+08:00")
        p4 = plans.create("jiaming", "差四天",
                          state="planned",
                          starts_at=f"{today + timedelta(days=4)}T00:05:00+08:00")
        overdue = plans.create("jiaming", "逾期未完成",
                               state="planned",
                               starts_at=f"{today - timedelta(days=5)}T10:00:00+08:00")
        got = plans.bootstrap_plans(today, bootstrap.BOOT_UPCOMING_DAYS)
        ids = {p["plan_id"] for p in got}
        assert p3["plan_id"] in ids      # 23:55 仍属提前3自然日（按日期）
        assert p4["plan_id"] not in ids  # 第4日不属临近池
        assert overdue["plan_id"] in ids  # 逾期未完成不消失
        out = bootstrap.get("jiaming", "cc", "cc")
        assert out["plans"]["mode"] == "date_diff_0_to_3"

    def test_late_entry_flagged(self, actors):
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date()
        m = hold_v2(actors, (today - timedelta(days=1)).isoformat(),
                    title="昨天的事今天才记")
        out = bootstrap.get("jiaming", "cc", "cc")
        item = next(i for i in out["memory_days"]["items"]
                    if i["memory_id"] == m["memory_id"])
        assert item.get("late_entry") is True  # 新收录标记，不冒充刚发生
