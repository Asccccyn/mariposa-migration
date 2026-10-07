"""规格 v2.0 §11 验收矩阵（C/R/Q/D/T）。

真实合同路径（Registry invoke），不直调 service 报绿。
"""
from __future__ import annotations

import threading

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import DeleteBlocked, Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all

J = {"id": "jiaming", "name": "周家明", "kind": "agent",
     "src": "claude_chat", "b": "bj"}
Q = {"id": "qiaosheng", "name": "江乔生", "kind": "human",
     "src": "web", "b": "bq"}


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal(J["id"], J["name"], J["kind"],
                                      J["src"], J["b"]),
        "qiaosheng": identity.Principal(Q["id"], Q["name"], Q["kind"],
                                        Q["src"], Q["b"]),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="acc",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def _req(actors, mid, reason, op):
    return registry.invoke(actors["qiaosheng"], "memory.deletion.request",
                           {"memory_id": mid, "reason": reason,
                            "operation_id": op}, None)["data"]


# ============================================================ C 清理与接口

class TestC:
    def test_c01_archive_action_rejected(self, actors):
        """action 参数已被 schema 结构性拒绝（不留静默吞参）。"""
        m = _hold(actors, "c01")
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["qiaosheng"],
                            "memory.deletion.request",
                            {"memory_id": m["memory_id"], "reason": "r",
                             "action": "archive",
                             "operation_id": "op"}, None)
        assert ei.value.code == "SCHEMA_VIOLATION"

    def test_c02_fresh_schema(self, actors):
        with db.formal() as c:
            names = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "relation_corrections" in names
            cols = {r["name"] for r in c.execute(
                "PRAGMA table_info(memory_relations)")}
            assert "confidence" not in cols and "active" not in cols
            assert "relation_id" in cols
            vis = c.execute(
                "SELECT sql FROM sqlite_master WHERE name='memories'"
            ).fetchone()[0]
            assert "'archived'" not in vis

    def test_c03_old_contracts_gone_new_reachable(self, actors):
        for cap in ("memory.relations.detach", "source.binding.revoke",
                    "memory.deletion.restore"):
            assert cap not in registry.REGISTRY
        for cap in ("memory.relations.correct", "source.binding.correct",
                    "plan.memory.correct", "memory.our_words.source.correct",
                    "i.item.relations.correct"):
            assert cap in registry.REGISTRY

    def test_c06_retired_stay_retired(self):
        from tests.unit.test_retired_capabilities_negative import RETIRED
        left = [k for k in registry.REGISTRY if RETIRED.match(k)]
        assert left == []


# ============================================================ R 关系生命周期

