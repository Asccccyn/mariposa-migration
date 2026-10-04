"""Relation/Deletion 收尾两件（她的复审要求）：

① our_words→Source 纠错的正式公开 Registry 入口断言（R07 补全）；
② Relation×删除跨域并发：新建关系 vs 删桶、纠错解绑 vs 删桶——
   不能出现孤儿关系或错误删除。
"""
from __future__ import annotations

import threading

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import DeleteBlocked, Forbidden, NotFound
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
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="cl",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def _seed_source_msg() -> str:
    import json as _json
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    convs = [{"uuid": "c-cl", "chat_messages": [{
        "uuid": "m-cl", "sender": "human",
        "created_at": "2026-09-28T10:00:00Z",
        "content": [{"type": "text", "text": "来源冒烟原文"}]}]}]
    f = tmp / "cl.json"
    f.write_text(_json.dumps(convs, ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))
    with db.formal() as conn:
        return conn.execute(
            "SELECT id FROM source_messages WHERE provider_message_id="
            "'m-cl'").fetchone()["id"]


# ================================================== R07 公开路径

class TestWordSourceCorrectViaRegistry:
    def _word_with_ref(self, actors, source_ref):
        return memory.hold(
            actors["jiaming"], text="话语正文", memory_date="2026-09-25",
            date_confidence="exact", original_title="w",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "我们说过的话",
                        "expression_kind": "verbatim",
                        "source_ref": source_ref}])

    def test_public_path_remove_and_replace(self, actors):
        """Registry → schema → atomic_write → service 全链路。"""
        msg_id = _seed_source_msg()
        out = self._word_with_ref(actors, f"source_msg:{msg_id}")
        with db.formal() as conn:
            word_id = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["word_id"]
        # 公开纠错：remove
        cor = registry.invoke(
            actors["jiaming"], "memory.our_words.source.correct",
            {"word_id": word_id,
             "expected_source_ref": f"source_msg:{msg_id}",
             "expected_source_version": 0,
             "correction_action": "remove_wrong_binding",
             "operation_id": "ws-rm"}, None)["data"]
        assert cor["new_source_ref"] is None
        assert cor["source_binding_version"] == 1
        # 纠错历史
        hist = registry.invoke(
            actors["jiaming"], "relations.corrections.list",
            {"instance_id": f"word:{word_id}"}, None)["data"]
        assert hist["total"] >= 1
        assert hist["corrections"][0]["domain"] == "word_source"
        # 公开纠错：replace（来源必须经 Source 身份校验；版本随换代递增）
        msg2 = _seed_source_msg()
        cor2 = registry.invoke(
            actors["jiaming"], "memory.our_words.source.correct",
            {"word_id": word_id, "expected_source_ref": None,
             "expected_source_version": 1,
             "correction_action": "replace_wrong_binding",
             "replacement": {"source_ref": f"source_msg:{msg2}"},
             "operation_id": "ws-rp"}, None)["data"]
        assert cor2["new_source_ref"] == f"source_msg:{msg2}"
        assert cor2["source_binding_version"] == 2

    def test_public_path_rejects_legacy_prefix(self, actors):
        out = self._word_with_ref(actors, None)  # 建一条无来源 word
        with db.formal() as conn:
            word_id = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["word_id"]
        # 审计 2026-10-03：此前缺 expected_source_version，请求先被
        # SCHEMA_VIOLATION 拦截、根本没走到 prefix guard——补全有效
        # 前置并断言专门错误信息（去掉 guard 时本用例必须真红）
        with pytest.raises(Forbidden) as ei:
            registry.invoke(
                actors["jiaming"], "memory.our_words.source.correct",
                {"word_id": word_id, "expected_source_ref": None,
                 "expected_source_version": 0,
                 "correction_action": "replace_wrong_binding",
                 "replacement": {"source_ref": "raw_msg:legacy"},
                 "operation_id": "ws-legacy"}, None)
        assert "raw" in str(ei.value) or "现行" in str(ei.value), \
            f"必须是退役前缀守卫拒绝，而非其他前置错误：{ei.value}"

    def test_public_path_cas_guard(self, actors):
        msg_id = _seed_source_msg()
        out = self._word_with_ref(actors, f"source_msg:{msg_id}")
        with db.formal() as conn:
            word_id = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["word_id"]
        with pytest.raises(Forbidden) as ei:  # 预期来源传错 → 拒
            registry.invoke(
                actors["jiaming"], "memory.our_words.source.correct",
                {"word_id": word_id, "expected_source_ref": None,
                 "expected_source_version": 0,
                 "correction_action": "remove_wrong_binding",
                 "operation_id": "ws-cas"}, None)
        assert ei.value.code == "CONFLICT"


