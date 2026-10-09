"""bea4f10 复审 9 项修复回归（2026-10-04 cho 复审包）。

逐条对应复审 findings：CR-01-R1（event/words 角色缩权锁重放）、
RSRC-01（md 统一围栏词法状态）、RSRC-02（身份结构化序列化）、
RSRC-03（页面上传 filename）、RSRC-05（归档异常不卡 running）、
RSRC-07（区间反查重叠+序号倒置）、CR-02-R1（HTTP query compact）、
CR-03-R1（MCP schema 声明 profile）、RE-WR-01（契约与 Registry 同步）。
全部合成夹具，不用任何真实文档。
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import MariposaError
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.source import importer
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj")}


def _write_tmp(content: str, name: str = "t.md") -> pathlib.Path:
    p = pathlib.Path(tempfile.mkdtemp()) / name
    p.write_text(content, encoding="utf-8")
    return p


def _parse_md(text: str, name: str = "t.md"):
    from mariposa.source import md_transcript
    return md_transcript.parse(_write_tmp(text, name), name)


class TestRSRC01UnifiedLexicalState:
    """RSRC-01：Thinking 提取/闭围栏/方言识别共享围栏词法状态。"""

    def test_thinking_inside_code_example_kept_in_body(self):
        """四反引号代码示例内的 ### Thinking 与三反引号示例是代码
        内容——不得被剥离成思考块、正文不得缺块。"""
        md = (
            "## Claude\n"
            "**2026-09-19T18:12:05.343Z**\n\n"
            "````\n"
            "### Thinking\n"
            "```\n"
            "示例围栏内容\n"
            "```\n"
            "````\n\n"
            "正文结尾。\n")
        provider, elements = _parse_md(md)
        assert provider == "claude"
        msgs = elements[0]["chat_messages"]
        assert len(msgs) == 1
        types = [b["type"] for b in msgs[0]["content"]]
        assert "thinking" not in types, "代码示例内的 Thinking 样式不得剥离"
        text = next(b["text"] for b in msgs[0]["content"]
                    if b["type"] == "text")
        assert "### Thinking" in text and "示例围栏内容" in text \
            and "正文结尾" in text

    def test_content_fence_not_a_closing_line(self):
        """代码块内的 ```not_a_closing_fence 是围栏内正文——闭栏必须
        是纯围栏行；其后的 ## User 不得把一条 assistant 切成两条。"""
        md = (
            "## Claude\n"
            "**2026-09-19T18:12:05.343Z**\n\n"
            "```python\n"
            "```not_a_closing_fence\n"
            "## User\n"
            "still inside code\n"
            "```\n\n"
            "收尾正文。\n")
        provider, elements = _parse_md(md)
        msgs = elements[0]["chat_messages"]
        assert len(msgs) == 1, "围栏内的 ## User 不得切块"
        assert msgs[0]["sender"] == "assistant"
        text = next(b["text"] for b in msgs[0]["content"]
                    if b["type"] == "text")
        assert "## User" in text and "still inside code" in text

    def test_gemini_dialect_not_hijacked_by_code_heading(self):
        """Gemini 正文代码里引用 ## User 不得把整个文件认成 claude
        ——方言判定共享围栏状态。"""
        md = (
            "# you asked\n"
            "message time: 2026-09-20 10:00:00\n\n"
            "Question 正文。\n\n"
            "# gemini response\n"
            "message time: 2026-09-20 10:01:00\n\n"
            "回答正文。\n\n"
            "```text\n"
            "## User\n"
            "```\n")
        provider, elements = _parse_md(md, "g.md")
        assert provider == "gemini"
        msgs = elements[0]["chat_messages"]
        assert len(msgs) == 2
        text_all = "\n".join(
            b["text"] for m in msgs for b in m["content"]
            if b["type"] == "text")
        assert "Question 正文" in text_all and "回答正文" in text_all


