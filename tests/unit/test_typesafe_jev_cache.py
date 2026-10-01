"""TypeSafe System One v1 契约、批量精排与 Jev 派生缓存。"""
from __future__ import annotations

import json

from mariposa import config, db, schema
from mariposa.retrieval import evidence as evidence_mod
from mariposa.retrieval.judges import base, cache, typesafe_jev
from tests.conftest import reset_all


def _candidate(ref="memory:m1", version="1", text="搬家事件"):
    return {
        "candidate_ref": ref,
        "resource_ref": ref,
        "channel": "event",
        "content_version": version,
        "representation_version": version,
        "projection_version": "retrieval_projection_v1",
        "matched_by": ["keyword"],
        "matched_fields": ["event_text"],
        "evidence": [
            evidence_mod.make_evidence(
                "authored_event", "event_text", text, ref,
                source_version=version)
        ],
    }


def _plan(text="找搬家的事"):
    return {
        "original_request": text,
        "channels": ["event"],
        "lexical_terms": ["搬家"],
        "explicit_constraints": {},
        "evidence_requirement": "any",
    }


def _judge(monkeypatch):
    monkeypatch.setenv("MARIPOSA_TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("MARIPOSA_RECALL_JUDGE_ALLOWED_DATA",
                       "event_excerpt_only")
    return typesafe_jev.TypeSafeJevJudge()


def test_payload_is_system_one_v1_and_event_excerpt_is_not_empty(monkeypatch):
    judge = _judge(monkeypatch)
    payload = judge._payload(_plan(), [_candidate(text="真实事件片段")])
    assert set(payload) == {"state", "questions", "model"}
    assert payload["state"]["request"]["original_request"] == "找搬家的事"
    segs = payload["state"]["candidates"][0]["segments"]
    ev = [s for s in segs if s["field"] == "event_text"]
    assert ev and "真实事件片段" in ev[0]["text"]
    assert payload["questions"]["candidate_0"]["type"] == "noul"
    assert "question" not in payload


def test_http_parser_reads_answers_noul_and_sends_current_contract(monkeypatch):
    reset_all()
    judge = _judge(monkeypatch)
    sent = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "model": "jev-resolved-test",
                "answers": {
                    "candidate_0": {"type": "noul", "noul": 0.91},
                    "candidate_1": {"type": "noul", "noul": 0.22},
                },
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }).encode()

    def fake_urlopen(req, timeout):
        sent["payload"] = json.loads(req.data.decode())
        sent["auth"] = req.headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr(typesafe_jev.urllib.request, "urlopen", fake_urlopen)
    candidates = [
        _candidate(ref="memory:m1", text="搬家一"),
        _candidate(ref="memory:m2", text="无关二"),
    ]
    projections = [judge._candidate_projection(c) for c in candidates]
    items = judge._judge_batch(_plan(), candidates, projections)

    assert sent["payload"]["model"] == config.RECALL_JUDGE_MODEL_ID
    assert set(sent["payload"]) == {"state", "questions", "model"}
    assert set(sent["payload"]["questions"]) == {
        "candidate_0", "candidate_1"}
    assert sent["auth"] == "Bearer test-key"
    assert [i.relevance_signal for i in items] == [0.91, 0.22]
    assert all(i.model_id == "jev-resolved-test" for i in items)


def test_second_identical_rerank_is_cache_hit_without_network(monkeypatch):
    reset_all()
    judge = _judge(monkeypatch)
    calls = []

    def fake_batch(plan, candidates, projections):
        calls.append([c["candidate_ref"] for c in candidates])
        return [
            base.JudgeItem(
                candidate_ref=c["candidate_ref"],
                candidate_version=c.get("content_version"),
                relevance_signal=0.87,
                confidence_kind="not_applicable",
                evaluation_status="evaluated",
                model_id="jev-test-2026-09",
                prompt_version=config.RECALL_JUDGE_PROMPT_VERSION,
                input_projection_version=c.get("projection_version", ""),
            )
            for c in candidates
        ]

    monkeypatch.setattr(judge, "_judge_batch", fake_batch)
    ctx = {"policy_version": config.RECALL_POLICY_VERSION}
    first = judge.judge(_plan(), [_candidate()], ctx)
    second = judge.judge(_plan(), [_candidate()], ctx)

    assert len(calls) == 1
    assert first.cache_hits == 0 and first.cache_misses == 1
    assert first.request_count == 1
    assert second.cache_hits == 1 and second.cache_misses == 0
    assert second.request_count == 0
    assert second.items[0].relevance_signal == 0.87


