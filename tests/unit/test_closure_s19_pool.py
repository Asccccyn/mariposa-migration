"""S19 提前收口（江乔生指示，四轮复审前）：event 候选池分页取尽 +
coverage 诚实化（林石见三轮引用的已知未闭项，原排 WP07）。

反例：第 2001+ 个作用域桶永远不被检索（LIMIT 2000 截断），coverage
却仍签 complete_within_scope。
"""
from __future__ import annotations

import pytest

from mariposa import config as cfg, db
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _fillers(n: int):
    """0- 前缀填料桶（'0-' 排序在纯数字编号之前）（memory_id 排序在 mem_* 之前），有 phase 事实、
    无版本无字段文档——参与池迭代但永不命中。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT INTO memories(memory_id, current_version_no,"
                " memory_date, date_confidence, visibility,"
                " compression_state, created_at, updated_at, held_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [(f"0-filler-{i:05d}", 1, "2026-09-01", "exact",
                  "active", "full", "2026-09-01T00:00:00+00:00",
                  "2026-09-01T00:00:00+00:00",
                  "2026-09-01T00:00:00+00:00") for i in range(n)])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _hold_hit(actors):
    return memory.hold(
        actors["jiaming"], text="深巷尽头的乌桕树记录",
        memory_date="2026-09-25", date_confidence="exact",
        original_title="s19", categories=["daily"],
        creation_mode="contemporaneous", raw_pending=False)


def _start(actors, terms=("乌桕",)):
    return recall_service.start(actors["jiaming"], {"query_plan": {
        "original_request": "乌桕", "channels": ["event"],
        "lexical_terms": list(terms)}})


class TestS19Pool:
    def test_bucket_2005_now_retrieved(self, actors):
        """2004 个 a 填料 + 命中桶（mem_* 排序在第 2005 位）——
        旧 LIMIT 2000 永远扫不到它，分页取尽后必须命中且签 complete。"""
        _fillers(2004)
        _hold_hit(actors)
        p = _start(actors)
        refs = [c.get("resource_ref") for c in p["candidates"]]
        # 2026-10-05 新编号：分类字母+四位（与旧 mem_ 并存）
        assert any(r and r.startswith("memory:") for r in refs), \
            f"第 2005 个桶必须被检索到：{refs}"
        assert p["coverage"]["event"] == "complete_within_scope"
        assert p["coverage"]["event_pool"] == {"scanned": 2005,
                                               "truncated": False}

    def test_cap_truncation_is_honest(self, actors, monkeypatch):
        """安全阀触顶：不签 complete、漏桶不冒充"完整搜过没有候选"，
        round2 升级被 incomplete_families 正确拒绝。"""
        monkeypatch.setattr(cfg, "RECALL_POOL_MAX_BUCKETS", 50)
        _fillers(60)
        _hold_hit(actors)  # mem_* 排在 60 个填料后——未进扫描窗
        p = _start(actors)
        assert p["coverage"]["event"] == "partial_pool_truncated"
        assert p["coverage"]["event_pool"]["truncated"] is True
        assert p["candidates"] == []
        with pytest.raises(Forbidden) as ei:
            recall_service.round2(actors["jiaming"], {
                "session_id": p["recall_session_id"],
                "reason": "NO_DELIVERABLE_CANDIDATE"})
        gate = ei.value.detail["gate"]
        assert gate.get("retrieval_complete") is False
        assert "event" in gate.get("incomplete_families", [])

    def test_exactly_cap_still_complete(self, actors, monkeypatch):
        """恰好扫完 = 完整（触顶探针精确性）：50 个桶全进窗、无剩余，
        不得保守误标 truncated。"""
        monkeypatch.setattr(cfg, "RECALL_POOL_MAX_BUCKETS", 50)
        _fillers(49)
        _hold_hit(actors)  # 第 50 个，在窗内
        p = _start(actors)
        assert p["coverage"]["event"] == "complete_within_scope"
        assert p["coverage"]["event_pool"] == {"scanned": 50,
                                               "truncated": False}
        refs = [c.get("resource_ref") for c in p["candidates"]]
        assert any(r and r.startswith("memory:") for r in refs)
