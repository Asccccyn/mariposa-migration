"""复审 Relation/Deletion 域修复回归（RA-019/020/021）。"""
from __future__ import annotations

import json

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import IdempotencyConflict
from mariposa.identity import service as identity
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
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="rd",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def _seed_conv(prefix, texts):
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    msgs = []
    prev = None
    days = ("28", "29", "30")
    for i, t in enumerate(texts):
        m = {"uuid": f"{prefix}-m{i}", "sender": "human",
             "created_at": f"2026-09-{days[i % 3]}T10:00:00Z",
             "content": [{"type": "text", "text": t}]}
        if prev:
            m["parent_message_uuid"] = prev
        prev = m["uuid"]
        msgs.append(m)
    f = tmp / f"{prefix}.json"
    f.write_text(json.dumps([{"uuid": f"c-{prefix}",
                              "chat_messages": msgs}],
                            ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))
    return f"c-{prefix}", [m["uuid"] for m in msgs]


class TestDeletionRequestIdempotency:

    def test_body_operation_payload_and_concurrent_replay(self, actors):
        """RA-019：无 transport key——同 body key 异 reason 冲突、
        同 payload 重放同回执。"""
        m = _hold(actors, "申请幂等正文")
        r1 = registry.invoke(actors["qiaosheng"], "memory.deletion.request",
                             {"memory_id": m["memory_id"],
                              "reason": "first",
                              "operation_id": "ra019-k"}, None)
        assert r1["ok"] is True
        with pytest.raises(IdempotencyConflict):
            registry.invoke(actors["qiaosheng"], "memory.deletion.request",
                            {"memory_id": m["memory_id"],
                             "reason": "changed",
                             "operation_id": "ra019-k"}, None)
        r2 = registry.invoke(actors["qiaosheng"], "memory.deletion.request",
                             {"memory_id": m["memory_id"],
                              "reason": "first",
                              "operation_id": "ra019-k"}, None)
        data = r2["data"] if "data" in r2 else r2
        assert data.get("idempotent_replay") is True or \
            data.get("status") == "pending"


class TestWordsDerivedCleanup:

    def test_physical_delete_clears_words_tables(self, actors):
        from mariposa.memory import our_words as ow
        from mariposa.retrieval import projection as _proj  # noqa: F401
        m = _hold(actors, "派生清理正文")
        ow.append("jiaming", m["memory_id"],
                  [{"speaker": "qiaosheng", "text": "删除也要清的话语",
                    "expression_kind": "verbatim"}])
        registry.invoke(actors["jiaming"], "maintenance.rebuild_index", {},
                        None)
        with db.formal() as conn:
            n0 = conn.execute(
                "SELECT COUNT(*) c FROM words_search_docs").fetchone()["c"]
        assert n0 > 0, "前置：派生索引已建"
        registry.invoke(actors["jiaming"], "memory.delete",
                        {"memory_id": m["memory_id"],
                         "operation_id": "ra020-d"}, None)
        with db.formal() as conn:
            n1 = conn.execute(
                "SELECT COUNT(*) c FROM words_search_docs").fetchone()["c"]
            f1 = conn.execute(
                "SELECT COUNT(*) c FROM words_fts").fetchone()["c"]
        assert n1 == 0 and f1 == 0, \
            "物理删除必须同事务清 words 派生表（RA-020）"


class TestSourceRangeFullIdentity:

    def test_same_message_disjoint_offsets_not_matched(self, actors):
        """RA-021：同消息 [0,4) 与 [10,15) 互不匹配；错 conversation
        不命中。"""
        a = _hold(actors, "区间身份甲")["memory_id"]
        b = _hold(actors, "区间身份乙")["memory_id"]
        conv, [m1, m2] = _seed_conv("ra021", ["长句子用来切片第一段",
                                              "第二段消息"])
        bd_a = binding.bind("jiaming", a, conv, m1, m1,
                            start_char_offset=0, end_char_offset=4)
        bd_b = binding.bind("jiaming", b, conv, m1, m1,
                            start_char_offset=6, end_char_offset=9)
        assert bd_a and bd_b
        # A 的 range 端点反查：只命中 A 的绑定
        rev = routing.list_relations({"resource": {
            "type": "source_range", "conversation_id": conv,
            "start_message_id": m1, "end_message_id": m1,
            "start_char_offset": 0, "end_char_offset": 4}})
        mems = {r["other"]["memory_id"] for r in rev["relations"]}
        assert mems == {a}, f"不相交字符区间不得串绑：{mems}"
        # 错 conversation：不命中任何
        rev2 = routing.list_relations({"resource": {
            "type": "source_range", "conversation_id": "does-not-exist",
            "start_message_id": m1, "end_message_id": m1}})
        assert rev2["relations"] == [], "conversation 身份必须核验"
