"""Independent anniversary retirement preserves archives and memory semantics."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mariposa import db, schema
from mariposa.bootstrap import service as bootstrap
from mariposa.capabilities import input_schemas, registry
from mariposa.errors import Forbidden
from mariposa.identity import Principal
from mariposa.memory import service as memory
from mariposa.recall import phase_policy


OLD_TABLES = {"anniversary_definitions", "anniversary_occurrences"}
ARCHIVES = {"legacy_" + table for table in OLD_TABLES}


def connection():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def tables(conn):
    return {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_has_only_empty_readonly_archives():
    with connection() as conn:
        schema._apply(conn, schema.FORMAL_MIGRATIONS)
        assert not OLD_TABLES & tables(conn)
        assert ARCHIVES <= tables(conn)
        for table in ARCHIVES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert {"memory_categories", "memory_moods", "memories", "plans"} <= tables(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_preserves_every_old_column_row_and_foreign_key():
    with connection() as conn:
        schema._apply(conn, [m for m in schema.FORMAL_MIGRATIONS if m[0] < 37])
        conn.execute("INSERT INTO anniversary_definitions VALUES(?,?,?,?,?,?,?)",
                     ("def-1", "合成纪念😀", "yearly", "legacy-rule", "memory-ref",
                      "qiaosheng", "2020-01-01T01:02:03+00:00"))
        conn.executemany("INSERT INTO anniversary_occurrences VALUES(?,?,?)", [
            ("occ-2020", "def-1", "2020-06-01"),
            ("occ-2021", "def-1", "2021-06-01")])
        before = {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
                  for name in OLD_TABLES}
        schema._apply(conn, schema.FORMAL_MIGRATIONS)
        schema._apply(conn, schema.FORMAL_MIGRATIONS)  # restart must be idempotent
        assert not OLD_TABLES & tables(conn)
        for old, rows in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM legacy_{old} ORDER BY 1")] == rows
        fks = conn.execute("PRAGMA foreign_key_list(legacy_anniversary_occurrences)").fetchall()
        assert {row["table"] for row in fks} == {"legacy_anniversary_definitions"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        statements = [
            "INSERT INTO legacy_anniversary_definitions(definition_id,title,created_by,created_at) VALUES('new','x','qiaosheng','now')",
            "UPDATE legacy_anniversary_definitions SET title='changed'",
            "DELETE FROM legacy_anniversary_definitions",
            "INSERT INTO legacy_anniversary_occurrences VALUES('new','def-1','2022-06-01')",
            "UPDATE legacy_anniversary_occurrences SET occurrence_date='2023-06-01'",
            "DELETE FROM legacy_anniversary_occurrences",
        ]
        for statement in statements:
            with pytest.raises(sqlite3.IntegrityError, match="RETIRED_ARCHIVE_READ_ONLY"):
                conn.execute(statement)
        for old, rows in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM legacy_{old} ORDER BY 1")] == rows


def test_bootstrap_omits_retired_section_and_rejects_its_cursor(actors):
    actor = Principal("jiaming", "char", "agent", "estomago", "binding_jiaming")
    for profile in ("legacy", "compact_v1"):
        first = registry.invoke(actor, "bootstrap.get", {
            "profile": "estomago", "output_profile": profile}, None)["data"]
        assert "anniversaries" not in first
        with pytest.raises(Forbidden) as error:
            registry.invoke(actor, "bootstrap.next", {
                "snapshot_id": first["snapshot_id"], "section": "anniversaries",
                "cursor": {"anniversaries_offset": 0}}, None)
        assert error.value.code == "SCHEMA_VIOLATION"
        with pytest.raises(Forbidden):
            bootstrap.next_page("jiaming", "estomago", first["snapshot_id"],
                                {"anniversaries_offset": 0}, "anniversaries")
    assert "anniversaries" not in input_schemas.schema_for("bootstrap.next")["properties"]["section"]["enum"]


def test_anniversary_memory_category_still_has_permanent_wide(actors):
    mid = memory.hold(actors["jiaming"], "纪念事件仍是普通记忆桶",
                      original_title="纪念分类", categories=["anniversary"],
                      creation_mode="retrospective", memory_date="2000-01-01")["memory_id"]
    with db.formal() as conn:
        conn.execute("UPDATE memories SET held_at=? WHERE memory_id=?",
                     ((datetime.now(timezone.utc) - timedelta(days=1000)).isoformat(), mid))
        assert memory.get(conn, mid)["categories"] == ["anniversary"]
    phase = phase_policy.phase_of(mid)
    assert phase.stage == "WIDE"
    assert phase.reason == "PERMANENT_CATEGORY"


