"""复审关卡 6：snapshot（生产→staging 字节副本）与 dry-run-real（真实规模映射）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mariposa import migration
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    yield


def _make_legacy(root: Path):
    for sub in ("dynamic/恋爱", "letters", "archive/feel", "i", "plans/active"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("dynamic/恋爱", "letters", "archive/feel", "i", "plans/active"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "dynamic/恋爱/2026-07-01 10-00-00 样本_aaa111222333.md").write_text(
        "---\ntype: dynamic\ndate: 2026-07-01\nimportance: 6\npinned: false\n"
        "meaning:\n  - 第一层\n---\n正文甲\n", encoding="utf-8")
    (root / "letters/2026-09-07 22-00-00 锁信_bbb444555666.md").write_text(
        "---\ntype: letter\nlock_type: timed\nunlock_date: \"2027-07-09\"\n---\n"
        "锁信正文（合成）\n", encoding="utf-8")
    (root / "archive/feel/2026-06-01 08-00-00 旧桶_ccc777888999.md").write_text(
        "---\ntype: archived\ndate: 2026-06-01\ndont_surface: true\n---\n旧正文\n",
        encoding="utf-8")
    (root / "i/2026-06-15 09-00-00 自我_ddd111222333.md").write_text(
        "---\ntype: i\ndate: 2026-06-15\n---\n我想成为……\n", encoding="utf-8")
    (root / "plans/active/2026-09-21 11-19-54_941fd0eb7652.md").write_text(
        "---\ntype: plan\ndate: 2026-09-21\nstatus: active\n---\n计划正文\n",
        encoding="utf-8")


class TestSnapshotAndRealDryRun:
    def test_snapshot_copies_bytes_with_manifest(self, actors, tmp_path):
        src = tmp_path / "src"
        _make_legacy(src)
        out = migration.snapshot(str(src), str(tmp_path / "staging"))
        assert out["ok"] is True and out["copied"] == 5
        manifest = json.loads(
            (tmp_path / "staging/_manifest.json").read_text(encoding="utf-8"))
        assert len(manifest["files"]) == 5
        # 逐字节一致（hash 核对）
        import hashlib
        orig = src / "dynamic/恋爱/2026-07-01 10-00-00 样本_aaa111222333.md"
        copy = tmp_path / "staging/dynamic/恋爱/2026-07-01 10-00-00 样本_aaa111222333.md"
        assert hashlib.sha256(orig.read_bytes()).hexdigest() == \
               hashlib.sha256(copy.read_bytes()).hexdigest()

    def test_snapshot_refuses_nonempty_dest(self, actors, tmp_path):
        src = tmp_path / "src"
        _make_legacy(src)
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "x.md").write_text("占位", encoding="utf-8")
        out = migration.snapshot(str(src), str(dest))
        assert out["ok"] is False and "not empty" in out["error"]

    def test_real_dry_run_full_mapping(self, actors, tmp_path):
        src = tmp_path / "src"
        _make_legacy(src)
        migration.snapshot(str(src), str(tmp_path / "staging"))
        report = tmp_path / "real.json"
        out = migration.dry_run_real(str(tmp_path / "staging"), str(report))
        st = out["stats"]
        assert st["total"] == 5
        assert st["by_target"] == {"memories": 1, "self_entries": 1,
                               "plans": 1}
        oos = [e for e in out["entries"] if e.get("target")
               == "out_of_scope"]
        assert len(oos) >= 1  # archived（v2.0 不迁移；letter 另路处理）
        assert st["with_meaning"] == 1
        # letter 桶出范围：计 unknown_type 且落 UNMAPPED（无正文无哈希）
        assert st["unknown_type"] == 1 and st["bad_frontmatter"] == 0
        unmapped = [e for e in out["entries"] if e["target"] == "UNMAPPED"]
        assert len(unmapped) == 1 and unmapped[0]["type"] == "letter"
        # 正文与锁信正文不进报告（断言完整正文串，避开 note 文案）
        text = report.read_text(encoding="utf-8")
        assert "锁信正文（合成）" not in text and "正文甲" not in text
        assert "旧正文" not in text and "我想成为" not in text
        # v2.0：archived 桶 out_of_scope，dont_surface 不再入扩展清单
