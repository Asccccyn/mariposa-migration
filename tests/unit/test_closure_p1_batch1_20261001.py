"""全量审计（2026-10-01）第一批 P1-01..P1-05 验收测试。

对应林石见报告 §七验收底线（ROUND2/JUDGE/WORDS/IDEM 系列；
PLAN/MEDIA 属后续批次）。
"""
from __future__ import annotations

import pytest

from mariposa import config as cfg, db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service, store
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


def _grant_source_excerpt():
    class GrantedTypeSafe(typesafe_jev.TypeSafeJevJudge):
        name = "granted_ts_p1"
        _api_key = "test-key"

        def __init__(self):
            super().__init__()
            self._data_profile = frozenset(
                {"event_excerpt", "title_cue", "word_excerpt",
                 "source_excerpt"})
            self._disabled_reason = None

        def judge(self, plan, candidates, ctx):
            items = [jb.JudgeItem(
                candidate_ref=c.get("candidate_ref")
                or c["resource_ref"],
                candidate_version=str(c.get("content_version") or ""),
                relevance_signal=0.8,
                evaluation_status="evaluated", model_id=self.name,
                prompt_version="t") for c in candidates]
            return jb.JudgeBatchResult(
                items=items, provider_status="evaluated")

    jb.register_for_tests("granted_ts_p1", GrantedTypeSafe())
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = "granted_ts_p1"
    return old


