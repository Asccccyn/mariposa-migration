"""Relation/routing 域审计修复回归（2026-10-02 基线审计 P2 批）。

- CB-032：Source 端反查——relations.list 返回的 source_range/
  source_ref 端点对象可原样作为下一次查询起点（审计反例
  public_endpoint_roundtrip / source_to_word_unavailable）。
- CB-033：direction/domains 统一过滤（direction_filter_ignored——
  非 memory 分支与 memory 分支的跨域子句此前全不检查）。
- CB-034：trace 双向独立遍历 + 真实截断判定（trace_sibling_as_
  ancestor / trace_false_truncation）。
- CB-035：I 写 schema 接受 canonical related_to（读写往返一致）。
- CB-037：word append 的目标存在性检查在写锁内（顺序+结构断言）。
"""
from __future__ import annotations

import contextlib

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import relations as rel
from mariposa.memory import service as memory
from mariposa.relations import routing
from mariposa.source import binding, importer
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="rt",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def _seed_conv(prefix: str, texts: list[str]) -> tuple[str, list[str]]:
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    msgs = []
    prev = None
    days = ("28", "29", "30")
    for i, t in enumerate(texts):
        m = {"uuid": f"{prefix}-m{i}", "sender": "human",
             "created_at": f"2026-09-{days[i % 3]}T1{i}:00:00Z",
             "content": [{"type": "text", "text": t}]}
        if prev:
            m["parent_message_uuid"] = prev
        prev = m["uuid"]
        msgs.append(m)
    f = tmp / f"{prefix}.json"
    f.write_text(_json.dumps([{"uuid": f"c-{prefix}",
                               "chat_messages": msgs}],
                             ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))
    return f"c-{prefix}", [m["uuid"] for m in msgs]


# ---------------------------------------------------------------- CB-033

class TestDirectionDomainFilters:

    def _setup(self, actors):
        a = _hold(actors, "方向甲")["memory_id"]
        b = _hold(actors, "方向乙")["memory_id"]
        # 出边：memory→memory
        rel.link("jiaming", a, b, "related_to")
        # 入边：i_revision→memory
        from mariposa.identity_i import service as i_svc
        i_svc.item_create("jiaming", "I 正文",
                          relations=[{"memory_id": a,
                                      "relation_type": "related_to"}])
        return a, b

    def test_direction_out_excludes_in_edges(self, actors):
        a, b = self._setup(actors)
        out = routing.list_relations({"resource": {"type": "memory",
                                                   "memory_id": a},
                                      "direction": "out"})
        dirs = {(r["domain"], r["direction"]) for r in out["relations"]}
        assert ("i_revision_relation", "in") not in dirs, \
            "direction=out 不得混入入边（审计反例）"

    def test_direction_in_excludes_out_edges(self, actors):
        a, b = self._setup(actors)
        out = routing.list_relations({"resource": {"type": "memory",
                                                   "memory_id": a},
                                      "direction": "in"})
        dirs = {(r["domain"], r["direction"]) for r in out["relations"]}
        assert ("memory_relation", "out") not in dirs
        assert ("plan_link", "out") not in dirs
        assert ("i_revision_relation", "in") in dirs

    def test_plan_link_direction_from_memory_is_in(self, actors):
        a = _hold(actors, "计划方向")["memory_id"]
        from mariposa.plans import service as plans
        plans.create("jiaming", "P", state="active", link_memory_ids=[a])
        out = routing.list_relations({"resource": {"type": "memory",
                                                   "memory_id": a},
                                      "direction": "in",
                                      "domains": ["plan_link"]})
        assert out["relations"], "plan→memory 对 memory 端是入边"
        assert all(r["direction"] == "in" for r in out["relations"])

    def test_non_memory_branch_respects_domains(self, actors):
        a = _hold(actors, "域过滤")["memory_id"]
        from mariposa.plans import service as plans
        plan = plans.create("jiaming", "P2", state="active",
                            link_memory_ids=[a])
        out = routing.list_relations({
            "resource": {"type": "plan", "plan_id": plan["plan_id"]},
            "domains": ["memory_relation"]})
        assert out["relations"] == [], \
            "非 memory 端点也要应用 domains 过滤"


# ---------------------------------------------------------------- CB-032

class TestSourceReverseLookup:

    def test_source_range_endpoint_roundtrip(self, actors):
        """审计反例：memory→Source 的 other 端点原样作 resource 反查
        曾抛 ValueError——现在返回覆盖该区间的绑定。"""
        a = _hold(actors, "反查甲")["memory_id"]
        c = _hold(actors, "反查乙")["memory_id"]
        conv, [m1, m2, m3] = _seed_conv("rt", ["段一原文", "段二原文",
                                               "段三原文"])
        bd = binding.bind("jiaming", a, conv, m1, m2)
        # 同 conversation 不同 range：只绑 m3——反查 m1 不应命中它
        binding.bind("jiaming", c, conv, m3, m3)
        fwd = routing.list_relations({"resource": {"type": "memory",
                                                   "memory_id": a},
                                      "domains": ["source_binding"]})
        endpoint = fwd["relations"][0]["other"]
        assert endpoint["type"] == "source_range"
        rev = routing.list_relations({"resource": endpoint})
        hits = {(r["other"]["memory_id"], r["relation_id"])
                for r in rev["relations"]}
        assert (a, bd["binding_id"]) in hits, "range 端点必须能反查回甲"
        assert all(mi != c for mi, _ in hits), \
            "同 conversation 不同 range 不得串段"

    def test_source_ref_endpoint_reverse_lookup(self, actors):
        """word→source 的 other 端点（source_ref）原样反查：引用该
        source_msg 的 word 与覆盖它的绑定。"""
        conv, [m1, m2] = _seed_conv("rf", ["引用原文一", "引用原文二"])
        with db.formal() as conn:
            msg_id = conn.execute(
                "SELECT id FROM source_messages WHERE"
                " provider_message_id=?", (m1,)).fetchone()["id"]
        ref = f"source_msg:{msg_id}"
        w = _hold(actors, "引用话语",
                  our_words=[{"speaker": "qiaosheng", "text": "引用的话",
                              "expression_kind": "verbatim",
                              "source_ref": ref}])
        fwd = routing.list_relations({"resource": {
            "type": "word",
            "word_id": None or _word_of(w["memory_id"])}})
        endpoint = fwd["relations"][0]["other"]
        assert endpoint == {"type": "source_ref", "ref": ref}
        rev = routing.list_relations({"resource": endpoint})
        domains = {r["domain"] for r in rev["relations"]}
        assert "word_source" in domains, "source_msg 反查必须能找到 word"


