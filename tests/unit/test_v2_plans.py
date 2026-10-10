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


class TestPlanPromotionC2:
    """C 档选 2（她 2026-10-10 裁定）：plan.get/complete/cancel 从 v1
    薄实现转正主表——三动作不再仅有兼容层入口。"""

    def test_plan_get_complete_cancel_main_table(self):
        from mariposa.capabilities import registry as reg
        from mariposa.identity import service as identity
        from tests.conftest import reset_all
        reset_all()
        P = identity.Principal("qiaosheng", "q", "human", "web", "bq")
        J = identity.Principal("jiaming", "n", "agent", "cc", "bj")
        # 转正：主表描述非薄实现标记；薄层同名让位（REGISTRY 无重复注册路径）
        for cap in ("plan.get", "plan.complete", "plan.cancel"):
            assert not reg.REGISTRY[cap].description.startswith(
                "[v1.1"), f"{cap} 应为主表描述"
        c = reg.invoke(P, "plan.create",
                       {"title": "转正验收", "content": "正文"}, None)["data"]
        pid, ver = c["plan_id"], c["version"]
        g = reg.invoke(J, "plan.get", {"plan_id": pid}, None)["data"]
        assert g["title"] == "转正验收" and g["version"] == ver
        done = reg.invoke(P, "plan.complete",
                          {"plan_id": pid, "expected_version": ver},
                          None)["data"]
        assert done["state"] == "done" and done["version"] == ver + 1
        c2 = reg.invoke(P, "plan.create", {"title": "弃"}, None)["data"]
        x = reg.invoke(P, "plan.cancel",
                       {"plan_id": c2["plan_id"],
                        "expected_version": c2["version"]}, None)["data"]
        assert x["state"] == "cancelled"
        # schema 面：缺 expected_version 拒（转正后走真 schema 非薄层宽松）
        import pytest as _pytest
        from mariposa.errors import Forbidden as _F
        with _pytest.raises(_F) as ei:
            reg.invoke(P, "plan.complete", {"plan_id": pid}, None)
        assert ei.value.code == "SCHEMA_VIOLATION"
