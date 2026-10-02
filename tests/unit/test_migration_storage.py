"""migration inventory/dry-run/verify 与 storage backup/verify-only。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mariposa import config, migration, storage


FIXTURES = Path(__file__).parents[1] / "fixtures" / "legacy_bucket_sample"


class TestMigrationDryRun:
    def test_dry_run_mapping_and_lock_protection(self, tmp_path):
        out = tmp_path / "dry.json"
        r = migration.dry_run(str(FIXTURES), str(out))
        assert r["ok"] is True
        assert r["counts"] == {"total": 2, "to_memories": 2, "out_of_scope": 0}
        mem = next(e for e in r["entries"]
                   if e["legacy_id"].startswith("2026-08-15"))
        assert mem["mapping"]["pinned"] is True
        assert mem["mapping"]["importance"] == "10"
        first = next(e for e in r["entries"]
                     if e["legacy_id"].startswith("2026-07-01"))
        assert "海风" in first["mapping"]["meaning_layers"][0]
        assert r["apply"].startswith("blocked")

    def test_verify_roundtrip(self, tmp_path):
        out = tmp_path / "dry.json"
        migration.dry_run(str(FIXTURES), str(out))
        v = migration.verify(str(out))
        assert v["ok"] is True and v["checked"] == 2

    def test_verify_detects_tampering(self, tmp_path):
        out = tmp_path / "dry.json"
        migration.dry_run(str(FIXTURES), str(out))
        report = json.loads(out.read_text(encoding="utf-8"))
        report["entries"][0]["payload_hash"] = "0" * 64
        out.write_text(json.dumps(report), encoding="utf-8")
        v = migration.verify(str(out))
        assert v["ok"] is False
        assert v["problems"][0]["issue"] == "payload_hash_mismatch"

    def test_real_data_guard(self, tmp_path):
        big = tmp_path / "big"
        big.mkdir()
        for i in range(60):
            (big / f"2026-01-01 00-00-{i:02d} x_{i:012x}.md").write_text(
                "---\ntype: dynamic\n---\nbody", encoding="utf-8")
        r = migration.dry_run(str(big))
        assert r["ok"] is False and "真实数据" in r["error"]

    def test_letters_out_of_scope_body_protected(self, tmp_path):
        """信件已拆出 mariposa：dry-run 只登记存在性，正文不进报告。"""
        src = tmp_path / "fx"
        src.mkdir()
        (src / "2026-09-07 22-00-00 旧信_bbb444555666.md").write_text(
            "---\ntype: letter\nlock_type: timed\n---\n极其隐私的正文内容",
            encoding="utf-8")
        out = tmp_path / "dry.json"
        r = migration.dry_run(str(src), str(out))
        assert r["counts"] == {"total": 1, "to_memories": 0, "out_of_scope": 1}
        e = r["entries"][0]
        assert e["target"] == "out_of_scope" and "mapping" not in e
        assert "极其隐私" not in json.dumps(r)
        v = migration.verify(str(out))
        assert v["ok"] is True

    def test_inventory_metadata_only(self, tmp_path, capsys):
        src = tmp_path / "src"
        (src / "archive").mkdir(parents=True)
        (src / "archive" / "2026-07-01 10-00-00 私人样本_abc123def456.md").write_text(
            "---\ntype: dynamic\n---\n极其隐私的正文内容",
            encoding="utf-8")
        r = migration.inventory(str(src))
        assert r["ok"] is True
        assert r["total_files"] == 1
        captured = capsys.readouterr().out
        assert "极其隐私" not in captured  # 正文不进输出


class TestStorage:
    def test_backup_and_verify_only(self, tmp_path, actors):
        manifest = storage.backup()
        assert set(manifest["databases"]) == {"formal", "workspace"}
        bdir = Path(manifest["databases"]["formal"]["path"]).parent
        v = storage.restore_verify(str(bdir))
        assert v["ok"] is True

    def test_verify_detects_corruption(self, tmp_path, actors):
        manifest = storage.backup()
        bdir = Path(manifest["databases"]["formal"]["path"]).parent
        # 篡改备份文件
        Path(manifest["databases"]["formal"]["path"]).write_bytes(b"corrupted")
        v = storage.restore_verify(str(bdir))
        assert v["ok"] is False
        issues = {p["issue"] for p in v["problems"]}
        # CB-002：verify 现在按备份目录内文件核对（不再信 manifest 绝对
        # 路径），字节级篡改会先命中 size 快速短路或 sha256——均为有效
        # 损坏检测
        assert issues & {"sha256_mismatch", "unreadable", "size_mismatch"}
