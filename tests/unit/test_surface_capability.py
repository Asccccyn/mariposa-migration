"""surface 四 capability 全链（registry 面）：门/幂等/预算/prepare→validate→check。"""
from __future__ import annotations

import json

import pytest

from mariposa import config, db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import judge_policy
from mariposa.surfacing import judge as surface_judge
from mariposa.surfacing import store as surface_store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    surface_judge.clear_transport_override()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "宿主用户", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "char", "agent",
                                      "claude_chat", "bj"),
    }


def _policy_on(monkeypatch, *, grants=True, cost_cap=5.0,
               enabled=True, judge_enabled=True):
    monkeypatch.setattr(config, "SURFACE_ENABLED", enabled)
    monkeypatch.setattr(config, "SURFACE_DAILY_JUDGE_COST_CAP_USD",
                        cost_cap)
    judge_policy.update_policy(
        "qiaosheng", expected_revision=_rev(), enabled=judge_enabled,
        provider="typesafe_jev", model_id="fake-model")
    grants_json = (json.dumps({
        "event_excerpt": True, "structured_metadata": True,
        "current_dialogue": True}) if grants else json.dumps({}))
    with db.formal() as conn:
        conn.execute("UPDATE recall_judge_policy SET allowed_data=?"
                     " WHERE id=1", (grants_json,))


def _rev() -> int:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT revision FROM recall_judge_policy WHERE id=1").fetchone()
        return (row["revision"] if row else 0) + 1


class CountingTransport:
    """正向问句答 value；反向问句（ambiguity_risk）答低值。"""

    def __init__(self, value=0.99):
        self.value = value
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        answers = {k: {"noul": 0.01 if "ambiguity_risk" in k
                       else self.value}
                   for k in payload["questions"]}
        return {"answers": answers, "model": "fake"}


