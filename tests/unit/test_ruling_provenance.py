"""裁定（2026-10-04 江乔生）：provenance 来源收紧回归。

- 新写：source_msg:<id> 必须解析到当前已发布消息；raw_msg: 无正式
  raw registry，不再接受随手写入。
- 删除硬门：dangling provenance 不算有效引用，不得阻止删除。
- 读侧：legacy raw 前缀/不可解析来源标 gap，不伪装有效 provenance。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mariposa import db
from mariposa.errors import DeleteBlocked, Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.source import importer
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _seed_published_msg(prefix="pv") -> str:
    tmp = Path(tempfile.mkdtemp())
    f = tmp / f"{prefix}.json"
    f.write_text(json.dumps([{
        "uuid": f"c-{prefix}", "chat_messages": [{
            "uuid": f"{prefix}-m1", "sender": "human",
            "created_at": "2026-09-20T10:00:00.000Z",
            "content": [{"type": "text", "text": f"{prefix} 已发布原文"}]}]}],
        ensure_ascii=False), encoding="utf-8")
    r = importer.import_file("jiaming", str(f))
    assert r["status"] == "completed", r
    with db.formal() as conn:
        return conn.execute(
            "SELECT id FROM source_messages WHERE"
            " provider_message_id=? OR id=?",
            (f"{prefix}-m1", f"{prefix}-m1")).fetchone()["id"]


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-20",
                date_confidence="exact", original_title="pv",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


class TestWriteSideTightening:

    def test_raw_prefix_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            _hold(actors, "raw前缀话语", our_words=[
                {"speaker": "qiaosheng", "text": "随手写的raw引用",
                 "expression_kind": "verbatim",
                 "source_ref": "raw_msg:whatever"}])
        assert ei.value.code == "INVALID_SOURCE_REF"

    def test_dangling_source_msg_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            _hold(actors, "悬空引用话语", our_words=[
                {"speaker": "qiaosheng", "text": "指向不存在消息",
                 "expression_kind": "verbatim",
                 "source_ref": "source_msg:no-such-msg"}])
        assert ei.value.code == "INVALID_SOURCE_REF"

    def test_unpublished_source_rejected(self, actors):
        msg_id = _seed_published_msg("unpub")
        with db.formal() as conn:
            conn.execute("UPDATE source_messages SET published=0"
                         " WHERE id=?", (msg_id,))
        with pytest.raises(Forbidden):
            _hold(actors, "未发布引用话语", our_words=[
                {"speaker": "qiaosheng", "text": "指向未发布消息",
                 "expression_kind": "verbatim",
                 "source_ref": f"source_msg:{msg_id}"}])

    def test_published_source_accepted(self, actors):
        msg_id = _seed_published_msg("okref")
        out = _hold(actors, "合法引用话语", our_words=[
            {"speaker": "qiaosheng", "text": "指向已发布消息",
             "expression_kind": "verbatim",
             "source_ref": f"source_msg:{msg_id}"}])
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_our_words WHERE"
                " memory_id=? AND source_ref=?",
                (out["memory_id"], f"source_msg:{msg_id}")).fetchone()["c"]
        assert n == 1


class TestDeletionGateIgnoresDangling:

    def test_dangling_does_not_block_delete(self, actors):
        out = _hold(actors, "只有dangling引用的桶")
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                " speaker, text, expression_kind, source_ref, created_by,"
                " created_at) VALUES('ow-dangling-test', ?, 1,"
                " 'qiaosheng', 'legacy悬空引用', 'verbatim',"
                " 'raw_msg:legacy-xyz', 'jiaming', datetime('now'))",
                (out["memory_id"],))
        from mariposa.deletion import service as deletion
        deletion.direct_delete("jiaming", out["memory_id"])
        with db.formal() as conn:
            gone = conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["c"]
        assert gone == 0, "dangling provenance 不得阻止删除"

    def test_resolvable_source_still_blocks(self, actors):
        msg_id = _seed_published_msg("gate")
        out = _hold(actors, "有效引用的桶", our_words=[
            {"speaker": "qiaosheng", "text": "有效来源话语",
             "expression_kind": "verbatim",
             "source_ref": f"source_msg:{msg_id}"}])
        from mariposa.deletion import service as deletion
        with pytest.raises(DeleteBlocked) as ei:
            deletion.direct_delete("jiaming", out["memory_id"])
        assert ei.value.detail["references"].get("word_sources") == 1


class TestReadSideGapLabels:

    def _evidence_state(self, mid):
        from mariposa.retrieval.words import _word_evidence
        with db.formal() as conn:
            row = conn.execute(
                "SELECT * FROM memory_our_words WHERE memory_id=?",
                (mid,)).fetchone()
            return _word_evidence(row, conn)

    def test_legacy_raw_prefix_labels_gap(self, actors):
        out = _hold(actors, "legacy前缀读取")
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO memory_our_words(word_id, memory_id, ordinal,"
                " speaker, text, expression_kind, source_ref, created_by,"
                " created_at) VALUES('ow-legacy-read', ?, 1,"
                " 'qiaosheng', 'legacy话语', 'verbatim',"
                " 'raw_msg:old', 'jiaming', datetime('now'))",
                (out["memory_id"],))
        evs = self._evidence_state(out["memory_id"])
        states = [ev.get("structured_value", {}).get("source_ref_state")
                  for ev in evs if ev.get("field") == "our_words.source_ref"]
        assert states == ["legacy_raw_prefix"], evs
        kinds = [ev.get("evidence_kind") for ev in evs]
        assert "word_verbatim" not in kinds, "legacy 悬空不得签 verified"


class TestDecideDestructiveIdempotency:
    """裁定（2026-10-04）：破坏性决定与回执同事务——同 key 重试恢复
    同一结果，不重复执行删除；直删路径（atomic_write）已有同性质。"""

    def test_decide_same_key_replays_same_outcome(self, actors):
        from mariposa.capabilities import registry
        from mariposa.deletion import service as deletion
        out = _hold(actors, "决定幂等正文")
        mid = out["memory_id"]
        req = deletion.deletion_submit("qiaosheng", mid, "裁定测试",
                                       action="delete")
        args = {"request_id": req["request_id"], "decision": "approve",
                "operation_id": "dec-idem-1"}
        first = registry.invoke(actors["jiaming"],
                                "memory.deletion.decide", args, None)
        assert first["data"]["status"] == "approved"
        # 同 key 同载荷重试：重放同一决定结果，不 AlreadyDecided、
        # 不重复删除
        retry = registry.invoke(actors["jiaming"],
                                "memory.deletion.decide", dict(args),
                                None)
        assert retry["data"]["status"] == "approved"
        assert retry["data"].get("idempotent_replay") is True
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM audit_events WHERE"
                " event_type='memory.deleted' AND resource_id=?",
                (mid,)).fetchone()["c"]
        assert n == 1, "删除副作用必须恰一次"

    def test_decide_same_key_different_payload_conflicts(self, actors):
        from mariposa.capabilities import registry
        from mariposa.deletion import service as deletion
        from mariposa.errors import IdempotencyConflict
        out = _hold(actors, "决定冲突正文")
        req = deletion.deletion_submit("qiaosheng", out["memory_id"],
                                       "冲突测试", action="delete")
        registry.invoke(actors["jiaming"], "memory.deletion.decide",
                        {"request_id": req["request_id"],
                         "decision": "approve",
                         "operation_id": "dec-conf-1"}, None)
        with pytest.raises(IdempotencyConflict):
            registry.invoke(actors["jiaming"], "memory.deletion.decide",
                            {"request_id": req["request_id"],
                             "decision": "reject",
                             "rejection_reason": "x",
                             "operation_id": "dec-conf-1"}, None)

    def test_direct_delete_atomic_receipt_proof(self, actors):
        """直删（memory.delete）业务与回执同事务（atomic_write）——
        同 key 重放同一结果；删除恰一次。"""
        from mariposa.capabilities import registry
        out = _hold(actors, "直删原子回执正文")
        mid = out["memory_id"]
        args = {"memory_id": mid, "operation_id": "dd-idem-1"}
        r1 = registry.invoke(actors["jiaming"], "memory.delete",
                             args, None)
        assert r1["data"]["deleted"] is True
        r2 = registry.invoke(actors["jiaming"], "memory.delete",
                             dict(args), None)
        assert r2["data"]["deleted"] is True
        assert r2["data"].get("idempotent_replay") is True
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM audit_events WHERE"
                " event_type='memory.deleted' AND resource_id=?",
                (mid,)).fetchone()["c"]
        assert n == 1


class TestReadSideSafetySemantics:
    """裁定（2026-10-04）：模型可见读路径安全语义等价（形状不要求
    统一）。memory.get 补 gap；Bootstrap 补 content_role/权限语义。"""

    def test_memory_get_marks_legacy_summary_gap(self, actors):
        out = _hold(actors, "gap标注正文")
        mid = out["memory_id"]
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE memory_versions SET representation="
                    "'forgotten_summary', hold_text='旧摘要正文',"
                    " event_text=NULL WHERE memory_id=? AND version_no=1",
                    (mid,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        with db.formal() as conn:
            got = memory.get(conn, mid)
        assert got["content_role"] == "retrieved_memory"
        assert got["instruction_authority"] == "none"
        assert got["content_gap"]["code"] == "LEGACY_CONTENT_GAP"
        assert got["content_gap"]["reason"] == "retired_forgotten_summary"

    def test_memory_get_marks_empty_body_gap(self, actors):
        out = _hold(actors, "空正文gap")
        mid = out["memory_id"]
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE memory_versions SET hold_text=NULL,"
                    " event_text=NULL WHERE memory_id=? AND version_no=1",
                    (mid,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        with db.formal() as conn:
            got = memory.get(conn, mid)
        assert got["content_gap"]["code"] == "LEGACY_CONTENT_GAP"
        assert got["content_gap"]["reason"] == "empty_body"

    def test_normal_memory_has_no_gap(self, actors):
        out = _hold(actors, "正常正文无gap")
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])
        assert "content_gap" not in got

    def test_bootstrap_carries_safety_semantics(self, actors):
        from mariposa.bootstrap import service as boot
        pkg = boot.get("jiaming", "cc", "cc")
        assert pkg["instruction_authority"] == "none"
        assert pkg["content_role"] == "bootstrap_memory_package"
