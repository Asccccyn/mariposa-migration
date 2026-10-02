"""复审存储/迁移域修复回归（RA-023/024/025/027）。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mariposa import config as cfg
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa import migration, storage
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


class TestRuntimeVacuumGate:

    def test_vacuum_empty_sqlite_rejected(self, tmp_path, monkeypatch):
        from mariposa import schema as sch
        dbfile = tmp_path / "recall.sqlite3"
        c = sqlite3.connect(str(dbfile))
        c.execute("VACUUM")
        c.close()
        assert dbfile.stat().st_size > 0, "前置：非零大小空 SQLite"
        monkeypatch.setattr(cfg, "RECALL_DB", dbfile)
        monkeypatch.setattr(cfg, "ALLOW_DB_CREATE", False)
        with pytest.raises(RuntimeError, match="拒绝静默新建"):
            sch.migrate_runtime()


class TestBucketTypeAlias:

    def test_bucket_type_archived_unmapped(self, tmp_path, actors):
        f = tmp_path / "2026-01-01-b.md"
        f.write_text("---\ndate: 2026-01-01\nbucket_type: archived\n---\n\n"
                     "别名归档正文\n", encoding="utf-8")
        report = migration.dry_run(str(tmp_path),
                                   out=str(tmp_path / "r.json"))
        by_target = {e["legacy_id"]: e["target"] for e in report["entries"]}
        assert by_target["2026-01-01-b.md"] == "unmapped", \
            "bucket_type:archived 不得绕过白名单（RA-024）"


class TestStaleRestoreReport:

    def test_report_after_edit_no_op(self, actors):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "restore_legacy", "scripts/migrations/offline_v17/"
            "restore_legacy_summaries.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from mariposa import db
        m = memory.hold(actors["jiaming"], text="恢复目标正文",
                        memory_date="2026-05-05", date_confidence="exact",
                        original_title="t", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            v = conn.execute(
                "SELECT MAX(version_no) AS n FROM memory_versions WHERE"
                " memory_id=?", (m["memory_id"],)).fetchone()["n"]
            conn.execute(
                "UPDATE memory_versions SET event_text='旧完整正文'"
                " WHERE memory_id=? AND version_no=?",
                (m["memory_id"], v))
            conn.execute(
                "INSERT INTO memory_versions(memory_id, version_no,"
                " representation, compressed_summary, authored_by,"
                " origin_kind, payload_hash, created_at)"
                " VALUES(?,?,'forgotten_summary','摘要','t',"
                "'forget_approval','x',datetime('now'))",
                (m["memory_id"], v + 1))
            conn.execute(
                "UPDATE memories SET current_version_no=?,"
                " compression_state='forgotten_summary' WHERE memory_id=?",
                (v + 1, m["memory_id"]))
            conn.execute("COMMIT")
        report = mod.dry_run()
        assert mod.apply(report)["applied"] == 1
        # 正常修订（新正文）
        memory.hold  # noqa
        from mariposa.memory import extras
        with db.formal() as conn:
            row = conn.execute(
                "SELECT current_version_no FROM memories WHERE"
                " memory_id=?", (m["memory_id"],)).fetchone()
        extras.update_text("jiaming", m["memory_id"],
                           row["current_version_no"],
                           "修订后的新正文")
        # 旧 report 重放：不得把当前正文改回旧版本
        assert mod.apply(report)["applied"] == 0, \
            "dry-run 后状态前进的旧 report 必须拒绝生效（RA-025）"
        got = memory.hold  # noqa
        from mariposa.memory import listing
        items = registry_get(m["memory_id"])
        assert "修订后的新正文" in items["text"]


def registry_get(mid):
    from mariposa import db
    with db.formal() as conn:
        from mariposa.memory import service as ms
        return ms.get(conn, mid)


class TestBackupIdentity:

    def test_unrelated_sqlite_rejected(self, tmp_path, actors):
        d = tmp_path / "bk"
        d.mkdir()
        for name in ("formal.sqlite3", "workspace.sqlite3"):
            c = sqlite3.connect(str(d / name))
            c.execute("CREATE TABLE unrelated(x)")
            c.commit()
            c.close()
        manifest = {"databases": {}}
        for name in ("formal", "workspace"):
            p = d / f"{name}.sqlite3"
            manifest["databases"][name] = {
                "path": str(p), "bytes": p.stat().st_size,
                "sha256": storage._sha256_file(p)}
        (d / "manifest.json").write_text(json.dumps(manifest),
                                         encoding="utf-8")
        v = storage.restore_verify(str(d))
        assert v["ok"] is False, "无关 SQLite 不得判可供恢复（RA-027）"
        assert any("not_a_mariposa" in p["issue"]
                   for p in v["problems"])
