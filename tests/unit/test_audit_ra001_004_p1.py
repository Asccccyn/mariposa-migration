"""复审 P1 修复回归（2026-10-02 post-fix reaudit，RA-001~004）。

- RA-001：plan.update 公开 schema 与 handler 对齐（顶层平铺）——
  changes 嵌套反例：合法请求 ok/version+1 却不改 state/title、Phase
  不动；顶层 state 曾被 SCHEMA_VIOLATION。
- RA-002：Raw 租约 fencing——TTL 过期被接管后，原持有者外发前中止、
  提交事务内不得成为赢家。
- RA-003：Judge 完成证明统一有效口径——NaN 分值不计 judged；Round2
  重复 ref 整批判断作废、不保留首项交付。
- RA-004：幂等键真隔离——transport 层 t: 编码，transport="op:k" 与
  body operation_id="k" 不再碰撞。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import pipeline, service as recall_service, store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="ra",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


# ---------------------------------------------------------------- RA-001

class TestPlanUpdateContract:

    def test_flat_fields_modify_state_title_and_phase(self, actors):
        from mariposa.plans import service as plans
        plan = plans.create("jiaming", "RA001计划", state="active")
        pid = plan["plan_id"]
        out = registry.invoke(actors["jiaming"], "plan.update", {
            "plan_id": pid, "expected_version": 1,
            "state": "done", "title": "改后的标题",
            "operation_id": "ra001-a"}, None)
        assert out["ok"] is True
        got = registry.invoke(actors["jiaming"], "plan.get",
                              {"plan_id": pid}, None)["data"]
        assert got["state"] == "done", "state 必须真实修改（审计反例）"
        assert got["title"] == "改后的标题"
        assert got["version"] == 2
        assert got["completed_at"], "终态锚点必须落地"

    def test_nested_changes_no_longer_accepted_silently(self, actors):
        from mariposa.plans import service as plans
        plan = plans.create("jiaming", "嵌套反例", state="planned")
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"], "plan.update", {
                "plan_id": plan["plan_id"], "expected_version": 1,
                "changes": {"state": "done"},
                "operation_id": "ra001-b"}, None)


# ---------------------------------------------------------------- RA-002

class TestRawLeaseFencing:

    def test_takeover_owner_aborts_before_outbound_and_commit(self, actors,
                                                              monkeypatch):
        """审计反例 lease_live_ttl_takeover：A 在 raw 计算期间被 TTL
        接管——A 不得外发（judge=0）、不得提交（raw 轮 0）。"""
        from mariposa import config as cfg
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev

        class Granted(typesafe_jev.TypeSafeJevJudge):
            name = "ra002_typesafe"
            _api_key = "test-key"

            def __init__(self):
                super().__init__()
                self._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "source_excerpt"})
                self._disabled_reason = None

            def judge(self, plan, candidates, ctx):
                self.calls = getattr(self, "calls", 0) + 1
                items = [jb.JudgeItem(
                    candidate_ref=c.get("candidate_ref")
                    or c["resource_ref"],
                    candidate_version=str(c.get("content_version") or ""),
                    relevance_signal=0.8,
                    evaluation_status="evaluated", model_id=self.name,
                    prompt_version="t") for c in candidates]
                return jb.JudgeBatchResult(
                    items=items, provider_status="evaluated",
                    degraded_reason=None, cache_hits=0,
                    cache_misses=len(candidates), request_count=1)

        provider = Granted()
        jb.register_for_tests("ra002_typesafe", provider)
        old_prov = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "ra002_typesafe"
        try:
            _hold(actors, "租约fencing正文",
                  our_words=[{"speaker": "qiaosheng", "text": "fencing话语",
                              "expression_kind": "paraphrase"}])
            r1 = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {"query_plan": {"original_request": "fencing",
                                "channels": ["words"],
                                "lexical_terms": ["fencing"],
                                "evidence_requirement":
                                    "verbatim_required"},
                 "operation_id": "op-ra002-s"},
                None)["data"]
            sid = r1["recall_session_id"]
            _seed_src()
            s = store.get_session(sid)

            orig_raw = pipeline.raw_deep_search

            def hooked_raw(principal, plan, **kw):
                out = orig_raw(principal, plan, **kw)
                # raw 返回后：TTL 过期，接管者获得新租约
                with db.recall_runtime() as conn:
                    conn.execute(
                        "UPDATE recall_raw_leases SET created_at="
                        "'2020-01-01T00:00:00+00:00' WHERE session_id=?",
                        (sid,))
                recall_service._acquire_raw_lease(
                    sid, s["current_revision"], s["current_burst"])
                return out

            monkeypatch.setattr(pipeline, "raw_deep_search", hooked_raw)
            calls_before = getattr(provider, "calls", 0)
            with pytest.raises(Forbidden) as ei:
                registry.invoke(actors["jiaming"], "memory.recall.round2", {
                    "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                    "operation_id": "op-ra002-r2"}, None)
            assert ei.value.code == "RAW_LEASE_LOST"
            assert getattr(provider, "calls", 0) == calls_before, \
                "失去所有权的执行者不得外发 Jev（R2 期间零新增）"
            with db.recall_runtime() as conn:
                raw_n = conn.execute(
                    "SELECT COUNT(*) c FROM recall_rounds WHERE"
                    " session_id=? AND kind='raw'", (sid,)).fetchone()["c"]
            assert raw_n == 0, "失去所有权的执行者不得提交"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old_prov


def _seed_src():
    from mariposa.source import importer
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    f = tmp / "ra002.json"
    f.write_text(_json.dumps([{
        "uuid": "c-ra002",
        "chat_messages": [{
            "uuid": "ra002-m1", "sender": "human",
            "created_at": "2026-09-20T10:00:00.000Z",
            "content": [{"type": "text", "text": "dup 原文材料"}]}]}],
        ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))


# ---------------------------------------------------------------- RA-003

class TestJudgeValidity:

    def test_nan_signal_not_counted_and_gate_closed(self, actors):
        from mariposa.retrieval.judges import base as jb
        from mariposa import config as cfg
        import math

        class NaNJudge(jb.JudgeProvider):
            name = "ra003_nan"
            _api_key = "k"

            def judge(self, plan, candidates, ctx):
                items = [jb.JudgeItem(
                    candidate_ref=c.get("candidate_ref")
                    or c["resource_ref"],
                    candidate_version=str(c.get("content_version") or ""),
                    relevance_signal=math.nan,
                    evaluation_status="evaluated", model_id=self.name,
                    prompt_version="t") for c in candidates]
                return jb.JudgeBatchResult(
                    items=items, provider_status="evaluated",
                    degraded_reason=None, cache_hits=0,
                    cache_misses=len(candidates), request_count=1)

        jb.register_for_tests("ra003_nan", NaNJudge())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "ra003_nan"
        try:
            _hold(actors, "NaN证明正文",
                  our_words=[{"speaker": "qiaosheng", "text": "nan话语",
                              "expression_kind": "paraphrase"}])
            r1 = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {"query_plan": {"original_request": "nan",
                                "channels": ["words"],
                                "lexical_terms": ["nan"],
                                "evidence_requirement":
                                    "verbatim_required"},
                 "operation_id": "op-ra003-s"},
                None)["data"]
            sid = r1["recall_session_id"]
            with db.recall_runtime() as conn:
                receipt = store.read_round1_receipt(conn, sid, 1)
            assert receipt["judged_count"] == 0, \
                "NaN 分值不得计入 judged（审计反例：judged=1/gate=true）"
            assert receipt["unavailable_count"] > 0
            with pytest.raises(Forbidden):
                registry.invoke(actors["jiaming"], "memory.recall.round2", {
                    "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                    "operation_id": "op-ra003-r2"}, None)
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_round2_duplicate_ref_delivers_nothing(self, actors):
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev
        from mariposa import config as cfg

        class DupRef(typesafe_jev.TypeSafeJevJudge):
            name = "ra003_dup"
            _api_key = "k"

            def __init__(self):
                super().__init__()
                self._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "source_excerpt"})
                self._disabled_reason = None

            def judge(self, plan, candidates, ctx):
                raw_only = [c for c in candidates
                            if c.get("channel") == "raw"]
                if not raw_only:
                    items = [jb.JudgeItem(
                        candidate_ref=c.get("candidate_ref")
                        or c["resource_ref"],
                        candidate_version=str(
                            c.get("content_version") or ""),
                        relevance_signal=0.8,
                        evaluation_status="evaluated",
                        model_id=self.name, prompt_version="t")
                        for c in candidates]
                    return jb.JudgeBatchResult(
                        items=items, provider_status="evaluated",
                        degraded_reason=None, cache_hits=0,
                        cache_misses=len(candidates), request_count=1)
                ref = (raw_only[0].get("candidate_ref")
                       or raw_only[0]["resource_ref"])
                ver = str(raw_only[0].get("content_version") or "")
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        candidate_ref=ref, candidate_version=ver,
                        relevance_signal=0.8,
                        evaluation_status="evaluated",
                        model_id=self.name, prompt_version="t"),
                        jb.JudgeItem(
                            candidate_ref=ref, candidate_version=ver,
                            relevance_signal=0.7,
                            evaluation_status="evaluated",
                            model_id=self.name, prompt_version="t")],
                    provider_status="evaluated", degraded_reason=None,
                    cache_hits=0, cache_misses=len(candidates),
                    request_count=1)

        jb.register_for_tests("ra003_dup", DupRef())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "ra003_dup"
        try:
            _hold(actors, "dupref正文",
                  our_words=[{"speaker": "qiaosheng", "text": "dup话语",
                              "expression_kind": "paraphrase"}])
            r1 = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {"query_plan": {"original_request": "dup",
                                "channels": ["words"],
                                "lexical_terms": ["dup"],
                                "evidence_requirement":
                                    "verbatim_required"},
                 "operation_id": "op-ra003d-s"},
                None)["data"]
            sid = r1["recall_session_id"]
            _seed_src()
            r2 = registry.invoke(actors["jiaming"], "memory.recall.round2", {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "operation_id": "op-ra003d-r2"}, None)["data"]
            assert r2["candidates"] == [], \
                "重复 ref 整批判断作废——不得保留首项交付正文（审计反例）"
            assert r2["coverage"]["judge"] == "unavailable"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


# ---------------------------------------------------------------- RA-004

class TestKeyNamespaceIsolation:

    def test_transport_op_prefixed_body_key_succeeds(self, actors):
        """审计反例 transport_op_prefix_collision：transport="op:x" 与
        body operation_id="x" 此前碰撞致首次失败；t: 编码后成功且三层
        键互不相交。"""
        mid = _hold(actors, "键隔离正文")["memory_id"]
        out = registry.invoke(
            actors["jiaming"], "memory.delete",
            {"memory_id": mid, "operation_id": "prefixed"},
            "op:prefixed")
        assert out["ok"] is True
        with db.formal() as conn:
            rows = {r["idempotency_key"]: r["status"] for r in conn.execute(
                "SELECT idempotency_key, status FROM idempotency_records"
                " WHERE capability='memory.delete'")}
        assert rows.get("t:op:prefixed") == "completed"
        assert rows.get("op:prefixed") == "completed"
        assert "prefixed" not in rows, "裸键不得再出现（真隔离）"

    def test_deletion_request_op_prefixed_key(self, actors):
        mid = _hold(actors, "申请键隔离")["memory_id"]
        out = registry.invoke(
            actors["qiaosheng"],
            "memory.deletion.request",
            {"memory_id": mid, "reason": "r",
             "operation_id": "prefixed"}, "op:prefixed")
        assert out["ok"] is True
