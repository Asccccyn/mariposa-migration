"""a3db0f9 复审 9 项修复回归（2026-10-04 第二轮复审）。

逐条对应：CR-01-R2（标题命中不替代事件主体必要角色）、ASRC-01
（代码围栏结束元数据状态）、ASRC-07（反查偏移校验+空区间）、
GATE-01（三档解耦）、GATE-02（门禁错误序列化）、GATE-03（回收
与容量）、GATE-04（零配置防御）、GATE-05（到期审计）、GATE-06
（审计隔离事务）。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    from mariposa.identity import service as identity
    return {"jiaming": identity.Principal(
        "jiaming", "周家明", "agent", "claude_chat", "bj")}


def _client():
    from fastapi.testclient import TestClient
    from mariposa.app import app
    return TestClient(app)


AUTH_J = {"Authorization": f"Bearer {TOKENS['jiaming']}"}
AUTH_BAD = {"Authorization": "Bearer wrong-token"}


def _parse_md(text: str, name: str = "t.md"):
    from mariposa.source import md_transcript
    p = pathlib.Path(tempfile.mkdtemp()) / name
    p.write_text(text, encoding="utf-8")
    return md_transcript.parse(p, name)


class TestCR01R2TitleOnlyRole:

    @staticmethod
    def _register(name, profile):
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev

        class G(typesafe_jev.TypeSafeJevJudge):
            def __init__(self):
                super().__init__()
                self._api_key = "k"  # 实例级：__init__ 会用空 env 覆盖
                self._data_profile = frozenset(profile)
                self._disabled_reason = None

        jb.register_for_tests(name, G())

    @staticmethod
    def _fake_http(monkeypatch):
        from mariposa.retrieval.judges import typesafe_jev

        class _Resp:
            def __init__(self, body):
                self._body = body.encode("utf-8")

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            typesafe_jev.urllib.request, "urlopen",
            lambda req, timeout=None: _Resp(json.dumps(
                {"model": "fake-jev",
                 "answers": {"candidate_0": {"noul": 0.9}}})))

    def test_title_only_hit_matrix(self, actors, monkeypatch):
        """标题独占命中的普通事件：必要主体角色恒为 event_excerpt
        ——title_cue-only 时重放不得出正文；event_excerpt-only 时
        重放不得误杀（fresh=1 则 replay=1）。"""
        from mariposa import config as cfg
        from mariposa.capabilities import registry
        from mariposa.identity import service as identity
        from mariposa.memory import service as memory
        self._fake_http(monkeypatch)
        self._register("r2_full", {"event_excerpt", "title_cue",
                                   "word_excerpt", "source_excerpt",
                                   "structured_metadata"})
        self._register("r2_title", {"title_cue", "structured_metadata"})
        self._register("r2_event", {"event_excerpt",
                                    "structured_metadata"})
        old = cfg.RECALL_JUDGE_PROVIDER
        plan = {"query_plan": {
            "original_request": "中秋",
            "channels": ["event"],
            "lexical_terms": ["中秋"]}}
        try:
            # 锚词只在标题——检索层 title-only 命中
            memory.hold(actors["jiaming"], text="当晚在山里修电路",
                        memory_date="2026-09-20", date_confidence="exact",
                        original_title="中秋约会", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
            cfg.RECALL_JUDGE_PROVIDER = "r2_full"
            r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                                 {**plan, "operation_id": "r2-op"},
                                 None)["data"]["data"]
            assert len(r1["candidates"]) == 1

            cfg.RECALL_JUDGE_PROVIDER = "r2_title"
            fresh_t = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-f1"}, None)["data"]["data"]
            replay_t = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-op"}, None)["data"]["data"]
            assert len(fresh_t["candidates"]) == 0
            assert len(replay_t["candidates"]) == 0, \
                "title_cue-only：标题命中不得让旧 operation 重放事件正文"

            cfg.RECALL_JUDGE_PROVIDER = "r2_event"
            fresh_e = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-f2"}, None)["data"]["data"]
            replay_e = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-op"}, None)["data"]["data"]
            assert len(fresh_e["candidates"]) == 1
            assert len(replay_e["candidates"]) == 1, \
                "event_excerpt-only：事件许可下标题命中重放不得被误杀"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestASRC01FenceEndsMetadata:

    def test_date_after_code_block_stays_in_body(self):
        """无初始时间戳的消息以代码块开头：块后的加粗 ISO 日期是
        正文，不剥离、不当 created_at。"""
        md = (
            "## Claude\n"
            "```python\n"
            "print('先来一段代码')\n"
            "```\n"
            "**2026-10-02T00:00:00Z**\n"
            "说明文字。\n")
        provider, elements = _parse_md(md)
        msgs = elements[0]["chat_messages"]
        assert len(msgs) == 1
        assert msgs[0]["created_at"] is None, "正文日期不得误当元数据"
        text = next(b["text"] for b in msgs[0]["content"]
                    if b["type"] == "text")
        assert "**2026-10-02T00:00:00Z**" in text
        assert "说明文字" in text


class TestASRC07OffsetsAndEmptyIntervals:

    def _setup(self, actors):
        from mariposa.memory import service as memory
        from mariposa.source import binding, importer
        src = pathlib.Path(tempfile.mkdtemp()) / "asrc07.json"
        src.write_text(json.dumps([{
            "uuid": "conv-a7", "chat_messages": [
                {"uuid": m, "sender": "human", "parent_message_uuid": p,
                 "created_at": "2026-09-20T10:00:%02d.000Z" % (10 + i),
                 "content": [{"type": "text", "text": m * 11}]}
                for i, (m, p) in enumerate(
                    [("a", None), ("b", "a"), ("c", "b")])]}]),
            encoding="utf-8")
        importer.import_file("jiaming", str(src))
        from mariposa import db
        with db.formal() as conn:
            conv = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='conv-a7'").fetchone()
        mem_id = memory.hold(
            actors["jiaming"], text="a7 记忆", memory_date="2026-09-20",
            date_confidence="exact", original_title="a7",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False)["memory_id"]
        return conv["id"], mem_id

    def _reverse(self, conv_id, start, end, s_off=None, e_off=None):
        from mariposa.relations import routing
        res = {"type": "source_range", "conversation_id": conv_id,
               "start_message_id": start, "end_message_id": end}
        if s_off is not None:
            res["start_char_offset"] = s_off
        if e_off is not None:
            res["end_char_offset"] = e_off
        return routing.list_relations({"resource": res, "direction": "in",
                                       "domains": ["source_binding"],
                                       "limit": 50, "offset": 0})

    def test_empty_and_overlapping_intervals(self, actors):
        from mariposa.source import binding
        conv_id, mem_id = self._setup(actors)
        # 正文 11 个 code points；绑定 [2,6)
        binding.bind("jiaming", mem_id, conv_id, "b", "b",
                     start_char_offset=2, end_char_offset=6)
        cases = [
            ("b", "b", 4, 4, 0),   # 空查询 [4,4) → 不命中
            ("b", "b", 2, 6, 1),   # 精确重叠 → 命中
            ("b", "b", 4, 8, 1),   # 部分重叠 → 命中
            ("b", "b", 999, 1000, 0),  # 超界 → 解析失败不命中不抛错
            ("b", "b", 0, 2, 0),   # 半开相邻 [0,2) 不重叠
        ]
        for s, e, so, eo, want in cases:
            got = len(self._reverse(conv_id, s, e, so, eo)["relations"])
            assert got == want, f"[{so},{eo}) 期望 {want} 得 {got}"

    def test_empty_binding_zero_coverage(self, actors):
        from mariposa.source import binding
        conv_id, mem_id = self._setup(actors)
        binding.bind("jiaming", mem_id, conv_id, "b", "b",
                     start_char_offset=4, end_char_offset=4)  # 空绑定
        got = len(self._reverse(conv_id, "b", "b", 2, 6)["relations"])
        assert got == 0, "空半开绑定（读侧零覆盖）不得命中重叠查询"


class TestGATE01TierDecoupling:

    def test_success_does_not_consume_anon(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 3)
        with _client() as c:
            codes = [c.get("/api/capabilities",
                           headers=AUTH_J).status_code for _ in range(5)]
        assert codes == [200] * 5, "成功请求不得占匿名档"

    def test_write_does_not_squeeze_read(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 1)
        monkeypatch.setattr(cfg, "GATE_RATE_WRITE_PER_MIN", 10)
        with _client() as c:
            w = c.post("/api/capability/memory.hold",
                       json={"arguments": {"text": "t"}},
                       headers=AUTH_J).status_code
            r = c.get("/api/capabilities", headers=AUTH_J).status_code
        assert w in (200, 400, 403)
        assert r == 200, "写档消耗不得挤占读档额度"

    def test_failed_auth_consumes_anon(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 3)
        with _client() as c:
            codes = [c.get("/api/capabilities",
                           headers=AUTH_BAD).status_code for _ in range(5)]
        assert codes[:3] == [401, 401, 401]
        assert codes[3] == 429, "认证失败请求占匿名档"


class TestGATE02ErrorSerialization:

    def test_http_429_has_retry_after_header_and_detail(self, actors,
                                                        monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 1)
        with _client() as c:
            c.get("/api/capabilities", headers=AUTH_J)
            r = c.get("/api/capabilities", headers=AUTH_J)
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert r.json()["error"]["detail"]["retry_after"] >= 1

    def test_lock_423_has_retry_after_header(self, actors):
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
            r = c.get("/api/capabilities", headers=AUTH_J)
        assert r.status_code == 423
        assert "Retry-After" in r.headers

    def test_write_limit_detail_not_stripped(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_WRITE_PER_MIN", 1)
        with _client() as c:
            c.post("/api/capability/memory.hold",
                   json={"arguments": {"text": "t"}}, headers=AUTH_J)
            r = c.post("/api/capability/memory.hold",
                       json={"arguments": {"text": "t"}}, headers=AUTH_J)
        assert r.status_code == 429
        assert r.json()["error"]["detail"]["retry_after"] >= 1, \
            "写档 429 不得丢失重试秒数"

    def test_mcp_rate_limit_not_http_200(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 1)
        with _client() as c:
            c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list"}, headers=AUTH_J)
            r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                     "method": "tools/list"},
                       headers=AUTH_J)
        assert r.status_code == 429, "MCP 主体限速不得退回 HTTP 200"
        assert "Retry-After" in r.headers
        body = r.json()["error"]
        assert body["data"]["code"] == "RATE_LIMITED"
        assert body["data"]["retry_after"] >= 1


class TestGATE03Recycling:

    def test_stale_windows_and_states_reclaimed(self, actors,
                                                monkeypatch):
        from mariposa import gate
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        for i in range(100):
            gate.note_auth_failure(f"10.0.0.{i}")   # fails=1 未锁
            gate.check_rate("anon", f"10.0.0.{i}")
        fake_now[0] += 100000.0  # 推进远超 TTL
        gate._maintain()
        assert not gate._auth_failures, "过期失败状态必须回收"
        assert not gate._rate_windows, "滑出窗口的限速键必须回收"

    def test_capacity_bound_evicts_oldest_unlocked(self, actors,
                                                   monkeypatch):
        from mariposa import gate
        monkeypatch.setattr(gate, "_MAX_TRACKED_KEYS", 5)
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        for i in range(20):
            gate.note_auth_failure(f"10.1.0.{i}")  # 全部未达阈值
        gate._maintain()
        assert len(gate._auth_failures) <= 5, \
            "失败状态容量必须有界（淘汰最旧未锁）"


class TestGATE04ZeroConfigDefense:

    def test_zero_read_limit_returns_429_not_500(self, actors,
                                                monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 0)
        with _client() as c:
            r = c.get("/api/capabilities", headers=AUTH_J)
        assert r.status_code == 429, "零限速配置必须拒绝服务而非 500"

    def test_config_clamps_non_positive(self):
        from mariposa import config as cfg
        assert cfg.GATE_AUTH_FAIL_THRESHOLD >= 1
        assert cfg.GATE_LOCKOUT_BASE_SECONDS >= 1
        assert cfg.GATE_LOCKOUT_MAX_SECONDS >= cfg.GATE_LOCKOUT_BASE_SECONDS
        assert cfg.GATE_RATE_READ_PER_MIN >= 1
        assert cfg.GATE_RATE_WRITE_PER_MIN >= 1
        assert cfg.GATE_RATE_ANON_PER_MIN >= 1


class TestGATE05LockExpiryAudit:

    def test_expiry_written_once(self, actors, monkeypatch):
        from mariposa import db, gate
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        from mariposa import config as cfg
        old = cfg.GATE_LOCKOUT_BASE_SECONDS
        cfg.GATE_LOCKOUT_BASE_SECONDS = 10
        try:
            with _client() as c:
                for _ in range(5):
                    c.get("/api/capabilities", headers=AUTH_BAD)
                fake_now[0] += 11  # 锁到期
                c.get("/api/capabilities", headers=AUTH_BAD)  # 触发识别
                c.get("/api/capabilities", headers=AUTH_BAD)  # 不重复记
        finally:
            cfg.GATE_LOCKOUT_BASE_SECONDS = old
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE"
                " event_type='auth.lock.expired'").fetchone()["c"]
        assert n == 1, "到期事件一次性写入，不得每请求重复"


class TestGATE06IsolatedAudit:

    def test_record_isolated_rolls_back_on_failure(self, actors):
        from mariposa import audit
        from mariposa import db
        from mariposa.audit import service as audit_service
        _orig = audit_service.record

        def _partial_then_fail(conn, *a, **kw):
            _orig(conn, *a, **kw)
            raise RuntimeError("simulated outbox failure")

        audit_service.record = _partial_then_fail
        try:
            with pytest.raises(RuntimeError):
                audit_service.record_isolated(
                    "auth.locked", "system", resource_id="1.2.3.4",
                    payload={"lock_level": 1})
        finally:
            audit_service.record = _orig
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE"
                " resource_id='1.2.3.4'").fetchone()["c"]
        assert n == 0, "隔离事务半途失败必须整体回滚，不留孤立事件"

    def test_lock_survives_audit_failure(self, actors, monkeypatch):
        from mariposa import audit
        from mariposa.audit import service as audit_service

        def _boom(*a, **kw):
            raise RuntimeError("audit down")

        monkeypatch.setattr(audit_service, "record_isolated", _boom)
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
            r = c.get("/api/capabilities", headers=AUTH_J)
        assert r.status_code == 423, "审计失败不得撤销锁定（安全优先）"
