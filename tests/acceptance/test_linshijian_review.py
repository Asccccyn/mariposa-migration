"""林石见独立复审问题修复的回归测试（八项）。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from mariposa import db
from mariposa.app import app
from mariposa.capabilities import mcp_adapter
from mariposa.capabilities import registry
from mariposa.capabilities.input_schemas import validate
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


@pytest.fixture()
def c(actors):
    with TestClient(app) as client:
        yield client


class TestMCPNameMapping:
    """[高] 三段能力名 roundtrip（52 个曾全部反解错误）。"""

    def test_all_capabilities_roundtrip(self):
        for name in registry.REGISTRY:
            assert mcp_adapter._canonical_name(mcp_adapter._transport_name(name)) \
                == name, name

    def test_mcp_call_three_segment_capability(self, c):
        r = c.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "mariposa_workspace_proposals_list",
                       "arguments": {}}},
            headers={"Authorization": f"Bearer {TOKENS['jiaming']}"})
        result = r.json()["result"]
        assert result["isError"] is False  # 不再 unknown capability

    def test_tools_list_carries_strict_schemas(self, c):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                 "method": "tools/list", "params": {}},
                   headers={"Authorization": f"Bearer {TOKENS['jiaming']}"})
        tools = {t["name"]: t for t in r.json()["result"]["tools"]}
        hold = tools["mariposa_memory_hold"]["inputSchema"]
        assert hold.get("additionalProperties") is False
        assert "required" in hold and hold["properties"]


class TestIdempotencyRace:
    """[中高] 同 key 并发：只执行一次副作用。"""

    def test_concurrent_same_key_single_side_effect(self, actors):
        args = {"text": "并发幂等桶", "memory_date": "2026-06-01", "date_confidence": "exact", "raw_pending": False}

        def run(_):
            try:
                out = registry.invoke(actors["jiaming"], "memory.hold", args,
                                      "race-key-1")
                return out["data"]["memory_id"]
            except Exception as e:
                return f"ERR:{getattr(e, 'code', type(e).__name__)}"

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(run, range(6)))
        ids = {r for r in results if not str(r).startswith("ERR")}
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE memory_date="
                "'2026-06-01'").fetchone()["c"]
        assert len(ids) == 1, results  # 恰一个 memory_id
        # 失败方全部是重放（不能是第二次副作用）
        replays = [r for r in results if str(r).startswith("ERR:IDEMPOTENCY")]
        others = [r for r in results if str(r).startswith("ERR:")
                  and not str(r).startswith("ERR:IDEMPOTENCY")]
        assert not others, others  # 无其他类型失败


class TestContractCoverage:
    """[高] v1.1 规格 150 项最低能力全覆盖。"""

    def test_spec_150_fully_covered(self):
        import json as _json
        from mariposa.capabilities import v1_compat
        v1_compat.register_v1_compat()
        spec = _json.load(open("docs/execution_pack_v1.1/contracts/"
                               "capabilities.v1.json", encoding="utf-8"))
        spec_names = {x.get("canonical_name") or x.get("name")
                      for x in spec.get("capabilities", spec)}
        missing = sorted(spec_names - set(registry.REGISTRY))
        assert not missing, missing

    def test_blocked_capabilities_report_honestly(self, actors):
        out = registry.invoke(actors["qiaosheng"], "chat.send",
                              {"conversation_id": "x", "text": "t"}, None)
        assert out["data"]["status"] == "blocked"
        assert "claude" in out["data"]["reason"] or "CLI" in out["data"]["reason"]
        lis = registry.invoke(actors["qiaosheng"], "listening.play", {}, None)
        assert lis["data"]["status"] == "reserved"


class TestStrictSchemas:
    """[中高] 12 个严格 schema 在 invoke 路径真实拦截。"""

    def test_missing_required_rejected(self, actors):
        with pytest.raises(Forbidden) as e:
            validate("memory.hold", {})  # text 必填
        assert e.value.detail.get("code") == "SCHEMA_VIOLATION"

    def test_unexpected_field_rejected(self, actors):
        with pytest.raises(Forbidden):
            validate("memory.get", {"memory_id": "m", "evil": 1})

    def test_wrong_type_rejected(self, actors):
        with pytest.raises(Forbidden):
            validate("memory.restore", {"memory_id": "m",
                                        "expected_current_version": "not-int"})

    def test_valid_passes(self, actors):
        validate("memory.get", {"memory_id": "mem_x"})


class TestRevokeEdgeBranch:
    """[中] revoke 不存在/已撤销 -> 结构化 NOT_FOUND（曾为 NameError/500）。"""

    def test_revoke_missing_binding_structured(self, actors):
        with pytest.raises(NotFound):
            identity.revoke_binding("qiaosheng", "binding_missing")
        identity.revoke_binding("qiaosheng", "binding_worker")
        with pytest.raises(NotFound):  # 重复撤销
            identity.revoke_binding("qiaosheng", "binding_worker")
        identity.seed(TOKENS)  # 恢复


class TestEvidenceMapIntegrity:
    """[中] 118 映射证据可核验（持久校验，防止再退化）。"""

    def test_all_pass_evidence_resolvable(self):
        import json as _json
        import re as _re
        import subprocess as _sp
        import collections as _cc
        m = _json.load(open("docs/verification/acceptance_map_v1.1.json",
                            encoding="utf-8"))
        out = _sp.run([r".venv\Scripts\python", "-m", "pytest",
                       "--collect-only", "-q", "tests"],
                      capture_output=True, text=True).stdout
        idx = set()
        for line in out.splitlines():
            line = line.strip().replace("/", "\\")
            if line.startswith("tests\\") and "::" in line:
                idx.add(line.split("::")[-1])
        file_re = _re.compile(r"[A-Za-z0-9_/]*[.]py")
        fn_re = _re.compile(r"test_[A-Za-z0-9_]+")
        bad = []
        for case in m["cases"]:
            if case["status"] != "PASS":
                continue
            fns = fn_re.findall(file_re.sub(" ", case["evidence"]))
            for fn in fns:
                if fn not in idx:
                    bad.append((case["id"], fn))
        assert not bad, bad[:5]
