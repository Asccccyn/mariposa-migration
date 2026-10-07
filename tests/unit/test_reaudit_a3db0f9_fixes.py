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
                                 None)["data"]
            assert len(r1["candidates"]) == 1

            cfg.RECALL_JUDGE_PROVIDER = "r2_title"
            fresh_t = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-f1"}, None)["data"]
            replay_t = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-op"}, None)["data"]
            assert len(fresh_t["candidates"]) == 0
            assert len(replay_t["candidates"]) == 0, \
                "title_cue-only：标题命中不得让旧 operation 重放事件正文"

            cfg.RECALL_JUDGE_PROVIDER = "r2_event"
            fresh_e = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-f2"}, None)["data"]
            replay_e = registry.invoke(
                actors["jiaming"], "memory.recall.start",
                {**plan, "operation_id": "r2-op"}, None)["data"]
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
        from mariposa.errors import Forbidden as _F
        cases = [
            ("b", "b", 4, 4, 0),   # 空查询 [4,4) → 不命中
            ("b", "b", 2, 6, 1),   # 精确重叠 → 命中
            ("b", "b", 4, 8, 1),   # 部分重叠 → 命中
            ("b", "b", 0, 2, 0),   # 半开相邻 [0,2) 不重叠
        ]
        for s, e, so, eo, want in cases:
            got = len(self._reverse(conv_id, s, e, so, eo)["relations"])
            assert got == want, f"[{so},{eo}) 期望 {want} 得 {got}"
        # 超界：SOURCE_RANGE_OFFSET 向上冒泡 403（不再静默 200+空）
        with pytest.raises(_F) as ei:
            self._reverse(conv_id, "b", "b", 999, 1000)
        assert ei.value.code == "SOURCE_RANGE_OFFSET"

    def test_tail_omitted_zero_coverage(self, actors):
        """ASRC-07（三轮）：None 尾端用真实长度归一——start 偏移
        ==len(text) 的零覆盖不得算相交。"""
        from mariposa.memory import service as memory
        from mariposa.source import binding, importer
        src = pathlib.Path(tempfile.mkdtemp()) / "z0.json"
        src.write_text(json.dumps([{
            "uuid": "conv-z0", "chat_messages": [
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
                " provider_conversation_id='conv-z0'").fetchone()
        mem_id = memory.hold(
            actors["jiaming"], text="z0 记忆", memory_date="2026-09-20",
            date_confidence="exact", original_title="z0",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False)["memory_id"]
        # 绑定 [2,11)（尾端省略 = 到消息末尾），查询 [11,末尾) 零覆盖
        binding.bind("jiaming", mem_id, conv["id"], "b", "b",
                     start_char_offset=2)
        got = len(self._reverse(conv["id"], "b", "b", 11)["relations"])
        assert got == 0, "s_off==len 的零覆盖不得命中"
        got2 = len(self._reverse(conv["id"], "b", "b", 2)["relations"])
        assert got2 == 1, "同起点的正常覆盖仍应命中"

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


class TestCR01R3FullProfileMatrix:
    """CR-01-R3（2026-10-04 三轮复审）：通道 × 许可子集全矩阵回归。

    教训（R1→R2→R3）：逐格修复审点名的反例永远收敛不了——本轮
    改为**全矩阵**断言"同权"：对每个场景先以全量许可建 session 与
    operation，再逐个许可子集切换，断言同 operation 重放的候选数
    与 fresh 新请求完全一致（fresh 是基准，无论其内部门控细节）。
    5 个文本角色共 32 个子集 × 3 场景 × 2 通道卡。
    """

    ALL_ROLES = ("event_excerpt", "title_cue", "word_excerpt",
                 "source_excerpt", "structured_metadata")

    @staticmethod
    def _register(name, profile):
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev

        class G(typesafe_jev.TypeSafeJevJudge):
            def __init__(self):
                super().__init__()
                self._api_key = "k"
                self._data_profile = frozenset(profile)
                self._disabled_reason = None

        jb.register_for_tests(name, G())

    @pytest.fixture()
    def seeded(self, actors):
        from mariposa.identity import service as identity
        from mariposa.memory import service as memory
        reset_all()
        j = identity.Principal("jiaming", "周家明", "agent",
                               "claude_chat", "bj")
        # ① 正文命中（普通事件卡）——锚词互不重叠，避免分词后串台
        memory.hold(j, text="柚子茶冲泡事件正文", memory_date="2026-09-20",
                    date_confidence="exact", original_title="柚子茶标题",
                    categories=["daily"], creation_mode="contemporaneous",
                    raw_pending=False)
        # ② 标题独占命中（title-only 事件卡）
        memory.hold(j, text="凤梨酥场景的事件正文", memory_date="2026-09-21",
                    date_confidence="exact", original_title="凤梨酥只在标题",
                    categories=["daily"], creation_mode="contemporaneous",
                    raw_pending=False)
        # ③ 话语命中：同一召回返回两张卡（word-target 事件卡 + words 卡）
        memory.hold(j, text="杨枝甘露场景的事件正文", memory_date="2026-09-22",
                    date_confidence="exact", original_title="杨枝甘露标题",
                    categories=["daily"], creation_mode="contemporaneous",
                    raw_pending=False,
                    our_words=[{"speaker": "qiaosheng",
                                "text": "杨枝甘露在原话里",
                                "expression_kind": "paraphrase"}])
        return j

    def _run(self, monkeypatch, seeded, plan, tag):
        """返回 {(profile 子集): (fresh_n, replay_n)}。"""
        import itertools
        from mariposa import config as cfg
        from mariposa.capabilities import registry
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
                {"model": "m",
                 "answers": {f"candidate_{i}": {"noul": 0.9}
                             for i in range(8)}})))
        results = {}
        old = cfg.RECALL_JUDGE_PROVIDER
        try:
            cfg.RECALL_JUDGE_PROVIDER = f"mx_{tag}_full"
            self._register(f"mx_{tag}_full", set(self.ALL_ROLES))
            base = registry.invoke(
                seeded, "memory.recall.start",
                {**plan, "operation_id": f"mx-{tag}-base"},
                None)["data"]
            for n in range(1, len(self.ALL_ROLES) + 1):
                for combo in itertools.combinations(self.ALL_ROLES, n):
                    key = ",".join(combo)
                    pname = f"mx_{tag}_{n}_{abs(hash(key)) % 10**8}"
                    self._register(pname, set(combo))
                    cfg.RECALL_JUDGE_PROVIDER = pname
                    fresh = registry.invoke(
                        seeded, "memory.recall.start",
                        {**plan, "operation_id": f"mx-{tag}-{n}-"
                         f"{abs(hash(key)) % 10**8}"},
                        None)["data"]
                    replay = registry.invoke(
                        seeded, "memory.recall.start",
                        {**plan, "operation_id": f"mx-{tag}-base"},
                        None)["data"]
                    results[key] = (len(fresh["candidates"]),
                                    len(replay["candidates"]))
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old
        return results, len(base["candidates"])

    def test_event_anchor_matrix(self, seeded, monkeypatch):
        plan = {"query_plan": {
            "original_request": "柚子茶", "channels": ["event"],
            "lexical_terms": ["柚子茶"]}}
        results, base_n = self._run(monkeypatch, seeded, plan, "ev")
        assert base_n == 1
        bad = {k: v for k, v in results.items() if v[0] != v[1]}
        assert not bad, f"正文命中矩阵重放/新建不一致: {bad}"

    def test_title_only_matrix(self, seeded, monkeypatch):
        plan = {"query_plan": {
            "original_request": "凤梨酥只在标题", "channels": ["event"],
            "lexical_terms": ["凤梨酥只在标题"]}}
        results, base_n = self._run(monkeypatch, seeded, plan, "ti")
        assert base_n == 1
        bad = {k: v for k, v in results.items() if v[0] != v[1]}
        assert not bad, f"标题命中矩阵重放/新建不一致: {bad}"

    def test_words_anchor_matrix(self, seeded, monkeypatch):
        """CR-01-R3 主反例场景：话语命中（word-target 事件卡 + words
        卡同场）——事件许可撤回时两卡 fresh/replay 同权。"""
        plan = {"query_plan": {
            "original_request": "杨枝甘露在原话里",
            "channels": ["event", "words"],
            "lexical_terms": ["杨枝甘露在原话里"]}}
        results, base_n = self._run(monkeypatch, seeded, plan, "wd")
        assert base_n == 2
        # CR-01-R3 权威语义（三轮复审 6×32 实测矩阵）：word-target
        # 卡交付以 event_excerpt 为准——不预设各格 fresh 值（随
        # fixture 段回退细节而异），合同是**每格 replay==fresh**：
        # fresh=0 的格（复审反例：缺事件许可）replay 必须 0；
        # fresh=1 的格 replay 保留且不弱于 fresh 的正文口径
        bad = {k: v for k, v in results.items() if v[0] != v[1]}
        assert not bad, f"话语命中矩阵重放/新建不一致: {bad}"

    def test_event_and_words_double_hit_matrix(self, seeded, monkeypatch):
        """CR-01-R3 第二反例场景：event_text 与 our_words 双命中——
        通道仍是 event，必要角色恒 event_excerpt（word 许可不参与
        交付判定），全格 replay==fresh。"""
        plan = {"query_plan": {
            "original_request": "双命中锚词",
            "channels": ["event", "words"],
            "lexical_terms": ["双命中锚词"]}}
        from mariposa.identity import service as identity
        from mariposa.memory import service as memory
        memory.hold(seeded, text="双命中锚词的事件正文",
                    memory_date="2026-09-23", date_confidence="exact",
                    original_title="双命中标题", categories=["daily"],
                    creation_mode="contemporaneous", raw_pending=False,
                    our_words=[{"speaker": "qiaosheng",
                                "text": "双命中锚词的原话",
                                "expression_kind": "paraphrase"}])
        results, base_n = self._run(monkeypatch, seeded, plan, "dh")
        assert base_n >= 1
        bad = {k: v for k, v in results.items() if v[0] != v[1]}
        assert not bad, f"双命中矩阵重放/新建不一致: {bad}"


class TestSelfAudit20261005:
    """2026-10-05 自查批（不等复审点名的离舱点）。"""

    @staticmethod
    def _register(name, profile):
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev

        class G(typesafe_jev.TypeSafeJevJudge):
            def __init__(self):
                super().__init__()
                self._api_key = "k"
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
                {"model": "m",
                 "answers": {f"candidate_{i}": {"noul": 0.9}
                             for i in range(8)}})))

    def test_navigate_cards_survive_profile_withdrawn(self, actors,
                                                      monkeypatch):
        """自查②：导航卡是结构事实卡（fresh 的 navigate 不经 judge、
        无文本段），缩权 profile 下重放不得按 event 通道误杀。"""
        from mariposa import config as cfg
        from mariposa.capabilities import registry
        from mariposa.identity import service as identity
        from mariposa.memory import service as memory
        self._fake_http(monkeypatch)
        self._register("snav_full", {"event_excerpt", "title_cue",
                                     "word_excerpt", "source_excerpt",
                                     "structured_metadata"})
        self._register("snav_narrow", {"title_cue"})
        old = cfg.RECALL_JUDGE_PROVIDER
        try:
            j = identity.Principal("jiaming", "周家明", "agent",
                                   "claude_chat", "bj")
            memory.hold(j, text="导航自查较早事件", memory_date="2026-09-10",
                        date_confidence="exact", original_title="早",
                        categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
            memory.hold(j, text="导航自查锚词事件", memory_date="2026-09-20",
                        date_confidence="exact", original_title="晚",
                        categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
            plan = {"query_plan": {"original_request": "导航自查锚词",
                                   "channels": ["event"],
                                   "lexical_terms": ["导航自查锚词"]},
                    "operation_id": "snav-1"}
            cfg.RECALL_JUDGE_PROVIDER = "snav_full"
            r1 = registry.invoke(j, "memory.recall.start", dict(plan),
                                 None)["data"]
            sid = r1["recall_session_id"]
            nav_args = {"session_id": sid, "direction": "earlier",
                        "operation_id": "snavn-1"}
            full = registry.invoke(j, "memory.recall.navigate",
                                   dict(nav_args), None)["data"]
            n_full = len(full["candidates"])
            assert n_full >= 1, "夹具应产出导航卡"
            body_full = json.dumps(full, ensure_ascii=False)
            assert "导航自查较早事件" not in body_full, \
                "导航卡不带正文（结构事实）"
            # 撤到 title_cue-only：fresh navigate 仍交付结构卡；
            # 同 op 重放必须同权（不得按 event_excerpt 误杀）
            cfg.RECALL_JUDGE_PROVIDER = "snav_narrow"
            fresh_n = registry.invoke(
                j, "memory.recall.navigate",
                {**nav_args, "operation_id": "snavn-2"},
                None)["data"]["candidates"]
            replay = registry.invoke(j, "memory.recall.navigate",
                                     dict(nav_args), None)["data"]
            assert len(fresh_n) == n_full
            assert len(replay["candidates"]) == n_full, \
                "缩权 profile 下导航卡重放不得被误杀"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_health_and_static_rate_limited(self, actors, monkeypatch):
        """自查①：/health 与静态面不再是无门禁打点面（IP 匿名档）。"""
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 3)
        with _client() as c:
            codes = [c.get("/health").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429, "/health 必须受 IP 匿名档限速"

    def test_lock_covers_unauthenticated_surface(self, actors):
        """自查①联动：在 /api 触发锁定的来源，/health 同样被挡。"""
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
            r = c.get("/health")
        assert r.status_code == 423

    def test_corrupt_binding_offsets_zero_coverage(self, actors):
        """自查③：绑定偏移数据损坏（负数/超界）按零覆盖保守处理，
        不放大覆盖范围、不让反查报错。"""
        from mariposa import db
        from mariposa.memory import service as memory
        from mariposa.relations import routing
        from mariposa.source import binding, importer
        src = pathlib.Path(tempfile.mkdtemp()) / "corrupt.json"
        src.write_text(json.dumps([{
            "uuid": "conv-cor", "chat_messages": [
                {"uuid": m, "sender": "human", "parent_message_uuid": p,
                 "created_at": "2026-09-20T10:00:%02d.000Z" % (10 + i),
                 "content": [{"type": "text", "text": m * 11}]}
                for i, (m, p) in enumerate(
                    [("a", None), ("b", "a"), ("c", "b")])]}]),
            encoding="utf-8")
        importer.import_file("jiaming", str(src))
        with db.formal() as conn:
            conv = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='conv-cor'").fetchone()
        mem_id = memory.hold(
            actors["jiaming"], text="cor 记忆", memory_date="2026-09-20",
            date_confidence="exact", original_title="cor",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False)["memory_id"]
        binding.bind("jiaming", mem_id, conv["id"], "b", "b",
                     start_char_offset=2, end_char_offset=6)
        # 数据损坏：负数 start 偏移
        with db.formal() as conn:
            conn.execute(
                "UPDATE memory_source_bindings SET start_char_offset=-5")
        out = routing.list_relations({"resource": {
            "type": "source_range", "conversation_id": conv["id"],
            "start_message_id": "b", "end_message_id": "b",
            "start_char_offset": 0, "end_char_offset": 4},
            "direction": "in", "domains": ["source_binding"],
            "limit": 10, "offset": 0})
        assert len(out["relations"]) == 0, \
            "负数绑定偏移按零覆盖，不得放大命中范围"


class TestFourthRound20261005:
    """四轮复审修复回归：CR-NAV-01 + AF-GATE-01/02/03。"""

    @staticmethod
    def _register(name, profile, disabled=False, key="k"):
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev

        class G(typesafe_jev.TypeSafeJevJudge):
            def __init__(self):
                super().__init__()
                self._api_key = key
                self._data_profile = (None if disabled
                                      else frozenset(profile))
                self._disabled_reason = ("allowed_data_policy_missing"
                                         if disabled else None)

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
                {"model": "m",
                 "answers": {"candidate_0": {"noul": 0.9}}})))

    def test_judge_down_navigate_replay_keeps_structured_cards(
            self, actors, monkeypatch):
        """CR-NAV-01：judge 不可用（缺策略/缺 key/空 profile）时
        fresh navigate 交付无正文结构卡——同 operation 重放同权。"""
        from mariposa import config as cfg
        from mariposa.capabilities import registry
        from mariposa.identity import service as identity
        from mariposa.memory import service as memory
        self._fake_http(monkeypatch)
        self._register("r4_full", {"event_excerpt", "title_cue",
                                   "word_excerpt", "source_excerpt",
                                   "structured_metadata"})
        self._register("r4_no_policy", None, disabled=True)
        self._register("r4_no_key", {"event_excerpt"}, key="")
        old = cfg.RECALL_JUDGE_PROVIDER
        try:
            j = identity.Principal("jiaming", "周家明", "agent",
                                   "claude_chat", "bj")
            memory.hold(j, text="四轮导航较早事件", memory_date="2026-09-10",
                        date_confidence="exact", original_title="早",
                        categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
            memory.hold(j, text="四轮导航锚词事件", memory_date="2026-09-20",
                        date_confidence="exact", original_title="晚",
                        categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
            plan = {"query_plan": {"original_request": "四轮导航锚词",
                                   "channels": ["event"],
                                   "lexical_terms": ["四轮导航锚词"]},
                    "operation_id": "r4-1"}
            cfg.RECALL_JUDGE_PROVIDER = "r4_full"
            r1 = registry.invoke(j, "memory.recall.start", dict(plan),
                                 None)["data"]
            nav_args = {"session_id": r1["recall_session_id"],
                        "direction": "earlier",
                        "operation_id": "r4n-1"}
            full = registry.invoke(j, "memory.recall.navigate",
                                   dict(nav_args), None)["data"]
            n_full = len(full["candidates"])
            assert n_full >= 1
            for down in ("r4_no_policy", "r4_no_key"):
                cfg.RECALL_JUDGE_PROVIDER = down
                fresh = registry.invoke(
                    j, "memory.recall.navigate",
                    {**nav_args, "operation_id": f"r4n-{down}"},
                    None)["data"]["candidates"]
                replay = registry.invoke(j, "memory.recall.navigate",
                                         dict(nav_args),
                                         None)["data"]
                assert len(fresh) == n_full, f"{down} fresh 导航不得丢卡"
                assert len(replay["candidates"]) == n_full, \
                    f"{down} judge 不可用时导航重放不得丢结构卡"
                assert "四轮导航较早事件" not in json.dumps(
                    replay, ensure_ascii=False)
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_afgate01_overflow_stickiness(self, monkeypatch):
        """AF-GATE-01：容量腾出后，仍在溢出桶窗口内的主体不得获得
        独立新桶（否则同主体 60s 内双份配额）。"""
        from mariposa import gate
        from mariposa import config as cfg
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        monkeypatch.setattr(gate, "_MAX_TRACKED_KEYS", 3)
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 2)
        gate.check_rate("anon", "10.9.0.1")
        gate.check_rate("anon", "10.9.0.2")
        gate.check_rate("anon", "10.9.0.3")     # 容量满
        fake_now[0] += 59
        assert gate.check_rate("read", "p-main") == 0   # 入溢出桶
        assert gate.check_rate("read", "p-main") == 0   # 溢出桶满 2
        assert gate.check_rate("read", "p-main") > 0    # 第 3 次超限
        fake_now[0] += 1                                # t60：旧键回收
        gate._maintain()
        # 溢出桶内 p-main 的 t59 计数未滑出 → 粘性：不得建独立桶
        assert gate.check_rate("read", "p-main") > 0, \
            "溢出窗口未滑出前不得给独立新桶（双份配额）"
        fake_now[0] += 60                               # 溢出滑空
        gate._maintain()
        assert gate.check_rate("read", "p-main") == 0, \
            "溢出桶滑空后恢复独立桶资格"

    def test_afgate02_mcp_protocol_errors_metered(self, actors,
                                                  monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 1)
        with _client() as c:
            first = c.post("/mcp", content=b"{bad json",
                           headers=AUTH_J)
            second = c.post("/mcp", content=b"{bad json",
                            headers=AUTH_J)
        # 坏 JSON 是 RPC 层 -32700（传输层 200）；计档后超限是 429
        assert first.status_code == 200
        assert first.json()["error"]["code"] == -32700
        assert second.status_code == 429, "协议错误路径必须计读档"

    def test_afgate02_unknown_paths_locked_and_metered(self, actors,
                                                       monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 3)
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
            r = c.get("/apiX")            # 未注册路径（宽前缀豁免曾漏）
            assert r.status_code == 423, "锁定必须覆盖未注册路径"
        from mariposa import gate
        gate.reset_for_tests()            # 解除上一段的锁定再测 404 补计
        with _client() as c:
            codes = [c.get("/api/unknown-path").status_code
                     for _ in range(5)]
        assert codes[:3] == [404, 404, 404]
        assert codes[3] == 429, "404 路径必须事后补计匿名档"

    def test_afgate03_maintain_consumes_pending_expiry(self, actors,
                                                       monkeypatch):
        """AF-GATE-03：原来源不回来的到期事件由维护流程有界消费
        （sink 落审计）并回收，不再永久积压。"""
        from mariposa import db, gate
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        monkeypatch.setattr(gate, "_MAX_TRACKED_KEYS", 3)
        consumed = []

        def sink(events):
            consumed.extend(events)

        gate.set_expiry_sink(sink)
        for i in range(20):
            for _ in range(5):
                gate.note_auth_failure(f"10.8.0.{i}")
        fake_now[0] += 7 * 86400  # 锁到期且远超 TTL
        gate._maintain()
        gate._maintain()          # 消费标记后下一轮回收
        assert len(consumed) == 20, "20 条到期事件必须经 sink 消费"
        assert not gate._auth_failures, "消费后按 TTL 回收，不积压"

    def test_afgate03_sink_failure_keeps_state(self, monkeypatch):
        from mariposa import gate
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])

        def boom(events):
            raise RuntimeError("audit down")

        gate.set_expiry_sink(boom)
        for _ in range(5):
            gate.note_auth_failure("10.7.0.1")
        fake_now[0] += 7 * 86400
        gate._maintain()
        st = gate._auth_failures.get("10.7.0.1")
        assert st is not None and not st.get("expired_reported"), \
            "sink 失败时保留未消费状态（下轮重试），不静默丢"


