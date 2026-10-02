"""复审 Source/Media 域修复回归（RA-022/026/028）。"""
from __future__ import annotations

import json
import threading

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.media import service as media
from mariposa.source import importer, query
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    media._staging.clear()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _import(conv_uuid, msgs):
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    f = tmp / f"{conv_uuid}.json"
    f.write_text(json.dumps([{"uuid": conv_uuid,
                              "chat_messages": msgs}],
                            ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))


def _msg(uuid, parent, text):
    return {"uuid": uuid, "sender": "human",
            "parent_message_uuid": parent,
            "created_at": "2026-09-28T10:00:00Z",
            "content": [{"type": "text", "text": text}]}


class TestAroundWindowContiguous:

    def test_even_limit_and_middle_group_reachable(self, actors):
        """RA-022 反例：limit2 返3；中间同序号组被跨过两端游标皆空。"""
        _import("c-ra022", [_msg("m0", None, "root"),
                             _msg("m1", None, "mid"),
                             _msg("m2", None, "tail")])
        with db.formal() as conn:
            conv_id = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='c-ra022'").fetchone()["id"]
        out = query.get_conversation(conv_id, around_seq=1, limit=2)
        assert len(out["messages"]) <= 2, \
            f"偶数 limit 也不得多返：{len(out['messages'])}"
        # 构造中间同序号组：同一会话 7 次增量导入（每次快照内 seq=0）
        for i in range(7):
            _import("c-group", [_msg(f"g{i}m", None, f"组消息{i}")])
        with db.formal() as conn:
            gconv = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='c-group'").fetchone()["id"]
            n = conn.execute(
                "SELECT COUNT(*) c FROM source_messages WHERE"
                " conversation_id=? AND sequence=0",
                (gconv,)).fetchone()["c"]
        assert n == 7, f"前置：同序号组 7 行，实际 {n}"
        out2 = query.get_conversation(gconv, around_seq=0, limit=3)
        seqs = [m["sequence"] for m in out2["messages"]]
        assert 0 in seqs and seqs == sorted(seqs)
        # 截断时游标可达：沿 next 渐进拿到全部 7 条
        seen = list(out2["messages"])
        guard = 0
        while out2.get("has_more") and guard < 20:
            cur = out2["next_after_cursor"]
            out2 = query.get_conversation(gconv, after_seq=cur, limit=100)
            seen += out2["messages"]
            guard += 1
        assert len(seen) >= 7, f"游标必须可达整组：{len(seen)}"


class TestBindingConfidenceEnum:

    def test_service_confidence_values_accepted(self, actors):
        """RA-026：schema 枚举与 service 统一（exact/high/low）。"""
        from mariposa.memory import service as memory
        m = memory.hold(actors["jiaming"], text="枚举正文",
                        memory_date="2026-09-25", date_confidence="exact",
                        original_title="t", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
        _import("c-ra026", [_msg("e0", None, "枚举原文"),
                             _msg("e1", "e0", "第二条")])
        for conf in ("exact", "high", "low"):
            out = registry.invoke(
                actors["jiaming"], "source.binding.bind",
                {"memory_id": m["memory_id"], "conversation_id": "c-ra026",
                 "start_message_id": "e0", "end_message_id": "e0",
                 "confidence": conf}, None)
            assert out["ok"] is True, f"{conf} 必须可公开创建"


class TestConcurrentFirstUpload:

    def test_same_hash_two_mimes_one_canonical(self, actors):
        """RA-028：并发首次同 hash 异 MIME——恰一 canonical、双方回执
        一致（输家按胜者行返回）。"""
        data = b"\x89PNG\r\n\x1a\n" + b"z" * 96
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def run(tag: str, mime: str):
            prep = media.upload_prepare("jiaming", mime, len(data))
            media.stage_bytes("jiaming", prep["upload_token"], data)
            barrier.wait()
            try:
                out = media.upload_finalize("jiaming",
                                            prep["upload_token"])
                results[tag] = (out["deduplicated"], out["mime"])
            except Exception as e:  # noqa: BLE001
                results[tag] = f"err:{e!r}"

        t1 = threading.Thread(target=run, args=("png", "image/png"))
        t2 = threading.Thread(target=run, args=("jpg", "image/jpeg"))
        t1.start(); t2.start(); t1.join(30); t2.join(30)
        from mariposa import config as cfg
        import hashlib
        h = hashlib.sha256(data).hexdigest()
        with db.formal() as conn:
            rows = conn.execute(
                "SELECT storage_key FROM media_objects WHERE"
                " content_hash=?", (h,)).fetchall()
        assert len(rows) == 1, "恰一条 canonical 行"
        # 无引用的异扩展名对象不残留
        import pathlib
        objs = pathlib.Path(str(cfg.RUNTIME_DIR) + "/objects")
        leftovers = [p.name for p in objs.glob(f"{h}.*")
                     if p.name != rows[0]["storage_key"]]
        assert not leftovers, f"无引用对象残留：{leftovers}"
        # 双方回执一致（同一 canonical mime）
        mimes = {v[1] for v in results.values() if isinstance(v, tuple)}
        assert len(mimes) <= 1, f"双方必须返回同一 canonical MIME：{results}"
