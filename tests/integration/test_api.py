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
    # v1.7：退役能力对任何主体都是不可调用（404 而非 403）
    r = call(c, "worker", "memory.forgetting.decide", {"proposal_id": "nope"})
    assert r.status_code == 404
    # 保留能力：worker 无正式检索、无 owner 级 source 绑定
    assert call(c, "worker", "source.binding.bind", {}).status_code == 403


def test_retired_capabilities_uncallable(c):
    """v1.7 遗忘/审查链负向：HTTP 入口结构化拒绝且无写入。"""
    retired = ("memory.restore", "workspace.forgetting.scan",
               "workspace.forgetting.generate", "workspace.proposals.revise",
               "workspace.proposals.submit", "workspace.proposals.list",
               "workspace.proposals.withdraw", "workspace.proposals.get",
               "workspace.proposals.decide_batch",
               "memory.forgetting.decide", "memory.retention.decide",
               "memory.forgetting.request",
               "memory.forgetting.proposals.list",
               "memory.forgetting.proposals.get",
               "workspace.review.claim", "workspace.review.get",
               "workspace.review.revise", "workspace.review.submit",
               "workspace.memory.inspect")
    for name in retired:
        r = call(c, "qiaosheng", name, {"memory_id": "x", "proposal_id": "x"})
        assert r.status_code == 404, f"{name} 应已退役（404），实得 {r.status_code}"
        assert r.json()["error"]["code"] == "NOT_FOUND"




def test_unknown_capability_404(c):
    r = call(c, "qiaosheng", "exec.sql", {"q": "SELECT 1"})
    assert r.status_code == 404


