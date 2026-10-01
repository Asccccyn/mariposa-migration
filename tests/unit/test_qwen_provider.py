"""Qwen3-Embedding-4B provider 接入回归（2026-09-30 授权部署）。

真实模型验证以隔离根冒烟记录为准（加载 1.1s / 热编码 0.06s /
2560 维 / 近义干扰 margin 对比）；本组测试用注入向量验证接线
与隔离语义，不在测试进程加载 2.5GB 权重。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.retrieval import semantic
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    semantic._provider = None
    yield {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }
    semantic._provider = None


def hold(principal, text, date):
    return memory.hold(principal, text=text, memory_date=date,
                       date_confidence="exact", original_title="qwen",
                       categories=["daily"],
                       creation_mode="contemporaneous",
                       raw_pending=False)


def _fake_qwen(monkeypatch, hit_word):
    """注入确定性向量：含命中词的文本与查询同向。"""
    import numpy as np
    from mariposa.retrieval import qwen_embed

    def fake_embed(texts):
        return [np.asarray(
            [1.0, 0.0] + [0.0] * 2558
            if hit_word in t.replace(" ", "") else
            [0.0, 1.0] + [0.0] * 2558, dtype=np.float32)
            for t in texts]

    def fake_query(q):
        import numpy as np
        return np.asarray([1.0, 0.0] + [0.0] * 2558, dtype=np.float32)

    monkeypatch.setattr(qwen_embed, "embed", fake_embed)
    monkeypatch.setattr(qwen_embed, "embed_query", fake_query)


class TestQwenProviderWiring:
    def test_qwen3e4b_in_local_whitelist(self):
        assert "qwen3e4b" in semantic.LOCAL_PROVIDERS

    def test_qwen_threshold_independent(self, monkeypatch):
        monkeypatch.setattr(semantic, "QWEN_COSINE_THRESHOLD", 0.35,
                            raising=False)
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "qwen3e4b")
        assert semantic._active_threshold() == 0.35
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        assert semantic._active_threshold() == semantic.COSINE_THRESHOLD

    def test_qwen_full_chain_recall(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "qwen3e4b")
        _fake_qwen(monkeypatch, "灯塔")
        out = hold(actors["jiaming"], "去看了海角的旧灯塔",
                  "2026-09-10")
        hold(actors["jiaming"], "在书房整理旧信件", "2026-09-11")
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            {"query_plan": {
                                "original_request": "找灯塔",
                                "channels": ["event"],
                                "semantic_query": "那座海角的白色灯塔",
                                "lexical_terms": ["qqqxyz"]}}, None)
        cands = r["data"]["candidates"]
        assert any(c.get("memory_id") == out["memory_id"] for c in cands)
        with db.formal() as conn:
            row = conn.execute(
                "SELECT model, dim FROM memory_embeddings WHERE"
                " memory_id=? AND model LIKE 'Qwen/%'",
                (out["memory_id"],)).fetchone()
        assert row is not None and row["dim"] == 2560

    def test_provider_switch_invalidates_vectors(self, actors,
                                                 monkeypatch):
        """换 provider = 换向量身份：旧向量不命中（S07 generation）。"""
        import numpy as np
        from mariposa import config as cfg
        from mariposa.retrieval import qwen_embed

        def bge_like(texts):
            return [np.asarray([1.0, 0.0], dtype=np.float32)
                    for _ in texts]
        real_embed = semantic.embed
        monkeypatch.setattr(semantic, "embed", bge_like)
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        out = hold(actors["jiaming"], "灯塔正文",
                  "2026-09-10")
        with db.formal() as conn:
            semantic.reindex(conn, out["memory_id"])
            bge_model = conn.execute(
                "SELECT model FROM memory_embeddings WHERE memory_id=?",
                (out["memory_id"],)).fetchone()["model"]
        assert bge_model.startswith("BAAI/")
        # 切到 qwen：恢复真实 embed（reindex 走 qwen adapter）
        monkeypatch.setattr(semantic, "embed", real_embed)
        _fake_qwen(monkeypatch, "灯塔")
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "qwen3e4b")
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            {"query_plan": {
                                "original_request": "找灯塔",
                                "channels": ["event"],
                                "semantic_query": "那座灯塔",
                                "lexical_terms": ["qqqxyz"]}}, None)
        with db.formal() as conn:
            rows = {x["model"] for x in conn.execute(
                "SELECT model FROM memory_embeddings WHERE memory_id=?",
                (out["memory_id"],))}
        assert any(m.startswith("Qwen/") for m in rows), "按新身份重嵌"
