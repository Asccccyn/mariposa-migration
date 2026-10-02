"""林石见 P1 复审（2026-10-01，对 8ed80ca）定点补丁的验收反例。

四项：P1-06 真 title-only / P1-08 双向串线 / P1-02 judge 对账 /
P1-07 原子发布与传输层。
"""
from __future__ import annotations

import pytest

from mariposa import config as cfg, db
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service, store
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


class _Cap(jb.JudgeProvider):
    name = "cap_p1rc"

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
                evaluation_status="evaluated", model_id="cap",
                prompt_version="t") for c in candidates],
            provider_status="evaluated")


class TestJudgeTitleOnlyReal:
    """P1-06 复审：真 title-only（正文不含查询词）。"""

    def test_title_only_tail_fact_reaches_jev(self, actors):
        """标题含"中秋"、正文 900 字无关 + 尾部"约会"事实、正文
        **不含**中秋——event_evidence 必须含尾部事实，不再是头部
        600 字盲窗。"""
        cap = _Cap()
        jb.register_for_tests(cap.name, cap)
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            body = "平" * 900 + "那天我们去河边约会看灯，人很多"
            assert "中秋" not in body
            memory.hold(
                actors["jiaming"], text=body, memory_date="2026-09-25",
                date_confidence="exact", original_title="中秋灯会",
                categories=["date"], creation_mode="contemporaneous",
                raw_pending=False)
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "中秋",
                               "channels": ["event"],
                               "lexical_terms": ["中秋"]}})
            # 前置：真 title-only（event_text 未命中）
            assert p["candidates"], "标题命中应交付"
            segs = (cap.payloads[-1]["state"]["candidates"][0]
                    ["segments"])
            assert any(s["field"] == "original_title" for s in segs)
            ev_segs = [s for s in segs
                       if s["field"] == "event_text"
                       and "event_evidence" in s["roles"]]
            assert ev_segs, "title-only 仍须附 event_evidence"
            joined = "".join(s["text"] for s in ev_segs).replace(" ", "")
            assert "约会" in joined, \
                f"尾部事实必须进入 event_evidence（锚落空补尾窗）：{[s['text'][:30] for s in ev_segs]}"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_anchored_window_when_body_has_term(self, actors):
        """正文含查询词但命中在深处：anchored 窗生效（非头部）。"""
        cap = _Cap()
        jb.register_for_tests(cap.name, cap)
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            body = "静" * 800 + "中秋那天的月亮很亮"
            memory.hold(
                actors["jiaming"], text=body, memory_date="2026-09-25",
                date_confidence="exact", original_title="夜记",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
            recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "中秋",
                               "channels": ["event"],
                               "lexical_terms": ["中秋"]}})
            segs = (cap.payloads[-1]["state"]["candidates"][0]
                    ["segments"])
            ev = [s for s in segs if s["field"] == "event_text"]
            joined = "".join(s["text"] for s in ev).replace(" ", "")
            assert "月亮" in joined, "深处命中进窗"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestPlanMappingStrict:
    """P1-08 复审：双向串线封堵。"""

    def test_link_without_plan_category_is_relation_only(self, actors):
        """daily 桶被 plan 关联（link_memory_ids）→ 阶段不变（CORE），
        不被强制成 plan 资源。"""
        from mariposa.plans import service as plans
        out = memory.hold(
            actors["jiaming"], text="旧年日常的备选路线记录",
            memory_date="2025-03-01", date_confidence="exact",
            original_title="远行清单", categories=["daily"],
            creation_mode="retrospective", raw_pending=False)
        mid = out["memory_id"]
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET held_at='2025-03-01T00:00:00+00:00'"
                " WHERE memory_id=?", (mid,))
        plans.create("jiaming", "关联旧桶的计划", state="active",
                     link_memory_ids=[mid])
        # 阶段仍是 CORE（link 只是关系）——标题不命中
        p = recall_service.start(actors["jiaming"], {
            "query_plan": {"original_request": "远行", "channels": ["event"],
                           "lexical_terms": ["远行"]}})
        assert p["candidates"] == [], "link 不得把 daily 桶抬成 plan/WIDE"
        from mariposa.recall import phase_policy as pp
        with db.formal() as conn:
            pass
        assert pp.phase_of(mid).stage == "CORE"

    def test_mixed_plan_daily_without_binding_is_gap(self, actors):
        """遗留 plan+daily 无绑定桶 → PLAN_MAPPING_GAP（不得按 daily
        H=20 算阶段绕过）。"""
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO memories(memory_id, current_version_no,"
                    " memory_date, date_confidence, visibility,"
                    " compression_state, created_at, updated_at, held_at)"
                    " VALUES('mem_legacy_mix', 1, '2026-09-01', 'exact',"
                    " 'active', 'full', '2026-09-01T00:00:00+00:00',"
                    " '2026-09-01T00:00:00+00:00',"
                    " '2026-09-01T00:00:00+00:00')")
                conn.execute(
                    "INSERT INTO memory_categories(memory_id, category,"
                    " added_by, created_at) VALUES"
                    " ('mem_legacy_mix', 'plan', 'jiaming',"
                    " '2026-09-01T00:00:00+00:00'),"
                    " ('mem_legacy_mix', 'daily', 'jiaming',"
                    " '2026-09-01T00:00:00+00:00')")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        from mariposa.recall import phase_policy as pp
        with pytest.raises(pp.DataGap):
            pp.phase_of("mem_legacy_mix")
        # 召回侧：gap 桶被跳过，不产出候选
        p = recall_service.start(actors["jiaming"], {
            "query_plan": {"original_request": "流水", "channels": ["event"],
                           "lexical_terms": ["流水"]}})
        refs = [c["resource_ref"] for c in p["candidates"]]
        assert "memory:mem_legacy_mix" not in refs


