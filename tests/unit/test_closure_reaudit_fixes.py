"""闭环复审（林石见 2026-09-30）反例回归——P1-1..P1-5 + P2。

每个测试对应审计报告的一条实测反例。
"""
from __future__ import annotations

import json

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service, store
from mariposa.retrieval.judges import typesafe_jev
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


def _hold_word(principal, word_text, kind="paraphrase"):
    return memory.hold(
        principal, text="事件正文", memory_date="2026-09-25",
        date_confidence="exact", original_title="audit",
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False,
        our_words=[{"speaker": "qiaosheng", "text": word_text,
                    "expression_kind": kind}])


def _start_words(actors, principal="jiaming", op="op-a1"):
    from mariposa import config as cfg
    assert cfg.RECALL_WORDS_ENABLED, "conftest 应启用 words 通道"
    return registry.invoke(actors[principal], "memory.recall.start",
                           {"query_plan": {
                               "original_request": "我当时的原话",
                               "channels": ["words"],
                               "lexical_terms": ["晚风"],
                               "evidence_requirement":
                                   "verbatim_required"},
                            "operation_id": op}, None)


class TestP11SessionOwnerIsolation:
    def test_cross_principal_session_access_denied(self, actors):
        """审计反例：周家明建 chat-A session，乔生持 id 不带 scope
        调 status——原先可读，现必须拒。"""
        r = _start_words(actors)
        sid = r["data"]["data"]["recall_session_id"]
        # 不带 scope、跨主体
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["qiaosheng"],
                            "memory.recall.status",
                            {"session_id": sid}, None)
        assert ei.value.code == "SESSION_OWNER_MISMATCH"
        # 同主体（归属者）不受影响
        ok = registry.invoke(actors["jiaming"], "memory.recall.status",
                             {"session_id": sid}, None)
        assert ok["data"]["session"]["session_id"] == sid

    def test_all_actions_guarded(self, actors):
        r = _start_words(actors)
        sid = r["data"]["data"]["recall_session_id"]
        for cap, args in (
                ("memory.recall.refine",
                 {"session_id": sid, "operation_id": "op-p11-refine",
                  "query_plan": {
                      "original_request": "再查", "channels": ["words"],
                      "lexical_terms": ["晚风"]}}),
                ("memory.recall.close",
                 {"session_id": sid, "outcome": "cancelled",
                  "operation_id": "op-p11-close"}),
                ("memory.recall.round2",
                 {"session_id": sid,
                  "reason": "EVIDENCE_INSUFFICIENT",
                  "operation_id": "op-p11-round2"})):
            with pytest.raises(Forbidden) as ei:
                registry.invoke(actors["qiaosheng"], cap, args, None)
            assert ei.value.code == "SESSION_OWNER_MISMATCH", cap




class TestP13Round2ReasonFacts:
    def _grant(self, monkeypatch):
        from mariposa.retrieval.judges import base as jb
        from mariposa import config as cfg

        class G(typesafe_jev.TypeSafeJevJudge):
            name = "g_p13"
            _api_key = "k"

            def __init__(self):
                super().__init__()
                self._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "source_excerpt", "structured_metadata"})
                self._disabled_reason = None

            def judge(self, plan, cs, ctx):
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        c.get("candidate_ref") or c["resource_ref"],
                        c.get("content_version"), relevance_signal=0.8,
                        evaluation_status="evaluated",
                        model_id=self.name, prompt_version="t")
                        for c in cs], provider_status="evaluated")

        jb.register_for_tests("g_p13", G())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "g_p13"
        return old

    def test_normal_hit_cannot_upgrade(self, actors, monkeypatch):
        """审计反例：正常'中秋约会'候选（rank-only 下
        needs_validation）声明 EVIDENCE_INSUFFICIENT——原先放行，
        现必须拒。"""
        old = self._grant(monkeypatch)
        try:
            memory.hold(actors["jiaming"],
                        text="我们一起出去约会了",
                        memory_date="2026-09-29",
                        date_confidence="exact", original_title="中秋",
                        categories=["date"],
                        creation_mode="contemporaneous",
                        raw_pending=False)
            r = registry.invoke(actors["jiaming"],
                                "memory.recall.start",
                                {"query_plan": {
                                    "original_request": "中秋约会",
                                    "channels": ["event"],
                                    "lexical_terms": ["中秋", "约会"]},
                                 "operation_id": "op-p13a"}, None)
            sid = r["data"]["data"]["recall_session_id"]
            for reason in ("EVIDENCE_INSUFFICIENT",
                           "SOURCE_DISAMBIGUATION_NEEDED"):
                with pytest.raises(Forbidden) as ei:
                    registry.invoke(actors["jiaming"],
                                    "memory.recall.round2",
                                    { "operation_id": "op-auto-test_c-0","session_id": sid, "reason": reason},
                                    None)
                gate = ei.value.detail["gate"]
                assert gate["reason_fact_supported"] is False, reason
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_real_insufficient_upgrades(self, actors, monkeypatch):
        """真证据不足（verbatim 要求 + 仅 paraphrase）→ 升级合法。"""
        old = self._grant(monkeypatch)
        try:
            _hold_word(actors["jiaming"], "复述：晚风吹过窗边")
            r = _start_words(actors)
            sid = r["data"]["data"]["recall_session_id"]
            with db.recall_runtime() as conn:
                receipt = store.read_round1_receipt(conn, sid, 1)
            facts = receipt["coverage"]["_first_round_facts"]
            assert facts["requirement_met"] is False
            assert facts["delivered_count"] > 0
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old