class TestR:
    def _pair(self, actors):
        a = _hold(actors, "R甲")
        b = _hold(actors, "R乙")
        return a["memory_id"], b["memory_id"]

    def test_r01_all_domains_correctable(self, actors):
        """五域纠错各一例：memory/I/source/plan/word。"""
        a, b = self._pair(actors)
        # memory
        out = registry.invoke(actors["jiaming"], "memory.relations.link",
                              {"from_memory": a, "to_memory": b,
                               "relation_type": "related_to"}, None)["data"]
        rid = out["relation_id"]
        cor = registry.invoke(actors["jiaming"], "memory.relations.correct",
                              {"relation_id": rid, "correction_action":
                               "remove_wrong_binding",
                               "operation_id": "op-r1a"}, None)["data"]
        assert cor["correction_id"]
        # plan
        from mariposa.plans import service as plans
        plan = plans.create("jiaming", "R计划", state="active",
                            link_memory_ids=[a])
        with db.formal() as conn:
            link_id = conn.execute(
                "SELECT link_id FROM plan_memory_links WHERE plan_id=?",
                (plan["plan_id"],)).fetchone()["link_id"]
        cor2 = registry.invoke(actors["jiaming"], "plan.memory.correct",
                               {"link_id": link_id, "correction_action":
                                "remove_wrong_binding",
                                "operation_id": "op-r1b"}, None)["data"]
        assert cor2["removed_link_id"] == link_id
        # I：item.revise 带关系后纠错
        from mariposa.identity_i import service as i_svc
        i_svc.item_create("jiaming", "I 正文")
        item = i_svc.item_create("jiaming", "I 纠错正文",
                                 relations=[{"memory_id": a,
                                             "relation_type":
                                                 "related_to"}])
        # 找到修订关系 id
        with db.formal() as conn:
            irr = conn.execute(
                "SELECT relation_id FROM i_revision_memory_relations"
                " WHERE item_id=? AND memory_id=?",
                (item["item_id"], a)).fetchone()
        assert irr, "前置：I 修订关系已建"
        cor3 = registry.invoke(actors["jiaming"],
                               "i.item.relations.correct",
                               {"relation_id": irr["relation_id"],
                                "correction_action":
                                    "remove_wrong_binding",
                                "operation_id": "op-r1c"}, None)["data"]
        assert cor3["removed_relation_id"] == irr["relation_id"]
        # source / word 的纠错在各自专项测试覆盖（Q03/R06/R07）

    def test_r02_r03_plan_state_and_time_do_not_unbind(self, actors):
        from mariposa.plans import service as plans
        from mariposa.memory import relations as rel
        a, b = self._pair(actors)
        plan = plans.create("jiaming", "状态不变计划", state="active",
                            link_memory_ids=[a])
        out = rel.link("jiaming", a, b, "related_to")
        plans.update("jiaming", plan["plan_id"], 1, state="done")
        plans.update("jiaming", plan["plan_id"], 2, state="active")
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM plan_memory_links WHERE"
                " memory_id=?", (a,)).fetchone()["c"]
            m = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations WHERE"
                " relation_id=?", (out["relation_id"],)).fetchone()["c"]
        assert n == 1 and m == 1, "Plan 状态往返/时间推进不解绑"

    def test_r08_r09_duplicate_and_new_instance(self, actors):
        a, b = self._pair(actors)
        o1 = registry.invoke(actors["jiaming"], "memory.relations.link",
                             {"from_memory": a, "to_memory": b,
                              "relation_type": "related_to"}, None)["data"]
        o2 = registry.invoke(actors["jiaming"], "memory.relations.link",
                             {"from_memory": a, "to_memory": b,
                              "relation_type": "related_to"}, None)["data"]
        assert o2["relation_id"] == o1["relation_id"]  # R08 幂等
        rid = o1["relation_id"]
        registry.invoke(actors["jiaming"], "memory.relations.correct",
                        {"relation_id": rid, "correction_action":
                         "remove_wrong_binding", "operation_id": "k1"},
                        None)
        o3 = registry.invoke(actors["jiaming"], "memory.relations.link",
                             {"from_memory": a, "to_memory": b,
                              "relation_type": "related_to"}, None)["data"]
        assert o3["relation_id"] != rid  # 纠错后重建=新实例
        # R09：同 key 重放不撤销新边
        rep = registry.invoke(actors["jiaming"], "memory.relations.correct",
                              {"relation_id": rid, "correction_action":
                               "remove_wrong_binding", "operation_id": "k1"},
                              None)
        assert rep["data"].get("idempotent_replay") is True
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations WHERE"
                " from_memory=? AND to_memory=?", (a, b)).fetchone()["c"]
        assert n == 1, "重放不得撤销后来新建的正确边"

    def test_r11_last_plan_link_leaves_gap(self, actors):
        """最后一条错误 plan link 纠错后：桶仍带 plan 分类→GAP 暴露。"""
        from mariposa.plans import service as plans
        from mariposa.recall import phase_policy as pp
        out = memory.hold(
            actors["jiaming"], text="plan 桶", memory_date="2026-09-25",
            date_confidence="exact", original_title="p",
            categories=["plan"], creation_mode="contemporaneous",
            raw_pending=False,
            plan_ids=[plans.create("jiaming", "唯一链接", state="active")
                      ["plan_id"]])
        mid = out["memory_id"]
        with db.formal() as conn:
            link_id = conn.execute(
                "SELECT link_id FROM plan_memory_links WHERE memory_id=?",
                (mid,)).fetchone()["link_id"]
        registry.invoke(actors["jiaming"], "plan.memory.correct",
                        {"link_id": link_id, "correction_action":
                         "remove_wrong_binding", "operation_id": "op-g"},
                        None)
        with pytest.raises(pp.DataGap):
            pp.phase_of(mid)  # 明确 GAP，不伪装终态


