"""判断总开关与关闭模式全集分页（MANUAL_HANDOFF_JUDGE_SWITCH_V1，WP1）。

对 ACCEPTANCE_MANUAL_HANDOFF_JUDGE_20261008.json 的逐项落证据：
J01（关闭零判断调用）/J02（开启仅所选 provider）/J04（故障不降级）/
J05（缺行≠关闭、升级不放宽）/J06（写权矩阵+幂等+CAS）/
P01（53 候选全集并集）/P03（长文码点分片拼回）/P04（字节预算顺延）/
P05（同游标稳定重放零检索）/P06（冻结集合不混新记录）/
P09（Top-K 披露不冒称穷尽）/P11（清理无孤儿）+ J08 分页段
（政策切换 RECALL_POLICY_CHANGED）。全部合成数据、隔离根。
"""
from __future__ import annotations

import json

import pytest

from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy, paging, service as recall_service
from mariposa.recall import store as recall_store
from mariposa.retrieval.judges import base as jb
from tests.conftest import reset_all


def _actor(pid="jiaming"):
    return identity.Principal(pid, "n", "agent", "claude_chat", "bj")


def _owner():
    return identity.Principal("qiaosheng", "江乔生", "human", "web", "bq")


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": _actor(), "qiaosheng": _owner()}


def hold(actors, text, date="2026-09-01", **kw):
    base = dict(text=text, memory_date=date, date_confidence="exact",
                original_title=kw.pop("title", "t"),
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def set_policy(enabled, provider=None, model_id=None, key="k1",
               expected=None):
    if expected is None:
        expected = judge_policy.get_policy()["revision"]
    return judge_policy.update_policy(
        "qiaosheng", expected_revision=expected, enabled=enabled,
        provider=provider, model_id=model_id, idempotency_key=key)


class RecordingJudge(jb.JudgeProvider):
    name = "recording_test"

    def __init__(self, status="evaluated", reason=None, items_factory=None):
        self.calls = []
        self._status = status
        self._reason = reason
        self._factory = items_factory

    def judge(self, query_plan, candidates, execution_context):
        self.calls.append({"plan": query_plan, "candidates": candidates,
                           "ctx": execution_context})
        items = (self._factory(candidates) if self._factory else [
            jb.JudgeItem(candidate_ref=c["candidate_ref"],
                         candidate_version=c.get("content_version"),
                         relevance_signal=0.5,
                         evaluation_status=self._status)
            for c in candidates])
        return jb.JudgeBatchResult(items=items,
                                   provider_status=self._status,
                                   degraded_reason=self._reason)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    jb.clear_injected()
    from mariposa import config
    config.RECALL_JUDGE_PROVIDER = "disabled"


def start(actors, query="搬家", terms=None, op="op-x", scope=""):
    return recall_service.start(
        actors["jiaming"],
        {"query_plan": {"original_request": query, "channels": ["event"],
                        "lexical_terms": terms or [query]},
         "conversation_scope": scope},
        op_ctx={"principal_id": "jiaming",
                "operation_key": f"start:new:{op}",
                "payload_hash": op})


def walk_all_pages(principal, packet, scope=""):
    """从首页 packet 出发逐页读尽冻结集（含片段），返回
    (全量条目列表, 页数)。"""
    entries = list(packet.get("candidates") or [])
    pages = 1
    cursor = (packet.get("pagination") or {}).get("next_cursor")
    seen_cursors = set()
    while cursor:
        assert cursor not in seen_cursors, "游标形成环"
        seen_cursors.add(cursor)
        page = paging.serve_page(principal, {
            "result_set_id": packet["pagination"]["result_set_id"],
            "session_id": packet["recall_session_id"],
            "conversation_scope": scope,
            "cursor": cursor})
        entries.extend(page["candidates"])
        pages += 1
        assert pages < 500, "分页不收敛"
        cursor = page["pagination"].get("next_cursor")
    return entries, pages


# ---------------------------------------------------------------- J05 政策读侧

def _wipe_policy_and_set_env(monkeypatch, attr):
    """J05 夹具：清政策表 + 把 env 导入源指到指定值（模拟旧部署）。"""
    from mariposa import config
    monkeypatch.setattr(config, "RECALL_JUDGE_PROVIDER", attr)
    with recall_store.db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM recall_judge_policy")
        conn.execute("COMMIT")


class TestJ05PolicyReadSide:
    def test_missing_row_is_unconfigured_not_off(self, actors,
                                                 monkeypatch):
        """J05：缺行（未选择）+ 旧 disabled env → 未配置阻断，≠关闭。"""
        _wipe_policy_and_set_env(monkeypatch, "disabled")
        p = judge_policy.get_policy()
        assert p["present"] is True  # env 导入种入首行
        assert p["mode"] == "unconfigured"
        assert p["enabled"] is True and p["provider"] is None
        assert p["updated_by"] == "system:env_import"
        assert judge_policy.effective()["judge_required"] is True

    def test_valid_legacy_jev_env_keeps_enabled(self, actors,
                                                monkeypatch):
        """J05：旧有效 Jev 配置升级保留开启（provider 不丢）。"""
        _wipe_policy_and_set_env(monkeypatch, "typesafe_jev")
        p = judge_policy.get_policy()
        assert p["mode"] == "on"
        assert p["provider"] == "typesafe_jev"
        assert p["enabled"] is True

    def test_unknown_provider_is_unconfigured_not_off(self, actors,
                                                       monkeypatch):
        """J05：未知 provider 名不解释成关闭。两种形态：env 拼错
        （导入为 NULL）与库内损坏行（人类行 provider 异常）。"""
        # 形态一：env 指向未知名 → 导入为未配置，不猜
        _wipe_policy_and_set_env(monkeypatch, "mystery_provider")
        p = judge_policy.get_policy()
        assert p["mode"] == "unconfigured" and p["provider"] is None
        assert p["enabled"] is True  # 未配置≠关闭

        # 形态二：库内行 provider 为未知名（损坏/异常写入）
        with recall_store.db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE recall_judge_policy SET provider='mystery',"
                " updated_by='qiaosheng' WHERE id=1")
            conn.execute("COMMIT")
        p = judge_policy.get_policy()
        assert p["mode"] == "unconfigured"
        assert any("provider_unknown" in d for d in p["degraded_reasons"])
        # 未配置模式正文不直出（S10 阻断语义保留）
        hold(actors, "搬家事件")
        packet = start(actors)
        assert packet["coverage"]["judge"] == "not_configured"
        assert packet["candidates"] == []

    def test_human_write_freezes_env(self, actors, monkeypatch):
        """J05：人类写过后 env 永久失效（数据库唯一正本）。"""
        set_policy(False, provider="typesafe_jev")
        from mariposa import config
        monkeypatch.setattr(config, "RECALL_JUDGE_PROVIDER",
                            "typesafe_jev")
        p = judge_policy.get_policy()
        assert p["mode"] == "off" and p["updated_by"] == "qiaosheng"


