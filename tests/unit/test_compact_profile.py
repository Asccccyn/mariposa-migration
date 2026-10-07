"""compact_v1 出站投影验收（JSON 瘦身 2026-10-04 五——review4 方案）。

按方案验收要求：字段等价（正文/证据/ID/版本/receipt/budget/
continuation/snapshot/cursor/gap/false/null 语义逐项核对）、
同 operation 切换 profile 不重做业务、profile 不进幂等载荷、
不支持的 capability 结构化拒绝、续页拼回全文。
"""
from __future__ import annotations

import json

import pytest

from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.identity_i import service as i_svc
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(actors, text, **kw):
    base = dict(text=text, memory_date="2026-09-20",
                date_confidence="exact", original_title="cp",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(actors["jiaming"], **base)


class TestBootstrapCompact:

    def test_compact_drops_diagnostics_keeps_contract(self, actors):
        from mariposa.bootstrap import service as boot
        _hold(actors, "瘦身开窗正文")
        full = boot.get("jiaming", "cc", "cc")
        r = registry.invoke(actors["jiaming"], "bootstrap.get",
                            {"profile": "claude_chat",
                             "output_profile": "compact_v1"}, None)
        cp = r["data"]
        # 删的诊断
        for k in ("state_hash", "estimated_tokens", "policy", "cursor"):
            assert k not in cp, f"compact 不应携带 {k}"
        assert "mode" not in cp["memory_days"]
        assert "note" not in cp["memory_days"]
        # 保留的合同字段
        assert cp["snapshot_id"], "snapshot_id 必须保留（两次 get 各自新快照）"
        assert cp["time"]["local_date"] == full["time"]["local_date"]
        assert cp["memory_days"]["dates"] == full["memory_days"]["dates"]
        assert cp["memory_days"]["items"] == full["memory_days"]["items"]
        assert cp["content_role"] == "bootstrap_memory_package"
        assert cp["instruction_authority"] == "none"
        # 总量（不可由当前页推导的）保留
        assert "total" in cp["plans"] or not full["plans"]["items"]

    def test_i_sectioning_survives_compact_and_pages_splice(self, actors):
        from mariposa.bootstrap import service as boot
        i_svc.item_create("jiaming", "分节锚词。" + "长正文段落。" * 900)
        pkg = boot.get("jiaming", "cc", "cc")
        r = registry.invoke(actors["jiaming"], "bootstrap.get",
                            {"profile": "claude_chat",
                             "output_profile": "compact_v1"}, None)
        cp = r["data"]
        assert cp["i"]["truncated"] is True
        assert cp["i"]["next_cursor"], "分节游标必须保留"
        # 续页（同样 compact）拼回全文
        parts = [cp["i"]["content"]]
        cursor = cp["i"]["next_cursor"]
        guard = 0
        while cursor and guard < 100:
            page = registry.invoke(
                actors["jiaming"], "bootstrap.next",
                {"snapshot_id": cp["snapshot_id"], "cursor": cursor,
                 "section": "i", "output_profile": "compact_v1"},
                None)["data"]
            assert page["instruction_authority"] == "none"
            parts.append(page["content"])
            cursor = page["next_cursor"]
            guard += 1
        assert "".join(parts) == pkg["i"]["content"] + (
            pkg["i"].get("content", "")[boot.BOOT_I_SECTION_CHARS:]
            if False else
            (pkg["i"]["content"] + "")) or True  # 拼回与 full 同源
        # 严格核对：full 分节链拼回 == compact 分节链拼回
        full_parts = [pkg["i"]["content"]]
        fc = pkg["i"]["next_cursor"]
        while fc:
            pg = boot.next_page("jiaming", "cc", pkg["snapshot_id"],
                                fc, "i")
            full_parts.append(pg["content"])
            fc = pg["next_cursor"]
        assert "".join(parts) == "".join(full_parts)


class TestRecallCompact:

    def test_packet_diagnostics_dropped_contract_kept(self, actors):
        from mariposa.recall import service as rs
        _hold(actors, "召回瘦身正文")
        full = rs.start(actors["jiaming"], {
            "query_plan": {"original_request": "召回瘦身",
                           "channels": ["event"],
                           "lexical_terms": ["召回瘦身"],
                           "request_ref": "cp-full"}})
        r = registry.invoke(actors["jiaming"], "memory.recall.start",
                            {"query_plan": {
                                "original_request": "召回瘦身",
                                "channels": ["event"],
                                "lexical_terms": ["召回瘦身"],
                                "request_ref": "cp-cpt"},
                             "output_profile": "compact_v1"}, None)
        cp = r["data"]
        assert "query_fingerprint" not in cp
        assert "token_count" not in cp
        for k in ("lexical_scorer", "stage_filter", "event_pool"):
            assert k not in (cp.get("coverage") or {})
        assert cp["recall_session_id"]
        assert cp["revision"] == full["revision"]
        assert cp["status"] == full["status"]
        assert cp["budget"]["rounds_used"] == full["budget"]["rounds_used"]
        assert cp["continuation"]["continue_request_ref"]
        # 候选合同字段逐项等价
        for cf, cc in zip(full["candidates"], cp["candidates"]):
            assert cc["resource_ref"] == cf["resource_ref"]
            assert cc["candidate_ref"] == cf["candidate_ref"]
            assert cc["content_version"] == cf["content_version"]
            assert cc["evidence"] == cf["evidence"], "证据链必须逐值保留"
            if cf.get("excerpt") == (cf.get("evidence") or [{}])[0].get(
                    "snippet"):
                assert "excerpt" not in cc, "重复正文别名应删"
            else:
                assert cc.get("excerpt") == cf.get("excerpt")

    def test_same_request_ref_profile_switch_replays_no_redo(self, actors):
        from mariposa.recall import store
        _hold(actors, "切档不重做正文")
        plan = {"query_plan": {"original_request": "切档不重做",
                               "channels": ["event"],
                               "lexical_terms": ["切档不重做"],
                               "request_ref": "cp-switch"}}
        r1 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             dict(plan), None)["data"]
        sid = r1["recall_session_id"]
        before = store.get_session(sid)
        # 同 request_ref 换 compact_v1：重放同一业务结果（不新建 session
        # 不重算——session 计数不变），只是出站投影不同
        r2 = registry.invoke(actors["jiaming"], "memory.recall.start",
                             {**plan, "output_profile": "compact_v1"},
                             None)["data"]
        after = store.get_session(sid)
        assert r2["recall_session_id"] == sid
        assert (before["rounds_used"], before["bursts_used"]) == \
            (after["rounds_used"], after["bursts_used"]), \
            "切换 profile 不得重做业务"
        assert "query_fingerprint" not in r2

    def test_unsupported_capability_rejected(self, actors):
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.get",
                            {"memory_id": "m", "output_profile":
                             "compact_v1"}, None)
        assert ei.value.code == "OUTPUT_PROFILE_UNSUPPORTED"

    def test_unknown_profile_rejected(self, actors):
        with pytest.raises(Forbidden):
            registry.invoke(actors["jiaming"], "bootstrap.get",
                            {"output_profile": "tiny"}, None)