def _word_of(memory_id: str) -> str:
    with db.formal() as conn:
        return conn.execute(
            "SELECT word_id FROM memory_our_words WHERE memory_id=?",
            (memory_id,)).fetchone()["word_id"]


# ---------------------------------------------------------------- CB-034

class TestTraceSemantics:

    def test_sibling_not_classified_as_ancestor(self, actors):
        """审计反例：B continuation_of A 且 B continuation_of C——
        从 A trace 曾把 C 归进 earlier（C 与 A 是同辈合流）。"""
        a = _hold(actors, "链甲")["memory_id"]
        b = _hold(actors, "链乙")["memory_id"]
        c = _hold(actors, "链丙")["memory_id"]
        rel.link("jiaming", b, a, "continuation_of")  # b 晚于 a
        rel.link("jiaming", b, c, "continuation_of")  # b 晚于 c
        t = rel.trace(a)
        assert t["later"] == [b], f"甲的更晚只有乙：{t}"
        assert c not in t["earlier"], "同辈合流节点不得进入祖先侧"
        assert c not in t["later"]

    def test_complete_leaf_not_reported_truncated(self, actors):
        """审计反例：唯一边 A→B，max_depth=1 已覆盖全部——仍
        truncated=true。"""
        a = _hold(actors, "叶甲")["memory_id"]
        b = _hold(actors, "叶乙")["memory_id"]
        rel.link("jiaming", a, b, "continuation_of")
        t = rel.trace(a, max_depth=1)
        assert t["earlier"] == [b]
        assert t["truncated"] is False, "完整覆盖的叶节点不是截断"

    def test_real_depth_limit_reports_truncated(self, actors):
        ids = [_hold(actors, f"深{i}")["memory_id"] for i in range(4)]
        for x, y in zip(ids, ids[1:]):
            rel.link("jiaming", x, y, "continuation_of")
        t = rel.trace(ids[0], max_depth=2)
        assert t["truncated"] is True, "真实超深必须披露截断"
        assert len(t["earlier"]) == 2


# ---------------------------------------------------------------- CB-035

class TestIRelationSchemaRoundtrip:

    def test_canonical_related_to_writable(self, actors):
        a = _hold(actors, "规范甲")["memory_id"]
        out = registry.invoke(
            actors["jiaming"], "i.item.create",
            {"content": "canonical 类型写入",
             "relations": [{"memory_id": a,
                            "relation_type": "related_to"}]}, None)
        item_id = out["data"]["item_id"]
        lst = registry.invoke(
            actors["jiaming"], "relations.list",
            {"resource": {"type": "i_revision", "item_id": item_id,
                          "revision": 1}}, None)
        assert any(r.get("relation_type") == "related_to"
                   for r in lst["data"]["relations"]), \
            "读到的 canonical 类型必须能原样写回（读写往返）"


# ---------------------------------------------------------------- CB-037

class TestWordAppendTxCheck:

    def test_existence_check_inside_transaction(self, actors, monkeypatch):
        """结构断言：存在性检查 SQL 必须在 BEGIN IMMEDIATE 之后执行
        （锁内重查结构性消除删除竞态的未结构化 FK 异常）。"""
        from mariposa.memory import our_words as ow
        mid = _hold(actors, "锁内检查")["memory_id"]
        seq: list[str] = []
        orig_formal = db.formal

        class _Recording:
            def __init__(self, conn):
                self._c = conn

            def execute(self, sql, *a):
                seq.append(" ".join(str(sql).split())[:60])
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

        @contextlib.contextmanager
        def wrapped():
            with orig_formal() as c:
                yield _Recording(c)

        monkeypatch.setattr(ow.db, "formal", wrapped)
        ow.append("jiaming", mid,
                  [{"speaker": "qiaosheng", "text": "锁内检查话语",
                    "expression_kind": "verbatim"}])
        begin_i = next(i for i, s in enumerate(seq)
                       if s.startswith("BEGIN IMMEDIATE"))
        check_i = next(i for i, s in enumerate(seq)
                       if "FROM memories WHERE memory_id" in s
                       and "SELECT 1" in s)
        assert begin_i < check_i, \
            f"存在性检查必须在写锁内：{seq[:4]}"

    def test_missing_memory_structured_not_found(self, actors):
        from mariposa.memory import our_words as ow
        with pytest.raises(NotFound):
            ow.append("jiaming", "mem_nonexistent",
                      [{"speaker": "qiaosheng", "text": "x",
                        "expression_kind": "verbatim"}])
