"""WP-01（run-160858）：召回分页/政策纪元/预算/重放统一坐标系回归。

- T1/T2/T3（JFA-007）：off 分页页间失效不静默丢卡——装配吃冻结全集
  +原序失效标记（delivered ∪ invalidated == 冻结全集，candidate_total
  恒冻结值）。
- T4（A03）：政策纪元=mode+revision——同 mode 下 provider 变更
  （revision 前进）续页 RECALL_POLICY_CHANGED；同 revision 同页稳定（P05）。
- T5（A04）：翻页中通道关闭（words）→ 对应前缀卡剔除披露。
- T6（A02）：outbound_grants 空集=零许可（fail-closed）——readiness
  就绪但未声明许可面的 provider，replay 旧包 suppress（allowed_data_empty）。
- T7（CX-01）：多载体超限卡逐载体续取——第二载体不再静默丢失。
- T8（CX-06）：最终信封预算门——装页骨架=最终出站 packet 形状，
  出口信封实测 ≤24576。
- T9（CX-02）：off 同模式重放返回原候选（不清空不 degraded）。
- T10（CX-17）：judge 在途切政策 → start 提交 RECALL_POLICY_CHANGED。
"""
from __future__ import annotations

import json

import pytest

from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy, paging, service as recall_service
from mariposa.recall import store as recall_store
from mariposa.retrieval.judges import base as jb
from mariposa.errors import Forbidden
from tests.conftest import reset_all


def _actor(pid="jiaming"):
    return identity.Principal(pid, "n", "agent", "claude_chat", "bj")


def _owner():
    return identity.Principal("qiaosheng", "江乔生", "human", "web", "bq")


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": _actor(), "qiaosheng": _owner()}


def hold(actors, text, date="2026-09-01", title="t"):
    return memory.hold(
        actors["jiaming"], text=text, memory_date=date,
        date_confidence="exact", original_title=title,
        categories=["daily"], creation_mode="contemporaneous",
        raw_pending=False)["memory_id"]


def set_policy(enabled, provider=None, key=None, expected=None):
    if expected is None:
        expected = judge_policy.get_policy()["revision"]
    return judge_policy.update_policy(
        "qiaosheng", expected_revision=expected, enabled=enabled,
        provider=provider, idempotency_key=key)


def start(actors, terms, op, ref=None):
    """经 transport 实际入口（registry.invoke）——request_ref 幂等键、
    replay_guard、信封包装与生产同路径（plan 必带 request_ref）。"""
    from mariposa.capabilities import registry
    plan = {"original_request": f"找{terms[0]}",
            "channels": ["event"], "lexical_terms": terms,
            "request_ref": ref or op}
    return registry.invoke(actors["jiaming"], "memory.recall.start",
                           {"query_plan": plan}, None)["data"]


def walk_all(principal, packet, expect_pages=500):
    """翻尽冻结集：返回 (entries, invalidated 全集, 页数)。"""
    entries = list(packet.get("candidates") or [])
    inv = list(packet.get("invalidated") or [])
    pages = 1
    cursor = (packet.get("pagination") or {}).get("next_cursor")
    seen = set()
    while cursor:
        assert cursor not in seen, "游标形成环"
        seen.add(cursor)
        page = paging.serve_page(principal, {
            "result_set_id": packet["pagination"]["result_set_id"],
            "session_id": packet["recall_session_id"],
            "cursor": cursor})
        entries.extend(page.get("candidates") or [])
        inv.extend(page.get("invalidated") or [])
        pages += 1
        assert pages < expect_pages, "分页不收敛"
        cursor = page["pagination"].get("next_cursor")
    return entries, inv, pages


def _delivered_refs(entries):
    return {c.get("resource_ref") for c in entries
            if c.get("resource_ref") and not c.get("invalid")}


def _inv_refs(inv):
    return {i["resource_ref"] for i in inv}