def test_query_or_candidate_change_forces_cache_miss(monkeypatch):
    reset_all()
    judge = _judge(monkeypatch)
    calls = 0

    def fake_batch(plan, candidates, projections):
        nonlocal calls
        calls += 1
        return [
            base.JudgeItem(
                candidate_ref=c["candidate_ref"],
                candidate_version=c.get("content_version"),
                relevance_signal=0.7,
                confidence_kind="not_applicable",
                evaluation_status="evaluated",
                model_id="jev-test",
                prompt_version=config.RECALL_JUDGE_PROMPT_VERSION,
            )
            for c in candidates
        ]

    monkeypatch.setattr(judge, "_judge_batch", fake_batch)
    ctx = {"policy_version": config.RECALL_POLICY_VERSION}
    judge.judge(_plan(), [_candidate()], ctx)
    judge.judge(_plan("找另一次搬家"), [_candidate()], ctx)
    judge.judge(_plan(), [_candidate(version="2", text="修改后的事件")], ctx)
    assert calls == 3


def test_batching_reduces_http_request_count(monkeypatch):
    reset_all()
    judge = _judge(monkeypatch)
    monkeypatch.setattr(config, "RECALL_JUDGE_BATCH_SIZE", 4)
    batches = []

    def fake_batch(plan, candidates, projections):
        batches.append(len(candidates))
        return [
            base.JudgeItem(
                candidate_ref=c["candidate_ref"],
                candidate_version=c.get("content_version"),
                relevance_signal=0.6,
                confidence_kind="not_applicable",
                evaluation_status="evaluated",
                model_id="jev-test",
                prompt_version=config.RECALL_JUDGE_PROMPT_VERSION,
            )
            for c in candidates
        ]

    monkeypatch.setattr(judge, "_judge_batch", fake_batch)
    candidates = [
        _candidate(ref=f"memory:m{i}", text=f"事件{i}") for i in range(9)
    ]
    result = judge.judge(
        _plan(), candidates, {"policy_version": config.RECALL_POLICY_VERSION})
    assert batches == [4, 4, 1]
    assert result.request_count == 3
    assert len(result.items) == 9


def test_feature_cache_is_versioned_derived_metadata_only():
    reset_all()
    identity_v1 = cache.feature_identity(
        resource_ref="memory:m1",
        content_version="1",
        projection_version="retrieval_projection_v1",
        feature_name="example_dimension",
        feature_schema_version="feature-v1",
        requested_model="jev-test",
    )
    cache.put_feature(identity_v1, feature_value={"score": 0.8},
                      resolved_model="jev-test")
    assert cache.get_feature(identity_v1)["feature_value"] == {"score": 0.8}

    identity_v2 = cache.feature_identity(
        resource_ref="memory:m1",
        content_version="2",
        projection_version="retrieval_projection_v1",
        feature_name="example_dimension",
        feature_schema_version="feature-v1",
        requested_model="jev-test",
    )
    assert cache.get_feature(identity_v2) is None
    with db.recall_runtime() as conn:
        row = conn.execute(
            "SELECT * FROM jev_feature_cache WHERE feature_key=?",
            (identity_v1["feature_key"],)).fetchone()
        # 缓存表没有正文列；只留结构化 derived value 与版本元数据。
        assert "body" not in row.keys()
        assert "excerpt" not in row.keys()


def test_runtime_migration_creates_both_jev_cache_tables():
    schema.migrate_runtime()
    with db.recall_runtime() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name LIKE 'jev_%_cache'").fetchall()}
    assert names == {"jev_rerank_cache", "jev_feature_cache"}