class TestRound2CompletionProof:
    """ROUND2-01/02/03 + refine 不可沿用旧 complete。"""

    def test_words_disabled_round1_cannot_back_round2(self, actors,
                                                      monkeypatch):
        """ROUND2-01/02：words disabled → words_lexical=blocked、
        search_status=UNAVAILABLE——统计回执存在但 completed=0，
        blocked 也不在 complete 值集，Round2 必拒。"""
        monkeypatch.setattr(cfg, "RECALL_WORDS_ENABLED", False)
        old = _grant_source_excerpt()
        try:
            p = recall_service.start(actors["jiaming"], {"query_plan": {
                "original_request": "晚风", "channels": ["words"],
                "lexical_terms": ["晚风"]}})
            sid = p["recall_session_id"]
            with db.recall_runtime() as conn:
                receipt = store.read_round1_receipt(conn, sid, 1)
            assert receipt is not None, "前置：统计回执仍写（审计链）"
            assert receipt["completed"] == 0, \
                "UNAVAILABLE 轮不得标 completed"
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid,
                    "reason": "NO_DELIVERABLE_CANDIDATE"})
            gate = ei.value.detail["gate"]
            assert ei.value.code == "ROUND2_GATE_DENIED"
            assert gate["round1_completed"] is False, \
                "统计回执存在 ≠ 完整完成"
            assert "words_lexical" in gate["incomplete_families"], \
                "blocked 不能算 complete"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_browse_topk_not_complete(self, actors):
        """ROUND2-03：browse 只看 latest-K 窗口——scope 25 桶时
        coverage=partial_topk_window，不得冒充 complete 背书 Round2。"""
        old = _grant_source_excerpt()
        try:
            for i in range(25):
                memory.hold(
                    actors["jiaming"], text=f"日常流水第{i}号",
                    memory_date="2026-09-%02d" % (1 + i % 28),
                    date_confidence="exact", original_title=f"d{i}",
                    categories=["daily"],
                    creation_mode="contemporaneous", raw_pending=False)
            p = recall_service.start(actors["jiaming"], {"query_plan": {
                "original_request": "翻翻最近",
                "channels": ["event"],
                "explicit_constraints": {"event_date": {
                    "from": "2026-01-01", "to": "2026-12-31"}}}})
            assert p["coverage"]["event"] == "partial_topk_window", \
                "browse latest-K 不得签 complete"
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": p["recall_session_id"],
                    "reason": "EVIDENCE_INSUFFICIENT"})
            gate = ei.value.detail["gate"]
            assert gate.get("retrieval_complete") is False
            assert "event" in gate["incomplete_families"]
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_refine_new_revision_cannot_reuse_old_complete(self, actors):
        """refine 前进 revision 后，旧 revision 的 completed 不可沿用
        （completed 按 session_id+revision 键定）。"""
        old = _grant_source_excerpt()
        try:
            memory.hold(
                actors["jiaming"], text="晚风事件正文", memory_date="2026-09-25",
                date_confidence="exact", original_title="rv",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False,
                our_words=[{"speaker": "qiaosheng", "text": "复述：晚风的傍晚",
                            "expression_kind": "paraphrase"}])
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "晚风",
                               "channels": ["words"],
                               "lexical_terms": ["晚风"]}})
            sid = p["recall_session_id"]
            with db.recall_runtime() as conn:
                assert store.read_round1_receipt(
                    conn, sid, 1)["completed"] == 1
            recall_service.refine(actors["jiaming"], {
                "session_id": sid, "query_plan": {
                    "original_request": "晚风再查",
                    "channels": ["words"],
                    "lexical_terms": ["晚风"]}})
            with db.recall_runtime() as conn:
                assert store.read_round1_receipt(
                    conn, sid, 2)["completed"] == 1, \
                    "前置：新 revision 自己完成"
            # 旧 revision 的回执仍在但 gate 读的是当前 revision
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid,
                    "reason": "EVIDENCE_INSUFFICIENT"})
            assert ei.value.code == "ROUND2_GATE_DENIED"
            gate = ei.value.detail["gate"]
            assert "round1_completed" not in gate or \
                gate.get("round1_completed"), \
                "新 revision 自身完成——若拒须是其他条件，不得是完成证明缺失"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestRound2BurstPreflight:
    """ROUND2-04：当前 burst 满 → 有成本操作前拒。"""

    def test_burst_full_zero_jev_calls(self, actors):
        cap = _CountingJudge()
        jb.register_for_tests(cap.name, cap)
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            memory.hold(
                actors["jiaming"], text="崧蓝事件", memory_date="2026-09-25",
                date_confidence="exact", original_title="b4",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
            plan = {"original_request": "崧蓝", "channels": ["event"],
                    "lexical_terms": ["崧蓝"]}
            p0 = recall_service.start(actors["jiaming"],
                                      {"query_plan": plan})
            sid = p0["recall_session_id"]
            # 同 burst 补满 3 轮（start 已 1 轮）
            recall_service.refine(actors["jiaming"], {
                "session_id": sid, "query_plan": plan})
            recall_service.refine(actors["jiaming"], {
                "session_id": sid, "query_plan": plan})
            with db.recall_runtime() as conn:
                used = conn.execute(
                    "SELECT COUNT(*) c FROM recall_rounds WHERE"
                    " session_id=? AND burst_no=1", (sid,)).fetchone()["c"]
                total = store.count_rounds(conn, sid)
            assert used >= cfg.RECALL_BURST_ROUNDS and \
                total < cfg.RECALL_SESSION_BURSTS_MAX * \
                cfg.RECALL_BURST_ROUNDS, "前置：burst 满、总额未满"
            n0 = cap.calls
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid,
                    "reason": "NO_DELIVERABLE_CANDIDATE"})
            assert ei.value.code in ("ROUND2_GATE_DENIED",
                                     "BUDGET_EXHAUSTED")
            assert ei.value.detail["gate"]["budget_available"] is False, \
                "当前 burst 额度必须在预检确认"
            assert cap.calls == n0, "Jev 零调用（成本先于预算确认=违规）"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class _CountingJudge(jb.JudgeProvider):
    name = "counting_p1"

    def __init__(self):
        self.calls = 0

    def judge(self, plan, candidates, ctx):
        self.calls += 1
        items = [jb.JudgeItem(
            c.get("candidate_ref") or c["resource_ref"],
            c.get("content_version"), relevance_signal=0.8,
            evaluation_status="evaluated", model_id=self.name,
            prompt_version="t") for c in candidates]
        return jb.JudgeBatchResult(items=items,
                                   provider_status="evaluated")


class TestJudgedCount:
    """JUDGE-01：mixed 全集 unjudged 计算。"""

    def test_event30_words40_unjudged_20(self, actors, monkeypatch):
        monkeypatch.setattr(cfg, "RECALL_LEXICAL_K", 30)
        for i in range(30):
            memory.hold(
                actors["jiaming"], text=f"崧蓝事件第{i}则",
                memory_date="2026-09-25", date_confidence="exact",
                original_title=f"e{i}", categories=["daily"],
                creation_mode="contemporaneous", raw_pending=False,
                our_words=[{"speaker": "qiaosheng",
                            "text": f"崧蓝话语第{i}句",
                            "expression_kind": "verbatim"}])
        p = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "崧蓝", "channels": ["event", "words"],
            "lexical_terms": ["崧蓝"]}})
        sid = p["recall_session_id"]
        with db.recall_runtime() as conn:
            receipt = store.read_round1_receipt(conn, sid, 1)
        assert receipt["judged_count"] == cfg.RECALL_JUDGE_CANDIDATE_CAP, \
            f"judge 输入应封顶 {cfg.RECALL_JUDGE_CANDIDATE_CAP}"
        assert receipt["unjudged_count"] == 20, \
            "event30+words30 送判40：被截掉的 20 必须如实计入 unjudged"
        with pytest.raises(Forbidden) as ei:
            recall_service.round2(actors["jiaming"], {
                "session_id": sid,
                "reason": "NO_DELIVERABLE_CANDIDATE"})
        gate = ei.value.detail["gate"]
        assert gate.get("unjudged_zero") is False, \
            "未判尽不得给 Round2 背书"


