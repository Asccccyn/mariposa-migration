"""WP05：words 专项 dense 与三入口一致（S08）。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service, store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold_words(principal, words):
    return memory.hold(principal, text="普通事件正文",
                       memory_date="2026-09-25", date_confidence="exact",
                       original_title="wp05", categories=["daily"],
                       creation_mode="contemporaneous", raw_pending=False,
                       our_words=words)


class TestWordsDense:
    def test_dense_recalls_paraphrased_word(self, actors, monkeypatch):
        """S08：词义近邻召回（fake embedder：语义查询 ↔ 话语改述）。"""
        from mariposa import config as cfg
        from mariposa.retrieval import semantic, words_semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")

        def fake(texts):
            return [np.asarray(
                [1.0, 0.0] if "海边的风" in t.replace(" ", "")
                else [0.0, 1.0], dtype=np.float32) for t in texts]
        monkeypatch.setattr(semantic, "embed", fake)
        monkeypatch.setattr(words_semantic, "embed", fake)

        hold_words(actors["jiaming"], [
            {"speaker": "qiaosheng", "text": "海边的风好大啊",
             "expression_kind": "verbatim"}])
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "zzz不命中的词",
            "semantic_query": "海边的风"})
        cands = p["candidates"]
        assert cands, "words dense 应召回语义近邻话语"
        assert any("海边的风" in (c.get("excerpt") or
                                 str(c.get("evidence")))
                   for c in cands)

    def test_dense_separate_space_from_event(self, actors, monkeypatch):
        """S08/#6：word 向量独立空间且 model 身份绑定当前 provider。"""
        from mariposa import config as cfg
        from mariposa.retrieval import words_semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        # 触发一次 words dense 建表建向量
        hold_words(actors["jiaming"], [
            {"speaker": "qiaosheng", "text": "空间身份测试话语",
             "expression_kind": "verbatim"}])
        recall_service.words_recall(actors["jiaming"], {
            "query": "qqqxyz", "semantic_query": "空间身份"})
        with db.formal() as conn:
            models = {r["model"] for r in conn.execute(
                "SELECT DISTINCT model FROM word_embeddings")}
        from mariposa.retrieval.words_semantic import _active_model_key
        want = _active_model_key()
        # 换代隔离：不同 provider 身份的行可共存（generation 隔离设计），
        # 但当前 provider 的行必须存在且身份精确
        assert want in models, \
            f"当前 provider 向量缺失：{models}"
        assert all(m != "words|wordbody-v1" for m in models), \
            "不得存在无模型身份的旧格式行"

    def test_fingerprint_invalidates_on_edit(self, actors, monkeypatch):
        """S18/S08：话语编辑/来源变化 → 旧向量失效重嵌。"""
        from mariposa import config as cfg
        from mariposa.retrieval import semantic, words_semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        calls = []

        def fake(texts):
            calls.append(list(texts))
            return [np.asarray([1.0, 0.0], dtype=np.float32)
                    for _ in texts]
        monkeypatch.setattr(semantic, "embed", fake)
        monkeypatch.setattr(words_semantic, "embed", fake)
        m = hold_words(actors["jiaming"], [
            {"speaker": "jiaming", "text": "初版话语内容",
             "expression_kind": "verbatim"}])
        # 先跑一次建立向量，再取基线指纹
        recall_service.words_recall(actors["jiaming"], {
            "query": "zzz", "semantic_query": "初版话语内容"})
        with db.formal() as conn:
            wid = conn.execute(
                "SELECT word_id FROM memory_our_words WHERE memory_id=?",
                (m["memory_id"],)).fetchone()["word_id"]
            fp1 = conn.execute(
                "SELECT word_fingerprint FROM word_embeddings"
                " WHERE word_id=?", (wid,)).fetchone()["word_fingerprint"]
        # 编辑该词条文本 → 词条指纹变化 → 旧向量失效重嵌
        with db.formal() as conn:
            conn.execute(
                "UPDATE memory_our_words SET text='二版话语内容'"
                " WHERE word_id=?", (wid,))
        recall_service.words_recall(actors["jiaming"], {
            "query": "zzz", "semantic_query": "二版话语内容"})
        with db.formal() as conn:
            fp2 = conn.execute(
                "SELECT word_fingerprint FROM word_embeddings"
                " WHERE word_id=?", (wid,)).fetchone()["word_fingerprint"]
        assert fp1 and (fp2 is None or fp2 != fp1), \
            "词身份变化必须令旧向量失效"


class TestUnifiedEntries:
    def test_all_three_entries_sessionized_and_bounded(self, actors):
        """三入口（session words / words.recall / find_words）同一
        统一语义：packet 形态、≤3、judged、session 化（预算不免费）。"""
        hold_words(actors["jiaming"], [
            {"speaker": "qiaosheng", "text": "窗外的梧桐叶落了",
             "expression_kind": "verbatim"}])
        # 1) 独立 words.recall
        p1 = recall_service.words_recall(actors["jiaming"],
                                         {"query": "梧桐"})
        # 2) find_words（registry）
        p2 = registry.invoke(actors["jiaming"], "memory.find_words",
                             {"query": "梧桐",
                              "original_request": "找梧桐话语"}, None)
        # 3) session words（start channels=words）
        p3 = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "找梧桐话语", "channels": ["words"],
            "lexical_terms": ["梧桐"]}})
        for out in (p1, p2["data"], p3):
            assert out["candidates"], "入口应有命中"
            assert len(out["candidates"]) <= 3
            assert out["delivery_action"] == "needs_validation"
        # 独立入口消耗预算（session 化，非免费额度）
        with db.recall_runtime() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM recall_rounds WHERE"
                " kind='words'").fetchone()["c"]
        assert n >= 2, "独立入口必须记 words 轮（预算复用）"

    def test_speaker_filter_applies_to_dense_too(self, actors,
                                                 monkeypatch):
        from mariposa import config as cfg
        from mariposa.retrieval import semantic, words_semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")

        def fake(texts):
            return [np.asarray([1.0, 0.0], dtype=np.float32)
                    for _ in texts]
        monkeypatch.setattr(semantic, "embed", fake)
        monkeypatch.setattr(words_semantic, "embed", fake)
        hold_words(actors["jiaming"], [
            {"speaker": "jiaming", "text": "晚风说话者甲",
             "expression_kind": "verbatim"},
            {"speaker": "qiaosheng", "text": "晚风说话者乙",
             "expression_kind": "verbatim"}])
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "zzz", "semantic_query": "晚风",
            "explicit_constraints": {"speaker": "qiaosheng"}})
        assert p["candidates"]
        assert all(c.get("speaker") == "qiaosheng"
                   for c in p["candidates"]), \
            "S08：dense 候选同样应用 speaker 条件"
