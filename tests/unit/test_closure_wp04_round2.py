"""WP04：Round2 完整门禁与事务化（S13/S14）。

全部走真实链路（start 完成 Round1 → round2），不手工给 gate 传
complete。fake judge（conftest test_deterministic）+ 测试专用
source_excerpt 许可。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
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


def hold(principal, text):
    return memory.hold(principal, text=text, memory_date="2026-09-25",
                       date_confidence="exact", original_title="wp04",
                       categories=["daily"],
                       creation_mode="contemporaneous", raw_pending=False)


def _seed_source(text="原文里的崧蓝染色记忆"):
    from mariposa.source import importer
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    f = tmp / "s.json"
    f.write_text(_json.dumps([{
        "uuid": "c-wp04",
        "chat_messages": [{
            "uuid": "wp04-m1", "sender": "human",
            "created_at": "2026-09-20T10:00:00.000Z",
            "content": [{"type": "text", "text": text}]}]}],
        ensure_ascii=False), encoding="utf-8")
    return importer.import_file("jiaming", str(f))


def _start(actors, terms=("崧蓝",), op="op-w4-s"):
    return registry.invoke(actors["jiaming"], "memory.recall.start",
                           {"query_plan": {
                               "original_request": "找崧蓝",
                               "channels": ["event"],
                               "lexical_terms": list(terms)},
                            "operation_id": op}, None)


def _round2(actors, sid, reason="EVIDENCE_INSUFFICIENT", op="op-w4-r2"):
    return registry.invoke(actors["jiaming"], "memory.recall.round2",
                           {"session_id": sid, "reason": reason,
                            "operation_id": op}, None)


def _grant_source_excerpt(monkeypatch):
    """fake judge 不走 typesafe profile——门禁的外发授权检查需要
    typesafe 实例的 data profile 含 source_excerpt；直接给 store
    层注入一个带许可的实例不可行（provider 是 DisabledJudge），
    改为注入带许可的 TypeSafe 实例到 _INJECTED。"""
    from mariposa.retrieval.judges import base as jb
    from mariposa.retrieval.judges import typesafe_jev

    class GrantedTypeSafe(typesafe_jev.TypeSafeJevJudge):
        name = "granted_typesafe"
        _api_key = "test-key"

        def __init__(self):
            super().__init__()
            self._data_profile = frozenset(
                {"event_excerpt", "title_cue", "word_excerpt",
                 "source_excerpt"})
            self._disabled_reason = None

        def judge(self, plan, candidates, ctx):
            from mariposa.retrieval.judges import base as _jb
            items = [_jb.JudgeItem(
                candidate_ref=c.get("candidate_ref")
                or c["resource_ref"],
                candidate_version=str(c.get("content_version") or ""),
                relevance_signal=0.8,
                evaluation_status="evaluated", model_id=self.name,
                prompt_version="t") for c in candidates]
            return _jb.JudgeBatchResult(
                items=items, provider_status="evaluated",
                degraded_reason=None, cache_hits=0,
                cache_misses=len(candidates), request_count=1)

    jb.register_for_tests("granted_typesafe", GrantedTypeSafe())
    from mariposa import config as cfg
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = "granted_typesafe"
    return old


class TestRound2FullChain:
    def test_round1_then_round2_delivers_judged_raw(self, actors,
                                                    monkeypatch):
        """真实链路：完整 Round1（judge 正常）→ round2 过门禁，
        raw 候选经同一层 Jev 交付（≤3），事务留痕 kind='raw'。"""
        old = _grant_source_excerpt(monkeypatch)
        try:
            hold(actors["jiaming"], "正文里的崧蓝染色记忆")
            r1 = _start(actors)
            sid = r1["data"]["data"]["recall_session_id"]
            _seed_source()
            r2 = _round2(actors, sid)
            packet = r2["data"]["data"]
            assert packet["round"] == 2
            assert packet["candidates"], "raw 候选应经 judge 交付"
            assert all(c["channel"] == "raw" for c in packet["candidates"])
            assert packet["coverage"]["judge"] == "evaluated"
            with db.recall_runtime() as conn:
                kinds = [r["kind"] for r in conn.execute(
                    "SELECT kind FROM recall_rounds WHERE session_id=?",
                    (sid,))]
                receipt = store.read_round1_receipt(conn, sid, 1)
            assert "raw" in kinds and "memory" in kinds
            assert receipt and receipt["judged_count"] > 0
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_judge_fault_blocks_round2(self, actors, monkeypatch):
        """Round1 Jev 故障（unavailable）→ 无升级（S13-3）。"""
        from mariposa.retrieval.judges import base as jb

        class FaultyJudge(jb.JudgeProvider):
            name = "faulty_w4"
            def judge(self, plan, cs, ctx):
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        c.get("candidate_ref") or c["resource_ref"],
                        c.get("content_version"),
                        evaluation_status="unavailable") for c in cs],
                    provider_status="unavailable",
                    degraded_reason="provider_down")

        jb.register_for_tests("faulty_w4", FaultyJudge())
        from mariposa import config as cfg
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "faulty_w4"
        try:
            hold(actors["jiaming"], "故障场景正文")
            r1 = _start(actors, terms=("故障",))
            sid = r1["data"]["data"]["recall_session_id"]
            with pytest.raises(Forbidden) as ei:
                _round2(actors, sid)
            assert ei.value.code == "ROUND2_GATE_DENIED"
            assert ei.value.detail["gate"].get("judge_no_fault") \
                is False or ei.value.detail["gate"].get(
                    "judge_outbound_authorized") is False
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_reason_closed_set_and_fact_support(self, actors,
                                                monkeypatch):
        old = _grant_source_excerpt(monkeypatch)
        try:
            hold(actors["jiaming"], "事实支持场景崧蓝正文")
            r1 = _start(actors)
            sid = r1["data"]["data"]["recall_session_id"]
            # 闭集外
            with pytest.raises(Forbidden) as e1:
                _round2(actors, sid, reason="随便理由")
            assert e1.value.detail["gate"]["reason_in_closed_set"] \
                is False
            # 闭集内但无事实支持（Round1 交付正常，宣称无候选）
            with pytest.raises(Forbidden) as e2:
                _round2(actors, sid, reason="NO_DELIVERABLE_CANDIDATE",
                        op="op-w4-x2")
            g = e2.value.detail["gate"]
            assert g["round1_receipt"] and \
                g["reason_fact_supported"] is False
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_no_source_excerpt_grant_blocks_raw_start(self, actors):
        """S15：无 source_excerpt 外发许可 → raw 不开始（授权门在
        判据前，不产生任何 round）。"""
        r1 = _start(actors)
        sid = r1["data"]["data"]["recall_session_id"]
        with pytest.raises(Forbidden) as ei:
            _round2(actors, sid)
        gate = ei.value.detail["gate"]
        assert gate["judge_outbound_authorized"] is False

    def test_same_operation_replays(self, actors, monkeypatch):
        old = _grant_source_excerpt(monkeypatch)
        try:
            hold(actors["jiaming"], "重放场景崧蓝正文")
            r1 = _start(actors)
            sid = r1["data"]["data"]["recall_session_id"]
            _seed_source()
            r2 = _round2(actors, sid, op="op-w4-replay")
            r2b = _round2(actors, sid, op="op-w4-replay")
            assert r2b["data"].get("idempotent_replay") is True
            with db.recall_runtime() as conn:
                raw_rounds = conn.execute(
                    "SELECT COUNT(*) c FROM recall_rounds WHERE"
                    " session_id=? AND kind='raw'", (sid,)).fetchone()["c"]
            assert raw_rounds == 1, "同 key 重放不得二次 raw 轮"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_prior_raw_round_blocks_rerun_with_new_op(self, actors,
                                                      monkeypatch):
        """S13-5：同 burst 已有 raw 轮——换 operation_id 也不无界重跑。"""
        old = _grant_source_excerpt(monkeypatch)
        try:
            hold(actors["jiaming"], "防重跑场景崧蓝正文")
            r1 = _start(actors)
            sid = r1["data"]["data"]["recall_session_id"]
            _seed_source()
            _round2(actors, sid, op="op-w4-first")
            with pytest.raises(Forbidden) as ei:
                _round2(actors, sid, op="op-w4-second")
            assert ei.value.detail["gate"]["no_prior_raw_round"] is False
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_missing_round1_receipt_blocks(self, actors):
        """无 Round1 完成回执（从未 start 过该 revision）→ 拒绝。"""
        draft = store.new_session_draft("jiaming", "", {})
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                store.insert_session(conn, draft)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        with pytest.raises(Forbidden) as ei:
            _round2(actors, draft["session_id"])
        assert ei.value.detail["gate"]["round1_receipt"] is False
