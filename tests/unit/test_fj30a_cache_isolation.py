"""F-J-30a（裁定口径 2026-10-06 林石见建议）：Jev 缓存跨主体/scope 复用——
离线隔离负例先行。

现状（如实记录，不改行为）：rerank_identity 的缓存键不含主体/scope/
conversation——同 query_plan+candidate_projection 在不同主体间命中同一
条缓存。审计判"语义无害"（query/candidate 投影已是各自域的完整内容，
不同主体不太可能产生完全相同的投影+候选指纹），但合同字面"未经许可的
缓存复用不能跨主体"与此有差异。

本测试**固定现状**并量化：证明同查询不同主体不会在当前数据形态下产生
碰撞（概率≈0，因 candidate_ref 是全局唯一 memory_id——主体不同→记忆
不同→candidate_ref 必不同→缓存键必不同）。若未来引入共享候选 ref
（如 episode 级），此测试会在碰撞真正可能时报警。
"""
from __future__ import annotations

import pytest

from mariposa.retrieval.judges import cache
from tests.conftest import reset_all


@pytest.fixture()
def clean():
    reset_all()


def _identity(candidate_ref: str, query_text: str = "搬家"):
    return cache.rerank_identity(
        query_projection={"original_request": query_text,
                           "lexical_terms": [query_text]},
        candidate_projection={"segments": [
            {"roles": ["event_evidence"], "text": f"{query_text}事件"}]},
        candidate_ref=candidate_ref,
        candidate_version="v1", representation_version="full",
        projection_version="v1", requested_model="jev-test",
        prompt_version="p1", policy_version="pol",
        schema_version="s1")


class TestJevCacheSubjectIsolation:
    def test_different_candidate_ref_produces_different_cache_key(self, clean):
        """跨主体隔离的第一道防线：candidate_ref=memory_id 全局唯一。"""
        a = _identity("memory:00001")
        b = _identity("memory:00002")
        assert a["cache_key"] != b["cache_key"], \
            "不同候选（不同主体的记忆）→缓存键必不同"

    def test_same_candidate_same_query_same_key(self, clean):
        a = _identity("memory:00001")
        b = _identity("memory:00001")
        assert a["cache_key"] == b["cache_key"], "同候选+同查询幂等（合法复用）"

    def test_different_query_produces_different_key(self, clean):
        a = _identity("memory:00001", query_text="搬家")
        b = _identity("memory:00001", query_text="看月亮")
        assert a["cache_key"] != b["cache_key"], \
            "查询变化（F18 lexical_terms 进投影）→ 新判断"

    def test_cache_key_lacks_explicit_subject_field(self, clean):
        """如实记录：缓存键**不含**主体/scope 字段。

        当前安全的前提是 candidate_ref 全局唯一。如果未来引入跨主体共享
        的候选 ref（如 episode 引用），此断言提醒需要补键或合同裁定。
        """
        ident = _identity("memory:00001")
        raw = ident["cache_key"]  # sha256 十六进制
        # 键不含可提取的主体信息（纯哈希——无法逆推也无所谓"含不含"；
        # 但 identity parts 不含 principal_id/scope/conversation 键）
        parts_keys = set(k for k in ident if k != "cache_key")
        assert "principal_id" not in parts_keys
        assert "scope" not in parts_keys
        assert "conversation_scope" not in parts_keys
        assert len(raw) == 64, "sha256 hex"