# ============================================================ Q 反查与链路

class TestQ:
    def test_q04_direction_words_not_flipped(self, actors):
        a, b = self._pair_x(actors)
        registry.invoke(actors["jiaming"], "memory.relations.link",
                        {"from_memory": a, "to_memory": b,
                         "relation_type": "continuation_of"}, None)
        out1 = registry.invoke(actors["jiaming"], "relations.list",
                               {"memory_id": a, "direction": "out"},
                               None)["data"]["relations"]
        out2 = registry.invoke(actors["jiaming"], "relations.list",
                               {"memory_id": b, "direction": "in"},
                               None)["data"]["relations"]
        assert out1[0]["direction"] == "out"
        assert out2[0]["reversed"] is True  # B 视角：A 引用了我

    def _pair_x(self, actors):
        a = _hold(actors, "Q甲")
        b = _hold(actors, "Q乙")
        return a["memory_id"], b["memory_id"]

    def test_q05_multi_branch_reachable(self, actors):
        root = _hold(actors, "根")["memory_id"]
        kids = [_hold(actors, f"支{i}")["memory_id"] for i in range(3)]
        for k in kids:
            registry.invoke(actors["jiaming"], "memory.relations.link",
                            {"from_memory": k, "to_memory": root,
                             "relation_type": "continuation_of"}, None)
        tr = registry.invoke(actors["jiaming"], "relations.trace",
                             {"memory_id": root}, None)["data"]
        assert sorted(tr["later"]) == sorted(kids)  # 多后续全部可达
        assert tr["truncated"] is False

    def test_q06_cycle_bounded(self, actors):
        a, b = self._pair_x(actors)
        registry.invoke(actors["jiaming"], "memory.relations.link",
                        {"from_memory": a, "to_memory": b,
                         "relation_type": "continuation_of"}, None)
        registry.invoke(actors["jiaming"], "memory.relations.link",
                        {"from_memory": b, "to_memory": a,
                         "relation_type": "continuation_of"}, None)
        tr = registry.invoke(actors["jiaming"], "relations.trace",
                             {"memory_id": a, "max_depth": 20}, None)["data"]
        # CB-034：earlier/later 各自沿固定方向遍历——矛盾环（B 晚于 A
        # 且 A 晚于 B）两侧各自有界，节点可同时出现在两侧（如实披露
        # 矛盾结构，不再按最后一跳归类）；visited=两侧合计+自身
        assert set(tr["earlier"]) <= {b} and set(tr["later"]) <= {b}
        assert tr["visited"] == 3  # a + b（早侧） + b（晚侧）
        assert tr["truncated"] is False  # 环不失控也不误报截断

    def test_q07_history_not_mixed_missing_flagged(self, actors):
        a, b = self._pair_x(actors)
        out = registry.invoke(actors["jiaming"], "memory.relations.link",
                              {"from_memory": a, "to_memory": b,
                               "relation_type": "related_to"}, None)["data"]
        registry.invoke(actors["jiaming"], "memory.relations.correct",
                        {"relation_id": out["relation_id"],
                         "correction_action": "remove_wrong_binding",
                         "operation_id": "q7"}, None)
        lst = registry.invoke(actors["jiaming"], "relations.list",
                              {"memory_id": a}, None)["data"]["relations"]
        assert lst == []  # 有效查询不混纠错历史
        hist = registry.invoke(
            actors["jiaming"], "relations.corrections.list",
            {"endpoint": a}, None)["data"]["corrections"]
        assert hist and hist[0]["endpoint_a"] == a  # 历史仍可查


