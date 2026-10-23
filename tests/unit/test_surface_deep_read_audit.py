"""Direct layered reads preserve full source/relations and honest errors."""
import hashlib

import pytest

from mariposa import config, db
from mariposa.capabilities import registry
from mariposa.memory import service as memory, detail_sections
from mariposa.memory.model_projection import detail_view
from tests.unit.test_hold_domain_op import seeded
from tests.unit.test_event_source_spans import event_args
from tests.unit.test_memory_detail import _receipt_with_detail


def pages(actor, section):
    args = {"detail_ref": "dref_test", "section": section, "view": "model"}
    result = []
    for _ in range(200):
        page = registry.invoke(actor, "memory.detail.read", args, None)["data"]
        result.append(page)
        if not page.get("next_cursor"):
            return result
        args["cursor"] = page["next_cursor"]
    pytest.fail("layered read did not terminate")


def test_source_layer_preserves_all_pinned_messages_and_long_fragments(seeded, monkeypatch):
    actor = seeded["actors"]["jiaming"]
    args = event_args(seeded)
    long = "全文🙂\n保留消息边界。" * 200
    member = args["event_source_ranges"][0]["members"][1]
    member["content_hash"] = hashlib.sha256(long.encode()).hexdigest()
    with db.formal() as conn:
        conn.execute("UPDATE source_messages SET text=?,content_hash=? WHERE id=?",
                     (long, member["content_hash"], member["source_message_id"]))
    held = registry.invoke(actor, "memory.hold", args, None)["data"]
    _receipt_with_detail(held["memory_id"], args["text"])
    monkeypatch.setattr(config, "SURFACE_DETAIL_PAGE_BYTES", 2048)
    result = pages(actor, "source")
    restored = []
    for page in result:
        for message in page.get("source", []):
            if restored and restored[-1]["message_id"] == message["message_id"]:
                assert message["partial_item"] is True
                restored[-1]["text"] += message["text"]
            else:
                restored.append(dict(message))
    assert len(result) > 2
    assert [m["text"] for m in restored] == ["第一句", long, "第三句"]
    assert [m["speaker"] for m in restored] == ["qiaosheng", "jiaming", "qiaosheng"]
    assert all("occurred_at" in m for m in restored)
    with db.formal() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_view_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_our_words").fetchone()[0] == 0


def test_relations_read_crosses_service_and_model_page_boundaries(actors, monkeypatch):
    actor = actors["jiaming"]
    held = memory.hold(actor, text="中心事件正文", original_title="中心事件", categories=["daily"],
                       creation_mode="retrospective", memory_date="2026-07-09")
    mid = held["memory_id"]
    others = []
    for i in range(205):
        other = memory.hold(actor, text=f"关联全文{i}", original_title=f"关联{i}", categories=["daily"],
                            creation_mode="retrospective", memory_date="2026-07-09")["memory_id"]
        others.append(other)
    with db.formal() as conn:
        conn.executemany("INSERT INTO memory_relations(relation_id,from_memory,to_memory,relation_type,created_by,created_at) VALUES(?,?,?,?,?,?)",
                         [(f"audit-rel-{i}", mid if i % 2 else other, other if i % 2 else mid,
                           "related_to", "jiaming", f"2026-07-09T00:00:{i % 60:02d}Z") for i, other in enumerate(others)])
    _receipt_with_detail(mid, "中心事件正文")
    monkeypatch.setattr(config, "SURFACE_DETAIL_PAGE_BYTES", 4096)
    result = pages(actor, "relations")
    relations = [r for page in result for r in page.get("relations", [])]
    assert len(result) > 1
    assert len(relations) == 205
    assert len({r["relation_id"] for r in relations}) == 205
    assert {r["text"] for r in relations} == {f"关联全文{i}" for i in range(205)}
    assert {r["direction"] for r in relations} == {"in", "out"}


def test_unavailable_source_cannot_be_slimmed_into_empty_success():
    projected = detail_view({"memory_id": "synthetic", "sections": {"source": None},
                             "section_status": {"source": "unavailable"}, "complete": True})
    assert projected["incomplete_sections"] == {"source": "unavailable"}
    assert "source" not in projected  # no invented messages
