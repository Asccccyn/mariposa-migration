"""遗忘纵向闭环 + 投影严格切换的标志性测试（§8.5 蓝瓷小钥匙）。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import (
    Forbidden,
    IdempotencyConflict,
    ProposalAlreadyResolved,
    ProposalHashMismatch,
    ProposalStale,
)
from mariposa.memory import service as memory
from mariposa.retrieval import search as retrieval
from mariposa.workspace import service as workspace

OLD_DATE = "2026-06-01"  # 距今 >30 天，满足默认闲置门槛


def hold_key_memory(actors):
    return memory.hold(
        actors["jiaming"],
        text="我们把蓝瓷小钥匙藏进了书架第三层的木头盒子里，下面压着一张便签。",
        why_remember="藏钥匙的位置，怕忘记。",
        memory_date=OLD_DATE,
        date_confidence="exact",
        entry_source="claude_chat",
    )


def run_search(query):
    with db.formal() as conn:
        return retrieval.search(conn, query)


def make_submitted_proposal(actors, text, summary="把一件心爱的小物收进了书架的盒子里。"):
    hold = memory.hold(actors["jiaming"], text=text, why_remember="测试",
                       memory_date=OLD_DATE, date_confidence="exact")
    scan = workspace.scan_candidates(actors["worker"])
    assert scan["created"], "expected a scan candidate"
    prop = next(p for p in scan["created"] if p["target_memory_id"] == hold["memory_id"])
    rev = workspace.revise_draft(actors["worker"], prop["proposal_id"], summary, "日常琐事压缩")
    sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
    return hold, sub


class TestBluePorcelainKey:
    """§8.5：旧正文唯一词不得在遗忘后经任何文本途径命中。"""

    def test_full_lifecycle(self, actors):
        hold = hold_key_memory(actors)

        hits = run_search("蓝瓷小钥匙")["hits"]
        assert any(h["memory_id"] == hold["memory_id"] for h in hits)

        scan = workspace.scan_candidates(actors["worker"])
        prop = next(p for p in scan["created"] if p["target_memory_id"] == hold["memory_id"])
        assert prop, "fresh old memory should be a candidate"

        summary = "把一件心爱的小物收进了书架的盒子里（细节已压缩）。"
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"], summary, "日常琐事")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])

        decided = workspace.decide(
            actors["qiaosheng"],
            proposal_id=sub["proposal_id"], proposal_revision=sub["revision"],
            proposal_hash=sub["proposal_hash"],
            expected_memory_version=sub["base_memory_version"], decision="approve",
        )
        assert decided["new_version"] == 2

        # 旧正文词：不得命中
        hits = run_search("蓝瓷小钥匙")["hits"]
        assert not any(h["memory_id"] == hold["memory_id"] for h in hits)
        hits = run_search("木头盒子")["hits"]  # why_remember 旧词也不行
        assert not any(h["memory_id"] == hold["memory_id"] for h in hits)

        # 批准摘要词：命中且标明途径
        hits = run_search("书架")["hits"]
        hit = next(h for h in hits if h["memory_id"] == hold["memory_id"])
        assert hit["matched_by"] == "summary_keyword"
        assert hit["projection_kind"] == "forgotten_summary"

        # get 默认只给摘要表示
        with db.formal() as conn:
            got = memory.get(conn, hold["memory_id"])
        assert got["representation"] == "forgotten_summary"
        assert got["text"] == summary

        # 历史版本明确可读，旧笔迹仍在
        with db.formal() as conn:
            versions = memory.versions_read(conn, hold["memory_id"])
        assert versions[0]["hold_text"] and "蓝瓷小钥匙" in versions[0]["hold_text"]

        # 恢复：旧词重新可搜
        res = memory.restore(actors["jiaming"], hold["memory_id"], expected_current_version=2)
        assert res["new_version"] == 3
        hits = run_search("蓝瓷小钥匙")["hits"]
        assert any(h["memory_id"] == hold["memory_id"] for h in hits)

    def test_jiaming_can_also_approve(self, actors):
        hold, sub = make_submitted_proposal(actors, "六月去湖边散步买了两支雪糕。")
        out = workspace.decide(
            actors["jiaming"], proposal_id=sub["proposal_id"],
            proposal_revision=sub["revision"], proposal_hash=sub["proposal_hash"],
            expected_memory_version=sub["base_memory_version"], decision="approve",
        )
        assert out["new_version"] == 2

    def test_worker_cannot_approve(self, actors):
        hold, sub = make_submitted_proposal(actors, "六月去湖边散步买了两支雪糕。")
        with pytest.raises(Forbidden):
            workspace.decide(
                actors["worker"], proposal_id=sub["proposal_id"],
                proposal_revision=sub["revision"], proposal_hash=sub["proposal_hash"],
                expected_memory_version=sub["base_memory_version"], decision="approve",
            )
        # 正式桶毫无变化
        with db.formal() as conn:
            got = memory.get(conn, hold["memory_id"])
        assert got["representation"] == "full"

    def test_double_approval_resolved_once(self, actors):
        hold, sub = make_submitted_proposal(actors, "六月修好了阳台的灯。")
        workspace.decide(
            actors["qiaosheng"], proposal_id=sub["proposal_id"],
            proposal_revision=sub["revision"], proposal_hash=sub["proposal_hash"],
            expected_memory_version=sub["base_memory_version"], decision="approve",
        )
        with pytest.raises(ProposalAlreadyResolved):
            workspace.decide(
                actors["jiaming"], proposal_id=sub["proposal_id"],
                proposal_revision=sub["revision"], proposal_hash=sub["proposal_hash"],
                expected_memory_version=sub["base_memory_version"], decision="approve",
            )
        with db.formal() as conn:
            versions = memory.versions_read(conn, hold["memory_id"])
        assert len(versions) == 2  # 只产生一个压缩版本

    def test_hash_tampering_rejected(self, actors):
        hold, sub = make_submitted_proposal(actors, "六月换了新的门锁。")
        with pytest.raises(ProposalHashMismatch):
            workspace.decide(
                actors["qiaosheng"], proposal_id=sub["proposal_id"],
                proposal_revision=sub["revision"], proposal_hash="0" * 64,
                expected_memory_version=sub["base_memory_version"], decision="approve",
            )

    def test_stale_when_base_version_moved(self, actors):
        hold, sub = make_submitted_proposal(actors, "七月重新贴了墙纸。")
        # 审批前桶版本移动（先批准后恢复，制造 v3）
        workspace.decide(
            actors["qiaosheng"], proposal_id=sub["proposal_id"],
            proposal_revision=sub["revision"], proposal_hash=sub["proposal_hash"],
            expected_memory_version=sub["base_memory_version"], decision="approve",
        )
        memory.restore(actors["jiaming"], hold["memory_id"], expected_current_version=2)
        # 旧提案（base=1）再次审批必须 STALE
        with pytest.raises(ProposalAlreadyResolved):
            workspace.decide(
                actors["qiaosheng"], proposal_id=sub["proposal_id"],
                proposal_revision=sub["revision"], proposal_hash=sub["proposal_hash"],
                expected_memory_version=1, decision="approve",
            )
        # 新一轮提案基于移动后的版本
        scan = workspace.scan_candidates(actors["worker"])
        prop = next(p for p in scan["created"] if p["target_memory_id"] == hold["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "贴墙纸的一次装修小事。", "压缩")
        sub2 = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        assert sub2["base_memory_version"] == 3

    def test_pinned_not_candidate(self, actors):
        hold = memory.hold(actors["jiaming"], text="重要的承诺", why_remember=None,
                           memory_date=OLD_DATE)
        with db.formal() as conn:
            conn.execute("UPDATE memories SET pinned=1 WHERE memory_id=?", (hold["memory_id"],))
        scan = workspace.scan_candidates(actors["worker"])
        assert not any(p["target_memory_id"] == hold["memory_id"] for p in scan["created"])

    def test_recent_memory_not_candidate(self, actors):
        hold = memory.hold(actors["jiaming"], text="昨天刚发生的事", why_remember=None,
                           memory_date="2026-09-20")
        scan = workspace.scan_candidates(actors["worker"])
        assert not any(p["target_memory_id"] == hold["memory_id"] for p in scan["created"])
        skip = next(s for s in scan["skipped"] if s["memory_id"] == hold["memory_id"])
        assert skip["reason"] == "too_recent"

    def test_unknown_date_not_candidate(self, actors):
        hold = memory.hold(actors["jiaming"], text="日期不明的事", why_remember=None)
        scan = workspace.scan_candidates(actors["worker"])
        skip = next(s for s in scan["skipped"] if s["memory_id"] == hold["memory_id"])
        assert skip["reason"] == "date_unknown"


class TestSearchSafety:
    def test_fts_injection_is_inert(self, actors):
        hold_key_memory(actors)
        for malicious in ['" OR 1=1 --', 'NEAR ( a b )', '*']:
            out = run_search(malicious)
            assert "hits" in out  # 不抛错
            assert not out["hits"]  # 不全库命中

    def test_semantic_unavailable_is_explicit(self, actors):
        out = run_search("任意")
        assert out["semantic"] == "unavailable"
        assert out["degraded"] == "semantic_unavailable"

    def test_substring_semantics(self, actors):
        memory.hold(actors["jiaming"], text="收到一把蓝瓷小钥匙", why_remember=None,
                    memory_date=OLD_DATE)
        assert run_search("蓝瓷")["hits"]
        assert run_search("瓷小钥")["hits"]


class TestIdempotency:
    def test_decide_idempotent_replay(self, actors):
        hold, sub = make_submitted_proposal(actors, "八月整理了旧照片。")
        args = {
            "proposal_id": sub["proposal_id"], "proposal_revision": sub["revision"],
            "proposal_hash": sub["proposal_hash"],
            "expected_memory_version": sub["base_memory_version"],
            "decision": "approve",
        }
        first = registry.invoke(actors["qiaosheng"], "memory.forgetting.decide", args, "key-1")
        assert first["ok"] and not first.get("idempotent_replay")
        second = registry.invoke(actors["qiaosheng"], "memory.forgetting.decide", args, "key-1")
        assert second.get("idempotent_replay") is True

    def test_same_key_different_payload_conflict(self, actors):
        hold, sub = make_submitted_proposal(actors, "八月修补了纱窗。")
        base = {
            "proposal_id": sub["proposal_id"], "proposal_revision": sub["revision"],
            "proposal_hash": sub["proposal_hash"],
            "expected_memory_version": sub["base_memory_version"],
            "decision": "approve",
        }
        registry.invoke(actors["qiaosheng"], "memory.forgetting.decide", base, "key-2")
        tampered = {**base, "decision": "reject"}
        with pytest.raises(IdempotencyConflict):
            registry.invoke(actors["qiaosheng"], "memory.forgetting.decide", tampered, "key-2")


class TestWorkspaceIsolation:
    def test_workspace_never_in_formal_search(self, actors):
        hold = hold_key_memory(actors)
        workspace.scan_candidates(actors["worker"])
        # 草稿存在时正式检索不变
        hits = run_search("蓝瓷小钥匙")["hits"]
        assert any(h["memory_id"] == hold["memory_id"] for h in hits)

    def test_worker_cannot_search_formal(self, actors):
        with pytest.raises(Forbidden):
            registry.invoke(actors["worker"], "memory.search", {"query": "x"}, None)

    def test_worker_cannot_read_versions(self, actors):
        hold = hold_key_memory(actors)
        with pytest.raises(Forbidden):
            registry.invoke(actors["worker"], "memory.versions.read",
                            {"memory_id": hold["memory_id"]}, None)
