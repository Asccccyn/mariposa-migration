"""在线原文 live ingest 单元测试（estómago 生命周期 WP1，迁移 30）。

契约依据：docs/specs/MARIPOSA_LIFECYCLE_v1.0 §4（联合装订）——重放/
乱序/编辑修订/ACL/原子性/母本先落盘/受限 binding 白名单。全部合成
数据，无真实对话。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, IdempotencyConflict
from mariposa.identity import Principal
from mariposa.source import ingest as live

STREAM = "stream_pri_1"
ROOM = "room-abc"
ARCHIVE_BINDING = "binding_archive_worker"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def env(actors):
    """受限归档凭据 + stream 授权（服务端钉 origin）。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO client_bindings(binding_id, token_hash,"
                " principal_id, entry_source, capabilities_allowlist,"
                " created_at) VALUES(?,?,?,?,?,datetime('now'))",
                (ARCHIVE_BINDING, "x" * 64, "worker", "estomago_archive",
                 json.dumps(["source.ingest", "source.ingest.status"])))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    live.create_grant(ARCHIVE_BINDING, STREAM, "estomago", ROOM,
                      ["user", "assistant"], "private", "qiaosheng")
    return Principal("worker", "维护工具人", "agent", "estomago_archive",
                     ARCHIVE_BINDING,
                     capabilities_allowlist=frozenset(
                         ["source.ingest", "source.ingest.status"]))


def _msg(omid: str, seq: int, text: str, rev: int = 1,
         prev_rev=None, pred=None, sender="user",
         occurred="2026-10-05T01:00:00Z"):
    return {"origin_message_id": omid, "revision": rev,
            "previous_revision": prev_rev, "conversation_sequence": seq,
            "predecessor": pred, "sender": sender,
            "published_kind": "chat_message", "occurred_at": occurred,
            "received_at": occurred, "published_at": occurred,
            "text": text, "assets": [], "content_hash": _text_hash(text)}


def _req(op: str, messages: list[dict], **over) -> dict:
    r = {"operation_id": op, "stream_id": STREAM, "origin_instance":
         "estomago", "origin_conversation_id": ROOM, "messages": messages}
    r.update(over)
    return r


class TestHappyPath:
    def test_first_batch(self, env):
        ack = live.ingest(env, _req("op-1", [
            _msg("m1", 1, "今天想吃火锅", sender="user"),
            _msg("m2", 2, "好，晚上七点老地方", sender="assistant",
                 pred={"origin_message_id": "m1", "revision": 1}),
        ]))
        assert ack["integrity"] == "verified"
        assert ack["batch_ref"].startswith("live-")
        assert len(ack["messages"]) == 2
        m1, m2 = ack["messages"]
        assert m1["origin_message_id"] == "m1" and m1["revision"] == 1
        assert m1["source_conversation_id"] == m2["source_conversation_id"]
        with db.formal() as conn:
            rows = conn.execute(
                "SELECT provider_message_id, normalized_sender, speaker,"
                " published, live_superseded, text, sequence FROM"
                " source_messages WHERE id IN (?,?)",
                (m1["source_message_id"], m2["source_message_id"])).fetchall()
            assert len(rows) == 2
            by = {r["text"]: r for r in rows}
            assert by["今天想吃火锅"]["speaker"] == "qiaosheng"
            assert by["好，晚上七点老地方"]["speaker"] == "jiaming"
            assert all(r["published"] == 1 and r["live_superseded"] == 0
                       for r in rows)
            lr = conn.execute(
                "SELECT * FROM source_live_revisions WHERE stream_id=?",
                (STREAM,)).fetchall()
            assert len(lr) == 2
            assert lr[1]["predecessor_message_id"] == "m1"
            batch = conn.execute(
                "SELECT kind, status FROM source_import_batches WHERE"
                " batch_id=?", (ack["batch_ref"],)).fetchone()
            assert batch["kind"] == "live_delta" and \
                batch["status"] == "completed"
            conv = conn.execute(
                "SELECT message_count FROM source_conversations WHERE id=?",
                (m1["source_conversation_id"],)).fetchone()
            assert conv["message_count"] == 2
        # 母本已落盘且批次 raw_path 指向它（ACK 前提）
        from pathlib import Path
        with db.formal() as conn:
            raw = conn.execute(
                "SELECT raw_path FROM source_import_batches WHERE"
                " batch_id=?", (ack["batch_ref"],)).fetchone()["raw_path"]
        assert Path(raw).is_file()

    def test_search_projection_current_only(self, env):
        live.ingest(env, _req("op-1", [_msg("m1", 1, "独特关键词甲")]))
        live.ingest(env, _req("op-2", [_msg(
            "m1", 1, "修订后的独特关键词乙", rev=2, prev_rev=1)]))
        from mariposa.source import query
        hits = query.search("独特关键词", senders=["human", "assistant"])
        texts = [h["excerpt"] for h in hits["hits"]
                 if "独特关键词" in (h.get("excerpt") or "")]
        assert any("乙" in t for t in texts)
        assert not any("甲" in t for t in texts), "旧修订不得留在检索投影"


