"""第 5 轮：list/by_date/by_tag、meanings、重建索引、任务租约、
hold 去重、quotes/diary 补齐、stickers、两阶段导入、事件补齐。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.content import service as content
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.maintenance import service as maintenance
from mariposa.memory import listing, service as memory
from mariposa.raw import service as raw
from mariposa.retrieval import rebuild as rebuild_mod
from mariposa.retrieval import search as retrieval
from mariposa.workspace import service as workspace
from mariposa.workspace import tasks as ws_tasks
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent", "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent", "gpt_chat", "bw"),
    }


def _hold(actors, text, date="2026-06-01"):
    return memory.hold(actors["jiaming"], text=text, memory_date=date)


class TestListing:
    def test_list_by_date_by_tag(self, actors):
        _hold(actors, "六月一日的事")
        h2 = _hold(actors, "六月二日的事", date="2026-06-02")
        out = listing.list_memories()
        assert out["items"][0]["memory_id"] == h2["memory_id"]
        by_date = listing.by_date("2026-06-02")
        assert by_date["matched_by"] == "date" and len(by_date["items"]) == 1
        from mariposa.content import service as tags_mod
        tags_mod.tags_add("qiaosheng", h2["memory_id"],
                          [{"tag": "旅行", "whose": "qiaosheng"}])
        # 默认 namespace=emotion：tag namespace 不命中
        assert not listing.by_tag("tag", "旅行")["items"]
        by_tag2 = listing.by_tag("emotion", "旅行", whose="qiaosheng")
        assert by_tag2["items"] and by_tag2["matched_by"] == "tag"


class TestMeanings:
    def test_append_into_projection_and_replace_archive(self, actors):
        h = _hold(actors, "一起看了日出")
        listing.meanings_append("jiaming", h["memory_id"], "日出是她的侧脸形状")
        with db.formal() as conn:
            hits = retrieval.search(conn, "侧脸形状")["hits"]
        assert any(x["memory_id"] == h["memory_id"] for x in hits)  # 纳入投影
        out = listing.meanings_replace("jiaming", h["memory_id"], ["新的理解层"])
        assert out["archived_old"] == 1
        layers = listing.meanings_list(h["memory_id"])
        assert len(layers) == 1 and layers[0]["content"] == "新的理解层"
        with db.formal() as conn:
            archived = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_meanings WHERE memory_id=?"
                " AND layer_no>=1000", (h["memory_id"],)).fetchone()["c"]
        assert archived == 1  # 留底
        with db.formal() as conn:
            hits = retrieval.search(conn, "侧脸形状")["hits"]
        assert not hits  # 替换后旧层不再可搜

    def test_forgotten_meaning_not_searchable(self, actors):
        h = _hold(actors, "待遗忘的桶")
        listing.meanings_append("jiaming", h["memory_id"], "独特意义词雾隐青竹")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "压缩后的摘要。", "压缩")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        workspace.decide(actors["qiaosheng"], proposal_id=sub["proposal_id"],
                         proposal_revision=sub["revision"],
                         proposal_hash=sub["proposal_hash"],
                         expected_memory_version=sub["base_memory_version"],
                         decision="approve")
        with db.formal() as conn:
            assert not retrieval.search(conn, "雾隐青竹")["hits"]  # §10.5

    def test_only_jiaming(self, actors):
        h = _hold(actors, "x")
        with pytest.raises(Forbidden):
            listing.meanings_append("qiaosheng", h["memory_id"], "她不能写他的意义")


class TestRebuildIndex:
    def test_rebuild_does_not_revive_old_words(self, actors):
        """§8.5 标志性测试最后一条：管理员重建索引不能让旧词重新可搜。"""
        h = _hold(actors, "蓝瓷小钥匙重建测试")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "重建索引后的摘要。", "压缩")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        workspace.decide(actors["qiaosheng"], proposal_id=sub["proposal_id"],
                         proposal_revision=sub["revision"],
                         proposal_hash=sub["proposal_hash"],
                         expected_memory_version=sub["base_memory_version"],
                         decision="approve")
        rebuild_mod.rebuild_index("qiaosheng")  # 管理员全量重建
        with db.formal() as conn:
            assert not retrieval.search(conn, "蓝瓷小钥匙重建测试")["hits"]
            hits = retrieval.search(conn, "重建索引后的摘要")["hits"]
        assert any(x["memory_id"] == h["memory_id"] for x in hits)


class TestTaskLeases:
    def test_claim_release_and_conflict(self, actors):
        with db.workspace() as wconn:
            wconn.execute(
                "INSERT INTO work_items(item_id, item_type, target_memory_id,"
                " state, current_revision, created_by, created_at, updated_at)"
                " VALUES('item_x','forget_proposal','mem_x','draft',1,'worker',"
                " datetime('now'), datetime('now'))")
        assert len(ws_tasks.tasks_list()) == 1
        lease = ws_tasks.task_claim("worker", "item_x")
        with pytest.raises(Forbidden) as e:
            ws_tasks.task_claim("jiaming", "item_x")
        assert e.value.detail.get("code") == "LEASE_HELD"
        with pytest.raises(Forbidden):
            ws_tasks.task_release("jiaming", lease["lease_id"])  # 非认领人
        ws_tasks.task_release("worker", lease["lease_id"])
        again = ws_tasks.task_claim("jiaming", "item_x")  # 释放后可重领
        assert again["lease_id"] != lease["lease_id"]

    def test_inspect_scoped_material(self, actors):
        h = _hold(actors, "被检查的桶")
        out = ws_tasks.memory_inspect("worker", h["memory_id"])
        assert out["memory"]["memory_id"] == h["memory_id"]
        assert "open_proposals" in out and "versions" in out


class TestHoldDedupe:
    def _payload(self):
        base = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        return {
            "source_channel": "dedupe_test", "external_id": "d1",
            "messages": [
                {"source_message_id": f"dm{i}", "role": "user",
                 "body": f"去重测试 {i}",
                 "occurred_at": (base + timedelta(minutes=i)).isoformat(),
                 "sequence": i} for i in range(3)]}

    def test_same_source_returns_existing(self, actors):
        raw.import_payload("worker", self._payload())
        conv = raw.conversations_list()[0]["id"]
        ref = [{"conversation_id": conv, "message_from": "dm0", "message_to": "dm2"}]
        first = memory.hold(actors["jiaming"], text="第一次 Hold",
                            memory_date="2026-06-01", raw_refs=ref)
        assert first.get("deduplicated") is None
        assert first["memory_id"]
        with db.formal() as conn:
            state = conn.execute("SELECT source_state FROM memories WHERE"
                                 " memory_id=?", (first["memory_id"],)).fetchone()
        assert state["source_state"] == "bound"  # 带 refs 直接绑定
        second = memory.hold(actors["jiaming"], text="重复 Hold",
                             memory_date="2026-06-01", raw_refs=ref)
        assert second["deduplicated"] is True
        assert second["memory_id"] == first["memory_id"]  # 返回已有记录


class TestQuotesDiaryExtras:
    def test_quote_get_and_by_memory(self, actors):
        from mariposa.quotes import service as quotes
        h = _hold(actors, "引用来源桶")
        q = quotes.keep("jiaming", "她说：今晚吃火锅。",
                        raw_ref=f"memory:{h['memory_id']}")
        got = quotes.get_quote(q["quote_id"])
        assert "火锅" in got["text"]
        related = quotes.by_memory(h["memory_id"])
        assert any(x["id"] == q["quote_id"] for x in related)

    def test_diary_read_revise(self, actors):
        d = content.diary_write("qiaosheng", "初稿", "正文第一版",
                                covers_from="2026-09-01", covers_to="2026-09-01")
        got = content.diary_read(d["diary_id"])
        assert got["content"] == "正文第一版"
        u = content.diary_revise("qiaosheng", d["diary_id"], 1,
                                 content="正文第二版逐字保留")
        assert u["version"] == 2
        with pytest.raises(Forbidden):
            content.diary_revise("jiaming", d["diary_id"], 2, content="别人改")
        with pytest.raises(Forbidden):
            content.diary_revise("qiaosheng", d["diary_id"], 1, content="旧版本")


class TestStickersAndTwoPhaseImport:
    def test_sticker_crud_and_search(self, actors):
        from mariposa.media import service as media
        png = b"\x89PNG\r\n\x1a\n" + b"S" * 40
        prep = media.upload_prepare("qiaosheng", "image/png", len(png))
        out = media.upload_finalize("qiaosheng", prep["upload_token"], png)
        from mariposa.capabilities import registry
        registry.invoke(actors["qiaosheng"], "sticker.add",
                        {"label": "开心猫", "content_hash": out["content_hash"]},
                        None)
        found = registry.invoke(actors["qiaosheng"], "sticker.search",
                                {"query": "开心"}, None)
        assert found["data"]["stickers"][0]["label"] == "开心猫"
        # 未上传媒体的 hash 拒绝
        with pytest.raises(Forbidden):
            registry.invoke(actors["qiaosheng"], "sticker.add",
                            {"label": "幽灵", "content_hash": "0" * 64}, None)

    def test_two_phase_import_and_raw_read(self, actors):
        prep = raw.import_prepare("worker", "tp_test", "tp1", 2)
        assert prep["status"] == "prepared"
        base = datetime(2026, 6, 2, 8, 0, tzinfo=timezone.utc)
        payload = {
            "source_channel": "tp_test", "external_id": "tp1",
            "messages": [
                {"source_message_id": "tpm0", "role": "user", "body": "两阶段消息",
                 "occurred_at": base.isoformat(), "sequence": 0},
                {"source_message_id": "tpm1", "role": "assistant", "body": "回复",
                 "occurred_at": (base + timedelta(minutes=1)).isoformat(),
                 "sequence": 1}]}
        out = raw.import_complete(prep["job_id"], payload, "worker")
        assert out["inserted"] == 2
        status = raw.import_status(prep["job_id"])
        assert status["status"] == "completed"
        msg = raw.read_message(out["conversation_id"] and
                               raw.list_recent(5)[0]["id"])
        assert msg["source"] == "raw"


class TestEventsBackfill:
    def test_emotion_changed_and_reminder_due_events(self, actors):
        from mariposa.content import service as tags_mod
        from mariposa.reminders import service as reminders
        h = _hold(actors, "事件测试桶")
        tags_mod.tags_add("jiaming", h["memory_id"],
                          [{"tag": "安心", "whose": "jiaming"}])
        reminders.create("qiaosheng", "事件提醒",
                         remind_at="2026-01-01T00:00:00+00:00")  # 已过期
        maintenance.reminders_fire_due()
        with db.formal() as conn:
            types = {r["event_type"] for r in conn.execute(
                "SELECT DISTINCT event_type FROM audit_events")}
        assert "memory.emotion.changed" in types
        assert "reminder.due" in types

    def test_proposal_resolved_event(self, actors):
        h = _hold(actors, "决议事件桶")
        scan = workspace.scan_candidates(actors["worker"], min_idle_days=0)
        prop = next(p for p in scan["created"]
                    if p["target_memory_id"] == h["memory_id"])
        rev = workspace.revise_draft(actors["worker"], prop["proposal_id"],
                                     "决议摘要。", "x")
        sub = workspace.submit(actors["worker"], prop["proposal_id"], rev["revision"])
        workspace.decide(actors["jiaming"], proposal_id=sub["proposal_id"],
                         proposal_revision=sub["revision"],
                         proposal_hash=sub["proposal_hash"],
                         expected_memory_version=sub["base_memory_version"],
                         decision="approve")
        with db.formal() as conn:
            resolved = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE event_type="
                "'workspace.proposal.resolved'").fetchone()["c"]
        assert resolved >= 1
