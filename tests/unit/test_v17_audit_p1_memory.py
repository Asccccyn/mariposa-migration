"""v1.7 审计整改第一阶段回归：F04/F13/F14 统一正文与投影。

覆盖审计要求：
- event_text 修改后 update 再读取（v2 只改元数据，检索不丢正文）
- event_text 修改后 meaning（追加层重建用统一正文）
- event_text 修改后 rebuild（全库重建保持 v2 正文可检索）
- history 中旧 revision 与新 revision 分别正确（versions_read.text）
- 老数据只有旧字段（v1 hold_text）时的兼容读取
- 新数据同时存在字段时不得错误优先旧字段（event_text 优先）
- F13：rebuild_index 不再抛 NameError，返回含全部步骤统计
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.memory import extras as memory_extras
from mariposa.memory import listing as memory_listing
from mariposa.memory import service as memory
from mariposa.retrieval import rebuild as retrieval_rebuild
from mariposa.retrieval import search as retrieval_search
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold_v2(actors, text="傍晚沿河散步看见白鹭", title="散步的晚上",
            cats=None, principal="jiaming"):
    return memory.hold(
        actors[principal], text=text, memory_date="2026-08-15",
        date_confidence="exact", original_title=title,
        categories=cats or ["daily"], mood=None, our_words=None,
        creation_mode="contemporaneous", raw_pending=False)


def _search_body_hits(keyword: str) -> int:
    with db.formal() as conn:
        return len(retrieval_search.search(conn, keyword, 10)["hits"])


def _row(memory_id: str):
    with db.formal() as conn:
        return conn.execute(
            "SELECT * FROM memory_versions WHERE memory_id=?"
            " ORDER BY version_no", (memory_id,)).fetchall()


class TestF04UnifiedBody:
    def test_v2_update_metadata_keeps_body_searchable(self, actors):
        """只改 why/日期：v2 事件正文仍可从 legacy 检索命中。"""
        out = hold_v2(actors)
        mid = out["memory_id"]
        assert _search_body_hits("白鹭") == 1
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            why_remember="想记住那天的光线")
        with db.formal() as conn:
            assert memory.get(conn, mid)["text"] == "傍晚沿河散步看见白鹭"
        assert _search_body_hits("白鹭") == 1, "F04：元数据更新后正文从检索消失"

    def test_v2_update_body_change_indexes_new_text(self, actors):
        out = hold_v2(actors)
        mid = out["memory_id"]
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            text="傍晚沿河散步看见夜鹭与月亮")
        assert _search_body_hits("白鹭") == 0
        assert _search_body_hits("夜鹭") == 1
        with db.formal() as conn:
            assert memory.get(conn, mid)["text"] == "傍晚沿河散步看见夜鹭与月亮"

    def test_v2_meaning_append_keeps_body_searchable(self, actors):
        out = hold_v2(actors)
        mid = out["memory_id"]
        memory_listing.meanings_append(
        actors["jiaming"].principal_id, mid,
                                       "那天之后我们常走这条路")
        assert _search_body_hits("白鹭") == 1, "F04：meaning 追加后正文从检索消失"
        # meaning 层也进投影
        assert _search_body_hits("常走这条路") == 1

    def test_v2_meaning_replace_keeps_body_searchable(self, actors):
        out = hold_v2(actors)
        mid = out["memory_id"]
        memory_listing.meanings_append(
        actors["jiaming"].principal_id, mid, "第一层含义")
        memory_listing.meanings_replace(
        actors["jiaming"].principal_id, mid, ["替换后的含义"])
        assert _search_body_hits("白鹭") == 1
        assert _search_body_hits("替换后的含义") == 1

    def test_v2_full_rebuild_keeps_body_searchable(self, actors):
        out = hold_v2(actors)
        mid = out["memory_id"]
        memory_listing.meanings_append(
        actors["jiaming"].principal_id, mid, "重建前含义")
        result = retrieval_rebuild.rebuild_index()
        # F13：入口正常返回，不再抛 NameError
        assert result["rebuilt"]["full"] >= 1
        assert "field_projection" in result
        assert "words_index" in result
        assert "source_projection" in result
        assert _search_body_hits("白鹭") == 1, "F04：全库重建后正文从检索消失"
        assert _search_body_hits("重建前含义") == 1

    def test_event_text_not_overwritten_by_stale_hold_text(self, actors):
        """新数据两字段并存：event_text 优先，不得让 hold_text 覆盖。

        v2 版本行 hold_text=NULL、event_text=正文；update 生成的新版本行
        同样保持该形态，读取/投影一律取 event_text。
        """
        out = hold_v2(actors, text="正文唯一版本甲")
        mid = out["memory_id"]
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            text="正文第二版乙")
        rows = _row(mid)
        assert len(rows) == 2
        for r in rows:
            assert r["hold_text"] is None, "v2 版本行 hold_text 应保持 NULL"
        with db.formal() as conn:
            assert memory.get(conn, mid)["text"] == "正文第二版乙"


class TestLegacyCompat:
    def test_v1_hold_text_only_still_readable_and_searchable(self, actors):
        """老数据只有 hold_text：兼容 fallback 读取、检索、update 延续。"""
        # 直接落一行 v1 形态旧数据（绕过 v2 分层路径）
        mid = "mem_legacy0001"
        now = "2026-09-01T00:00:00+00:00"
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                memory._insert_core_rows(
                    conn, memory_id=mid, principal_id="jiaming",
                    text="旧式单一字段正文里的萤火虫", why_remember="旧数据",
                    memory_date="2026-07-01", date_confidence="exact",
                    mode="contemporaneous", original_title=None, v2=False,
                    now=now)
                from mariposa.memory import categories as cats_mod
                cats_mod.replace(conn, mid, ["daily"], "jiaming")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        with db.formal() as conn:
            assert memory.get(conn, mid)["text"] == "旧式单一字段正文里的萤火虫"
        assert _search_body_hits("萤火虫") == 1
        rows = _row(mid)
        assert rows[0]["event_text"] is None
        # 老数据 update：正文延续 hold_text，检索不丢
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1,
            why_remember="补充理由")
        with db.formal() as conn:
            assert memory.get(conn, mid)["text"] == "旧式单一字段正文里的萤火虫"
        assert _search_body_hits("萤火虫") == 1

    def test_v1_rebuild_and_meaning_compat(self, actors):
        mid = "mem_legacy0002"
        now = "2026-09-01T00:00:00+00:00"
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                memory._insert_core_rows(
                    conn, memory_id=mid, principal_id="jiaming",
                    text="旧式正文里的银杏叶", why_remember=None,
                    memory_date="2026-07-02", date_confidence="exact",
                    mode="contemporaneous", original_title=None, v2=False,
                    now=now)
                from mariposa.memory import categories as cats_mod
                cats_mod.replace(conn, mid, ["daily"], "jiaming")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        memory_listing.meanings_append(
        actors["jiaming"].principal_id, mid, "秋天含义")
        assert _search_body_hits("银杏叶") == 1
        retrieval_rebuild.rebuild_index()
        assert _search_body_hits("银杏叶") == 1
        assert _search_body_hits("秋天含义") == 1


class TestF14VersionsRead:
    def test_v2_versions_read_includes_event_body(self, actors):
        out = hold_v2(actors, text="历史首版正文有桂花")
        mid = out["memory_id"]
        memory_extras.update_text(
            actors["jiaming"].principal_id, mid, expected_version=1, text="历史次版正文有茉莉")
        with db.formal() as conn:
            versions = memory.versions_read(conn, mid)
        assert [v["version_no"] for v in versions] == [1, 2]
        # 每个 revision 的 text 准确反映对应版本，而不是混入 NULL
        assert versions[0]["text"] == "历史首版正文有桂花"
        assert versions[0]["hold_text"] is None  # 原始列保留可审计
        assert versions[1]["text"] == "历史次版正文有茉莉"

    def test_v1_versions_read_falls_back_to_hold_text(self, actors):
        mid = "mem_legacy0003"
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                memory._insert_core_rows(
                    conn, memory_id=mid, principal_id="jiaming",
                    text="v1 历史正文", why_remember=None,
                    memory_date="2026-07-03", date_confidence="exact",
                    mode="contemporaneous", original_title=None, v2=False,
                    now="2026-09-01T00:00:00+00:00")
                from mariposa.memory import categories as cats_mod
                cats_mod.replace(conn, mid, ["daily"], "jiaming")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        with db.formal() as conn:
            versions = memory.versions_read(conn, mid)
        assert versions[0]["text"] == "v1 历史正文"
        assert versions[0]["hold_text"] == "v1 历史正文"
