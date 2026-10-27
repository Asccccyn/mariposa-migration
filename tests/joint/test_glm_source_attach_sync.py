"""F03（GLM_VOICE_UNIFICATION 02§4/§5.2）M 端验证：source.attachments.sync。

真实公开面：create_grant（管理入口）→ source.ingest（真实正文归档）→
attachments_sync（晚到观察关联补同步，幂等、不改正文/hash）。
授权同 ingest：stream grant 钉定；同 op 重放无新增副作用。
"""

from __future__ import annotations

import json
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # M 仓 conftest 已固定 MARIPOSA_ROOT/ALLOW_CREATE 并完成 migrate
    from mariposa import schema

    schema.migrate()
    schema.migrate_runtime()
    yield None


def test_attachments_sync_idempotent_and_honest(env):
    from mariposa import db as mdb
    from mariposa.capabilities import registry
    from mariposa.errors import MariposaError
    from mariposa.identity.service import Principal
    from mariposa.source import ingest as source_ingest

    # 管理入口：登记 stream grant（真实 create_grant）
    with mdb.formal() as conn:
        # 归档 worker 主体（source.* 能力集只放行 worker 主体，与生产装配一致）
        conn.execute(
            "INSERT OR IGNORE INTO principals(principal_id, display_name, kind)"
            " VALUES ('worker','test','agent')")
        conn.execute(
            "INSERT OR IGNORE INTO client_bindings(binding_id, token_hash,"
            " principal_id, entry_source, revoked, created_at)"
            " VALUES ('bind-test','test-hash','worker','test',0,datetime('now'))")
    grant = source_ingest.create_grant(
        binding_id="bind-test", stream_id="estomago-primary-live",
        origin_instance="estomago", origin_conversation_id="primary",
        allowed_senders=["user", "assistant"], owner_scope="primary",
        created_by="test")
    assert grant["stream_id"] == "estomago-primary-live"

    worker = Principal("worker", "Worker", "agent", "worker", "bind-test")
    # 真实正文归档（source.ingest）
    ingest_req = {
        "operation_id": "op-ing-1", "stream_id": "estomago-primary-live",
        "origin_instance": "estomago", "origin_conversation_id": "primary",
        "messages": [{
            "origin_message_id": "msg_f03", "revision": 1,
            "previous_revision": None, "conversation_sequence": 1,
            "predecessor": None, "sender": "user",
            "published_kind": "voice_message",
            "occurred_at": "2026-10-26T10:00:00Z",
            "received_at": "2026-10-26T10:00:01Z",
            "published_at": "2026-10-26T10:00:02Z",
            "text": "这段是她的语音。",
            "assets": ["est:ast_1"],
            "content_hash": __import__("hashlib").sha256("这段是她的语音。".encode()).hexdigest(),
        }],
    }
    ack = registry.invoke(worker, "source.ingest", ingest_req, None)["data"]
    msg_ack = ack["messages"][0]
    assert msg_ack["origin_message_id"] == "msg_f03"
    assert not msg_ack.get("deduplicated")

    # 晚到观察补同步（幂等键=宿主 obs 身份）
    sync_args = {
        "operation_id": "att-sync:obs_1", "stream_id": "estomago-primary-live",
        "origin_message_id": "msg_f03", "revision": 1, "kind": "observation",
        "payload": {"observation": "停顿：1.2 s", "source": "oido_quick_or_deep",
                     "quality_note": "声学观察，供参考非诊断"},
    }
    out1 = registry.invoke(worker, "source.attachments.sync", sync_args, None)["data"]
    assert out1["synced"] is True and out1["source_message_id"]
    # 同 op 重放：幂等（不新增行）
    out2 = registry.invoke(worker, "source.attachments.sync", sync_args, None)["data"]
    assert out2["source_message_id"] == out1["source_message_id"]
    # 正文不可变：attachments 列仍为 ingest 时的值（补同步不改正文行）
    with mdb.formal() as conn:
        n = conn.execute(
            "SELECT COUNT(*) n FROM source_message_attachments_meta"
            " WHERE operation_id='att-sync:obs_1'").fetchone()["n"]
        assert n == 1, "幂等：同 op 只一行"
        meta = conn.execute(
            "SELECT kind, payload_json FROM source_message_attachments_meta"
            " WHERE origin_message_id='msg_f03'").fetchone()
        assert meta["kind"] == "observation"
        assert json.loads(meta["payload_json"])["observation"] == "停顿：1.2 s"
        sm = conn.execute(
            "SELECT text, attachments FROM source_messages WHERE id=?",
            (out1["source_message_id"],)).fetchone()
        assert sm["text"] == "这段是她的语音。", "正文不动"
        assert json.loads(sm["attachments"]) == ["est:ast_1"], "ingest assets 不被补同步改写"
    # 未授权 stream：fail-closed
    bad = dict(sync_args, stream_id="estomago-other", operation_id="att-sync:bad")
    with pytest.raises(MariposaError):
        registry.invoke(worker, "source.attachments.sync", bad, None)