class RecordingJudge(jb.JudgeProvider):
    name = "recording_test"

    def __init__(self, status="evaluated", on_judge=None):
        self.calls = []
        self._status = status
        self._on_judge = on_judge

    def judge(self, query_plan, candidates, execution_context):
        self.calls.append({"plan": query_plan, "candidates": candidates})
        if self._on_judge:
            self._on_judge()
        return jb.JudgeBatchResult(
            items=[jb.JudgeItem(
                candidate_ref=c["candidate_ref"],
                candidate_version=c.get("content_version"),
                relevance_signal=0.5,
                evaluation_status=self._status)
                for c in candidates],
            provider_status=self._status)

    def outbound_grants(self):
        return frozenset({"event_excerpt", "word_excerpt",
                          "source_excerpt"})


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    jb.clear_injected()


# ------------------------------------------------------- JFA-007 冻结完整性

class TestFreezeIntegrity:
    def test_t1_delivered_union_invalidated_equals_frozen(self, actors):
        """T1：15 候选/页 1 交付 10/页间修订已交付索引 2 的卡/翻尽——
        delivered ∪ invalidated == 冻结全集 且 candidate_total 恒 15。"""
        held = [hold(actors, f"南瓜灯事件编号{i:03d}号记录") for i in range(15)]
        set_policy(False)
        p1 = start(actors, ["南瓜灯"], "op-t1")
        assert len(p1["candidates"]) == 10, "首页恰 10 条目"
        assert p1["pagination"]["candidate_total"] == 15
        # 页间修订已交付卡（索引 2）→ content_version 前进 → 失效
        from mariposa.capabilities import registry
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": held[2], "expected_version": 1,
            "text": "南瓜灯事件编号002号记录（已修订）"}, None)
        entries, inv, pages = walk_all(actors["jiaming"], p1)
        refs = _delivered_refs(entries) | _inv_refs(inv)
        frozen = {f"memory:{m}" for m in held}
        assert refs == frozen, \
            f"并集必须等于冻结全集：缺 {frozen - refs}，多 {refs - frozen}"
        assert f"memory:{held[2]}" in _inv_refs(inv), "已修订卡进 invalidated"
        # 页 1 在修订前已交付 00003（历史交付事实保留）；修订后翻页
        # 对其披露失效——delivered=15、invalidated 含 00003、并集=15
        assert len(_delivered_refs(entries)) == 15, "15 卡全部真实交付过"
        for e in entries:
            if e.get("resource_ref"):
                assert not e.get("invalid"), "有效卡不得标 invalid"

    def test_t2_invalidation_after_cursor_disclosed(self, actors):
        """T2：失效发生在游标之后（未交付卡）→ 进 invalidated、其余全交付。"""
        held = [hold(actors, f"南瓜灯事件编号{i:03d}号记录") for i in range(15)]
        set_policy(False)
        p1 = start(actors, ["南瓜灯"], "op-t2")
        assert p1["pagination"]["has_more"] is True
        # 页间修订未交付卡（索引 12 > 游标 10）
        from mariposa.capabilities import registry
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": held[12], "expected_version": 1,
            "text": "南瓜灯事件编号012号记录（已修订）"}, None)
        entries, inv, _ = walk_all(actors["jiaming"], p1)
        assert f"memory:{held[12]}" in _inv_refs(inv), \
            "游标后失效的未交付卡必须进 invalidated（不得静默跳过）"
        assert len(_delivered_refs(entries)) == 14
        frozen = {f"memory:{m}" for m in held}
        assert _delivered_refs(entries) | _inv_refs(inv) == frozen

    def test_t3_tail_mass_invalidation_no_swallow(self, actors):
        """T3：尾部多卡失效——不得出现空页活锁，也不得把未交付未失效卡
        吞进 has_more=false。"""
        held = [hold(actors, f"南瓜灯事件编号{i:03d}号记录") for i in range(15)]
        set_policy(False)
        p1 = start(actors, ["南瓜灯"], "op-t3")
        from mariposa.capabilities import registry
        for i in (11, 12, 13, 14):  # 尾部 4 卡失效（页1 已交付 0-9）
            registry.invoke(actors["jiaming"], "memory.update", {
                "memory_id": held[i], "expected_version": 1,
                "text": f"南瓜灯事件编号{i:03d}号记录（已修订）"}, None)
        entries, inv, pages = walk_all(actors["jiaming"], p1)
        # 卡 10 未失效未交付——必须交付（不得被尾部失效吞掉）
        assert f"memory:{held[10]}" in _delivered_refs(entries), \
            "尾部多卡失效不得吞未失效卡"
        assert _delivered_refs(entries) | _inv_refs(inv) == \
            {f"memory:{m}" for m in held}
        assert pages <= 3, "尾部失效不应翻出额外空页（游标按冻结坐标前进）"

    def test_t1_candidate_total_constant_across_pages(self, actors):
        """candidate_total 恒冻结值：翻页各页与首页一致，失效不收缩。"""
        held = [hold(actors, f"南瓜灯事件编号{i:03d}号记录") for i in range(15)]
        set_policy(False)
        p1 = start(actors, ["南瓜灯"], "op-t1b")
        from mariposa.capabilities import registry
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": held[2], "expected_version": 1,
            "text": "南瓜灯事件编号002号记录（已修订）"}, None)
        cursor = p1["pagination"]["next_cursor"]
        totals = [p1["pagination"]["candidate_total"]]
        while cursor:
            page = paging.serve_page(actors["jiaming"], {
                "result_set_id": p1["pagination"]["result_set_id"],
                "session_id": p1["recall_session_id"], "cursor": cursor})
            totals.append(page["pagination"]["candidate_total"])
            cursor = page["pagination"].get("next_cursor")
        assert totals == [15] * len(totals), \
            f"candidate_total 不得随失效收缩：{totals}"


