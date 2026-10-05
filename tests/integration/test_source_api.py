"""Source Layer HTTP / MCP 传输层集成测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mariposa.app import app
from tests.conftest import TOKENS, reset_all

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_export"


@pytest.fixture()
def c(actors):
    with TestClient(app) as client:
        yield client


def auth(pid: str) -> dict:
    return {"Authorization": f"Bearer {TOKENS[pid]}"}


def call(c, pid, capability, arguments=None):
    return c.post(f"/api/capability/{capability}",
                  json={"arguments": arguments or {}}, headers=auth(pid))


def upload(c, pid, name="standard.json") -> dict:
    data = (FIXTURES / name).read_bytes()
    r = c.put("/api/source/upload", content=data, headers=auth(pid))
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_upload_requires_auth(c):
    r = c.put("/api/source/upload", content=b"[]")
    assert r.status_code == 401


def test_upload_worker_forbidden(c):
    r = c.put("/api/source/upload", content=b"[]", headers=auth("worker"))
    assert r.status_code == 403


def test_full_http_flow(c):
    up = upload(c, "jiaming")
    assert up["bytes"] > 0

    r = call(c, "jiaming", "source.import",
             {"path": up["upload_path"], "filename": "standard.json"})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["status"] == "completed"
    assert body["stats"]["messages_new"] == 2
    batch_id = body["batch_id"]

    r = call(c, "jiaming", "source.import.status", {"batch_id": batch_id})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "completed"

    r = call(c, "jiaming", "source.search", {"query": "海边"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["hits"]) == 1
    assert data["source"] == "source_layer"

    r = call(c, "jiaming", "source.conversations.list", {})
    convs = r.json()["data"]["conversations"]
    assert convs and convs[0]["provider_conversation_id"] == "conv-std-001"

    r = call(c, "jiaming", "source.conversation.get",
             {"conversation_id": "conv-std-001", "limit": 10})
    assert len(r.json()["data"]["messages"]) == 2

    r = call(c, "jiaming", "source.message.get",
             {"provider_message_id": "std-m2", "context": 3})
    msg = r.json()["data"]["message"]
    assert msg["speaker_display"] == "周家明"

    # memory 绑定 → 动态打开原文
    r = call(c, "jiaming", "memory.hold", {
        "text": "海边周末计划", "why_remember": "集成测试",
        "memory_date": "2026-03-01", "date_confidence": "exact",
        "categories": ["date"]})
    mem_id = r.json()["data"]["memory_id"]
    r = call(c, "jiaming", "source.binding.bind", {
        "memory_id": mem_id, "conversation_id": "conv-std-001",
        "start_message_id": "std-m1", "end_message_id": "std-m2"})
    assert r.status_code == 200
    r = call(c, "jiaming", "source.memory.open", {"memory_id": mem_id})
    ranges = r.json()["data"]["ranges"]
    assert len(ranges) == 1 and len(ranges[0]["messages"]) == 2


def test_worker_forbidden_on_source_search(c):
    upload(c, "jiaming")
    r = call(c, "worker", "source.search", {"query": "x"})
    assert r.status_code == 403
    r = call(c, "worker", "source.import", {"path": "/etc/hosts"})
    assert r.status_code == 403


def test_source_search_is_not_memory_search(c):
    upload(c, "jiaming")
    call(c, "jiaming", "source.import",
         {"path": str(FIXTURES / "standard.json")})
    # 普通 memory 检索不应命中原文内容
    r = call(c, "jiaming", "memory.search", {"query": "海边"})
    hits = r.json()["data"]["hits"]
    assert not any("海边" in json.dumps(h, ensure_ascii=False) for h in hits)


def test_mcp_tools_expose_source(c):
    r = c.post("/mcp", headers=auth("jiaming"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "mariposa_source_search" in names
    assert "mariposa_source_import" in names
    assert "mariposa_source_memory_open" in names

    # MCP 调用同 Registry 同权限
    r = c.post("/mcp", headers=auth("jiaming"), json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "mariposa_source_search",
                   "arguments": {"query": "不存在词"}}})
    body = r.json()
    assert body["result"]["content"][0]["text"].startswith("{")

    # worker 的 MCP 工具列表只含 live ingest 两个窄能力（WP1 迁移 30
    # 起 source.ingest/status 属 worker；真调用还需 stream grant 绑定
    # 同一 binding）——owner 的其余 source 工具（import/search/绑定）
    # 仍不泄露给 worker
    r = c.post("/mcp/maintenance", headers=auth("worker"), json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    worker_source = {n for n in names if n.startswith("mariposa_source_")}
    assert worker_source == {"mariposa_source_ingest",
                             "mariposa_source_ingest_status"}


def test_upload_broken_json_records_failed(c, tmp_path):
    data = (FIXTURES / "broken.json").read_bytes()
    r = c.put("/api/source/upload", content=data, headers=auth("jiaming"))
    up = r.json()["data"]
    r = call(c, "jiaming", "source.import", {"path": up["upload_path"],
                                             "filename": "broken.json"})
    assert r.status_code == 400  # 结构化拒绝，不得静默
    r = call(c, "jiaming", "source.import.batches", {})
    batches = r.json()["data"]["batches"]
    assert batches[0]["status"] == "failed" and batches[0]["error"]
