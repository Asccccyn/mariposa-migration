"""letters/deletion：按已核验旧规格的特征测试（synthetic fixtures）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.errors import Forbidden, LockedResource, NotFound
from mariposa.identity import service as identity
from mariposa.letters import service as letters
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


class TestLetters:
    def test_write_read_unlocked(self, actors):
        r = letters.write_letter("jiaming", "一封普通的信，写给未来的我们。",
                                 letter_date="2026-09-07")
        got = letters.read_letter("qiaosheng", r["letter_id"])
        assert "未来的我们" in got["content"]
        assert got["lock"] == {"locked": False, "lock_type": "none", "unlock_date": None}

    def test_metadata_only_list(self, actors):
        r = letters.write_letter("qiaosheng", "正文里有蓝瓷小钥匙这个词。")
        listed = letters.list_letters()
        item = next(x for x in listed if x["letter_id"] == r["letter_id"])
        assert "content" not in item and "蓝瓷小钥匙" not in str(item)
        assert item["author"] == "qiaosheng"

    def test_timed_lock_and_unlock(self, actors):
        """旧规格验收日期：2026-09-07 写锁、2027-07-09 解锁（合成正文）。"""
        r = letters.write_letter("jiaming", "上锁的信。", letter_date="2026-09-07",
                                 lock_type="timed", unlock_date="2027-07-09T00:00:00+08:00")
        with pytest.raises(LockedResource):
            letters.read_letter("qiaosheng", r["letter_id"])
        # 锁中列表仍可见元数据
        item = next(x for x in letters.list_letters() if x["letter_id"] == r["letter_id"])
        assert item["lock"]["locked"] is True
        # 到期：读时归一化解锁
        future = datetime.now(timezone.utc) + timedelta(days=1)
        letters.edit_letter("jiaming", r["letter_id"], 1, None,
                            lock_type="timed",
                            unlock_date=future.isoformat())
        with pytest.raises(LockedResource):
            letters.read_letter("qiaosheng", r["letter_id"])
        letters.edit_letter("jiaming", r["letter_id"], 1, None,
                            lock_type="timed", unlock_date="2020-01-01T00:00:00+00:00")
        got = letters.read_letter("qiaosheng", r["letter_id"])
        assert got["lock"]["locked"] is False and got["lock"]["expired"] is True

    def test_timed_lock_requires_date(self, actors):
        with pytest.raises(Forbidden):
            letters.write_letter("jiaming", "x", lock_type="timed")

    def test_only_author_edits(self, actors):
        r = letters.write_letter("jiaming", "只有我能改。")
        with pytest.raises(Forbidden):
            letters.edit_letter("qiaosheng", r["letter_id"], 1, "别人改的")
        u = letters.edit_letter("jiaming", r["letter_id"], 1, "我改的")
        assert u["version"] == 2
        with pytest.raises(Forbidden):
            letters.edit_letter("jiaming", r["letter_id"], 1, "旧版本再改")


class TestDeletion:
    def _hold(self, actors, text="将被申请删除的测试记忆"):
        return memory.hold(actors["jiaming"], text=text,
                           memory_date="2026-06-01", date_confidence="exact")

    def test_full_flow_approve_archive(self, actors):
        hold = self._hold(actors)
        req = letters.deletion_submit("qiaosheng", hold["memory_id"], "写错了日期",
                                      action="archive")
        assert req["status"] == "pending"
        out = letters.deletion_decide("jiaming", req["request_id"], "approve",
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
        req = letters.deletion_submit("qiaosheng", hold["memory_id"], "彻底删除这条",
                                      action="delete")
        letters.deletion_decide("jiaming", req["request_id"], "approve")
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
            letters.deletion_submit("qiaosheng", hold["memory_id"], "  ")

    def test_pending_exists(self, actors):
        hold = self._hold(actors)
        letters.deletion_submit("qiaosheng", hold["memory_id"], "第一次")
        with pytest.raises(Forbidden) as e:
            letters.deletion_submit("qiaosheng", hold["memory_id"], "第二次")
        assert e.value.detail.get("code") == "pending_exists"

    def test_withdraw_then_resubmit(self, actors):
        hold = self._hold(actors)
        letters.deletion_submit("qiaosheng", hold["memory_id"], "先提交")
        w = letters.deletion_withdraw("qiaosheng", hold["memory_id"])
        assert w["status"] == "withdrawn"
        again = letters.deletion_submit("qiaosheng", hold["memory_id"], "再提交")
        assert again["status"] == "pending"

    def test_lifetime_limit(self, actors):
        hold = self._hold(actors)
        for i in range(letters.LIFETIME_LIMIT):
            letters.deletion_submit("qiaosheng", hold["memory_id"], f"r{i}")
            letters.deletion_withdraw("qiaosheng", hold["memory_id"])
        with pytest.raises(Forbidden) as e:
            letters.deletion_submit("qiaosheng", hold["memory_id"], "超限")
        assert e.value.detail.get("code") == "lifetime_limit"

    def test_daily_limit(self, actors):
        for i in range(letters.DAILY_LIMIT):
            hold = self._hold(actors, text=f"日常限额测试 {i}")
            letters.deletion_submit("qiaosheng", hold["memory_id"], "r")
        extra = self._hold(actors, text="第 11 条")
        with pytest.raises(Forbidden) as e:
            letters.deletion_submit("qiaosheng", extra["memory_id"], "超每日")
        assert e.value.detail.get("code") == "daily_limit"

    def test_only_jiaming_decides(self, actors):
        hold = self._hold(actors)
        req = letters.deletion_submit("qiaosheng", hold["memory_id"], "r")
        with pytest.raises(Forbidden):
            letters.deletion_decide("qiaosheng", req["request_id"], "approve")
        with pytest.raises(Forbidden):
            letters.deletion_decide("worker", req["request_id"], "approve")

    def test_superseded_when_target_inactive(self, actors):
        hold = self._hold(actors)
        req = letters.deletion_submit("qiaosheng", hold["memory_id"], "r",
                                      action="archive")
        # 目标先被归档（另一请求路径）
        with db.formal() as conn:
            conn.execute("UPDATE memories SET visibility='archived' WHERE memory_id=?",
                         (hold["memory_id"],))
        with pytest.raises(Forbidden) as e:
            letters.deletion_decide("jiaming", req["request_id"], "approve")
        assert e.value.detail.get("code") == "superseded"

    def test_expected_resource_mismatch(self, actors):
        hold = self._hold(actors)
        req = letters.deletion_submit("qiaosheng", hold["memory_id"], "r")
        with pytest.raises(Forbidden) as e:
            letters.deletion_decide("jiaming", req["request_id"], "approve",
                                    expected_resource_id="mem_other")
        assert e.value.detail.get("code") == "bucket_mismatch"

    def test_worker_cannot_request(self, actors):
        hold = self._hold(actors)
        # worker 经 registry 被拒（service 层不设主体限制，权限在注册表）
        from mariposa.capabilities import registry
        with pytest.raises(Forbidden):
            registry.invoke(actors["worker"], "memory.deletion.request",
                            {"resource_id": hold["memory_id"], "reason": "x"}, None)