def _hold(actors, i, **kw):
    base = dict(text=f"7月9日事件正文{i}", memory_date="2026-07-09",
                date_confidence="exact", original_title=f"标题{i}",
                categories=["sweet"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def _prepare_args(actors, text="7.9记忆库做成了"):
    return {
        "operation_id": "surf-op-1",
        "conversation_scope": "room-1",
        "source_input": {"message_id": "m1", "revision": 1, "sequence": 10,
                          "text": text, "body_hash": "a" * 64,
                          "occurred_at": None},
        "recent_dialogue": [],
        "event_hypotheses": [
            {"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
             "inherited_date_span": None}],
    }


def _invoke(actors, name, args):
    return registry.invoke(actors["jiaming"], name, args, None)


class TestGates:
    def test_disabled_by_default(self, actors, monkeypatch):
        monkeypatch.setattr(config, "SURFACE_ENABLED", False)
        out = _invoke(actors, "memory.surfacing.prepare", _prepare_args(actors))
        assert out["data"]["status"] == "blocked"
        assert out["data"]["reason_code"] == "SURFACE_DISABLED"

    def test_judge_off_blocks(self, actors, monkeypatch):
        _policy_on(monkeypatch, judge_enabled=False)
        out = _invoke(actors, "memory.surfacing.prepare", _prepare_args(actors))
        assert out["data"]["reason_code"] == "SURFACE_JUDGE_OFF"

    def test_outbound_grant_missing_blocks(self, actors, monkeypatch):
        _policy_on(monkeypatch, grants=False)
        out = _invoke(actors, "memory.surfacing.prepare", _prepare_args(actors))
        assert out["data"]["reason_code"] == "SURFACE_OUTBOUND_NOT_GRANTED"

    def test_budget_unconfigured_blocks(self, actors, monkeypatch):
        _policy_on(monkeypatch, cost_cap=None)
        _hold(actors, 0)
        out = _invoke(actors, "memory.surfacing.prepare", _prepare_args(actors))
        assert out["data"]["reason_code"] == "SURFACE_BUDGET_UNCONFIGURED"

    def test_hypotheses_over_cap_rejected_whole(self, actors, monkeypatch):
        _policy_on(monkeypatch)
        args = _prepare_args(actors)
        args["event_hypotheses"] = [
            {"hypothesis_id": f"h{i}", "start_cp": 0, "end_cp": 2,
             "inherited_date_span": None}
            for i in range(config.SURFACE_MAX_EVENT_HYPOTHESES + 1)]
        # registry 面：schema maxItems=24 结构性先拦
        with pytest.raises(Forbidden) as ei:
            _invoke(actors, "memory.surfacing.prepare", args)
        assert "SCHEMA_VIOLATION" in str(ei.value) or \
            ei.value.code == "SCHEMA_VIOLATION"
        # service 面（防御纵深）：直调同样整次拒绝，不截前半段猜意图
        from mariposa.surfacing import service as surface_service
        out = surface_service.prepare(actors["jiaming"], args)
        assert out["status"] == "rejected"
        assert out["reason_code"] == "SURFACE_EVENT_AMBIGUOUS"


class TestPrepareValidateCheck:
    def test_full_loop_with_winner(self, actors, monkeypatch):
        _policy_on(monkeypatch)
        _hold(actors, 0, text="7.9记忆库建成了")
        transport = CountingTransport(0.99)
        surface_judge.set_transport_for_tests(transport)

        out = _invoke(actors, "memory.surfacing.prepare",
                      _prepare_args(actors))
        assert out["data"]["status"] == "prepared", out["data"]
        assert out["data"]["selected_count"] == 1
        run_id = out["data"]["candidate_snapshot_ref"]

        # validate：目标消息延续话题 → ready + 完整正文 item
        vargs = {
            "operation_id": "surf-val-1",
            "conversation_scope": "room-1",
            "candidate_snapshot_ref": run_id,
            "source_input_ref": {"message_id": "m1", "revision": 1},
            "target_input": {"message_id": "m2", "revision": 1,
                              "sequence": 11, "text": "7.9那天怎么样",
                              "body_hash": "b" * 64, "occurred_at": None},
            "recent_dialogue": [],
            "window_id": "w1",
            "deadline_at": "2999-01-01T00:00:00+00:00",
        }
        vout = _invoke(actors, "memory.surfacing.validate", vargs)
        assert vout["data"]["status"] == "ready", vout["data"]
        item = vout["data"]["items"][0]
        assert item["event_text"] == "7.9记忆库建成了"
        assert item["memory_date"] == "2026-07-09"
        assert item["detail_ref"]

        # check：回执+目标+窗口全对 → valid；本地无网络（transport 未增）
        before = transport.calls
        cout = _invoke(actors, "memory.surfacing.check", {
            "validation_receipt": vout["data"]["validation_receipt"],
            "conversation_scope": "room-1",
            "target_message_id": "m2", "target_revision": 1,
            "window_id": "w1"})
        assert cout["data"]["valid"] is True
        assert cout["data"]["accepted_detail_refs"] == [item["detail_ref"]]
        assert transport.calls == before

    def test_target_changed_invalidates_check(self, actors, monkeypatch):
        _policy_on(monkeypatch)
        _hold(actors, 0, text="7.9记忆库建成了")
        surface_judge.set_transport_for_tests(CountingTransport(0.99))
        out = _invoke(actors, "memory.surfacing.prepare", _prepare_args(actors))
        run_id = out["data"]["candidate_snapshot_ref"]
        vout = _invoke(actors, "memory.surfacing.validate", {
            "operation_id": "v2", "conversation_scope": "room-1",
            "candidate_snapshot_ref": run_id,
            "source_input_ref": {"message_id": "m1", "revision": 1},
            "target_input": {"message_id": "m2", "revision": 1,
                              "sequence": 11, "text": "7.9呢",
                              "body_hash": "b" * 64, "occurred_at": None},
            "recent_dialogue": [], "window_id": "w1",
            "deadline_at": "2999-01-01T00:00:00+00:00"})
        receipt = vout["data"]["validation_receipt"]
        cout = _invoke(actors, "memory.surfacing.check", {
            "validation_receipt": receipt, "conversation_scope": "room-1",
            "target_message_id": "m2", "target_revision": 2,  # 修订变了
            "window_id": "w1"})
        assert cout["data"]["valid"] is False
        assert cout["data"]["reason_code"] == "SURFACE_TARGET_MISMATCH"

    def test_prepare_idempotent_replay_no_second_paid_call(
            self, actors, monkeypatch):
        _policy_on(monkeypatch)
        _hold(actors, 0, text="7.9记忆库建成了")
        transport = CountingTransport(0.99)
        surface_judge.set_transport_for_tests(transport)
        args = _prepare_args(actors)
        first = _invoke(actors, "memory.surfacing.prepare", args)
        calls_after_first = transport.calls
        second = _invoke(actors, "memory.surfacing.prepare", args)
        assert second["data"]["surface_run_id"] == \
            first["data"]["surface_run_id"]
        assert transport.calls == calls_after_first   # 不重跑付费 judge

    def test_same_op_different_payload_conflicts(self, actors, monkeypatch):
        _policy_on(monkeypatch)
        _hold(actors, 0)
        surface_judge.set_transport_for_tests(CountingTransport(0.99))
        args = _prepare_args(actors)
        _invoke(actors, "memory.surfacing.prepare", args)
        args2 = _prepare_args(actors, text="8.19我们结婚")
        from mariposa.errors import IdempotencyConflict
        with pytest.raises(IdempotencyConflict):
            _invoke(actors, "memory.surfacing.prepare", args2)

    def test_validate_memory_revised_after_prepare_zero_inject(
            self, actors, monkeypatch):
        """赢家在 validate 前被修订 → 该事件零注入，不退第二名。"""
        _policy_on(monkeypatch)
        held = _hold(actors, 0, text="7.9记忆库建成了")
        mem_id = held["memory_id"] if isinstance(held, dict) else held
        surface_judge.set_transport_for_tests(CountingTransport(0.99))
        out = _invoke(actors, "memory.surfacing.prepare", _prepare_args(actors))
        run_id = out["data"]["candidate_snapshot_ref"]
        # 修订正文（就地覆写，正文哈希变化 → 赢家过期）
        cur = registry.invoke(actors["jiaming"], "memory.get",
                              {"memory_id": mem_id}, None)["data"]
        registry.invoke(actors["jiaming"], "memory.update", {
            "memory_id": mem_id, "text": "7.9记忆库建成了（修订版）",
            "expected_version": cur["version"]}, None)
        vout = _invoke(actors, "memory.surfacing.validate", {
            "operation_id": "v3", "conversation_scope": "room-1",
            "candidate_snapshot_ref": run_id,
            "source_input_ref": {"message_id": "m1", "revision": 1},
            "target_input": {"message_id": "m2", "revision": 1,
                              "sequence": 11, "text": "7.9呢",
                              "body_hash": "b" * 64, "occurred_at": None},
            "recent_dialogue": [], "window_id": "w1",
            "deadline_at": "2999-01-01T00:00:00+00:00"})
        assert vout["data"]["items"] == []
        assert vout["data"]["reason_code"] == "SURFACE_VERSION_STALE"

    def test_validate_deadline_passed_times_out(self, actors, monkeypatch):
        _policy_on(monkeypatch)
        args = {
            "operation_id": "v-late", "conversation_scope": "room-1",
            "candidate_snapshot_ref": "sfr_missing",
            "source_input_ref": {"message_id": "m1", "revision": 1},
            "target_input": {"message_id": "m2", "revision": 1,
                              "sequence": 11, "text": "x",
                              "body_hash": "b" * 64, "occurred_at": None},
            "recent_dialogue": [], "window_id": "w1",
            "deadline_at": "2000-01-01T00:00:00+00:00"}
        out = _invoke(actors, "memory.surfacing.validate", args)
        assert out["data"]["status"] == "timed_out"

    def test_binding_allowlist_gates_surface_caps(self, actors, monkeypatch):
        """受限服务凭据只放行白名单四能力（生产 surfacing binding 形态）。"""
        _policy_on(monkeypatch)
        scoped = identity.Principal(
            "jiaming", "char", "agent", "binding", "bb",
            capabilities_allowlist=frozenset(
                {"memory.surfacing.prepare", "memory.surfacing.validate",
                 "memory.surfacing.check", "memory.detail.read"}))
        out = registry.invoke(scoped, "memory.surfacing.prepare",
                              _prepare_args(actors), None)
        assert out["ok"]
        blocked = identity.Principal(
            "jiaming", "char", "agent", "binding", "bb",
            capabilities_allowlist=frozenset({"memory.get"}))
        with pytest.raises(Forbidden):
            registry.invoke(blocked, "memory.surfacing.prepare",
                            _prepare_args(actors), None)
