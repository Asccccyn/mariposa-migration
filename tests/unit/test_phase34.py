"""Phase 3/4：raw 幂等、quotes 独立检索、handoff、plans、calendar、bootstrap、time。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.bootstrap import service as bootstrap
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.plans import service as plans
from mariposa.time_context import service as time_ctx
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "binding_qiaosheng"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "binding_jiaming"),
        "jiaming_cc": identity.Principal("jiaming", "周家明", "agent", "cc", "binding_jiaming_cc"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "binding_worker"),
    }


def _conv_payload(n=35, channel="claude_export", external=None):
    external = external or f"export_{uuid.uuid4().hex[:8]}"
    base = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    messages = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({
            "source_message_id": f"msg_{i:04d}",
            "role": role,
            "body": f"测试消息 {i}：今天聊到蓝瓷小钥匙的只有第 5 条。",
            "occurred_at": (base + timedelta(minutes=i)).isoformat(),
            "sequence": i,
        })
    return {"source_channel": channel, "external_id": external, "messages": messages}




class TestHandoff:
    def test_only_jiaming_writes(self, actors):
        r = time_ctx.handoff_write("jiaming", "claude_chat", "明天记得提醒她体检报告的事")
        assert r["handoff_id"]
        latest = time_ctx.handoff_latest()["handoff"]
        assert latest["content"].startswith("明天")
        assert latest["expired"] is False

    def test_worker_rejected(self, actors):
        with pytest.raises(Forbidden):
            time_ctx.handoff_write("worker", "gpt_chat", "工具人不能代写便签")


class TestPlansAndCalendar:
    def test_plan_lifecycle_and_links(self, actors):
        hold = memory.hold(actors["jiaming"], text="讨论了周年旅行计划",
                           memory_date="2026-09-10", categories=["daily"], original_title="测试标题")
        p = plans.create("jiaming", title="周年旅行", state="active",
                         date_start="2026-09-25", date_end="2026-09-27",
                         link_memory_ids=[hold["memory_id"]])
        assert p["state"] == "active"
        with pytest.raises(NotFound):
            plans.create("jiaming", title="坏链接", link_memory_ids=["mem_missing"])
        u = plans.update("jiaming", p["plan_id"], expected_version=1, state="waiting")
        assert u["version"] == 2 and u["state"] == "waiting"
        with pytest.raises(Forbidden):
            plans.update("jiaming", p["plan_id"], expected_version=1, state="done")

    def test_bootstrap_plans_filter(self, actors):
        from zoneinfo import ZoneInfo
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
        plans.create("jiaming", title="进行中", state="active")
        plans.create("jiaming", title="近七日开始", state="planned",
                     starts_at=(today + timedelta(days=3)).isoformat())
        plans.create("jiaming", title="远期无期日", state="planned",
                     starts_at=(today + timedelta(days=30)).isoformat())
        plans.create("jiaming", title="无日期planned", state="planned")
        plans.create("jiaming", title="已完成", state="done")
        got = {p["title"] for p in plans.bootstrap_plans(today)}
        assert "进行中" in got and "近七日开始" in got
        assert "远期无期日" not in got and "无日期planned" not in got and "已完成" not in got


class TestBootstrap:


    def test_profile_mismatch_rejected(self, actors):
        with pytest.raises(Forbidden):
            bootstrap.get("jiaming", "claude_chat", "cc")
        with pytest.raises(Forbidden):
            bootstrap.get("worker", "gpt_chat", "claude_chat")

    def test_three_day_bucket_uses_event_date(self, actors):
        # superseded by V2-BOOT-01：开窗条目不再携带正文，改为按事件日期
        # 判定窗口成员（三天=今天及前两天，四天前不进窗）
        from zoneinfo import ZoneInfo
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date()
        h1 = memory.hold(actors["jiaming"], text="今天的记忆", memory_date=today.isoformat(), categories=["daily"], original_title="测试标题")
        h2 = memory.hold(actors["jiaming"], text="前天的记忆",
                         memory_date=(today - timedelta(days=2)).isoformat(), categories=["daily"], original_title="测试标题")
        memory.hold(actors["jiaming"], text="四天前旧桶",
                    memory_date=(today - timedelta(days=4)).isoformat(), categories=["daily"], original_title="测试标题")
        out = bootstrap.get("jiaming", "cc", "cc")
        ids = {m["memory_id"] for m in out["memory_days"]["items"]}
        assert h1["memory_id"] in ids and h2["memory_id"] in ids
        assert all("text" not in m for m in out["memory_days"]["items"])


class TestTimeContext:

    def test_since_no_contact(self, actors):
        out = time_ctx.since("qiaosheng")
        assert out["since_last_contact"] is None
        assert "未齐" in out["note"]
