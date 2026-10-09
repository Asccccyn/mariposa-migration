"""Codex SDK provider 离线验收（WP6：J03/J09/J10/J11/J12）。

全部 fake transport / monkeypatch 离线：零网络、零真实模型、不读真实凭据。
真实 SDK smoke/质量对比 = live 项（无认证，NOT_EXECUTED——readiness 如实
blocked，不冒称可用）。
"""
from __future__ import annotations

import json

import pytest

from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy, paging, service as recall_service
from mariposa.retrieval.judges import base as jb
from mariposa.retrieval.judges import codex_sdk
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal(
        "jiaming", "周家明", "agent", "claude_chat", "bj")}


def hold(actors, text, our_words=None):
    return memory.hold(actors["jiaming"], text=text,
                       memory_date="2026-09-25", date_confidence="exact",
                       original_title="w", categories=["daily"],
                       creation_mode="contemporaneous", raw_pending=False,
                       our_words=our_words)


def fake_transport_ok(items_out):
    calls = []
    closed = {"count": 0}

    def transport(spec):
        calls.append(spec)
        # 临时 thread 语义：每次调用=新 thread（ephemeral+read_only 传入）
        return {"status": "ok",
                "text": json.dumps({"items": items_out}, ensure_ascii=False)}

    return transport, calls, closed


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    codex_sdk.clear_transport()
    jb.clear_injected()


def enable_codex(monkeypatch, allowed="event_excerpt,source_excerpt",
                 model="codex-test-model"):
    monkeypatch.setenv("MARIPOSA_CODEX_ALLOWED_DATA", allowed)
    monkeypatch.setenv("MARIPOSA_CODEX_MODEL_ID", model)
    judge = codex_sdk.CodexSdkJudge()
    jb.register_for_tests("codex_sdk", judge)
    cur = judge_policy.get_policy()
    judge_policy.update_policy(
        "qiaosheng", expected_revision=cur["revision"], enabled=True,
        provider="codex_sdk", idempotency_key="wp6-on")


