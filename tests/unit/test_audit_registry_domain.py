"""Registry/HTTP 域审计修复回归（2026-10-02 基线审计 P2 批）。

- CB-048：HTTP/MCP 坏类型与非法编码 → 结构化 400 / JSON-RPC -32600
  （http_mcp_malformed——body=[]/null、params=[1]、method=1、0xff
  字节、坏 Content-Length 均曾 500）。
- CB-050：全部 write 能力具备严格 schema（pin_string_false——
  "false" 字符串曾被 bool() 强转真实写入 pinned=1）。
- CB-051：退役表引用清除（raw_refs_residue / retired_maintenance_
  jobs——fresh schema 上合法入口不再 no such table 崩溃）。
"""
from __future__ import annotations

import pytest

from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
    }


def _hold(actors):
    return memory.hold(actors["jiaming"], text="注册表域正文",
                       memory_date="2026-09-25", date_confidence="exact",
                       original_title="r", categories=["daily"],
                       creation_mode="contemporaneous", raw_pending=False)


# ---------------------------------------------------------------- CB-048

class TestMalformedEnvelopes:

    def _client(self):
        from fastapi.testclient import TestClient
        from mariposa.app import app
        return TestClient(app)

    def test_http_bad_content_length(self, actors):
        with self._client() as c:
            r = c.post("/api/capability/memory.search",
                       content=b"{}",
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}",
                                "Content-Length": "bad"})
        assert r.status_code == 400, \
            f"坏 Content-Length 是坏请求不是 500：{r.status_code}"

    def test_http_invalid_utf8(self, actors):
        with self._client() as c:
            r = c.post("/api/capability/memory.search",
                       content=b"\xff\xfe{}",
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}"})
        assert r.status_code == 400, \
            f"非法 UTF-8 是坏请求不是 500：{r.status_code}"

    def test_mcp_non_object_bodies(self, actors):
        with self._client() as c:
            for bad in (b"[]", b"null", b"42"):
                r = c.post("/mcp", content=bad,
                           headers={"Authorization":
                                    f"Bearer {TOKENS['jiaming']}"})
                body = r.json()
                assert r.status_code == 200
                assert body.get("error", {}).get("code") == -32600, \
                    f"非 object envelope 必须 -32600：{body}"

    def test_mcp_bad_method_and_params(self, actors):
        with self._client() as c:
            r = c.post("/mcp", content=b'{"id":1,"method":1}',
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}"})
            assert r.json()["error"]["code"] == -32600
            r2 = c.post("/mcp",
                        content=(b'{"id":2,"method":"tools/call",'
                                 b'"params":[1]}'),
                        headers={"Authorization":
                                 f"Bearer {TOKENS['jiaming']}"})
            assert r2.json()["error"]["code"] == -32600
            r3 = c.post("/mcp",
                        content=(b'{"id":3,"method":"tools/call",'
                                 b'"params":{"name":"memory.search",'
                                 b'"arguments":[1]}}'),
                        headers={"Authorization":
                                 f"Bearer {TOKENS['jiaming']}"})
            assert r3.json()["error"]["code"] == -32600


# ---------------------------------------------------------------- CB-050

class TestWriteSchemaCoverage:

    def test_all_write_capabilities_have_schema(self):
        from mariposa.capabilities import input_schemas
        missing = [n for n, c in registry.REGISTRY.items()
                   if c.write and not input_schemas.schema_for(n)]
        assert missing == [], f"write 能力不得缺 schema：{missing}"

    def test_pin_string_false_rejected(self, actors):
        """审计反例 pin_string_false：value="false" 曾被 bool() 强转
        真实写入 pinned=1。"""
        m = _hold(actors)
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"], "memory.pin",
                            {"memory_id": m["memory_id"],
                             "value": "false"}, None)
        from mariposa import db
        with db.formal() as conn:
            pinned = conn.execute(
                "SELECT pinned FROM memories WHERE memory_id=?",
                (m["memory_id"],)).fetchone()["pinned"]
        assert pinned == 0, "被拒请求不得产生写副作用"

    def test_pin_boolean_still_works(self, actors):
        m = _hold(actors)
        out = registry.invoke(actors["jiaming"], "memory.pin",
                              {"memory_id": m["memory_id"],
                               "value": True}, None)
        assert out["ok"] is True
        from mariposa import db
        with db.formal() as conn:
            pinned = conn.execute(
                "SELECT pinned FROM memories WHERE memory_id=?",
                (m["memory_id"],)).fetchone()["pinned"]
        assert pinned == 1


# ---------------------------------------------------------------- CB-051

class TestRetiredTableResidue:

    def test_hold_raw_refs_rejected(self, actors):
        with pytest.raises(Forbidden):
            memory.hold(actors["jiaming"], text="退役参数正文",
                        memory_date="2026-09-25", date_confidence="exact",
                        original_title="r", categories=["daily"],
                        creation_mode="contemporaneous",
                        raw_refs=[{"conversation_id": "c",
                                   "start_message_id": "a",
                                   "end_message_id": "b",
                                   "source_hash": "x" * 64}])

    def test_jobs_status_on_fresh_schema(self, actors):
        from mariposa.maintenance import service as maint
        out = maint.jobs_status()
        assert "import_jobs" in out
        assert "active_leases" not in out, "退役 lease 指标不得再查询"
        out2 = registry.invoke(actors["jiaming"],
                               "maintenance.jobs.status", {}, None)
        assert out2["ok"] is True