class TestJudgeCardinality:
    """P1-02 复审：provider 返回与送判集合严格对账。"""

    def _seed_two(self, actors):
        for i in range(2):
            memory.hold(
                actors["jiaming"], text=f"崧蓝事件第{i}则",
                memory_date="2026-09-25", date_confidence="exact",
                original_title=f"e{i}", categories=["daily"],
                creation_mode="contemporaneous", raw_pending=False)

    def test_partial_return_counts_unavailable(self, actors):
        """送 2 回 1：judged=1 / unavailable=1，Round2 被 judge_no_fault
        拒（不再当"全部判完"）。"""
        self._seed_two(actors)

        class Partial(jb.JudgeProvider):
            name = "partial_ret"
            def judge(self, plan, cs, ctx):
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        cs[0].get("candidate_ref") or cs[0]["resource_ref"],
                        cs[0].get("content_version"),
                        relevance_signal=0.8,
                        evaluation_status="evaluated",
                        model_id="p", prompt_version="t")],
                    provider_status="evaluated")

        jb.register_for_tests("partial_ret", Partial())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "partial_ret"
        try:
            p = recall_service.start(actors["jiaming"], {"query_plan": {
                "original_request": "崧蓝", "channels": ["event"],
                "lexical_terms": ["崧蓝"]}})
            sid = p["recall_session_id"]
            with db.recall_runtime() as conn:
                receipt = store.read_round1_receipt(conn, sid, 1)
            assert receipt["judged_count"] == 1
            assert receipt["unavailable_count"] == 1, \
                "漏返回的候选必须计入 unavailable"
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid,
                    "reason": "NO_DELIVERABLE_CANDIDATE"})
            gate = ei.value.detail["gate"]
            assert gate.get("judge_no_fault") is False
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_unknown_ref_fails_closed(self, actors):
        """返回未送判的 ref → 整轮 judge 作废（coverage=unavailable），
        Round2 拒。"""
        self._seed_two(actors)

        class GhostRef(jb.JudgeProvider):
            name = "ghost_ret"
            def judge(self, plan, cs, ctx):
                return jb.JudgeBatchResult(
                    items=[jb.JudgeItem(
                        "ghost:never-sent", None,
                        relevance_signal=0.8,
                        evaluation_status="evaluated",
                        model_id="g", prompt_version="t")],
                    provider_status="evaluated")

        jb.register_for_tests("ghost_ret", GhostRef())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "ghost_ret"
        try:
            p = recall_service.start(actors["jiaming"], {"query_plan": {
                "original_request": "崧蓝", "channels": ["event"],
                "lexical_terms": ["崧蓝"]}})
            assert p["coverage"]["judge"] == "unavailable"
            assert "judge_cardinality_violation" in p["degraded_reasons"]
            sid = p["recall_session_id"]
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid,
                    "reason": "NO_DELIVERABLE_CANDIDATE"})
            assert ei.value.code == "ROUND2_GATE_DENIED"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestMediaAtomicAndTransport:
    """P1-07 复审：原子发布 + 传输层。"""

    def _png(self):
        return b"\x89PNG\r\n\x1a\n" + b"q" * 128

    def test_half_file_repaired_atomically(self, actors):
        """crash window 反例：object 路径先有半截文件（无 DB 行）→
        finalize 必须验证并原子重写，不得信任既有文件。"""
        import hashlib
        from mariposa.media import service as media
        data = self._png()
        prep = media.upload_prepare("jiaming", "image/png", len(data))
        media.stage_bytes("jiaming", prep["upload_token"], data)
        h = hashlib.sha256(data).hexdigest()
        obj_path = media.config.RUNTIME_DIR / "objects" / f"{h}.png"
        obj_path.parent.mkdir(parents=True, exist_ok=True)
        obj_path.write_bytes(data[:10])  # 半截文件模拟
        out = media.upload_finalize("jiaming", prep["upload_token"])
        assert out["content_hash"] == h
        assert obj_path.read_bytes() == data, "半截文件必须被原子重写"

    def test_bad_json_is_structured_not_500(self, actors):
        """坏 JSON → 结构化 INVALID_JSON（此前 _json_body 在 try 外
        抛 500）。"""
        from fastapi.testclient import TestClient
        from mariposa.app import app
        with TestClient(app) as c:
            r = c.post("/api/capability/memory.hold",
                       content=b"{not json",
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}"})
        assert r.status_code != 500
        assert r.json()["error"]["code"] == "INVALID_JSON"

    def test_mcp_body_cap_and_auth_order(self, actors):
        """MCP：超限 body → -32600（不进 json 解析）；未鉴权 → 401。"""
        from fastapi.testclient import TestClient
        from mariposa.app import app
        with TestClient(app) as c:
            big = b"x" * (2 * 1024 * 1024 + 100)
            r = c.post("/mcp", content=b"j" * (3 * 1024 * 1024),
                       headers={"Authorization":
                                f"Bearer {TOKENS['jiaming']}"})
            assert r.status_code == 200
            assert r.json()["error"]["code"] == -32600
            r2 = c.post("/mcp", content=b"{}",
                        headers={"Authorization": "Bearer bad-token"})
            assert r2.status_code == 401

    def test_stage_rejects_oversized_declared(self, actors):
        """prepare 声明 20MB 上限内、实际塞超限字节 → 413。"""
        from fastapi.testclient import TestClient
        from mariposa.app import app
        from mariposa.media import service as media
        prep = media.upload_prepare("jiaming", "image/png",
                                    media._MAX_SIZE)
        with TestClient(app) as c:
            r = c.put(prep["stage_url"],
                      content=b"z" * (media._MAX_SIZE + 1),
                      headers={"Authorization":
                               f"Bearer {TOKENS['jiaming']}"})
        assert r.status_code == 413
