"""HTTP 适配器：同一 Registry 的传输层验证。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mariposa.app import app
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def c(actors):
    with TestClient(app) as c:
        yield c


def auth(pid: str) -> dict:
    return {"Authorization": f"Bearer {TOKENS[pid]}"}


def call(c, pid, capability, arguments=None, idem=None):
    headers = auth(pid)
    if idem:
        headers["Idempotency-Key"] = idem
    return c.post(f"/api/capability/{capability}", json={"arguments": arguments or {}},
                  headers=headers)


def test_health(c):
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_unauthenticated_rejected(c):
    r = c.post("/api/capability/memory.search", json={"arguments": {"query": "x"}})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_worker_forbidden_on_search_and_decide(c):
    assert call(c, "worker", "memory.search", {"query": "x"}).status_code == 403
    r = call(c, "worker", "memory.forgetting.decide", {"proposal_id": "nope"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_unknown_capability_404(c):
    r = call(c, "qiaosheng", "exec.sql", {"q": "SELECT 1"})
    assert r.status_code == 404


def test_http_full_forget_loop(c):
    r = call(c, "jiaming", "memory.hold", {
        "text": "蓝瓷小钥匙挂在玄关第二个抽屉的挂钩上。",
        "why_remember": "钥匙位置",
        "memory_date": "2026-06-01",
        "date_confidence": "exact",
    })
    assert r.status_code == 200
    mem_id = r.json()["data"]["memory_id"]

    hits = call(c, "qiaosheng", "memory.search", {"query": "蓝瓷小钥匙"}).json()["data"]["hits"]
    assert any(h["memory_id"] == mem_id for h in hits)

    scan = call(c, "worker", "workspace.forgetting.scan", {}).json()["data"]
    prop = next(p for p in scan["created"] if p["target_memory_id"] == mem_id)

    rev = call(c, "worker", "workspace.proposals.revise", {
        "proposal_id": prop["proposal_id"],
        "compressed_summary": "一件随身小物放在玄关收纳处。",
        "reason": "琐事压缩",
    }).json()["data"]

    sub = call(c, "worker", "workspace.proposals.submit",
               {"proposal_id": prop["proposal_id"], "revision": rev["revision"]},
               idem=f"submit-{prop['proposal_id']}").json()["data"]

    dec = call(c, "jiaming", "memory.forgetting.decide", {
        "proposal_id": sub["proposal_id"], "proposal_revision": sub["revision"],
        "proposal_hash": sub["proposal_hash"],
        "expected_memory_version": sub["base_memory_version"],
        "decision": "approve",
    }, idem=f"decide-{prop['proposal_id']}")
    assert dec.status_code == 200

    hits = call(c, "qiaosheng", "memory.search", {"query": "蓝瓷小钥匙"}).json()["data"]["hits"]
    assert not any(h["memory_id"] == mem_id for h in hits)
    hits = call(c, "qiaosheng", "memory.search", {"query": "玄关"}).json()["data"]["hits"]
    hit = next(h for h in hits if h["memory_id"] == mem_id)
    assert hit["matched_by"] == "summary_keyword"

    got = call(c, "qiaosheng", "memory.get", {"memory_id": mem_id}).json()["data"]
    assert got["representation"] == "forgotten_summary"

    res = call(c, "jiaming", "memory.restore", {
        "memory_id": mem_id, "expected_current_version": 2,
    }).json()["data"]
    assert res["new_version"] == 3
    hits = call(c, "qiaosheng", "memory.search", {"query": "蓝瓷小钥匙"}).json()["data"]["hits"]
    assert any(h["memory_id"] == mem_id for h in hits)


def test_capability_listing_respects_role(c):
    names_q = {x["name"] for x in c.get("/api/capabilities", headers=auth("qiaosheng")).json()["data"]}
    names_w = {x["name"] for x in c.get("/api/capabilities", headers=auth("worker")).json()["data"]}
    assert "memory.forgetting.decide" in names_q
    assert "memory.forgetting.decide" not in names_w
    assert "workspace.proposals.submit" in names_w
