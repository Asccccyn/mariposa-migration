"""RETIRED_CAPABILITIES_NEGATIVE（legacy-removal §11）。

退役能力不可调用：不在 Registry / 不在 MCP tools/list / 无公共
HTTP handler / fresh DB 不建退役表 / 无旧模块新写入路径。
"""
from __future__ import annotations

import re

import pytest

from mariposa import db, schema
from mariposa.errors import NotFound
from mariposa.identity import service as identity
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


RETIRED = re.compile(
    r"^(letter\.|home\.|self\.|diary\.(write|list|search|hide|read|revise)|"
    r"calendar\.|reminder\.|moments\.|sticker|memory\.quotes\.|"
    r"workspace\.quotes\.|memory\.meanings\.|raw\.|"
    r"workspace\.(tasks|runs|candidates)\.|memory\.(candidates|emotions)\.)")

RETIRED_TABLES = [
    "letters", "letter_versions", "home", "home_versions",
    "self_entries", "self_versions", "diary_entries", "diary_versions",
    "reminders", "moments", "moment_versions", "moment_comments",
    "moment_reactions", "stickers", "quotes", "quote_versions",
    "memory_meanings", "raw_conversations", "raw_messages",
    "memory_raw_refs", "provisional_sources",
]


class TestRetiredCapabilitiesNegative:
    def test_not_in_registry(self):
        from mariposa.capabilities.registry import REGISTRY
        left = [k for k in REGISTRY if RETIRED.match(k)]
        assert left == [], f"Registry 仍暴露退役能力：{left}"

    def test_not_in_mcp_tools_list(self, actors):
        from mariposa.capabilities.mcp_adapter import _tools_for
        tools = _tools_for(actors["jiaming"])
        left = [t["name"] for t in tools if RETIRED.match(t["name"])]
        assert left == [], f"MCP tools/list 仍暴露：{left}"

    def test_invoke_rejected(self, actors):
        from mariposa.capabilities import registry
        for cap in ["letter.write", "diary.write", "calendar.day",
                    "reminder.create", "moments.post", "sticker.add",
                    "memory.quotes.keep", "memory.meanings.append",
                    "raw.search", "workspace.tasks.claim"]:
            with pytest.raises(NotFound):
                registry.invoke(actors["jiaming"], cap, {}, None)

    def test_fresh_db_has_no_retired_tables(self):
        with db.formal() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        present = [t for t in RETIRED_TABLES if t in names]
        assert present == [], f"fresh 正式库仍创建退役表：{present}"

    def test_fresh_workspace_has_no_review_tables(self):
        with db.workspace() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ["work_items", "proposal_versions", "worker_runs",
                  "workspace_task_leases"]:
            assert t not in names, f"fresh workspace 库仍创建 {t}"

    def test_no_legacy_module_import(self):
        import importlib
        for mod in ["mariposa.letters", "mariposa.content",
                    "mariposa.calendar", "mariposa.reminders",
                    "mariposa.moments", "mariposa.quotes",
                    "mariposa.raw", "mariposa.workspace"]:
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(mod)

    def test_relation_untouched(self, actors):
        """§15：Relation 冻结区回归——模块删除未伤 relation。"""
        from mariposa.memory import service as memory
        from mariposa.capabilities import registry
        a = memory.hold(actors["jiaming"], text="关系回归甲",
                        memory_date="2026-09-25", date_confidence="exact",
                        original_title="ra", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
        b = memory.hold(actors["jiaming"], text="关系回归乙",
                        memory_date="2026-09-25", date_confidence="exact",
                        original_title="rb", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
        out = registry.invoke(actors["jiaming"], "memory.relations.link",
                              {"from_memory": a["memory_id"],
                               "to_memory": b["memory_id"],
                               "relation_type": "continuation_of"}, None)
        assert out["ok"] is True or out["data"]
        lst = registry.invoke(actors["jiaming"], "memory.relations.list",
                              {"memory_id": a["memory_id"]}, None)
        assert lst["data"]["relations"]
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM memory_relations").fetchone()["c"]
        assert n >= 1, "relation 表未被误删且可写读"
