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


class TestRRA005StateHashCoversD6:
    def test_word_and_relation_change_invalidate_snapshot(self, jiaming):
        """RRA-005：D-6 新增输出依赖（our_words/memory_relations）写入后
        旧 snapshot 必须失效（loaded_snapshot_id 复用返回 SNAPSHOT_STALE
        语义——unchanged=true 不得再出现）。"""
        from mariposa.memory import service as mem
        r = _hold_with_mood(0)
        pack1 = bootstrap.get("jiaming", "estomago", "estomago")
        snap = pack1["snapshot_id"]
        # 追加 our_words（不触 memories.updated_at）
        mem.hold(
            identity.Principal("jiaming", "周家明", "agent",
                               "estomago", "bj"),
            text="追加话语的事件正文", original_title="追加话语",
            categories=["sweet"], creation_mode="contemporaneous",
            our_words=[{"speaker": "qiaosheng", "text": "NEW_WORD_CANARY",
                        "expression_kind": "verbatim"}],
            raw_pending=False)
        from mariposa.errors import SnapshotStale as _Stale
        try:
            bootstrap.get("jiaming", "estomago", "estomago",
                          loaded_snapshot_id=snap)
            raise AssertionError("话语写入后旧快照必须失效（不得 unchanged）")
        except _Stale:
            pass  # RRA-005：显式 SNAPSHOT_STALE（比 unchanged=False 更强）
        # 建关系边（同因）
        pack3 = bootstrap.get("jiaming", "estomago", "estomago")
        snap3 = pack3["snapshot_id"]
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO memory_relations(from_memory, to_memory,"
                " relation_type, created_by, created_at)"
                " VALUES(?,?,?,?,datetime('now'))",
                (r["memory_id"], pack3["memory_days"]["items"][-1][
                    "memory_id"], "related", "jiaming"))
            conn.execute("COMMIT")
        try:
            bootstrap.get("jiaming", "estomago", "estomago",
                          loaded_snapshot_id=snap3)
            raise AssertionError("关系边写入后旧快照必须失效")
        except _Stale:
            pass