# ------------------------------------------------------- A03 政策纪元

class TestPolicyEpoch:
    def test_t4_revision_advance_same_mode_rejects_page(self, actors):
        """T4：政策 revision 前进而 mode 不变（off 换 provider 保留）→
        旧结果集续页 RECALL_POLICY_CHANGED；同 revision 同页稳定（P05）。"""
        for i in range(15):
            hold(actors, f"南瓜灯事件编号{i:03d}号记录")
        set_policy(False)
        p1 = start(actors, ["南瓜灯"], "op-t4")
        rsid = p1["pagination"]["result_set_id"]
        cursor = p1["pagination"]["next_cursor"]
        # 同 revision 对照：同游标两读字节级一致（P05）
        page_a = paging.serve_page(actors["jiaming"], {
            "result_set_id": rsid, "session_id": p1["recall_session_id"],
            "cursor": cursor})
        page_b = paging.serve_page(actors["jiaming"], {
            "result_set_id": rsid, "session_id": p1["recall_session_id"],
            "cursor": cursor})
        assert json.dumps(page_a, sort_keys=True) == \
            json.dumps(page_b, sort_keys=True), "同 revision 同页必须稳定"
        # 纪元前进：off(provider=None) → off(provider=typesafe_jev)
        set_policy(False, provider="typesafe_jev", key="t4-provider")
        with pytest.raises(Forbidden) as ei:
            paging.serve_page(actors["jiaming"], {
                "result_set_id": rsid,
                "session_id": p1["recall_session_id"], "cursor": cursor})
        assert ei.value.code == "RECALL_POLICY_CHANGED"
        assert ei.value.detail["set_mode"] == "off"
        assert ei.value.detail["current_mode"] == "off", "mode 未变"
        assert ei.value.detail["current_policy_revision"] > \
            ei.value.detail["set_policy_revision"], "revision 前进"


# ------------------------------------------------------- A04 通道开关

class TestChannelSwitch:
    def test_t5_words_disabled_mid_paging_drops_and_discloses(self, actors):
        """T5：off 冻结集含 our_word 卡；翻页中 RECALL_WORDS_ENABLED=0 →
        续页剔除并披露（channel_words_disabled），不再出站。"""
        from mariposa import config
        mids = [hold(actors, f"南瓜灯事件编号{i:03d}号记录") for i in range(12)]
        set_policy(False)
        p1 = start(actors, ["南瓜灯"], "op-t5")
        rsid = p1["pagination"]["result_set_id"]
        # 直接往冻结集补两张 our_word 卡（服务级夹具：A04 测翻页重校验
        # 层的字前缀判定，不依赖 words 检索形态）
        word_refs = ["our_word:w-t5-1", "our_word:w-t5-2"]
        pset = paging.load_set(rsid)
        cards = pset["candidates"] + [
            {"resource_ref": r, "channel": "words", "excerpt": "我们的话",
             "content_version": "1", "matched_fields": ["our_words"]}
            for r in word_refs]
        with recall_store.db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE recall_page_sets SET candidates_json=?,"
                " candidate_total=? WHERE result_set_id=?",
                (json.dumps(cards, ensure_ascii=False), len(cards), rsid))
            conn.execute("COMMIT")
        # 翻页中关 words 通道
        old = config.RECALL_WORDS_ENABLED
        config.RECALL_WORDS_ENABLED = False
        try:
            entries, inv, _ = walk_all(actors["jiaming"], p1)
        finally:
            config.RECALL_WORDS_ENABLED = old
        assert set(word_refs) <= _inv_refs(inv), "our_word 卡必须披露失效"
        inv_reasons = {i["resource_ref"]: i["reason"] for i in inv}
        assert inv_reasons[word_refs[0]] == "channel_words_disabled"
        assert not (set(word_refs) & _delivered_refs(entries)), \
            "关闭通道的卡不得出站"
        frozen = {f"memory:{m}" for m in mids} | set(word_refs)
        assert _delivered_refs(entries) | _inv_refs(inv) == frozen, \
            "通道关闭也是失效披露，并集仍等于冻结全集"


