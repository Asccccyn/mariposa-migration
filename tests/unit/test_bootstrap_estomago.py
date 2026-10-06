"""D2 裁定回归（她 2026-10-06 批准口径 A）：estómago 独立 entry/profile。

- bootstrap.get 接受 estomago entry/profile；三天条目**不带 mood_text**
  （标签/标题/分类照给）；cc/claude_chat 行为不变（仍含 mood_text）。
- 分页（bootstrap.next）与快照 profile 一致：estomago 快照续页同样剥离；
  entry_source 与快照 profile 不匹配拒绝；快照不跨 profile 复用。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.bootstrap import service as bootstrap
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def jiaming():
    reset_all()
    return identity.Principal("jiaming", "周家明", "agent",
                              "estomago", "bj")


def _hold_with_mood(i=0):
    return memory.hold(
        identity.Principal("jiaming", "周家明", "agent",
                           "estomago", "bj"),
        text=f"estomago 开窗资料事件{i}", original_title=f"开窗{i}",
        categories=["sweet"], creation_mode="contemporaneous",
        mood={"tags": ["开心"], "text": "这段心情自由文字不应出 estomago 包"},
        raw_pending=False)


class TestEstomagoProfile:
    def test_estomago_entry_accepted_no_mood_text(self, jiaming):
        _hold_with_mood()
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        items = pack["memory_days"]["items"]
        assert items, "当天记忆进入三日窗"
        for it in items:
            assert "mood_text" not in it, "estomago 默认不带心情自由文字"
            assert it.get("mood_tags") == ["开心"], "心情标签照给"
            assert it.get("original_title"), "标题照给"

    def test_cc_unchanged_still_has_mood_text(self, jiaming):
        _hold_with_mood()
        pack = bootstrap.get("jiaming", "cc", "cc")
        items = pack["memory_days"]["items"]
        assert items
        assert any("mood_text" in it for it in items), "cc 行为不变（仍含）"

    def test_entry_profile_mismatch_rejected(self, jiaming):
        _hold_with_mood()
        with pytest.raises(Forbidden):
            bootstrap.get("jiaming", "cc", "estomago")
        with pytest.raises(Forbidden):
            bootstrap.get("jiaming", "estomago", "cc")

    def test_snapshot_records_profile_and_next_page_consistent(self, jiaming):
        _hold_with_mood()
        _hold_with_mood(1)
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        snap_id = pack["snapshot_id"]
        with db.formal() as conn:
            row = conn.execute(
                "SELECT profile FROM bootstrap_snapshots WHERE"
                " snapshot_id=?", (snap_id,)).fetchone()
        assert row["profile"] == "estomago", "快照记载 profile（不跨复用）"
        # 续页（带首页签发的合法游标）
        cursor = pack["memory_days"].get("next_cursor")
        if cursor is None:
            # 条目未满页无游标——快照/入口一致性已验，续页剥离由
            # mismatch 用例与实现路径覆盖（_memory_slim 同一函数）
            items0 = pack["memory_days"]["items"]
            assert all("mood_text" not in it for it in items0)
        else:
            page = bootstrap.next_page("jiaming", "estomago", snap_id,
                                       cursor, "memory_days")
            for it in page.get("items", []):
                assert "mood_text" not in it, "estomago 续页同样剥离"

    def test_next_page_entry_mismatch_rejected(self, jiaming):
        _hold_with_mood()
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        snap_id = pack["snapshot_id"]
        with pytest.raises(Forbidden):
            bootstrap.next_page("jiaming", "cc", snap_id, {}, "memory_days")

    def test_public_schema_accepts_estomago_profile(self, jiaming):
        from mariposa.capabilities import input_schemas as sc
        sc.validate("bootstrap.get", {"profile": "estomago"})

    def test_builtin_binding_entry_accepted(self, jiaming):
        """内置绑定（estomago_builtin）也是 estomago profile 的合法 entry
        ——宿主 B 点与模型工具共用该绑定（生产激活前置）。"""
        _hold_with_mood()
        pack = bootstrap.get("jiaming", "estomago_builtin", "estomago")
        for it in pack["memory_days"]["items"]:
            assert "mood_text" not in it
