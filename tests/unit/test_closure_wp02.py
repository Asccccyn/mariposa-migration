"""WP02：外发 profile / 指纹扩充 / 命中取窗（S09/S15/S16/S18）。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.retrieval.evidence import excerpt
from mariposa.retrieval.judges import cache
from mariposa.retrieval.judges.typesafe_jev import TypeSafeJevJudge
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "agent", "claude_chat",
                                      "bj", "bj"),
    }


class TestDataProfile:
    def test_parse_valid_and_fail_closed(self, monkeypatch):
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "event_excerpt, word_excerpt")
        j = TypeSafeJevJudge()
        assert j._data_profile == frozenset(
            {"event_excerpt", "word_excerpt"})

    def test_unknown_field_fails_closed(self, monkeypatch):
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "event_excerpt, everything")
        j = TypeSafeJevJudge()
        assert j._data_profile is None
        assert j._disabled_reason == "allowed_data_profile_invalid"

    def test_compat_event_excerpt_only(self, monkeypatch):
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "event_excerpt_only")
        j = TypeSafeJevJudge()
        assert j._data_profile == frozenset({"event_excerpt"})

    def test_word_excerpt_blocked_without_grant(self, monkeypatch):
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "event_excerpt")
        j = TypeSafeJevJudge()
        j._api_key = "k"  # 绕过 key 检查聚焦 profile 行为
        cand = {"candidate_ref": "w1", "resource_ref": "word:w1",
                "channel": "word", "matched_fields": ["our_words.text"],
                "excerpt": "原话片段内容"}
        proj = j._candidate_projection(cand)
        word_segs = [seg for seg in proj["segments"]
                     if seg["field"] == "our_words"]
        assert not word_segs, "无 word_excerpt 许可不外发话语"

    def test_event_excerpt_granted_passes(self, monkeypatch):
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "event_excerpt")
        j = TypeSafeJevJudge()
        cand = {"candidate_ref": "m1", "resource_ref": "memory:m1",
                "channel": "event", "matched_fields": ["event_text"],
                "excerpt": "事件正文片段",
                "_row": {"whitelist_body": "事件正文片段"}}
        proj = j._candidate_projection(cand)
        ev_segs = [seg for seg in proj["segments"]
                   if seg["field"] == "event_text"]
        assert ev_segs and "事件正文片段" in ev_segs[0]["text"]


class TestFingerprintExpansion:
    def _ident(self, plan_diff):
        qp = {"original_request": "r", "explicit_constraints": {},
              "evidence_requirement": None}
        qp.update(plan_diff)
        return cache.rerank_identity(
            query_projection=qp,
            candidate_projection={"excerpt": "e", "channel": "event"},
            candidate_ref="m", candidate_version="1",
            representation_version="1", projection_version="p",
            requested_model="m", prompt_version="1",
            policy_version="1", schema_version="1")["cache_key"]

    def test_semantic_query_changes_identity(self):
        a = self._ident({"semantic_query": "海边"})
        b = self._ident({"semantic_query": "山间"})
        assert a != b, "S18：semantic_query 变化必须换缓存身份"

    def test_negative_constraints_change_identity(self):
        a = self._ident({})
        b = self._ident({"explicit_negative_constraints":
                         {"categories_excluded": ["sad"]}})
        assert a != b

    def test_exact_phrases_and_intent_change_identity(self):
        base = self._ident({})
        assert self._ident({"exact_phrases": ["原话"]}) != base
        assert self._ident({"intent": "find_words"}) != base


class TestAnchorWindow:
    def test_late_hit_not_swallowed(self):
        head = "前" * 800
        text = head + "这里是命中的关键词位置" + "后" * 100
        snip, truncated = excerpt(text, limit=120,
                                  anchors=["关键词"])
        assert truncated
        assert "命中" in snip.replace(" ", "") or "关键词" in snip, \
            "S09：800 字后的命中不得被固定头部截断吞掉"

    def test_short_text_untouched(self):
        assert excerpt("短文本", anchors=["x"]) == ("短文本", False)