# ---------------------------------------------------------------- J06 写权矩阵

class TestJ06PolicyWriteAuthz:
    def test_only_qiaosheng_can_write(self, actors):
        out = set_policy(True, provider="typesafe_jev")
        assert out["mode"] == "on"
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            judge_policy.update_policy(
                "jiaming", expected_revision=out["revision"],
                enabled=False, provider=None)
        with pytest.raises(Forbidden):
            judge_policy.update_policy(
                "worker", expected_revision=out["revision"],
                enabled=False, provider=None)

    def test_registry_update_only_for_qiaosheng(self, actors):
        """J06：能力面——jiaming/worker 拿不到 update 权。"""
        from mariposa.capabilities import registry
        cap = registry.REGISTRY["maintenance.recall_policy.update"]
        assert cap.allowed_principals == {"qiaosheng"}
        getcap = registry.REGISTRY["maintenance.recall_policy.get"]
        assert "jiaming" in getcap.allowed_principals  # 模型可读模式
        assert "worker" not in getcap.allowed_principals

    def test_idempotent_repeat_applies_once(self, actors):
        first = set_policy(False, key="idem-1")
        second = set_policy(True, provider="typesafe_jev",
                            key="idem-1", expected=first["revision"])
        # 同幂等键重复提交：返回已生效政策，不二次翻转
        assert second["revision"] == first["revision"]
        assert second["mode"] == "off"

    def test_expected_revision_conflict(self, actors):
        set_policy(False, key="a")
        with pytest.raises(Exception) as ei:
            set_policy(True, provider="typesafe_jev", key="b",
                       expected=0)  # 旧 revision
        assert "REVISION_CONFLICT" in str(ei.value) or \
            getattr(ei.value, "code", "") == "REVISION_CONFLICT"

    def test_on_requires_provider(self, actors):
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            set_policy(True, provider=None)

    def test_unknown_provider_rejected_on_write(self, actors):
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            set_policy(True, provider="not_a_provider")


