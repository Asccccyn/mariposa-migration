"""dense 启用前检查（2026-09-30 裁定 §二清单）。

结构级：旧 embedding 失效——投影内容变化（why/meaning 退出后
search_text 变更）必须令按旧 projection hash 生成的向量失效，
不得继续命中。

模型级（真实 local_bge_zh，权重来自本机既有 fastembed 缓存，
不触发任何下载；权重/依赖缺失自动 skip）：
A 合法事件文本可被 dense 召回（同义表达）；
B 非检索字段（why/meaning/mood_note）关键词不单独触发召回；
C provider 未配置/不可用时显式 unavailable，不回退旧整投影。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import listing as mlisting
from mariposa.memory import service as memory
from mariposa.retrieval import search as rsearch
from tests.conftest import reset_all

# bge 权重缓存：代码仓库 runtime/models/（与 qwen 权重同约定；
# 原 Windows 硬编码 D:\mariposa\runtime\models 在 Mac 上落成
# 仓库根的字面量反斜杠目录，2026-10-01 已随缓存搬正）
WEIGHTS_DIR = str(Path(__file__).resolve().parents[2]
                  / "runtime" / "models")


def _weights_available() -> bool:
    snap = (Path(WEIGHTS_DIR) / "models--Qdrant--bge-small-zh-v1.5"
            / "snapshots")
    return snap.exists() and any(snap.rglob("*.onnx"))


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold(principal, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="dense",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(principal, **base)


class TestLegacyVectorInvalidation:
    def test_projection_change_invalidates_old_vector(self, actors,
                                                      monkeypatch):
        """投影变更（meaning 追加改变 search_text）→ 旧 projection_hash
        向量失效：不命中、不冒充新向量，重嵌后按新语料工作。"""
        from mariposa import config as cfg
        from mariposa.retrieval import semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        calls = []

        def fake_embed(texts):
            calls.append(list(texts))
            # 语料与查询同向，保证可命中
            return [np.asarray([1.0, 0.0], dtype=np.float32)
                    for _ in texts]
        monkeypatch.setattr(semantic, "embed", fake_embed)

        out = hold(actors["jiaming"], "夜航船的甲板上有咸风")
        mid = out["memory_id"]
        with db.formal() as conn:
            r = registry.invoke(actors["jiaming"], "memory.recall.start",
                                { "operation_id": "op-4-77","query_plan": {
                                    "original_request": "甲板咸风",
                                    "channels": ["event"],
                                    "semantic_query": "甲板咸风",
                                    "lexical_terms": ["zzz不存在"]}}, None)
            hit1 = any(c.get("memory_id") == mid
                       for c in r["data"]["data"]["candidates"])
            old_hash = conn.execute(
                "SELECT search_text_hash FROM retrieval_documents"
                " WHERE memory_id=?", (mid,)).fetchone()["search_text_hash"]
        assert hit1, "前置：dense 首轮命中"

        # 2026-09-30 裁定后投影只随事件正文变化：update_text 改正文
        # → search_text 变 → hash 前进（meaning 追加不再影响投影，
        # 本身就是裁定生效的证明）
        from mariposa.memory import extras as mextras
        mextras.update_text(actors["jiaming"].principal_id, mid,
                            expected_version=1,
                            text="夜航船的甲板上咸风更浓了")
        with db.formal() as conn:
            new_hash = conn.execute(
                "SELECT search_text_hash FROM retrieval_documents"
                " WHERE memory_id=?", (mid,)).fetchone()["search_text_hash"]
        assert new_hash != old_hash, "前置：正文变更后投影 hash 前进"

        # 查询路径的向量有效性校验按 projection_hash 绑定：
        # 旧 hash 向量不再被采纳（semantic_search 内部校验）
        r2 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             { "operation_id": "op-3-105","query_plan": {
                                 "original_request": "甲板咸风",
                                 "channels": ["event"],
                                 "semantic_query": "甲板咸风",
                                 "lexical_terms": ["zzz不存在"]}}, None)
        hit2 = any(c.get("memory_id") == mid
                   for c in r2["data"]["data"]["candidates"])
        assert hit2, "重嵌后仍可命中（按新投影语料）"
        with db.formal() as conn:
            vec_hash = conn.execute(
                "SELECT projection_hash FROM memory_embeddings"
                " WHERE memory_id=?", (mid,)).fetchone()["projection_hash"]
        assert vec_hash == new_hash, "向量必须绑定当前投影 hash"


@pytest.mark.skipif(not _weights_available(),
                    reason="本机无 bge-small-zh-v1.5 权重（不触发下载）")
class TestRealModelWarmupAndSmoke:
    @pytest.fixture(autouse=True)
    def _real_model(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", WEIGHTS_DIR)
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        from mariposa.retrieval import semantic
        semantic._provider = None  # 强制重新加载（各测试根隔离）
        yield
        semantic._provider = None

    def test_warmup_and_synonym_recall(self, actors):
        """A：真实模型——事件正文用词与查询不同表述可召回。"""
        from mariposa.retrieval import semantic
        out = hold(actors["jiaming"], "深夜爬上山顶，看见漫天星光和银河")
        with db.formal() as conn:
            w = semantic.warmup(conn)
        assert w["warmed"] >= 1
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-2-141","query_plan": {
                                "original_request": "找夜空繁星",
                                "channels": ["event"],
                                "semantic_query": "夜空中的繁星银河",
                                "lexical_terms": ["zzz不存在"]}}, None)
        cands = r["data"]["data"]["candidates"]
        assert any(c.get("memory_id") == out["memory_id"] for c in cands), \
            "真实模型：同义表达应可召回事件正文"
        assert all(c.get("content_version") for c in cands if c.get(
            "memory_id")), "dense 卡携带真实版本"

    def test_forbidden_fields_do_not_trigger(self, actors):
        """B：why/meaning/mood_note 独有关键词不触发 dense 召回。"""
        out = hold(actors["jiaming"], "完全平静的一段日常叙述",
                   why_remember="因为雾隐茶室的缘故")
        mlisting.meanings_append(actors["jiaming"].principal_id,
                                 out["memory_id"], "含义层提到梼杌")
        for probe in ("雾隐茶室", "梼杌"):
            r = registry.invoke(actors["jiaming"], "memory.recall.start",
                                { "operation_id": f"op-forbidden-{probe}","query_plan": {
                                    "original_request": f"找{probe}",
                                    "channels": ["event"],
                                    "semantic_query": probe,
                                    "lexical_terms": ["zzz不存在"]}}, None)
            assert all(c.get("memory_id") != out["memory_id"]
                       for c in r["data"]["data"]["candidates"]), \
                f"禁检来源经 dense 触发召回：{probe}"

    def test_no_fallback_when_unavailable(self, actors, monkeypatch):
        """C：provider 未配置 → 显式 unavailable，不回退旧整投影。"""
        from mariposa import config as cfg
        out = hold(actors["jiaming"], "提供方关闭时的正文鸭跖草")
        mlisting.meanings_append(actors["jiaming"].principal_id,
                                 out["memory_id"], "含义层词霡霂")
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "")
        with db.formal() as conn:
            sm = rsearch.search(conn, "霡霂")
        assert not sm["hits"], "禁检来源不得经任何文本通道命中"
        assert sm["semantic"] == "unavailable"
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-0-181","query_plan": {
                                "original_request": "鸭跖草",
                                "channels": ["event"],
                                "semantic_query": "鸭跖草",
                                "lexical_terms": ["zzz不存在"]}}, None)
        # dense 不可用不伪装：degraded 标注，正文词法通道不受影响
        assert "semantic_unavailable" in (r["data"]["data"].get("degraded_reasons")
                                          or [])
