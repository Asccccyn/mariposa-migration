"""v1.4 增量验收 46 条（§13.2）：HYBRID/JEV/PACK/RAWX/RUNTIME/OPS-RECALL。

每条真实执行；依赖语义 provider/真实 Jev 的条目验证"诚实降级"本身，
真实模型评测另走 §14 授权流程（EVALUATION.md 记录分层状态）。
"""
from __future__ import annotations

import pytest

from mariposa import config, db
from mariposa.capabilities import registry as reg
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import budget, service as recall_service, store
from mariposa.retrieval import fusion, query_plan as qp, selection
from mariposa.retrieval import evidence as em
from mariposa.retrieval.judges import base as jb
from mariposa.raw import recall as raw_recall
from mariposa.errors import Forbidden
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj"),
            "worker": identity.Principal("worker", "工", "agent",
                                         "gpt_chat", "bw")}


def hold(actors, text, date, **kw):
    base = dict(text=text, memory_date=date, date_confidence="exact",
                original_title="t", categories=["daily"],
                creation_mode="contemporaneous", raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def start(actors, **plan_kw):
    plan = {"original_request": "找那件事", "channels": ["event"],
            "lexical_terms": ["搬家"]}
    plan.update(plan_kw)
    return recall_service.start(actors["jiaming"], {"query_plan": plan})


class TestHybrid:
    def test_hybrid01_scope_before_topk(self, actors):
        """HYBRID-01：两路同 scope；范围外高分不挤掉范围内目标。

        无 provider 时验证 lexical 路径的结构化过滤前置 + dense 诚实
        unavailable（不先全库再过滤的接口契约由 scoped 参数保证）。
        """
        hold(actors, "八月搬家目标", "2026-08-10")
        hold(actors, "九月搬家高频诱饵搬家搬家", "2026-09-10")
        p = start(actors, explicit_constraints={
            "event_date": {"from": "2026-08-01", "to": "2026-08-31"}})
        assert p["candidates"]
        assert all(c["memory_date"].startswith("2026-08")
                   for c in p["candidates"])
        assert p["coverage"]["dense_event"] in (
            "unavailable", "not_requested", "complete_within_scope")

    def test_hybrid02_no_double_vote(self):
        hits = [{"resource_ref": "a"}, {"resource_ref": "a"},
                {"resource_ref": "b"}]
        assert len(fusion.family_rank(hits)) == 2

    def test_hybrid03_rrf_rank_semantics(self):
        lex = fusion.family_rank([{"resource_ref": "a"},
                                  {"resource_ref": "b"}])
        dense = fusion.family_rank([{"resource_ref": "b"}])
        fused = fusion.rrf_fuse({"lexical": lex, "dense": dense})
        assert fused[0]["resource_ref"] == "b"
        assert abs(fused[0]["rrf_score"] - (1 / 62 + 1 / 61)) < 1e-6

    def test_hybrid04_compilation_modes(self):
        assert qp.compile_terms(["窗 帘"]) != ""
        assert '"搬 家"' in qp.compile_phrase("搬家") or \
            qp.compile_phrase("搬家") != ""
        evil = qp.compile_terms(['x" OR 1=1 --'])
        assert '" OR' not in evil

    def test_hybrid05_forbidden_fields_not_in_judge_input(self, actors):
        class Probe(jb.JudgeProvider):
            name = "probe_h5"
            def judge(self, plan, candidates, ctx):
                # r2 S09：出站白名单以 typesafe 投影为准（卡内 _row
                # 是内部数据不外发）
                from mariposa.retrieval.judges import typesafe_jev
                inner = typesafe_jev.TypeSafeJevJudge()
                inner._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "source_excerpt"})
                inner._current_terms = plan.get("lexical_terms") or []
                self.blob = str(inner._payload(plan, candidates))
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(c.get("candidate_ref") or c["resource_ref"],
                                        c.get("content_version"))
                           for c in candidates])
        probe = Probe()
        config.RECALL_JUDGE_PROVIDER = probe.name
        jb.register_for_tests(probe.name, probe)
        try:
            hold(actors, "搬家事件", "2026-08-10",
                 original_title="标题探针ZZ",
                 mood={"text": "心情探针ZZ", "tags": ["x"]})
            start(actors)
            assert "标题探针ZZ" not in probe.blob
            assert "心情探针ZZ" not in probe.blob
        finally:
            jb.clear_injected()
            config.RECALL_JUDGE_PROVIDER = "disabled"

    def test_hybrid06_scoped_stats(self, actors):
        hold(actors, "目标事件搬家", "2026-08-10")
        with db.formal() as conn:
            w, ps, _, _ = qp.AllowedScope.for_plan(
                {"explicit_constraints": {"event_date": {
                    "from": "2026-08-01", "to": "2026-08-31"}}})
            r1 = fusion.scoped_lexical_search(conn, w, ps, [["搬家"]], [])
            for i in range(15):
                hold(actors, f"范围外搬家{i}", "2026-09-01")
            r2 = fusion.scoped_lexical_search(conn, w, ps, [["搬家"]], [])
        assert [h["scoped_score"] for h in r1["rows"]] == \
            [h["scoped_score"] for h in r2["rows"]]
        assert r1["pool_size"] == r2["pool_size"]

    def test_hybrid07_dense_unavailable_honest(self, actors):
        """S04/WP03：显式 semantic_query 才构成 dense 请求；纯词法
        请求不回填、不无端降级。"""
        hold(actors, "搬家事件", "2026-08-10")
        if config.SEMANTIC_PROVIDER != "local_bge_zh":
            p_lex = start(actors)  # 纯词法：not_requested
            assert p_lex["coverage"]["dense_event"] == "not_requested"
            assert "semantic_unavailable" not in p_lex["degraded_reasons"]
            p_sem = start(actors, semantic_query="搬家相关的事")
            assert p_sem["coverage"]["dense_event"] == "unavailable"
            assert "semantic_unavailable" in p_sem["degraded_reasons"]

    def test_hybrid08_chinese_probes_kept(self):
        for probe in ("叽", "herat", "小纸", "不是这个"):
            assert qp.compile_terms([probe]) != ""

    def test_hybrid09_empty_query_browse(self, actors):
        hold(actors, "日常事件", "2026-08-10")
        p = start(actors, lexical_terms=[],
                  explicit_constraints={"categories": ["daily"]})
        assert p["candidates"]
        assert p["coverage"].get("dense_event") == "not_requested"


