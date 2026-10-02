"""Memory 删除（规格 v2.0 §7）：人类申请 + 周家明直删双路径。

旧 archive/审批审计/双主体申请语义测试随产品退役；本文件断言
目标语义（P-D01..P-D04）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.deletion import service as deletion
from mariposa.errors import AlreadyDecided, DeleteBlocked, Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory

pytestmark = pytest.mark.usefixtures("actors")


@pytest.fixture()
def actors():
    from tests.conftest import reset_all
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj"),
            "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                            "web", "bq")}


def _hold(actors, text="待删桶", **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="d",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


class TestHumanPath:
    def test_blank_reason_rejected(self, actors):
        out = _hold(actors)
        with pytest.raises(Forbidden):
            deletion.deletion_request("qiaosheng", out["memory_id"], "  ")

    def test_jiaming_cannot_request(self, actors):
        out = _hold(actors)
        with pytest.raises(Forbidden):
            deletion.deletion_request("jiaming", out["memory_id"], "r")

    def test_pending_unique_and_quota(self, actors):
        out = _hold(actors)
        rid = deletion.deletion_request(
            "qiaosheng", out["memory_id"], "r1", operation_key="k1")["request_id"]
        with pytest.raises(Forbidden):
            deletion.deletion_request("qiaosheng", out["memory_id"], "r2")
        # 同 key 重放返回同一申请（不重复计数）
        replay = deletion.deletion_request(
            "qiaosheng", out["memory_id"], "r1", operation_key="k1")
        assert replay["request_id"] == rid
        assert replay.get("idempotent_replay") is True
        deletion.deletion_withdraw("qiaosheng", rid)
        # 撤回不返还：再 4 次 = 一生 5 次后拒
        for i in range(2, 6):
            r = deletion.deletion_request(
                "qiaosheng", out["memory_id"], f"r{i}",
                operation_key=f"k{i}")
            deletion.deletion_withdraw("qiaosheng", r["request_id"])
        with pytest.raises(Forbidden) as ei:
            deletion.deletion_request("qiaosheng", out["memory_id"], "r6",
                                      operation_key="k6")
        assert ei.value.code == "QUOTA_EXCEEDED"

    def test_decide_reject_needs_reason(self, actors):
        out = _hold(actors)
        rid = deletion.deletion_request(
            "qiaosheng", out["memory_id"], "r", operation_key="k")["request_id"]
        with pytest.raises(Forbidden):
            deletion.deletion_decide("jiaming", rid, "reject")
        got = deletion.deletion_decide("jiaming", rid, "reject",
                                       rejection_reason="不是误记")
        assert got["status"] == "rejected" and got["rejection_reason"]

    def test_approve_deletes_and_keeps_request(self, actors):
        out = _hold(actors)
        rid = deletion.deletion_request(
            "qiaosheng", out["memory_id"], "r", operation_key="k")["request_id"]
        got = deletion.deletion_decide("jiaming", rid, "approve")
        assert got["status"] == "approved"
        with db.formal() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM memories WHERE"
                             " memory_id=?",
                             (out["memory_id"],)).fetchone()["c"]
        assert n == 0
        assert deletion.deletion_get(rid)["status"] == "approved"

    def test_relation_block_keeps_pending(self, actors):
        a = _hold(actors, "A")
        b = _hold(actors, "B")
        from mariposa.memory import relations as rel
        rel.link("jiaming", a["memory_id"], b["memory_id"], "related_to")
        rid = deletion.deletion_request(
            "qiaosheng", a["memory_id"], "r", operation_key="k")["request_id"]
        with pytest.raises(DeleteBlocked):
            deletion.deletion_decide("jiaming", rid, "approve")
        # 技术拦截不落 rejected：pending 保留，纠错后可再处理
        assert deletion.deletion_get(rid)["status"] == "pending"


class TestDirectPath:
    def test_direct_delete_no_reason_no_quota(self, actors):
        out = _hold(actors)
        r = deletion.direct_delete("jiaming", out["memory_id"])
        assert r["deleted"] is True
        # 该桶人类配额从未消耗也无所谓——直删路径没有配额概念

    def test_only_jiaming(self, actors):
        out = _hold(actors)
        with pytest.raises(Forbidden):
            deletion.direct_delete("qiaosheng", out["memory_id"])

    def test_relations_block_direct(self, actors):
        a = _hold(actors, "A")
        b = _hold(actors, "B")
        from mariposa.memory import relations as rel
        rel.link("jiaming", a["memory_id"], b["memory_id"], "related_to")
        with pytest.raises(DeleteBlocked) as ei:
            deletion.direct_delete("jiaming", a["memory_id"])
        assert "memory_relations" in ei.value.detail["references"]

    def test_direct_supersedes_pending(self, actors):
        out = _hold(actors)
        rid = deletion.deletion_request(
            "qiaosheng", out["memory_id"], "r", operation_key="k")["request_id"]
        deletion.direct_delete("jiaming", out["memory_id"])
        assert deletion.deletion_get(rid)["status"] == "superseded"

    def test_archive_retired(self, actors):
        out = _hold(actors)
        deletion.direct_delete("jiaming", out["memory_id"])
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE"
                " visibility='archived'").fetchone()["c"]
        assert n == 0