class TestIdempotency:
    def test_same_op_same_payload_replays(self, env):
        r1 = live.ingest(env, _req("op-1", [_msg("m1", 1, "hello")]))
        r2 = live.ingest(env, _req("op-1", [_msg("m1", 1, "hello")]))
        assert r2["messages"] == r1["messages"]
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM source_live_revisions").fetchone()
            assert n["c"] == 1

    def test_same_op_different_payload_conflicts(self, env):
        live.ingest(env, _req("op-1", [_msg("m1", 1, "hello")]))
        with pytest.raises(IdempotencyConflict):
            live.ingest(env, _req("op-1", [_msg("m1", 1, "different")]))

    def test_status_roundtrip(self, env):
        ack = live.ingest(env, _req("op-1", [_msg("m1", 1, "hello")]))
        st = live.ingest_status(env, {"operation_id": "op-1"})
        assert st["completed"] is True
        assert st["receipt"]["messages"] == ack["messages"]
        st2 = live.ingest_status(env, {"operation_id": "op-none"})
        assert st2["completed"] is False and st2["receipt"] is None


class TestChains:
    def test_revision_gap_not_ready(self, env):
        with pytest.raises(Forbidden) as e:
            live.ingest(env, _req("op-1", [
                _msg("m1", 1, "first"),
                _msg("m2", 2, "second", rev=2, prev_rev=1,
                     pred={"origin_message_id": "m1", "revision": 1})]))
        assert e.value.code == "SOURCE_NOT_READY"

    def test_missing_predecessor_not_ready(self, env):
        with pytest.raises(Forbidden) as e:
            live.ingest(env, _req("op-1", [
                _msg("m2", 2, "second",
                     pred={"origin_message_id": "ghost", "revision": 1})]))
        assert e.value.code == "SOURCE_NOT_READY"

    def test_same_revision_different_content_conflict(self, env):
        live.ingest(env, _req("op-1", [_msg("m1", 1, "first")]))
        with pytest.raises(Forbidden) as e:
            live.ingest(env, _req("op-2", [_msg("m1", 1, "changed")]))
        assert e.value.code == "SOURCE_REVISION_CONFLICT"

    def test_stale_previous_revision_conflict(self, env):
        live.ingest(env, _req("op-1", [_msg("m1", 1, "v1")]))
        live.ingest(env, _req("op-2", [
            _msg("m1", 2, "v2", rev=2, prev_rev=1)]))
        # 第三次编辑声称基于 r1（陈旧基准）→ 拒绝，不静默覆盖
        with pytest.raises(Forbidden) as e:
            live.ingest(env, _req("op-3", [
                _msg("m1", 3, "v3", rev=3, prev_rev=1)]))
        assert e.value.code == "SOURCE_REVISION_CONFLICT"

    def test_edit_supersedes_old_row(self, env):
        a1 = live.ingest(env, _req("op-1", [_msg("m1", 1, "v1 text")]))
        live.ingest(env, _req("op-2", [
            _msg("m1", 1, "v2 text", rev=2, prev_rev=1)]))
        with db.formal() as conn:
            old = conn.execute(
                "SELECT live_superseded FROM source_messages WHERE id=?",
                (a1["messages"][0]["source_message_id"],)).fetchone()
            assert old["live_superseded"] == 1

    def test_hash_mismatch_rejected(self, env):
        bad = _msg("m1", 1, "actual text")
        bad["content_hash"] = _text_hash("claimed text")
        with pytest.raises(Forbidden) as e:
            live.ingest(env, _req("op-1", [bad]))
        assert e.value.code == "SOURCE_HASH_MISMATCH"


