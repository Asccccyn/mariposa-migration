"""林石见二轮复审（2026-09-30）反例回归——#1..#7。

每条对应其独立复现的攻击面；fake judge/embedder 注入，不加载
真实权重。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service, store
from mariposa.retrieval import scoped_bm25
from mariposa.retrieval.judges import base as jb
from mariposa.retrieval.judges import typesafe_jev
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


class _Cap(jb.JudgeProvider):
    """捕获 Jev payload 的注入 provider。"""
    name = "cap_r2audit"

    def __init__(self):
        self.payloads = []

    def judge(self, plan, candidates, ctx):
        inner = typesafe_jev.TypeSafeJevJudge()
        inner._data_profile = frozenset(
            {"event_excerpt", "title_cue", "word_excerpt",
             "source_excerpt", "structured_metadata"})
        inner._current_terms = plan.get("lexical_terms") or []
        self.payloads.append(inner._payload(plan, candidates))
        return jb.JudgeBatchResult(
            items=[jb.JudgeItem(
                c.get("candidate_ref") or c["resource_ref"],
                c.get("content_version"), relevance_signal=0.8,
                evaluation_status="evaluated", model_id="cap",
                prompt_version="t") for c in candidates],
            provider_status="evaluated")


def _install(cap):
    jb.register_for_tests(cap.name, cap)
    from mariposa import config as cfg
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = cap.name
    return old


class Test1MatchedExcerpt:
    def test_our_words_hit_sends_real_word_text(self, actors):
        """审计反例：event=雨夜、words=梧桐叶——Jev 的 our_words
        段必须是真实命中句"梧桐叶落了"，不是 event 正文。"""
        cap = _Cap()
        old = _install(cap)
        try:
            memory.hold(
                actors["jiaming"], text="窗外的雨下了整夜",
                memory_date="2026-09-26", date_confidence="exact",
                original_title="雨夜", categories=["daily"],
                creation_mode="contemporaneous", raw_pending=False,
                our_words=[{"speaker": "qiaosheng",
                            "text": "窗外的梧桐叶落了",
                            "expression_kind": "verbatim"}])
            recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "梧桐",
                               "channels": ["event"],
                               "lexical_terms": ["梧桐"]}})
            segs = (cap.payloads[-1]["state"]["candidates"][0]
                    ["segments"])
            word_seg = next(s for s in segs
                            if s["field"] == "our_words")
            ev_seg = next(s for s in segs
                          if s["field"] == "event_text")
            assert "梧桐叶落了" in word_seg["text"], \
                f"our_words 段必须是真实命中句：{word_seg['text']}"
            assert "雨" in ev_seg["text"]
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_late_hit_reaches_jev(self, actors):
        """关键词在 600 字后——Jev match_evidence 含命中窗。"""
        cap = _Cap()
        old = _install(cap)
        try:
            body = "平" * 600 + "路灯亮起来了" + "静" * 50
            memory.hold(
                actors["jiaming"], text=body, memory_date="2026-09-26",
                date_confidence="exact", original_title="夜路",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
            recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "路灯",
                               "channels": ["event"],
                               "lexical_terms": ["路灯"]}})
            segs = (cap.payloads[-1]["state"]["candidates"][0]
                    ["segments"])
            ev = next(s for s in segs
                      if s["field"] == "event_text"
                      and "match_evidence" in s["roles"])
            assert "路灯" in ev["text"].replace(" ", ""), \
                "600 字后的命中必须进入 Jev 命中窗"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old


class Test2RawOrSpeaker:
    def _seed(self):
        from mariposa.source import importer
        import json as _json
        import tempfile
        import pathlib
        tmp = pathlib.Path(tempfile.mkdtemp())
        msgs_a = [{"uuid": "or-1", "sender": "human",
                   "created_at": "2026-09-28T10:00:00.000Z",
                   "content": [{"type": "text", "text": "中秋的月亮很圆"}]}]
        msgs_b = [{"uuid": "or-2", "sender": "human",
                   "created_at": "2026-09-28T11:00:00.000Z",
                   "content": [{"type": "text", "text": "约会地点定在河边"}]}]
        for tag, msgs in (("a", msgs_a), ("b", msgs_b)):
            f = tmp / f"or-{tag}.json"
            f.write_text(_json.dumps(
                [{"uuid": f"c-or-{tag}", "chat_messages": msgs}],
                ensure_ascii=False), encoding="utf-8")
            importer.import_file("jiaming", str(f))

    def test_multi_term_or_finds_both(self, actors, monkeypatch):
        """中秋+约会 OR——两条原文都命中（此前二次编译吃掉 OR 为 0）。"""
        self._seed()
        from mariposa.recall import pipeline as pl
        res = pl.raw_deep_search(
            actors["jiaming"],
            {"lexical_terms": ["中秋", "约会"],
             "explicit_constraints": {}}, limit=20)
        texts = " ".join(h.get("excerpt") or "" for h in res["hits"])
        assert "中秋" in texts and "约会" in texts, \
            f"OR 两词都应命中：{texts}"

    def test_speaker_hard_filter(self, actors):
        """speaker=qiaosheng：周家明发言不返回（硬过滤不过 Jev）。"""
        from mariposa.recall import pipeline as pl
        res = pl.raw_deep_search(
            actors["jiaming"],
            {"lexical_terms": ["晚风"],
             "explicit_constraints": {"speaker": "qiaosheng"}},
            limit=20)
        assert res["hits"] == [] or all(
            h.get("speaker") == "qiaosheng" for h in res["hits"])


class Test3CoverageGate:
    def test_unavailable_dense_blocks_no_deliverable(self, actors):
        from mariposa import config as cfg
        cfg.SEMANTIC_PROVIDER = ""
        p = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "找雾隐", "channels": ["event"],
            "lexical_terms": ["qqqxyz"],
            "semantic_query": "雾隐茶室"}})
        with pytest.raises(Forbidden) as ei:
            recall_service.round2(actors["jiaming"], {
                "session_id": p["recall_session_id"],
                "reason": "NO_DELIVERABLE_CANDIDATE"})
        gate = ei.value.detail["gate"]
        assert gate.get("retrieval_complete") is False
        assert "dense_event" in gate.get("incomplete_families", [])


class Test4ScopeBinding:
    def test_scoped_session_requires_explicit_scope(self, actors):
        p = recall_service.start(actors["jiaming"], {
            "query_plan": {"original_request": "查",
                           "channels": ["event"],
                           "lexical_terms": ["查"]},
            "conversation_scope": "chat-A"})
        sid = p["recall_session_id"]
        # 同主体、省略 scope → 拒（不再默认继承）
        with pytest.raises(Forbidden) as e1:
            registry.invoke(actors["jiaming"], "memory.recall.status",
                            {"session_id": sid}, None)
        assert e1.value.code == "SCOPE_REQUIRED"
        # 匹配 scope → 通过
        ok = registry.invoke(actors["jiaming"], "memory.recall.status",
                             {"session_id": sid,
                              "conversation_scope": "chat-A"}, None)
        assert ok["data"]["session"]["session_id"] == sid


class Test5FindWords:
    def _words(self, actors):
        memory.hold(
            actors["jiaming"], text="事件正文", memory_date="2026-09-25",
            date_confidence="exact", original_title="fw",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False,
            our_words=[
                {"speaker": "qiaosheng", "text": "乔生说的晚风",
                 "expression_kind": "verbatim"},
                {"speaker": "jiaming", "text": "家明说的晚风",
                 "expression_kind": "verbatim"}])

    def test_negative_speaker_excluded_sparse(self, actors):
        self._words(actors)
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "晚风",
            "explicit_negative_constraints": {
                "speaker_excluded": ["qiaosheng"]}})
        sps = [c.get("speaker") for c in p["candidates"]]
        assert sps and all(s == "jiaming" for s in sps), \
            f"负向 speaker 必须过滤稀疏结果：{sps}"

    def test_specialized_replay_keeps_core_words(self, actors):
        """CORE 桶的话语专项首查 1 条——同 operation_id 重放不丢。"""
        out = memory.hold(
            actors["jiaming"], text="久远事件正文", memory_date="2026-01-01",
            date_confidence="exact", original_title="core",
            categories=["daily"], creation_mode="retrospective",
            raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "久远年代的鹡鸰",
                        "expression_kind": "verbatim"}])
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET held_at='2026-01-01T00:00:00+00:00'"
                " WHERE memory_id=?", (out["memory_id"],))
        a = {"query": "鹡鸰", "operation_id": "op-fw-core"}
        r1 = registry.invoke(actors["jiaming"], "memory.words.recall",
                             a, None)
        first = r1["data"]["data"]
        assert first["candidates"], "前置：专项跨阶段命中"
        r2 = registry.invoke(actors["jiaming"], "memory.words.recall",
                             a, None)
        assert r2["data"].get("idempotent_replay") is True
        replayed = r2["data"]["data"]
        assert len(replayed["candidates"]) == \
            len(first["candidates"]), \
            "专项 words 重放不得按 event phase 剔卡"

    def test_dense_word_has_evidence(self, actors, monkeypatch):
        from mariposa import config as cfg
        from mariposa.retrieval import semantic, words_semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")

        def fake(texts):
            return [np.asarray(
                [1.0, 0.0] if "鹧鸪" in t.replace(" ", "") else
                [0.0, 1.0], dtype=np.float32) for t in texts]
        monkeypatch.setattr(semantic, "embed", fake)
        monkeypatch.setattr(words_semantic, "embed", fake)
        self._words(actors)
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "qqqxyz", "semantic_query": "鹧鸪的叫声",
            "explicit_constraints": {"speaker": "qiaosheng"}})
        for c in p["candidates"]:
            assert c.get("evidence"), \
                "纯 dense word 必须携带正式证据身份"
            assert c.get("content_version"), "补当前 memory version"


class Test6QwenWordsDense:
    def test_words_semantic_active_under_qwen(self, actors, monkeypatch):
        from mariposa import config as cfg
        from mariposa.retrieval import qwen_embed, words_semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "qwen3e4b")

        def fake(texts):
            return [np.asarray([1.0, 0.0] + [0.0] * 2558,
                               dtype=np.float32) for _ in texts]
        monkeypatch.setattr(qwen_embed, "embed", fake)
        monkeypatch.setattr(words_semantic, "embed", fake)
        assert words_semantic._provider_active()
        assert "Qwen" in words_semantic._active_model_key()


class Test7Bm25GroupSemantics:
    def test_term_group_requires_continuity(self):
        docs = [{"owner": "a", "field": "event_text",
                 "tokens": "很 小 的 路".split()}]
        out = scoped_bm25.score_documents(
            docs, [["小", "路", "灯"]], [])
        assert out == [], "'小路灯'整组连续——只有'小'不命中"

    def test_phrase_and_terms(self):
        docs = [{"owner": "a", "field": "event_text",
                 "tokens": "中 秋".split()}]
        out = scoped_bm25.score_documents(
            docs, [["约", "会"]], [["中", "秋"]])
        assert out == [], "phrase 命中但 term 组未命中——AND 不满足"
        out2 = scoped_bm25.score_documents(
            docs, [["中", "秋"]], [])
        assert out2, "纯 term 组连续命中"
