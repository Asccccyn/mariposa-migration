"""bootstrap 分页 cursor（§12.2 / v2-BOOT）：不静默截断、STALE 不一半新一半旧。

superseded：旧 raw 30 条分页用例由 V2-BOOT-03 废止（开窗默认包不含原文）；
分页语义改为 memory_days/plans 两段验证；raw 的显式分页仍由 raw.list_recent
（before 游标）承担。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from mariposa.bootstrap import service as bootstrap
from mariposa.errors import Forbidden, SnapshotStale
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
    }


def _today():
    return datetime.now(timezone.utc).astimezone(
        ZoneInfo("Asia/Shanghai")).date()


def test_no_raw_in_default_package_boot03(actors):
    _import(75)
    first = bootstrap.get("jiaming", "claude_chat", "claude_chat")
    assert "raw" not in first
    assert first["coverage"]["raw"] == "not_in_default_package"


def test_memory_days_pagination_no_silent_truncation(actors):
    today = _today()
    ids = set()
    for i in range(60):  # 窗口内 60 桶 > 段上限 50
        h = memory.hold(actors["jiaming"], text=f"分页桶 {i}",
                        memory_date=today.isoformat(), categories=["daily"], original_title="测试标题")
        ids.add(h["memory_id"])
    first = bootstrap.get("jiaming", "cc", "cc")
    md = first["memory_days"]
    assert md["count"] == 50 and md["total_in_window"] == 60
    seen = {m["memory_id"] for m in md["items"]}
    page2 = bootstrap.next_page("jiaming", "cc", first["snapshot_id"],
                                md["next_cursor"], section="memory_days")
    seen |= {m["memory_id"] for m in page2["items"]}
    assert seen == ids  # 全量可达，无重叠无丢失
    assert page2["next_cursor"] is None


def test_page2_stale_when_resources_change(actors):
    first = bootstrap.get("jiaming", "cc", "cc")
    # 底层资源变化（记忆/计划/I/纪念日任一）→ 旧快照分页拒绝
    memory.hold(actors["jiaming"], text="新桶", memory_date=_today().isoformat(), categories=["daily"], original_title="测试标题")
    with pytest.raises(SnapshotStale):
        bootstrap.next_page("jiaming", "cc", first["snapshot_id"],
                            {"plans_offset": 0}, section="plans")


def test_i_change_invalidates_snapshot(actors):
    from mariposa.identity_i import service as i_svc
    first = bootstrap.get("jiaming", "cc", "cc")
    i_svc.write("jiaming", "I 正本第一版")
    with pytest.raises(SnapshotStale):
        bootstrap.get("jiaming", "cc", "cc",
                      loaded_snapshot_id=first["snapshot_id"])


def test_raw_section_removed_from_next_page(actors):
    first = bootstrap.get("jiaming", "claude_chat", "claude_chat")
    with pytest.raises(Forbidden):
        bootstrap.next_page("jiaming", "claude_chat", first["snapshot_id"],
                            {"raw_before": "x"}, section="raw")


def test_small_dataset_no_cursor(actors):
    _import(10)
    first = bootstrap.get("jiaming", "claude_chat", "claude_chat")
    assert first["cursor"]["next"] is None
    assert first["memory_days"]["next_cursor"] is None


def _import(n):
    """D13：legacy raw 导入退役——用现行 Source importer 铺数据。"""
    import json as _json
    import tempfile, pathlib as _pl
    from mariposa.source import importer
    base = datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)
    tmp = _pl.Path(tempfile.mkdtemp())
    convs = [{"uuid": f"c_{uuid.uuid4().hex[:6]}",
              "chat_messages": [
                  {"uuid": f"m{i:04d}", "sender":
                   "human" if i % 2 == 0 else "assistant",
                   "created_at": (base + timedelta(minutes=i))
                   .isoformat().replace("+00:00", "Z"),
                   "content": [{"type": "text",
                                "text": f"分页测试消息 {i}"}]}
                  for i in range(n)]}]
    f = tmp / "s.json"
    f.write_text(_json.dumps(convs, ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))