class TestAcl:
    def test_unknown_stream(self, env):
        with pytest.raises(Forbidden):
            live.ingest(env, _req("op-1", [_msg("m1", 1, "x")],
                                  stream_id="stream-other"))

    def test_origin_mismatch(self, env):
        with pytest.raises(Forbidden) as e:
            live.ingest(env, _req("op-1", [_msg("m1", 1, "x")],
                                  origin_conversation_id="room-other"))
        assert e.value.code == "FORBIDDEN"

    def test_revoked_grant_blocks(self, env):
        live.revoke_grant(STREAM, "qiaosheng")
        with pytest.raises(Forbidden):
            live.ingest(env, _req("op-1", [_msg("m1", 1, "x")]))

    def test_binding_allowlist_enforced_in_invoke(self, env):
        # 受限凭据（白名单外能力）经 registry.invoke 一律拒——不能只
        # 藏 tools/list 而 HTTP 仍可调（方案 §8）
        with pytest.raises(Forbidden) as e:
            registry.invoke(env, "memory.hold",
                            {"text": "t", "categories": ["daily"]}, None)
        assert e.value.code == "FORBIDDEN"
        tools = registry.list_capabilities(env)
        names = {t["name"] for t in tools}
        assert names == {"source.ingest", "source.ingest.status"}

    def test_owner_principal_cannot_ingest(self, actors):
        # capability 主体门：source.ingest 只属 worker
        with pytest.raises(Forbidden):
            registry.invoke(actors["qiaosheng"], "source.ingest",
                            _req("op-1", [_msg("m1", 1, "x")]), None)

    def test_sender_not_in_grant(self, env):
        live.revoke_grant(STREAM, "qiaosheng")
        live.create_grant(ARCHIVE_BINDING, "s2", "estomago", "room2",
                          ["user"], "private", "qiaosheng")
        with pytest.raises(Forbidden) as e:
            live.ingest(env, _req("op-9", [_msg("m1", 1, "x",
                                                sender="assistant")],
                                  stream_id="s2",
                                  origin_conversation_id="room2"))
        assert e.value.code == "FORBIDDEN"


class TestAtomicity:
    def test_failure_leaves_no_partial_rows(self, env):
        live.ingest(env, _req("op-1", [_msg("m1", 1, "first")]))
        # 第二条前版缺失 → 整批拒绝，m_new 不留半行
        with pytest.raises(Forbidden):
            live.ingest(env, _req("op-2", [
                _msg("m9", 9, "tail", rev=2, prev_rev=1)]))
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM source_messages WHERE"
                " provider='estomago'").fetchone()
            assert n["c"] == 1
            ops = conn.execute(
                "SELECT COUNT(*) c FROM idempotency_records WHERE"
                " capability='source.ingest'").fetchone()
            assert ops["c"] == 1  # 只有 op-1 的完成记录

    def test_over_limit_batch_rejected(self, env):
        msgs = [_msg(f"m{i}", i, f"t{i}") for i in range(51)]
        with pytest.raises(Forbidden):
            live.ingest(env, _req("op-1", msgs))