class TestRRA006BudgetAndSectioning:
    def _big_hold(self, actors_unused=None):
        import mariposa.identity.service as ids
        return memory.hold(
            ids.Principal("jiaming", "周家明", "agent", "estomago", "bj"),
            text="事" * 11000, original_title="巨桶",
            categories=["sweet"], creation_mode="contemporaneous",
            our_words=[{"speaker": "qiaosheng",
                        "text": "话" * 1500,
                        "expression_kind": "verbatim"}] * 2,
            raw_pending=False)

    def test_full_envelope_within_budget_and_spliceable(self, jiaming):
        """RRA-006：estomago 大桶（11k 字 event+3k 字 words）——整包
        ≤24576；event/words 分节标记+续取拼回完整。"""
        import json as _json
        r = self._big_hold()
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        blob = len(_json.dumps(pack, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"))
        assert blob <= 24576, f"整包 {blob}B 击穿 24576 信封预算"
        items = pack["memory_days"]["items"]
        assert items, "大桶条目在首页（分节后有界）"
        it = next(x for x in items if x["memory_id"] == r["memory_id"])
        assert it.get("event_text_truncated") is True, "长 event 标记截断"
        assert it.get("event_text_total_chars") == 11000
        # 续取拼回完整（event_text）
        cur = it.get("event_text_next")
        parts = [it["event_text"]]
        while cur:
            pg = bootstrap.next_page("jiaming", "estomago",
                                     pack["snapshot_id"], cur,
                                     "memory_item")
            parts.append(pg["content"])
            cur = pg.get("next_cursor")
        assert "".join(parts) == "事" * 11000, "event 续取拼回逐字完整"
        # our_words 条目级分节（首段+续取补齐全部条目）
        wcur = it.get("our_words_next")
        witems = list(it["our_words"])
        while wcur:
            pg = bootstrap.next_page("jiaming", "estomago",
                                     pack["snapshot_id"], wcur,
                                     "memory_item")
            witems += pg["items"]
            wcur = pg.get("next_cursor")
        assert len(witems) == 2, "words 续取补齐全部条目"

    def test_short_bucket_single_page_unchanged(self, jiaming):
        """短对照：正文/话语不超节宽——整带、无 truncated/next 键。"""
        r = _hold_with_mood(9)
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        it = next((x for x in pack["memory_days"]["items"]
                   if x["memory_id"] == r["memory_id"]), None)
        assert it is not None
        assert it.get("event_text") == "estomago 开窗资料事件9"
        assert "event_text_truncated" not in it
        assert "event_text_next" not in it


def _env_bytes(obj) -> int:
    import json as _json
    return len(_json.dumps({"ok": True, "data": obj}, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8"))


class TestRRA006FollowUp:
    """RRA-006（回访 2026-10-09 run-035424）：memory_item 公开 schema 可
    消费、多桶分页不漏桶且续页同预算、单条超宽话语按码点分节。"""

    def _hold(self, text, words=None, title="synthetic"):
        return memory.hold(
            identity.Principal("jiaming", "周家明", "agent",
                               "estomago", "bj"),
            text=text, original_title=title, categories=["daily"],
            creation_mode="contemporaneous",
            our_words=words, raw_pending=False)

    def test_registry_memory_item_via_public_schema(self, jiaming):
        """4013 字事件——首页分节后经 registry（公开 schema 面）
        bootstrap.next section=memory_item 续取拼回完整（此前 schema
        拒绝 SCHEMA_VIOLATION，分节承诺不可消费）。"""
        from mariposa.capabilities import registry as reg
        full = "EVENT_CANARY-" + "甲" * 4000
        self._hold(full)
        p = identity.Principal("jiaming", "n", "agent",
                               "estomago_builtin", "binding_jiaming")
        first = reg.invoke(p, "bootstrap.get", {"profile": "estomago"},
                           None)["data"]
        it = first["memory_days"]["items"][0]
        cur = it["event_text_next"]
        assert cur and cur["field"] == "event_text", "4013 字必分节"
        parts, snap = [it["event_text"]], first["snapshot_id"]
        while cur:
            r = reg.invoke(p, "bootstrap.next",
                           {"snapshot_id": snap,
                            "section": "memory_item", "cursor": cur},
                           None)["data"]
            parts.append(r["content"])
            cur = r.get("next_cursor")
        assert "".join(parts) == full, "registry 面续取拼回逐字完整"

    def test_seven_bucket_walk_no_gap_within_budget(self, jiaming):
        """7 个 ~6KB 桶——首页+全部续页均 ≤24576B；七桶全部可达不漏
        （此前首页游标取溢出行致漏第 2 桶；续页无预算 31250B）。"""
        ids = [self._hold("甲" * 1995 + f"{i:04d}", title=f"桶{i}")
               ["memory_id"] for i in range(7)]
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        assert _env_bytes(pack) <= 24576
        seen = [it["memory_id"] for it in pack["memory_days"]["items"]]
        cur = pack["memory_days"]["next_cursor"]
        pages = 0
        while cur and pages < 50:
            page = bootstrap.next_page("jiaming", "estomago",
                                       pack["snapshot_id"], cur,
                                       "memory_days")
            assert _env_bytes(page) <= 24576, "续页同预算（31250B 残因）"
            seen += [it["memory_id"] for it in page["items"]]
            cur = page.get("next_cursor")
            pages += 1
        assert sorted(set(seen)) == sorted(ids), "七桶全部可达（不漏桶）"
        assert len(seen) == len(set(seen)), "不重复交付"

    def test_single_long_word_sectioned_by_chars(self, jiaming):
        """单条 10000 字话语——首段按码点分节（整包 ≤24576；此前 31596B），
        条内 char_offset 续取拼回逐字完整。"""
        full = "话" * 10000
        mid = self._hold("small event",
                         words=[{"speaker": "qiaosheng", "text": full,
                                 "expression_kind": "verbatim"}]
                         )["memory_id"]
        pack = bootstrap.get("jiaming", "estomago", "estomago")
        assert _env_bytes(pack) <= 24576, \
            f"单长 word 整包 {_env_bytes(pack)}B 击穿预算（31596B 残因）"
        it = next(x for x in pack["memory_days"]["items"]
                  if x["memory_id"] == mid)
        assert it["our_words_truncated"] is True
        chunks = [w["text"] for w in it["our_words"]]
        cur = it["our_words_next"]
        assert cur.get("char_offset", 0) > 0, "单条超宽走条内字符游标"
        while cur:
            pg = bootstrap.next_page("jiaming", "estomago",
                                     pack["snapshot_id"], cur,
                                     "memory_item")
            chunks += [w["text"] for w in pg["items"]]
            cur = pg.get("next_cursor")
        assert "".join(chunks) == full, "条内分节拼回逐字完整"
