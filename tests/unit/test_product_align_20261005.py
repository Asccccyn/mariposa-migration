"""2026-10-05 产品对齐批回归（江乔生裁定四项）。

1. why_remember 字段删除（解释槽归心情层；正文=唯一真相源）
2. 心情层放宽：跨窗口补记允许，evidence_state 如实标注
3. 原始标题必填 + ≤30 字符（全量浏览先扫标题）
4. 桶编号 = 分类字母 a..i + 四位序号，持久计数永不复用
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text="对齐批正文", title="对齐标题", cats=None, **kw):
    return memory.hold(actors["jiaming"], text=text,
                       original_title=title,
                       memory_date=kw.pop("memory_date", "2026-06-01"),
                       categories=cats or ["daily"], **kw)


class TestWhyRememberRemoved:
    def test_hold_rejects_why_argument(self, actors):
        """字段已删：经能力入口传 why_remember 是结构化拒绝。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.hold", {
                "text": "正文", "original_title": "标题",
                "categories": ["daily"],
                "why_remember": "理由"}, None)
        assert "why_remember" in str(ei.value)

    def test_update_rejects_why_argument(self, actors):
        h = _hold(actors)
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.update", {
                "memory_id": h["memory_id"], "expected_version": 1,
                "why_remember": "新理由"}, None)
        assert "why_remember" in str(ei.value)

    def test_get_no_longer_exposes_field(self, actors):
        h = _hold(actors)
        with db.formal() as conn:
            out = memory.get(conn, h["memory_id"])
        assert "why_remember" not in out


class TestTitleRequired:
    def test_missing_title_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            memory.hold(actors["jiaming"], text="无标题正文",
                        memory_date="2026-06-01", categories=["daily"])
        assert ei.value.code == "ORIGINAL_TITLE_REQUIRED"

    def test_overlong_title_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            _hold(actors, title="标" * 31)
        assert ei.value.code == "ORIGINAL_TITLE_TOO_LONG"

    def test_schema_level_required(self, actors):
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.hold", {
                "text": "正文", "categories": ["daily"]}, None)
        assert "original_title" in str(ei.value)


class TestBucketIds:
    def test_format_and_series(self, actors):
        a1 = _hold(actors, cats=["daily"])["memory_id"]
        c1 = _hold(actors, cats=["sad"])["memory_id"]
        a2 = _hold(actors, cats=["daily"])["memory_id"]
        assert (a1, c1, a2) == ("a0001", "c0001", "a0002"), \
            "字母按分类固定顺序，各分类独立四位序号"

    def test_no_reuse_after_delete(self, actors):
        """号段永不复用：删除后的重建拿到下一号，不回卷旧号。"""
        from mariposa.deletion import service as ds
        mid = _hold(actors, cats=["daily"])["memory_id"]
        ds.direct_delete("jiaming", mid)
        again = _hold(actors, cats=["daily"])["memory_id"]
        assert mid == "a0001" and again == "a0002", \
            "此前从存活行取 MAX 会在删除后回卷复用 a0001"

    def test_exhaustion_structured(self, actors):
        with db.formal() as conn:
            conn.execute(
                "INSERT INTO bucket_id_counters(category, next)"
                " VALUES('daily', 10000)")
        with pytest.raises(Forbidden) as ei:
            _hold(actors, cats=["daily"])
        assert ei.value.code == "BUCKET_ID_EXHAUSTED"


class TestMoodWindowRelaxed:
    def test_retrospective_mood_allowed_and_labeled(self, actors):
        out = _hold(actors, creation_mode="retrospective",
                    mood={"text": "后来补的解释", "tags": []})
        with db.formal() as conn:
            row = conn.execute(
                "SELECT mood_text, evidence_state FROM memory_moods"
                " WHERE memory_id=?", (out["memory_id"],)).fetchone()
        assert row["mood_text"] == "后来补的解释"
        assert row["evidence_state"] == "retrospective", \
            "补记如实标注，不冒充当时心境"

    def test_contemporaneous_still_labeled_so(self, actors):
        out = _hold(actors, creation_mode="contemporaneous",
                    mood={"text": "当时的心情", "tags": []})
        with db.formal() as conn:
            row = conn.execute(
                "SELECT evidence_state FROM memory_moods"
                " WHERE memory_id=?", (out["memory_id"],)).fetchone()
        assert row["evidence_state"] == "contemporaneous"


class TestLegacyWhyMigratesToMood:
    def test_apply_maps_why_to_mood(self, actors, tmp_path):
        """旧库 why_remembered 不丢弃：迁为心情层记录（解释槽归心情）。"""
        from mariposa import migration
        (tmp_path / "2026-07-01 10-00-00 对齐样本_aa11bb22cc33.md").write_text(
            "---\ntype: note\ndate: 2026-07-01\nwhy_remembered: 因为想留住那天\n---\n"
            "对齐批迁移正文", encoding="utf-8")
        r = migration.dry_run(str(tmp_path), None)
        assert r["counts"]["total"] == 1
        from tests.unit.test_audit1004_batch2 import _last_report_path
        out = migration.apply_from_report(_last_report_path(tmp_path, r))
        assert out["ok"] is True, out.get("problems")
        assert out["applied"] == 1
        assert out["legacy_why_to_mood"] == 1
        with db.formal() as conn:
            row = conn.execute(
                "SELECT mood_text FROM memory_moods"
                " ORDER BY captured_at DESC LIMIT 1").fetchone()
        assert row["mood_text"] == "因为想留住那天"
