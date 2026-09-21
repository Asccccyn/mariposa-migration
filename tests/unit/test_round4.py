"""第 4 轮：flags/update、relations+relation 检索、绑定流、reminders、
maintenance、media、moments、批量+冷却、reserved 契约。"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.calendar import service as calendar
from mariposa.errors import Forbidden, NotFound, VersionConflict
from mariposa.identity import service as identity
from mariposa.maintenance import service as maintenance
from mariposa.media import service as media
from mariposa.memory import extras, relations, service as memory
from mariposa.moments import service as moments
from mariposa.raw import binding, service as raw
from mariposa.reminders import service as reminders
from mariposa.retrieval import search as retrieval
from mariposa.workspace import service as workspace
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
    return memory.hold(actors["jiaming"], text=text, memory_date=date)


class TestMemoryExtras:
    def test_flags_exclude_from_scan(self, actors):
        h = _hold(actors)
        assert extras.set_flag("qiaosheng", h["memory_id"], "protect", True)
        scan = workspace.scan_candidates(actors["worker"])
        assert not any(p["target_memory_id"] == h["memory_id"]
                       for p in scan["created"])

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


class TestRawBinding:
    def _import_conv(self, n=3):
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        raw.import_payload("worker", {
            "source_channel": "claude_export", "external_id": "bind_test",
            "messages": [
                {"source_message_id": f"bm{i}", "role": "user",
                 "body": f"绑定测试消息 {i}",
                 "occurred_at": (base + timedelta(minutes=i)).isoformat(),
                 "sequence": i} for i in range(n)]})

    def test_pending_then_bind_then_revoke(self, actors):
        h = memory.hold(actors["jiaming"], text="周末去了山里", memory_date="2026-06-01")
        with db.formal() as conn:
            state = conn.execute("SELECT source_state FROM memories WHERE memory_id=?",
                                 (h["memory_id"],)).fetchone()
        assert state["source_state"] == "raw_pending"
        frag = binding.report_fragment("jiaming", "我们那天在山里看到了云海",
                                       h["memory_id"])
        self._import_conv()
        conv = raw.conversations_list()[0]["id"]
        out = binding.bind("jiaming", h["memory_id"], conv, "bm0", "bm2")
        assert out["source_state"] == "bound" and out["messages"] == 3
        # Hold 内容不被绑定改写
        with db.formal() as conn:
            got = memory.get(conn, h["memory_id"])
        assert got["text"] == "周末去了山里"
        binding.revoke("jiaming", h["memory_id"], conv)
        refs = binding.refs_of(h["memory_id"])
        assert refs[0]["bind_confidence"] == "revoked"  # 留历史

    def test_dedupe_needs_review(self, actors):
        h1 = memory.hold(actors["jiaming"], text="第一个桶", memory_date="2026-06-01")
        h2 = memory.hold(actors["jiaming"], text="第二个桶", memory_date="2026-06-01")
        self._import_conv()
        conv = raw.conversations_list()[0]["id"]
        binding.bind("jiaming", h1["memory_id"], conv, "bm0", "bm2")
        with pytest.raises(Forbidden) as e:
            binding.bind("jiaming", h2["memory_id"], conv, "bm0", "bm2")
        assert e.value.detail.get("code") == "DEDUPE_NEEDS_REVIEW"

    def test_fragment_only_jiaming(self, actors):
        with pytest.raises(Forbidden):
            binding.report_fragment("qiaosheng", "乔生不能替他报告片段")


class TestRemindersAndCalendar:
    def test_reminder_lifecycle_and_calendar(self, actors):
        r = reminders.create("qiaosheng", "拿体检报告",
                             remind_at="2026-09-25T09:00:00+08:00")
        day = calendar.day("2026-09-25", types=["reminder"])
        assert day["items"] and day["items"][0]["kind"] == "reminder"
        out = maintenance.reminders_fire_due(now="2026-09-26T00:00:00+00:00")
        assert r["reminder_id"] in [x["id"] for x in out["fired"]]
        again = maintenance.reminders_fire_due(now="2026-09-27T00:00:00+00:00")
        assert not again["fired"]  # 幂等
        with pytest.raises(Forbidden):
            reminders.cancel("qiaosheng", r["reminder_id"])  # 已 fired 不可取消


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
        out = media.upload_finalize("qiaosheng", prep["upload_token"], png)
        assert out["deduplicated"] is False
        prep2 = media.upload_prepare("jiaming", "image/png", len(png))
        out2 = media.upload_finalize("jiaming", prep2["upload_token"], png)
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
            media.upload_finalize("qiaosheng", prep["upload_token"], b"short")


class TestMoments:
    def test_post_comment_react(self, actors):
        m = moments.post("jiaming", "今天一起做了晚饭。")
        assert m["kind"] == "post"
        moments.comment("qiaosheng", m["moment_id"], "番茄牛腩面好评")
        moments.react("qiaosheng", m["moment_id"], "❤")
        listed = moments.list_moments()
        assert listed[0]["content"].startswith("今天")
        # group_archive 与 post 分开
        assert all(x["kind"] == "post" for x in listed)


class TestBatchAndCooldown:
    def _submitted(self, actors, text):
        h = _hold(actors, text)
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     f"{text} 的摘要。", "压缩")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        return h, sub

    def test_batch_and_cooldown(self, actors):
        h1, s1 = self._submitted(actors, "批量测试一")
        h2, s2 = self._submitted(actors, "批量测试二")
        out = workspace.decide_batch(actors["qiaosheng"], [
            {"proposal_id": s1["proposal_id"], "proposal_revision": s1["revision"],
             "proposal_hash": s1["proposal_hash"],
             "expected_memory_version": s1["base_memory_version"],
             "decision": "approve"},
            {"proposal_id": s2["proposal_id"], "proposal_revision": s2["revision"],
             "proposal_hash": "0" * 64,  # 篡改 -> 单项失败
             "expected_memory_version": s2["base_memory_version"],
             "decision": "approve"},
        ])
        by_id = {r["proposal_id"]: r for r in out["results"]}
        assert by_id[s1["proposal_id"]]["ok"] is True
        assert by_id[s2["proposal_id"]]["ok"] is False
        assert by_id[s2["proposal_id"]]["error"] == "PROPOSAL_HASH_MISMATCH"

        # 篡改的 decide 未产生终局：用正确 hash 正式拒绝 -> 冷却生效
        workspace.decide(actors["jiaming"], proposal_id=s2["proposal_id"],
                         proposal_revision=s2["revision"],
                         proposal_hash=s2["proposal_hash"],
                         expected_memory_version=s2["base_memory_version"],
                         decision="reject")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        assert any(s["memory_id"] == h2["memory_id"] and
                   s["reason"] == "rejection_cooldown" for s in scan["skipped"])


class TestReservedContracts:
    def test_emotion_and_listening_reserved(self, actors):
        from mariposa.capabilities import registry
        emo = registry.invoke(actors["qiaosheng"], "emotion.context.get", {}, None)
        assert emo["data"]["status"] == "reserved"
        assert emo["data"]["enabled"] is False
        lis = registry.invoke(actors["qiaosheng"], "listening.status", {}, None)
        assert lis["data"]["status"] == "reserved"
        assert lis["data"]["provider"] is None