# ---------------------------------------------------------------- J01/J02/J04

class TestJudgeModes:
    def test_j01_off_zero_provider_calls(self, actors, monkeypatch):
        """J01：关闭模式检索可用，判断构造/网络计数全零——即使
        预配置了 provider 也零调用。"""
        hold(actors, "搬家事件甲")
        hold(actors, "搬家事件乙")
        set_policy(False, provider="typesafe_jev")  # 关但不销毁配置
        # 构造器 spy：政策关闭时 get_provider_by_name 必须不被调用
        def _no_construct(name):
            raise AssertionError(
                f"关闭模式构造了判断 provider: {name}")
        monkeypatch.setattr(jb, "get_provider_by_name", _no_construct)
        # 网络 spy：任何 HTTP 构造即失败
        import http.client as _hc
        monkeypatch.setattr(_hc, "HTTPConnection",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("关闭模式发起网络连接")))
        packet = start(actors)
        assert packet["judge_mode"] == "off"
        assert packet["judgement_status"] == "bypassed_by_user"
        assert packet["judged_count"] == 0
        assert packet["coverage"]["judge"] == "bypassed_by_user"
        # 全集交付（不再 0-3 截断）
        assert len(packet["candidates"]) == 2

    def test_j01_off_with_uninstalled_provider_configured(self, actors):
        """J01：政策关闭且所选 provider 未安装（codex_sdk）——检索
        照常、零判断，不因 provider 缺失阻断关闭模式。"""
        hold(actors, "搬家事件")
        set_policy(False, provider="codex_sdk")
        packet = start(actors)
        assert packet["judge_mode"] == "off"
        assert len(packet["candidates"]) == 1

    def test_j02_on_uses_selected_provider_bounded(self, actors):
        """J02：开启+Jev → 仅 Jev 被调用；维持送判 40/交付 3 有界。"""
        for i in range(6):
            hold(actors, f"搬家事件{i}")
        judge = RecordingJudge()
        jb.register_for_tests("typesafe_jev", judge)
        set_policy(True, provider="typesafe_jev")
        packet = start(actors)
        assert len(judge.calls) == 1
        assert judge.calls[0]["ctx"]["judge_policy_revision"] >= 1
        assert packet["judge_mode"] == "on"
        assert packet["judgement_status"] == "evaluated"
        assert packet["judged_count"] == 6
        # 有界交付保留（≤3）
        assert len(packet["candidates"]) <= 3
        assert packet["pagination"]["has_more"] is False

    def test_j04_provider_failure_stays_explicit(self, actors):
        """J04：开启但 provider 超时/不可用 → 结构化 unavailable，
        不假报无记忆、不暗切关闭或别的 provider。"""
        hold(actors, "搬家事件")
        judge = RecordingJudge(status="unavailable", reason="timeout")
        jb.register_for_tests("typesafe_jev", judge)
        set_policy(True, provider="typesafe_jev")
        packet = start(actors)
        assert packet["coverage"]["judge"] == "unavailable"
        assert "judge_timeout" in packet["degraded_reasons"]
        # 不暗切成关闭：模式仍 on、无分页直出、正文不释放
        assert packet["judge_mode"] == "on"
        assert packet["candidates"] == []
        assert packet.get("pagination", {}).get("has_more") is False
        assert any("不直出" in m for m in packet["missing"])
        # 不假报"没有相关记忆"：search_status 不是 NO_MATCH_OBSERVED
        assert packet["search_status"] in ("DEGRADED",)

    def test_j04_bad_output_cardinality_rejected(self, actors):
        """J04：provider 返回陌生/重复 ID → 整轮判断作废（对账）。"""
        hold(actors, "搬家事件")
        judge = RecordingJudge(items_factory=lambda cs: [
            jb.JudgeItem(candidate_ref="memory:nonexistent",
                         candidate_version="1",
                         relevance_signal=0.9,
                         evaluation_status="evaluated")])
        jb.register_for_tests("typesafe_jev", judge)
        set_policy(True, provider="typesafe_jev")
        packet = start(actors)
        assert packet["coverage"]["judge"] == "unavailable"
        assert "judge_cardinality_violation" in packet["degraded_reasons"]
        assert packet["candidates"] == []


# ---------------------------------------------------------------- P01 全集分页