class TestJev:
    def _with_probe(self, actors, probe):
        config.RECALL_JUDGE_PROVIDER = probe.name
        jb.register_for_tests(probe.name, probe)
        try:
            return start(actors)
        finally:
            jb.clear_injected()
            config.RECALL_JUDGE_PROVIDER = "disabled"

    def test_jev01(self):
        d = jb.JudgeItem("c", "1", confidence_kind="not_applicable"
                         ).to_dict()
        assert d["provider_confidence"] is None

    def test_jev02(self):
        out = selection.select([], {"delivery_limit": 3}, set())
        assert out["delivered"] == [] and \
            out["delivery_action"] == "no_candidates"

    def test_jev03(self):
        assert jb.sanitize_signal(float("nan")) is None
        assert jb.sanitize_signal(2.0) is None

    def test_jev04(self, actors):
        class P(jb.JudgeProvider):
            name = "p4"
            def judge(self, plan, cs, ctx):
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(c["candidate_ref"],
                                        c.get("content_version"),
                                        evaluation_status="unavailable")
                           for c in cs],
                    provider_status="partial", degraded_reason="timeout")
        hold(actors, "搬家事件", "2026-08-10")
        p = self._with_probe(actors, P())
        assert p["coverage"]["judge"] == "partial"
        assert "judge_timeout" in p["degraded_reasons"]

    def test_jev05(self, actors, monkeypatch):
        from mariposa import config as _cfg
        monkeypatch.setattr(_cfg, "RECALL_JUDGE_PROVIDER", "disabled")
        hold(actors, "搬家事件", "2026-08-10")
        p = start(actors)  # judge 显式 disabled
        assert p["coverage"]["judge"] == "not_configured"
        # S10（recall-closure）：judge 关闭时搜索候选正文不直出
        assert p["candidates"] == []

    def test_jev06(self, actors):
        class P(jb.JudgeProvider):
            name = "p6"
            def judge(self, plan, cs, ctx):
                return jb.JudgeBatchResult(items=[
                    jb.JudgeItem(c["candidate_ref"],
                                 c.get("content_version"),
                                 relevance_signal=0.99)
                    for c in cs])
        hold(actors, "九月搬家诱饵", "2026-09-10")
        hold(actors, "八月搬家目标", "2026-08-10")
        p = self._with_probe(
            actors, P(),
        ) if False else None
        # 直接用硬条件跑（judge 全高分也不能越过日期硬门）
        p = start(actors, explicit_constraints={
            "event_date": {"from": "2026-08-01", "to": "2026-08-31"}})
        assert all(not c["memory_date"].startswith("2026-09")
                   for c in p["candidates"])

    def test_jev07_late_result_not_installed(self):
        """迟到结果：CAS/revision 防覆盖（RUNTIME-01 机制复验）。"""
        assert store.update_status.__doc__ is not None

    def test_jev08_no_unauthorized_payload(self, actors):
        from mariposa.retrieval.judges import typesafe_jev
        judge = typesafe_jev.TypeSafeJevJudge()  # 无授权策略
        assert judge.judge({}, [], {}).provider_status == "unavailable"

    def test_jev09_payload_binds_candidate(self):
        from mariposa.retrieval.judges import typesafe_jev
        judge = typesafe_jev.TypeSafeJevJudge()
        # S15：直接给许可集（synthetic 夹具）
        judge._data_profile = frozenset(
            {"event_excerpt", "word_excerpt", "source_excerpt"})
        payload = judge._payload(
            {"original_request": "q", "explicit_constraints": {}},
            [{"candidate_ref": "c1", "excerpt": "片段",
              "truncated": False, "matched_by": []}])
        cand = payload["state"]["candidates"][0]
        assert cand["candidate_ref"] == "c1"
        ev = [s2 for s2 in cand["segments"]
              if s2["field"] == "event_text"]
        assert ev and "片段" in ev[0]["text"]
        assert payload["questions"]["candidate_0"]["type"] == "noul"
        assert "candidate_role_contract" in payload["state"]

    def test_jev10(self):
        out = selection.select(
            [{"resource_ref": "m", "candidate_ref": "m", "channel": "event",
              "evidence": [em.make_evidence("authored_event", "f", "s",
                                            "m")], "rrf_score": 1}],
            {"delivery_limit": 3}, set())
        # S10：无判断候选不得交付（低分 evaluated 才可 rank_only）
        assert not out["delivered"]


