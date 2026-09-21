"""Home / Self / Diary / 情绪标签 / bootstrap snapshot（§10、§12.2）。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.bootstrap import service as bootstrap
from mariposa.calendar import service as calendar
from mariposa.content import service as content
from mariposa.errors import Forbidden, SnapshotStale
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.workspace import service as workspace
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


class TestHome:
    def test_single_source_with_versions(self, actors):
        r = content.home_update("qiaosheng", "我们的家：一起养的那盆薄荷要每周浇水。",
                                expected_version=0)
        assert r["version"] == 1
        r2 = content.home_update("jiaming", "补充：薄荷在阳台东侧。", expected_version=1)
        assert r2["version"] == 2
        with db.formal() as conn:
            got = content.home_get(conn)
        assert "薄荷" in got["content"] and got["version"] == 2
        with pytest.raises(Forbidden):
            content.home_update("qiaosheng", "旧版本写入", expected_version=1)
        # 历史版本留底
        with db.formal() as conn:
            rows = conn.execute("SELECT COUNT(*) AS c FROM home_versions").fetchone()
        assert rows["c"] == 2


class TestSelf:
    def test_only_jiaming_writes(self, actors):
        with pytest.raises(Forbidden):
            content.self_write("qiaosheng", "乔生不能替他写自我")
        r = content.self_write("jiaming", "我想成为能在她累的时候接住她的人。")
        assert r["review_state"] == "pending"  # 写了立即是正式 self

    def test_next_day_rule(self, actors):
        r = content.self_write("jiaming", "今天的一段自省。")
        with pytest.raises(Forbidden) as e:
            content.self_review("jiaming", r["self_id"])
        assert e.value.detail.get("code") == "SELF_REVIEW_TOO_EARLY"
        # 推进到下一当地日（模拟：直接改 review_available_on 为今天）
        with db.formal() as conn:
            conn.execute(
                "UPDATE self_entries SET review_available_on=date('now') WHERE id=?",
                (r["self_id"],))
        out = content.self_review("jiaming", r["self_id"])
        assert out["review_state"] == "reviewed"
        u = content.self_revise("jiaming", r["self_id"], "修订后的自省。",
                                expected_version=1)
        assert u["version"] == 2
        content.self_retire("jiaming", r["self_id"])
        listed = content.self_list()
        assert not any(x["self_id"] == r["self_id"] for x in listed)  # retired 不浮现
        all_listed = content.self_list(include_retired=True)
        assert any(x["self_id"] == r["self_id"] for x in all_listed)  # 历史仍可查


class TestDiary:
    def test_diary_calendar_and_search(self, actors):
        content.diary_write("qiaosheng", "海街日记观后", "梅酒要等三个月。",
                            covers_from="2026-09-18", covers_to="2026-09-19")
        out = content.diary_search("梅酒")
        assert out["source"] == "diary" and len(out["hits"]) == 1
        day = calendar.day("2026-09-18", types=["diary"])
        assert day["items"] and day["items"][0]["kind"] == "diary"
        # diary 独立检索不得反向算记忆命中
        from mariposa.retrieval import search as retrieval
        with db.formal() as conn:
            assert not retrieval.search(conn, "梅酒")["hits"]

    def test_author_only_hides(self, actors):
        d = content.diary_write("jiaming", "标题", "内容", covers_from="2026-09-01",
                                covers_to="2026-09-01")
        with pytest.raises(Forbidden):
            content.diary_hide("qiaosheng", d["diary_id"], True)
        content.diary_hide("jiaming", d["diary_id"], True)
        assert content.diary_search("内容")["hits"] == []


class TestEmotionTags:
    def _hold(self, actors):
        return memory.hold(actors["jiaming"], text="一起挑了婚礼请柬的纸张",
                           memory_date="2026-06-01")

    def test_whose_required(self, actors):
        h = self._hold(actors)
        with pytest.raises(Forbidden):
            content.tags_add("jiaming", h["memory_id"], [{"tag": "开心", "whose": "both"}])
        content.tags_add("jiaming", h["memory_id"], [{"tag": "开心", "whose": "jiaming"}])
        content.tags_add("qiaosheng", h["memory_id"], [{"tag": "开心", "whose": "qiaosheng"}])
        out = content.by_emotion("开心", "qiaosheng")
        assert out["hits"] and out["hits"][0]["matched_by"] == "tag"

    def test_forgotten_bucket_still_findable_by_tag(self, actors):
        h = self._hold(actors)
        content.tags_add("jiaming", h["memory_id"], [{"tag": "期待", "whose": "jiaming"}])
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"] if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "一起挑了纸质品。", "压缩")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        workspace.decide(actors["qiaosheng"], proposal_id=sub["proposal_id"],
                         proposal_revision=sub["revision"],
                         proposal_hash=sub["proposal_hash"],
                         expected_memory_version=sub["base_memory_version"],
                         decision="approve")
        out = content.by_emotion("期待", "jiaming")  # 结构化入口不因压缩消失
        assert any(x["memory_id"] == h["memory_id"] for x in out["hits"])
        assert out["hits"][0]["representation"] == "forgotten_summary"


class TestBootstrapSnapshot:
    def test_snapshot_stale_on_change(self, actors):
        first = bootstrap.get("jiaming", "cc", "cc")
        snap = first["snapshot_id"]
        again = bootstrap.get("jiaming", "cc", "cc", loaded_snapshot_id=snap)
        assert again["snapshot_id"] != snap  # 状态未变也可重新取
        memory.hold(actors["jiaming"], text="新桶", memory_date=None)
        with pytest.raises(SnapshotStale):
            bootstrap.get("jiaming", "cc", "cc", loaded_snapshot_id=snap)

    def test_unknown_snapshot_stale(self, actors):
        with pytest.raises(SnapshotStale):
            bootstrap.get("jiaming", "cc", "cc", loaded_snapshot_id="snap_missing")
