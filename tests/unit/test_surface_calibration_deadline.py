"""Calibration separates candidate admission from delivery; deadlines are shared."""
import time
import urllib.error
import pytest
from mariposa.surfacing import judge, contract
from mariposa.surfacing.models import surface_policy_snapshot, SurfaceBlocked
from mariposa.retrieval.judges import typesafe_jev
from tests.unit.test_surface_selection_judge import _pool_entry, _SOURCE, _HYP
from tests.conftest import reset_all

@pytest.fixture(autouse=True)
def isolated():
    reset_all()
    judge.clear_transport_override()
    yield
    judge.clear_transport_override()

def transport(same, relevant=0.93):
    def post(payload):
        return {'model': 'calibration-fixture', 'answers': {
            k: {'type': 'noul', 'noul': relevant if k.endswith('relevant_now') else same}
            for k in payload['questions']}}
    return post

def test_prepare_is_provisional_delivery_is_stricter():
    p = surface_policy_snapshot()
    judge.set_transport_for_tests(transport(0.77))
    j = judge.SurfaceJudge('typesafe_jev', p)
    d = j.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
    assert d.assessments[0].strictly_confirmed()
    d = j.judge_delivery(_SOURCE, [], _SOURCE, _HYP,
                         [{**_pool_entry(), 'event_id': 'ev_1'}])
    assert not d.assessments[0].strictly_confirmed()
    judge.set_transport_for_tests(transport(0.96, 0.87))
    fresh = judge.SurfaceJudge('typesafe_jev', p)
    d = fresh.judge_delivery(_SOURCE, [], {**_SOURCE, 'body_hash': 'fresh'}, _HYP,
                             [{**_pool_entry(), 'event_id': 'ev_1'}])
    assert d.assessments[0].strictly_confirmed()

def test_delivery_threshold_change_invalidates_cache_identity_only():
    p = surface_policy_snapshot()
    a = judge.SurfaceJudge('typesafe_jev', p)
    b = judge.SurfaceJudge('typesafe_jev', {**p, 'delivery_same_occurrence_threshold': 0.95})
    assert a._identity('delivery','q','ctx','memory:x@1') != b._identity('delivery','q','ctx','memory:x@1')

def test_expired_judge_does_not_send_even_first_request():
    judge.set_transport_for_tests(lambda p: pytest.fail('must not send'))
    j = judge.SurfaceJudge('typesafe_jev', surface_policy_snapshot(), deadline_monotonic=time.monotonic()-1)
    with pytest.raises(SurfaceBlocked, match='SURFACE_TIMEOUT'):
        j.judge_prepare(_SOURCE, [], _HYP, [_pool_entry()])
    assert j.request_count == 0

def test_late_first_result_stops_pool_and_is_not_cached(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(judge.time, 'monotonic', lambda: clock[0])
    def slow(payload):
        clock[0] = 12.0
        return transport(0.99)(payload)
    judge.set_transport_for_tests(slow)
    j = judge.SurfaceJudge('typesafe_jev', surface_policy_snapshot(), deadline_monotonic=11.0)
    with pytest.raises(SurfaceBlocked, match='SURFACE_TIMEOUT'):
        j.judge_prepare(_SOURCE, [], _HYP, [_pool_entry('memory:a'), _pool_entry('memory:b')])
    assert j.request_count == 1
    assert j.cache_hits == 0

def test_http_timeout_and_429_backoff_obey_remaining_budget(monkeypatch):
    calls = []
    def reject(req, timeout):
        calls.append(timeout)
        raise urllib.error.HTTPError(req.full_url, 429, 'rate limit', {}, None)
    monkeypatch.setattr(typesafe_jev.urllib.request, 'urlopen', reject)
    j = typesafe_jev.TypeSafeJevJudge()
    with pytest.raises(typesafe_jev.JudgeUnavailable, match='deadline_exceeded'):
        j._post_with_limited_retry({}, deadline_monotonic=time.monotonic()+0.001)
    assert len(calls) == 1
    assert 0 < calls[0] <= 0.001


def test_semantic_projection_omits_internal_identity_even_on_revision_change():
    source = {'message_id': 'm', 'revision': 1, 'body_hash': 'hash'}
    target = {**source, 'text': 'current synthetic message'}
    assert judge._state(source, [], [], target) == {'target': target['text']}
    assert 'text' not in source
    assert judge._state(source, [], [], {**target, 'revision': 2}) == {'target': target['text']}


def test_provider_failure_is_blocked_not_empty_and_does_not_continue_pool(monkeypatch):
    def fail(self, payload, **kw):
        raise typesafe_jev.JudgeUnavailable('transport_or_json')
    monkeypatch.setattr(typesafe_jev.TypeSafeJevJudge, '_post_with_limited_retry', fail)
    j = judge.SurfaceJudge('typesafe_jev', surface_policy_snapshot())
    with pytest.raises(SurfaceBlocked, match='SURFACE_UNKNOWN_OUTCOME'):
        j.judge_prepare(_SOURCE, [], _HYP, [_pool_entry('memory:a'), _pool_entry('memory:b')])
    assert j.request_count == 1


# Successful real responses only; provider failures stay in the external report.
import json
from pathlib import Path
_REPLAY = json.loads((Path(__file__).parents[1] / 'fixtures' /
                       'surface_calibration_20261021.json').read_text())

@pytest.mark.parametrize('case', _REPLAY, ids=lambda c: c['id'])
def test_real_response_replay_preserves_positive_and_negative_contract(case):
    response = {**case['response'], 'answers': {'c0_' + k.split('@',1)[1].split('_',1)[1]: v for k,v in case['response']['answers'].items()}}
    judge.set_transport_for_tests(lambda _: response)
    j = judge.SurfaceJudge('typesafe_jev', surface_policy_snapshot())
    source = {'message_id': case['id'], 'revision': 1, 'text': case['input'],
              'body_hash': case['id']}
    hypotheses = [{'hypothesis_id': 'h1', 'start_cp': 0, 'end_cp': len(case['input'])}]
    candidate = _pool_entry('memory:00001', text=case['candidate'])
    if case['expected_match']:
        decision = j.judge_delivery(source, [], source, hypotheses,
                                    [{**candidate, 'event_id': 'ev_1'}])
    else:
        decision = j.judge_prepare(source, [], hypotheses, [candidate])
    assert decision.assessments[0].strictly_confirmed() == case['expected_match']
