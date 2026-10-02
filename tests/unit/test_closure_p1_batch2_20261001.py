"""全量审计（2026-10-01）第二批 P1-06/P1-08/P1-07 验收测试。

对应审计 §七：JUDGE-02、PLAN-01..03、MEDIA-01..04。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.errors import Forbidden, NotFound
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


class _Cap(jb.JudgeProvider):
    name = "cap_p1b2"

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


class TestJudgeQueryAnchor:
    """JUDGE-02：title-only 长事件的 event_evidence 围绕本次查询锚词。"""

    def test_title_hit_long_body_anchor_window(self, actors):
        cap = _Cap()
        jb.register_for_tests(cap.name, cap)
        from mariposa import config as cfg
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            body = "平" * 900 + "中秋那天的约会在河边看灯，人很多"
            memory.hold(
                actors["jiaming"], text=body, memory_date="2026-09-25",
                date_confidence="exact", original_title="中秋灯会",
                categories=["date"], creation_mode="contemporaneous",
                raw_pending=False)
            recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "中秋",
                               "channels": ["event"],
                               "lexical_terms": ["中秋"]}})
            segs = (cap.payloads[-1]["state"]["candidates"][0]
                    ["segments"])
            ev = next(s for s in segs
                      if s["field"] == "event_text"
                      and "event_evidence" in s["roles"])
            assert "约会" in ev["text"].replace(" ", ""), \
                f"900 字后的查询事实必须进入 event_evidence 窗：{ev['text'][:60]}…"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


class TestPlanMapping:
    """PLAN-01..03：plan 分类 ↔ plan 资源状态接线。"""

    def _old_plan_memory(self, actors, state):
        from mariposa.plans import service as plans
        plan = plans.create("jiaming", "新年旅行计划", state=state)
        out = memory.hold(
            actors["jiaming"], text="计划里的备选路线都写在纸上",
            memory_date="2025-03-01", date_confidence="exact",
            original_title="远行清单", categories=["plan"],
            creation_mode="retrospective", raw_pending=False,
            plan_ids=[plan["plan_id"]])
        # 久远 held_at（一年以上）——非 plan 语义下早就是 CORE
        with db.formal() as conn:
            conn.execute(
                "UPDATE memories SET held_at='2025-03-01T00:00:00+00:00'"
                " WHERE memory_id=?", (out["memory_id"],))
        return plan, out["memory_id"]

    def _find_by(self, actors, term):
        p = recall_service.start(actors["jiaming"], {
            "query_plan": {"original_request": term, "channels": ["event"],
                           "lexical_terms": [term]}})
        return p["candidates"]

    def test_active_plan_is_wide(self, actors):
        """PLAN-01：open plan → WIDE——久远桶仍按标题命中（标题词
        只出现在标题，正文不含）。"""
        self._old_plan_memory(actors, "active")
        cands = self._find_by(actors, "远行")
        assert cands, "active plan 的桶应 WIDE（标题可命中）"

    def test_done_plan_is_core(self, actors):
        """PLAN-02：done plan → CORE——标题退出，正文仍可命中。"""
        plan, mid = self._old_plan_memory(actors, "done")
        assert self._find_by(actors, "远行") == [], \
            "终态 plan 桶应 CORE：标题不再命中"
        body_hits = self._find_by(actors, "备选路线")
        assert any(c.get("memory_id") == mid for c in body_hits), \
            "CORE 仍含 event_text：正文命中不丢"

    def test_plan_category_without_binding_rejected(self, actors):
        """PLAN-03：plan 分类无资源绑定 → 写入时结构化拒绝，不等到
        召回时静默消失。"""
        with pytest.raises(Forbidden) as ei:
            memory.hold(
                actors["jiaming"], text="无绑定的计划桶", memory_date="2026-09-25",
                date_confidence="exact", original_title="x",
                categories=["plan"], creation_mode="contemporaneous",
                raw_pending=False)
        assert ei.value.code == "PLAN_BINDING_REQUIRED"
        # categories.replace 加 plan 同样要求已有绑定
        out = memory.hold(
            actors["jiaming"], text="普通桶", memory_date="2026-09-25",
            date_confidence="exact", original_title="y",
            categories=["daily"], creation_mode="contemporaneous",
            raw_pending=False)
        from mariposa.memory import categories as cats_mod
        with db.formal() as conn:
            with pytest.raises(Forbidden) as e2:
                cats_mod.replace(conn, out["memory_id"], ["plan"],
                                 "jiaming")
        assert e2.value.code == "PLAN_BINDING_REQUIRED"

    def test_plan_link_with_unknown_plan_rejected(self, actors):
        from mariposa.plans import service as plans
        plans.create("jiaming", "真实计划", state="active")
        with pytest.raises(NotFound):
            memory.hold(
                actors["jiaming"], text="绑到不存在的计划", memory_date="2026-09-25",
                date_confidence="exact", original_title="z",
                categories=["plan"], creation_mode="contemporaneous",
                raw_pending=False, plan_ids=["plan_nope"])


class TestMediaTwoPhase:
    """MEDIA-01..04：真两阶段上传。"""

    def _client(self):
        from fastapi.testclient import TestClient
        from mariposa.app import app
        return TestClient(app)

    def _png(self):
        return b"\x89PNG\r\n\x1a\n" + b"p" * 64

    def test_stage_then_finalize_token_only(self, actors):
        """MEDIA-01：prepare→PUT stage→finalize(token only)；data_b64
        被 schema 结构性拒绝。"""
        from mariposa.media import service as media
        prep = media.upload_prepare("jiaming", "image/png", len(self._png()))
        c = self._client()
        r = c.put(prep["stage_url"], content=self._png(),
                  headers={"Authorization": f"Bearer {TOKENS['jiaming']}"})
        assert r.status_code == 200 and r.json()["data"]["staged"] is True
        # data_b64 不得再被接受（additionalProperties=False）
        from mariposa.capabilities import registry
        from mariposa.capabilities import input_schemas
        with pytest.raises(Forbidden):
            input_schemas.validate("media.upload.finalize",
                                   {"upload_token": prep["upload_token"],
                                    "data_b64": "AAAA"})
        out = registry.invoke(actors["jiaming"], "media.upload.finalize",
                              {"upload_token": prep["upload_token"]}, None)
        assert out["data"]["content_hash"]
        with db.formal() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM media_objects").fetchone()["c"]
        assert n == 1

    def test_other_principal_cannot_stage_or_finalize(self, actors):
        """MEDIA-02：他人 token 不可 stage / finalize。"""
        from mariposa.media import service as media
        prep = media.upload_prepare("jiaming", "image/png", len(self._png()))
        c = self._client()
        r = c.put(prep["stage_url"], content=self._png(),
                  headers={"Authorization": f"Bearer {TOKENS['qiaosheng']}"})
        assert r.status_code == 403
        with pytest.raises(Forbidden):
            media.upload_finalize("qiaosheng", prep["upload_token"])

    def test_expired_token_rejected(self, actors):
        """MEDIA-03：TTL 真执行（此前 expires_in_s 只是返回值）。"""
        from mariposa.media import service as media
        from datetime import datetime, timezone, timedelta
        prep = media.upload_prepare("jiaming", "image/png", 8)
        token = prep["upload_token"]
        media._staging[token]["created"] = (
            datetime.now(timezone.utc)
            - timedelta(seconds=media._STAGING_TTL_S + 1)).isoformat()
        with pytest.raises(NotFound):
            media.stage_bytes("jiaming", token, b"12345678")
        with pytest.raises(NotFound):
            media.upload_finalize("jiaming", token)

    def test_size_and_hash_mismatch_rejected(self, actors):
        """MEDIA-04：字节数不符拒绝；暂存被改后 finalize 复核拒绝。"""
        from mariposa.media import service as media
        prep = media.upload_prepare("jiaming", "image/png", 100)
        with pytest.raises(Forbidden):
            media.stage_bytes("jiaming", prep["upload_token"], b"short")
        # 正常 stage 后篡改暂存文件 → finalize hash 复核拒绝
        data = b"x" * 100
        prep2 = media.upload_prepare("jiaming", "image/png", 100)
        media.stage_bytes("jiaming", prep2["upload_token"], data)
        p = media._staging_dir() / prep2["upload_token"]
        p.write_bytes(b"y" * 100)  # 篡改
        with pytest.raises(Forbidden) as ei:
            media.upload_finalize("jiaming", prep2["upload_token"])
        assert ei.value.code == "MEDIA_STAGED_MISMATCH"