# ============================================================ D 删除双路径

class TestD:
    def _m(self, actors):
        return _hold(actors, "D桶")["memory_id"]

    def test_d01_blank_reason_and_double_pending(self, actors):
        m = self._m(actors)
        with pytest.raises(Forbidden):
            _req(actors, m, " ", "k0")
        _req(actors, m, "理由", "k1")
        with pytest.raises(Forbidden) as ei:
            _req(actors, m, "再来", "k2")
        assert ei.value.code == "CONFLICT"

    def test_d02_five_lifetime_sixth_rejected(self, actors):
        m = self._m(actors)
        for i in range(5):
            r = _req(actors, m, f"r{i+1}", f"d2k{i}")
            registry.invoke(actors["qiaosheng"],
                            "memory.deletion.withdraw",
                            {"request_id": r["request_id"]}, None)
        with pytest.raises(Forbidden) as ei:
            _req(actors, m, "第六次", "d2x")
        assert ei.value.code == "QUOTA_EXCEEDED"

    def test_d03_withdraw_counts_and_replay_free(self, actors):
        m = self._m(actors)
        r1 = _req(actors, m, "r", "d3a")
        replay = registry.invoke(actors["qiaosheng"],
                                 "memory.deletion.request",
                                 {"memory_id": m, "reason": "r",
                                  "operation_id": "d3a"}, None)["data"]
        assert replay["request_id"] == r1["request_id"]
        assert replay.get("idempotent_replay") is True  # 同 key 不多计

    def test_d06_d07_reject_reason_approve_silent(self, actors):
        m = self._m(actors)
        r = _req(actors, m, "申请", "d6")
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"], "memory.deletion.decide",
                            {"request_id": r["request_id"],
                             "decision": "reject"}, None)
        out = registry.invoke(actors["jiaming"], "memory.deletion.decide",
                              {"request_id": r["request_id"],
                               "decision": "reject",
                               "rejection_reason": "核实非误记"}, None)["data"]
        assert out["status"] == "rejected"
        assert memory_get(m)  # 桶保留
        m2 = self._m(actors)
        r2 = _req(actors, m2, "申请2", "d7")
        out2 = registry.invoke(actors["jiaming"], "memory.deletion.decide",
                               {"request_id": r2["request_id"],
                                "decision": "approve"}, None)["data"]
        assert out2["status"] == "approved"  # approve 无理由要求

    def test_d08_d09_direct_delete(self, actors):
        m = self._m(actors)
        out = registry.invoke(actors["jiaming"], "memory.delete",
                              {"memory_id": m, "operation_id": "dd"},
                              None)["data"]
        assert out["deleted"] is True
        # 人类额度耗尽后仍可直删（无配额概念）
        m2 = self._m(actors)
        for i in range(5):
            r = _req(actors, m2, f"r{i}", f"ddk{i}")
            registry.invoke(actors["qiaosheng"],
                            "memory.deletion.withdraw",
                            {"request_id": r["request_id"]}, None)
        with pytest.raises(Forbidden):
            _req(actors, m2, "超限", "ddx")
        out2 = registry.invoke(actors["jiaming"], "memory.delete",
                               {"memory_id": m2, "operation_id": "dd2"},
                               None)["data"]
        assert out2["deleted"] is True

    def test_d10_non_jiaming_cannot_direct(self, actors):
        m = self._m(actors)
        with pytest.raises(Forbidden):
            registry.invoke(actors["qiaosheng"], "memory.delete",
                            {"memory_id": m, "operation_id": "x"}, None)

    def test_d12_d13_five_domains_block(self, actors):
        a = self._m(actors)
        b = self._m(actors)
        # memory in/out 边
        registry.invoke(actors["jiaming"], "memory.relations.link",
                        {"from_memory": b, "to_memory": a,
                         "relation_type": "related_to"}, None)
        with pytest.raises(DeleteBlocked) as ei:
            registry.invoke(actors["jiaming"], "memory.delete",
                            {"memory_id": a, "operation_id": "db1"},
                            None)
        assert "memory_relations" in ei.value.detail["references"]
        # 其余四域由 service 层 blocking_relations 单测覆盖

    def test_d14_d15_corrected_then_deletable_pending_kept(self, actors):
        a = self._m(actors)
        b = self._m(actors)
        out = registry.invoke(actors["jiaming"], "memory.relations.link",
                              {"from_memory": a, "to_memory": b,
                               "relation_type": "related_to"}, None)["data"]
        r = _req(actors, a, "申请", "dc")
        with pytest.raises(DeleteBlocked):
            registry.invoke(actors["jiaming"], "memory.deletion.decide",
                            {"request_id": r["request_id"],
                             "decision": "approve"}, None)
        assert registry.invoke(
            actors["jiaming"], "memory.deletion.get",
            {"request_id": r["request_id"]}, None)["data"]["status"] \
            == "pending"
        registry.invoke(actors["jiaming"], "memory.relations.correct",
                        {"relation_id": out["relation_id"],
                         "correction_action": "remove_wrong_binding",
                         "operation_id": "dc2"}, None)
        out2 = registry.invoke(actors["jiaming"], "memory.deletion.decide",
                               {"request_id": r["request_id"],
                                "decision": "approve"}, None)["data"]
        assert out2["status"] == "approved"

    def test_d16_history_survives_deletion(self, actors):
        m = self._m(actors)
        r = _req(actors, m, "历史保留", "dh")
        registry.invoke(actors["jiaming"], "memory.deletion.decide",
                        {"request_id": r["request_id"],
                         "decision": "approve"}, None)
        got = registry.invoke(actors["jiaming"], "memory.deletion.get",
                              {"request_id": r["request_id"]}, None)["data"]
        assert got["human_reason"] == "历史保留"