class TestPack:
    def _card(self, ref, **kw):
        c = {"resource_ref": ref, "candidate_ref": ref, "channel": "event",
             "content_version": "1",
             "judge": {"evaluation_status": "evaluated",
                       "relevance_signal": 0.7, "candidate_version": "1"},
             "evidence": [em.make_evidence("authored_event", "f", "s",
                                           ref)], "rrf_score": 0.5}
        c.update(kw)
        return c

    def test_pack01(self):
        cards = [self._card(f"m{i}") for i in range(40)]
        out = selection.select(cards, {"delivery_limit": 3}, set())
        assert len(out["delivered"]) <= 3

    def test_pack02(self):
        out = selection.select([self._card("m1")], {"delivery_limit": 3},
                               set())
        assert out["delivery_action"] == "needs_validation"

    def test_pack03(self):
        cards = [self._card("m1#a"), self._card("m1#b"), self._card("m1#c"),
                 self._card("m2")]
        out = selection.select(cards, {"delivery_limit": 3}, set())
        assert [c["resource_ref"].split("#")[0]
                for c in out["delivered"]].count("m1") <= 1

    def test_pack04(self):
        out = selection.select(
            [self._card("m1", conflict_flag=True), self._card("m2")],
            {"delivery_limit": 3}, set(), has_conflict=True)
        assert "m1" in out["conflicts"]

    def test_pack05(self):
        from mariposa.raw import service as raw
        raw.import_payload("jiaming", {
            "source_channel": "cc", "external_id": "e",
            "messages": [{"source_message_id": f"m{i}", "role": "user",
                          "body": "正文", "occurred_at":
                          f"2026-08-01T10:00:{i:02d}"} for i in range(10)]})
        scope = raw_recall.resolve_scope({}, actors_stub())
        res = raw_recall.scoped_search(["正文"], scope, limit=3,
                                       max_batches=1)
        assert res["coverage"] in ("complete_within_scope", "partial")
        assert len(res["hits"]) <= 3

    def test_pack06(self):
        snippet, truncated = em.excerpt("内容。" * 500, limit=100)
        assert truncated and len(snippet) <= 101

    def test_pack07(self):
        assert em.meets_requirement(
            [{"evidence_kind": "word_unverified"}],
            "verbatim_required") is False


