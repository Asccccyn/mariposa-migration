"""JudgeProvider 契约与故障注入（JEV-01..10 / HYBRID-05 / §6.5）。

用注入的假 provider 验证协议：Noul 无 confidence、缺项是 unknown 不是
低分、超时降级、低分只进 deferred、payload 白名单（event 精排输入不含
标题/心情文字/our_words/raw）。真实 provider 默认 disabled 不联网。
"""
from __future__ import annotations

import pytest

from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from mariposa.retrieval.judges import base as jb
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj")}


def hold(actors, text, date, **kw):
    base = dict(text=text, memory_date=date, date_confidence="exact",
                original_title=kw.pop("title", "t"),
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


class RecordingJudge(jb.JudgeProvider):
    name = "recording_test"

    def __init__(self, items_factory=None, status="evaluated",
                 reason=None):
        self.calls: list[dict] = []
        self._factory = items_factory
        self._status = status
        self._reason = reason

    def judge(self, query_plan, candidates, execution_context):
        self.calls.append({"plan": query_plan, "candidates": candidates,
                           "ctx": execution_context})
        items = (self._factory(candidates) if self._factory else [
            jb.JudgeItem(candidate_ref=c["candidate_ref"],
                         candidate_version=c.get("content_version"),
                         relevance_signal=0.1,
                         evaluation_status="evaluated")
            for c in candidates])
        return jb.JudgeBatchResult(items=items,
                                   provider_status=self._status,
                                   degraded_reason=self._reason)


@pytest.fixture(autouse=True)
def cleanup_judge():
    yield
    jb.clear_injected()
    from mariposa import config
    config.RECALL_JUDGE_PROVIDER = "disabled"


def enable(provider_name):
    from mariposa import config
    config.RECALL_JUDGE_PROVIDER = provider_name


def start_with_judge(actors, judge, **plan_kw):
    enable(judge.name)
    jb.register_for_tests(judge.name, judge)
    plan = {"original_request": "找搬家的事", "channels": ["event"],
            "lexical_terms": ["搬家"]}
    plan.update(plan_kw)
    return recall_service.start(actors["jiaming"], {"query_plan": plan})


class TestJudgeContract:
    def test_jev01_noul_no_confidence(self):
        """JEV-01：Noul 映射 noul，confidence=null/not_applicable。"""
        item = jb.JudgeItem(candidate_ref="c1", candidate_version="1",
                            relevance_signal=0.8,
                            confidence_kind="not_applicable")
        d = item.to_dict()
        assert d["provider_confidence"] is None
        assert d["confidence_kind"] == "not_applicable"

    def test_jev03_invalid_signals_are_unknown(self):
        """JEV-03：NaN/越界/非数值 → None（unknown），不当低分淘汰。"""
        assert jb.sanitize_signal(float("nan")) is None
        assert jb.sanitize_signal(1.5) is None
        assert jb.sanitize_signal("high") is None
        assert jb.sanitize_signal(0.7) == 0.7

    def test_jev05_disabled_provider_no_network(self, actors):
        """JEV-05：judge 关闭时完整可用，不联网不抛错。"""
        hold(actors, "搬家事件", "2026-08-10")
        packet = start_with_judge(actors, RecordingJudge())
        # 重新按 disabled 跑
        from mariposa import config
        config.RECALL_JUDGE_PROVIDER = "disabled"
        packet = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "找搬家", "channels": ["event"],
            "lexical_terms": ["搬家"]}})
        assert packet["coverage"]["judge"] == "not_configured"
        # S10（recall-closure）：judge 关闭时搜索候选正文不直出
        assert packet["candidates"] == []
        assert any("不直出" in m or "Jev" in m for m in packet["missing"])

    def test_jev04_timeout_batch_degrades(self, actors):
        """JEV-04：provider 不可用 → 有界降级，未评估候选不标 evaluated。"""
        hold(actors, "搬家事件", "2026-08-10")
        judge = RecordingJudge(
            items_factory=lambda cs: [
                jb.JudgeItem(candidate_ref=c["candidate_ref"],
                             candidate_version=c.get("content_version"),
                             evaluation_status="unavailable")
                for c in cs],
            status="unavailable", reason="timeout")
        packet = start_with_judge(actors, judge)
        assert packet["coverage"]["judge"] == "unavailable"
        assert "judge_timeout" in packet["degraded_reasons"]

    def test_jev10_low_score_deferred_not_global_rejected(self, actors):
        """JEV-10：低分候选进 deferred 集合，可被新 revision 重评。"""
        hold(actors, "搬家事件甲", "2026-08-10")
        judge = RecordingJudge(
            items_factory=lambda cs: [
                jb.JudgeItem(candidate_ref=c["candidate_ref"],
                             candidate_version=c.get("content_version"),
                             relevance_signal=-0.9,
                             evaluation_status="evaluated")
                for c in cs])
        packet = start_with_judge(actors, judge)
        sid = packet["recall_session_id"]
        states = {c["candidate_ref"]: c["state"] for c in
                  recall_service.status(actors["jiaming"],
                                        {"session_id": sid})["candidates"]}
        # 低分 ≠ 用户拒绝：状态是 seen/deferred，不是 rejected
        assert all(v in ("seen", "deferred") for v in states.values())

    def test_hybrid05_judge_payload_whitelist(self, actors):
        """HYBRID-05：event 精排输入不含标题/心情文字/our_words/raw。"""
        hold(actors, "搬家事件甲", "2026-08-10", title="独有标题探针",
             mood={"text": "独有心情文字探针", "tags": ["愉悦"]},
             our_words=[{"speaker": "qiaosheng",
                         "text": "独有话语探针"}])
        judge = RecordingJudge()
        start_with_judge(actors, judge)
        # r2 S09：出站白名单以真实 typesafe 投影为准——卡内 _row 是
        # 内部数据（不外发），标题/心情/未参与匹配的话语不得进入
        # 投影 segments
        from mariposa.retrieval.judges import typesafe_jev
        inner = typesafe_jev.TypeSafeJevJudge()
        inner._data_profile = frozenset(
            {"event_excerpt", "title_cue", "word_excerpt",
             "source_excerpt"})
        call0 = judge.calls[0]
        inner._current_terms = call0["plan"].get("lexical_terms") or []
        blob = str(inner._payload(call0["plan"], call0["candidates"]))
        assert "独有标题探针" not in blob
        assert "独有心情文字探针" not in blob
        assert "独有话语探针" not in blob

    def test_jev06_hard_gates_by_code(self, actors):
        """JEV-06：日期/版本门控由代码执行；judge 高分不覆盖硬条件。"""
        hold(actors, "九月搬家事件高分诱饵", "2026-09-10")
        hold(actors, "八月搬家事件", "2026-08-10")
        judge = RecordingJudge(
            items_factory=lambda cs: [
                jb.JudgeItem(
                    candidate_ref=c["candidate_ref"],
                    candidate_version=c.get("content_version"),
                    # 九月诱饵打高分
                    relevance_signal=(0.9 if "九月" in str(
                        c.get("excerpt", "")) else 0.2),
                    evaluation_status="evaluated")
                for c in cs])
        packet = start_with_judge(
            actors, judge,
            explicit_constraints={"event_date": {"from": "2026-08-01",
                                                 "to": "2026-08-31"}})
        for c in packet["candidates"]:
            assert not (c.get("memory_date") or "").startswith("2026-09")

    def test_jev02_zero_delivery_allowed(self):
        """JEV-02：全部不相关时可返回 0 条，不强迫唯一 winner。"""
        from mariposa.retrieval import selection
        out = selection.select([], {"delivery_limit": 3}, set())
        assert out["delivered"] == []
        assert out["delivery_action"] == "no_candidates"
