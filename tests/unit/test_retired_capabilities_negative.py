"""RETIRED_CAPABILITIES_NEGATIVE（legacy-removal §11）。

退役能力不可调用：不在 Registry / 不在 MCP tools/list / 无公共
HTTP handler / fresh DB 不建退役表 / 无旧模块新写入路径。
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from mariposa import db, schema
from mariposa.errors import NotFound
from mariposa.identity import service as identity
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "char", "agent",
                                      "claude_chat", "bj"),
    }


RETIRED = re.compile(
    r"^(letter\.|home\.|self\.|diary\.(write|list|search|hide|read|revise)|"
    r"calendar\.|reminder\.|moments\.|sticker|memory\.quotes\.|"
    r"workspace\.quotes\.|memory\.meanings\.|raw\.|"
    r"workspace\.(tasks|runs|candidates)\.|memory\.(candidates|emotions)\.)")

# 2026-10-13 两次合同差集：9 + 37，精确名单防新能力误撞旧前缀。
RETIRED_20261013 = frozenset({
    "handoff.latest", "handoff.write", "presence.handoff.latest",
    "presence.handoff.write", "memory.anchor", "memory.by_tag",
    "memory.pin", "memory.protect", "memory.tags.add",
    "chat.cancel", "chat.conversations.create", "chat.conversations.get",
    "chat.conversations.list", "chat.conversations.messages",
    "chat.messages.list", "chat.send", "group.archive.get",
    "group.archive.list", "group.history", "group.list", "group.profile",
    "group.send", "group.send_message", "group.status", "listening.enqueue",
    "listening.join", "listening.leave", "listening.pause", "listening.play",
    "listening.queue", "listening.seek", "listening.status", "listening.sync",
    "memory.reengagement.record", "settings.update", "voice.call.end",
    "voice.call.get", "voice.call.start", "voice.send", "voice.status",
    "wakeup.configure", "wakeup.status", "wishstar.get", "wishstar.list",
    "wishstar.respond", "wishstar.write",
})


def _retired(name: str) -> bool:
    return bool(RETIRED.match(name)) or name in RETIRED_20261013


def _retired_tool(name: str) -> bool:
    # 精确名正向编码，保留 send_message 等原名中的下划线身份。
    return (name in {"mariposa_" + n.replace(".", "_")
                     for n in RETIRED_20261013}
            or bool(RETIRED.match(name.removeprefix("mariposa_")
                                  .replace("_", "."))))


RETIRED_TABLES = [
    "letters", "letter_versions", "home", "home_versions",
    "self_entries", "self_versions", "diary_entries", "diary_versions",
    "reminders", "moments", "moment_versions", "moment_comments",
    "moment_reactions", "stickers", "quotes", "quote_versions",
    "memory_meanings", "raw_conversations", "raw_messages",
    "memory_raw_refs", "provisional_sources",
    "handoffs", "memory_tags", "memory_reengagements",
    "anniversary_definitions", "anniversary_occurrences",
]
RETIRED_MEMORY_COLUMNS = {"pinned", "protected", "anchor"}


class TestRetiredCapabilitiesNegative:
    def test_not_in_registry(self):
        from mariposa.capabilities.registry import REGISTRY
        left = [k for k in REGISTRY if _retired(k)]
        assert left == [], f"Registry 仍暴露退役能力：{left}"

    def test_not_in_mcp_tools_list(self, actors):
        from mariposa.capabilities.mcp_adapter import _tools_for
        tools = _tools_for(actors["jiaming"])
        left = [t["name"] for t in tools if _retired_tool(t["name"])]
        assert left == [], f"MCP tools/list 仍暴露：{left}"

    def test_guard_catches_injected_retired_tools(self):
        """自证映射有效：注入的退役 transport 名必须被同一规则命中
        （否则上面的负测是恒真空转）。"""
        fakes = ["mariposa_letter_write", "mariposa_diary_write",
                 "mariposa_raw_search", "mariposa_group_send_message",
                 "mariposa_presence_handoff_write"]
        caught = [f for f in fakes if _retired_tool(f)]
        assert caught == fakes, f"映射漏抓：{set(fakes) - set(caught)}"
        assert len(RETIRED_20261013) == 46
        assert not _retired("memory.mood.update"), "授权心情编辑应保留"

    def test_invoke_rejected(self, actors):
        from mariposa.capabilities import registry
        for cap in ["letter.write", "diary.write", "calendar.day",
                    "reminder.create", "moments.post", "sticker.add",
                    "memory.quotes.keep", "memory.meanings.append",
                    "raw.search", "workspace.tasks.claim",
                    *sorted(RETIRED_20261013)]:
            with pytest.raises(NotFound):
                registry.invoke(actors["jiaming"], cap, {}, None)

    def test_fresh_db_has_no_retired_tables(self):
        with db.formal() as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        present = [t for t in RETIRED_TABLES if t in names]
        assert present == [], f"fresh 正式库仍创建退役表：{present}"
        with db.formal() as conn:
            columns = {r["name"] for r in conn.execute(
                "PRAGMA table_info(memories)")}
        assert not columns & RETIRED_MEMORY_COLUMNS

    def test_oct13_upgrade_removes_only_retired_schema(self):
        """历史迁移跑完但未清理的部署形态，经当前清理收敛且重复安全。"""
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            schema._apply(conn, schema.FORMAL_MIGRATIONS)
            before = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"handoffs", "memory_tags", "memory_reengagements"} <= before
            schema._drop_deleted_columns(conn)
            schema._drop_deleted_columns(conn)
            after = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert not set(RETIRED_TABLES) & after
            assert {"memory_keeps", "memories", "source_import_batches"} <= after
            columns = {r["name"] for r in conn.execute(
                "PRAGMA table_info(memories)")}
            assert not columns & RETIRED_MEMORY_COLUMNS

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