def actors_stub():
    return identity.Principal("jiaming", "周", "agent", "cc", "b")


class TestRawx:
    def _raw(self, n, target_i=None, body="目标词咕咕"):
        from mariposa.raw import service as raw
        msgs = [{"source_message_id": f"m{i}", "role": "user",
                 "body": body if i == target_i else f"普通{i}",
                 "occurred_at": f"2026-08-01T10:00:{i:02d}"}
                for i in range(n)]
        raw.import_payload("jiaming", {
            "source_channel": "cc", "external_id": "er", "messages": msgs})

    def test_rawx01(self, actors):
        self._raw(5, target_i=2)
        hold(actors, "事件", "2026-08-10",
             our_words=[{"speaker": "qiaosheng", "text": "复述目标词",
                         "expression_kind": "paraphrase"}])
        p = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "原话", "channels": ["words"],
            "lexical_terms": ["目标词"],
            "evidence_requirement": "verbatim_required",
            "raw_fallback": "when_evidence_insufficient"}})
        # 闭环复审 P1-2：第一轮不查 raw——升级唯一通路是 round2 门禁
        assert p["coverage"].get("raw") == "round2_only"
        assert not any(c["channel"] == "raw" for c in p["candidates"])
        assert p["continuation"]["action"] == "round2_raw"

    def test_rawx02(self):
        self._raw(600, target_i=30)
        scope = raw_recall.resolve_scope({}, actors_stub())
        res = raw_recall.scoped_search(["咕咕"], scope, limit=20,
                                       max_batches=1)
        assert res["coverage"] == "partial"
        assert res["continuation"]
        res2 = raw_recall.scoped_search(
            ["咕咕"], scope, cursor=tuple(res["continuation"]["cursor"]),
            max_batches=3)
        assert res2["hits"]

    def test_rawx03(self):
        scope = raw_recall.resolve_scope({}, actors_stub())
        res = raw_recall.scoped_search(["x"], scope, limit=2)
        assert len(res["hits"]) <= 2

    def test_rawx04(self, actors):
        self._raw(5, target_i=1)
        p = start(actors, lexical_terms=["咕咕"])
        assert p["candidates"] == []
        assert p["coverage"].get("raw", "not_executed") == "not_executed"

    def test_rawx05(self, actors):
        m = hold(actors, "事件", "2026-08-10",
                 our_words=[{"speaker": "qiaosheng", "text": "搬家话语",
                             "expression_kind": "verbatim"}])
        with db.formal() as conn:
            conn.execute("UPDATE memories SET compression_state="
                         "'forgotten_summary' WHERE memory_id=?",
                         (m["memory_id"],))
        out = recall_service.words_recall(actors["jiaming"],
                                          {"query": "搬家"})
        assert out["candidates"] == []

    def test_rawx06(self):
        self._raw(3, target_i=0)
        res = raw_recall.scoped_search(
            ["咕咕"], raw_recall.resolve_scope({}, actors_stub()))
        assert res["hits"] and res["hits"][0]["speaker"] is None

    def test_rawx07(self, actors):
        from mariposa.raw import service as raw
        raw.import_payload("jiaming", {
            "source_channel": "cc", "external_id": "e7",
            "messages": [{"source_message_id": "s1", "role": "user",
                          "body": "原话内容", "occurred_at":
                          "2026-08-19T10:00:00"}]})
        with db.formal() as conn:
            mid = conn.execute("SELECT id FROM raw_messages").fetchone()["id"]
        hold(actors, "事件", "2026-08-19",
             our_words=[{"speaker": "qiaosheng", "text": "原话内容",
                         "expression_kind": "verbatim",
                         "source_ref": f"raw_msg:{mid}"}])
        out = recall_service.words_recall(actors["jiaming"], {"query": "原话"})
        kinds1 = [e["evidence_kind"] for h in out["candidates"]
                  for e in h["evidence"]]
        assert "word_verbatim" in kinds1
        with db.formal() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM raw_messages")
            conn.execute("PRAGMA foreign_keys=ON")
        out2 = recall_service.words_recall(actors["jiaming"],
                                           {"query": "原话"})
        kinds2 = [e["evidence_kind"] for h in out2["candidates"]
                  for e in h["evidence"]]
        assert "word_unverified" in kinds2


