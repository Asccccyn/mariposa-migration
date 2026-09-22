"""Phase 3/4：raw 幂等、quotes 独立检索、handoff、plans、calendar、bootstrap、time。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.bootstrap import service as bootstrap
from mariposa.calendar import service as calendar
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.plans import service as plans
from mariposa.quotes import service as quotes
from mariposa.raw import service as raw
from mariposa.time_context import service as time_ctx
from mariposa.workspace import service as workspace
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


class TestRaw:
    def test_import_idempotent(self, actors):
        payload = _conv_payload()
        r1 = raw.import_payload("worker", payload)
        assert r1["inserted"] == 35
        r2 = raw.import_payload("worker", _conv_payload(
            channel=payload["source_channel"], external=payload["external_id"]))
        assert r2["inserted"] == 0 and r2["skipped_duplicate"] == 35
        assert r1["conversation_id"] == r2["conversation_id"]
        msgs = raw.list_recent(200)
        assert len(msgs) == 35

    def test_recent_30_cap_and_order(self, actors):
        raw.import_payload("worker", _conv_payload(45))
        msgs = raw.list_recent(30)
        assert len(msgs) == 30
        times = [m["occurred_at"] for m in msgs]
        assert times == sorted(times)  # 时间正序展示
        assert msgs[-1]["source_message_id"] == "msg_0044"  # 最新 30 条

    def test_raw_search_independent_of_memory(self, actors):
        raw.import_payload("worker", _conv_payload(35))
        out = raw.search("蓝瓷小钥匙", limit=50)
        assert out["source"] == "raw" and len(out["hits"]) == 35
        # 原文不进 memory 投影
        from mariposa.retrieval import search as retrieval
        with db.formal() as conn:
            mem_hits = retrieval.search(conn, "蓝瓷小钥匙")["hits"]
        assert not mem_hits


class TestQuotes:
    def test_keep_search_withdraw(self, actors):
        q = quotes.keep("jiaming", "她说：今晚想喝热可可，加一点点肉桂。", said_at="2026-09-18T14:00:00+08:00")
        assert q["version"] == 1
        out = quotes.search("热可可")
        assert out["source"] == "quotes"
        assert out["hits"][0]["quote_id"] == q["quote_id"]
        quotes.withdraw("qiaosheng", q["quote_id"])
        assert quotes.search("热可可")["hits"] == []
        listed = quotes.list_quotes(include_withdrawn=True)
        assert listed and listed[0]["withdrawn"] == 1  # 撤下不物理删

    def test_worker_cannot_keep(self, actors):
        with pytest.raises(Forbidden):
            quotes.keep("worker", "工具人不能替他保留她的话")


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
                           memory_date="2026-09-10")
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

    def test_calendar_aggregates_and_forgotten_preview(self, actors):
        hold = memory.hold(actors["jiaming"], text="蓝瓷小钥匙放进书架盒子",
                           memory_date="2026-09-10")
        plans.create("jiaming", title="复诊", state="planned", due_at="2026-09-10T09:00:00")
        day = calendar.day("2026-09-10")
        kinds = {i["kind"] for i in day["items"]}
        assert kinds == {"memory", "plan"}
        assert day["range_end_inclusive"] is True

        # 遗忘后日历只显示摘要
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"] if p["target_memory_id"] == hold["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "把心爱小物收进书架。", "压缩")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        workspace.decide(actors["qiaosheng"], proposal_id=sub["proposal_id"],
                         proposal_revision=sub["revision"], proposal_hash=sub["proposal_hash"],
                         expected_memory_version=sub["base_memory_version"], decision="approve")
        day = calendar.day("2026-09-10")
        mem_item = next(i for i in day["items"] if i["kind"] == "memory")
        assert mem_item["preview_kind"] == "forgotten_summary"
        assert "蓝瓷小钥匙" not in mem_item["preview"]

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
    def test_claude_chat_profile(self, actors):
        # superseded by V2-BOOT-03/08：claude_chat 不再默认附 30 条原文；
        # 临近计划改为日期差 0..3 日
        raw.import_payload("worker", _conv_payload(35))
        memory.hold(actors["jiaming"], text="今天的桶", memory_date=None)  # 无日期不进三天
        out = bootstrap.get("jiaming", "claude_chat", "claude_chat")
        assert "raw" not in out  # v2：取原文走 raw.messages.list 显式查询
        assert out["coverage"]["raw"] == "not_in_default_package"
        assert out["memory_days"]["mode"] == "calendar_days"
        assert out["plans"]["upcoming_days"] == 3
        assert out["policy"]["raw_in_default_package"] is False

    def test_cc_profile_no_raw(self, actors):
        raw.import_payload("worker", _conv_payload(35))
        out = bootstrap.get("jiaming", "cc", "cc")
        assert "raw" not in out
        assert out["coverage"]["raw"] == "not_in_default_package"

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
        h1 = memory.hold(actors["jiaming"], text="今天的记忆", memory_date=today.isoformat())
        h2 = memory.hold(actors["jiaming"], text="前天的记忆",
                         memory_date=(today - timedelta(days=2)).isoformat())
        memory.hold(actors["jiaming"], text="四天前旧桶",
                    memory_date=(today - timedelta(days=4)).isoformat())
        out = bootstrap.get("jiaming", "cc", "cc")
        ids = {m["memory_id"] for m in out["memory_days"]["items"]}
        assert h1["memory_id"] in ids and h2["memory_id"] in ids
        assert all("text" not in m for m in out["memory_days"]["items"])


class TestTimeContext:
    def test_three_timelines_separated(self, actors):
        raw.import_payload("worker", _conv_payload(2))
        time_ctx.presence_touch("qiaosheng", "human")     # ui_activity
        time_ctx.presence_touch("worker", "agent")        # agent_or_system
        ctx = time_ctx.context("qiaosheng")
        assert ctx["last_user_message_at"]      # raw user 消息
        assert ctx["last_ui_activity_at"]       # 乔生 touch
        assert ctx["last_agent_or_system_activity_at"]  # worker touch
        with db.formal() as conn:
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM activity_events ORDER BY occurred_at")]
        assert kinds.count("ui_activity") >= 1
        assert kinds.count("agent_or_system_activity") >= 1  # 两条时间线分开记录

    def test_since_no_contact(self, actors):
        out = time_ctx.since("qiaosheng")
        assert out["since_last_contact"] is None
        assert "未齐" in out["note"]