class TestJ03CodexJudge:
    def test_j03_codex_alone_no_jev_prefilter(self, actors, monkeypatch):
        """J03：开启 Codex，与 Jev 相同候选输入——仅 Codex 被调用，候选
        没有先经 Jev 裁减，正文由 Mariposa 装配（非 Codex 改写）。"""
        for i in range(4):
            hold(actors, f"崧蓝事件{i}")
        refs_seen: dict[str, list] = {}

        def transport(spec):
            refs_seen.setdefault("calls", []).append(spec)
            payload = json.loads(spec["prompt"])
            refs = [c["candidate_ref"] for c in payload["candidates"]]
            refs_seen["refs"] = refs
            return {"status": "ok", "text": json.dumps({
                "items": [
                    {"candidate_ref": r,
                     "candidate_version": None,
                     "relevant": "relevant" if i % 2 == 0 else "uncertain"}
                    for i, r in enumerate(refs)]})}

        codex_sdk.set_transport_for_tests(transport)
        # Jev 同时注册（诱饵）：若发生"Jev 先筛"，Jev 会被调用
        jev_calls = []

        class DecoyJev(jb.JudgeProvider):
            name = "recording_test"

            def judge(self, plan, candidates, ctx):
                jev_calls.append(candidates)
                return jb.JudgeBatchResult(provider_status="evaluated")

        jb.register_for_tests("recording_test", DecoyJev())
        enable_codex(monkeypatch)
        packet = recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找崧蓝", "channels":
                            ["event"], "lexical_terms": ["崧蓝"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:wp6-j03",
                    "payload_hash": "h"})
        assert packet["judge_mode"] == "on"
        assert packet["coverage"]["judge"] == "evaluated"
        assert len(jev_calls) == 0, "Jev 不得预先筛掉候选（不串联）"
        assert len(refs_seen.get("refs") or []) == 4, "全部候选送 Codex"
        # 正文由 Mariposa 装配：交付卡 evidence 来自本地记录（含原始 snippet）
        assert packet["judged_count"] >= 1
        assert all(c.get("candidate_ref") for c in packet["candidates"])

    def test_j03_same_material_as_jev(self, actors, monkeypatch):
        """J03：同一批候选分别给 Jev/Codex——送判集合一致（同材料不接力）。"""
        hold(actors, "崧蓝事件甲")
        hold(actors, "崧蓝事件乙")
        captured: dict[str, list] = {}

        def codex_transport(spec):
            payload = json.loads(spec["prompt"])
            captured["codex"] = sorted(
                c["candidate_ref"] for c in payload["candidates"])
            refs = captured["codex"]
            return {"status": "ok", "text": json.dumps({
                "items": [{"candidate_ref": r, "relevant": "relevant"}
                          for r in refs]})}

        class RecordingJev(jb.JudgeProvider):
            name = "recording_test"

            def judge(self, plan, candidates, ctx):
                captured["jev"] = sorted(
                    c["candidate_ref"] for c in candidates)
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        candidate_ref=c["candidate_ref"],
                        candidate_version=c.get("content_version"),
                        relevance_signal=0.5,
                        evaluation_status="evaluated")
                        for c in candidates],
                    provider_status="evaluated")

        jb.register_for_tests("recording_test", RecordingJev())
        # 先 Codex
        codex_sdk.set_transport_for_tests(codex_transport)
        enable_codex(monkeypatch)
        recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找崧蓝", "channels":
                            ["event"], "lexical_terms": ["崧蓝"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:wp6-a", "payload_hash": "a"})
        # 再切 Jev（同库同查询）
        cur = judge_policy.get_policy()
        judge_policy.update_policy(
            "qiaosheng", expected_revision=cur["revision"], enabled=True,
            provider="recording_test", idempotency_key="wp6-jev")
        recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找崧蓝", "channels":
                            ["event"], "lexical_terms": ["崧蓝"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:wp6-b", "payload_hash": "b"})
        assert captured["codex"] == captured["jev"], \
            "两个 provider 消费同一事实材料（非 Jev 筛后集合）"


class TestJ09OutputValidation:
    def _run_nohold(self, actors, monkeypatch, items_out):
        codex_sdk.set_transport_for_tests(
            fake_transport_ok(items_out)[0])
        enable_codex(monkeypatch)
        packet = recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找崧蓝", "channels":
                            ["event"], "lexical_terms": ["崧蓝"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:j09", "payload_hash": "h"})
        return packet

    def _run(self, actors, monkeypatch, items_out):
        hold(actors, "崧蓝事件")
        return self._run_nohold(actors, monkeypatch, items_out)
        enable_codex(monkeypatch)
        packet = recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找崧蓝", "channels":
                            ["event"], "lexical_terms": ["崧蓝"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:j09", "payload_hash": "h"})
        return packet

    def test_unknown_ref_rejected(self, actors, monkeypatch):
        packet = self._run(actors, monkeypatch, [
            {"candidate_ref": "memory:nonexistent", "relevant": "relevant"}])
        assert packet["coverage"]["judge"] == "unavailable"
        assert "judge_codex_unknown_ref" in packet["degraded_reasons"]
        assert packet["candidates"] == [], "无效判断不释放正文"

    def test_duplicate_ref_rejected(self, actors, monkeypatch):
        hold(actors, "崧蓝事件")
        from mariposa import db as _db
        with _db.formal() as conn:
            mid = conn.execute("SELECT memory_id FROM memories"
                               ).fetchone()["memory_id"]
        packet = self._run_nohold(actors, monkeypatch, [
            {"candidate_ref": f"memory:{mid}", "relevant": "relevant"},
            {"candidate_ref": f"memory:{mid}", "relevant": "irrelevant"}])
        assert packet["coverage"]["judge"] == "unavailable"
        assert "judge_codex_duplicate_ref" in packet["degraded_reasons"]

    def test_relevant_invalid_rejected(self, actors, monkeypatch):
        hold(actors, "崧蓝事件")
        from mariposa import db as _db
        with _db.formal() as conn:
            mid = conn.execute("SELECT memory_id FROM memories"
                               ).fetchone()["memory_id"]
        packet = self._run_nohold(actors, monkeypatch, [
            {"candidate_ref": f"memory:{mid}",
             "relevant": "definitely"}])
        assert packet["coverage"]["judge"] == "unavailable"
        assert "judge_codex_relevant_invalid" in packet["degraded_reasons"]

    def test_not_json_rejected(self, actors, monkeypatch):
        hold(actors, "崧蓝事件")
        codex_sdk.set_transport_for_tests(
            lambda spec: {"status": "ok", "text": "不是 JSON"})
        enable_codex(monkeypatch)
        packet = recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找崧蓝", "channels":
                            ["event"], "lexical_terms": ["崧蓝"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:j09b", "payload_hash": "h"})
        assert packet["coverage"]["judge"] == "unavailable"
        assert "judge_codex_output_not_json" in packet["degraded_reasons"]

    def test_missing_items_counted_unavailable(self, actors, monkeypatch):
        packet = self._run(actors, monkeypatch, [])  # provider 全漏返回
        assert packet["coverage"]["judge"] == "evaluated"  # 对账通过（空列表合法）
        # 但 receipts 里 unavailable 计数=送判数（不凭空消失）
        assert packet["judged_count"] == 0


class TestJ10Lifecycle:
    def test_serial_lock_and_thread_per_call(self, actors, monkeypatch):
        hold(actors, "崧蓝事件")
        active = {"now": 0, "max": 0, "calls": 0}

        def transport(spec):
            active["calls"] += 1
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            import time as _t
            _t.sleep(0.02)
            active["now"] -= 1
            payload = json.loads(spec["prompt"])
            refs = [c["candidate_ref"] for c in payload["candidates"]]
            return {"status": "ok", "text": json.dumps({
                "items": [{"candidate_ref": r, "relevant": "relevant"}
                          for r in refs]})}

        codex_sdk.set_transport_for_tests(transport)
        enable_codex(monkeypatch)
        import threading
        errs = []

        def run_one(k):
            try:
                judge = codex_sdk.CodexSdkJudge()
                judge.judge({"original_request": "q", "lexical_terms": []},
                            [], {})
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        threads = [threading.Thread(target=run_one, args=(i,))
                   for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errs
        assert active["max"] == 1, "并发 1（进程内串行锁）"

    def test_ephemeral_readonly_isolated_cwd(self, actors, monkeypatch):
        specs = []

        def transport(spec):
            specs.append(spec)
            return {"status": "ok", "text": json.dumps({"items": []})}

        monkeypatch.setenv("MARIPOSA_CODEX_ALLOWED_DATA",
                           "event_excerpt")
        codex_sdk.set_transport_for_tests(transport)
        judge = codex_sdk.CodexSdkJudge()
        judge.judge({"original_request": "q"}, [], {})
        assert specs, "至少一次调用"
        assert specs[0]["ephemeral"] is True, "临时 thread（ephemeral 优先）"
        assert specs[0]["sandbox"] == "read_only", "只读沙盒"
        assert "isolated" in specs[0]["cwd"], "隔离空目录（不当沙盒用）"
        assert specs[0]["timeout_ms"] > 0, "deadline 贯通"


class TestJ11Permissions:
    def test_readiness_blocked_without_grants(self, monkeypatch):
        monkeypatch.delenv("MARIPOSA_CODEX_ALLOWED_DATA", raising=False)
        monkeypatch.delenv("CODEX_ALLOWED_DATA", raising=False)
        out = codex_sdk.readiness()
        assert out["ready"] is False
        assert out["blocked_reason"] in ("sdk_not_installed",
                                         "allowed_data_missing")

    def test_grants_fail_closed_on_typo(self, monkeypatch):
        monkeypatch.setenv("MARIPOSA_CODEX_ALLOWED_DATA",
                           "event_exceprt")  # 拼错
        assert codex_sdk.parse_grants("event_exceprt") is None, \
            "拼错=整份拒绝（不猜不放宽）"
        judge = codex_sdk.CodexSdkJudge()
        assert judge.outbound_grants() == frozenset()
        result = judge.judge({"original_request": "q"}, [], {})
        assert result.provider_status == "unavailable"
        assert result.degraded_reason == "allowed_data_profile_invalid"

    def test_jev_grant_not_auto_transferred(self, monkeypatch):
        """J12/J11：Jev 侧许可存在 ≠ Codex 获得许可（分立配置）。"""
        monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                           "event_excerpt,source_excerpt")
        monkeypatch.delenv("MARIPOSA_CODEX_ALLOWED_DATA", raising=False)
        monkeypatch.delenv("CODEX_ALLOWED_DATA", raising=False)
        judge = codex_sdk.CodexSdkJudge()
        assert judge.outbound_grants() == frozenset(), \
            "Jev 的批准不自动转授 Codex"

    def test_round2_own_grant_passes(self, actors, monkeypatch):
        """R04 补充：Codex 开启且有其自身 source_excerpt 许可 → Round2 门
        不因类型不是 TypeSafeJevJudge 被拒（outbound_grants 接口）。
        WP-01 A03：政策纪元=mode+revision——先切 codex 再 start，
        Round1/Round2 同纪元（旧夹具 start 后切政策现按
        RECALL_POLICY_CHANGED 拒绝，须重新发起查询）。"""
        from mariposa.source import importer
        import tempfile, pathlib
        tmp = pathlib.Path(tempfile.mkdtemp())
        f = tmp / "s.json"
        f.write_text(json.dumps([{
            "uuid": "c-wp6", "chat_messages": [{
                "uuid": "wp6-m1", "sender": "human",
                "created_at": "2026-09-20T10:00:00.000Z",
                "content": [{"type": "text",
                             "text": "原文里的崧蓝染色记忆"}]}]}],
            ensure_ascii=False), encoding="utf-8")
        importer.import_file("jiaming", str(f))
        hold(actors, "崧蓝事件正文",
     our_words=[{"speaker": "qiaosheng", "text": "复述：崧蓝",
                 "expression_kind": "paraphrase"}])

        def transport(spec):
            payload = json.loads(spec["prompt"])
            refs = [c["candidate_ref"] for c in payload["candidates"]]
            return {"status": "ok", "text": json.dumps({
                "items": [{"candidate_ref": r, "relevant": "relevant"}
                          for r in refs]})}

        codex_sdk.set_transport_for_tests(transport)
        # WP-02（CX-15）：判断出站按卡必要角色过滤——words 专项卡需
        # word_excerpt 许可（单 event/source 许可下 words 候选正确被拦）
        enable_codex(monkeypatch,
                     allowed="event_excerpt,word_excerpt,source_excerpt")
        from mariposa.capabilities import registry
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {"query_plan": {
                                 "original_request": "原话",
                                 "channels": ["words"],
                                 "lexical_terms": ["崧蓝"],
                                 "evidence_requirement":
                                     "verbatim_required"},
                              "operation_id": "wp6-r1"}, None)
        sid = r1["data"]["recall_session_id"]
        r2 = registry.invoke(actors["jiaming"], "memory.recall.round2",
                             {"session_id": sid,
                              "reason": "VERBATIM_REQUIRED_NOT_MET",
                              "operation_id": "wp6-r2"}, None)
        assert r2["ok"] is True, json.dumps(r2)[:300]
        assert r2["data"]["coverage"]["judge"] == "evaluated"


class TestJ12CacheSeparation:
    def test_cache_namespaces_do_not_collide(self):
        from mariposa.retrieval.judges import cache
        common = dict(
            query_projection={"q": 1}, candidate_projection={"c": 1},
            candidate_ref="memory:x", candidate_version="1",
            representation_version="1", projection_version="p1",
            requested_model="m", prompt_version="v",
            policy_version="pol", schema_version="s")
        jev = cache.rerank_identity(**common, namespace="jev")
        codex = cache.rerank_identity(**common, namespace="codex")
        assert jev["cache_key"] != codex["cache_key"], \
            "Jev 与 Codex 分数缓存不互相命中（provider 入键）"

    def test_policy_off_never_enters_codex(self, actors, monkeypatch):
        """J01 补充：关闭模式完全不进入 Codex 模块（构造/调用计数零）。"""
        entered = []
        orig_init = codex_sdk.CodexSdkJudge.__init__

        def spy_init(self):
            entered.append(1)
            orig_init(self)

        monkeypatch.setattr(codex_sdk.CodexSdkJudge, "__init__", spy_init)
        hold(actors, "崧蓝事件")
        cur = judge_policy.get_policy()
        judge_policy.update_policy(
            "qiaosheng", expected_revision=cur["revision"], enabled=False,
            provider=None, idempotency_key="wp6-off")
        recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找崧蓝", "channels":
                            ["event"], "lexical_terms": ["崧蓝"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:wp6-off",
                    "payload_hash": "h"})
        assert entered == [], "政策关闭不构造 Codex provider"