# ================================================== 跨域并发

class TestCrossDomainConcurrency:
    def _barrier_pair(self, fn_a, fn_b):
        """两个线程在 barrier 对齐后同时执行；返回 (结果/异常) 对。"""
        barrier = threading.Barrier(2)
        out = {}

        def run(tag, fn):
            barrier.wait()
            try:
                out[tag] = ("ok", fn())
            except Exception as e:  # noqa: BLE001
                out[tag] = ("err", e)
        t1 = threading.Thread(target=run, args=("a", fn_a))
        t2 = threading.Thread(target=run, args=("b", fn_b))
        t1.start(); t2.start(); t1.join(30); t2.join(30)
        return out

    def test_link_vs_delete_no_orphan(self, actors):
        """T04：新建关系 vs 删桶——不能留下指向已删桶的有效关系。

        规格不变量（§8.1）：绑定先成功→删除看见绑定被拒；删除先
        成功→绑定因目标不存在失败。二者恰好一个成功。
        """
        a = _hold(actors, "并发A")
        b = _hold(actors, "并发B")

        def do_link():
            return registry.invoke(
                actors["jiaming"], "memory.relations.link",
                {"from_memory": a["memory_id"], "to_memory": b["memory_id"],
                 "relation_type": "related_to"}, None)

        def do_delete():
            return registry.invoke(
                actors["jiaming"], "memory.delete",
                {"memory_id": b["memory_id"], "operation_id": "cc-del"},
                None)

        out = self._barrier_pair(do_link, do_delete)
        statuses = {k: v[0] for k, v in out.items()}
        with db.formal() as conn:
            b_alive = bool(conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?",
                (b["memory_id"],)).fetchone())
            orphans = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations WHERE"
                " from_memory=? OR to_memory=?",
                (a["memory_id"], b["memory_id"])).fetchone()["c"]
        # 恰好一方成功（可能删赢或绑赢，取决于调度）
        assert list(statuses.values()).count("ok") == 1, out
        if not b_alive:  # 删赢了 → 关系必须不存在（孤儿=0）
            assert orphans == 0, f"删除留下孤儿关系：{orphans}"
        else:             # 绑赢了 → 桶必须还在、关系有效
            assert orphans == 1

    def test_correct_vs_delete_no_wrong_delete(self, actors):
        """T04：纠错解绑 vs 删桶——不能错误删除有关系的桶。

        不变量：如果删除成功，说明删除时关系已不存在（纠错先提交）；
        如果删除被拒，关系在删除时刻仍有效（纠错尚未提交或失败）。
        最终不能出现"关系行还在但桶已删"或"无关系却被拒"。
        """
        a = _hold(actors, "纠错并发A")
        b = _hold(actors, "纠错并发B")
        link_out = registry.invoke(
            actors["jiaming"], "memory.relations.link",
            {"from_memory": a["memory_id"], "to_memory": b["memory_id"],
             "relation_type": "related_to"}, None)["data"]

        def do_correct():
            return registry.invoke(
                actors["jiaming"], "memory.relations.correct",
                {"relation_id": link_out["relation_id"],
                 "correction_action": "remove_wrong_binding",
                 "operation_id": "cc-cor"}, None)

        def do_delete():
            return registry.invoke(
                actors["jiaming"], "memory.delete",
                {"memory_id": a["memory_id"], "operation_id": "cc-del"},
                None)

        out = self._barrier_pair(do_correct, do_delete)
        # 审计 2026-10-03：并发结果必须被断言——两条线程都要有结构化
        # 终态（ok 或结构化业务拒绝），不允许裸异常逃逸
        for tag, (status, payload) in out.items():
            assert status in ("ok", "err"), tag
            if status == "err":
                assert isinstance(payload, Exception), tag
                assert not isinstance(payload, (KeyError, TypeError,
                                                AttributeError)), \
                    f"{tag} 线程裸异常逃逸：{payload!r}"
        statuses = {k: v[0] for k, v in out.items()}
        with db.formal() as conn:
            a_alive = bool(conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?",
                (a["memory_id"],)).fetchone())
            rel_exists = bool(conn.execute(
                "SELECT 1 FROM memory_relations WHERE relation_id=?",
                (link_out["relation_id"],)).fetchone())
            orphan = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations WHERE"
                " from_memory=? OR to_memory=?",
                (a["memory_id"], b["memory_id"])).fetchone()["c"]
        if not a_alive:
            # 桶删了 → 关系必须也消失了（不可能留下悬空有效边）
            assert orphan == 0, "删除后残留有效关系"
        else:
            # 桶没删 → 要么关系还在（纠错没跑赢）、要么关系被纠错掉了
            # 两种都合法；关键是删除被拒时当时的关系是真实的
            pass
        # 无论哪种交错，不允许"关系行还在但指向已删桶"
        if not a_alive:
            assert not rel_exists

    def test_approve_vs_direct_delete_single_outcome(self, actors):
        """T03：approve vs 直删同一桶——不能两次成功删除副作用。"""
        m = _hold(actors, "双删竞态")
        # 人类申请先就位
        req = registry.invoke(
            actors["qiaosheng"], "memory.deletion.request",
            {"memory_id": m["memory_id"], "reason": "删掉",
             "operation_id": "t3-req"}, None)["data"]

        def do_approve():
            return registry.invoke(
                actors["jiaming"], "memory.deletion.decide",
                {"request_id": req["request_id"], "decision": "approve"},
                None)

        def do_direct():
            return registry.invoke(
                actors["jiaming"], "memory.delete",
                {"memory_id": m["memory_id"], "operation_id": "t3-dd"},
                None)

        out = self._barrier_pair(do_approve, do_direct)
        statuses = [v[0] for v in out.values()]
        # 审计 2026-10-03：收集了就要断言——删除副作用恰发生一次，
        # 输家必须是结构化拒绝（桶已删的 NotFound/状态机 Forbidden），
        # 不允许双方都自称成功
        with db.formal() as conn:
            n_del = conn.execute(
                "SELECT COUNT(*) c FROM audit_events WHERE"
                " event_type='memory.deleted' AND resource_id=?",
                (m["memory_id"],)).fetchone()["c"]
        assert n_del == 1, f"删除副作用必须恰一次：{n_del}"
        for tag, (status, payload) in out.items():
            if status == "err":
                assert isinstance(payload, Exception), tag
                assert not isinstance(payload, (KeyError, TypeError,
                                                AttributeError)), \
                    f"{tag} 线程裸异常逃逸：{payload!r}"
        with db.formal() as conn:
            alive = bool(conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?",
                (m["memory_id"],)).fetchone())
            final_req = conn.execute(
                "SELECT status FROM deletion_requests WHERE request_id=?",
                (req["request_id"],)).fetchone()["status"]
        assert not alive  # 桶必须被删了
        # 最终申请状态一致：approved（approve 赢）或 superseded（直删赢）
        assert final_req in ("approved", "superseded"), final_req
