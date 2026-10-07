"""复审 R01（2026-10-07）根因回归：召回出站单一信封。

反例（LINSHIJIAN_REVIEW_20261007 R01）：recall 运行时能力出站为
{ok, data:{data:packet}}（fresh）与 {ok, data:{idempotent_replay,
data:packet}}（replay）两种双层形状，estómago 只拆一层，FOUND 候选
被默认成空。修复后合同：registry 出站恒为单层 {ok, data:packet}；
重放标记并进 packet 顶层（与通用 transport 及 deletion/keep/
corrections 各域的重放惯例一致）。

验收（复审原文）：真实 registry 响应覆盖首次成功及同 request_ref
重放，断言候选正文、session 与状态确实位于出站 packet 顶层。
"""
from __future__ import annotations

import pytest

from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text, title):
    return memory.hold(
        actors["jiaming"], text=text, memory_date="2026-10-07",
        date_confidence="exact", original_title=title,
        categories=["sweet"], mood=None, our_words=None,
        creation_mode="contemporaneous", raw_pending=False)


def _start(actors, terms, request_ref):
    return registry.invoke(
        actors["jiaming"], "memory.recall.start",
        {"query_plan": {"original_request": "复审 R01 回归查询",
                        "channels": ["event"],
                        "lexical_terms": list(terms),
                        "request_ref": request_ref}}, None)


class TestR01SingleEnvelope:
    def test_first_success_packet_is_bare(self, actors):
        _hold(actors, "正文里有独特雪松R01", "雪松R01")
        res = _start(actors, ["雪松R01"], "rr01-start-1")
        assert res["ok"] is True
        packet = res["data"]
        # 裸 packet：必需字段在顶层，不再有内层 {"data": ...} 信封
        assert isinstance(packet["candidates"], list)
        assert packet["candidates"], "词项命中应至少交付一个候选"
        assert isinstance(packet["recall_session_id"], str) \
            and packet["recall_session_id"]
        assert isinstance(packet["search_status"], str) \
            and packet["search_status"]
        assert "data" not in packet
        assert "idempotent_replay" not in packet

    def test_same_request_ref_replay_bare_with_top_marker(self, actors):
        _hold(actors, "正文里有独特青柏R01", "青柏R01")
        r1 = _start(actors, ["青柏R01"], "rr01-start-2")
        r2 = _start(actors, ["青柏R01"], "rr01-start-2")
        p1, p2 = r1["data"], r2["data"]
        assert p2["recall_session_id"] == p1["recall_session_id"], \
            "同 request_ref 重放必须回到原 session"
        assert p2["idempotent_replay"] is True
        assert p2["candidates"] == p1["candidates"]
        assert "data" not in p2
