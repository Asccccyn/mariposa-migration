"""1005B 第二批修复回归（信封协商 / tools/list 占位隐藏 / 迁移 8）。

- 信封：content+structuredContent 双份默认保留（MCP 兼容通道），
  显式 params._meta.content_envelope="single" 才省略完整 JSON 副本；
  序列化统一紧凑分隔符；
- tools/list：[blocked]/[reserved] 占位不再出现在 MCP 发现面
  （能力仍注册，HTTP capabilities.list 照旧）；
- WORKSPACE 迁移链压缩为迁移 8（fresh 库净效果等价）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mariposa.app import app
from mariposa.capabilities import registry, v1_compat
from mariposa.identity import service as identity
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def c(actors):
    with TestClient(app) as client:
        yield client


def _call(c, params):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 7,
                                "method": "tools/call",
                                "params": params},
                  headers={"Authorization": f"Bearer {TOKENS['qiaosheng']}"})


class TestEnvelopeNegotiation:
    def test_default_both_copies_compact(self, c):
        """默认双份（兼容）；文本副本与结构化副本同数据、紧凑分隔。"""
        r = _call(c, {"name": "mariposa_time_now", "arguments": {}})
        result = r.json()["result"]
        sc = result["structuredContent"]
        text = result["content"][0]["text"]
        assert sc["ok"] is True
        assert json.loads(text) == sc, "文本副本=同一负载"
        assert '": ' not in text and '", "' not in text, "紧凑分隔符生效"

    def test_single_envelope_by_meta(self, c):
        """显式 _meta.content_envelope=single：content 只留指针行。"""
        r = _call(c, {"name": "mariposa_time_now", "arguments": {},
                      "_meta": {"content_envelope": "single"}})
        result = r.json()["result"]
        assert result["structuredContent"]["ok"] is True
        assert result["content"][0]["text"] == "see structuredContent"

    def test_initialize_declares_negotiation(self, c):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                 "method": "initialize", "params": {}},
                   headers={"Authorization": f"Bearer {TOKENS['qiaosheng']}"})
        instr = r.json()["result"]["instructions"]
        assert "content_envelope" in instr


class TestToolsListNoise:
    def test_blocked_reserved_hidden_from_mcp(self, c):
        """占位工具不进 MCP 发现面；HTTP capabilities.list 仍可见
        （能力未注销，负测可验证）。"""
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                 "method": "tools/list", "params": {}},
                   headers={"Authorization": f"Bearer {TOKENS['qiaosheng']}"})
        names = [t["name"] for t in r.json()["result"]["tools"]]
        from mariposa.capabilities.mcp_adapter import _transport_name
        placeholders = {_transport_name(cap.name)
                        for cap in registry.REGISTRY.values()
                        if cap.description.startswith(("[blocked]",
                                                        "[reserved]"))}
        assert placeholders, "前提：v1_compat 注册了占位"
        assert not (set(names) & placeholders), \
            f"MCP 发现面不得含占位：{set(names) & placeholders}"


class TestWorkspaceMigration8:
    def test_fresh_db_single_migration_zero_dead_tables(self, tmp_path):
        """fresh 库：WORKSPACE 链只剩迁移 8，净效果=零死表。
        （子进程跑——config 路径在 import 时固化，进程内换根无效）"""
        import subprocess
        import sys
        root = tmp_path / "ws-mig8"
        script = tmp_path / "mig8_probe.py"
        script.write_text(
            "import sys, json, sqlite3\n"
            "sys.path.insert(0, 'backend')\n"
            "import os\n"
            f"os.environ['MARIPOSA_ROOT'] = {str(root)!r}\n"
            "os.environ['MARIPOSA_ALLOW_CREATE'] = '1'\n"
            "from mariposa import schema\n"
            "schema.migrate()\n"
            "db = sqlite3.connect(os.path.join(\n"
            "    os.environ['MARIPOSA_ROOT'],\n"
            "    'runtime/workspace/workspace.sqlite3'))\n"
            "applied = sorted(r[0] for r in db.execute(\n"
            "    'SELECT version FROM schema_migrations'))\n"
            "dead = [r[0] for r in db.execute(\n"
            "    \"SELECT name FROM sqlite_master WHERE type='table' AND \"\n"
            "    \"(name LIKE 'recall_%' OR name='workspace_task_leases' \"\n"
            "    \"OR name LIKE 'v2_%')\")]\n"
            "print(json.dumps({'applied': applied, 'dead': dead}))\n",
            encoding="utf-8")
        out = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[-800:]
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        assert payload["applied"] == [8], payload
        assert payload["dead"] == [], payload
