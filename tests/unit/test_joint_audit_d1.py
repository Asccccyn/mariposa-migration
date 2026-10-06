"""F-J-03/D1 裁定回归（江乔生 2026-10-06）：当天记录自动日期。

裁定：contemporaneous 且未给日期 → 服务端同事务用 held_at 的上海业务日
填 memory_date；显式日期不覆盖（手填日期属于补写）；补写未给日期保持
NULL 不猜；date_confidence 不因自动填充改写；同 op 幂等重放返回原日期。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa.memory import listing, service as memory
from mariposa.recall import service as recall_service, store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def hold(actors, **kw):
    base = dict(text="当天事件正文", original_title="当天标题",
                categories=["sweet"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


def row(memory_id):
    with db.formal() as conn:
        return conn.execute(
            "SELECT memory_date, date_confidence, creation_mode,"
            " held_at FROM memories WHERE memory_id=?",
            (memory_id,)).fetchone()


class TestAutoDate:
    def test_contemporaneous_without_date_fills_shanghai_today(self, actors):
        out = hold(actors)
        r = row(out["memory_id"])
        today = datetime.now(timezone.utc).astimezone(
            ZoneInfo("Asia/Shanghai")).date().isoformat()
        assert r["memory_date"] == today, "自动填 held_at 上海业务日"
        assert r["creation_mode"] == "contemporaneous"

    def test_bootstrap_three_day_window_contains_it(self, actors):
        from mariposa.bootstrap import service as bootstrap
        out = hold(actors, text="三天窗应看到我", original_title="三日窗")
        pack = bootstrap.get("jiaming", "claude_chat", "claude_chat")
        dates = pack["memory_days"]["dates"]
        titles = [i.get("original_title")
                  for page_items in [pack["memory_days"]["items"]]
                  for i in page_items]
        assert "三日窗" in titles, f"bootstrap 三日窗（{dates}）含当天随手记"
        assert pack["memory_days"]["count"] >= 1

    def test_by_date_today_contains_it(self, actors):
        out = hold(actors, text="按日直达应看到我", original_title="按日")
        today = row(out["memory_id"])["memory_date"]
        page = listing.by_date(today)
        ids = [i["memory_id"] for i in page["items"]]
        assert out["memory_id"] in ids, "by_date 当日直达可见"

    def test_explicit_date_not_overwritten(self, actors):
        out = hold(actors, memory_date="2026-09-30",
                   date_confidence="exact")
        r = row(out["memory_id"])
        assert r["memory_date"] == "2026-09-30", "显式日期不被覆盖"
        assert r["date_confidence"] == "exact"

    def test_retrospective_without_date_stays_null(self, actors):
        out = hold(actors, creation_mode="retrospective",
                   text="补写不给日期", original_title="补写")
        r = row(out["memory_id"])
        assert r["memory_date"] is None, "补写未给日期不猜（保持 NULL）"

    def test_confidence_not_fabricated_by_autofill(self, actors):
        out = hold(actors, date_confidence="inferred")
        r = row(out["memory_id"])
        assert r["memory_date"] is not None, "自动填日期"
        assert r["date_confidence"] == "inferred", "调用方主张保持不变"

    def test_same_op_replay_returns_original_date(self, actors):
        from mariposa.capabilities import registry
        plan = {"original_request": "找当天的事", "channels": ["event"],
                "lexical_terms": ["当天"]}
        first = registry.invoke(actors["jiaming"], "memory.hold", {
            "text": "同 op 重放的当天事件", "original_title": "同op当天",
            "categories": ["sweet"], "creation_mode": "contemporaneous",
            "operation_id": "op-d1-replay"}, None)
        mid = first["data"]["memory_id"]
        d1 = row(mid)["memory_date"]
        assert d1 is not None
        again = registry.invoke(actors["jiaming"], "memory.hold", {
            "text": "同 op 重放的当天事件", "original_title": "同op当天",
            "categories": ["sweet"], "creation_mode": "contemporaneous",
            "operation_id": "op-d1-replay"}, None)
        assert again["data"]["memory_id"] == mid, "同 op 幂等重放同桶"
        assert again["data"]["idempotent_replay"] is True
        assert row(mid)["memory_date"] == d1, "重放不改写原事务日期"

    def test_held_at_shanghai_boundary_same_tx(self, actors):
        # 边界：UTC 16:00（上海 00:00 次日）写入 → 上海日=次日（同事务一致性）
        utc_next_day = datetime(2026, 10, 7, 16, 30,
                                tzinfo=timezone.utc).isoformat()
        # hold() 公开签名不收 now——走 hold_in_tx 同事务注入（与生产同一路径）
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = memory.hold_in_tx(
                    conn, actors["jiaming"],
                    text="跨日边界正文", original_title="边界",
                    categories=["sweet"],
                    creation_mode="contemporaneous",
                    now=utc_next_day)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        r = row(out["memory_id"])
        assert r["memory_date"] == "2026-10-08", "上海业务日（非 UTC 日）"
        assert r["held_at"] == utc_next_day, "held_at 原样"