class TestP14WordsIdentityAndEnvelope:
    def test_dense_and_lexical_same_resource_one_slot(self, actors,
                                                      monkeypatch):
        """审计反例：同一句'晚风吹过窗边'——稀疏 our_word: 与 dense
        word: 双卡占位。统一身份后应一个槽位。"""
        from mariposa import config as cfg
        from mariposa.retrieval import semantic, words_semantic
        import numpy as np
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "qwen3e4b")

        def fake(texts):
            return [np.asarray(
                [1.0, 0.0] + [0.0] * 2558
                if "晚风" in t.replace(" ", "") else
                [0.0, 1.0] + [0.0] * 2558, dtype=np.float32)
                for t in texts]
        monkeypatch.setattr(words_semantic, "embed", fake)
        # CB-054：provider=qwen3e4b 时文档/查询侧走 qwen_embed（结构
        # 测试同样注入假 2560 维，不 import 真实 ML）
        from mariposa.retrieval import qwen_embed
        monkeypatch.setattr(qwen_embed, "embed", fake)
        monkeypatch.setattr(
            qwen_embed, "embed_query",
            lambda q: np.asarray([1.0, 0.0] + [0.0] * 2558,
                                 dtype=np.float32))
        _hold_word(actors["jiaming"], "晚风吹过窗边", kind="verbatim")
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "晚风", "semantic_query": "晚风吹过窗边"})
        refs = [c["resource_ref"] for c in p["candidates"]]
        prefixes = {r.split(":")[0] for r in refs}
        assert prefixes <= {"our_word"}, \
            f"资源身份统一为一个前缀：{refs}"
        assert len(refs) == len(set(refs)), "同话语不得重复占位"

    def test_wide_words_hit_carries_event_evidence(self, actors):
        """审计反例：普通 WIDE 靠 our_words 命中——envelope 不得把
        event_text 错标 our_words，且 event_evidence 必须在场。"""
        from mariposa.retrieval.judges import base as jb

        cap = {}

        class Cap(jb.JudgeProvider):
            name = "cap_p14"
            def judge(self, plan, candidates, ctx):
                inner = typesafe_jev.TypeSafeJevJudge()
                inner._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "structured_metadata"})
                inner._current_terms = plan.get("lexical_terms") or []
                cap["payload"] = inner._payload(plan, candidates)
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        c.get("candidate_ref") or c["resource_ref"],
                        c.get("content_version"), relevance_signal=0.8,
                        evaluation_status="evaluated", model_id="c",
                        prompt_version="t") for c in candidates],
                    provider_status="evaluated")

        jb.register_for_tests("cap_p14", Cap())
        from mariposa import config as cfg
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "cap_p14"
        try:
            m = memory.hold(
                actors["jiaming"], text="窗外的雨下了整夜",
                memory_date="2026-09-26", date_confidence="exact",
                original_title="雨夜", categories=["daily"],
                creation_mode="contemporaneous", raw_pending=False,
                our_words=[{"speaker": "qiaosheng",
                            "text": "窗外的梧桐叶落了",
                            "expression_kind": "verbatim"}])
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "梧桐",
                               "channels": ["event"],
                               "lexical_terms": ["梧桐"]}})
            word_cards = [c for c in p["candidates"]
                          if c.get("channel") == "event"
                          and "our_words" in (c.get("matched_fields")
                                              or [])]
            # 审计 2026-10-03：前置由本用例自己构造（同数据同索引），
            # 命中消失是回归不是可选依赖——skip 会把真红变成静默跳过
            assert word_cards, \
                "前置失败：event 通道应命中 our_words 字段卡（WIDE 阶段）"
            cands = {c["candidate_ref"]: c
                     for c in cap["payload"]["state"]["candidates"]}
            word_ref = word_cards[0]["candidate_ref"]
            segs = cands[word_ref]["segments"]
            word_segs = [s for s in segs if s["field"] == "our_words"]
            ev_segs = [s for s in segs if s["field"] == "event_text"]
            assert word_segs and "match_evidence" in word_segs[0]["roles"]
            assert ev_segs, "P1-4：words 命中必须附事件事实主体"
            assert "event_evidence" in ev_segs[0]["roles"]
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestP15JevProfileFailClosed:
    @pytest.fixture(autouse=True)
    def _ensure_runtime_root(self):
        # judge 结果会写 runtime 缓存库——单独运行本类时临时根与
        # runtime 迁移尚未由其他用例完成；reset_all 幂等建齐
        reset_all()

    def test_structured_metadata_blocked(self, monkeypatch):
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "event_excerpt")
        j = typesafe_jev.TypeSafeJevJudge()
        j._api_key = "k"
        cand = {"candidate_ref": "m1", "resource_ref": "memory:m1",
                "channel": "event", "matched_fields": ["event_text"],
                "excerpt": "正文", "memory_id": "mem_x",
                "memory_date": "2026-09-01",
                "_row": {"whitelist_body": "正文"}}
        proj = j._candidate_projection(cand, ['测试锚词'])
        assert proj["metadata"]["memory_id"] is None
        assert proj["metadata"]["speaker"] is None

    def test_empty_segments_not_judged(self, monkeypatch):
        """必要证据段被 profile 剥空 → 候选不送 payload、
        unavailable——无证据即无有效判断。"""
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "structured_metadata")  # 无任何文本许可
        j = typesafe_jev.TypeSafeJevJudge()
        j._api_key = "k"
        out = j.judge({"original_request": "q",
                       "explicit_constraints": {}},
                      [{"candidate_ref": "m1",
                        "resource_ref": "memory:m1", "channel": "event",
                        "matched_fields": ["event_text"],
                        "excerpt": "正文",
                        "_row": {"whitelist_body": "正文"}}],
                      {})
        item = out.items[0]
        assert item.evaluation_status == "unavailable"
        assert out.request_count == 0, "空段候选不产生请求"

    def test_title_only_segments_not_judged(self, monkeypatch):
        """F05（2026-10-03 审计 P1）：许可只够发标题（title_cue），
        event 主体被剥——段非空但缺 event_evidence，照样不送 Jev、
        unavailable。标题不能替代事件主体（r2 S09）。"""
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "title_cue")
        j = typesafe_jev.TypeSafeJevJudge()
        j._api_key = "k"
        out = j.judge({"original_request": "q",
                       "explicit_constraints": {}},
                      [{"candidate_ref": "m1",
                        "resource_ref": "memory:m1", "channel": "event",
                        "matched_fields": ["original_title"],
                        "excerpt": "标题命中片段",
                        "_row": {"whitelist_body": "当晚在山里修电路",
                                 "original_title": "中秋约会"}}],
                      {})
        item = out.items[0]
        assert item.evaluation_status == "unavailable"
        assert out.request_count == 0, "缺事件主体的候选不产生请求"

    def test_word_match_cannot_substitute_event_body(self, monkeypatch):
        """F05 混合面：普通 recall 中 our_words 命中，许可只给
        word_excerpt——话语 match 段在场但 event 主体被剥 →
        unavailable（话语命中不能替代事件事实主体）。"""
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "word_excerpt")
        j = typesafe_jev.TypeSafeJevJudge()
        j._api_key = "k"
        out = j.judge({"original_request": "q",
                       "explicit_constraints": {}},
                      [{"candidate_ref": "m2",
                        "resource_ref": "memory:m2", "channel": "event",
                        "matched_fields": ["our_words"],
                        "excerpt": "具体命中话语",
                        "_row": {"whitelist_body": "事件正文主体",
                                 "original_title": "t"}}],
                      {})
        item = out.items[0]
        assert item.evaluation_status == "unavailable"
        assert out.request_count == 0


