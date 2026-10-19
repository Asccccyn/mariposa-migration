"""Public Bootstrap contract for estómago; synthetic data, no model calls."""
import json

import pytest

from mariposa.capabilities import registry
from mariposa.errors import Forbidden, SnapshotStale
from mariposa.identity import Principal
from mariposa.memory import extras, service as memory


@pytest.fixture
def actor(actors):
    return Principal("jiaming", "char", "agent", "estomago", "binding_jiaming")


def call(actor, capability, **args):
    wire = registry.invoke(actor, capability, {**args, "output_profile": "compact_v1"}, None)
    assert len(json.dumps(wire, ensure_ascii=False).encode("utf-8")) <= 24576
    assert wire["data"]["instruction_authority"] == "none"
    return wire["data"]


@pytest.mark.parametrize("text", ["", "短心情😀", "😀\x01心" * 2000 + "心情尾标记"])
def test_mood_codepoint_cursor_and_empty_final_shape(actor, text):
    mid = memory.hold(actor, "合成事件", original_title="心情分页",
                      categories=["daily"], creation_mode="contemporaneous",
                      mood={"text": text, "tags": ["开心"]})["memory_id"]
    first = call(actor, "bootstrap.get", profile="estomago")
    item = first["memory_days"]["items"][0]
    parts = [item["mood_text"]]
    cursor = item.get("mood_text_next")
    assert bool(item.get("mood_text_truncated")) == bool(cursor)
    while cursor:
        assert cursor["memory_id"] == mid and cursor["field"] == "mood_text"
        assert type(cursor["offset"]) is int
        assert cursor["offset"] == len("".join(parts))
        page = call(actor, "bootstrap.next", snapshot_id=first["snapshot_id"],
                    section="memory_item", cursor=cursor)
        assert page["content"] and page["offset"] == cursor["offset"]
        assert page["total_chars"] == len(text)
        parts.append(page["content"])
        cursor = page["next_cursor"]
    assert "".join(parts) == text
    terminal = call(actor, "bootstrap.next", snapshot_id=first["snapshot_id"],
                    section="memory_item", cursor={"memory_id": mid, "field": "mood_text", "offset": len(text)})
    assert terminal["content"] == "" and terminal["next_cursor"] is None
    for bad in (-1, True, "1", 1.5, len(text) + 1):
        with pytest.raises(Forbidden):
            call(actor, "bootstrap.next", snapshot_id=first["snapshot_id"],
                 section="memory_item", cursor={"memory_id": mid, "field": "mood_text", "offset": bad})
    extras.update_mood("jiaming", mid, {"text": "新心情", "tags": []})
    with pytest.raises(SnapshotStale):
        call(actor, "bootstrap.next", snapshot_id=first["snapshot_id"],
             section="memory_item", cursor={"memory_id": mid, "field": "mood_text", "offset": 0})
