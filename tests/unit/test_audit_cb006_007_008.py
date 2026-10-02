"""Relation 域审计修复回归（2026-10-02 基线审计 P1 批）。

- CB-006：替换纠错不得退化为解绑（五域 replace 必带完整 replacement、
  remove 禁带；同端点替换先删旧再建新实例，回执不得指向已删除的
  replacement——审计反例 replace_missing_replacement_removes /
  replace_same_identity_deleted / custom_label_correction_deletes_edge）。
- CB-007：word 来源 CAS 加入换代计数（审计反例
  word_source_ABA_stale_correction：A→撤销→重建 A 后，旧 expected 请求
  删掉了新绑定）。
- CB-008：transport 幂等与领域 atomic_write 键空间隔离（审计反例
  outer_inner_idempotency_collision / request_header_operation_collision：
  同 key 双协议互占、冲突分支 NameError）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, IdempotencyConflict
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.source import binding, importer
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
                date_confidence="exact", original_title="cb",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def _seed_source_conv(prefix: str) -> tuple[str, str, str]:
    """建一个真实会话（消息带 parent 链，满足区间绑定校验），
    返回 (conversation_id, first_msg, last_msg)。"""
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    convs = [{"uuid": f"c-{prefix}", "chat_messages": [
        {"uuid": f"m-{prefix}-1", "sender": "human",
         "parent_message_uuid": None,
         "created_at": "2026-09-28T10:00:00Z",
         "content": [{"type": "text", "text": f"{prefix} 原文一"}]},
        {"uuid": f"m-{prefix}-2", "sender": "assistant",
         "parent_message_uuid": f"m-{prefix}-1",
         "created_at": "2026-09-28T10:05:00Z",
         "content": [{"type": "text", "text": f"{prefix} 原文二"}]}]}]
    f = tmp / f"{prefix}.json"
    f.write_text(_json.dumps(convs, ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))
    return f"c-{prefix}", f"m-{prefix}-1", f"m-{prefix}-2"


def _msg_internal_id(provider_message_id: str) -> str:
    with db.formal() as conn:
        return conn.execute(
            "SELECT id FROM source_messages WHERE provider_message_id=?",
            (provider_message_id,)).fetchone()["id"]


# ---------------------------------------------------------------- CB-006

class TestReplaceSemantics:
    """五域 replace/remove 语义与同端点替换。"""

    def test_replace_without_replacement_rejected_all_domains(self, actors):
        """审计反例（五域）：replace 不带 replacement 曾把关系移出
        active 而无新关系——现在必须结构化拒绝且关系不动。"""
        a = _hold(actors, "甲")["memory_id"]
        b = _hold(actors, "乙")["memory_id"]
        rid = registry.invoke(
            actors["jiaming"], "memory.relations.link",
            {"from_memory": a, "to_memory": b,
             "relation_type": "related_to"}, None)["data"]["relation_id"]
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["jiaming"], "memory.relations.correct",
                {"relation_id": rid,
                 "correction_action": "replace_wrong_binding",
                 "operation_id": "cb006-m-noRep"}, None)
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations WHERE"
                " relation_id=?", (rid,)).fetchone()["c"]
        assert n == 1, "被拒纠错不得移除有效边"

        # plan 域
        from mariposa.plans import service as plans
        plan = plans.create("jiaming", "P", state="active",
                            link_memory_ids=[a])
        with db.formal() as conn:
            link_id = conn.execute(
                "SELECT link_id FROM plan_memory_links WHERE plan_id=?",
                (plan["plan_id"],)).fetchone()["link_id"]
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["jiaming"], "plan.memory.correct",
                {"link_id": link_id,
                 "correction_action": "replace_wrong_binding",
                 "operation_id": "cb006-p-noRep"}, None)

        # I 域
        from mariposa.identity_i import service as i_svc
        item = i_svc.item_create("jiaming", "I 正文",
                                 relations=[{"memory_id": a,
                                             "relation_type":
                                                 "related_to"}])
        with db.formal() as conn:
            irr = conn.execute(
                "SELECT relation_id FROM i_revision_memory_relations"
                " WHERE item_id=? AND memory_id=?",
                (item["item_id"], a)).fetchone()["relation_id"]
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["jiaming"], "i.item.relations.correct",
                {"relation_id": irr,
                 "correction_action": "replace_wrong_binding",
                 "operation_id": "cb006-i-noRep"}, None)

        # source 域
        conv, m1, m2 = _seed_source_conv("cb006s")
        bd = binding.bind("jiaming", a, conv, m1, m2)
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["jiaming"], "source.binding.correct",
                {"binding_id": bd["binding_id"],
                 "correction_action": "replace_wrong_binding",
                 "operation_id": "cb006-s-noRep"}, None)
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_source_bindings WHERE"
                " binding_id=?", (bd["binding_id"],)).fetchone()["c"]
        assert n == 1

        # word 域（缺 source_ref 的 replace 即撤销——拒绝）
        w = _hold(actors, "话语记忆", our_words=[
            {"speaker": "qiaosheng", "text": "被引用的话",
             "expression_kind": "verbatim"}])
        with db.formal() as conn:
            word_id = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (w["memory_id"],)).fetchone()["word_id"]
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["jiaming"], "memory.our_words.source.correct",
                {"word_id": word_id, "expected_source_ref": None,
                 "expected_source_version": 0,
                 "correction_action": "replace_wrong_binding",
                 "replacement": {},
                 "operation_id": "cb006-w-noRep"}, None)

    def test_remove_with_replacement_rejected(self, actors):
        a = _hold(actors, "移除甲")["memory_id"]
        b = _hold(actors, "移除乙")["memory_id"]
        rid = registry.invoke(
            actors["jiaming"], "memory.relations.link",
            {"from_memory": a, "to_memory": b, "relation_type": "custom",
             "custom_label": "L"}, None)["data"]["relation_id"]
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["jiaming"], "memory.relations.correct",
                {"relation_id": rid,
                 "correction_action": "remove_wrong_binding",
                 "replacement": {"from_memory": a, "to_memory": b,
                                 "relation_type": "related_to"},
                 "operation_id": "cb006-m-rmRep"}, None)
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations WHERE"
                " relation_id=?", (rid,)).fetchone()["c"]
        assert n == 1, "被拒请求不得有副作用"

    def test_same_endpoint_replace_creates_new_instance_memory(self, actors):
        """审计反例（custom_label_correction_deletes_edge）：同端点替换
        （修 label）曾复用旧 ID 再删除，active 边为空——现在新实例承接。"""
        a = _hold(actors, "同端甲")["memory_id"]
        b = _hold(actors, "同端乙")["memory_id"]
        rid = registry.invoke(
            actors["jiaming"], "memory.relations.link",
            {"from_memory": a, "to_memory": b, "relation_type": "custom",
             "custom_label": "旧标签"}, None)["data"]["relation_id"]
        cor = registry.invoke(
            actors["jiaming"], "memory.relations.correct",
            {"relation_id": rid, "correction_action": "replace_wrong_binding",
             "replacement": {"from_memory": a, "to_memory": b,
                             "relation_type": "custom",
                             "custom_label": "新标签"},
             "operation_id": "cb006-m-same"}, None)["data"]
        assert cor["replacement_relation_id"]
        assert cor["replacement_relation_id"] != cor["removed_relation_id"]
        with db.formal() as conn:
            gone = conn.execute(
                "SELECT 1 FROM memory_relations WHERE relation_id=?",
                (rid,)).fetchone()
            new_row = conn.execute(
                "SELECT custom_label FROM memory_relations WHERE"
                " relation_id=?", (cor["replacement_relation_id"],)
            ).fetchone()
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations WHERE"
                " from_memory=? AND to_memory=?", (a, b)).fetchone()["c"]
        assert gone is None
        assert new_row["custom_label"] == "新标签"
        assert n == 1, "同端点替换后 active 边必须存在且唯一"

    def test_same_endpoint_replace_i_plan(self, actors):
        a = _hold(actors, "同端I甲")["memory_id"]
        from mariposa.plans import service as plans
        from mariposa.identity_i import service as i_svc

        # I：replacement 同 memory 同类型 = 同端点
        item = i_svc.item_create("jiaming", "I 同端",
                                 relations=[{"memory_id": a,
                                             "relation_type":
                                                 "related_to"}])
        with db.formal() as conn:
            irr = conn.execute(
                "SELECT relation_id FROM i_revision_memory_relations"
                " WHERE item_id=? AND memory_id=?",
                (item["item_id"], a)).fetchone()["relation_id"]
        cor = registry.invoke(
            actors["jiaming"], "i.item.relations.correct",
            {"relation_id": irr, "correction_action":
             "replace_wrong_binding",
             "replacement": {"memory_id": a, "relation_type": "related_to"},
             "operation_id": "cb006-i-same"}, None)["data"]
        assert cor["replacement_relation_id"] != irr
        with db.formal() as conn:
            kept = conn.execute(
                "SELECT COUNT(*) c FROM i_revision_memory_relations WHERE"
                " item_id=? AND memory_id=?",
                (item["item_id"], a)).fetchone()["c"]
        assert kept == 1

        # plan：replacement 同 plan 同 memory = 同端点
        plan = plans.create("jiaming", "P同端", state="active",
                            link_memory_ids=[a])
        with db.formal() as conn:
            link_id = conn.execute(
                "SELECT link_id FROM plan_memory_links WHERE plan_id=?",
                (plan["plan_id"],)).fetchone()["link_id"]
        cor2 = registry.invoke(
            actors["jiaming"], "plan.memory.correct",
            {"link_id": link_id, "correction_action":
             "replace_wrong_binding",
             "replacement": {"plan_id": plan["plan_id"], "memory_id": a},
             "operation_id": "cb006-p-same"}, None)["data"]
        assert cor2["replacement_link_id"] != link_id
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM plan_memory_links WHERE"
                " plan_id=?", (plan["plan_id"],)).fetchone()["c"]
        assert n == 1

    def test_same_endpoint_replace_source(self, actors):
        a = _hold(actors, "同端源绑")["memory_id"]
        conv, m1, m2 = _seed_source_conv("cb006src")
        bd = binding.bind("jiaming", a, conv, m1, m2)
        cor = registry.invoke(
            actors["jiaming"], "source.binding.correct",
            {"binding_id": bd["binding_id"],
             "correction_action": "replace_wrong_binding",
             "replacement": {"conversation_id": conv,
                             "start_message_id": m1,
                             "end_message_id": m2},
             "operation_id": "cb006-s-same"}, None)["data"]
        assert cor["replacement_binding_id"]
        assert cor["replacement_binding_id"] != bd["binding_id"]
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_source_bindings WHERE"
                " memory_id=?", (a,)).fetchone()["c"]
        assert n == 1, "同端点替换后有效绑定必须存在且唯一"


# ---------------------------------------------------------------- CB-007

class TestWordSourceVersionCAS:
    """word 来源换代计数进入 CAS。"""

    def _word(self, actors, source_ref):
        out = _hold(actors, "ABA话语记忆", our_words=[
            {"speaker": "qiaosheng", "text": "ABA 话语",
             "expression_kind": "verbatim", "source_ref": source_ref}])
        with db.formal() as conn:
            word_id = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["word_id"]
        return word_id

    @staticmethod
    def _invoke(actors, word_id, expected_ref, version, action,
                operation_id, replacement=None):
        args = {"word_id": word_id, "expected_source_ref": expected_ref,
                "expected_source_version": version,
                "correction_action": action, "operation_id": operation_id}
        if replacement is not None:
            args["replacement"] = replacement
        return registry.invoke(
            actors["jiaming"], "memory.our_words.source.correct",
            args, None)["data"]

    def test_aba_stale_request_rejected(self, actors):
        """审计反例：A→(撤销)→重建 A 后，携带旧 expected 的延迟请求
        必须被换代计数拒绝（旧实现删掉了新绑定）。"""
        conv, m1, m2 = _seed_source_conv("cb007a")
        ref_a = f"source_msg:{_msg_internal_id(m1)}"
        word_id = self._word(actors, ref_a)
        # 撤销（v0→v1）
        out1 = self._invoke(actors, word_id, ref_a, 0,
                            "remove_wrong_binding", "cb007-rm")
        assert out1["source_binding_version"] == 1
        # 重建 A（v1→v2）
        out2 = self._invoke(actors, word_id, None, 1,
                            "replace_wrong_binding", "cb007-rp",
                            replacement={"source_ref": ref_a})
        assert out2["new_source_ref"] == ref_a
        assert out2["source_binding_version"] == 2
        # 旧延迟请求：expected=(A, v0) → 拒绝，新绑定不动
        with pytest.raises(Forbidden) as ei:
            self._invoke(actors, word_id, ref_a, 0,
                         "remove_wrong_binding", "cb007-stale")
        assert ei.value.code == "CONFLICT"
        with db.formal() as conn:
            row = conn.execute(
                "SELECT source_ref, source_binding_version FROM"
                " memory_our_words WHERE word_id=?",
                (word_id,)).fetchone()
        assert row["source_ref"] == ref_a
        assert row["source_binding_version"] == 2

    def test_replay_does_not_touch_new_generation(self, actors):
        """旧 operation 重放返回旧回执，不改当前绑定。"""
        conv, m1, _ = _seed_source_conv("cb007b")
        ref_a = f"source_msg:{_msg_internal_id(m1)}"
        word_id = self._word(actors, ref_a)
        self._invoke(actors, word_id, ref_a, 0,
                     "remove_wrong_binding", "cb007-old")
        self._invoke(actors, word_id, None, 1,
                     "replace_wrong_binding", "cb007-new",
                     replacement={"source_ref": ref_a})
        replay = self._invoke(actors, word_id, ref_a, 0,
                              "remove_wrong_binding", "cb007-old")
        assert replay.get("idempotent_replay") is True
        with db.formal() as conn:
            row = conn.execute(
                "SELECT source_ref, source_binding_version FROM"
                " memory_our_words WHERE word_id=?",
                (word_id,)).fetchone()
        assert row["source_ref"] == ref_a and \
            row["source_binding_version"] == 2

    def test_missing_version_rejected(self, actors):
        word_id = self._word(actors, None)
        with pytest.raises(Forbidden):
            registry.invoke(
                actors["jiaming"], "memory.our_words.source.correct",
                {"word_id": word_id, "expected_source_ref": None,
                 "correction_action": "remove_wrong_binding",
                 "operation_id": "cb007-nov"}, None)

    def test_correction_history_records_generation(self, actors):
        conv, m1, _ = _seed_source_conv("cb007c")
        ref_a = f"source_msg:{_msg_internal_id(m1)}"
        word_id = self._word(actors, ref_a)
        self._invoke(actors, word_id, ref_a, 0,
                     "remove_wrong_binding", "cb007-h1")
        hist = registry.invoke(
            actors["jiaming"], "relations.corrections.list",
            {"instance_id": f"word:{word_id}"}, None)["data"]
        meta = hist["corrections"][0]["original_meta"]
        assert meta.get("source_binding_version") == 0


# ---------------------------------------------------------------- CB-008

class TestIdempotencyNamespace:
    """transport 幂等与领域 atomic_write 键空间隔离。"""

    def _idempotency_rows(self, capability: str) -> dict:
        with db.formal() as conn:
            rows = conn.execute(
                "SELECT idempotency_key, status FROM idempotency_records"
                " WHERE capability=?", (capability,)).fetchall()
        return {r["idempotency_key"]: r["status"] for r in rows}

    def test_same_transport_and_operation_key_succeeds(self, actors):
        """审计反例 outer_inner_idempotency_collision：transport key 与
        body operation_id 同字符串——两层互占导致 NameError/IntegrityError
        与 running 残留；现在成功且两份 completed 记录并存。"""
        mid = _hold(actors, "幂等碰撞")["memory_id"]
        out = registry.invoke(
            actors["jiaming"], "memory.delete",
            {"memory_id": mid, "operation_id": "same-key"}, "same-key")
        assert out["ok"] is True
        rows = self._idempotency_rows("memory.delete")
        assert rows.get("same-key") == "completed"
        assert rows.get("op:same-key") == "completed"
        assert "running" not in rows.values()
        with db.formal() as conn:
            gone = conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?",
                (mid,)).fetchone()
        assert gone is None

    def test_deletion_request_same_key_structured_conflict(self, actors):
        """审计反例 request_header_operation_collision：人类申请同
        header/body key——首请求成功；同 key 异 reason 重试得到结构化
        IdempotencyConflict（不再 NameError），无 running 残留。"""
        mid = _hold(actors, "申请幂等")["memory_id"]
        r1 = registry.invoke(
            actors["qiaosheng"], "memory.deletion.request",
            {"memory_id": mid, "reason": "first reason",
             "operation_id": "same-key"}, "same-key")
        assert r1["ok"] is True
        with pytest.raises(IdempotencyConflict):
            registry.invoke(
                actors["qiaosheng"], "memory.deletion.request",
                {"memory_id": mid, "reason": "changed reason",
                 "operation_id": "same-key"}, "same-key")
        rows = self._idempotency_rows("memory.deletion.request")
        assert "running" not in rows.values()

    def test_body_operation_key_conflict_structured(self, actors):
        """不带 transport key：body 同 operation_id 异 memory_id →
        领域层结构化冲突（旧实现 NameError）。"""
        m1 = _hold(actors, "体幂等甲")["memory_id"]
        m2 = _hold(actors, "体幂等乙")["memory_id"]
        registry.invoke(
            actors["jiaming"], "memory.delete",
            {"memory_id": m1, "operation_id": "dup-op"}, None)
        with pytest.raises(IdempotencyConflict):
            registry.invoke(
                actors["jiaming"], "memory.delete",
                {"memory_id": m2, "operation_id": "dup-op"}, None)
        rows = self._idempotency_rows("memory.delete")
        assert "running" not in rows.values()