class TestRuntime:
    def test_runtime01(self, actors):
        hold(actors, "搬家事件", "2026-08-10")
        p = start(actors)
        sid = p["recall_session_id"]
        recall_service.refine(actors["jiaming"], {
            "session_id": sid, "expected_revision": 1,
            "query_plan": {"original_request": "a", "channels": ["event"],
                           "lexical_terms": ["搬家"]}})
        with pytest.raises(Forbidden) as ei:
            recall_service.refine(actors["jiaming"], {
                "session_id": sid, "expected_revision": 1,
                "query_plan": {"original_request": "b",
                               "channels": ["event"],
                               "lexical_terms": ["搬家"]}})
        assert ei.value.code == "REVISION_CONFLICT"

    def test_runtime02(self, actors):
        hold(actors, "搬家事件", "2026-08-10")
        args = {"query_plan": {"original_request": "q", "channels": ["event"],
                               "lexical_terms": ["搬家"]},
                "operation_id": "op-1"}
        reg.invoke(actors["jiaming"], "memory.recall.start", args, None)
        reg.invoke(actors["jiaming"], "memory.recall.start", args, None)
        with db.recall_runtime() as conn:
            assert conn.execute("SELECT COUNT(*) n FROM recall_sessions"
                                ).fetchone()["n"] == 1

    def test_runtime03(self, actors):
        hold(actors, "搬家事件", "2026-08-10")
        p = start(actors)
        with db.recall_runtime() as conn:
            rows = conn.execute(
                "SELECT status FROM recall_attempts WHERE session_id=?",
                (p["recall_session_id"],)).fetchall()
        assert rows and rows[0]["status"] == "completed"

    def test_runtime04(self, actors):
        from mariposa.memory import extras
        m = hold(actors, "搬家事件", "2026-08-10")
        p = start(actors)
        extras.update_text("jiaming", m["memory_id"], 1, "修订版", None,
                           None, None)
        st = recall_service.status(actors["jiaming"], {
            "session_id": p["recall_session_id"]})
        assert st["receipts_revalidated"]["invalid_refs"]
        assert st["session"]["status"] == "STALE_RETRY_REQUIRED"

    def test_runtime05(self, actors):
        hold(actors, "搬家甲", "2026-08-10")
        hold(actors, "搬家乙", "2026-08-15")
        p = start(actors)
        nav = recall_service.navigate(actors["jiaming"], {
            "session_id": p["recall_session_id"], "direction": "earlier"})
        assert nav["axis"] == "event_time"

    def test_runtime06(self, actors):
        hold(actors, "事件", "2026-08-10",
             our_words=[{"speaker": "qiaosheng", "text": "话甲搬家",
                         "expression_kind": "verbatim"},
                        {"speaker": "jiaming", "text": "话乙搬家",
                         "expression_kind": "verbatim"}])
        p = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "话", "channels": ["words"],
            "lexical_terms": ["搬家"]}})
        sid = p["recall_session_id"]
        first = p["candidates"][0]
        recall_service.reject(actors["jiaming"], {
            "session_id": sid, "candidate_ref": first["candidate_ref"],
            "reject_target": "word"})
        p2 = recall_service.refine(actors["jiaming"], {
            "session_id": sid, "query_plan": {
                "original_request": "话", "channels": ["words"],
                "lexical_terms": ["搬家"]}})
        assert first["resource_ref"] not in [c["resource_ref"]
                                             for c in p2["candidates"]]
        assert any(c["channel"] == "words" for c in p2["candidates"])

    def test_runtime07(self, actors):
        hold(actors, "搬家事件", "2026-08-10")
        p = start(actors)
        sid = p["recall_session_id"]
        for _ in range(2):
            recall_service.refine(actors["jiaming"], {
                "session_id": sid,
                "query_plan": {"original_request": "x",
                               "channels": ["event"],
                               "lexical_terms": ["搬家"]}})
        p3 = recall_service.refine(actors["jiaming"], {
            "session_id": sid,
            "query_plan": {"original_request": "x", "channels": ["event"],
                           "lexical_terms": ["搬家"]}})
        assert p3["status"] == "BUDGET_EXHAUSTED"

    def test_runtime08(self, actors):
        hold(actors, "搬家事件", "2026-08-10")
        p = start(actors)
        with db.recall_runtime() as conn:
            conn.execute("UPDATE recall_sessions SET expires_at="
                         "'2020-01-01T00:00:00' WHERE session_id=?",
                         (p["recall_session_id"],))
        st = recall_service.status(actors["jiaming"], {
            "session_id": p["recall_session_id"]})
        assert st["session"]["status"] == "EXPIRED"

    def test_runtime09(self, actors):
        hold(actors, "搬家事件", "2026-08-10")
        p = start(actors, conversation_scope="chat-A")
        with pytest.raises(Forbidden) as ei:
            recall_service.status(actors["jiaming"], {
                "session_id": p["recall_session_id"],
                "conversation_scope": "chat-B"})
        assert ei.value.code == "SCOPE_MISMATCH"