class TestRSRC02StructuredIdentity:
    """RSRC-02：身份前象结构化序列化——内容含分隔符不撞 ID。"""

    def test_gemini_repeat_and_suffix_no_collision(self):
        """同 sender 无时间三条 OK、OK、OK#1——第二条的 occurrence
        与第三条正文后缀不得拼出同一前象（复审 Gemini 反例）。"""
        md = (
            "# you asked\nquestion\n\n"
            "# gemini response\nOK\n\n"
            "# you asked\nquestion2\n\n"
            "# gemini response\nOK\n\n"
            "# you asked\nquestion3\n\n"
            "# gemini response\nOK#1\n")
        _, elements = _parse_md(md, "g.md")
        msgs = elements[0]["chat_messages"]
        assistants = [m for m in msgs if m["sender"] == "assistant"]
        assert len(assistants) == 3, "三条不同消息不得合并"
        uuids = [m["uuid"] for m in assistants]
        assert len(set(uuids)) == 3

    def test_claude_pipe_fields_no_collision(self):
        """同时间 text=x|y/thinking=z 与 text=x/thinking=y|z——竖线
        进入字段值不得拼出同一前象（复审 Claude 反例）。"""
        md1 = (
            "## Claude\n"
            "**2026-09-19T18:12:05.343Z**\n\n"
            "### Thinking\n"
            "```\nz\n```\n\nx|y\n")
        md2 = (
            "## Claude\n"
            "**2026-09-19T18:12:05.343Z**\n\n"
            "### Thinking\n"
            "```\ny|z\n```\n\nx\n")
        _, e1 = _parse_md(md1)
        _, e2 = _parse_md(md2)
        u1 = e1[0]["chat_messages"][0]["uuid"]
        u2 = e2[0]["chat_messages"][0]["uuid"]
        assert u1 != u2, "不同 (text, thinking) 字段值不得共享身份"

    def test_same_content_same_id_stable(self):
        """同内容重导身份稳定（结构化序列化不破坏幂等）。"""
        md = ("## Claude\n**2026-09-19T18:12:05.343Z**\n\n同一段正文。\n")
        _, e1 = _parse_md(md)
        _, e2 = _parse_md(md)
        assert e1[0]["chat_messages"][0]["uuid"] == \
            e2[0]["chat_messages"][0]["uuid"]


class TestRSRC05ArchiveFailure:
    """RSRC-05：归档异常走 failed 定稿，不 UnboundLocalError 卡 running。"""

    def test_archive_publish_failure_fails_batch_and_retry_works(
            self, actors, monkeypatch):
        from mariposa.source import archive
        src = _write_tmp(json.dumps([{
            "uuid": "conv-r05",
            "chat_messages": [{
                "uuid": "r05-m1", "sender": "human",
                "created_at": "2026-09-20T10:00:00.000Z",
                "content": [{"type": "text", "text": "正文"}]}]}]),
            "r05.json")

        def boom(*a, **k):
            raise OSError("simulated archive failure")

        _real_publish = archive.publish_snapshot
        monkeypatch.setattr(archive, "publish_snapshot", boom)
        with pytest.raises(OSError):
            importer.import_file("jiaming", str(src))
        with db.formal() as conn:
            row = conn.execute(
                "SELECT status, error, raw_path, stats FROM"
                " source_import_batches ORDER BY import_started_at DESC"
            ).fetchone()
        assert row["status"] == "failed", "归档失败必须 failed 定稿"
        assert "archive publish failed" in (row["error"] or "")
        assert row["stats"], "失败留痕依赖的 stats 必须已落库"
        # 立即重试不被 running 租约挡（failed 可接管）
        monkeypatch.setattr(archive, "publish_snapshot", _real_publish)
        out = importer.import_file("jiaming", str(src))
        assert out["status"] == "completed"


def _conv_element(mid, parent, seq, text="m"):
    return {
        "uuid": "conv-r07",
        "chat_messages": [
            {"uuid": m, "sender": "human",
             "created_at": "2026-09-20T10:00:0%d.000Z" % (i % 10),
             "content": [{"type": "text", "text": m}]}
            for i, m in enumerate([mid if isinstance(mid, str) else x
                                   for x in [mid]])]}


def _import_chain(msgs: list[tuple[str, str | None]]):
    """导入一条 parent 链：msgs = [(uuid, parent_uuid), ...]。"""
    src = _write_tmp(json.dumps([{
        "uuid": "conv-chain",
        "chat_messages": [
            {"uuid": m, "sender": "human",
             "parent_message_uuid": p,
             "created_at": "2026-09-20T10:00:%02d.000Z" % (10 + i),
             "content": [{"type": "text", "text": m}]}
            for i, (m, p) in enumerate(msgs)]}]), "chain.json")
    return importer.import_file("jiaming", str(src))