class TestP26ReplayKeepsSourceMsg:
    def test_round2_replay_identical(self, actors, monkeypatch):
        """审计反例：round2 首次 1 条 source_msg，同 operation_id
        重试变 0——重放必须逐字一致。"""
        from mariposa.retrieval.judges import base as jb
        from mariposa import config as cfg

        class G(typesafe_jev.TypeSafeJevJudge):
            name = "g_p26"
            _api_key = "k"

            def __init__(self):
                super().__init__()
                self._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "source_excerpt", "structured_metadata"})
                self._disabled_reason = None

            def judge(self, plan, cs, ctx):
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        c.get("candidate_ref") or c["resource_ref"],
                        c.get("content_version"), relevance_signal=0.8,
                        evaluation_status="evaluated",
                        model_id=self.name, prompt_version="t")
                        for c in cs], provider_status="evaluated")

        jb.register_for_tests("g_p26", G())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "g_p26"
        try:
            _hold_word(actors["jiaming"], "复述：晚风吹过窗边")
            r1 = _start_words(actors)
            sid = r1["data"]["data"]["recall_session_id"]
            from mariposa.source import importer
            import json as _json
            import tempfile
            import pathlib
            tmp = pathlib.Path(tempfile.mkdtemp())
            f = tmp / "s.json"
            f.write_text(_json.dumps([{
                "uuid": "c-p26",
                "chat_messages": [{
                    "uuid": "p26-m1", "sender": "human",
                    "created_at": "2026-09-20T10:00:00.000Z",
                    "content": [{"type": "text",
                                 "text": "原文：晚风吹过窗边"}]}]}],
                ensure_ascii=False), encoding="utf-8")
            importer.import_file("jiaming", str(f))
            a = {"session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                 "operation_id": "op-p26"}
            first = registry.invoke(actors["jiaming"],
                                    "memory.recall.round2", a, None)
            second = registry.invoke(actors["jiaming"],
                                     "memory.recall.round2", a, None)
            f1 = first["data"]["data"]["candidates"]
            f2 = second["data"]["data"]["candidates"]
            assert second["data"].get("idempotent_replay") is True
            assert [c["resource_ref"] for c in f1] == \
                [c["resource_ref"] for c in f2], \
                "P2-6：重放候选必须与首次一致（含 source_msg）"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestF05RegistryLevelRoleGate:
    """裁定（2026-10-04）：Registry/公开响应级集成回归——judge 层正确
    不代表调用链不会绕过。复刻审计关键反例：许可只够发标题，fake
    HTTP 回高置信，最终公开响应仍不得交付带事件正文的卡。"""

    def test_title_cue_only_high_score_not_delivered(self, actors,
                                                     monkeypatch):
        from mariposa import config as cfg
        from mariposa.memory import service as memory
        from mariposa.retrieval.judges import typesafe_jev
        from mariposa.capabilities import registry

        memory.hold(actors["jiaming"], text="当晚在山里修电路",
                    memory_date="2026-09-20", date_confidence="exact",
                    original_title="中秋约会", categories=["daily"],
                    creation_mode="contemporaneous", raw_pending=False)

        class _Resp:
            def __init__(self, body):
                self._body = body.encode("utf-8")
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            return _Resp(json.dumps({
                "model": "fake-jev",
                "answers": {"candidate_0": {"noul": 0.9}},
            }))

        old_provider = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "fake_title_cue_jev"

        class G(typesafe_jev.TypeSafeJevJudge):
            name = "fake_title_cue_jev"
            _api_key = "k"

            def __init__(self):
                super().__init__()
                # 许可只够发标题——event 主体被 profile 剥除
                self._data_profile = frozenset({"title_cue",
                                                "structured_metadata"})
                self._disabled_reason = None

        from mariposa.retrieval.judges import base as jb
        jb.register_for_tests("fake_title_cue_jev", G())
        try:
            monkeypatch.setattr(typesafe_jev.urllib.request, "urlopen",
                                fake_urlopen)
            packet = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {"query_plan": {
                    "original_request": "中秋",
                    "channels": ["event"],
                    "lexical_terms": ["中秋"]},
                 "operation_id": "f05-reg-1"}, None)["data"]["data"]
            for c in packet["candidates"]:
                body_bits = json.dumps(c, ensure_ascii=False)
                assert "山里修电路" not in body_bits, \
                    "缺事件主体的候选不得经公开响应交付正文"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old_provider