def memory_get(mid):
    with db.formal() as conn:
        from mariposa.memory import service as ms
        return ms.get(conn, mid)


class TestR09LinkingRights:
    """接线权（她 1007 裁定"我也可以连线，不管是 relation 还是 episode"）：
    i 层关系纠错对她开放（连线=关系结构，不是 i 正文）；记忆层主动连线
    memory.relations.link/correct 本就 _owners() 两人都有——现状保留。"""

    def test_qiaosheng_i_relation_correct_and_memory_link(self, actors):
        from mariposa.identity_i import service as i_svc
        m = memory.hold(actors["jiaming"], text="接线权正文一",
                        memory_date="2026-09-20", date_confidence="exact",
                        original_title="t", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
        item = i_svc.item_create("jiaming", "她的连线正文",
                                 relations=[{"memory_id": m["memory_id"],
                                             "relation_type": "related_to"}])
        with db.formal() as conn:
            irr = conn.execute(
                "SELECT relation_id FROM i_revision_memory_relations"
                " WHERE item_id=? AND memory_id=?",
                (item["item_id"], m["memory_id"])).fetchone()
        assert irr, "前置：I 修订关系已建"
        cor = registry.invoke(actors["qiaosheng"],
                              "i.item.relations.correct",
                              {"relation_id": irr["relation_id"],
                               "correction_action":
                                   "remove_wrong_binding",
                               "operation_id": "op-r09-link"}, None)["data"]
        assert cor["removed_relation_id"] == irr["relation_id"], \
            "她的 i 层关系纠错权（复刻一份）"

        m2 = memory.hold(actors["jiaming"], text="接线权正文二",
                         memory_date="2026-09-21", date_confidence="exact",
                         original_title="t2", categories=["daily"],
                         creation_mode="contemporaneous", raw_pending=False)
        lnk = registry.invoke(actors["qiaosheng"], "memory.relations.link",
                              {"from_memory": m["memory_id"],
                               "to_memory": m2["memory_id"],
                               "relation_type": "related_to"}, None)["data"]
        assert lnk, "她的记忆层主动连线（现状确认保留）"