# ------------------------------------------------------- A02 许可空集

class TestEmptyGrantsFailClosed:
    def test_t6_empty_grants_ready_provider_replay_suppressed(self, actors):
        """T6：readiness 就绪但 outbound_grants 空集的 provider →
        replay 旧包 suppressed（空集=零许可，base 契约 fail-closed）。"""
        m = hold(actors, "南瓜灯窗帘正文事件")
        set_policy(True, provider="test_deterministic")
        r1 = start(actors, ["南瓜灯"], "op-t6", ref="t6-alpha")
        assert r1["candidates"], "前置：全许可面 provider 有正文交付"
        # 同名换空 grants 实例（政策不动、revision 不变——模拟未来
        # provider 未覆写 outbound_grants 的接入形态）
        class EmptyGrantsJudge(RecordingJudge):
            name = "test_deterministic"

            def outbound_grants(self):
                return frozenset()

        jb.register_for_tests("test_deterministic", EmptyGrantsJudge())
        r2 = start(actors, ["南瓜灯"], "op-t6", ref="t6-alpha")
        assert r2["candidates"] == [], "空许可面重放不得释放正文"
        assert "allowed_data_empty" in (r2.get("degraded_reasons") or []), \
            "必须结构化标注空许可原因"
        assert r2.get("coverage", {}).get("judge") == "unavailable"


# ------------------------------------------------------- CX-01 多载体

class TestMultiCarrier:
    def test_t7_dual_carrier_canaries_all_served(self, actors):
        """T7：双载体超限卡（两段长正文）→ 翻尽后两载体 canary 都在
        出站拼回中出现，has_more 结态如实。"""
        set_policy(False)
        canary_a = "甲" * 10000
        canary_b = "乙" * 10000
        card = {
            "resource_ref": "memory:cx01-mem", "channel": "event",
            "content_version": "1", "matched_fields": ["text"],
            "evidence": [
                {"evidence_kind": "authored_event", "field": "text",
                 "snippet": canary_a},
                {"evidence_kind": "word_excerpt", "field": "our_words",
                 "snippet": canary_b},
            ],
        }
        # 服务级直调装配器（夹具不依赖检索产卡形态）
        extra = {"result_set_id": "rps_fixture_cx01",
                 "recall_session_id": "rs_fixture", "revision": 1,
                 "judge_mode": "off"}
        pos = (0, 0, 0)
        frags = []
        pages = 0
        while True:
            page = paging.assemble_page([card], pos, extra)
            pages += 1
            frags.extend(page["candidates"])
            np_ = page["pagination"]["next_position"]
            if not np_:
                assert page["pagination"]["has_more"] is False
                break
            pos = tuple(np_)
            assert pages < 500, "载体续取不收敛"
        got_a = "".join(
            f["fragment"]["text"] for f in frags
            if f.get("fragment", {}).get("evidence_index") == 0)
        got_b = "".join(
            f["fragment"]["text"] for f in frags
            if f.get("fragment", {}).get("evidence_index") == 1)
        assert got_a == canary_a, "第一载体（evidence 0）拼回完整"
        assert got_b == canary_b, "第二载体（evidence 1）拼回完整（不再静默丢失）"


# ------------------------------------------------------- CX-06 最终信封

