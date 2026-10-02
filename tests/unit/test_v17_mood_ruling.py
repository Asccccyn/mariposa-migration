"""2026-09-30 心情字段裁定回归（十六-3/6/7/8/9/10/13/14/15）。

- mood_note / why / meaning 为禁检来源：经 memory.search、memory.recall、
  recall 主链、dense（fake embedder）任何通道都不得因文本匹配召回
- mood_tags 是结构化筛选（filterable/visible/non-ranking）
- mood_note 非原文（is_source_text=False）+ mood_written_at 后补语义
- mood.write 轻量补写/修正；note=null 合法；不为完整性编内容
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
        out = hold(actors["jiaming"], "一段平静的日常叙述",
                   mood={"text": "惘湎心情词其实有点吃醋", "tags": ["想念"]},
                   why_remember="因为紫藤花架值得记")
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
            cands = r["data"]["data"]["candidates"]
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
        cands = r["data"]["data"]["candidates"]
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

    def test_mood_write_append_and_revise(self, actors):
        out = hold(actors["jiaming"], "待补心情的正文")
        mid = out["memory_id"]
        r1 = registry.invoke(actors["jiaming"], "memory.mood.write",
                             {"memory_id": mid, "note": "后来补的当时心情",
                              "tags": ["想念"]}, None)
        assert r1["data"]["note_present"] is True
        with db.formal() as conn:
            got = memory.get(conn, mid)["mood"]
        assert got["text"] == "后来补的当时心情"
        assert got["tags"] == ["想念"]
        assert got["is_source_text"] is False
        first_written = got["mood_written_at"]
        # 修正：覆盖 + 留审计，note=null 合法
        r2 = registry.invoke(actors["jiaming"], "memory.mood.write",
                             {"memory_id": mid, "note": None,
                              "tags": ["安心"]}, None)
        assert r2["data"]["note_present"] is False
        with db.formal() as conn:
            got2 = memory.get(conn, mid)["mood"]
        assert got2["text"] is None
        assert got2["tags"] == ["安心"]
        assert got2["mood_written_at"] >= first_written, \
            "后补时间戳推进（event_date+mood_written_at 表达后补）"

    def test_mood_write_jiaming_only(self, actors):
        out = hold(actors["jiaming"], "权限正文")
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            memory.mood_write("qiaosheng", out["memory_id"], note="x")

    def test_written_mood_still_not_searchable(self, actors):
        out = hold(actors["jiaming"], "补写心情后检索正文")
        mid = out["memory_id"]
        registry.invoke(actors["jiaming"], "memory.mood.write",
                        {"memory_id": mid, "note": "补写里的罕见词梼杌"}, None)
        with db.formal() as conn:
            assert not rsearch.search(conn, "梼杌")["hits"], \
            "十六-8：mood_note 不进 BM25"
