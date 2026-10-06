"""接续复审（周家明 @77a0969）修复回归。

- NP1：Bootstrap Plan 共享 snapshot（list_plans conn 遮蔽）+
  next_page 校验/取页同事务（交错写入使旧 snapshot 拿不到新数据）。
- NP3：陈旧 view_receipt（版本过期）不得写 recollection/keep_wide。
- NP4：mood/tags/reengagement/open/pin 并发删除下结构化拒绝
  （无 FK 500、无假回执）。
- NP5：worker 在 maintenance profile 能拿到 maintenance 工具。
- NP6：by_emotion(whose=...) 不再 no such column。
- NP7：recollection 只能修订当前 leaf（不分叉）。
"""
from __future__ import annotations

import threading

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, NotFound, ViewReceiptInvalid
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.memory import listing, recollections
from mariposa.bootstrap import service as boot
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="np",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


# ---------------------------------------------------------------- NP1

class TestBootstrapPlanSnapshot:

    def test_list_plans_uses_caller_connection(self, actors, monkeypatch):
        """NP1：list_plans(conn=) 不得开第二个连接。"""
        from mariposa.plans import service as plans
        opened = []
        import mariposa.db as dbm
        conn = dbm._connect(str(dbm.config.FORMAL_DB))
        real = dbm._connect
        try:
            def counting_connect(*a, **kw):
                opened.append(1)
                return real(*a, **kw)
            monkeypatch.setattr("mariposa.db._connect", counting_connect)
            plans.list_plans(conn=conn)
            assert not opened, \
                "传入 conn 时不得再开新连接（此前 with db.formal() 遮蔽参数）"
        finally:
            conn.close()

    def test_next_page_single_connection(self, actors, monkeypatch):
        """NP2：next_page 全程只开一个连接——校验与取页同一读事务
        （此前校验连接先关、取页重开，交错窗口旧 snapshot 拿新数据；
        现在读事务快照内后续写入不可见，不再返回新数据）。"""
        from mariposa.plans import service as plans_mod
        _hold(actors, "开窗正文甲")
        plans_mod.create("jiaming", "P甲", state="active")
        b1 = boot.get("jiaming", "claude_chat", "claude_chat")
        opened = []
        import mariposa.db as dbm
        real = dbm._connect

        def counting_connect(*a, **kw):
            opened.append(1)
            return real(*a, **kw)
        monkeypatch.setattr("mariposa.db._connect", counting_connect)
        out = boot.next_page("jiaming", "claude_chat",
                             b1["snapshot_id"],
                             {"plans_offset": 0}, "plans")
        assert len(opened) == 1, \
            f"next_page 应恰好一个连接（校验+取页同事务）：{len(opened)}"
        assert out["items"], "前置：有 plan 在窗口内"


# ---------------------------------------------------------------- NP3

class TestStaleReceipt:

    def test_stale_receipt_rejected_for_recollection(self, actors):
        m = _hold(actors, "票据版本正文")
        opened = registry.invoke(actors["jiaming"], "memory.open",
                                 {"memory_id": m["memory_id"]}, None)
        receipt = opened["data"]["view_receipt"]
        registry.invoke(actors["jiaming"], "memory.view.confirm", {
            "memory_id": m["memory_id"], "receipt_id": receipt}, None)
        # Memory 更新到 v2 → v1 票据过期
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": m["memory_id"], "expected_version": 1,
            "text": "新版正文"}, None)
        with pytest.raises(ViewReceiptInvalid):
            registry.invoke(actors["jiaming"], "memory.recollections.append", {
                "memory_id": m["memory_id"], "receipt_id": receipt,
                "text": "陈旧票据的回忆", "keep_wide": True}, None)
        # keep_wide 也不得生效
        from mariposa.memory import keep as keep_mod
        assert not keep_mod.marks_of(m["memory_id"])

    def test_fresh_receipt_after_update_allows_append(self, actors):
        """F01 正例：更新到 v2 后重新 open+confirm，追加回忆必须成功。

        票据绑定的版本事实必须与追加校验同一维度——重开拿到的是
        当前内容的合法票据，不能再被表示版本/内容版本混比误拒。
        """
        m = _hold(actors, "票据版本正文一")
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": m["memory_id"], "expected_version": 1,
            "text": "票据版本正文二"}, None)
        opened = registry.invoke(actors["jiaming"], "memory.open", {
            "memory_id": m["memory_id"]}, None)
        assert opened["data"]["version"] == 2
        registry.invoke(actors["jiaming"], "memory.view.confirm", {
            "memory_id": m["memory_id"],
            "receipt_id": opened["data"]["view_receipt"]}, None)
        out = registry.invoke(actors["jiaming"], "memory.recollections.append", {
            "memory_id": m["memory_id"],
            "receipt_id": opened["data"]["view_receipt"],
            "text": "重开后的合法回忆", "keep_wide": True}, None)
        assert out["data"]["recollection_id"]
        from mariposa.memory import keep as keep_mod
        assert keep_mod.marks_of(m["memory_id"]), "合法票据的 keep_wide 应生效"

    def test_unconfirmed_old_receipt_rejected_after_update(self, actors):
        """F01 反例：v1 打开未确认 → 内容更新 v2 → 旧票据不得 confirm。

        否则旧票据会给 v2 内容写入明开回温事实（把没看过的内容
        当看过）。
        """
        m = _hold(actors, "未确认票据正文")
        opened = registry.invoke(actors["jiaming"], "memory.open",
                                 {"memory_id": m["memory_id"]}, None)
        receipt = opened["data"]["view_receipt"]
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": m["memory_id"], "expected_version": 1,
            "text": "更新后的正文"}, None)
        with pytest.raises(ViewReceiptInvalid):
            registry.invoke(actors["jiaming"], "memory.view.confirm", {
                "memory_id": m["memory_id"], "receipt_id": receipt}, None)
        with db.formal() as conn:
            row = conn.execute(
                "SELECT last_explicit_open_at FROM memories WHERE"
                " memory_id=?", (m["memory_id"],)).fetchone()
        assert row["last_explicit_open_at"] is None, \
            "未看过 v2 就确认成功会伪造明开事实"


