"""召回运行时 MCP/HTTP 等权（SESSION-01/02 / §2）。

Chat 与 CC 同一套 MCP / HTTP 领域契约；相同主体相同请求得到相同语义。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mariposa.app import app
from mariposa.memory import service as memory
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def c(actors):
    with TestClient(app) as client:
        yield client


def rpc(c, method, pid, params=None, msg_id=1):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": msg_id,
                                "method": method, "params": params or {}},
                  headers={"Authorization": f"Bearer {TOKENS[pid]}"})


def tool(c, pid, name, arguments, msg_id=1):
    r = rpc(c, "tools/call", pid, {"name": name, "arguments": arguments},
            msg_id)
    result = r.json()["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]["data"]


@pytest.fixture()
def seeded(actors):
    memory.hold(actors["jiaming"], text="八月搬家事件", memory_date="2026-08-10",
                date_confidence="exact", original_title="t",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False,
                our_words=[{"speaker": "qiaosheng", "text": "搬家说好一起挑窗帘",
                            "expression_kind": "verbatim"}])
    # scope 内更早锚（审计 2026-10-03：refine 收窄到八月后，earlier
    # 的合法目标是八月内更早事件；七月桶越界返回已被 scope 门拦截，
    # 旧断言期待七月桶与现行语义冲突，按语义修测）
    memory.hold(actors["jiaming"], text="八月上旬搬家准备", memory_date="2026-08-03",
                date_confidence="exact", original_title="t-aug-early",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    memory.hold(actors["jiaming"], text="九月搬家事件", memory_date="2026-09-10",
                date_confidence="exact", original_title="t2",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    memory.hold(actors["jiaming"], text="七月更早事件", memory_date="2026-07-15",
                date_confidence="exact", original_title="t3",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)


def test_session01_mcp_full_loop(c, seeded):
    """SESSION-01：Chat 通过 MCP 独立完成 start→reject→refine→navigate→
    evidence→close，不依赖 estómago。"""
    packet = tool(c, "jiaming", "mariposa_memory_recall_start", {
        "operation_id": "op-par-s1-start",
        "query_plan": {"original_request": "找搬家的事",
                       "channels": ["event"],
                       "lexical_terms": ["搬家"]}})["data"]
    sid = packet["recall_session_id"]
    assert packet["candidates"]
    newest = max(packet["candidates"], key=lambda x: x.get("memory_date")
                 or "")
    tool(c, "jiaming", "mariposa_memory_recall_reject", {
        "session_id": sid, "operation_id": "op-par-s1-reject",
        "resource_ref": newest["resource_ref"],
        "reject_target": "event"})
    p2 = tool(c, "jiaming", "mariposa_memory_recall_refine", {
        "session_id": sid, "operation_id": "op-par-s1-refine",
        "query_plan": {"original_request": "再找八月的搬家",
                       "channels": ["event"], "lexical_terms": ["搬家"],
                       "explicit_constraints": {
                           "event_date": {"from": "2026-08-01",
                                          "to": "2026-08-31"}}}})["data"]
    assert newest["resource_ref"] not in [x["resource_ref"]
                                          for x in p2["candidates"]]
    nav = tool(c, "jiaming", "mariposa_memory_recall_navigate", {
        "session_id": sid, "direction": "earlier",
        "operation_id": "op-par-s1-nav"})["data"]
    assert nav["candidates"], "scope 内应有更早的八月事件"
    assert all((x.get("memory_date") or "") >= "2026-08-01"
               for x in nav["candidates"]), \
        "earlier 不得越过 refine 的八月 scope 返回七月桶"
    st = tool(c, "jiaming", "mariposa_memory_recall_status",
              {"session_id": sid})
    assert st["receipts_revalidated"]["checked"] >= 1
    closed = tool(c, "jiaming", "mariposa_memory_recall_close", {
        "session_id": sid, "outcome": "resolved",
        "operation_id": "op-par-s1-close"})["data"]
    assert closed["status"] == "RESOLVED"


def test_session02_http_mcp_same_semantics(c, seeded):
    """SESSION-02：HTTP 与 MCP 同能力同结果（等权入口）。"""
    args = {"query": "搬家", "operation_id": "op-par-s2-words"}
    http = c.post("/api/capability/memory.words.recall",
                  json={"arguments": args},
                  headers={"Authorization": f"Bearer {TOKENS['jiaming']}"}
                  ).json()["data"]["data"]
    mcp = tool(c, "jiaming", "mariposa_memory_words_recall", args)["data"]
    # 统一入口每次建短期 session：候选等价性按稳定字段比较
    # （version_receipt/judge 回执为每次随机）
    def norm(cands):
        return [{k: v for k, v in c.items()
                 if k not in ("version_receipt", "judge")}
                for c in cands]
    assert norm(http["candidates"]) == norm(mcp["candidates"])
    assert http["instruction_authority"] == "none"
    # v1.7：WIDE 阶段 event 通道含 our_words 字段 → 两入口一致命中
    ev = tool(c, "jiaming", "mariposa_memory_recall_start", {
        "operation_id": "op-par-s2-start",
        "query_plan": {"original_request": "窗帘", "channels": ["event"],
                       "lexical_terms": ["窗帘"]}})["data"]
    assert ev["candidates"], "WIDE 六入口应含我们的话（HTTP/MCP 一致）"


def test_worker_cannot_use_recall_runtime(c, actors):
    """召回运行时对 worker 关闭（主体权限等权不等于全员开放）。"""
    r = rpc(c, "tools/call", "worker", {
        "name": "mariposa_memory_recall_start",
        "arguments": {"query_plan": {
            "original_request": "x", "channels": ["event"],
            "lexical_terms": ["x"]}}})
    body = r.json()
    # worker 无业务 profile 绑定：传输层拒绝或工具错误，二选一都算拒绝
    assert body.get("error", {}).get("code") == -32002 or \
        body.get("result", {}).get("isError") is True
