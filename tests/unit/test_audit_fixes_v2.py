"""2026-09-23 独立审计修复回归（对应 V2-RET-08/09/11、V2-REV-05/13、
V2-SEARCH-03/05、V2-OPS-02、V2-BOOT-05、V2-PLAN-02）。

每条用例对应审计发现的真实缺陷（复现脚本 .pytest_tmp/audit_repro.py
口径），修复前后行为差异见 docs/AUDIT_REPORT_v2_20260923.md。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.capabilities import input_schemas, registry
from mariposa.errors import Forbidden, NotFound, ProposalStale, SnapshotStale
from mariposa.identity import service as identity
from mariposa import biztime as ret_mod
from mariposa.memory import service as memory
from mariposa.memory import views as views_mod
from mariposa.retrieval import search as rsearch
from mariposa.plans import service as plans
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
    }


def hold_due(actors, key: str, cats=("daily",)) -> str:
    """v2 hold 并把首次 hold 时刻拨到 40 天前（已到期）。"""
    out = memory.hold(
        actors["jiaming"], text=f"事件正文-{key}",
        original_title=f"标题-{key}", categories=list(cats),
        creation_mode="contemporaneous", memory_date="2026-08-01")
    mid = out["memory_id"]
    past = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    with db.formal() as conn:
        conn.execute("UPDATE memories SET held_at=? WHERE memory_id=?",
                     (past, mid))
    return mid



class TestSearchDedupAndLabels:
    """V2-SEARCH-05：search 合并去重；matched_fields 如实标注。"""

    def test_related_of_dedup(self, actors):
        from mariposa.memory import relations as rel
        a = memory.hold(actors["jiaming"], text="烧烤探针事件甲",
                        memory_date="2026-09-01", date_confidence="exact",
                        raw_pending=False, categories=["daily"])["memory_id"]
        b = memory.hold(actors["jiaming"], text="无关正文乙",
                        memory_date="2026-09-02", date_confidence="exact",
                        raw_pending=False, categories=["daily"])["memory_id"]
        rel.link("jiaming", a, b, "related_to")
        with db.formal() as conn:
            out = rsearch.search(conn, "烧烤", related_of=b)
        ids = [h["memory_id"] for h in out["hits"]]
        assert ids.count(a) == 1  # 关键词+关联命中只出现一次

    def test_occurred_range_persisted_and_filterable(self, actors):
        """SEARCH-03：occurred 区间落库（不再吞参）且参与日期筛选。"""
        out = memory.hold(actors["jiaming"], text="跨零点的事件",
                          memory_date="2026-09-10",
                          date_confidence="exact", raw_pending=False,
                          occurred_start="2026-09-09T22:00:00+08:00",
                          occurred_end="2026-09-10T01:30:00+08:00", categories=["daily"])
        mid = out["memory_id"]
        with db.formal() as conn:
            got = memory.get(conn, mid)
        assert got["occurred_start"].startswith("2026-09-09")
        # memory_date 不在窗口内，但 occurred 区间与 09-09 重叠 → 命中
        with db.formal() as conn:
            out2 = rsearch.recall(conn, filters={
                "event_date": {"from": "2026-09-09", "to": "2026-09-09"}})
        assert mid in {h["memory_id"] for h in out2["hits"]}


class TestBootstrapSnapshotComponents:
    """V2-BOOT-05/07：快照指纹含业务日期；跨日续页拒绝。"""

    def test_next_page_cross_day_stale(self, actors):
        from mariposa.bootstrap import service as boot
        got = boot.get("jiaming", "cc", "cc")
        snap_id = got["snapshot_id"]
        # 正常续页可用（同日、状态未变）
        page = boot.next_page("jiaming", "cc", snap_id,
                              {"plans_offset": 0}, section="plans")
        assert page["section"] == "plans"
        # 人为把快照业务日期拨到另一天 → 跨日续页必须 SNAPSHOT_STALE
        with db.formal() as conn:
            conn.execute(
                "UPDATE bootstrap_snapshots SET business_date=? WHERE"
                " snapshot_id=?", ("2000-01-01", snap_id))
        with pytest.raises(SnapshotStale):
            boot.next_page("jiaming", "cc", snap_id,
                           {"plans_offset": 0}, section="plans")


class TestIdempotencyBusinessFailure:
    """V2-OPS-05：干净业务拒绝不伪装成 OUTCOME_UNKNOWN。"""