# ---------------------------------------------------------------- NP4

class TestLockInChecks:

    def test_tags_add_after_delete_structured(self, actors, monkeypatch):
        """锁外检查窗口遇删除 → 结构化 NotFound（非 FK 500）。
        （mood.write 已按 2026-10-05 终裁删除——心情不能补写。）"""
        m = _hold(actors, "心情锁内检查")
        with db.formal() as c2:
            c2.execute("PRAGMA foreign_keys=OFF")
            c2.execute("DELETE FROM memory_our_words WHERE memory_id=?",
                       (m["memory_id"],))
            c2.execute("DELETE FROM memories WHERE memory_id=?",
                       (m["memory_id"],))
        with pytest.raises(NotFound):
            listing.tags_add("jiaming", m["memory_id"], ["t"])

    def test_pin_deleted_memory_no_fake_success(self, actors):
        m = _hold(actors, "假回执检查")
        with db.formal() as c2:
            c2.execute("PRAGMA foreign_keys=OFF")
            c2.execute("DELETE FROM memories WHERE memory_id=?",
                       (m["memory_id"],))
        with pytest.raises(NotFound):
            registry.invoke(actors["jiaming"], "memory.pin",
                            {"memory_id": m["memory_id"], "value": True},
                            None)


# ---------------------------------------------------------------- NP5

class TestMaintenanceWorkerAccess:

    def test_worker_can_call_maintenance_tools(self, actors):
        out = registry.invoke(actors["worker"],
                              "maintenance.jobs.status", {}, None)
        assert out["ok"] is True, \
            "worker 必须能拿到 maintenance 工具（此前死入口）"
        out2 = registry.invoke(actors["worker"],
                               "maintenance.settings.get", {}, None)
        assert out2["ok"] is True


# ---------------------------------------------------------------- NP6

class TestByEmotionWhose:

    def test_whose_param_no_sql_error(self, actors):
        _hold(actors, "心情查询正文",
              mood={"text": "安心", "tags": ["安心"]})
        out = listing.by_emotion("安心", whose="jiaming")
        assert isinstance(out["items"], list), \
            "whose 参数不得触发 no such column（NP6）"
        assert out["items"], "有心情标签的桶应命中"


# ---------------------------------------------------------------- NP7

class TestRecollectionLeafOnly:

    def test_revise_superseded_rejected(self, actors):
        m = _hold(actors, "回忆分叉正文")
        opened = registry.invoke(actors["jiaming"], "memory.open",
                                 {"memory_id": m["memory_id"]}, None)
        receipt = opened["data"]["view_receipt"]
        registry.invoke(actors["jiaming"], "memory.view.confirm", {
            "memory_id": m["memory_id"], "receipt_id": receipt}, None)
        r1 = registry.invoke(actors["jiaming"],
                             "memory.recollections.append", {
                                 "memory_id": m["memory_id"],
                                 "receipt_id": receipt,
                                 "text": "第一版回忆"}, None)["data"]
        r2 = registry.invoke(actors["jiaming"],
                             "memory.recollections.revise", {
                                 "recollection_id":
                                     r1["recollection_id"],
                                 "text": "第二版回忆"}, None)
        assert r2["ok"] is True
        # 对已被取代的 r1 再修 → 拒绝（防分叉）
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"],
                            "memory.recollections.revise", {
                                "recollection_id": r1["recollection_id"],
                                "text": "分叉的第二版"}, None)
        current = recollections.list_for(m["memory_id"])
        assert len([c for c in current]) <= 2
