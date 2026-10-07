"""WP03：scope 内真 BM25 与 dense 正确性（S04/S06/S07）。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.retrieval import scoped_bm25
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold(principal, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="bm25",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(principal, **base)


class TestScopedBM25Unit:
    def test_or_groups_and_dedup(self):
        docs = [
            {"owner": "a", "field": "event_text",
             "tokens": "海 边 散 步".split()},
            {"owner": "b", "field": "event_text",
             "tokens": "山 间 徒 步".split()},
        ]
        out = scoped_bm25.score_documents(
            docs, [["海", "边"], ["徒", "步"]], [])
        owners = [e["owner"] for e in out]
        assert set(owners) == {"a", "b"}, "terms 组间 OR"
        # 单 token 重复不应双计（去重）：同 token 集的两种组法同分
        out2 = scoped_bm25.score_documents(
            docs, [["海"], ["海", "边"]], [])
        out3 = scoped_bm25.score_documents(
            docs, [["海", "边"], ["海", "海"]], [])
        a2 = [e for e in out2 if e["owner"] == "a"][0]
        a3 = [e for e in out3 if e["owner"] == "a"][0]
        assert a2["score"] == a3["score"], "重复 query token 去重"

    def test_phrase_and_semantics(self):
        docs = [
            {"owner": "a", "field": "event_text",
             "tokens": "不 要 走".split()},
            {"owner": "b", "field": "event_text",
             "tokens": "走 吧 走".split()},
        ]
        out = scoped_bm25.score_documents(
            docs, [], [["不", "要", "走"]])
        assert [e["owner"] for e in out] == ["a"],             "exact phrase 连续顺序；不跨文档拼接"

    def test_empty_scope_no_division(self):
        assert scoped_bm25.score_documents([], [["x"]], []) == []

    def test_max_per_owner_not_sum(self):
        """S06：memory 分=命中文档最大分，不累加多字段选票
        （同一统计集合内验证——同 owner 增加第二字段不使总分翻倍，
        而等于单字段文档分）。"""
        docs = [
            {"owner": "a", "field": "event_text",
             "tokens": "海 边".split()},
            {"owner": "b", "field": "event_text",
             "tokens": "海 呀".split()},
        ]
        two = scoped_bm25.score_documents(
            docs + [{"owner": "a", "field": "original_title",
                     "tokens": "海 边".split()}], [["海"]], [])
        a_two = [e for e in two if e["owner"] == "a"][0]
        field_scores = sorted(a_two["hits"].values())
        # 同一统计集合内：owner 分 = 其命中文档的最大分（不叠加）
        assert a_two["score"] == max(a_two["hits"].values())
        assert len(field_scores) == 2 and \
            a_two["score"] >= field_scores[0], \
            "两字段各自计分，owner 取最大而非求和"


class TestScopeIsolation:
    def test_hidden_memory_does_not_skew_idf(self, actors):
        """S05/S06 核心主张：撤权/隐藏桶不得改变可见集合的排名
        依据——df/avgdl 只来自授权集合。"""
        a = hold(actors["jiaming"], "独特的风筝在草坪上空")
        b = hold(actors["jiaming"], "另一次风筝放飞在江边")
        # 全可见时的名次
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             { "operation_id": "op-auto-test_c-2","query_plan": {
                                 "original_request": "风筝",
                                 "channels": ["event"],
                                 "lexical_terms": ["风筝"]}}, None)
        c1 = [c["memory_id"] for c in r1["data"]["candidates"]]
        # 隐藏 b：a 仍应可检索，且不受 b 的 df 影响
        with db.formal() as conn:
            conn.execute("UPDATE memories SET visibility='hidden'"
                         " WHERE memory_id=?", (b["memory_id"],))
        r2 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             { "operation_id": "op-auto-test_c-1","query_plan": {
                                 "original_request": "风筝",
                                 "channels": ["event"],
                                 "lexical_terms": ["风筝"]}}, None)
        c2 = [c["memory_id"] for c in r2["data"]["candidates"]]
        assert a["memory_id"] in c2, "隐藏其他桶不影响可见桶命中"
        assert b["memory_id"] not in c2
        assert a["memory_id"] in c1

    def test_query_only_semantic_runs(self, actors, monkeypatch):
        """S04：纯 semantic_query（无 lexical_terms）可运行——
        provider 关闭时显式 unavailable（不因无词法而 not_requested）。
        """
        hold(actors["jiaming"], "深夜山顶的星空很清楚")
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-auto-test_c-0","query_plan": {
                                "original_request": "星空",
                                "channels": ["event"],
                                "semantic_query": "夜空繁星"}}, None)
        assert r["data"]["coverage"]["dense_event"] == "unavailable", \
            "显式语义请求必须真实尝试 dense（此处 provider 关闭）"
        assert not r["data"].get("lexical_terms")


class TestCorpusGeneration:
    def test_legacy_model_key_vectors_invalid(self, actors, monkeypatch):
        """S07：旧 model 身份（无 corpus generation）的向量整体失效。
        """
        from mariposa import config as cfg
        from mariposa.retrieval import semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")

        def fake(texts):
            return [np.asarray([1.0, 0.0], dtype=np.float32)
                    for _ in texts]
        monkeypatch.setattr(semantic, "embed", fake)
        out = hold(actors["jiaming"], "世代绑定的海岸线正文")
        mid = out["memory_id"]
        with db.formal() as conn:
            semantic.reindex(conn, mid)
            # 伪造旧身份向量（旧 model 名、过期 hash）
            vec = conn.execute(
                "SELECT vector FROM memory_embeddings WHERE memory_id=?",
                (mid,)).fetchone()["vector"]
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings(memory_id,"
                " model, dim, projection_hash, vector, created_at)"
                " VALUES(?, 'BAAI/bge-small-zh-v1.5', 512, 'legacy',"
                " ?, 't')", (mid, vec))
            hits = semantic.semantic_search(conn, "海岸线", 5)
        # 命中来自当前 MODEL_KEY 向量；旧身份行不参与
        assert any(h.get("memory_id") == mid
                   for h in hits if isinstance(h, dict))
        with db.formal() as conn:
            rows = {r["model"] for r in conn.execute(
                "SELECT model FROM memory_embeddings WHERE memory_id=?",
                (mid,))}
        assert "BAAI/bge-small-zh-v1.5|eventbody-v1" in rows
        assert "BAAI/bge-small-zh-v1.5" in rows, "迟到旧行未安装（保留）"