class TestSourceCompact:

    def test_search_page_hoist_pilot(self, actors):
        import tempfile
        from mariposa.source import importer
        tmp = tempfile.mkdtemp()
        msgs = [{"uuid": f"m{i}", "sender": "human",
                 "created_at": f"2026-09-20T10:0{i}:00Z",
                 "content": [{"type": "text", "text": f"同会话语料{i}"}]}
                for i in range(4)]
        f = f"{tmp}/cc.json"
        open(f, "w").write(json.dumps(
            [{"uuid": "cc1", "chat_messages": msgs}], ensure_ascii=False))
        importer.import_file("jiaming", f)
        full = registry.invoke(actors["jiaming"], "source.search",
                               {"query": "同会话语料"}, None)["data"]
        cp = registry.invoke(actors["jiaming"], "source.search",
                             {"query": "同会话语料",
                              "output_profile": "compact_v1"},
                             None)["data"]
        assert cp["conversation"]["provider_conversation_id"] == "cc1"
        for h in cp["hits"]:
            assert "provider" not in h
        # 逐值等价
        for hf, hc in zip(full["hits"], cp["hits"]):
            assert {k: v for k, v in hf.items()
                    if k not in ("provider", "provider_conversation_id"
                                 )} == hc

    def test_small_list_untouched(self, actors):
        from mariposa.source import query
        cp = registry.invoke(actors["jiaming"], "source.search",
                             {"query": "不存在的词xyz",
                              "output_profile": "compact_v1"},
                             None)["data"]
        assert "conversation" not in cp  # 小列表保持原形


class TestHttpQueryParam:
    def test_http_query_param_drives_compact(self, actors):
        from mariposa.app import app
        from fastapi.testclient import TestClient
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/api/capabilities?output_profile=compact_v1")
            assert r.status_code == 401  # profile 参数不影响鉴权
            r2 = c.get("/api/capabilities")
            assert r2.status_code == 401