def _pid_of(conv_provider_id: str, message_uuid: str) -> str:
    with db.formal() as conn:
        row = conn.execute(
            "SELECT provider_message_id FROM source_messages WHERE"
            " provider_conversation_id=? AND provider_message_id=?",
            (conv_provider_id, message_uuid)).fetchone()
    assert row is not None
    return row["provider_message_id"]


class TestRSRC07RangeReverseOverlap:
    """RSRC-07：反查重叠=真实路径交集；序号倒置不误排除。"""

    def _setup_chain(self):
        _import_chain([("A", None), ("B", "A"), ("C", "B"), ("D", "C")])
        with db.formal() as conn:
            conv = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='conv-chain'").fetchone()
        return conv["id"]

    def _reverse(self, conv_id, start, end):
        from mariposa.relations import routing
        return routing.list_relations({
            "resource": {"type": "source_range",
                         "conversation_id": conv_id,
                         "start_message_id": start,
                         "end_message_id": end},
            "direction": "in", "domains": ["source_binding"],
            "limit": 50, "offset": 0})

    def test_partial_overlap_and_superset_hit(self, actors):
        from mariposa.source import binding
        conv_id = self._setup_chain()
        mem_id = memory.hold(
            actors["jiaming"], text="r07 记忆", memory_date="2026-09-20",
            date_confidence="exact", original_title="r07",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False)["memory_id"]
        binding.bind("jiaming", mem_id, conv_id, "B", "C")
        for s, e, want in (("B", "C", 1), ("A", "D", 1), ("A", "B", 1),
                           ("C", "D", 1), ("A", "A", 0), ("D", "D", 0)):
            got = len(self._reverse(conv_id, s, e)["relations"])
            assert got == want, f"查询 {s}→{e} 期望 {want} 得 {got}"

    def test_sequence_inversion_survives(self, actors):
        """先导 B/C 子集（B.parent 指向尚不在库的 A），再导完整
        X/A/B/C 快照——B/C 首见定格 seq 0/1，A 新插 seq 1，形成
        A.seq>B.seq 倒置且 parent 链 X→A→B→C 可达；绑定 A→C 后
        查 B 仍须命中（路径交集判定，不看序号）。"""
        from mariposa.source import binding
        sub = _write_tmp(json.dumps([{
            "uuid": "conv-inv", "chat_messages": [
                {"uuid": m, "sender": "human",
                 "parent_message_uuid": p,
                 "created_at": "2026-09-20T11:00:%02d.000Z" % (10 + i),
                 "content": [{"type": "text", "text": m}]}
                for i, (m, p) in enumerate([("B", "A"), ("C", "B")])]}]),
            "sub.json")
        importer.import_file("jiaming", str(sub))
        full = _write_tmp(json.dumps([{
            "uuid": "conv-inv", "chat_messages": [
                {"uuid": m, "sender": "human",
                 "parent_message_uuid": p,
                 "created_at": "2026-09-20T12:00:%02d.000Z" % (10 + i),
                 "content": [{"type": "text", "text": m}]}
                for i, (m, p) in enumerate(
                    [("X", None), ("A", "X"), ("B", "A"), ("C", "B")])]}]),
            "full.json")
        importer.import_file("jiaming", str(full))
        with db.formal() as conn:
            conv = conn.execute(
                "SELECT id FROM source_conversations WHERE"
                " provider_conversation_id='conv-inv'").fetchone()
            seqs = dict(conn.execute(
                "SELECT provider_message_id, sequence FROM"
                " source_messages WHERE conversation_id=?",
                (conv["id"],)).fetchall())
        assert seqs["A"] > seqs["B"], "夹具应形成序号倒置"
        mem_id = memory.hold(
            actors["jiaming"], text="inv 记忆", memory_date="2026-09-20",
            date_confidence="exact", original_title="inv",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False)["memory_id"]
        binding.bind("jiaming", mem_id, conv["id"], "A", "C")
        got = len(self._reverse(conv["id"], "B", "B")["relations"])
        assert got == 1, "序号倒置不得把路径交集内的消息排除"


