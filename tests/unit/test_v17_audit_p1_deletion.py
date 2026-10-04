"""v1.7 审计整改第一阶段回归：F34/F38 删除状态机与引用完整性。

覆盖审计要求：
- approve/reject 并发只有一个落定（真实线程并发 + CAS）
- approved -> rejected / rejected -> approved 不允许
- 数据库状态与实际删除副作用一致（approved 且 memory 真删 / rejected 且保留）
- 无引用 memory 正常物理删除
- 有 I revision 关系 / Source 绑定 → 结构化 DELETE_BLOCKED_BY_RELATIONS，
  不裸 500；memory 与 relation 均保留；请求保持 pending
- 多类引用同时存在全部列出
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mariposa import db
from mariposa.errors import AlreadyDecided, DeleteBlocked, NotFound
from mariposa.identity import service as identity
from mariposa.identity_i import service as i_service
from mariposa.deletion import service as deletion
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold_v2(actors, text):
    return memory.hold(
        actors["jiaming"], text=text, memory_date="2026-09-01",
        date_confidence="exact", original_title="删除审计",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False)


def submit_delete(actors, mid):
    return deletion.deletion_submit(actors["qiaosheng"].principal_id, mid,
                                   "审计测试", action="delete")


def memory_exists(mid: str) -> bool:
    with db.formal() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM memories WHERE memory_id=?", (mid,)).fetchone())


class TestF38ApproveRejectConcurrency:
    def test_concurrent_decide_only_one_wins(self, actors):
        out = hold_v2(actors, "并发审批场景的正文")
        mid = out["memory_id"]
        req = submit_delete(actors, mid)
        gate = threading.Barrier(2, timeout=10)
        results = {}
        errors = {}

        def run(name, decision):
            gate.wait()
            try:
                results[name] = deletion.deletion_decide(
                    actors["jiaming"].principal_id,
                    req["request_id"], decision,
                    rejection_reason="并发测试拒绝")
            except Exception as e:  # noqa: BLE001
                errors[name] = e

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(run, "approve", "approve")
            f2 = ex.submit(run, "reject", "reject")
            f1.result(timeout=20), f2.result(timeout=20)

        assert len(results) == 1, f"并发决定应只有一个成功：{results}"
        assert len(errors) == 1
        loser_err = errors.popitem()[1]
        # 失败方可能落入两种结构化拒绝之一：CAS 冲突（事务内）或
        # 入口发现已非 pending（事务外读取时决定已落定）。
        assert isinstance(loser_err, (AlreadyDecided, NotFound)), loser_err
        # 状态与副作用一致：唯一落定方决定 memory 去留
        #（v2.0：decide 返回申请行，winner 身份不再从返回值判）
        with db.formal() as conn:
            status = conn.execute(
                "SELECT status FROM deletion_requests WHERE request_id=?",
                (req["request_id"],)).fetchone()["status"]
        if status == "approved":
            assert not memory_exists(mid), "approved 但物理删除未发生"
        else:
            assert status == "rejected"
            assert memory_exists(mid), "rejected 却删除了正文"

    def test_no_reversal_after_decision(self, actors):
        out = hold_v2(actors, "顺序审批场景的正文")
        mid = out["memory_id"]
        req = submit_delete(actors, mid)
        deletion.deletion_decide(actors["jiaming"].principal_id,
                                 req["request_id"], "reject",
                                 rejection_reason="不再处理")
        # v2.0：已决定的申请再决定=AlreadyDecided（原 NotFound 断言随
        # 申请行永久保留而更新）
        with pytest.raises(AlreadyDecided):
            deletion.deletion_decide(actors["jiaming"].principal_id,
                                     req["request_id"], "approve")

    def test_approve_deletes_unreferenced_memory(self, actors):
        out = hold_v2(actors, "无引用可正常删除的正文")
        mid = out["memory_id"]
        req = submit_delete(actors, mid)
        res = deletion.deletion_decide(actors["jiaming"].principal_id,
                                      req["request_id"], "approve")
        assert res["status"] == "approved"
        assert not memory_exists(mid)
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_versions WHERE"
                " memory_id=?", (mid,)).fetchone()["c"]
        assert n == 0


class TestF34ReferentialIntegrity:
    def test_i_revision_relation_blocks_delete(self, actors):
        out = hold_v2(actors, "被 I 引用的正文")
        mid = out["memory_id"]
        i_service.item_create(
            "jiaming", "一条引用了记忆的 I 内容",
            relations=[{"memory_id": mid, "relation_type": "related"}])
        req = submit_delete(actors, mid)
        with pytest.raises(DeleteBlocked) as ei:
            deletion.deletion_decide(actors["jiaming"].principal_id,
                                    req["request_id"],
                                    "approve")
        assert ei.value.http_status == 409
        assert ei.value.detail["references"].get("i_revision_relations") == 1
        # memory、I 关系都保留；请求回滚为 pending
        assert memory_exists(mid)
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM i_revision_memory_relations"
                " WHERE memory_id=?", (mid,)).fetchone()["c"]
            status = conn.execute(
                "SELECT status FROM deletion_requests WHERE request_id=?",
                (req["request_id"],)).fetchone()["status"]
        assert n == 1, "I revision 关系被误删"
        assert status == "pending"

    def test_source_binding_blocks_delete(self, actors):
        out = hold_v2(actors, "被 Source 绑定的正文")
        mid = out["memory_id"]
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO source_conversations(id, provider,"
                    " provider_conversation_id, first_import_batch_id,"
                    " last_import_batch_id) VALUES('sc-audit','claude',"
                    " 'conv-audit','sib-x','sib-x')")
                conn.execute(
                    "INSERT INTO source_messages(id, conversation_id,"
                    " provider, provider_conversation_id,"
                    " provider_message_id, raw_sender, normalized_sender,"
                    " text, sequence, import_batch_id)"
                    " VALUES('sm-a1','sc-audit','claude','conv-audit',"
                    " 'pm-a1','human','human','绑定源消息',1,'sib-x')")
                conn.execute(
                    "INSERT INTO memory_source_bindings(binding_id,"
                    " memory_id, conversation_id, start_message_id,"
                    " end_message_id, bind_confidence, created_by,"
                    " created_at) VALUES('msb-audit',?, 'sc-audit',"
                    " 'sm-a1','sm-a1','exact','jiaming',"
                    " '2026-09-01T00:00:00Z')", (mid,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        req = submit_delete(actors, mid)
        with pytest.raises(DeleteBlocked) as ei:
            deletion.deletion_decide(actors["jiaming"].principal_id,
                                    req["request_id"],
                                    "approve")
        assert ei.value.detail["references"].get("source_bindings") == 1
        assert memory_exists(mid)
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_source_bindings"
                " WHERE memory_id=?", (mid,)).fetchone()["c"]
        assert n == 1, "source 绑定被误删"

    def test_multiple_reference_types_all_listed(self, actors):
        out = hold_v2(actors, "被多重引用的正文")
        mid = out["memory_id"]
        i_service.item_create(
            "jiaming", "多重引用 I",
            relations=[{"memory_id": mid, "relation_type": "related"}])
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO source_conversations(id, provider,"
                    " provider_conversation_id, first_import_batch_id,"
                    " last_import_batch_id) VALUES('sc-audit2','claude',"
                    " 'conv-audit2','sib-y','sib-y')")
                conn.execute(
                    "INSERT INTO source_messages(id, conversation_id,"
                    " provider, provider_conversation_id,"
                    " provider_message_id, raw_sender, normalized_sender,"
                    " text, sequence, import_batch_id)"
                    " VALUES('sm-a2','sc-audit2','claude','conv-audit2',"
                    " 'pm-a2','human','human','第二绑定源消息',1,'sib-y')")
                conn.execute(
                    "INSERT INTO memory_source_bindings(binding_id,"
                    " memory_id, conversation_id, start_message_id,"
                    " end_message_id, bind_confidence, created_by,"
                    " created_at) VALUES('msb-audit2',?, 'sc-audit2',"
                    " 'sm-a2','sm-a2','exact','jiaming',"
                    " '2026-09-01T00:00:00Z')", (mid,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        req = submit_delete(actors, mid)
        with pytest.raises(DeleteBlocked) as ei:
            deletion.deletion_decide(actors["jiaming"].principal_id,
                                    req["request_id"],
                                    "approve")
        refs = ei.value.detail["references"]
        assert refs.get("i_revision_relations") == 1
        assert refs.get("source_bindings") == 1
        assert memory_exists(mid)

    def test_archive_retired_from_flow(self, actors):
        """v2.0 P-A01：archive 分支退役——approve 一律真删除路径，
        有关系时结构化拒绝（提示指向纠错，不再建议 archive）。"""
        out = hold_v2(actors, "归档退役场景")
        mid = out["memory_id"]
        from mariposa.memory import relations as rel
        other = hold_v2(actors, "另一桶")
        rel.link("jiaming", mid, other["memory_id"], "related_to")
        req = submit_delete(actors, mid)
        with pytest.raises(DeleteBlocked) as ei:
            deletion.deletion_decide(actors["jiaming"].principal_id,
                                     req["request_id"], "approve")
        assert "archive" not in str(ei.value)
        assert deletion.deletion_get(req["request_id"])["status"] == "pending"



class TestF09VectorDerivedCleanup:
    """F09（2026-10-03 审计 P2）：物理删除必须同事务清空
    memory_embeddings/word_embeddings 派生向量（合成向量，不加载模型）。"""

    def _seed_vectors(self, mid):
        from mariposa.retrieval import semantic, words_semantic
        with db.formal() as conn:
            semantic.ensure_schema(conn)
            words_semantic.ensure_schema(conn)
            wrow = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (mid,)).fetchone()
            conn.execute(
                "INSERT INTO memory_embeddings(memory_id, model, dim,"
                " projection_hash, vector, created_at)"
                " VALUES(?,?,?,?,?,datetime('now'))",
                (mid, "synthetic-model", 2, "ph-f09",
                 b"\x00" * 8))
            if wrow:
                conn.execute(
                    "INSERT INTO word_embeddings(word_id, model, dim,"
                    " word_fingerprint, vector, created_at)"
                    " VALUES(?,?,?,?,?,datetime('now'))",
                    (wrow["word_id"], "synthetic-model", 2, "wf-f09",
                     b"\x00" * 8))
            return wrow["word_id"] if wrow else None

    def _leftovers(self, mid, wid):
        with db.formal() as conn:
            me = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_embeddings WHERE"
                " memory_id=?", (mid,)).fetchone()["c"]
            we = (conn.execute(
                "SELECT COUNT(*) AS c FROM word_embeddings WHERE"
                " word_id=?", (wid,)).fetchone()["c"] if wid else 0)
            mem = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE memory_id=?",
                (mid,)).fetchone()["c"]
        return me, we, mem

    def test_approve_clears_vectors(self, actors):
        out = memory.hold(
            actors["jiaming"], text="approve 向量清理",
            memory_date="2026-09-01", date_confidence="exact",
            original_title="f09a", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "approve 向量话语",
                        "expression_kind": "verbatim"}])
        mid = out["memory_id"]
        wid = self._seed_vectors(mid)
        req = submit_delete(actors, mid)
        deletion.deletion_decide(actors["jiaming"].principal_id,
                                 req["request_id"], "approve")
        me, we, mem = self._leftovers(mid, wid)
        assert (me, we, mem) == (0, 0, 0), \
            f"approve 后派生向量残留：mem_emb={me} word_emb={we} memory={mem}"

    def test_direct_delete_clears_vectors(self, actors):
        out = memory.hold(
            actors["jiaming"], text="直删向量清理",
            memory_date="2026-09-02", date_confidence="exact",
            original_title="f09d", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "直删向量话语",
                        "expression_kind": "verbatim"}])
        mid = out["memory_id"]
        wid = self._seed_vectors(mid)
        deletion.direct_delete("jiaming", mid)
        me, we, mem = self._leftovers(mid, wid)
        assert (me, we, mem) == (0, 0, 0), \
            f"直删后派生向量残留：mem_emb={me} word_emb={we} memory={mem}"
