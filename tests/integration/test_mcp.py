"""MCP JSON-RPC 适配层：与 HTTP 同一 Registry、同权限、传输名映射。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mariposa.app import app
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def c(actors):
    with TestClient(app) as client:
        yield client


def rpc(c, path, method, pid, params=None, msg_id=1):
    return c.post(path, json={"jsonrpc": "2.0", "id": msg_id, "method": method,
                              "params": params or {}},
                  headers={"Authorization": f"Bearer {TOKENS[pid]}"})


def test_initialize_and_tools_list(c):
    r = rpc(c, "/mcp", "initialize", "jiaming")
    body = r.json()
    assert body["result"]["serverInfo"]["name"] == "mariposa"
    r = rpc(c, "/mcp", "tools/list", "jiaming")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "mariposa_memory_search" in names
    assert "mariposa_memory_forgetting_decide" in names
    assert "mariposa_workspace_proposals_submit" in names


def test_tools_list_filtered_by_principal(c):
    r = rpc(c, "/mcp/maintenance", "tools/list", "worker")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "mariposa_workspace_proposals_submit" in names
    assert "mariposa_memory_search" not in names  # 工具人无正式检索
    assert "mariposa_memory_forgetting_decide" not in names


def test_business_profile_rejects_worker_binding(c):
    r = rpc(c, "/mcp", "tools/list", "worker")
    assert r.json()["error"]["code"] == -32002


def test_maintenance_profile_rejects_owner_binding(c):
    r = rpc(c, "/mcp/maintenance", "tools/list", "qiaosheng")
    assert r.json()["error"]["code"] == -32002


def test_tools_call_same_handler_as_http(c):
    r = rpc(c, "/mcp", "tools/call", "jiaming", {
        "name": "mariposa_memory_hold",
        "arguments": {"text": "MCP 与 HTTP 共用 handler 的验证桶",
                      "memory_date": "2026-06-01"},
    })
    out = r.json()["result"]
    assert out["isError"] is False
    mem_id = out["structuredContent"]["data"]["memory_id"]

    r = rpc(c, "/mcp", "tools/call", "jiaming", {
        "name": "mariposa_memory_search",
        "arguments": {"query": "共用 handler"},
    })
    hits = r.json()["result"]["structuredContent"]["data"]["hits"]
    assert any(h["memory_id"] == mem_id for h in hits)


def test_business_error_is_tool_error_not_protocol_error(c):
    r = rpc(c, "/mcp", "tools/call", "jiaming", {
        "name": "mariposa_memory_get", "arguments": {"memory_id": "mem_missing"},
    })
    result = r.json()["result"]
    assert result["isError"] is True
    assert "NOT_FOUND" in result["content"][0]["text"]


def test_unauthenticated(c):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_notification_returns_202(c):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={"Authorization": f"Bearer {TOKENS['jiaming']}"})
    assert r.status_code == 202