class TestCR01R1ReplayRoleGate:
    """CR-01-R1：撤回证据角色授权后，旧 operation 重放与 fresh 同权。"""

    @staticmethod
    def _register(name, profile):
        """原生 TypeSafeJevJudge（仅改 profile；judge() 走真 segments
        授权层 + fake HTTP——fresh 侧的空段不判/正文剥离由真实代码
        路径决定，重放侧才检验 revalidate_replayed 的共享判定）。"""
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev

        class G(typesafe_jev.TypeSafeJevJudge):
            def __init__(self):
                super().__init__()
                # 实例级 key：__init__ 会用空 env 覆盖类属性，缺 key
                # 会被判 judge unavailable 降级（探针 key_present=true）
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

        def fake_urlopen(req, timeout=None):
            return _Resp(json.dumps(
                {"model": "fake-jev",
                 "answers": {"candidate_0": {"noul": 0.9}}}))

        monkeypatch.setattr(typesafe_jev.urllib.request, "urlopen",
                            fake_urlopen)

    def test_event_role_withdrawn_replay_suppressed(self, actors,
                                                    monkeypatch):
        from mariposa import config as cfg
        self._fake_http(monkeypatch)
        self._register("g_full", {"event_excerpt", "title_cue",
                                  "word_excerpt", "source_excerpt",
                                  "structured_metadata"})
        self._register("g_narrow", {"title_cue"})
        old = cfg.RECALL_JUDGE_PROVIDER
        plan = {"query_plan": {
            "original_request": "审计锚词",
            "channels": ["event"],
            "lexical_terms": ["审计锚词"]}}
        try:
            memory.hold(actors["jiaming"],
                        text="审计锚词这一段是事件正文",
                        memory_date="2026-09-20", date_confidence="exact",
                        original_title="cr01", categories=["daily"],
                        creation_mode="contemporaneous", raw_pending=False)
            cfg.RECALL_JUDGE_PROVIDER = "g_full"
            r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                                 {**plan, "operation_id": "cr01-op"},
                                 None)["data"]
            assert len(r1["candidates"]) == 1
            # 撤回 event_excerpt：fresh 与旧 operation 重放同权 0 卡
            # RRA-008 后语义：换 provider 名=政策新纪元（重放按
            # RECALL_POLICY_CHANGED 拒——正确行为）。本测语义=同 provider
            # 许可缩权：同名重注册窄许可实例，env 不动（纪元不变）
            self._register("g_full", {"title_cue"})
            fresh = registry.invoke(actors["jiaming"],
                                    "memory.recall.start",
                                    {**plan, "operation_id": "cr01-fresh"},
                                    None)["data"]
            replay = registry.invoke(actors["jiaming"],
                                     "memory.recall.start",
                                     {**plan, "operation_id": "cr01-op"},
                                     None)["data"]
            assert len(fresh["candidates"]) == 0
            assert len(replay["candidates"]) == 0, \
                "event_excerpt 撤回后旧 operation 不得继续释放正文"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_word_role_withdrawn_replay_suppressed(self, actors,
                                                   monkeypatch):
        from mariposa import config as cfg
        self._fake_http(monkeypatch)
        self._register("gw_full", {"event_excerpt", "title_cue",
                                   "word_excerpt",
                                   "structured_metadata"})
        self._register("gw_narrow", {"event_excerpt"})
        old = cfg.RECALL_JUDGE_PROVIDER
        plan = {"query_plan": {
            "original_request": "我当时的原话",
            "channels": ["words"],
            "lexical_terms": ["话语缩权"],
            "evidence_requirement": "verbatim_required"}}
        try:
            memory.hold(
                actors["jiaming"], text="话语缩权事件正文",
                memory_date="2026-09-25", date_confidence="exact",
                original_title="cr01w", categories=["daily"],
                creation_mode="contemporaneous", raw_pending=False,
                our_words=[{"speaker": "qiaosheng",
                            "text": "话语缩权测试锚词",
                            "expression_kind": "paraphrase"}])
            cfg.RECALL_JUDGE_PROVIDER = "gw_full"
            r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                                 {**plan, "operation_id": "cr01w-op"},
                                 None)["data"]
            assert len(r1["candidates"]) == 1
            self._register("gw_full", {"event_excerpt"})
            fresh = registry.invoke(actors["jiaming"],
                                    "memory.recall.start",
                                    {**plan, "operation_id":
                                     "cr01w-fresh"}, None)["data"]
            replay = registry.invoke(actors["jiaming"],
                                     "memory.recall.start",
                                     {**plan, "operation_id": "cr01w-op"},
                                     None)["data"]
            assert len(fresh["candidates"]) == 0
            assert len(replay["candidates"]) == 0, \
                "word_excerpt 撤回后旧 operation 不得继续释放话语正文"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestCR02HttpQueryCompact:
    """CR-02-R1：query 参数并入 arguments（不再丢在外层 body）。"""

    def test_query_profile_drives_compact_and_unknown_rejected(
            self, actors):
        from mariposa.app import app
        from fastapi.testclient import TestClient
        _auth = {"Authorization": f"Bearer {TOKENS['jiaming']}"}
        _args = {"arguments": {"profile": "claude_chat"}}
        with TestClient(app) as c:
            legacy = c.post("/api/capability/bootstrap.get",
                            json=_args, headers=_auth).json()["data"]
            compact = c.post(
                "/api/capability/bootstrap.get?output_profile=compact_v1",
                json=_args, headers=_auth).json()["data"]
            assert "state_hash" in legacy
            assert "state_hash" not in compact, \
                "query 协商的 compact_v1 必须实际生效"
            unknown = c.post(
                "/api/capability/bootstrap.get?output_profile=tiny",
                json=_args, headers=_auth)
            assert 400 <= unknown.status_code < 500, \
                "未知 profile 必须结构化拒绝，不得静默 200"
            assert unknown.json()["error"]["code"] == "INVALID_ARGUMENT"


