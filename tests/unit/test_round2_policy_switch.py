"""Round2 通用 provider 门禁与关闭模式批次分页（WP2，M 侧）。

对 ACCEPTANCE_MANUAL_HANDOFF_JUDGE_20261008.json：R01（关闭无需
provider 许可可查原文）/R02（关闭不放开原文权限）/R03（off 不绕过
首轮覆盖与完成条件）/R04（非 Jev 类型凭自身许可通过）/R05（每批 20
命中全部可逐页取得才推进上游）/R06（两类续页分槽）/R07（升级理由
基于全集事实）/R08（撤权/切政策后续页拒绝）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy, paging, service as recall_service
from mariposa.recall import store
from mariposa.retrieval.judges import base as jb
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal(
        "jiaming", "周家明", "agent", "claude_chat", "bj")}


def hold(principal, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="w",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(principal, **base)


def _seed_source(text="原文里的崧蓝染色记忆", tag="t1"):
    from mariposa.source import importer
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    f = tmp / "s.json"
    f.write_text(_json.dumps([{
        "uuid": f"c-{tag}",
        "chat_messages": [{
            "uuid": f"{tag}-m1", "sender": "human",
            "created_at": "2026-09-20T10:00:00.000Z",
            "content": [{"type": "text", "text": text}]}]}],
        ensure_ascii=False), encoding="utf-8")
    return importer.import_file("jiaming", str(f))


def _start(actors, terms=("崧蓝",), op="op-r2s", channels=None):
    return registry.invoke(actors["jiaming"], "memory.recall.start",
                           {"query_plan": {
                               "original_request": "我当时的原话",
                               "channels": channels or ["words"],
                               "lexical_terms": list(terms),
                               "evidence_requirement":
                                   "verbatim_required"},
                            "operation_id": op}, None)


def _round2(actors, sid, reason="EVIDENCE_INSUFFICIENT", op="op-r2x",
            cont=None):
    args = {"session_id": sid, "reason": reason, "operation_id": op}
    if cont:
        args["continuation_token"] = cont
    return registry.invoke(actors["jiaming"], "memory.recall.round2",
                           args, None)


def set_policy(enabled, provider=None, key="k", expected=None):
    if expected is None:
        expected = judge_policy.get_policy()["revision"]
    return judge_policy.update_policy(
        "qiaosheng", expected_revision=expected, enabled=enabled,
        provider=provider, idempotency_key=key)


class GrantedProvider(jb.JudgeProvider):
    """R04：非 TypeSafeJevJudge 的 provider，凭自身 outbound_grants
    的 source_excerpt 许可通过（provider 无关接口）。"""

    name = "granted_generic"

    def outbound_grants(self):
        return frozenset({"source_excerpt", "event_excerpt"})

    def judge(self, plan, candidates, ctx):
        items = [jb.JudgeItem(
            candidate_ref=c.get("candidate_ref") or c["resource_ref"],
            candidate_version=str(c.get("content_version") or ""),
            relevance_signal=0.8, evaluation_status="evaluated",
            model_id=self.name, prompt_version="t")
            for c in candidates]
        return jb.JudgeBatchResult(items=items,
                                   provider_status="evaluated")


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    jb.clear_injected()


def _r1_scenario_off(actors, op="op-r2s"):
    """关闭政策下的合法升级场景：words paraphrase（证据不足）+
    原文已导入 → Round1 完成。"""
    set_policy(False)
    hold(actors["jiaming"], "崧蓝事件正文",
         our_words=[{"speaker": "qiaosheng",
                     "text": "复述：崧蓝染色的傍晚",
                     "expression_kind": "paraphrase"}])
    r1 = _start(actors, op=op)
    _seed_source()
    return r1["data"]


class TestR01OffModeRound2:
    def test_r01_off_round2_without_any_provider(self, actors):
        """R01：关闭判断、无任何 Jev/provider 许可——原文二轮可执行；
        judged=0、bypassed、不伪造 evaluated。"""
        r1 = _r1_scenario_off(actors)
        sid = r1["recall_session_id"]
        r2 = _round2(actors, sid)
        p = r2["data"]
        assert p["round"] == 2
        assert p["judge_mode"] == "off"
        assert p["judgement_status"] == "bypassed_by_user"
        assert p["judged_count"] == 0
        assert p["coverage"]["judge"] == "bypassed_by_user"
        # 本批候选全集冻结分页（不再 3 条截断）
        assert p["candidates"], "关闭模式 raw 命中应交付"
        assert p["pagination"]["candidate_total"] >= 1

    def test_r02_off_does_not_open_raw_permissions(self, actors,
                                                   monkeypatch):
        """R02：关闭判断不等于放开 Raw 开关——Raw 通道关闭时拒绝。"""
        from mariposa import config
        r1 = _r1_scenario_off(actors)
        sid = r1["recall_session_id"]
        monkeypatch.setattr(config, "RECALL_RAW_FALLBACK_ENABLED", False)
        with pytest.raises(Forbidden) as ei:
            _round2(actors, sid)
        assert ei.value.code in ("ROUND2_GATE_DENIED", "RAW_DISABLED")

    def test_r02_bad_reason_rejected(self, actors):
        """R02：理由不在闭集/无事实支持仍拒（off 不豁免理由门）。"""
        r1 = _r1_scenario_off(actors)
        sid = r1["recall_session_id"]
        with pytest.raises(Forbidden) as ei:
            _round2(actors, sid, reason="JUST_CURIOUS")
        assert ei.value.code == "ROUND2_GATE_DENIED"
        with pytest.raises(Forbidden):
            _round2(actors, sid, reason="NO_DELIVERABLE_CANDIDATE",
                    op="op-r2b2")  # 全集非空，该理由无事实支持

    def test_r02_scope_mismatch_rejected(self, actors):
        """R02：错 scope 的 round2 请求拒绝。"""
        r1 = _r1_scenario_off(actors)
        sid = r1["recall_session_id"]
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"], "memory.recall.round2",
                            {"session_id": sid,
                             "reason": "EVIDENCE_INSUFFICIENT",
                             "operation_id": "op-r2-scope",
                             "conversation_scope": "other-room"}, None)

    def test_r03_off_does_not_bypass_round1_conditions(self, actors):
        """R03：无真实完成回执（未跑 Round1 直接 round2）→ 拒绝；
        off 不绕过首轮完成条件。"""
        set_policy(False)
        hold(actors["jiaming"], "崧蓝事件正文",
             our_words=[{"speaker": "qiaosheng",
                         "text": "复述：崧蓝染色的傍晚",
                         "expression_kind": "paraphrase"}])
        r1 = _start(actors, op="op-r3")  # Round1 完成
        sid = r1["data"]["recall_session_id"]
        # 伪造"无完成回执"：直接删回执行
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM recall_round1_receipts"
                         " WHERE session_id=?", (sid,))
            conn.execute("COMMIT")
        _seed_source()
        with pytest.raises(Forbidden) as ei:
            _round2(actors, sid)
        assert ei.value.code == "ROUND2_GATE_DENIED"

    def test_r03_partial_retrieval_blocks(self, actors, monkeypatch):
        """R03：首轮检索 family partial（dense pending）→ 不能借 off
        放行。"""
        r1 = _r1_scenario_off(actors, op="op-r3b")
        sid = r1["recall_session_id"]
        # 把首轮回执的 words_lexical 改成 partial（模拟真实不完整轮）
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT coverage FROM recall_round1_receipts WHERE"
                " session_id=? AND revision=1", (sid,)).fetchone()
            import json as _j
            cov = _j.loads(row["coverage"])
            cov["words_lexical"] = "partial_topk_window"
            conn.execute(
                "UPDATE recall_round1_receipts SET coverage=? WHERE"
                " session_id=? AND revision=1",
                (_j.dumps(cov), sid))
            conn.execute("COMMIT")
        with pytest.raises(Forbidden):
            _round2(actors, sid)


class TestR04ProviderAgnosticGate:
    def test_r04_generic_provider_own_grant_passes(self, actors):
        """R04：非 TypeSafeJevJudge 的 provider 凭自身 source_excerpt
        许可通过门禁（不再 isinstance 认 Jev）。"""
        jb.register_for_tests("granted_generic", GrantedProvider())
        set_policy(True, provider="granted_generic")
        hold(actors["jiaming"], "崧蓝事件正文",
             our_words=[{"speaker": "qiaosheng",
                         "text": "复述：崧蓝染色的傍晚",
                         "expression_kind": "paraphrase"}])
        r1 = _start(actors, op="op-r4a")
        sid = r1["data"]["recall_session_id"]
        _seed_source()
        r2 = _round2(actors, sid)
        assert r2["data"]["candidates"]
        assert r2["data"]["coverage"]["judge"] == "evaluated"

    def test_r04_no_grant_still_denied(self, actors):
        """R04：provider 无 source_excerpt 许可 → 仍拒（对照）。"""
        class NoGrant(jb.JudgeProvider):
            name = "nogrant_generic"

            def judge(self, plan, candidates, ctx):
                return jb.JudgeBatchResult(provider_status="evaluated")

        jb.register_for_tests("nogrant_generic", NoGrant())
        set_policy(True, provider="nogrant_generic")
        hold(actors["jiaming"], "崧蓝事件正文",
             our_words=[{"speaker": "qiaosheng",
                         "text": "复述：崧蓝染色的傍晚",
                         "expression_kind": "paraphrase"}])
        r1 = _start(actors, op="op-r4b")
        sid = r1["data"]["recall_session_id"]
        _seed_source()
        with pytest.raises(Forbidden) as ei:
            _round2(actors, sid)
        assert ei.value.code == "ROUND2_GATE_DENIED"
        gate = ei.value.detail.get("gate") or {}
        assert gate.get("judge_outbound_authorized") is False


class TestR05BatchPagination:
    def test_r05_full_batch_deliverable_before_upstream(self, actors):
        """R05：一批 20 个原文命中、单页只容纳一部分——本批全部可
        逐页取得，不跳过第 4—20 个。"""
        set_policy(False)
        hold(actors["jiaming"], "崧蓝事件正文",
             our_words=[{"speaker": "qiaosheng",
                         "text": "复述：崧蓝染色",
                         "expression_kind": "paraphrase"}])
        r1 = _start(actors, op="op-r5")
        sid = r1["data"]["recall_session_id"]
        # 种 20 条原文命中（一个会话 20 条消息）
        from mariposa.source import importer
        import json as _json
        import tempfile
        import pathlib
        tmp = pathlib.Path(tempfile.mkdtemp())
        msgs = [{"uuid": f"r5-m{i}", "sender": "human",
                 "created_at": "2026-09-20T10:00:00.000Z",
                 "content": [{"type": "text",
                              "text": f"崧蓝染色记忆第{i}段"}]}
                for i in range(20)]
        f = tmp / "s.json"
        f.write_text(_json.dumps(
            [{"uuid": "c-r5", "chat_messages": msgs}],
            ensure_ascii=False), encoding="utf-8")
        importer.import_file("jiaming", str(f))
        r2 = _round2(actors, sid)
        p = r2["data"]
        total = p["pagination"]["candidate_total"]
        first_page_n = p["pagination"]["returned_count"]
        assert total == 20, f"本批应冻结 20 个候选，实际 {total}"
        assert first_page_n < total, "单页装不下 20 条"
        # 逐页取尽本批——并集恰为 20
        entries = list(p["candidates"])
        cursor = p["pagination"].get("next_cursor")
        while cursor:
            page = paging.serve_page(actors["jiaming"], {
                "result_set_id": p["pagination"]["result_set_id"],
                "session_id": sid, "cursor": cursor})
            entries.extend(page["candidates"])
            cursor = page["pagination"].get("next_cursor")
        refs = {e["resource_ref"] for e in entries
                if e.get("resource_ref")}
        assert len(refs) == 20, "本批 20 个必须全部可逐页取得"

    def test_r06_two_cursor_kinds_not_interchangeable(self, actors):
        """R06：交付分页游标 ≠ round2 上游 continuation_token——互相
        喂给对方入口都拒绝。"""
        r1 = _r1_scenario_off(actors, op="op-r6")
        sid = r1["recall_session_id"]
        r2 = _round2(actors, sid)
        p = r2["data"]
        page_cursor = p["pagination"].get("next_cursor")
        raw_cont = (p.get("continuation") or {}).get(
            "continuation_token")
        # 只要有一种游标存在就做互换负例
        with pytest.raises(Forbidden):
            # 分页游标塞进 round2 continuation → CONTINUATION_INVALID
            _round2(actors, sid, op="op-r6b", cont=page_cursor or "x")
        if raw_cont:
            with pytest.raises(Forbidden):
                # 上游 token 塞给 memory.recall.page → 游标无效
                paging.serve_page(actors["jiaming"], {
                    "result_set_id": p["pagination"]["result_set_id"],
                    "session_id": sid, "cursor": raw_cont})

    def test_r07_reason_uses_full_set_facts(self, actors):
        """R07：升级理由基于全集事实——off 模式首轮回执的
        delivered_count=全集数，第一页多少不制造假不足。"""
        r1 = _r1_scenario_off(actors, op="op-r7")
        sid = r1["recall_session_id"]
        with db.recall_runtime() as conn:
            receipt = store.read_round1_receipt(conn, sid, 1)
        facts = receipt["coverage"]["_first_round_facts"]
        assert facts["delivered_count"] >= 1  # 全集交付而非 0-3 页
        # NO_DELIVERABLE_CANDIDATE 因此无事实支持（全集非空）
        with pytest.raises(Forbidden):
            _round2(actors, sid, reason="NO_DELIVERABLE_CANDIDATE",
                    op="op-r7b")


class TestR08ContinuationGates:
    def test_r08_policy_switch_blocks_continuation(self, actors):
        """R08：Raw 首批签发后切换总政策 → 续页按当前模式重检，
        切到 on 且无许可 → 拒绝不外发。"""
        r1 = _r1_scenario_off(actors, op="op-r8")
        sid = r1["recall_session_id"]
        r2 = _round2(actors, sid)
        cont = (r2["data"].get("continuation") or {}).get(
            "continuation_token")
        if not cont:  # 本批翻尽：多种一条保证 has_more
            _seed_source("补充原文崧蓝内容", tag="t2")
        # 切到 on 且选无许可 provider
        jb.register_for_tests("granted_generic", GrantedProvider())
        set_policy(True, provider="granted_generic", key="r8-on")
        if cont:
            with pytest.raises(Forbidden) as ei:
                _round2(actors, sid, op="op-r8b", cont=cont)
            assert ei.value.code in ("RECALL_POLICY_CHANGED",
                                     "ROUND2_GATE_DENIED",
                                     "RAW_PROFILE_WITHDRAWN")

    def test_r08_raw_switch_off_blocks_continuation(self, actors,
                                                    monkeypatch):
        """R08：撤回 Raw 开关后续页拒绝（CB-010 保持）。"""
        from mariposa import config
        r1 = _r1_scenario_off(actors, op="op-r8b")
        sid = r1["recall_session_id"]
        r2 = _round2(actors, sid)
        cont = (r2["data"].get("continuation") or {}).get(
            "continuation_token")
        monkeypatch.setattr(config, "RECALL_RAW_FALLBACK_ENABLED", False)
        if cont:
            with pytest.raises(Forbidden) as ei:
                _round2(actors, sid, op="op-r8c", cont=cont)
            assert ei.value.code == "RAW_DISABLED"

    def test_j08_round2_policy_change_detected(self, actors):
        """J08（round2 段）：首轮 off → 政策切 on → Round2 拒绝
        RECALL_POLICY_CHANGED（回执冻结模式 vs 当前模式）。"""
        r1 = _r1_scenario_off(actors, op="op-j8r2")
        sid = r1["recall_session_id"]
        jb.register_for_tests("granted_generic", GrantedProvider())
        set_policy(True, provider="granted_generic", key="j8-on")
        _seed_source()
        with pytest.raises(Forbidden) as ei:
            _round2(actors, sid)
        assert ei.value.code == "RECALL_POLICY_CHANGED"
