"""存储/迁移域审计修复回归（2026-10-02 基线审计 P1 批）。

- CB-002：backup verify 只验证调用方指定目录、空/缺清单 fail closed
  （审计反例 backup-relocated-missing-file / backup-empty-manifest：
  副本缺 formal 仍 verify_ok=true）。
- CB-003：迁移 apply 的 Memory 创建与 ID 映射同事务（审计反例
  migration-interruption-retry：注入中断后重试产生 2 Memory 1 映射）。
- CB-004：schema 迁移 26 保留 memory/delete 申请历史、退役记录分离
  （审计反例 schema-upgrade-deletion-history：before=5, after=0）。
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from mariposa import db, storage
from mariposa import migration
from mariposa.identity import service as identity
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "binding_qiaosheng"),
    }


# ---------------------------------------------------------------- CB-002

class TestBackupVerify:
    """restore_verify 必须证明指定目录的恢复材料。"""

    def _backup_dir(self) -> Path:
        manifest = storage.backup()
        return Path(manifest["databases"]["formal"]["path"]).parent

    def test_relocated_missing_copy_fails(self, actors, tmp_path):
        """审计反例：目录复制后删掉副本 formal——verify 必须失败
        （旧行为打开 manifest 里的旧绝对路径，误判 ok）。"""
        src = self._backup_dir()
        copy = tmp_path / "relocated"
        shutil.copytree(src, copy)
        (copy / "formal.sqlite3").unlink()
        v = storage.restore_verify(str(copy))
        assert v["ok"] is False
        assert any(p["db"] == "formal" and p["issue"] == "file_missing"
                   for p in v["problems"])

    def test_relocated_backup_ok_after_original_removed(self, actors, tmp_path):
        """审计要求：目录迁移并删除旧位置后，新位置仍可验证通过。"""
        src = self._backup_dir()
        moved = tmp_path / "moved"
        shutil.move(str(src), str(moved))
        v = storage.restore_verify(str(moved))
        assert v["ok"] is True, v["problems"]

    def test_empty_manifest_rejected(self, actors, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        (d / "manifest.json").write_text(
            json.dumps({"created_at": "x", "databases": {}}),
            encoding="utf-8")
        v = storage.restore_verify(str(d))
        assert v["ok"] is False
        assert v["error"] == "manifest databases empty"

    def test_missing_workspace_rejected(self, actors, tmp_path):
        """缺 formal/workspace 任一清单项即 fail closed（审计要求）。"""
        bdir = self._backup_dir()
        m = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
        del m["databases"]["workspace"]
        (bdir / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        v = storage.restore_verify(str(bdir))
        assert v["ok"] is False
        assert any(p["issue"] == "missing_from_manifest"
                   and p["db"] == "workspace" for p in v["problems"])

    def test_corrupt_relocated_copy_fails(self, actors, tmp_path):
        bdir = self._backup_dir()
        copy = tmp_path / "corrupt"
        shutil.copytree(bdir, copy)
        (copy / "formal.sqlite3").write_bytes(b"corrupted")
        v = storage.restore_verify(str(copy))
        assert v["ok"] is False


# ---------------------------------------------------------------- CB-003

class _MapInsertBomb:
    """连接代理：第一次 INSERT INTO migration_id_map 时抛错——模拟
    Memory 创建与 ID 映射提交边界中断（审计 fault-injection 同款窗口）。"""

    def __init__(self, conn):
        self._conn = conn
        self.armed = True

    def execute(self, sql, *args):
        if self.armed and "INSERT INTO migration_id_map" in sql:
            self.armed = False
            raise sqlite3.OperationalError("AUDIT_INJECTED_INTERRUPT")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestMigrationApplyAtomic:
    """Memory 创建与 ID 映射同事务：中断重试始终一源一资源一映射。"""

    def _make_report(self, tmp_path) -> str:
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        note = bucket / "2026-01-15-note.md"
        note.write_text(
            "---\ndate: 2026-01-15\nwhy_remembered: keep\n---\n\n"
            "CB-003 合成迁移正文",
            encoding="utf-8")
        out = tmp_path / "report.json"
        report = migration.dry_run(str(bucket), out=str(out))
        assert report["ok"] and report["counts"]["to_memories"] == 1
        return str(out)

    @staticmethod
    def _counts() -> tuple[int, int]:
        with db.formal() as c:
            n_mem = c.execute("SELECT COUNT(*) n FROM memories").fetchone()["n"]
            n_map = c.execute(
                "SELECT COUNT(*) n FROM migration_id_map").fetchone()["n"]
        return n_mem, n_map

    def test_interrupt_then_retry_single_resource(self, actors, tmp_path,
                                                  monkeypatch, capsys):
        rp = self._make_report(tmp_path)
        orig_formal = db.formal

        @contextlib.contextmanager
        def bombed():
            with orig_formal() as c:
                yield _MapInsertBomb(c)

        monkeypatch.setattr(db, "formal", bombed)
        with pytest.raises(sqlite3.OperationalError):
            migration.apply_from_report(rp)
        capsys.readouterr()
        # 中断后零残留（旧行为：Memory 已提交、映射缺失）
        assert self._counts() == (0, 0), \
            "注入中断必须整体回滚：不得留下已建 Memory"

        monkeypatch.undo()
        r = migration.apply_from_report(rp)
        capsys.readouterr()
        assert r["ok"] and r["applied"] == 1
        assert self._counts() == (1, 1), \
            "重试后一源一资源一映射（审计反例为 2 Memory 1 映射）"

        # 再次重试：幂等跳过，不重复
        r2 = migration.apply_from_report(rp)
        capsys.readouterr()
        assert r2["ok"] and r2["applied"] == 0
        assert self._counts() == (1, 1)

    def test_pinned_hidden_review_same_transaction(self, actors, tmp_path):
        """pinned/hidden/needs_review 标记随 Memory 同事务落库。"""
        bucket = tmp_path / "bucket2"
        bucket.mkdir()
        note = bucket / "2026-02-02-note.md"
        note.write_text(
            "---\ndate: 2026-02-02\npinned: true\n---\n\nCB-003 置顶合成正文",
            encoding="utf-8")
        out = tmp_path / "report2.json"
        migration.dry_run(str(bucket), out=str(out))
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            r = migration.apply_from_report(str(out))
        assert r["ok"] and r["verified"] == 1
        with db.formal() as c:
            row = c.execute(
                "SELECT pinned FROM memories").fetchone()
            assert row is not None and row["pinned"] == 1


# ---------------------------------------------------------------- CB-004

class TestSchema26DeletionHistory:
    """迁移 26 不得清空现存删除申请历史（memory/delete 逐列保留，
    退役记录分离到 legacy 专表）。"""

    @staticmethod
    def _build_pre26(tmp_path) -> sqlite3.Connection:
        from mariposa import schema as sch
        dbfile = tmp_path / "legacy.sqlite3"
        conn = sqlite3.connect(dbfile)
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL)")
        for version, ddl in sch.FORMAL_MIGRATIONS:
            if version >= 26:
                break
            conn.executescript("BEGIN IMMEDIATE;\n" + ddl)
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?, datetime('now'))",
                (version,))
            conn.execute("COMMIT")
        # 旧表插入：4 条 memory/delete（覆盖 pending/rejected/withdrawn/
        # approved，含人类理由与拒绝理由）+ letter 1 条 + memory/archive
        # 1 条（退役产品记录）
        rows = [
            ("req-1", "mem_a", "memory", "delete", "太痛了", "",
             "pending", "qiaosheng", "2026-01-01T10:00:00", "2026-01-01",
             None),
            ("req-2", "mem_b", "memory", "delete", "写错了", "不需要这条",
             "rejected", "qiaosheng", "2026-01-02T10:00:00", "2026-01-02",
             "2026-01-03T09:00:00"),
            ("req-3", "mem_c", "memory", "delete", "重复记录", "",
             "withdrawn", "qiaosheng", "2026-01-04T10:00:00", "2026-01-04",
             "2026-01-05T09:00:00"),
            ("req-4", "mem_d", "memory", "delete", "同意删除", "",
             "approved", "qiaosheng", "2026-01-06T10:00:00", "2026-01-06",
             "2026-01-07T09:00:00"),
            ("req-5", "letter_x", "letter", "archive", "旧信件", "",
             "approved", "qiaosheng", "2026-01-08T10:00:00", "2026-01-08",
             "2026-01-09T09:00:00"),
            ("req-6", "mem_e", "memory", "archive", "旧归档", "",
             "approved", "qiaosheng", "2026-01-10T10:00:00", "2026-01-10",
             "2026-01-11T09:00:00"),
        ]
        conn.executemany(
            "INSERT INTO deletion_requests(id, resource_id, resource_kind,"
            " action, human_reason, ai_reason, status, submitted_by,"
            " submitted_at, local_date, decided_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return conn

    def test_upgrade_preserves_memory_delete_history(self, tmp_path):
        from mariposa import schema as sch
        conn = self._build_pre26(tmp_path)
        ddl26 = next(d for v, d in sch.FORMAL_MIGRATIONS if v == 26)
        conn.executescript("BEGIN IMMEDIATE;\n" + ddl26)
        conn.execute(
            "INSERT INTO schema_migrations VALUES(26, datetime('now'))")
        conn.execute("COMMIT")

        rows = conn.execute(
            "SELECT request_id, memory_id, human_reason, status,"
            " rejection_reason, submitted_local_date, decided_at,"
            " created_at FROM deletion_requests ORDER BY request_id"
        ).fetchall()
        # 审计反例：before=5（memory/delete 部分）after=0——现在必须全保留
        assert [r[0] for r in rows] == ["req-1", "req-2", "req-3", "req-4"]
        by_id = {r[0]: r for r in rows}
        assert by_id["req-2"][4] == "不需要这条", \
            "拒绝理由（旧 ai_reason）必须映射到 rejection_reason"
        assert by_id["req-2"][6] == "2026-01-03T09:00:00"
        assert by_id["req-1"][7] == "2026-01-01T10:00:00", \
            "created_at 取旧 submitted_at"
        assert by_id["req-3"][5] == "2026-01-04", \
            "配额依据 local_date → submitted_local_date"

        # lifetime 计数语义：迁移后按新表可数出全部历史
        n = conn.execute(
            "SELECT COUNT(*) FROM deletion_requests"
            " WHERE submitted_by='qiaosheng'").fetchone()[0]
        assert n == 4

        # 退役产品记录分离保留（letter + memory/archive），不静默清空
        legacy = conn.execute(
            "SELECT id, resource_kind, action FROM deletion_requests_legacy"
            " ORDER BY id").fetchall()
        assert [(r[0], r[1], r[2]) for r in legacy] == \
            [("req-5", "letter", "archive"), ("req-6", "memory", "archive")]
        conn.close()

    def test_fresh_chain_still_clean(self, tmp_path):
        """fresh 建库全链迁移：空旧表 → 新表空、legacy 空（无残留行）。"""
        from mariposa import schema as sch
        dbfile = tmp_path / "fresh.sqlite3"
        conn = sqlite3.connect(dbfile)
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL)")
        for version, ddl in sch.FORMAL_MIGRATIONS:
            conn.executescript("BEGIN IMMEDIATE;\n" + ddl)
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?, datetime('now'))",
                (version,))
            conn.execute("COMMIT")
        assert conn.execute(
            "SELECT COUNT(*) FROM deletion_requests").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM deletion_requests_legacy").fetchone()[0] == 0
        conn.close()
