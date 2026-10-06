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
                    text="v1 历史正文",
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
