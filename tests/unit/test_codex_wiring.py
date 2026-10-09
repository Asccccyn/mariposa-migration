"""WP-02（run-160858）：Codex 判断器生产接线打通（JFA-002/E05 +
CX-15/C-008 + CX-03/C-009）。

- T1（JFA-002 基线红）：SDK 在场（fake 模块）→ get_provider_by_name
  ("codex_sdk") 构造 CodexSdkJudge（与 readiness 同源；基线返回
  DisabledJudge）。
- T2：SDK 缺失 → DisabledJudge + provider_readiness 披露
  sdk_not_installed。
- T3（组件链路）：fake transport 下经 recall_service.start（政策
  provider=codex_sdk 真名，不经 _INJECTED 注入名）走通 fresh 交付。
- T4（E05）：live 形态（fake SDK 模块）连续 judge → tempfile 根下
  codex-judge-* 零残留；超时（fake 慢 transport）→ unavailable。
- T5b（CX-15）：单许可下未许可卡不进 prompt（按必要角色过滤）。
- T5c（CX-03）：policy.model_id 注入构造器，与 env 冲突时政策胜。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import types

import pytest

from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy, service as recall_service
from mariposa.retrieval.judges import base as jb
from mariposa.retrieval.judges import codex_sdk
from tests.conftest import reset_all


class _AgentMessageThreadItem:
    def __init__(self, message: str):
        self.message = message


class _TurnItem:
    def __init__(self, message: str):
        self.root = _AgentMessageThreadItem(message)


class _Result:
    def __init__(self, message: str):
        self.turn = types.SimpleNamespace(items=[_TurnItem(message)])


class _Thread:
    def __init__(self, reply: str):
        self._reply = reply
        self.calls: list[dict] = []

    # RRA-013：签名与锁定版 0.161.0 Thread.run 一致（无 timeout 参数）
    # ——fake 接受发明参数会掩盖真实接口不匹配
    def run(self, inputs, *, output_schema=None, model=None):
        self.calls.append({"inputs": inputs, "model": model})
        return _Result(self._reply)


class _Codex:
    def __init__(self, reply: str):
        self._reply = reply
        self.threads: list[_Thread] = []

    def thread_start(self, *, model=None, sandbox=None, cwd=None,
                     ephemeral=None, developer_instructions=None):
        t = _Thread(self._reply)
        self.threads.append(t)
        return t

    def close(self):
        pass


def _fake_sdk_module(reply: str = '{"items": []}') -> types.ModuleType:
    m = types.ModuleType("openai_codex")
    m.Codex = lambda config=None: _Codex(reply)
    m.CodexConfig = lambda *a, **kw: types.SimpleNamespace()
    m.Sandbox = types.SimpleNamespace(read_only="read_only")
    m.TextInput = lambda text=None: types.SimpleNamespace(text=text)
    return m


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj"),
            "qiaosheng": identity.Principal("qiaosheng", "江乔生",
                                            "human", "web", "bq")}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    jb.clear_injected()
    codex_sdk.set_transport_for_tests(None)
    sys.modules.pop("openai_codex", None)


def _hold(actors, text):
    return memory.hold(
        actors["jiaming"], text=text, memory_date="2026-09-01",
        date_confidence="exact", original_title="cw", categories=["daily"],
        creation_mode="contemporaneous", raw_pending=False)


class TestWiring:
    def test_t1_sdk_present_constructs_codex_judge(self, actors, monkeypatch):
        """T1：SDK 在场（fake 模块）→ 真构造 CodexSdkJudge（基线红：
        修复前恒 DisabledJudge——JFA-002 核心）。"""
        monkeypatch.setitem(sys.modules, "openai_codex",
                            _fake_sdk_module())
        assert codex_sdk.sdk_available() is True
        p = jb.get_provider_by_name("codex_sdk")
        assert isinstance(p, codex_sdk.CodexSdkJudge)
        assert not isinstance(p, jb.DisabledJudge)
        # readiness 与运行时同源识别 SDK 在场（不再报 sdk_not_installed；
        # ready 布尔另需 key/model/grants 配置——T3 链路与 T2 缺失态覆盖）
        rd = judge_policy.provider_readiness("codex_sdk")
        assert rd["blocked_reason"] != "sdk_not_installed"
        assert rd["sdk_version"] == codex_sdk.CODEX_SDK_VERSION

    def test_t2_sdk_missing_disabled_and_disclosed(self, actors):
        """T2：SDK 缺失（真机未装 openai-codex）→ DisabledJudge +
        readiness 披露 sdk_not_installed（阻断不静默换 provider）。"""
        sys.modules.pop("openai_codex", None)
        assert codex_sdk.sdk_available() is False
        p = jb.get_provider_by_name("codex_sdk")
        assert isinstance(p, jb.DisabledJudge)
        rd = judge_policy.provider_readiness("codex_sdk")
        assert rd["ready"] is False
        assert rd["blocked_reason"] == "sdk_not_installed"

    def test_t3_full_chain_via_policy_provider_name(self, actors,
                                                    monkeypatch):
        """T3：fake transport 下政策 provider=codex_sdk 真名走通 start
        fresh 交付（不经 _INJECTED 注入名——fresh 构造点与 readiness
        同源）。"""
        seen_specs: list[dict] = []

        def transport(spec):
            seen_specs.append(spec)
            refs = [c["candidate_ref"] for c in
                    json.loads(spec["prompt"])["candidates"]]
            return {"status": "ok", "text": json.dumps({
                "items": [{"candidate_ref": r, "relevant": "relevant",
                           "support": "support",
                           "confidence": "high",
                           "reason": "测试判断"}
                          for r in refs]})}

        codex_sdk.set_transport_for_tests(transport)
        monkeypatch.setenv("MARIPOSA_CODEX_ALLOWED_DATA",
                           "event_excerpt,word_excerpt,source_excerpt")
        monkeypatch.setitem(sys.modules, "openai_codex",
                            _fake_sdk_module())
        pol = judge_policy.get_policy()
        judge_policy.update_policy(
            "qiaosheng", expected_revision=pol["revision"], enabled=True,
            provider="codex_sdk", idempotency_key="cw-on")
        _hold(actors, "接线测试的窗帘正文")
        r1 = recall_service.start(
            actors["jiaming"], {"query_plan": {
                "original_request": "窗帘", "channels": ["event"],
                "lexical_terms": ["窗帘"]}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:cw-t3",
                    "payload_hash": "cw-t3"})
        assert r1["judgement_status"] == "evaluated", \
            json.dumps(r1.get("coverage"), ensure_ascii=False)
        assert seen_specs, "判断请求真实发出（经政策真名构造）"

    def test_t4_live_shape_no_tmpdir_leak_and_timeout(self, actors,
                                                      monkeypatch):
        """T4（E05）：live 形态（fake SDK 模块走真 tempfile 路径）连续
        judge → tempfile 根下 codex-judge-* 零残留；fake 慢 transport
        超时 → unavailable。"""
        prefix = "codex-judge-"

        def _count_leak() -> int:
            import os
            return len([n for n in os.listdir(tempfile.gettempdir())
                        if n.startswith(prefix)])

        reply = json.dumps({"items": []})
        monkeypatch.setitem(sys.modules, "openai_codex",
                            _fake_sdk_module(reply))
        # RRA-018（回访）：live 形态测试显式开门——生产默认关（G-1
        # 她批准后才设 MARIPOSA_CODEX_LIVE_ENABLED=1）
        monkeypatch.setenv("MARIPOSA_CODEX_LIVE_ENABLED", "1")
        judge = codex_sdk.CodexSdkJudge(
            allowed_data=["event_excerpt", "word_excerpt",
                          "source_excerpt"])
        before = _count_leak()
        for _ in range(3):
            judge.judge({"original_request": "x", "lexical_terms": ["x"]},
                        [], {})
        assert _count_leak() == before, \
            "live 形态临时目录零残留（finally rmtree）"
        # 超时路径：fake 慢 transport（超过 CODEX_TIMEOUT_MS）——
        # RRA-018 回访：deadline 在调用**期间**执行（elapsed 有界），
        # 不再等 transport 自然跑完后才报 timeout
        old_ms = codex_sdk.CODEX_TIMEOUT_MS
        codex_sdk.CODEX_TIMEOUT_MS = 50
        try:
            def slow(spec):
                time.sleep(0.2)
                return {"status": "ok", "text": "{}"}

            codex_sdk.set_transport_for_tests(slow)
            judge2 = codex_sdk.CodexSdkJudge(
                allowed_data=["event_excerpt"])
            t0 = time.monotonic()
            out = judge2.judge({"original_request": "x",
                                "lexical_terms": ["x"]}, [], {})
            elapsed = time.monotonic() - t0
            assert out.provider_status == "unavailable"
            assert out.degraded_reason == "timeout"
            assert elapsed < 0.15, \
                f"deadline 必须期间执行（elapsed={elapsed:.3f}s；" \
                "回访残因=fake 跑完 126.8ms 后才报 timeout）"
        finally:
            codex_sdk.CODEX_TIMEOUT_MS = old_ms
            codex_sdk.set_transport_for_tests(None)


class TestGrantFiltering:
    def test_t5b_single_grant_excludes_unauthorized_canary(self, actors,
                                                            monkeypatch):
        """T5b（CX-15）：单许可（仅 event_excerpt）下，words 专项卡的
        正文 canary 不进 prompt（按卡必要角色过滤）；导航卡（必要角色
        空）不受影响。"""
        prompts: list[str] = []

        def transport(spec):
            prompts.append(spec["prompt"])
            refs = [c["candidate_ref"] for c in
                    json.loads(spec["prompt"])["candidates"]]
            return {"status": "ok", "text": json.dumps({
                "items": [{"candidate_ref": r, "relevant": "relevant",
                           "confidence": "high", "reason": "r"}
                          for r in refs]})}

        codex_sdk.set_transport_for_tests(transport)
        judge = codex_sdk.CodexSdkJudge(allowed_data=["event_excerpt"])
        event_card = {
            "candidate_ref": "memory:e1", "resource_ref": "memory:e1",
            "channel": "event", "content_version": "1",
            "evidence": [{"evidence_kind": "authored_event",
                          "field": "text",
                          "snippet": "事件卡canary正文"}]}
        words_card = {
            "candidate_ref": "our_word:w1", "resource_ref": "our_word:w1",
            "channel": "words", "content_version": "1",
            "matched_fields": ["our_words"],
            "excerpt": "词卡canary正文",
            "evidence": [{"evidence_kind": "word_excerpt",
                          "field": "our_words",
                          "snippet": "词卡canary正文"}]}
        out = judge.judge({"original_request": "x",
                           "lexical_terms": ["x"]},
                          [event_card, words_card], {})
        assert out.provider_status == "evaluated"
        assert prompts, "判断请求发出"
        assert "事件卡canary正文" in prompts[0], "已许可卡进 prompt"
        assert "词卡canary正文" not in prompts[0], \
            "未许可卡正文不得进 prompt（按必要角色过滤）"
        # 无一卡获许可（有候选）→ fail-closed 披露
        judge2 = codex_sdk.CodexSdkJudge(allowed_data=["word_excerpt"])
        out2 = judge2.judge({"original_request": "x",
                             "lexical_terms": ["x"]},
                            [event_card], {})
        assert out2.provider_status == "unavailable"
        assert out2.degraded_reason == "no_authorized_candidates"


class TestPolicyModelId:
    def test_t5c_policy_model_id_wins_over_env(self, actors, monkeypatch):
        """T5c（CX-03）：policy.model_id 注入构造器；env 与政策冲突时
        政策胜（政策唯一正本，env 仅首导/回落）。"""
        monkeypatch.setenv("MARIPOSA_CODEX_MODEL_ID", "env-model-x")
        monkeypatch.setitem(sys.modules, "openai_codex",
                            _fake_sdk_module())
        pol = judge_policy.get_policy()
        judge_policy.update_policy(
            "qiaosheng", expected_revision=pol["revision"], enabled=True,
            provider="codex_sdk", model_id="policy-model-y",
            idempotency_key="cw-t5c")
        eff = judge_policy.effective()
        assert eff["model_id"] == "policy-model-y"
        # 运行时构造点形态：构造 + 政策注入（政策胜出）
        p = jb.apply_policy_overrides(
            jb.get_provider_by_name("codex_sdk"),
            model_id=eff.get("model_id"),
            allowed_data=(eff.get("allowed_data") or None))
        assert isinstance(p, codex_sdk.CodexSdkJudge)
        assert p._model_id == "policy-model-y", "政策 model_id 胜出"
        # 未注入（env 回落语义保留）
        p2 = jb.get_provider_by_name("codex_sdk")
        assert p2._model_id == "env-model-x", "无政策值时回落 env"


class TestFieldGrantReconciliation:
    """RRA-024/025（2026-10-09 复审）：同卡字段级许可投影+对账集=实际
    送判集合。"""

    def test_024_same_card_unlicensed_fields_not_in_prompt(self):
        prompts = []

        def transport(spec):
            prompts.append(spec["prompt"])
            refs = [c["candidate_ref"] for c in
                    json.loads(spec["prompt"])["candidates"]]
            return {"status": "ok", "text": json.dumps({
                "items": [{"candidate_ref": r, "relevant": "relevant",
                           "confidence": "high", "reason": "r"}
                          for r in refs]})}

        codex_sdk.set_transport_for_tests(transport)
        # 单许可 event_excerpt：同卡含 event 正文 + word 证据 + 标题
        judge = codex_sdk.CodexSdkJudge(allowed_data=["event_excerpt"])
        card = {
            "candidate_ref": "memory:m1", "resource_ref": "memory:m1",
            "channel": "event", "content_version": "1",
            "_row": {"title": "DENIED_TITLE"},
            "evidence": [
                {"evidence_kind": "authored_event", "field": "text",
                 "snippet": "ALLOWED_EVENT"},
                {"evidence_kind": "word_excerpt", "field": "our_words",
                 "snippet": "DENIED_WORD"}]}
        out = judge.judge({"original_request": "x",
                           "lexical_terms": ["x"]}, [card], {})
        assert out.provider_status == "evaluated"
        p0 = prompts[0]
        assert "ALLOWED_EVENT" in p0
        assert "DENIED_TITLE" not in p0, "无 title_cue 许可的标题不外发"
        assert "DENIED_WORD" not in p0, "无 word_excerpt 许可的同卡证据不外发"

    def test_025_unsent_ref_rejected_not_evaluated(self):
        """回包返回未送判的 ref（被许可过滤）→ 陌生 ref 整批作废。
        RRA-025（回访 2026-10-09）：固化**原始反例**——原候选含被
        许可过滤的 words 卡（过滤前集合成员），回包同时返回已送判 ref
        与该被过滤 ref → 仍整批作废（此前 sent_refs 取过滤前全集，未
        送判 ref 被伪标 evaluated）。"""

        def transport(spec):
            refs = [c["candidate_ref"] for c in
                    json.loads(spec["prompt"])["candidates"]]
            # 已送判 ref 正常回 + 被过滤 ref（不在送判集）一并回
            return {"status": "ok", "text": json.dumps({
                "items": [
                    {"candidate_ref": refs[0], "relevant": "relevant",
                     "confidence": "high", "reason": "r"},
                    {"candidate_ref": "our_word:filtered",
                     "relevant": "relevant", "confidence": "high",
                     "reason": "r"}]})}

        codex_sdk.set_transport_for_tests(transport)
        judge = codex_sdk.CodexSdkJudge(
            allowed_data=["event_excerpt"])
        sent_card = {
            "candidate_ref": "memory:m1", "resource_ref": "memory:m1",
            "channel": "event", "content_version": "1",
            "evidence": [{"evidence_kind": "authored_event",
                          "field": "text", "snippet": "body"}]}
        hidden = {
            "candidate_ref": "our_word:filtered",
            "resource_ref": "our_word:filtered", "channel": "words",
            "content_version": "1", "evidence": []}
        out = judge.judge({"original_request": "x",
                           "lexical_terms": ["x"]},
                          [sent_card, hidden], {})
        assert out.provider_status == "unavailable", \
            "被过滤卡的 ref 出现在回包=陌生 ref 整批作废（不得伪标 evaluated）"


class TestRRAFollowUpLiveGateAndProductionKinds:
    """RRA-018/024（回访 2026-10-09 run-035424）：live 安全门可执行 +
    生产证据词表字段许可映射。"""

    def test_018_live_gate_blocks_ready_and_judge_before_boundary(
            self, actors, monkeypatch):
        """合成 key/model/grants + SDK 在场（fake 模块）+ 门未开：
        readiness=live_gate_closed（不 ready）；直构 provider judge 在
        到达调用边界前拒绝（可执行门，不是注释声明）。"""
        monkeypatch.setenv("MARIPOSA_CODEX_API_KEY", "SYNTHETIC_NOT_REAL")
        monkeypatch.setenv("MARIPOSA_CODEX_MODEL_ID", "synthetic-model")
        monkeypatch.setenv("MARIPOSA_CODEX_ALLOWED_DATA", "event_excerpt")
        monkeypatch.delenv("MARIPOSA_CODEX_LIVE_ENABLED", raising=False)
        monkeypatch.setitem(sys.modules, "openai_codex",
                            _fake_sdk_module())
        assert codex_sdk.sdk_available() is True
        rd = codex_sdk.readiness()
        assert rd["ready"] is False
        assert rd["blocked_reason"] == "live_gate_closed", \
            "合成配置齐备也必须被可执行 live 门拦住"
        # 直构 provider：judge 在 _call_model 之前拒绝（live 边界不可达）
        p = jb.get_provider_by_name("codex_sdk")
        assert isinstance(p, codex_sdk.CodexSdkJudge)
        out = p.judge({"original_request": "x", "lexical_terms": ["x"]},
                      [], {})
        assert out.provider_status == "unavailable"
        assert out.degraded_reason == "codex_live_gate_closed"
        # 门开（她批准后的开关形态）→ readiness ready、judge 可达边界
        monkeypatch.setenv("MARIPOSA_CODEX_LIVE_ENABLED", "1")
        rd2 = codex_sdk.readiness()
        assert rd2["ready"] is True, rd2

    def test_018_lock_wait_bounded_by_deadline(self, monkeypatch):
        """锁被占时等待 ≤ 剩余 deadline（此前 max(0.1, ms/1000) 地板使
        20ms 配置实际等 ~105ms）→ codex_lock_timeout。"""
        import threading
        old_ms = codex_sdk.CODEX_TIMEOUT_MS
        codex_sdk.CODEX_TIMEOUT_MS = 40
        try:
            codex_sdk.set_transport_for_tests(
                lambda spec: {"status": "ok", "text": '{"items": []}'})
            codex_sdk._CODEX_LOCK.acquire()
            holder = threading.Thread(
                target=lambda: (time.sleep(0.3),
                                codex_sdk._CODEX_LOCK.release()))
            holder.start()
            judge = codex_sdk.CodexSdkJudge(
                allowed_data=["event_excerpt"])
            t0 = time.monotonic()
            out = judge.judge({"original_request": "x",
                               "lexical_terms": ["x"]}, [], {})
            elapsed = time.monotonic() - t0
            holder.join()
            assert out.degraded_reason == "codex_lock_timeout"
            assert elapsed < 0.12, \
                f"锁等待必须按剩余 deadline 上界（elapsed={elapsed:.3f}s）"
        finally:
            codex_sdk.CODEX_TIMEOUT_MS = old_ms
            codex_sdk.set_transport_for_tests(None)

    def test_024_production_word_kinds_field_matrix(self):
        """生产词表（word_verbatim/word_paraphrase/word_unverified）：
        单 event_excerpt 许可下同卡话语不外发（此前三 kind 均漏映射
        恒放行）；补 word_excerpt 许可后可发；未知 kind 带正文 fail-closed。"""
        prompts = []

        def transport(spec):
            prompts.append(spec["prompt"])
            refs = [c["candidate_ref"] for c in
                    json.loads(spec["prompt"])["candidates"]]
            return {"status": "ok", "text": json.dumps({
                "items": [{"candidate_ref": r, "relevant": "relevant",
                           "confidence": "high", "reason": "r"}
                          for r in refs]})}

        codex_sdk.set_transport_for_tests(transport)

        def run(kind, grants):
            prompts.clear()
            card = {
                "candidate_ref": "memory:audit", "resource_ref":
                    "memory:audit", "channel": "event",
                "content_version": "1", "_row": {"title": "DENIED_TITLE"},
                "evidence": [
                    {"evidence_kind": "authored_event", "field":
                     "event_text", "snippet": "ALLOWED_EVENT"},
                    {"evidence_kind": kind, "field": "our_words.text",
                     "snippet": f"DENIED_WORD_{kind}"}]}
            res = codex_sdk.CodexSdkJudge(
                allowed_data=grants).judge(
                {"original_request": "x", "lexical_terms": ["x"]},
                [card], {})
            body = prompts[0] if prompts else ""
            return {"event_sent": "ALLOWED_EVENT" in body,
                    "word_sent": f"DENIED_WORD_{kind}" in body,
                    "title_sent": "DENIED_TITLE" in body,
                    "status": res.provider_status}

        for kind in ("word_verbatim", "word_paraphrase",
                     "word_unverified"):
            r = run(kind, ["event_excerpt"])
            assert r["status"] == "evaluated"
            assert r["event_sent"] is True, f"{kind}：获准事件照发"
            assert r["word_sent"] is False, \
                f"{kind}：生产话语 kind 未许可不得外发（漏映射残因）"
            assert r["title_sent"] is False
        ctl = run("word_verbatim",
                  ["event_excerpt", "word_excerpt", "title_cue"])
        assert ctl["word_sent"] is True and ctl["title_sent"] is True, \
            "许可齐备时话语/标题照发（对照）"
        # 未知 kind 带正文 → fail-closed（不猜角色）
        unknown = run("mystery_kind", ["event_excerpt"])
        assert unknown["word_sent"] is False
