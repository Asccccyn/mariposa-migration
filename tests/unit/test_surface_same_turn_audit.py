"""2026-10-23: current same-turn contract, no paid providers or model loads."""
from datetime import datetime, timedelta, timezone
import hashlib
import socket

import pytest

from mariposa import config, db
from mariposa.capabilities import registry
from mariposa.surfacing import service, judge, facts, store
from mariposa.errors import Busy, OutcomeUnknown
from tests.unit.test_surface_capability import _policy_on, _hold, CountingTransport


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("external network forbidden"))
    judge.clear_transport_override()
    yield
    judge.clear_transport_override()


def source():
    text = "你还记得2026年7月9日记忆库建成那件事吗？"
    return {"message_id": "same-turn", "revision": 1, "sequence": 1,
            "text": text, "body_hash": hashlib.sha256(text.encode()).hexdigest(), "occurred_at": None}


def prepare_args():
    src = source()
    return {"operation_id": "audit-prepare", "conversation_scope": "audit-room",
            "source_input": src, "recent_dialogue": [], "event_hypotheses": [
                {"hypothesis_id": "h1", "start_cp": 0, "end_cp": len(src["text"]), "inherited_date_span": None}]}


def invoke(actors, name, args):
    return registry.invoke(actors["jiaming"], name, args, None)["data"]


def prepared(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, 1, text="2026年7月9日记忆库建成了。", original_title="记忆库建成")
    transport = CountingTransport()
    judge.set_transport_for_tests(transport)
    p = invoke(actors, "memory.surfacing.prepare", prepare_args())
    assert p["status"] == "prepared"
    args = {"operation_id": "audit-validate", "conversation_scope": "audit-room",
            "candidate_snapshot_ref": p["candidate_snapshot_ref"],
            "source_input_ref": {"message_id": source()["message_id"], "revision": 1},
            "target_input": source(), "recent_dialogue": [], "window_id": "w-audit",
            "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()}
    return transport, args


def test_expired_prepare_total_deadline_prevents_retrieval(actors, monkeypatch):
    _policy_on(monkeypatch)
    monkeypatch.setattr(service.cand_mod, "tiered_partition_retrieval", lambda *a, **k: pytest.fail("expired retrieval ran"))
    args = {**prepare_args(), "deadline_at": "2000-01-01T00:00:00+00:00"}
    result = invoke(actors, "memory.surfacing.prepare", args)
    assert result["reason_code"] == "SURFACE_TIMEOUT"


def test_prepare_remaining_budget_covers_retrieval_and_judge(actors, monkeypatch):
    _policy_on(monkeypatch)
    _hold(actors, 1, text="2026年7月9日记忆库建成了。", original_title="记忆库建成")
    real = service.cand_mod.tiered_partition_retrieval
    clock = [100.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    def slow(*a, **kw):
        result = real(*a, **kw)
        clock[0] += 2.0
        return result
    monkeypatch.setattr(service.cand_mod, "tiered_partition_retrieval", slow)
    judge.set_transport_for_tests(lambda _: pytest.fail("late judge ran"))
    args = {**prepare_args(), "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}
    result = invoke(actors, "memory.surfacing.prepare", args)
    assert result["reason_code"] == "SURFACE_TIMEOUT"
    assert result["selected_count"] == 0


@pytest.mark.parametrize("change", ["source_ref", "target_id", "target_revision", "target_hash"])
def test_validate_rejects_rebinding_without_another_judge_call(actors, monkeypatch, change):
    transport, args = prepared(actors, monkeypatch)
    if change == "source_ref": args["source_input_ref"]["message_id"] = "another-source"
    elif change == "target_id": args["target_input"]["message_id"] = "next-turn"
    elif change == "target_revision": args["target_input"]["revision"] = 2
    else: args["target_input"]["body_hash"] = "b" * 64
    before = transport.calls
    result = invoke(actors, "memory.surfacing.validate", args)
    assert result["status"] == "rejected"
    assert result["reason_code"] == "SURFACE_TARGET_MISMATCH"
    assert transport.calls == before


def test_final_check_honors_revoked_current_dialogue_grant(actors, monkeypatch):
    transport, args = prepared(actors, monkeypatch)
    result = invoke(actors, "memory.surfacing.validate", args)
    assert result["status"] == "ready"
    before = transport.calls
    with db.formal() as conn:
        conn.execute("UPDATE recall_judge_policy SET allowed_data='{}' WHERE id=1")
    checked = invoke(actors, "memory.surfacing.check", {
        "validation_receipt": result["validation_receipt"], "conversation_scope": "audit-room",
        "target_message_id": source()["message_id"], "target_revision": 1, "window_id": "w-audit"})
    assert checked["valid"] is False
    assert checked["reason_code"] == "SURFACE_OUTBOUND_NOT_GRANTED"
    assert transport.calls == before


@pytest.mark.parametrize("state,expected", [("running", Busy), ("unknown", OutcomeUnknown)])
def test_validate_inflight_or_unknown_operation_never_runs_judge_again(actors, monkeypatch, state, expected):
    transport, args = prepared(actors, monkeypatch)
    op_key = "surface:validate:" + args["operation_id"]
    assert store.claim_operation("jiaming", op_key, store.payload_hash(args)) is None
    if state == "unknown":
        store.mark_unknown("jiaming", op_key)
    before = transport.calls
    with pytest.raises(expected):
        invoke(actors, "memory.surfacing.validate", args)
    assert transport.calls == before


def test_inherited_date_is_a_real_constraint_not_ignored():
    text = "2026年7月9日记忆库建成，也是结婚纪念日。"
    split = text.index("，") + 1
    hypotheses = [
        {"hypothesis_id": "h1", "start_cp": 0, "end_cp": split, "inherited_date_span": None},
        {"hypothesis_id": "h2", "start_cp": split, "end_cp": len(text),
         "inherited_date_span": {"start_cp": 0, "end_cp": len("2026年7月9日")}}]
    groups = judge.partition_hypotheses({"text": text}, hypotheses)
    constraint = groups[1].constraints
    assert constraint["dates"] == [{"year": 2026, "month": 7, "day": 9}]
    verdict = facts.hard_checks({"memory_date": "2026-08-19"}, constraint)
    assert verdict["time"]["status"] == "conflict"


def test_invalid_inherited_span_cannot_manufacture_a_date():
    text = "2026年7月9日记忆库建成，也是结婚纪念日。"
    hypotheses = [{"hypothesis_id": "h1", "start_cp": 12, "end_cp": len(text),
                   "inherited_date_span": {"start_cp": -100, "end_cp": 999}}]
    with pytest.raises(Exception, match="SURFACE_EVENT_AMBIGUOUS"):
        judge.partition_hypotheses({"text": text}, hypotheses)
