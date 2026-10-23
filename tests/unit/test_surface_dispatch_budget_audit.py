"""The product registry and actual HTTP retry boundary own JEV admission."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
import threading
import urllib.error

import pytest

from mariposa import config, db
from mariposa.capabilities import registry
from mariposa.surfacing import judge, store
from mariposa.surfacing.models import SurfaceBlocked, surface_policy_snapshot
from mariposa.retrieval.judges import typesafe_jev
from tests.conftest import reset_all
from tests.unit.test_surface_capability import _policy_on, _hold, _prepare_args, CountingTransport


@pytest.fixture(autouse=True)
def isolated():
    reset_all(); judge.clear_transport_override()
    yield
    judge.clear_transport_override()


def test_request_cap_applies_before_each_candidate_in_one_prepare(actors, monkeypatch):
    _policy_on(monkeypatch)
    monkeypatch.setattr(config, "SURFACE_DAILY_JUDGE_REQUEST_CAP", 1)
    monkeypatch.setattr(config, "SURFACE_JUDGE_BATCH_SIZE", 1)
    _hold(actors, 1, text="7.9记忆库建成甲", original_title="甲")
    _hold(actors, 2, text="7.9记忆库建成乙", original_title="乙")
    transport = CountingTransport()
    judge.set_transport_for_tests(transport)
    out = registry.invoke(actors["jiaming"], "memory.surfacing.prepare", _prepare_args(actors), None)["data"]
    assert transport.calls == 1
    assert out["reason_code"] == "SURFACE_BUDGET_EXHAUSTED"
    assert store.judge_usage_today()["requests"] == 1


def test_money_cap_blocks_before_the_first_provider_call(actors, monkeypatch):
    _policy_on(monkeypatch, cost_cap=0.0001)
    _hold(actors, 1)
    transport = CountingTransport()
    judge.set_transport_for_tests(transport)
    out = registry.invoke(actors["jiaming"], "memory.surfacing.prepare", _prepare_args(actors), None)["data"]
    assert transport.calls == 0
    assert out["reason_code"] == "SURFACE_BUDGET_EXHAUSTED"


def test_two_concurrent_reservations_cannot_both_take_last_request():
    policy = {**surface_policy_snapshot(), "daily_judge_request_cap": 1, "daily_judge_cost_cap_usd": 1}
    barrier = threading.Barrier(2)
    def reserve(i):
        barrier.wait()
        try: return store.reserve_judge_request(f"op-{i}", "prepare", "typesafe_jev", "jev-latest", policy)
        except SurfaceBlocked as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(reserve, range(2)))
    assert sum(isinstance(v, int) for v in values) == 1
    assert values.count("SURFACE_BUDGET_EXHAUSTED") == 1


def test_429_retry_uses_another_admission_before_transport(monkeypatch):
    calls=[]
    def reject(req, **kw):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 429, "rate limit", {}, None)
    monkeypatch.setattr(typesafe_jev.urllib.request, "urlopen", reject)
    monkeypatch.setattr(typesafe_jev.TypeSafeJevJudge, "_RETRY_BACKOFF_S", (0,0))
    policy = {**surface_policy_snapshot(), "daily_judge_request_cap": 1, "daily_judge_cost_cap_usd": 1}
    j=judge.SurfaceJudge("typesafe_jev", policy, operation_key="retry-op", phase="prepare")
    with pytest.raises(SurfaceBlocked, match="SURFACE_BUDGET_EXHAUSTED"):
        j._post({"model":"jev-latest","state":"synthetic","questions":{}})
    assert len(calls)==1 and j.request_count==1
    assert store.judge_usage_today()["requests"]==1


def test_usage_settlement_is_estimated_once_and_unknown_keeps_full_reservation():
    policy={**surface_policy_snapshot(),"daily_judge_request_cap":8,"daily_judge_cost_cap_usd":1}
    lid=store.reserve_judge_request("known","delivery","typesafe_jev","jev-latest",policy)
    assert store.settle_judge_request(lid,{"model":"jev-1.13.0","usage":{"input_tokens":1000}})
    store.record_judge_requests("known","delivery","typesafe_jev",1,None,"completed")
    assert store.judge_usage_today()["requests"]==1
    assert store.judge_usage_today()["est_cost_usd"]==pytest.approx(0.000042)
    assert store.settle_judge_request(lid,{"model":"jev-1.13.0","usage":{"input_tokens":1000}})
    unknown=store.reserve_judge_request("unknown","prepare","typesafe_jev","jev-latest",policy)
    assert not store.settle_judge_request(unknown,None)
    with db.recall_runtime() as conn:
        row=conn.execute("SELECT * FROM surface_judge_ledger WHERE ledger_id=?",(unknown,)).fetchone()
        assert row["status"]=="unknown" and row["est_cost_usd"]==row["reserved_cost_usd"]>0
        known=conn.execute("SELECT * FROM surface_judge_ledger WHERE ledger_id=?",(lid,)).fetchone()
        assert known["cost_basis"].endswith(":usage_estimate")


def test_unpriced_model_and_alias_drift_stop_further_spending():
    policy={**surface_policy_snapshot(),"daily_judge_request_cap":8,"daily_judge_cost_cap_usd":1}
    with pytest.raises(SurfaceBlocked,match="SURFACE_PRICE_UNAVAILABLE"):
        store.reserve_judge_request("unknown","prepare","typesafe_jev","unpriced-v99",policy)
    lid=store.reserve_judge_request("drift","prepare","typesafe_jev","jev-latest",policy)
    assert not store.settle_judge_request(lid,{"model":"jev-unpriced-v99","usage":{"input_tokens":10}})
    with pytest.raises(SurfaceBlocked,match="SURFACE_PRICE_UNAVAILABLE"):
        store.reserve_judge_request("after-drift","prepare","typesafe_jev","jev-latest",policy)


def test_budget_day_is_shanghai_and_counts_legacy_rows_by_timestamp(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime(2026,10,23,16,1,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(store,"datetime",FrozenDateTime)
    assert store._day()=="2026-10-24"
    assert store._day_bounds()==("2026-10-23T16:00:00+00:00","2026-10-24T16:00:00+00:00")
    store.record_judge_requests("legacy","prepare","typesafe_jev",1,0.001,"completed")
    with db.recall_runtime() as conn:
        conn.execute("UPDATE surface_judge_ledger SET day='2026-10-23'")
    assert store.judge_usage_today()["requests"]==1
