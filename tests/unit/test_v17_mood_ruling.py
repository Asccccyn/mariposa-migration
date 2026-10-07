"""2026-09-30 心情字段裁定回归（十六-3/6/7/8/9/10/13/14/15）。

- mood_note / why / meaning 为禁检来源：经 memory.search、memory.recall、
  recall 主链、dense（fake embedder）任何通道都不得因文本匹配召回
- mood_tags 是结构化筛选（filterable/visible/non-ranking）
- mood_note 非原文（is_source_text=False）+ mood_written_at 后补语义
- 2026-10-05 终裁：不能补写——mood.write 通道已删，心情仅建桶当下可写
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.retrieval import search as rsearch
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold(principal, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="裁定",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(principal, **base)


class TestForbiddenSourcesNeverRecall:
    def test_mood_note_why_meaning_invisible_to_all_channels(self, actors):
        # why_remember 已删除（2026-10-05 裁定）：解释槽统一走心情层，
        # 两段解释文字并入同一条 mood note，断言不变——解释文字对全
        # 检索通道不可见
        out = hold(actors["jiaming"], "一段平静的日常叙述",
                   mood={"text": "惘湎心情词其实有点吃醋；因为紫藤花架值得记",
                         "tags": ["爱"]})
        mid = out["memory_id"]
        from mariposa.memory import listing as mlisting
        for probe in ("惘湎", "吃醋", "紫藤花架", "雾隐"):
            with db.formal() as conn:
                assert not rsearch.search(conn, probe)["hits"], \
                    f"memory.search 命中禁检来源：{probe}"
                assert not rsearch.recall(conn, probe)["hits"], \
                    f"memory.recall 命中禁检来源：{probe}"
            r = registry.invoke(actors["jiaming"], "memory.recall.start",
                                { "operation_id": f"op-mood-{probe}","query_plan": {
                                    "original_request": probe,
                                    "channels": ["event"],
                                    "lexical_terms": [probe]}}, None)
            cands = r["data"]["candidates"]
            assert all(c.get("memory_id") != mid for c in cands), \
                f"recall 主链命中禁检来源：{probe}"

    def test_forbidden_sources_never_enter_dense(self, actors, monkeypatch):
        """fake embedder：禁检来源词的语义向量不得召回（语料只含正文）。"""
        from mariposa import config as cfg
        from mariposa.retrieval import semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")

        def fake_embed(texts):
            out = []
            for t in texts:
                compact = t.replace(" ", "")
                out.append(np.asarray(
                    [1.0, 0.0] if "雾隐" in compact else [0.0, 1.0],
                    dtype=np.float32))
            return out
        monkeypatch.setattr(semantic, "embed", fake_embed)
        from mariposa.memory import listing as mlisting
        out = hold(actors["jiaming"], "河边普通散步正文")
        mid = out["memory_id"]
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            { "operation_id": "op-auto-test_v-0","query_plan": {
                                "original_request": "找雾隐",
                                "channels": ["event"],
                                "semantic_query": "雾隐",
                                "lexical_terms": ["zzz不存在"]}}, None)
        cands = r["data"]["candidates"]
        assert all(c.get("memory_id") != mid for c in cands), \
            "dense 经禁检来源（meaning）召回"

    def test_body_still_recalls(self, actors):
        out = hold(actors["jiaming"], "正文里的青苔石阶")
        with db.formal() as conn:
            hits = rsearch.search(conn, "青苔")["hits"]
        assert any(h["memory_id"] == out["memory_id"] for h in hits)

    def test_mood_tags_filterable_not_ranking(self, actors):
        """mood_tags 走结构化筛选（memory.recall filters），
        不作为文本相关性字段参与命中。"""
        a = hold(actors["jiaming"], "甲桶正文海边",
                 mood={"text": None, "tags": ["吃醋"]})
        hold(actors["jiaming"], "乙桶正文山间")
        with db.formal() as conn:
            out = rsearch.recall(conn, "", filters={
                "mood_tags": ["吃醋"], "mood_match": "any"})
            ids = {h["memory_id"] for h in out["hits"]}
        assert a["memory_id"] in ids, "mood_tags 结构化筛选生效"
        # 标签词不作为自由文本命中
        with db.formal() as conn:
            assert not rsearch.search(conn, "吃醋")["hits"]


class TestMoodProvenanceAndWrite:
    def test_get_mood_marks_non_source_and_written_at(self, actors):
        out = hold(actors["jiaming"], "带心情的正文",
                   mood={"text": "我当时其实有点吃醋", "tags": ["吃醋"]})
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])
        assert got["mood"]["is_source_text"] is False
        assert got["mood"]["mood_written_at"]
        assert got["mood"]["text"] == "我当时其实有点吃醋"

    def test_mood_write_channel_removed(self, actors):
        """2026-10-05 终裁：心情不能补写——mood.write 通道整体删除。

        心情只能在建桶当下（同期）经 hold 的 mood 参数写入；之后
        想表达"后来回看的心情"= 新桶 + relation 连回原桶。
        """
        from mariposa.capabilities import registry as reg
        assert "memory.mood.write" not in reg.REGISTRY
        assert not hasattr(memory, "mood_write")
        out = hold(actors["jiaming"], "建桶后想补心情的正文",
                   mood={"text": "当时就有的心情", "tags": ["开心"]})
        with db.formal() as conn:
            got = memory.get(conn, out["memory_id"])["mood"]
        assert got["text"] == "当时就有的心情"
        assert got["tags"] == ["开心"]


    def test_mood_write_jiaming_only(self, actors):
        # 2026-10-05 终裁：mood.write 已删——权限门收敛到 hold 期：
        # qiaosheng 建 mood 直接拒绝（仅周家明）
        from mariposa.errors import Forbidden
        qs = actors.get("qiaosheng")
        if qs is not None:
            with pytest.raises(Forbidden):
                hold(qs, "权限正文",
                     mood={"text": "x", "tags": ["生气"]})

    def test_written_mood_still_not_searchable(self, actors):
        out = hold(actors["jiaming"], "心情正文",
                   mood={"text": "心情里的罕见词梼杌", "tags": []})
        with db.formal() as conn:
            assert not rsearch.search(conn, "梼杌")["hits"], \
            "十六-8：mood_note 不进 BM25（hold 期写入同样禁检）"