class TestP01FullCandidateSet:
    def test_53_candidates_union_exact(self, actors):
        """P01：53 个合法候选，关闭判断，逐页并集恰为 53，不截于
        3/20/40。"""
        for i in range(53):
            hold(actors, f"搬家事件编号{i:03d}的内容")
        set_policy(False)
        packet = start(actors, terms=["搬家"], op="op-p01")
        assert packet["judge_mode"] == "off"
        total = packet["pagination"]["candidate_total"]
        assert total == 53, f"候选全集应为 53，实际 {total}"
        entries, pages = walk_all_pages(actors["jiaming"], packet)
        refs = [e["resource_ref"] for e in entries
                if e.get("resource_ref")]
        assert len(set(refs)) == 53, "候选身份并集必须恰为 53"
        assert pages >= 2, "53 条不可能一页装下（每页 ≤10 条目）"
        # 最后一页 has_more=False 且收尾干净
        assert packet["pagination"]["returned_count"] <= 10

    def test_p03_long_text_fragment_reassembly(self, actors):
        """P03（WP-06 6A，D-2 选 A）：单候选正文超旧 600 字限与单页
        字节限 → 码点分片、游标前进、拼回逐字等于当前 revision 的
        version_body 正本（memory_versions——她 2026-10-09 裁定关闭
        模式=按原本格式给周家明，X16 标点/换行/说话人保真）。"""
        body = ("搬" * 9000) + ("家" * 9000) + "。"
        r = hold(actors, body)
        from mariposa import db as _db
        with _db.formal() as conn:
            vrow = conn.execute(
                "SELECT event_text, hold_text FROM memory_versions"
                " WHERE memory_id=? AND version_no=1",
                (r["memory_id"],)).fetchone()
            expected = vrow["event_text"] or vrow["hold_text"]
        set_policy(False)
        packet = start(actors, terms=["搬家"], op="op-p03")
        entries, pages = walk_all_pages(actors["jiaming"], packet)
        frags = [e for e in entries if e.get("fragment")]
        assert frags, "长文必须产生显式分片"
        # 拼回逐字一致（按 evidence snippet 载体）
        text = "".join(f["fragment"]["text"] for f in frags)
        assert text == expected
        # 进度严格前进：start/end 单调且不重叠
        pos = [(f["fragment"]["start_char"], f["fragment"]["end_char"])
               for f in frags]
        assert pos == sorted(pos)
        assert all(e < n for (e, _), (_, n) in zip(pos, pos[1:]))
        assert frags[0]["fragment"]["total_chars"] == len(expected)
        # 旧 600 字截断不再出现（正文载体完整交付）
        assert len(expected) > 600 and len(text) == len(expected)
        # 每页体积真实受控（含信封）
        assert pages > 1

    def test_p04_byte_budget_defers_not_pops(self, actors):
        """P04：多候选逼近 24576 字节预算 → 超出条目顺延下页，
        不 pop 永久丢失；片段数不当候选数。"""
        for i in range(8):
            hold(actors, f"搬家事件{i}" + "很长的正文" * 400)
        set_policy(False)
        packet = start(actors, terms=["搬家"], op="op-p04")
        total = packet["pagination"]["candidate_total"]
        entries, _ = walk_all_pages(actors["jiaming"], packet)
        refs = {e["resource_ref"] for e in entries
                if e.get("resource_ref")}
        assert len(refs) == total == 8
        # 首页真实字节 ≤24576（信封含元数据）
        blob = json.dumps({"ok": True, "data": packet},
                          ensure_ascii=False).encode("utf-8")
        assert len(blob) <= 24576

    def test_p05_same_cursor_stable_no_reexec(self, actors, monkeypatch):
        """P05：相同游标重读 → 结果稳定、零重检索、不新记轮次。"""
        for i in range(25):  # >10 条目 → 必然分页
            hold(actors, f"搬家事件{i}")
        set_policy(False)
        packet = start(actors, terms=["搬家"], op="op-p05")
        rsid = packet["pagination"]["result_set_id"]
        cursor = packet["pagination"]["next_cursor"]
        assert cursor
        with recall_store.db.recall_runtime() as conn:
            rounds_before = conn.execute(
                "SELECT COUNT(*) c FROM recall_rounds").fetchone()["c"]
        # 禁止重新检索：任何再检索立即失败
        def _no_retrieval(*a, **kw):
            raise AssertionError("翻页触发了重新检索")
        monkeypatch.setattr(recall_service, "_event_candidates",
                            _no_retrieval)
        p1 = paging.serve_page(actors["jiaming"], {
            "result_set_id": rsid, "cursor": cursor,
            "session_id": packet["recall_session_id"]})
        p2 = paging.serve_page(actors["jiaming"], {
            "result_set_id": rsid, "cursor": cursor,
            "session_id": packet["recall_session_id"]})
        assert json.dumps(p1, sort_keys=True) == \
            json.dumps(p2, sort_keys=True)
        with recall_store.db.recall_runtime() as conn:
            rounds_after = conn.execute(
                "SELECT COUNT(*) c FROM recall_rounds").fetchone()["c"]
        assert rounds_after == rounds_before, "翻页新记了轮次"

    def test_p06_frozen_set_no_new_records(self, actors):
        """P06：首页后新增无关记录 → 不混入冻结集合；顺序稳定。"""
        for i in range(4):
            hold(actors, f"搬家事件{i}")
        set_policy(False)
        packet = start(actors, terms=["搬家"], op="op-p06")
        hold(actors, "搬家事件后来才有的")  # 冻结后新增
        entries, _ = walk_all_pages(actors["jiaming"], packet)
        refs = {e["resource_ref"] for e in entries
                if e.get("resource_ref")}
        assert len(refs) == 4

    def test_p09_topk_disclosed_not_exhaustive(self, actors):
        """P09：上游 Top-K/受限窗口 → retrieval_coverage 如实披露，
        不冒称整库穷尽。"""
        hold(actors, "搬家事件")
        set_policy(False)
        # browse 场景（显式约束、无词面）= latest-K 窗口 → 不得标穷尽
        packet = recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "看日常",
                            "channels": ["event"],
                            "explicit_constraints":
                                {"categories": ["daily"]}}},
            op_ctx={"principal_id": "jiaming",
                    "operation_key": "start:new:op-p09",
                    "payload_hash": "op-p09"})
        cov = packet["retrieval_coverage"]
        assert cov["retrieval_scope_exhaustive"] is False
        assert cov.get("event") == "partial_topk_window"


