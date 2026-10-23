"""surface 四能力 HTTP 面集成（EST_MARIPOSA_AUTO_SURFACING_V1）。

跨仓合同见证：estómago MariposaClient.defaultTransport 发的正是
{arguments:{...}} + Bearer 头到 /api/capability/{name}——本文件用真实
FastAPI 栈（鉴权/限速/schema/registry 全真）+ 临时正式库 + fake 外部
judge transport（typesafe HTTP）执行 prepare→validate→check→detail
全链；E 侧真实 client/真实 request assembly 的等价链见 estómago 仓
tests/unit/surfacing-seam.test.ts（transport 边界即本合同）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mariposa import config, db
from mariposa.app import app
from mariposa.surfacing import judge as surface_judge
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def c(actors):
    reset_all()
    surface_judge.clear_transport_override()
    with TestClient(app) as client:
        yield client
    surface_judge.clear_transport_override()


def auth(pid: str) -> dict:
    return {"Authorization": f"Bearer {TOKENS[pid]}"}


def call(c, pid, capability, arguments=None):
    return c.post(f"/api/capability/{capability}",
                  json={"arguments": arguments or {}},
                  headers=auth(pid))


class _FakeJudgeTransport:
    """正向问句 0.99 / 反向问句（ambiguity_risk）0.01——全部严格通过。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        return {"answers": {k: {"noul": 0.01 if "ambiguity_risk" in k
                                else 0.99}
                            for k in payload["questions"]},
                "model": "fake"}


@pytest.fixture()
def policy_on(monkeypatch):
    monkeypatch.setattr(config, "SURFACE_ENABLED", True)
    monkeypatch.setattr(config, "SURFACE_DAILY_JUDGE_COST_CAP_USD", 5.0)
    from mariposa.recall import judge_policy
    with db.formal() as conn:
        row = conn.execute(
            "SELECT revision FROM recall_judge_policy WHERE id=1").fetchone()
        rev = (row["revision"] if row else 0) + 1
    judge_policy.update_policy("qiaosheng", expected_revision=rev,
                               enabled=True, provider="typesafe_jev",
                               model_id="fake")
    with db.formal() as conn:
        conn.execute(
            "UPDATE recall_judge_policy SET allowed_data=? WHERE id=1",
            (json.dumps({"event_excerpt": True,
                         "structured_metadata": True,
                         "current_dialogue": True}),))
    t = _FakeJudgeTransport()
    surface_judge.set_transport_for_tests(t)
    return t


LONG_TEXT = "7月9日我们把记忆库建成了。" + "那天的细节值得完整读回。" * 80


def _prepare_args(text="7.9记忆库做成了"):
    return {
        "operation_id": "it-prepare-1",
        "conversation_scope": "room-it",
        "source_input": {"message_id": "m1", "revision": 1,
                          "sequence": 10, "text": text,
                          "body_hash": "a" * 64, "occurred_at": None},
        "recent_dialogue": [],
        "event_hypotheses": [
            {"hypothesis_id": "h1", "start_cp": 0, "end_cp": 9,
             "inherited_date_span": None}],
    }