class TestWordsDenseStatus:
    """WORDS-01/02/03：零命中/pending/未配置三态。"""

    def _seed(self, actors):
        memory.hold(
            actors["jiaming"], text="事件正文", memory_date="2026-09-25",
            date_confidence="exact", original_title="ws",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "鹧鸪的叫声记录",
                        "expression_kind": "verbatim"}])

    def _fake(self, monkeypatch):
        import numpy as np
        from mariposa.retrieval import words_semantic

        def fake(texts):
            return [np.asarray(
                [1.0, 0.0] if "鹧鸪" in t.replace(" ", "") else
                [0.0, 1.0], dtype=np.float32) for t in texts]
        monkeypatch.setattr(words_semantic, "embed", fake)
        return words_semantic

    def test_zero_hit_is_complete(self, actors, monkeypatch):
        """WORDS-01：provider 正常、语义零命中 → complete（非
        unavailable）。"""
        wsem = self._fake(monkeypatch)
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        self._seed(actors)
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "qqqxyz", "semantic_query": "完全无关的查询词"})
        assert p["coverage"]["words_dense"] == "complete_within_scope", \
            "正常搜完零命中不得误报 unavailable"

    def test_only_pending_is_partial(self, actors, monkeypatch):
        """WORDS-02：只有 pending 向量 → partial_vectors_pending。"""
        wsem = self._fake(monkeypatch)
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "local_bge_zh")
        monkeypatch.setattr(wsem, "WORDS_REINDEX_BUDGET", 0)
        self._seed(actors)
        with db.formal() as conn:
            conn.execute("DELETE FROM word_embeddings")
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "鹧鸪", "semantic_query": "鹧鸪的叫声"})
        assert p["coverage"]["words_dense"] == "partial_vectors_pending", \
            p["coverage"].get("words_dense")

    def test_provider_off_is_unavailable(self, actors, monkeypatch):
        """WORDS-03：provider 未配置 → unavailable。"""
        self._fake(monkeypatch)
        monkeypatch.setattr(cfg, "SEMANTIC_PROVIDER", "")
        self._seed(actors)
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "鹧鸪", "semantic_query": "鹧鸪的叫声"})
        assert p["coverage"]["words_dense"] == "unavailable"


class TestIdempotencyBoundary:
    """IDEM-01/02/03。"""

    def test_start_without_operation_id_rejected(self, actors):
        """IDEM-01：start 无 operation_id → schema 拒。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.recall.start",
                            {"query_plan": {
                                "original_request": "查",
                                "channels": ["event"],
                                "lexical_terms": ["查"]}}, None)
        assert ei.value.code == "SCHEMA_VIOLATION"

    def test_find_words_retry_same_session_no_double_judge(self, actors):
        """IDEM-02：find_words 同 operation_id 重试 → 同 session、
        Jev 不重复调用。"""
        memory.hold(
            actors["jiaming"], text="事件正文", memory_date="2026-09-25",
            date_confidence="exact", original_title="idem",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "梧桐叶落了",
                        "expression_kind": "verbatim"}])
        cap = _CountingJudge()
        jb.register_for_tests(cap.name, cap)
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            a = {"query": "梧桐", "operation_id": "op-idem-fw"}
            r1 = registry.invoke(actors["jiaming"], "memory.find_words",
                                 a, None)
            r2 = registry.invoke(actors["jiaming"], "memory.find_words",
                                 a, None)
            assert r2["data"].get("idempotent_replay") is True
            sid1 = r1["data"]["recall_session_id"]
            sid2 = r2["data"]["recall_session_id"]
            assert sid1 == sid2, "重试不得另建 session"
            assert cap.calls == 1, "重试不得重复 Jev"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_tools_list_not_readonly(self, actors):
        """IDEM-03：find_words/words.recall 在 tools/list 里
        readOnlyHint=false（有状态写操作）。"""
        from mariposa.capabilities.mcp_adapter import _tools_for
        tools = _tools_for(actors["jiaming"])
        hits = {t["name"]: t for t in tools
                if "find_words" in t["name"] or "words.recall" in t["name"]
                or "words_recall" in t["name"]}
        assert len(hits) >= 2, f"应暴露两个 words 入口：{sorted(hits)}"
        for name, t in hits.items():
            assert t["annotations"]["readOnlyHint"] is False, name
