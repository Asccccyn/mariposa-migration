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

    def test_backup_carries_referenced_objects(self, tmp_path, actors):
        """F07（2026-10-03 审计 P1）：恢复集 = 库 + 被引用的 Raw 母本 +
        媒体对象。导入与上传之后的备份必须携带对象字节，且 verify
        只看备份目录自身（删原母本/原对象不影响 verify）。"""
        import tempfile
        from mariposa import db
        from mariposa.source import importer
        up = Path(tempfile.mkdtemp())
        src = up / "f07.json"
        src.write_text(json.dumps([{
            "uuid": "f07-conv", "chat_messages": [{
                "uuid": "f07-m1", "sender": "human",
                "created_at": "2026-10-03T00:00:00Z",
                "content": [{"type": "text", "text": "F07 合成原文"}]}]}],
            ensure_ascii=False), encoding="utf-8")
        r = importer.import_file("jiaming", str(src))
        assert r["status"] == "completed", r
        from mariposa.media import service as media
        token = media.upload_prepare("jiaming", "image/png",
                                     4)["upload_token"]
        media.stage_bytes("jiaming", token, b"f07p")
        media.upload_finalize("jiaming", token)

        manifest = storage.backup()
        raws = manifest.get("raw_archives") or []
        objs = manifest.get("media_objects") or []
        assert raws and not raws[0].get("missing_at_backup")
        assert objs and not objs[0].get("missing_at_backup")
        bdir = Path(manifest["databases"]["formal"]["path"]).parent
        assert (bdir / raws[0]["rel"]).is_file()
        assert (bdir / objs[0]["rel"]).is_file()
        # 自包含：删除原母本与原对象，verify 仍 ok（凭备份内字节）
        with db.formal() as c:
            raw_path = c.execute(
                "SELECT raw_path FROM source_import_batches"
                " WHERE raw_path IS NOT NULL").fetchone()["raw_path"]
        Path(raw_path).unlink()
        for p in (config.RUNTIME_DIR / "objects").iterdir():
            p.unlink()
        v = storage.restore_verify(str(bdir))
        assert v["ok"] is True, v["problems"]

    def test_verify_fails_when_objects_absent(self, tmp_path, actors):
        """F07 负例：库引用了对象而备份没带字节（旧格式/被删）→
        verify 必须失败，不得宣称可完整恢复。"""
        import tempfile
        from mariposa.source import importer
        up = Path(tempfile.mkdtemp())
        src = up / "f07n.json"
        src.write_text(json.dumps([{
            "uuid": "f07n-conv", "chat_messages": [{
                "uuid": "f07n-m1", "sender": "human",
                "created_at": "2026-10-03T00:00:00Z",
                "content": [{"type": "text", "text": "F07N 合成原文"}]}]}],
            ensure_ascii=False), encoding="utf-8")
        r = importer.import_file("jiaming", str(src))
        assert r["status"] == "completed", r
        manifest = storage.backup()
        bdir = Path(manifest["databases"]["formal"]["path"]).parent
        # 伪造旧格式：清单无对象节，备份目录无对象字节
        for e in manifest.get("raw_archives") or []:
            (bdir / e["rel"]).unlink(missing_ok=True)
        manifest.pop("raw_archives", None)
        manifest.pop("media_objects", None)
        (bdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        v = storage.restore_verify(str(bdir))
        assert v["ok"] is False
        issues = {p["issue"] for p in v["problems"]}
        assert "raw_archive_not_in_backup" in issues, issues

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


class TestF10F11F12AuditFixes:
    """审计 2026-10-03 P2：json_stream 字节限额 / 备份返回值与目录
    唯一性 / verify 库身份。"""

    def test_multibyte_element_rejected_by_bytes(self, tmp_path):
        """490 字节（163 个三字节汉字）元素不得超过 400 字节限额——
        按字符数算只有 163 会误放。"""
        from mariposa.source import json_stream as js
        text = ("月" * 163)  # 163 chars = 489 UTF-8 bytes + quotes 491
        f = tmp_path / "big.json"
        f.write_text(json.dumps([text]), encoding="utf-8")
        from mariposa import config as _cfg
        old_limit = _cfg.SOURCE_MAX_ELEMENT_BYTES
        _cfg.SOURCE_MAX_ELEMENT_BYTES = 400
        try:
            with pytest.raises(js.JsonStreamError) as ei:
                with open(f, encoding="utf-8") as fh:
                    list(js.iter_top_level_array(fh))
        finally:
            _cfg.SOURCE_MAX_ELEMENT_BYTES = old_limit
        assert ei.value.code == "SOURCE_ELEMENT_TOO_LARGE"

    def test_small_elements_same_chunk_pass(self, tmp_path):
        """同读取块里 5 个各 42 字节合法元素：限额 100 必须全过——
        旧实现把整个缓冲当"单元素"误拒。"""
        from mariposa.source import json_stream as js
        items = [{"k": "偏移字段内容填充到四十二字节左右" * 1,
                  "n": i} for i in range(5)]
        f = tmp_path / "smalls.json"
        f.write_text(json.dumps(items, ensure_ascii=False),
                     encoding="utf-8")
        from mariposa import config as _cfg
        old_limit = _cfg.SOURCE_MAX_ELEMENT_BYTES
        _cfg.SOURCE_MAX_ELEMENT_BYTES = 100
        try:
            with open(f, encoding="utf-8") as fh:
                got = list(js.iter_top_level_array(fh))
        finally:
            _cfg.SOURCE_MAX_ELEMENT_BYTES = old_limit
        assert len(got) == len(items), "小元素不得被整块误拒"

    def test_backup_returns_ok_and_unique_dirs(self, actors):
        m1 = storage.backup()
        m2 = storage.backup()
        assert m1.get("ok") is True and m2.get("ok") is True
        assert m1["backup_dir"] != m2["backup_dir"], \
            "同秒两次备份必须有独立目录"
        v1 = storage.restore_verify(m1["backup_dir"])
        assert v1["ok"] is True

    def test_verify_rejects_swapped_databases(self, tmp_path, actors):
        """formal 备份冒充 workspace（重算合法 hash）→ verify 拒绝。"""
        m = storage.backup()
        bdir = Path(m["backup_dir"])
        forged = tmp_path / "swapped"
        forged.mkdir()
        for name in ("formal.sqlite3", "workspace.sqlite3",
                     "manifest.json"):
            src = bdir / name
            if src.exists():
                (forged / name).write_bytes(src.read_bytes())
        # 用 formal 的字节冒充 workspace，重算 manifest 哈希
        (forged / "workspace.sqlite3").write_bytes(
            (bdir / "formal.sqlite3").read_bytes())
        manifest = json.loads((forged / "manifest.json").read_text())
        import hashlib as _h
        p = forged / "workspace.sqlite3"
        digest = _h.sha256(p.read_bytes()).hexdigest()
        manifest["databases"]["workspace"]["sha256"] = digest
        manifest["databases"]["workspace"]["bytes"] = p.stat().st_size
        (forged / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        v = storage.restore_verify(str(forged))
        assert v["ok"] is False
        assert any(p["issue"] == "identity_mismatch"
                   for p in v["problems"]), v["problems"]


class TestF10CrossChunkBoundaryLimit:
    """裁定（2026-10-04）：跨读取块边界的元素——限额按当前元素自身
    UTF-8 字节判定，不因跨块而误判。"""

    def test_element_split_across_chunks_passes(self, tmp_path,
                                                monkeypatch):
        """限额 100：一个 ~66 字节的合法元素被 chunk 边界劈成两半，
        必须完整解析通过（不是"缓冲里字节数超限"）。"""
        from mariposa.source import json_stream as js
        from mariposa import config as _cfg
        items = [{"k": "跨块元素内容正好六十六字节左右ok", "n": 1}]
        f = tmp_path / "cross.json"
        f.write_text(json.dumps(items, ensure_ascii=False),
                     encoding="utf-8")
        old_limit = _cfg.SOURCE_MAX_ELEMENT_BYTES
        _cfg.SOURCE_MAX_ELEMENT_BYTES = 100
        old_chunk = js._CHUNK
        js._CHUNK = 20  # 强制多次 fill，元素跨块
        try:
            with open(f, encoding="utf-8") as fh:
                got = list(js.iter_top_level_array(fh))
        finally:
            js._CHUNK = old_chunk
            _cfg.SOURCE_MAX_ELEMENT_BYTES = old_limit
        assert got == items, "跨块元素必须完整解析，不得误判超限"

    def test_element_split_across_chunks_rejected_when_over(self, tmp_path):
        """限额 30：跨块的 66 字节元素按自身字节超限拒绝（结构化
        SOURCE_ELEMENT_TOO_LARGE，不是 malformed）。"""
        from mariposa.source import json_stream as js
        from mariposa import config as _cfg
        items = [{"k": "跨块超限元素内容超过三十字节肯定超", "n": 1}]
        f = tmp_path / "cross2.json"
        f.write_text(json.dumps(items, ensure_ascii=False),
                     encoding="utf-8")
        old_limit = _cfg.SOURCE_MAX_ELEMENT_BYTES
        _cfg.SOURCE_MAX_ELEMENT_BYTES = 30
        old_chunk = js._CHUNK
        js._CHUNK = 8
        try:
            with pytest.raises(js.JsonStreamError) as ei:
                with open(f, encoding="utf-8") as fh:
                    list(js.iter_top_level_array(fh))
        finally:
            js._CHUNK = old_chunk
            _cfg.SOURCE_MAX_ELEMENT_BYTES = old_limit
        assert ei.value.code == "SOURCE_ELEMENT_TOO_LARGE"


class TestCliExitCode:
    """裁定（2026-10-04）：轻量直接验证——CLI shell 退出码与函数
    返回一致（backup 成功 0；坏目录 restore 1），不建大套件。"""

    def test_backup_exit_zero_and_bad_restore_exit_one(self, actors):
        import os
        import subprocess
        import sys as _sys
        from pathlib import Path as _P
        repo = _P(__file__).resolve().parents[2]
        env = dict(os.environ)
        r1 = subprocess.run(
            [_sys.executable, "-B", "-m", "mariposa.storage", "backup"],
            cwd=repo / "backend", env=env, capture_output=True,
            text=True, timeout=120)
        assert r1.returncode == 0, r1.stderr[-300:]
        r2 = subprocess.run(
            [_sys.executable, "-B", "-m", "mariposa.storage", "restore",
             "--from", "/nonexistent-backup-dir-xyz"],
            cwd=repo / "backend", env=env, capture_output=True,
            text=True, timeout=120)
        assert r2.returncode == 1, r2.stdout[-200:]