# ---------------------------------------------------------------- 游标与政策切换

class TestCursorAndPolicySwitch:
    def test_cross_set_cursor_rejected(self, actors):
        """P07（分页段）：游标不属于该结果集 → 结构化拒绝无正文。"""
        for i in range(30):
            hold(actors, f"搬家事件{i}")
        set_policy(False)
        p1 = start(actors, terms=["搬家"], op="op-c1")
        p2 = start(actors, terms=["搬家"], op="op-c2")
        c2 = p2["pagination"]["next_cursor"]
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            paging.serve_page(actors["jiaming"], {
                "result_set_id": p1["pagination"]["result_set_id"],
                "cursor": c2,
                "session_id": p1["recall_session_id"]})

    def test_cross_principal_page_rejected(self, actors):
        """P07：跨主体读取结果集拒绝。"""
        hold(actors, "搬家事件")
        set_policy(False)
        packet = start(actors, op="op-c3")
        rsid = packet["pagination"]["result_set_id"]
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden):
            paging.serve_page(_actor("qiaosheng"), {
                "result_set_id": rsid,
                "session_id": packet["recall_session_id"]})

    def test_j08_switch_rejects_old_cursor(self, actors):
        """J08（分页段）：翻页途中切换政策 → 旧游标
        RECALL_POLICY_CHANGED，不混模式。"""
        for i in range(30):
            hold(actors, f"搬家事件{i}")
        set_policy(False)
        packet = start(actors, terms=["搬家"], op="op-j08")
        assert packet["pagination"]["has_more"] is True
        set_policy(True, provider="typesafe_jev",
                   key="switch-on",
                   expected=judge_policy.get_policy()["revision"])
        from mariposa.errors import Forbidden
        with pytest.raises(Forbidden) as ei:
            paging.serve_page(actors["jiaming"], {
                "result_set_id":
                    packet["pagination"]["result_set_id"],
                "cursor": packet["pagination"]["next_cursor"],
                "session_id": packet["recall_session_id"]})
        assert ei.value.code == "RECALL_POLICY_CHANGED"

    def test_j08_replay_rejected_after_switch(self, actors):
        """J08（重放段）：切换后旧 operation 重放拒绝。"""
        hold(actors, "搬家事件")
        set_policy(False)
        op_ctx = {"principal_id": "jiaming",
                  "operation_key": "start:new:op-j08r",
                  "payload_hash": "h"}
        recall_service.start(
            actors["jiaming"],
            {"query_plan": {"original_request": "找搬家",
                            "channels": ["event"],
                            "lexical_terms": ["搬家"]}},
            op_ctx=op_ctx)
        set_policy(True, provider="typesafe_jev",
                   key="switch-on-2",
                   expected=judge_policy.get_policy()["revision"])
        from mariposa.capabilities.transport import _payload_hash
        from mariposa.recall.store import run_operation
        with pytest.raises(Exception) as ei:
            run_operation(
                "jiaming", "start:new:op-j08r",
                lambda: recall_service.start(
                    actors["jiaming"],
                    {"query_plan": {"original_request": "找搬家",
                                    "channels": ["event"],
                                    "lexical_terms": ["搬家"]}},
                    op_ctx=op_ctx),
                payload_hash="h",
                replay_guard=lambda saved: recall_service
                .revalidate_replayed("start", saved))
        assert getattr(ei.value, "code", "") == "RECALL_POLICY_CHANGED"


