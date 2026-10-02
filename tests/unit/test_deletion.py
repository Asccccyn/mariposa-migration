"""deletion（memory-only）：审批制删除的特征测试（synthetic fixtures）。

2026-10-01：信件拆出 mariposa（独立项目另行开发），删除申请流自
letters/service.py 迁至 deletion/service.py 并收窄为仅 memory。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.deletion import service as deletion
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.retrieval import search as retrieval
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


class TestDeletion:
    def _hold(self, actors, text="将被申请删除的测试记忆"):
        return memory.hold(actors["jiaming"], text=text,
                           memory_date="2026-06-01", date_confidence="exact", categories=["daily"])

    def test_full_flow_approve_archive(self, actors):
        hold = self._hold(actors)
        req = deletion.deletion_submit("qiaosheng", hold["memory_id"], "写错了日期",
                                       action="archive")
        assert req["status"] == "pending"
        out = deletion.deletion_decide("jiaming", req["request_id"], "approve",
                                       ai_reason="确认有误，同意归档")
        assert out["status"] == "approved"
        with db.formal() as conn:
            m = conn.execute("SELECT visibility FROM memories WHERE memory_id=?",
                             (hold["memory_id"],)).fetchone()
        assert m["visibility"] == "archived"
        assert retrieval.search  # 归档后无默认投影
        with db.formal() as conn:
            hits = retrieval.search(conn, "将被申请删除")["hits"]
        assert not hits

    def test_approve_delete_physical_with_audit(self, actors):
        hold = self._hold(actors)
        req = deletion.deletion_submit("qiaosheng", hold["memory_id"], "彻底删除这条",
                                       action="delete")
        deletion.deletion_decide("jiaming", req["request_id"], "approve")
        with db.formal() as conn:
            assert not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                                    (hold["memory_id"],)).fetchone()
            audit_rows = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE event_type="
                "'deletion.approved'").fetchone()["c"]
        assert audit_rows >= 1

    def test_reason_required(self, actors):
        hold = self._hold(actors)
        with pytest.raises(Forbidden):
            deletion.deletion_submit("qiaosheng", hold["memory_id"], "  ")

    def test_pending_exists(self, actors):
        hold = self._hold(actors)
        deletion.deletion_submit("qiaosheng", hold["memory_id"], "第一次")
        with pytest.raises(Forbidden) as e:
            deletion.deletion_submit("qiaosheng", hold["memory_id"], "第二次")
        assert e.value.detail.get("code") == "pending_exists"

    def test_withdraw_then_resubmit(self, actors):
        hold = self._hold(actors)
        deletion.deletion_submit("qiaosheng", hold["memory_id"], "先提交")
        w = deletion.deletion_withdraw("qiaosheng", hold["memory_id"])
        assert w["status"] == "withdrawn"
        again = deletion.deletion_submit("qiaosheng", hold["memory_id"], "再提交")
        assert again["status"] == "pending"

    def test_lifetime_limit(self, actors):
        hold = self._hold(actors)
        for i in range(deletion.LIFETIME_LIMIT):
            deletion.deletion_submit("qiaosheng", hold["memory_id"], f"r{i}")
            deletion.deletion_withdraw("qiaosheng", hold["memory_id"])
        with pytest.raises(Forbidden) as e:
            deletion.deletion_submit("qiaosheng", hold["memory_id"], "超限")
        assert e.value.detail.get("code") == "lifetime_limit"

    def test_daily_limit(self, actors):
        for i in range(deletion.DAILY_LIMIT):
            hold = self._hold(actors, text=f"日常限额测试 {i}")
            deletion.deletion_submit("qiaosheng", hold["memory_id"], "r")
        extra = self._hold(actors, text="第 11 条")
        with pytest.raises(Forbidden) as e:
            deletion.deletion_submit("qiaosheng", extra["memory_id"], "超每日")
        assert e.value.detail.get("code") == "daily_limit"

    def test_only_jiaming_decides(self, actors):
        hold = self._hold(actors)
        req = deletion.deletion_submit("qiaosheng", hold["memory_id"], "r")
        with pytest.raises(Forbidden):
            deletion.deletion_decide("qiaosheng", req["request_id"], "approve")
        with pytest.raises(Forbidden):
            deletion.deletion_decide("worker", req["request_id"], "approve")

    def test_superseded_when_target_inactive(self, actors):
        hold = self._hold(actors)
        req = deletion.deletion_submit("qiaosheng", hold["memory_id"], "r",
                                       action="archive")
        # 目标先被归档（另一请求路径）
        with db.formal() as conn:
            conn.execute("UPDATE memories SET visibility='archived' WHERE memory_id=?",
                         (hold["memory_id"],))
        with pytest.raises(Forbidden) as e:
            deletion.deletion_decide("jiaming", req["request_id"], "approve")
        assert e.value.detail.get("code") == "superseded"

    def test_expected_resource_mismatch(self, actors):
        hold = self._hold(actors)
        req = deletion.deletion_submit("qiaosheng", hold["memory_id"], "r")
        with pytest.raises(Forbidden) as e:
            deletion.deletion_decide("jiaming", req["request_id"], "approve",
                                     expected_resource_id="mem_other")
        assert e.value.detail.get("code") == "bucket_mismatch"

    def test_worker_cannot_request(self, actors):
        hold = self._hold(actors)
        # worker 经 registry 被拒（service 层不设主体限制，权限在注册表）
        from mariposa.capabilities import registry
        with pytest.raises(Forbidden):
            registry.invoke(actors["worker"], "memory.deletion.request",
                            {"resource_id": hold["memory_id"], "reason": "x"}, None)
