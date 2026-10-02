"""存储/迁移域审计修复回归（2026-10-02 基线审计 P2 批）。

- CB-021：退役 archive 类型经迁移不再写成 active Memory
  （retired-archive-migration——dry-run/apply 白名单化）。
- CB-024：runtime 库新建受 ALLOW_DB_CREATE 约束（runtime-create-
  disabled——空/缺失文件不再绕过）。
- CB-025：snapshot 清单哈希按复制产物计算（复制后源变化不再产生
  与目标不一致的"成功"清单）。
- CB-026：离线恢复识别 event_text 形态、幂等、重建投影。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mariposa import config as cfg
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa import migration
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _bucket(tmp_path: Path, name: str, frontmatter: dict, body: str) -> Path:
    f = tmp_path / name
    fm = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    f.write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")
    return f


# ---------------------------------------------------------------- CB-021

class TestRetiredArchiveNotMigrated:

    def test_archived_type_unmapped(self, actors, tmp_path):
        _bucket(tmp_path, "2026-01-01-note.md",
                {"date": "2026-01-01", "type": "archived"}, "旧归档正文")
        report = migration.dry_run(str(tmp_path),
                                   out=str(tmp_path / "r.json"))
        by_target = {e["legacy_id"]: e["target"] for e in report["entries"]}
        assert by_target["2026-01-01-note.md"] == "unmapped", \
            "退役 archive 不得映射为正式 Memory"
        # apply 也不落地
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            r = migration.apply_from_report(str(tmp_path / "r.json"))
        assert r["ok"] and r["applied"] == 0
        from mariposa import db
        with db.formal() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        assert n == 0

    def test_known_type_still_migrates(self, actors, tmp_path):
        _bucket(tmp_path, "2026-01-02-note.md",
                {"date": "2026-01-02", "type": "memory"}, "合法迁移正文")
        report = migration.dry_run(str(tmp_path),
                                   out=str(tmp_path / "r2.json"))
        by_target = {e["legacy_id"]: e["target"] for e in report["entries"]}
        assert by_target["2026-01-02-note.md"] == "memories"


# ---------------------------------------------------------------- CB-024

class TestRuntimeCreateGate:

    def test_fresh_runtime_rejected_without_allow(self, tmp_path,
                                                  monkeypatch):
        from mariposa import schema as sch, db as db_mod
        missing = tmp_path / "absent" / "recall.sqlite3"
        monkeypatch.setattr(cfg, "RECALL_DB", missing)
        monkeypatch.setattr(cfg, "ALLOW_DB_CREATE", False)
        with pytest.raises(RuntimeError, match="拒绝静默新建"):
            sch.migrate_runtime()
        assert not missing.exists() or missing.stat().st_size == 0

        # 空文件同样拒绝（不建表）
        missing.parent.mkdir(parents=True, exist_ok=True)
        missing.write_bytes(b"")
        with pytest.raises(RuntimeError, match="拒绝静默新建"):
            sch.migrate_runtime()


# ---------------------------------------------------------------- CB-025

class TestSnapshotManifestMatchesDestination:

    def test_manifest_hashes_copied_bytes(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        f = _bucket(src, "2026-03-03-a.md", {"date": "2026-03-03"}, "快照正文")
        dst = tmp_path / "dst"
        out = migration.snapshot(str(src), str(dst))
        assert out["ok"] is True
        manifest = json.loads((dst / "_manifest.json").read_text())
        entry = manifest["files"][0]
        target_hash = __import__("hashlib").sha256(
            (dst / "2026-03-03-a.md").read_bytes()).hexdigest()
        assert entry["sha256"] == target_hash, \
            "清单哈希必须对应复制产物（审计反例：复制后源变化清单失真）"
        # 复制后源变化：清单仍与目标一致
        f.write_text("---\ndate: 2026-03-03\n---\n\n源又变了\n",
                     encoding="utf-8")
        out2 = migration.snapshot(str(src), str(dst / "2"))
        manifest2 = json.loads((dst / "2" / "_manifest.json").read_text())
        target2 = (dst / "2" / "2026-03-03-a.md").read_bytes()
        assert manifest2["files"][0]["sha256"] == \
            __import__("hashlib").sha256(target2).hexdigest()


# ---------------------------------------------------------------- CB-026

class TestOfflineRestore:

    def _forget_one(self, actors):
        """建一个遗忘桶：v1 full（event_text 形态）→ 遗忘摘要版本。"""
        m = memory.hold(actors["jiaming"], text="将被遗忘的正文",
                        memory_date="2026-05-05", date_confidence="exact",
                        original_title="t", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
        from mariposa import db
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            v = conn.execute(
                "SELECT MAX(version_no) AS n FROM memory_versions WHERE"
                " memory_id=?", (m["memory_id"],)).fetchone()["n"]
            conn.execute(
                "UPDATE memory_versions SET event_text='事件正文完整版'"
                " WHERE memory_id=? AND version_no=?",
                (m["memory_id"], v))
            conn.execute(
                "INSERT INTO memory_versions(memory_id, version_no,"
                " representation, hold_text, compressed_summary,"
                " authored_by, origin_kind, payload_hash, created_at)"
                " VALUES(?,?, 'forgotten_summary', NULL, '摘要',"
                " 'offline_t', 'forget_approval', 'x', datetime('now'))",
                (m["memory_id"], v + 1))
            conn.execute(
                "UPDATE memories SET current_version_no=?,"
                " compression_state='forgotten_summary' WHERE memory_id=?",
                (v + 1, m["memory_id"]))
            conn.execute("COMMIT")
        return m["memory_id"]

    def test_event_text_recoverable_and_idempotent(self, actors):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "restore_legacy", "scripts/migrations/offline_v17/"
            "restore_legacy_summaries.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        mid = self._forget_one(actors)
        report = mod.dry_run()
        item = next(i for i in report["items"] if i["memory_id"] == mid)
        assert item["action"] == "restore_full_version", \
            "event_text 形态的完整版本必须可恢复（此前误判 GAP）"
        r1 = mod.apply(report)
        assert r1["applied"] == 1
        from mariposa import db
        with db.formal() as conn:
            row = conn.execute(
                "SELECT compression_state FROM memories WHERE memory_id=?",
                (mid,)).fetchone()
            rd = conn.execute(
                "SELECT memory_version_no FROM retrieval_documents WHERE"
                " memory_id=?", (mid,)).fetchone()
            cur = conn.execute(
                "SELECT current_version_no FROM memories WHERE"
                " memory_id=?", (mid,)).fetchone()["current_version_no"]
        assert row["compression_state"] == "full"
        assert rd is not None and rd["memory_version_no"] == cur, \
            "恢复后检索投影必须指向当前版本（索引重建）"
        # 幂等：重复 apply 不再追加版本
        r2 = mod.apply(mod.dry_run())
        assert r2["applied"] == 0, "重复 apply 必须幂等"
        with db.formal() as conn:
            n = conn.execute(
                "SELECT MAX(version_no) AS n FROM memory_versions WHERE"
                " memory_id=?", (mid,)).fetchone()["n"]
        assert n == cur, f"版本不再追加：{n} vs {cur}"
