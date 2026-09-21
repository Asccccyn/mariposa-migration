"""bootstrap 分页 cursor（§12.2）：不静默截断、STALE 不一半新一半旧。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from mariposa.bootstrap import service as bootstrap
from mariposa.errors import Forbidden, SnapshotStale
from mariposa.identity import service as identity
from mariposa.raw import service as raw
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


def _import(n):
    base = datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
    raw.import_payload("worker", {
        "source_channel": "claude_export",
        "external_id": f"ex_{uuid.uuid4().hex[:8]}",
        "messages": [
            {"source_message_id": f"m{i:04d}", "role": "user" if i % 2 == 0 else "assistant",
             "body": f"分页测试消息 {i}", "occurred_at":
             (base + timedelta(minutes=i)).isoformat(), "sequence": i}
            for i in range(n)],
    })


def test_cursor_pagination_no_silent_truncation(actors):
    _import(75)
    first = bootstrap.get("jiaming", "claude_chat", "claude_chat")
    assert first["raw"]["count"] == 30
    assert first["cursor"]["next"] is not None
    seen = {m["source_message_id"] for m in first["raw"]["messages"]}

    second = bootstrap.next_page("jiaming", "claude_chat", first["snapshot_id"],
                                 first["cursor"]["next"])
    assert second["raw"]["count"] == 30
    second_ids = {m["source_message_id"] for m in second["raw"]["messages"]}
    assert not seen & second_ids  # 页间不重叠
    seen |= second_ids

    third = bootstrap.next_page("jiaming", "claude_chat", first["snapshot_id"],
                                second["cursor"]["next"])
    assert third["raw"]["count"] == 15
    seen |= {m["source_message_id"] for m in third["raw"]["messages"]}
    assert third["cursor"]["next"] is None
    assert len(seen) == 75  # 全量可达，无静默丢失


def test_page2_stale_when_resources_change(actors):
    _import(40)
    first = bootstrap.get("jiaming", "claude_chat", "claude_chat")
    _import(5)  # 新消息到来
    with pytest.raises(SnapshotStale):
        bootstrap.next_page("jiaming", "claude_chat", first["snapshot_id"],
                            first["cursor"]["next"])


def test_cc_profile_has_no_pagination(actors):
    first = bootstrap.get("jiaming", "cc", "cc")
    assert first["cursor"]["next"] is None


def test_small_dataset_no_cursor(actors):
    _import(10)
    first = bootstrap.get("jiaming", "claude_chat", "claude_chat")
    assert first["raw"]["count"] == 10
    assert first["cursor"]["next"] is None
