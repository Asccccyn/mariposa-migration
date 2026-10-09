"""estómago 开窗 profile 回归（D2/2026-10-06 口径 A + D-6/2026-10-09
字段矩阵补全——撤销 mood_text 排除）。

- bootstrap.get 接受 estomago entry/profile；三天条目按 D-6 携带
  时间/心情标签+心情文字/事件正文/我们的话；有边桶附 relations 摘要
  （无边省略）；cc/claude_chat 行为不变（不带 event_text/our_words，
  mood_text 保持）。
- 分页（bootstrap.next）与快照 profile 一致：entry_source 与快照
  profile 不匹配拒绝；快照不跨 profile 复用。
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
    def test_estomago_entry_field_matrix(self, jiaming):
        """D-6：时间/心情标签+文字/事件正文/我们的话全进桶；无边省略
        relations 键（不造 false-null）。"""
        r = _hold_with_mood()
        # 加一条 our_word（通过 hold 的 our_words 参数）
        memory.hold(
            identity.Principal("jiaming", "周家明", "agent",
                               "estomago", "bj"),
            text="带话语的开窗事件", original_title="话语窗",
            categories=["sweet"], creation_mode="contemporaneous",
            mood={"tags": ["开心"], "text": "话语心情"},
            our_words=[{"speaker": "qiaosheng",
                        "text": "我们的原话canary",
                        "expression_kind": "verbatim"}],
            raw_pending=False)
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        items = pack["memory_days"]["items"]
        assert items, "当天记忆进入三日窗"
        by_mid = {it["memory_id"]: it for it in items}
        a = by_mid[r["memory_id"]]
        assert a.get("mood_tags") == ["开心"], "心情标签照给"
        assert a.get("mood_text") == "这段心情自由文字不应出 estomago 包", \
            "D-6：心情文字纳入 estomago 桶"
        assert a.get("event_text") == "estomago 开窗资料事件0", \
            "事件正文（当前 revision 正本）"
        assert "relations" not in a, "无边桶省略 relations 键"
        b = next(it for it in items if it["original_title"] == "话语窗")
        assert b.get("event_text") == "带话语的开窗事件"
        words = b.get("our_words") or []
        assert any(w["text"] == "我们的原话canary" for w in words), \
            "我们的话进桶"

    def test_cc_unchanged_matrix(self, jiaming):
        """非 estomago profile 字段矩阵不变（不带 event_text/our_words），
        mood_text 保持。"""
        _hold_with_mood()
        pack = bootstrap.get("jiaming", "cc", "cc")
        items = pack["memory_days"]["items"]
        assert items
        assert any("mood_text" in it for it in items), "cc 行为不变（仍含）"
        assert all("event_text" not in it and "our_words" not in it
                   for it in items), "cc 不展开事件/话语（矩阵不变）"

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
        if cursor is not None:
            page = bootstrap.next_page("jiaming", "estomago", snap_id,
                                       cursor, "memory_days")
            for it in page.get("items", []):
                assert "mood_text" in it, "D-6：续页同样携带心情文字"

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
            assert "mood_text" in it, "内置绑定同 estomago 矩阵（D-6）"


class TestEstomagoRelationsSummary:
    def test_relations_summary_attached_with_edges(self, jiaming):
        """有边桶附 relations 摘要（计数+类型去重，不带目标明细）。"""
        from mariposa.memory import service as mem
        r1 = _hold_with_mood(0)
        r2 = memory.hold(
            identity.Principal("jiaming", "周家明", "agent",
                               "estomago", "bj"),
            text="关联事件正文", original_title="关联桶",
            categories=["sweet"], creation_mode="contemporaneous",
            raw_pending=False)
        # v2.0 表 UNIQUE(from,to,type)——两条不同型边
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for rtype in ("related", "continues"):
                conn.execute(
                    "INSERT INTO memory_relations(from_memory, to_memory,"
                    " relation_type, created_by, created_at)"
                    " VALUES(?,?,?,?,datetime('now'))",
                    (r1["memory_id"], r2["memory_id"], rtype,
                     "jiaming",))
            conn.execute("COMMIT")
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        by_mid = {it["memory_id"]: it for it in pack["memory_days"]["items"]}
        a = by_mid[r1["memory_id"]]
        rel = a.get("relations")
        assert rel, "有边桶附 relations 摘要"
        assert rel["count"] == 2
        assert rel["types"] == ["continues", "related"], "类型去重排序"
        assert "to_memory" not in str(rel), "不带目标桶明细"
        assert "relations" in by_mid[r2["memory_id"]], \
            "双向端都见到边（from OR to 对称计数）"
