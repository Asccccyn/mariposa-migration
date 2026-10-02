"""第 4 轮：flags/update、relations+relation 检索、绑定流、reminders、
maintenance、media、moments、批量+冷却、reserved 契约。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.errors import Forbidden, NotFound, VersionConflict
from mariposa.identity import service as identity
from mariposa.maintenance import service as maintenance
from mariposa.media import service as media
from mariposa.memory import extras, relations, service as memory
from mariposa.retrieval import search as retrieval
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


def _hold(actors, text="关联测试桶", date="2026-06-01"):
    return memory.hold(actors["jiaming"], text=text, memory_date=date, categories=["daily"])


class TestMemoryExtras:
    def test_update_creates_version_and_reindex(self, actors):
        h = _hold(actors, text="初版正文")
        u = extras.update_text("jiaming", h["memory_id"], 1,
                               text="修订后的正文包含新关键词榴莲")
        assert u["version"] == 2
        with db.formal() as conn:
            hits = retrieval.search(conn, "榴莲")["hits"]
        assert any(x["memory_id"] == h["memory_id"] for x in hits)
        with pytest.raises(VersionConflict):
            extras.update_text("jiaming", h["memory_id"], 1, text="旧版本")

    def test_versions_list_no_body(self, actors):
        h = _hold(actors)
        vs = extras.versions_list(h["memory_id"])
        assert set(vs[0]) == {"version_no", "representation", "origin_kind",
                              "authored_by", "confirmed_by", "payload_hash",
                              "created_at"}


class TestRelations:
    def test_link_direction_and_trace(self, actors):
        a = _hold(actors, "第一次尝试")
        b = _hold(actors, "第二次尝试")
        c = _hold(actors, "最终成功")
        relations.link("jiaming", b["memory_id"], a["memory_id"], "continuation_of")
        relations.link("jiaming", c["memory_id"], b["memory_id"], "continuation_of")
        fwd = relations.list_for(b["memory_id"], "out")
        rev = relations.list_for(b["memory_id"], "in")
        assert len(fwd) == 1 and fwd[0]["to_memory"] == a["memory_id"]
        assert len(rev) == 1 and rev[0]["reversed"] is True
        trace = relations.trace(c["memory_id"])
        assert trace["chain"] == [c["memory_id"], b["memory_id"], a["memory_id"]]

    def test_relation_search_channel(self, actors):
        a = _hold(actors, "海边拾贝")
        b = _hold(actors, "完全无关文本")
        relations.link("jiaming", b["memory_id"], a["memory_id"], "related_to")
        with db.formal() as conn:
            out = retrieval.search(conn, "zzz不存在的词", related_of=a["memory_id"])
        assert out["hits"] and out["hits"][0]["matched_by"] == "relation"

    def test_custom_label_rules(self, actors):
        a, b = _hold(actors, "x"), _hold(actors, "y")
        with pytest.raises(Forbidden):
            relations.link("jiaming", b["memory_id"], a["memory_id"],
                           "related_to", custom_label="私标")
        relations.link("jiaming", b["memory_id"], a["memory_id"], "custom",
                       custom_label="我们的暗号", reverse_label="被暗号")

    def test_detach_keeps_history(self, actors):
        a, b = _hold(actors, "x"), _hold(actors, "y")
        relations.link("jiaming", b["memory_id"], a["memory_id"], "related_to")
        relations.detach("jiaming", b["memory_id"], a["memory_id"], "related_to")
        assert relations.list_for(b["memory_id"], "out") == []
        with pytest.raises(NotFound):
            relations.detach("jiaming", b["memory_id"], a["memory_id"], "related_to")


class TestMaintenance:
    def test_outbox_drain_and_activity(self, actors):
        _hold(actors, "产生事件")
        status = maintenance.outbox_status()
        assert status["pending"] > 0
        drained = maintenance.outbox_drain()
        assert drained["drained"] > 0 and "memory.created" in drained["types"]
        assert maintenance.outbox_status()["pending"] == 0
        events = maintenance.activity_list(event_type="memory.created")
        assert events and events[0]["actor_principal"]


class TestMedia:
    def test_upload_dedupe_and_get(self, actors):
        png = b"\x89PNG\r\n\x1a\n" + b"A" * 100
        prep = media.upload_prepare("qiaosheng", "image/png", len(png))
        assert media.stage_bytes("qiaosheng", prep["upload_token"],
                                  png)["staged"]
        out = media.upload_finalize("qiaosheng", prep["upload_token"])
        assert out["deduplicated"] is False
        prep2 = media.upload_prepare("jiaming", "image/png", len(png))
        assert media.stage_bytes("jiaming", prep2["upload_token"],
                                  png)["staged"]
        out2 = media.upload_finalize("jiaming", prep2["upload_token"])
        assert out2["deduplicated"] is True
        assert out["content_hash"] == out2["content_hash"]
        meta, path = media.get_media("qiaosheng", out["content_hash"])
        assert path.exists() and meta["mime"] == "image/png"
        assert len(media.list_media()) == 1  # 同 hash 一行

    def test_bad_mime_rejected(self, actors):
        with pytest.raises(Forbidden):
            media.upload_prepare("qiaosheng", "application/x-msdownload", 10)
        with pytest.raises(Forbidden):
            media.upload_prepare("qiaosheng", "image/png", 0)

    def test_size_mismatch_rejected(self, actors):
        prep = media.upload_prepare("qiaosheng", "image/png", 100)
        with pytest.raises(Forbidden):
            media.stage_bytes("qiaosheng", prep["upload_token"], b"short")




class TestReservedContracts:
    def test_emotion_and_listening_reserved(self, actors):
        from mariposa.capabilities import registry
        emo = registry.invoke(actors["qiaosheng"], "emotion.context.get", {}, None)
        assert emo["data"]["status"] == "reserved"
        assert emo["data"]["enabled"] is False
        lis = registry.invoke(actors["qiaosheng"], "listening.status", {}, None)
        assert lis["data"]["status"] == "reserved"
        assert lis["data"]["provider"] is None
