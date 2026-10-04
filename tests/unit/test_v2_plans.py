"""v2 计划终结语义（P3）：V2-PLAN-01/02/03/05/08/09/10/11 + 到期队列。

S4 已确认：完成/放弃后固定 20 自然日；打开/阅读/刷新不续期、不改
terminal_revision；只有明确状态更新回活跃（重启执行）才取消旧周期。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa import biztime as ret_mod
from mariposa.plans import service as plans
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




# ------------------------------------------------- 审计 2026-10-03
# 本文件此前只有 fixture/helper、收集 0 项——补真实生命周期用例，
# 让文件恢复收集与判分能力（完整语义矩阵仍待专项）。

class TestPlanLifecycle:

    def test_create_get_update_state(self, actors):
        p = plans.create("jiaming", "生命周期计划",
                         state="planned",
                         starts_at="2026-12-01T09:00:00+08:00")
        from mariposa import db
        with db.formal() as conn:
            got = plans.get(conn, p["plan_id"])
        assert got["title"] == "生命周期计划"
        assert got["state"] == "planned"

    def test_terminal_state_records_anchors(self, actors):
        from mariposa import db
        from mariposa.capabilities import registry
        p = plans.create("jiaming", "完成锚计划", state="active")
        with db.formal() as conn:
            cur = plans.get(conn, p["plan_id"])
        registry.invoke(
            actors["jiaming"], "plan.update",
            {"plan_id": p["plan_id"], "state": "done",
             "expected_version": cur["version"]}, None)
        with db.formal() as conn:
            got = plans.get(conn, p["plan_id"])
        assert got["state"] == "done"
        assert got["completed_at"], "终结必须留终结时间锚"