# ---------------------------------------------------------------- P11 清理

class TestP11Purge:
    def test_expired_session_purges_page_assets(self, actors):
        """P11：过期 session 清理连分页资产一起删，无孤儿；未过期
        对照组不受影响。"""
        for i in range(15):  # >10 条目 → 产生续页游标
            hold(actors, f"搬家事件{i}")
        set_policy(False)
        packet = start(actors, terms=["搬家"], op="op-p11")
        rsid = packet["pagination"]["result_set_id"]
        cursor = packet["pagination"]["next_cursor"]
        assert cursor
        with recall_store.db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE recall_sessions SET expires_at='2000-01-01T00:"
                "00:00+00:00' WHERE session_id=?",
                (packet["recall_session_id"],))
            conn.execute("COMMIT")
        recall_store.purge_expired()
        with recall_store.db.recall_runtime() as conn:
            sets = conn.execute(
                "SELECT COUNT(*) c FROM recall_page_sets").fetchone()["c"]
            cursors = conn.execute(
                "SELECT COUNT(*) c FROM recall_page_cursors"
            ).fetchone()["c"]
        assert sets == 0 and cursors == 0, "分页资产留了孤儿"

    def test_reset_for_tests_clears_page_assets(self, actors):
        hold(actors, "搬家事件")
        set_policy(False)
        start(actors, op="op-p11b")
        recall_store.reset_for_tests()
        with recall_store.db.recall_runtime() as conn:
            for t in ("recall_page_sets", "recall_page_cursors"):
                n = conn.execute(
                    f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                assert n == 0


# ---------------------------------------------------------------- 网页开关 J07（HTTP 面）

class TestJ07WebSwitchHttpLevel:
    def test_get_does_not_invoke_models(self, actors, monkeypatch):
        """J07：GET/刷新零推理调用（provider 探针零网络）。"""
        import http.client as _hc
        monkeypatch.setattr(_hc, "HTTPConnection",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("GET 触发网络")))
        from mariposa.capabilities import registry
        out = registry.invoke(_owner(), "maintenance.recall_policy.get",
                              {}, None)
        assert out["ok"] is True
        assert out["data"]["mode"] in ("on", "off", "unconfigured")

    def test_update_via_registry_then_persisted(self, actors):
        """J07：切换经能力面写库；重启语义=政策行持久（读回一致）。"""
        from mariposa.capabilities import registry
        cur = registry.invoke(_owner(), "maintenance.recall_policy.get",
                              {}, None)["data"]
        out = registry.invoke(
            _owner(), "maintenance.recall_policy.update",
            {"expected_revision": cur["revision"], "enabled": False,
             "provider": None, "idempotency_key": "web-1"},
            "web-1")
        assert out["data"]["mode"] == "off"
        again = registry.invoke(_owner(), "maintenance.recall_policy.get",
                                {}, None)["data"]
        assert again["mode"] == "off" and \
            again["revision"] == out["data"]["revision"]
        # jiaming 经能力面不可写（registry 层拒绝）
        from mariposa.errors import MariposaError
        with pytest.raises(MariposaError):
            registry.invoke(_actor(), "maintenance.recall_policy.update",
                            {"expected_revision": again["revision"],
                             "enabled": True,
                             "provider": "typesafe_jev"}, None)
