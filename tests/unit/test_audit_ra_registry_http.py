"""复审 Registry/HTTP 域修复回归（RA-008/009/010）。"""
from __future__ import annotations

import pytest

from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors):
    return memory.hold(actors["jiaming"], text="注册表面文",
                       memory_date="2026-09-25", date_confidence="exact",
                       original_title="r", categories=["daily"],
                       creation_mode="contemporaneous", raw_pending=False)


class TestBadInputEdge:

    @staticmethod
    def _client():
        from fastapi.testclient import TestClient
        from mariposa.app import app
        return TestClient(app)

    def test_mcp_bad_content_length(self, actors):
        with self._client() as c:
            r = c.post("/mcp", content=b"{}",
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}",
                                "Content-Length": "bad"})
        body = r.json()
        assert r.status_code == 200
        assert body.get("error", {}).get("code") == -32600, \
            "坏 CL 是 -32600 不是 500（RA-008）"

    def test_media_stage_bad_content_length(self, actors):
        with self._client() as c:
            r = c.put("/api/media/stage/whatever", content=b"x",
                      headers={"Authorization":
                               f"Bearer {TOKENS['jiaming']}",
                               "Content-Length": "bad"})
        assert r.status_code == 400, \
            f"Media 坏 CL 是 400 不是 500：{r.status_code}"

    def test_mcp_false_arguments_no_side_effect(self, actors):
        with self._client() as c:
            r = c.post("/mcp",
                       content=(b'{"id":1,"method":"tools/call",'
                                b'"params":{"name":"presence.touch",'
                                b'"arguments":false}}'),
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}"})
        body = r.json()
        assert body.get("error", {}).get("code") == -32600, \
            "arguments=false 必须 -32600，不得经 or {} 执行成写请求"
        from mariposa import db
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM activity_events").fetchone()["c"]
        assert n == 0, "被拒 RPC 不得产生副作用"


class TestMcpSerialization:

    @staticmethod
    def _client():
        from fastapi.testclient import TestClient
        from mariposa.app import app
        return TestClient(app)

    def test_time_now_via_mcp_not_500(self, actors):
        with self._client() as c:
            r = c.post("/mcp",
                       content=(b'{"id":1,"method":"tools/call",'
                                b'"params":{"name":"time.now",'
                                b'"arguments":{}}}'),
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}"})
        assert r.status_code == 200
        body = r.json()
        assert not body.get("error"), f"MCP time.now 不可 500：{body}"
        assert body["result"]["isError"] is False


class TestDeadHandlersRewired:

    def test_tags_add_and_by_emotion(self, actors):
        m = _hold(actors)
        out = registry.invoke(actors["jiaming"], "memory.tags.add", {
            "memory_id": m["memory_id"],
            "tags": ["registry测试"]}, None)
        assert out["ok"] is True
        found = registry.invoke(actors["jiaming"], "memory.by_emotion", {
            "tag": "开心"}, None)  # by_emotion 走 mood 标签（词表内）
        assert found["ok"] is True, "by_emotion 不再 NameError（RA-010）"

    def test_settings_get_no_attribute_error(self, actors):
        out = registry.invoke(actors["jiaming"],
                              "maintenance.settings.get", {}, None)
        assert out["ok"] is True
        assert "bootstrap" in out["data"]