class TestFinalEnvelopeBudget:
    @pytest.mark.parametrize("size", [11200, 11400, 11600])
    def test_t8_final_envelope_within_budget(self, actors, size):
        """T8：临界输入（Codex budget probe 尺寸）——start 首页与全部
        续页的最终出站信封实测 ≤24576B（骨架=出口同源）。"""
        hold(actors, "临界预算正文" + "预算" * (size // 2))
        set_policy(False)
        p1 = start(actors, ["临界"], "op-t8")
        blob = json.dumps({"ok": True, "data": p1},
                          ensure_ascii=False).encode("utf-8")
        assert len(blob) <= 24576, \
            f"首页信封 {len(blob)}B 超限（输入 {size} 字）"
        # 翻尽每页同样实测
        cursor = p1["pagination"].get("next_cursor")
        while cursor:
            page = paging.serve_page(actors["jiaming"], {
                "result_set_id": p1["pagination"]["result_set_id"],
                "session_id": p1["recall_session_id"], "cursor": cursor})
            blob = json.dumps({"ok": True, "data": page},
                              ensure_ascii=False).encode("utf-8")
            assert len(blob) <= 24576, f"续页信封 {len(blob)}B 超限"
            cursor = page["pagination"].get("next_cursor")

    def test_t8_long_input_still_fragments(self, actors):
        """T8 对照：22000+ 长文仍按码点分片（不因骨架前置回归）。"""
        hold(actors, "长文分片对照" + "长文" * 11000)
        set_policy(False)
        p1 = start(actors, ["长文"], "op-t8c")
        entries, _, _ = walk_all(actors["jiaming"], p1)
        frags = [e for e in entries if e.get("fragment")]
        assert frags, "22000+ 字长文必须产生显式分片"
        text = "".join(f["fragment"]["text"] for f in frags)
        assert len(text) > 20000, "拼回完整"


# ------------------------------------------------------- CX-02 off 重放

class TestOffReplayIdentity:
    def test_t9_off_same_mode_replay_returns_original(self, actors):
        """T9：off start 1 候选 → 同 request_ref 网络重试 → 返回同
        1 候选（非 0+degraded——off 零 provider，重放即原结果）。"""
        hold(actors, "off 重放窗帘正文")
        set_policy(False)
        r1 = start(actors, ["窗帘"], "op-t9", ref="t9-alpha")
        assert len(r1["candidates"]) == 1
        assert r1["judge_mode"] == "off"
        r2 = start(actors, ["窗帘"], "op-t9", ref="t9-alpha")
        assert len(r2["candidates"]) == 1, "off 同模式重放返回原候选"
        assert r2["recall_session_id"] == r1["recall_session_id"]
        assert not (r2.get("degraded_reasons") or []), \
            "off 重放不得标 judge 降级（off 本就零判断）"


# ------------------------------------------------------- CX-17 in-flight

class TestInFlightPolicyFencing:
    def test_t10_policy_switch_during_judge_rejects_commit(self, actors):
        """T10：judge 在途切政策 → start 最终提交 RECALL_POLICY_CHANGED
        （judge 在途取消尽力中止照 §4.1）；同纪元正常提交对照。"""
        hold(actors, "在途切换窗帘事件")
        from mariposa.recall import judge_policy as _jp

        def _switch_mid_judge():
            pol = _jp.get_policy()
            _jp.update_policy(
                "qiaosheng", expected_revision=pol["revision"],
                enabled=False, provider=pol["provider"],
                idempotency_key="t10-mid")

        jb.register_for_tests("test_deterministic",
                              RecordingJudge(on_judge=_switch_mid_judge))
        set_policy(True, provider="test_deterministic")
        with pytest.raises(Forbidden) as ei:
            start(actors, ["窗帘"], "op-t10a")
        assert ei.value.code == "RECALL_POLICY_CHANGED"
        assert ei.value.detail["current_mode"] == "off"
        # 同纪元对照：judge 正常返回、政策不动 → 提交成功
        jb.register_for_tests("test_deterministic", RecordingJudge())
        pol = judge_policy.get_policy()
        set_policy(True, provider="test_deterministic", key="t10-back",
                   expected=pol["revision"])
        r = start(actors, ["窗帘"], "op-t10b")
        assert r["judge_mode"] == "on"
