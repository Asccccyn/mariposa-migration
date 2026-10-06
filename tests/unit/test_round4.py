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
    return memory.hold(actors["jiaming"], text=text, memory_date=date, categories=["daily"], original_title="测试标题")


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

    def test_versions_not_exposed_after_removal(self, actors):
        """2026-10-05 裁定：桶的修改历史查看入口已删（只保留 I 的
        修订历史）——能力层不再有 memory.versions.read/list。"""
        from mariposa.capabilities import registry as reg
        assert "memory.versions.read" not in reg.REGISTRY
        assert "memory.versions.list" not in reg.REGISTRY
        # 内部版本行仍在（冲突检测依赖），只是查看入口没了
        h2 = _hold(actors)
        extras.update_text("jiaming", h2["memory_id"], 1, text="第二版")
        with db.formal() as conn:
            c = conn.execute(
                "SELECT COUNT(*) c FROM memory_versions WHERE memory_id=?",
                (h2["memory_id"],)).fetchone()["c"]
        assert c == 2


class TestRelations:
    def test_link_direction_and_trace(self, actors):
        from mariposa.memory import relations as rel
        from mariposa.memory import service as memory
        a = memory.hold(actors["jiaming"], text="链A", memory_date="2026-09-25",
                        date_confidence="exact", original_title="ta",
                        categories=["daily"], creation_mode="contemporaneous",
                        raw_pending=False)
        b = memory.hold(actors["jiaming"], text="链B", memory_date="2026-09-25",
                        date_confidence="exact", original_title="tb",
                        categories=["daily"], creation_mode="contemporaneous",
                        raw_pending=False)
        rel.link("jiaming", a["memory_id"], b["memory_id"], "continuation_of")
        out = rel.list_for(a["memory_id"], "out")
        assert len(out) == 1 and out[0]["to_memory"] == b["memory_id"]
        back = rel.list_for(b["memory_id"], "in")
        assert back[0]["reversed"] is True
        tr = rel.trace(a["memory_id"])
        assert tr["earlier"] == [b["memory_id"]]  # A(新)的前序是 B
        tr2 = rel.trace(b["memory_id"])
        assert tr2["later"] == [a["memory_id"]]   # B 的后续是 A（方向不颠倒）

    def test_correction_replaces_detach(self, actors):
        """v2.0：detach 退役——纠错走 correct（历史留档、可重建新实例）。"""
        from mariposa.memory import relations as rel
        from mariposa.memory import service as memory
        a = memory.hold(actors["jiaming"], text="纠A", memory_date="2026-09-25",
                        date_confidence="exact", original_title="ta",
                        categories=["daily"], creation_mode="contemporaneous",
                        raw_pending=False)
        b = memory.hold(actors["jiaming"], text="纠B", memory_date="2026-09-25",
                        date_confidence="exact", original_title="tb",
                        categories=["daily"], creation_mode="contemporaneous",
                        raw_pending=False)
        out = rel.link("jiaming", a["memory_id"], b["memory_id"], "related_to")
        rid = out["relation_id"]
        assert not hasattr(rel, "detach")
        cor = rel.correct("jiaming", rid, "remove_wrong_binding",
                          note="绑错对象")
        assert cor["removed_relation_id"] == rid
        assert rel.list_for(a["memory_id"], "both") == []
        # 纠错后重建相同端点 = 新实例
        out2 = rel.link("jiaming", a["memory_id"], b["memory_id"],
                        "related_to")
        assert out2["relation_id"] != rid
        assert not out2.get("deduplicated")


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
