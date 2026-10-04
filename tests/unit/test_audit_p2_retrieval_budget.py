"""P2 检索/预算批次回归（审计 2026-10-03）。

- F17：words 词项编译保持组内顺序与相邻——"小路灯"不命中
  "灯在小路旁"。
- F18：Jev query 投影含 lexical_terms（缓存身份随词法目标变化）。
- F19：HTTP 中途断流（IncompleteRead）结构化降级，不裸异常。
- F20：成功包预算预览的剩余额度同步扣减。
"""
from __future__ import annotations

import http.client
import json

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import budget
from mariposa.retrieval.judges import typesafe_jev
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold_words(actors, text, words):
    return memory.hold(
        actors["jiaming"], text=text, memory_date="2026-09-20",
        date_confidence="exact", original_title="p2w",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False,
        our_words=[{"speaker": "qiaosheng", "text": w,
                    "expression_kind": "paraphrase"} for w in words])


# ---------------------------------------------------------------- F17

def test_f17_term_requires_order_and_adjacency(actors):
    from mariposa.retrieval import words as words_mod
    _hold_words(actors, "乱序话语事件", ["灯在小路旁"])
    plan = {"original_request": "找小路灯",
            "channels": ["words"], "lexical_terms": ["小路灯"]}
    with db.formal() as conn:
        out = words_mod.words_search(conn, plan)
    assert out["hits"] == [], \
        f"乱序不相邻的'灯在小路旁'不得命中词项'小路灯'：{out['hits']}"
    _hold_words(actors, "连续话语事件", ["门口那盏小路灯坏了"])
    with db.formal() as conn:
        out2 = words_mod.words_search(conn, plan)
    assert out2["hits"], "连续出现的'小路灯'应命中"


# ---------------------------------------------------------------- F18

def test_f18_query_projection_includes_lexical_terms():
    j = typesafe_jev.TypeSafeJevJudge()
    p1 = j._query_projection({"original_request": "上次约会",
                              "lexical_terms": ["海边"]})
    p2 = j._query_projection({"original_request": "上次约会",
                              "lexical_terms": ["山里"]})
    assert p1.get("lexical_terms") == ["海边"]
    assert p1 != p2, "词法目标变化必须构成新判断身份（缓存键随动）"


# ---------------------------------------------------------------- F19

def test_f19_incomplete_read_degrades_structured(monkeypatch):
    class _BrokenBody:
        def read(self, *a, **kw):
            raise http.client.IncompleteRead(b"partial")

    class _Resp:
        def __init__(self):
            self._b = _BrokenBody()
        def read(self):
            return self._b.read()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        return _Resp()

    monkeypatch.setattr(typesafe_jev.urllib.request, "urlopen",
                        fake_urlopen)
    j = typesafe_jev.TypeSafeJevJudge()
    j._api_key = "k"
    from mariposa.retrieval.judges.typesafe_jev import JudgeUnavailable
    with pytest.raises(JudgeUnavailable):
        j._post_with_limited_retry({"anything": 1})


# ---------------------------------------------------------------- F20

def test_f20_preview_remaining_consistent(actors):
    session = {"session_id": "s-f20", "current_burst": 1,
               "rounds_used": 0, "bursts_used": 0}
    plain = budget.snapshot(session)
    assert plain["rounds_remaining_in_burst"] >= 1
    preview = dict(session)
    preview["rounds_used"] = 1
    preview["_round_preview_offset"] = 1
    snap = budget.snapshot(preview)
    assert snap["rounds_remaining_in_burst"] == \
        plain["rounds_remaining_in_burst"] - 1, \
        "预览含本轮成功时，剩余额度须同步扣减（不得 1 已用/满额剩余）"
    # 硬门仍按已提交事实：预览标记不影响 rounds_left_in_burst 本身
    assert budget.rounds_left_in_burst(session) == \
        plain["rounds_remaining_in_burst"]
