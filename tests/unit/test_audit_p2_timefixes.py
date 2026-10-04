"""P2 时间语义修复回归（审计 2026-10-03）。

- F13：plans 临近窗口按业务时区自然日比较（UTC 锚的字符串日期
  会把上海次日计划错档纳入/排除）。
- F14：time_context 最近用户联系只认已发布 Source 消息。
- F15：混合 offset 的时间事实按规范化瞬时比较（字符串比较会把
  `20:00+08` 错选过同日 `15:00Z`）。
"""
from __future__ import annotations

from datetime import date

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa.plans import service as plans
from mariposa.time_context import service as tctx
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


# ---------------------------------------------------------------- F13

def test_f13_upcoming_window_uses_local_date(actors):
    """业务今天 2026-10-03（0..3 日窗口到 10-06）：UTC `10-06T18:00Z`
    是上海 10-07（第 4 天）不得纳入；同一时刻 `+08:00` 书写（第 3 天）
    必须纳入——两写法是同一计划语义，只差时区书写。"""
    p4 = plans.create("jiaming", "上海第四天", state="planned",
                      starts_at="2026-10-06T18:00:00Z")
    p3 = plans.create("jiaming", "上海第三天", state="planned",
                      starts_at="2026-10-06T18:00:00+08:00")
    ids = {p["plan_id"] for p in plans.bootstrap_plans(date(2026, 10, 3), 3)}
    assert p4["plan_id"] not in ids, "UTC 锚的上海第 4 天不得进 0..3 窗口"
    assert p3["plan_id"] in ids, "+08 锚的上海第 3 天必须进窗口"


# ---------------------------------------------------------------- F14/F15

def _seed_source_row(conn, mid, created_at, published, sender="human",
                     seq=1):
    conn.execute(
        "INSERT INTO source_conversations(id, provider,"
        " provider_conversation_id, created_at, first_import_batch_id,"
        " last_import_batch_id)"
        " VALUES(?, 'claude', ?, datetime('now'), ?, ?)",
        (f"conv-{mid}", f"conv-{mid}", f"batch-{mid}", f"batch-{mid}"))
    conn.execute(
        "INSERT INTO source_messages(id, conversation_id, provider,"
        " provider_conversation_id, provider_message_id, normalized_sender,"
        " speaker, created_at, text, sequence, import_batch_id, published)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, f"conv-{mid}", "claude", f"conv-{mid}", mid, sender,
         "qiaosheng" if sender == "human" else None, created_at,
         f"合成消息 {mid}", seq, f"batch-{mid}", published))


def test_f14_unpublished_not_counted_as_contact(actors):
    with db.formal() as conn:
        _seed_source_row(conn, "f14-pub", "2026-10-01T10:00:00Z", 1)
        _seed_source_row(conn, "f14-unpub", "2026-10-02T10:00:00Z", 0)
    out = tctx.context("jiaming")
    assert out["last_user_message_at"] == "2026-10-01T10:00:00Z", \
        "未发布消息不得产生最近联系时间"


def test_f15_mixed_offsets_compare_by_instant(actors):
    with db.formal() as conn:
        # 15:00Z（15UTC）晚于 20:00+08（12UTC）：字符串比较会错选后者
        _seed_source_row(conn, "f15-plus8", "2026-10-01T20:00:00+08:00", 1)
        _seed_source_row(conn, "f15-z", "2026-10-01T15:00:00Z", 1, seq=2)
    out = tctx.context("jiaming")
    assert out["last_user_message_at"] == "2026-10-01T15:00:00Z", \
        "混合 offset 必须按瞬时比较，20:00+08 不是更晚"