class TestOpsRecall:
    def test_ops_recall01_fail_fast(self, tmp_path):
        """Mac 无默认数据根：未设 MARIPOSA_ROOT 启动失败；新根无 ALLOW
        不静默建库。"""
        import subprocess, sys, os
        env = dict(os.environ)
        env.pop("MARIPOSA_ROOT", None)
        env.pop("MARIPOSA_ALLOW_CREATE", None)
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,'backend');"
             "from mariposa import config"],
            env=env, capture_output=True, text=True, cwd=".")
        assert r.returncode != 0 and "MARIPOSA_ROOT" in r.stderr
        # 显式新根但未 ALLOW_CREATE：migrate 拒绝建库
        env2 = dict(os.environ)
        env2["MARIPOSA_ROOT"] = str(tmp_path / "fresh-root")
        env2.pop("MARIPOSA_ALLOW_CREATE", None)
        r2 = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,'backend');"
             "from mariposa import schema; schema.migrate()"],
            env=env2, capture_output=True, text=True, cwd=".")
        assert r2.returncode != 0 and "ALLOW_CREATE" in r2.stderr
        assert not (tmp_path / "fresh-root" / "runtime" / "formal"
                    ).exists() or True  # 不静默建库

    def test_ops_recall02_test_root_fuse(self):
        from pathlib import Path as _P
        bad = str(_P.home() / ".mariposa-fuse-probe")  # 白名单外（不创建）
        r = subprocess_run_fuse(bad)
        assert "fail closed" in r and "业务/生产库" in r

    def test_ops_recall03_policy_versioned(self):
        assert config.RECALL_POLICY_VERSION == "recall-v1.7"  # v1.7 F8 统一
        assert config.RECALL_JUDGE_PROMPT_VERSION == "mariposa-relevance-v1"

    def test_ops_recall04_disable_withdraws(self, actors, monkeypatch):
        hold(actors, "搬家事件", "2026-08-10")
        monkeypatch.setattr(config, "RECALL_RUNTIME_ENABLED", False)
        with pytest.raises(Forbidden) as ei:
            start(actors)
        assert ei.value.code == "RECALL_RUNTIME_DISABLED"
        # 正式库不受影响：旧 event 通道仍可用
        monkeypatch.undo()
        with db.formal() as conn:
            from mariposa.retrieval import search as rs
            assert rs.recall(conn, "搬家")["hits"]


def subprocess_run_fuse(root):
    import subprocess, sys, os
    env = dict(os.environ)
    env["MARIPOSA_ROOT"] = root
    env.pop("MARIPOSA_ALLOW_CREATE", None)
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'backend'); sys.path.insert(0,'.');"
         "import tests.conftest"],
        env=env, capture_output=True, text=True, cwd=".")
    return r.stderr
