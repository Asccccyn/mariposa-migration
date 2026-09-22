"""v2 计划终结语义（P3）：V2-PLAN-01/02/03/05/08/09/10/11 + 到期队列。

S4 已确认：完成/放弃后固定 20 自然日；打开/阅读/刷新不续期、不改
terminal_revision；只有明确状态更新回活跃（重启执行）才取消旧周期。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa.memory import retention as ret_mod
from mariposa.plans import service as plans
from mariposa.workspace import due_queue
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
    }


def _due_of(terminal_date_iso: str) -> str:
    return (datetime.fromisoformat(terminal_date_iso)
            + timedelta(days=20)).date().isoformat()


class TestPlanTerminal:
    def test_active_plan_no_auto_forgetting_plan01(self, actors):
        p = plans.create("jiaming", "进行中的计划", state="active")
        with db.formal() as conn:
            got = plans.get(conn, p["plan_id"])
        assert got["due_date"] is None and got["terminal_date"] is None
        assert due_queue.due_items() == []

    def test_done_fixed_20_natural_days_plan02(self, actors):
        p = plans.create("jiaming", "完成的计划", state="active")
        v = plans.update("jiaming", p["plan_id"], 1, state="done")
        with db.formal() as conn:
            got = plans.get(conn, p["plan_id"])
        assert got["completed_at"] is not None
        assert got["terminal_date"] == ret_mod.local_date(
            got["completed_at"]).isoformat()
        assert got["due_date"] == _due_of(got["terminal_date"])
        assert got["terminal_revision"] == 1
        # 到期队列入队
        items = [i for i in due_queue.due_items(today="2999-01-01")
                 if i["target_id"] == p["plan_id"]]
        assert len(items) == 1 and items[0]["due_date"] == got["due_date"]

    def test_cancelled_fixed_20_natural_days_plan03(self, actors):
        p = plans.create("jiaming", "放弃的计划", state="planned")
        plans.update("jiaming", p["plan_id"], 1, state="cancelled")
        with db.formal() as conn:
            got = plans.get(conn, p["plan_id"])
        assert got["abandoned_at"] is not None and got["completed_at"] is None
        assert got["due_date"] == _due_of(got["terminal_date"])

    def test_reading_never_touches_anchors_plan08(self, actors):
        p = plans.create("jiaming", "阅读不续期", state="active")
        plans.update("jiaming", p["plan_id"], 1, state="done")
        with db.formal() as conn:
            before = plans.get(conn, p["plan_id"])
        for _ in range(5):  # 反复 get = 反复查看/刷新
            with db.formal() as conn:
                got = plans.get(conn, p["plan_id"])
        assert got == before  # completed_at/terminal_date/due_date/terminal_revision 原样

    def test_repeated_terminal_save_no_anchor_reset_plan11(self, actors):
        p = plans.create("jiaming", "重复终结", state="active")
        plans.update("jiaming", p["plan_id"], 1, state="done")
        with db.formal() as conn:
            first = plans.get(conn, p["plan_id"])
        # 重复保存同终结状态（内容/标题修改）
        plans.update("jiaming", p["plan_id"], 2, content="补充内容")
        with db.formal() as conn:
            second = plans.get(conn, p["plan_id"])
        assert second["completed_at"] == first["completed_at"]
        assert second["terminal_date"] == first["terminal_date"]
        assert second["due_date"] == first["due_date"]
        assert second["terminal_revision"] == first["terminal_revision"] == 1
        # 队列也只有一行
        items = [i for i in due_queue.due_items(today="2999-01-01")
                 if i["target_id"] == p["plan_id"]]
        assert len(items) == 1

    def test_explicit_reopen_cancels_old_cycle_plan05(self, actors):
        p = plans.create("jiaming", "重启的计划", state="active")
        plans.update("jiaming", p["plan_id"], 1, state="done")
        plans.update("jiaming", p["plan_id"], 2, state="active")  # 明确重启
        with db.formal() as conn:
            got = plans.get(conn, p["plan_id"])
        assert got["terminal_revision"] == 2
        assert got["due_date"] is None and got["completed_at"] is None
        assert all(i["target_id"] != p["plan_id"]
                   for i in due_queue.due_items(today="2999-01-01"))
        # 再次完成 → 新锚点新周期
        plans.update("jiaming", p["plan_id"], 3, state="done")
        with db.formal() as conn:
            got2 = plans.get(conn, p["plan_id"])
        assert got2["terminal_revision"] == 3
        assert got2["due_date"] == _due_of(got2["terminal_date"])

    def test_plan_and_event_buckets_independent_plan04_plan09(self, actors):
        from mariposa.memory import service as memory
        m = memory.hold(actors["jiaming"], text="关联事件", memory_date="2026-09-01",
                        date_confidence="exact", original_title="事件",
                        categories=["daily"], creation_mode="contemporaneous",
                        raw_pending=False)
        p = plans.create("jiaming", "带事件的计划", state="active",
                         link_memory_ids=[m["memory_id"]])
        plans.update("jiaming", p["plan_id"], 1, state="done")
        with db.formal() as conn:
            plan = plans.get(conn, p["plan_id"])
            mem_ret = ret_mod.get(conn, m["memory_id"])
        # 计划终结不改事件桶期限；事件桶打开也不改计划到期日
        assert plan["due_date"] == _due_of(plan["terminal_date"])
        from mariposa.memory import views as views_mod
        o = views_mod.open_memory(actors["jiaming"], m["memory_id"])
        views_mod.confirm_view(actors["jiaming"], m["memory_id"], o["view_receipt"])
        with db.formal() as conn:
            plan2 = plans.get(conn, p["plan_id"])
        assert plan2["due_date"] == plan["due_date"]
        assert plan2["terminal_revision"] == plan["terminal_revision"]

    def test_due_queue_lifecycle(self, actors):
        p = plans.create("jiaming", "队列计划", state="active")
        plans.update("jiaming", p["plan_id"], 1, state="done")
        with db.formal() as conn:
            due = plans.get(conn, p["plan_id"])["due_date"]
        item = [i for i in due_queue.due_items(today=due)
                if i["target_id"] == p["plan_id"]][0]
        assert due_queue.lease(item["item_id"]) is not None
        assert due_queue.lease(item["item_id"]) is None  # 已被认领
        out = due_queue.fail(item["item_id"], "provider超时")
        assert out["status"] == "failed" and out["attempts"] == 1
        out = due_queue.fail(item["item_id"], "再失败")
        out = due_queue.fail(item["item_id"], "第三次")
        assert out["status"] == "dead"  # dead-letter，不再自动重试
        assert all(i["item_id"] != item["item_id"]
                   for i in due_queue.due_items(today=due))
