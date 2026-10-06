"""memory.hold 领域原子化测试（estómago 生命周期 WP2，迁移 31）。

契约依据：docs/specs/MARIPOSA_LIFECYCLE_v1.0 §5——operation_id+source_
selections 同事务回执；提交后崩溃可按 op 恢复零重复桶；绑定失败整批
回滚无半桶；旧手工 hold 兼容；重试不因资源已删而重造。夹具全合成。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.capabilities.transport import _recover_transport_from_domain
from mariposa.errors import Forbidden, IdempotencyConflict, NotFound
from mariposa.identity import Principal
from mariposa.source import ingest as live

STREAM = "stream_hold_1"
ROOM = "room-hold"
ARCHIVE_BINDING = "binding_hold_worker"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def seeded(actors):
    """归档凭据+stream 授权+已入库的三条消息（含一次修订）。"""
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
    worker = Principal("worker", "维护工具人", "agent", "estomago_archive",
                       ARCHIVE_BINDING,
                       capabilities_allowlist=frozenset(
                           ["source.ingest", "source.ingest.status"]))
    def _msg(omid, seq, text, rev=1, prev_rev=None, pred=None,
             sender="user"):
        return {"origin_message_id": omid, "revision": rev,
                "previous_revision": prev_rev,
                "conversation_sequence": seq, "predecessor": pred,
                "sender": sender, "published_kind": "chat_message",
                "occurred_at": "2026-10-05T01:00:00Z",
                "received_at": None, "published_at": None,
                "text": text, "assets": [], "content_hash": _text_hash(text)}
    ack1 = live.ingest(worker, {"operation_id": "ing-1",
                                "stream_id": STREAM,
                                "origin_instance": "estomago",
                                "origin_conversation_id": ROOM,
                                "messages": [
                                    _msg("m1", 1, "第一句", sender="user"),
                                    _msg("m2", 2, "第二句", sender="assistant",
                                         pred={"origin_message_id": "m1",
                                               "revision": 1}),
                                    _msg("m3", 3, "第三句", sender="user",
                                         pred={"origin_message_id": "m2",
                                               "revision": 1})]})

    def _members(*specs):
        return [{"source_message_id": ack1["messages"][i]["source_message_id"],
                 "content_hash": ack1["messages"][i]["content_hash"]}
                for i in specs]

    return {"actors": actors, "worker": worker, "ack1": ack1,
            "conv_id": ack1["messages"][0]["source_conversation_id"],
            "members": _members}


def _hold_args(seeded, op="hold-1", specs=(0, 1), **extra):
    sel = {"conversation_id": seeded["conv_id"],
           "members": seeded["members"](*specs)}
    args = {"text": "今晚的约定", "categories": ["daily"],
            "original_title": "今晚的约定",
            "memory_date": "2026-10-05", "date_confidence": "exact",
            # F-J-22（2026-10-06）：公开面显式声明当下/补记
            "creation_mode": "retrospective",
            "operation_id": op, "source_selections": [sel]}
    args.update(extra)
    return args


class TestAtomicHold:
    def test_receipt_and_manifest(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        r = registry.invoke(jiaming, "memory.hold", _hold_args(seeded),
                            None)["data"]
        assert r["operation_id"] == "hold-1" and r["version"] == 1
        assert len(r["binding_ids"]) == 1
        with db.formal() as conn:
            mem = conn.execute(
                "SELECT * FROM memory_source_binding_members WHERE"
                " binding_id=?", (r["binding_ids"][0],)).fetchall()
            assert [m["ordinal"] for m in mem] == [0, 1]
            assert mem[0]["start_char_offset"] is None
        # 回执可查：hold.status 分列历史成功与当前状态
        st = registry.invoke(jiaming, "memory.hold.status",
                             {"operation_id": "hold-1"}, None)["data"]
        assert st["completed"] is True
        assert st["receipt"]["memory_id"] == r["memory_id"]
        assert st["memory_current"] == {"exists": True,
                                        "visibility": "active", "version": 1}

    def test_replay_no_duplicate_bucket(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        r1 = registry.invoke(jiaming, "memory.hold", _hold_args(seeded),
                             None)["data"]
        r2 = registry.invoke(jiaming, "memory.hold", _hold_args(seeded),
                             None)["data"]
        assert r2["memory_id"] == r1["memory_id"]
        with db.formal() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()
            assert n["c"] == 1
            nb = conn.execute(
                "SELECT COUNT(*) c FROM memory_source_bindings").fetchone()
            assert nb["c"] == 1

    def test_same_op_different_payload_conflict(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        registry.invoke(jiaming, "memory.hold", _hold_args(seeded), None)
        with pytest.raises(IdempotencyConflict):
            registry.invoke(jiaming, "memory.hold",
                            _hold_args(seeded, text="改了正文"), None)

    def test_binding_failure_rolls_back_whole_hold(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        # 消息 id 不存在 → 绑定失败 → 整批回滚：无半桶、无绑定、无回执
        bad = _hold_args(seeded)
        bad["source_selections"][0]["members"][0]["source_message_id"] = \
            "sm_does_not_exist"
        with pytest.raises(NotFound):
            registry.invoke(jiaming, "memory.hold", bad, None)
        with db.formal() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM memories").fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) c FROM memory_source_bindings"
            ).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) c FROM idempotency_records WHERE"
                " capability='memory.hold'").fetchone()["c"] == 0

    def test_hash_mismatch_rejects_manifest(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        bad = _hold_args(seeded)
        bad["source_selections"][0]["members"][0]["content_hash"] = \
            "0" * 64
        with pytest.raises(Forbidden) as e:
            registry.invoke(jiaming, "memory.hold", bad, None)
        assert e.value.code == "SOURCE_HASH_MISMATCH"

    def test_selections_without_op_rejected(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        args = _hold_args(seeded)
        del args["operation_id"]
        with pytest.raises(Forbidden):
            registry.invoke(jiaming, "memory.hold", args, None)

    def test_legacy_hold_unchanged(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        r = registry.invoke(jiaming, "memory.hold", {
            "text": "手工记忆", "original_title": "手工记忆",
            "categories": ["sweet"],
            "creation_mode": "contemporaneous"}, None)["data"]
        assert "operation_id" not in r or r.get("operation_id") is None
        with db.formal() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM memory_source_binding_members"
            ).fetchone()["c"] == 0

    def test_deleted_memory_replay_does_not_recreate(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        # op-only hold（无绑定）——删除门不受新绑定关系阻挡
        args = {"text": "无来源记忆", "original_title": "无来源记忆",
                "categories": ["daily"],
                "creation_mode": "contemporaneous",  # F-J-22：显式声明
                "operation_id": "hold-nd"}
        r = registry.invoke(jiaming, "memory.hold", args, None)["data"]
        registry.invoke(jiaming, "memory.delete",
                        {"memory_id": r["memory_id"],
                         "operation_id": "del-1"}, None)
        r2 = registry.invoke(jiaming, "memory.hold", args, None)["data"]
        assert r2["memory_id"] == r["memory_id"]
        with db.formal() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE memory_id=?",
                (r["memory_id"],)).fetchone()["c"] == 0
        st = registry.invoke(jiaming, "memory.hold.status",
                             {"operation_id": "hold-nd"}, None)["data"]
        assert st["completed"] is True
        assert st["memory_current"]["exists"] is False


class TestCrashRecovery:
    def test_transport_recovers_from_domain_receipt(self, seeded):
        """t 层崩溃残留 → 领域回执恢复（F23 面，memory.hold 补齐）。"""
        jiaming = seeded["actors"]["jiaming"]
        args = _hold_args(seeded)
        r = registry.invoke(jiaming, "memory.hold", args, "tkey-1")["data"]
        # 模拟：外层 t: 记录残留 running（领域已 completed）
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_records SET status='running',"
                " result_ref=NULL WHERE principal_id=? AND"
                " capability='memory.hold' AND idempotency_key='t:tkey-1'",
                (jiaming.principal_id,))
            conn.execute("COMMIT")
        verdict, recovered = _recover_transport_from_domain(
            jiaming, registry.REGISTRY["memory.hold"], "t:tkey-1", args)
        assert verdict == "completed"
        assert recovered["memory_id"] == r["memory_id"]
        assert recovered["operation_id"] == "hold-1"

    def test_await_completion_recovers_stale(self, seeded, monkeypatch):
        """完整链路：同 key 重试命中 stale running → 领域回执放行重放。"""
        from mariposa.capabilities import transport
        jiaming = seeded["actors"]["jiaming"]
        args = _hold_args(seeded)
        registry.invoke(jiaming, "memory.hold", args, "tkey-2")
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_records SET status='running',"
                " result_ref=NULL, created_at='2020-01-01T00:00:00'"
                " WHERE principal_id=? AND capability='memory.hold' AND"
                " idempotency_key='t:tkey-2'", (jiaming.principal_id,))
            conn.execute("COMMIT")
        # _idempotent_stale 由 created_at 判定（2020 即 stale）
        r2 = registry.invoke(jiaming, "memory.hold", args, "tkey-2")["data"]
        assert r2["operation_id"] == "hold-1"
        with db.formal() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM memories").fetchone()["c"] == 1


class TestSelectionOpen:
    def test_readback_and_drift(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        sel = {"conversation_id": seeded["conv_id"],
               "members": seeded["members"](0, 1, 2)}
        out = registry.invoke(jiaming, "source.selection.open",
                              {"selection": sel, "include_content": True},
                              None)["data"]
        assert len(out["members"]) == 3
        assert out["drifted_members"] == []
        texts = [m["text"] for m in out["members"]]
        assert texts == ["第一句", "第二句", "第三句"]
        # m2 修订后：旧 manifest 读回 → 该成员漂移显式标注
        live.ingest(seeded["worker"], {
            "operation_id": "ing-edit", "stream_id": STREAM,
            "origin_instance": "estomago", "origin_conversation_id": ROOM,
            "messages": [
                {"origin_message_id": "m2", "revision": 2,
                 "previous_revision": 1, "conversation_sequence": 2,
                 "predecessor": None, "sender": "assistant",
                 "published_kind": "chat_message", "occurred_at": None,
                 "received_at": None, "published_at": None,
                 "text": "第二句（改）", "assets": [],
                 "content_hash": _text_hash("第二句（改）")}]})
        out2 = registry.invoke(jiaming, "source.selection.open",
                               {"selection": sel}, None)["data"]
        assert len(out2["drifted_members"]) == 1
        drifted = [m for m in out2["members"] if "version_drift" in m]
        assert len(drifted) == 1
        assert drifted[0]["version_drift"]["pinned"] == \
            seeded["members"](1)[0]["content_hash"]

    def test_status_unknown_op(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        st = registry.invoke(jiaming, "memory.hold.status",
                             {"operation_id": "never"}, None)["data"]
        assert st == {"operation_id": "never", "completed": False,
                      "receipt": None}


class TestSelfAudit1005bOffsets:
    """自审④⑤：读回与写入两路径同一偏移口径；members 路径偏移在
    首末成员上生效且越界拒绝。"""

    def test_open_selection_rejects_out_of_bounds(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        sel = {"conversation_id": seeded["conv_id"],
               "members": seeded["members"](0),
               "start_char_offset": 99999}
        with pytest.raises(Forbidden) as e:
            registry.invoke(jiaming, "source.selection.open",
                            {"selection": sel}, None)
        assert e.value.code == "SOURCE_RANGE_OFFSET"

    def test_open_selection_rejects_bool_offset(self, seeded):
        jiaming = seeded["actors"]["jiaming"]
        sel = {"conversation_id": seeded["conv_id"],
               "members": seeded["members"](0),
               "start_char_offset": True}
        # schema 层先拒（integer 类型）；直接走 service 验证口径
        from mariposa.source.binding import open_selection
        with pytest.raises(Forbidden) as e:
            open_selection(sel)
        assert e.value.code == "SOURCE_RANGE_OFFSET"

    def test_hold_members_offset_validated_in_tx(self, seeded):
        """⑤ members 路径的偏移经同一 validate_range（含越界拒绝）——
        重构后单一校验来源，不因复用丢口径。"""
        jiaming = seeded["actors"]["jiaming"]
        bad = _hold_args(seeded)
        bad["source_selections"][0]["start_char_offset"] = 99999
        with pytest.raises(Forbidden) as e:
            registry.invoke(jiaming, "memory.hold", bad, None)
        assert e.value.code == "SOURCE_RANGE_OFFSET"
        with db.formal() as conn:
            assert conn.execute(
                "SELECT COUNT(*) c FROM memories").fetchone()["c"] == 0
