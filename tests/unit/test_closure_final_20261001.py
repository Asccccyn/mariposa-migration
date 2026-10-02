"""最终收口 batch（2026-10-01，复审对 0316e37）验收测试。

四判据：并发首轮恰一赢家 / title-only 中段事实进 Jev / chunked
超限在读取中截停 / Media 损坏 object 自愈。
"""
from __future__ import annotations

import threading

import pytest

from mariposa import config as cfg, db
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from mariposa.retrieval.judges import base as jb
from mariposa.retrieval.judges import typesafe_jev
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _grant():
    class G(typesafe_jev.TypeSafeJevJudge):
        name = "g_final"
        _api_key = "k"

        def __init__(self):
            super().__init__()
            self._data_profile = frozenset(
                {"event_excerpt", "title_cue", "word_excerpt",
                 "source_excerpt"})
            self._disabled_reason = None

        def judge(self, plan, cs, ctx):
            items = [jb.JudgeItem(
                c.get("candidate_ref") or c["resource_ref"],
                c.get("content_version"), relevance_signal=0.8,
                evaluation_status="evaluated", model_id="g",
                prompt_version="t") for c in cs]
            return jb.JudgeBatchResult(items=items,
                                       provider_status="evaluated")

    jb.register_for_tests("g_final", G())
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = "g_final"
    return old


def _legit(actors):
    memory.hold(
        actors["jiaming"], text="崧蓝事件正文", memory_date="2026-09-25",
        date_confidence="exact", original_title="f", categories=["daily"],
        creation_mode="contemporaneous", raw_pending=False,
        our_words=[{"speaker": "qiaosheng", "text": "复述：崧蓝的傍晚",
                    "expression_kind": "paraphrase"}])
    p = recall_service.start(actors["jiaming"], {
        "query_plan": {"original_request": "我当时的原话",
                       "channels": ["words"],
                       "lexical_terms": ["崧蓝"],
                       "evidence_requirement": "verbatim_required"}})
    from mariposa.source import importer
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    convs = [{"uuid": f"c-f{i}", "chat_messages": [{
        "uuid": f"m-f{i}", "sender": "human",
        "created_at": "2026-09-28T10:00:00.000Z",
        "content": [{"type": "text", "text": f"崧蓝备注第{i}条"}]}]}
        for i in range(25)]
    f = tmp / "s.json"
    f.write_text(_json.dumps(convs, ensure_ascii=False), encoding="utf-8")
    importer.import_file("jiaming", str(f))
    return p["recall_session_id"]


class TestRound2ConcurrentSingleWinner:
    def test_two_threads_one_raw_round(self, actors):
        """复审反例：两个不同 operation_id 并发首轮 → 两个都成功、
        2 条 raw round。三层防护后：恰一成功一拒绝、raw_round=1。"""
        old = _grant()
        try:
            sid = _legit(actors)
            out: list[str] = []

            def call(tag):
                try:
                    recall_service.round2(actors["jiaming"], {
                        "session_id": sid,
                        "reason": "EVIDENCE_INSUFFICIENT",
                        "operation_id": f"op-race-{tag}"})
                    out.append("ok")
                except Forbidden as e:
                    out.append(e.code)

            t1 = threading.Thread(target=call, args=("a",))
            t2 = threading.Thread(target=call, args=("b",))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert out.count("ok") == 1, out
            # CB-011 后输家更早被租约拒绝（RAW_ROUND_IN_PROGRESS，
            # 零昂贵调用）；TTL 抢占窗口下才会到事务复查的
            # ROUND2_GATE_DENIED——两者都是结构化拒绝
            assert out.count("ROUND2_GATE_DENIED") + \
                out.count("RAW_ROUND_IN_PROGRESS") == 1, out
            with db.recall_runtime() as conn:
                raw_n = conn.execute(
                    "SELECT COUNT(*) c FROM recall_rounds WHERE"
                    " session_id=? AND kind='raw'", (sid,)).fetchone()["c"]
            assert raw_n == 1, f"并发下 raw 轮必须唯一：{raw_n}"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_unique_index_holds(self, actors):
        """DB 层不变量：直接绕过服务层双插 raw 轮 → 唯一索引拒绝。"""
        from mariposa.recall import store as rstore
        draft = rstore.new_session_draft("jiaming", "", {})
        with db.recall_runtime() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rstore.insert_session(conn, draft)
                rstore.record_round(conn, draft["session_id"], burst_no=1,
                                    kind="raw")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        import sqlite3
        with db.recall_runtime() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO recall_rounds(session_id, round_no,"
                    " burst_no, created_at, kind) VALUES(?,?,1,?, 'raw')",
                    (draft["session_id"], 2, "2026-10-01T00:00:00+00:00"))