class TestCR03MCPSchemaDeclaresProfile:
    """CR-03-R1：tools/list 严格 schema 声明 output_profile 枚举。"""

    def test_schema_declares_enum_and_call_succeeds(self, actors):
        from mariposa.app import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            tools = c.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1,
                              "method": "tools/list"},
                headers={"Authorization":
                         f"Bearer {TOKENS['jiaming']}"}).json()
            by_name = {t["name"]: t for t in tools["result"]["tools"]}
            schema = by_name["mariposa_bootstrap_get"]["inputSchema"]
            assert schema["properties"]["output_profile"]["enum"] == \
                ["legacy", "compact_v1"]
            assert "output_profile" not in \
                by_name["mariposa_memory_get"]["inputSchema"].get(
                    "properties", {}), "不支持的能力不声明该参数"
            # 标准 MCP 调用（参数经 schema 合法）协商 compact
            out = c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {
                          "name": "mariposa_bootstrap_get",
                          "arguments": {"profile": "claude_chat",
                                        "output_profile": "compact_v1"}}},
                headers={"Authorization":
                         f"Bearer {TOKENS['jiaming']}"}).json()
            payload = json.loads(out["result"]["content"][0]["text"])
            assert "state_hash" not in payload["data"]


class TestREWR01ContractRegistrySync:
    """RE-WR-01：静态契约与 live Registry 同源一致。"""

    def test_contract_matches_registry(self):
        from mariposa.capabilities import v1_compat
        v1_compat.register_v1_compat()
        contract_path = pathlib.Path(__file__).resolve().parents[2] / \
            "contracts" / "capabilities.v1.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        by_name = {c["canonical_name"]: c
                   for c in contract["capabilities"]}
        assert set(by_name) == set(registry.REGISTRY), \
            "契约能力名单必须与 Registry 完全一致（导出后忘了重新生成）"
        for name, cap in registry.REGISTRY.items():
            c = by_name[name]
            assert c["write"] == cap.write, f"{name} write 不一致"
            assert c["idempotent"] == cap.idempotent, \
                f"{name} idempotent 不一致（消费者会按过时提示重试）"
            assert sorted(c["allowed_principals"]) == \
                sorted(cap.allowed_principals), \
                f"{name} allowed_principals 不一致"