def test_surface_full_chain_over_http(c, policy_on):
    # 种一条真实记忆（真实 hold 链，非 mock）
    r = call(c, "jiaming", "memory.hold", {
        "text": LONG_TEXT, "memory_date": "2026-07-09",
        "date_confidence": "exact", "original_title": "7.9 记忆库",
        "categories": ["sweet"], "creation_mode": "contemporaneous"})
    assert r.status_code == 200, r.text
    memory_id = r.json()["data"]["memory_id"]
    # The actual HTTP credential carries only the four surfacing capabilities;
    # neither a global owner fixture nor the chat writer credential substitutes.
    with db.formal() as conn:
        conn.execute("UPDATE client_bindings SET capabilities_allowlist=? WHERE binding_id='binding_jiaming'",
                     (json.dumps(["memory.surfacing.prepare", "memory.surfacing.validate",
                                  "memory.surfacing.check", "memory.detail.read"]),))

    # prepare（E 信封原样）
    p = call(c, "jiaming", "memory.surfacing.prepare", _prepare_args())
    assert p.status_code == 200, p.text
    pdata = p.json()["data"]
    assert pdata["status"] == "prepared"
    assert pdata["selected_count"] == 1
    # P2（她 1020 二批复）：标题"7.9 记忆库"连续出现在原话 + 日期 exact PASS +
    # 证据 PASS + 日期集唯一 → 确定性准入，**prepare 阶段零 JEV 请求**；
    # 交付阶段仍按 P0 合同调 relevant_now（下方单独记账）
    judge_calls_after_prepare = policy_on.calls
    assert judge_calls_after_prepare == 0
    det = pdata["coverage"]["deterministic"]["ev_1"]
    assert det["outcome"] == "confirmed_deterministic"
    assert det["strong_match"]["kind"] == "title"
    assert det["coverage"]["date_set_truncated"] is False
    assert pdata["coverage"]["judge_requests_prepare"] == 0

    # 幂等重放：同 op 同 payload 不再付费
    p2 = call(c, "jiaming", "memory.surfacing.prepare", _prepare_args())
    assert p2.json()["data"]["surface_run_id"] == pdata["surface_run_id"]
    assert policy_on.calls == judge_calls_after_prepare

    # validate：同步同轮同修订 → ready，item=日期+完整正文+detail_ref
    v = call(c, "jiaming", "memory.surfacing.validate", {
        "operation_id": "it-validate-1",
        "conversation_scope": "room-it",
        "candidate_snapshot_ref": pdata["candidate_snapshot_ref"],
        "source_input_ref": {"message_id": "m1", "revision": 1},
        "target_input": {"message_id": "m1", "revision": 1,
                          "sequence": 10, "text": "7.9记忆库做成了",
                          "body_hash": "a" * 64, "occurred_at": None},
        "recent_dialogue": [], "window_id": "w-it",
        "deadline_at": "2999-01-01T00:00:00+00:00"})
    assert v.status_code == 200, v.text
    vdata = v.json()["data"]
    assert vdata["status"] == "ready"
    judge_calls_after_validate = policy_on.calls   # validate 的 delivery 判断是合法新增
    # 分账：prepare 0 / delivery ≥1（她 1020：不许只报一个总计）
    assert judge_calls_after_validate >= 1
    item = vdata["items"][0]
    assert item["memory_id"] == memory_id
    assert item["event_text"] == LONG_TEXT      # 全文原样，不截断
    assert item["memory_date"] == "2026-07-09"

    # check：本地校验（不增 judge 调用）
    ck = call(c, "jiaming", "memory.surfacing.check", {
        "validation_receipt": vdata["validation_receipt"],
        "conversation_scope": "room-it",
        "target_message_id": "m1", "target_revision": 1,
        "window_id": "w-it"})
    assert ck.json()["data"]["valid"] is True
    assert ck.json()["data"]["accepted_detail_refs"] == [item["detail_ref"]]
    assert policy_on.calls == judge_calls_after_validate   # check 纯本地零判断

    # detail.read：直读全量 + 分页拼回 + 不建 view_receipt
    pages = []
    cursor = None
    for _ in range(60):
        args = {"detail_ref": item["detail_ref"]}
        if cursor:
            args["cursor"] = cursor
        d = call(c, "jiaming", "memory.detail.read", args)
        assert d.status_code == 200, d.text
        page = d.json()["data"]
        pages.append(page)
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    text = "".join(
        (page["sections"].get("memory") or {}).get("text", "")
        for page in pages)
    assert text == LONG_TEXT
    with db.formal() as conn:
        n = conn.execute(
            "SELECT COUNT(*) n FROM memory_view_receipts").fetchone()["n"]
    assert n == 0
    forbidden = call(c, "jiaming", "memory.hold", {"text": "must not write"})
    assert forbidden.status_code == 403


def test_worker_principal_forbidden(c, policy_on):
    r = call(c, "worker", "memory.surfacing.prepare", _prepare_args())
    assert r.status_code == 403


def test_disabled_flag_blocks_over_http(c, monkeypatch):
    monkeypatch.setattr(config, "SURFACE_ENABLED", False)
    r = call(c, "jiaming", "memory.surfacing.prepare", _prepare_args())
    assert r.status_code == 200
    assert r.json()["data"]["reason_code"] == "SURFACE_DISABLED"


def test_schema_rejects_unknown_field_over_http(c, policy_on):
    args = _prepare_args()
    args["evil"] = 1
    r = call(c, "jiaming", "memory.surfacing.prepare", args)
    assert r.status_code in (400, 403)
    assert "SCHEMA_VIOLATION" in r.text