class TestTitleOnlyMidBodyFact:
    def test_mid_body_fact_reaches_jev(self, actors):
        """复审反例：标题唯一命中，事实在 1700 字正文**中段**——
        头+尾双窗不覆盖，分段覆盖窗必须让 Jev 看到。"""
        cap = _CapturingJudge()
        jb.register_for_tests(cap.name, cap)
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            head = "平" * 800
            mid = "那天我们约会在河边的灯下走了很久"
            tail = "静" * 800
            body = head + mid + tail
            assert "中秋" not in body and len(body) > 1600
            memory.hold(
                actors["jiaming"], text=body, memory_date="2026-09-25",
                date_confidence="exact", original_title="中秋灯会",
                categories=["date"], creation_mode="contemporaneous",
                raw_pending=False)
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "中秋",
                               "channels": ["event"],
                               "lexical_terms": ["中秋"]}})
            assert p["candidates"], "title 命中应交付"
            segs = (cap.payloads[-1]["state"]["candidates"][0]
                    ["segments"])
            assert any(s["field"] == "original_title" for s in segs)
            joined = "".join(s["text"] for s in segs
                             if s["field"] == "event_text"
                             ).replace(" ", "")
            assert "约会" in joined, \
                "中段事实必须进入 Jev（分段覆盖，不赌头/尾）"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class _CapturingJudge(jb.JudgeProvider):
    name = "cap_final"

    def __init__(self):
        self.payloads = []

    def judge(self, plan, candidates, ctx):
        inner = typesafe_jev.TypeSafeJevJudge()
        inner._data_profile = frozenset(
            {"event_excerpt", "title_cue", "word_excerpt",
             "source_excerpt", "structured_metadata"})
        self.payloads.append(inner._payload(plan, candidates))
        return jb.JudgeBatchResult(
            items=[jb.JudgeItem(
                c.get("candidate_ref") or c["resource_ref"],
                c.get("content_version"), relevance_signal=0.8,
                evaluation_status="evaluated", model_id="c",
                prompt_version="t") for c in candidates],
            provider_status="evaluated")


class TestChunkedBodyCap:
    def test_chunked_unauth_401_before_body(self, actors):
        """未鉴权 + chunked 3MB：401（鉴权先于任何读体）。"""
        from fastapi.testclient import TestClient
        from mariposa.app import app

        def gen():
            yield b"x" * (3 * 1024 * 1024)

        with TestClient(app) as c:
            r = c.post("/api/capability/memory.hold",
                       content=gen(),
                       headers={"Authorization": "Bearer bad"})
        assert r.status_code == 401

    def test_chunked_authed_413_body_too_large(self, actors):
        """已鉴权 + chunked 3MB（无 Content-Length）：413
        BODY_TOO_LARGE，流式截停。"""
        from fastapi.testclient import TestClient
        from mariposa.app import app

        def gen():
            for _ in range(3):
                yield b"x" * (1024 * 1024)

        with TestClient(app) as c:
            r = c.post("/api/capability/memory.hold",
                       content=gen(),
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}"})
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "BODY_TOO_LARGE"

    def test_read_capped_stops_early(self):
        """单元级：流式读取超限即断——不消费后续 chunk。"""
        import asyncio
        from mariposa.app import _read_body_capped

        class FakeStream:
            def __init__(self, chunks):
                self._it = iter(chunks)
                self.consumed = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    c = next(self._it)
                except StopIteration:
                    raise StopAsyncIteration
                self.consumed += 1
                return c

        chunks = [b"a" * 1024] * 100  # 100KB 总量，上限 4KB
        fs = FakeStream(chunks)
        req = type("R", (), {"stream": lambda self: fs})()

        async def run():
            from mariposa.errors import MariposaError
            try:
                await _read_body_capped(req, 4096)
                return None
            except MariposaError as e:
                return e.code

        code = asyncio.run(run())
        assert code == "BODY_TOO_LARGE"
        assert fs.consumed <= 6, \
            f"超限后必须立即停止消费（consumed={fs.consumed}）"


class TestMediaSelfHeal:
    def test_dedup_repair_corrupt_object(self, actors):
        """复审反例：正常上传后把 object 改成 3 字节，再传同内容
        → deduplicated=True 但坏文件必须被修复。"""
        import hashlib
        from mariposa.media import service as media
        data = b"\x89PNG\r\n\x1a\n" + b"h" * 200
        prep1 = media.upload_prepare("jiaming", "image/png", len(data))
        media.stage_bytes("jiaming", prep1["upload_token"], data)
        out1 = media.upload_finalize("jiaming", prep1["upload_token"])
        h = out1["content_hash"]
        obj = media.config.RUNTIME_DIR / "objects" / f"{h}.png"
        assert obj.read_bytes() == data
        obj.write_bytes(b"bad")  # 损坏
        prep2 = media.upload_prepare("jiaming", "image/png", len(data))
        media.stage_bytes("jiaming", prep2["upload_token"], data)
        out2 = media.upload_finalize("jiaming", prep2["upload_token"])
        assert out2["deduplicated"] is True
        assert obj.read_bytes() == data, "损坏 object 必须自愈（原子重写）"