class TestSelfAuditRound2:
    """2026-10-05 第二轮自查（第五轮前）。"""

    def test_sink_partial_failure_no_duplicate_audit(self, actors,
                                                     monkeypatch):
        """自查 B：sink 批内单条审计失败——不重复写已成功条目
        （到期事件一次性语义），状态照常回收，失败条 stderr 留痕。"""
        from mariposa import db, gate
        from mariposa.audit import service as audit_service
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        calls = {"n": 0}

        def flaky_record(event_type, actor, resource_id=None, payload=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("2nd write fails")
            return _orig_record_isolated(event_type, actor,
                                         resource_id=resource_id,
                                         payload=payload)

        _orig_record_isolated = audit_service.record_isolated
        monkeypatch.setattr(audit_service, "record_isolated",
                            flaky_record)
        # lifespan sink 直接经 gate 注入等价行为
        def sink(events):
            for ip, payload in events:
                try:
                    audit_service.record_isolated(
                        "auth.lock.expired", "system", resource_id=ip,
                        payload={**payload, "consumer": "maintain"})
                except Exception:
                    pass  # app sink 的单条留痕语义（此处测 gate 端）

        gate.set_expiry_sink(sink)
        for i in range(3):
            for _ in range(5):
                gate.note_auth_failure(f"10.6.0.{i}")
        fake_now[0] += 7 * 86400
        gate._maintain()
        gate._maintain()
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE"
                " event_type='auth.lock.expired'").fetchone()["c"]
        assert n == 2, "第 2 条失败不得重试重写（3 锁至多 2 条成功）"
        assert not gate._auth_failures, "部分失败仍照常回收，不积压"

    def test_trailing_slash_paths_do_not_crash(self, actors):
        """自查 C：/mcp/、/api/ 尾斜杠等 redirect/404 路径不炸，
        且经过 middleware 门禁链。"""
        with _client() as c:
            r1 = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"},
                        headers=AUTH_J, follow_redirects=False)
            r2 = c.get("/api/", headers=AUTH_J)
            r3 = c.get("/static/nothing.png")
        assert r1.status_code in (307, 404, 200)
        assert r2.status_code in (404, 307, 200)
        assert r3.status_code == 404
