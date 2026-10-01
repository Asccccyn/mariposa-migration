"""自审（2026-10-01，程知行）反例回归——a28797f/2a64285 自查发现。

- 洞 1：raw 翻页游标的并发双花窗口 → 最终事务 CAS（store 级语义
  + 输家不动行）；
- 洞 2：raw round 首页接受客户端 offset（自由起跳，与服务端游标
  模型矛盾）→ 无 token 调用 offset 一律 0。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service, store
from mariposa.retrieval.judges import base as jb
from mariposa.retrieval.judges import typesafe_jev
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _grant_source_excerpt():
    class GrantedTypeSafe(typesafe_jev.TypeSafeJevJudge):
        name = "granted_typesafe_sa"
        _api_key = "test-key"

        def __init__(self):
            super().__init__()
            self._data_profile = frozenset(
                {"event_excerpt", "title_cue", "word_excerpt",
                 "source_excerpt"})
            self._disabled_reason = None

        def judge(self, plan, candidates, ctx):
            items = [jb.JudgeItem(
                candidate_ref=c.get("candidate_ref")
                or c["resource_ref"],
                candidate_version=str(c.get("content_version") or ""),
                relevance_signal=0.8,
                evaluation_status="evaluated", model_id=self.name,
                prompt_version="t") for c in candidates]
            return jb.JudgeBatchResult(
                items=items, provider_status="evaluated")

    jb.register_for_tests("granted_typesafe_sa", GrantedTypeSafe())
    from mariposa import config as cfg
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = "granted_typesafe_sa"
    return old


class TestContinuationCas:
    def test_store_cas_semantics(self, actors):
        """CAS 语义单元级：错 expect_token 输（行不动）、对 token 赢
        （重签）、clear_if 同样带 expect。并发双花时恰一赢家。"""
        with db.recall_runtime() as conn:
            t1 = store.issue_raw_continuation(
                conn, session_id="sa1", revision=1, burst_no=1,
                next_offset=20)
            # 输家：expect 不匹配 → None，行原样
            assert store.replace_raw_continuation(
                conn, session_id="sa1", revision=1, burst_no=1,
                expect_token="not-the-token", next_offset=40) is None
            row = store.read_raw_continuation(conn, "sa1", 1, 1)
            assert row["token"] == t1 and row["next_offset"] == 20, \
                "竞态输家不得改动游标行"
            # 赢家：重签新 token
            t2 = store.replace_raw_continuation(
                conn, session_id="sa1", revision=1, burst_no=1,
                expect_token=t1, next_offset=40)
            assert t2 and t2 != t1
            row = store.read_raw_continuation(conn, "sa1", 1, 1)
            assert row["token"] == t2 and row["next_offset"] == 40
            # clear_if：旧 token 清不动，现 token 才清得掉
            assert store.clear_raw_continuation_if(
                conn, session_id="sa1", revision=1, burst_no=1,
                expect_token=t1) is False
            assert store.clear_raw_continuation_if(
                conn, session_id="sa1", revision=1, burst_no=1,
                expect_token=t2) is True
            assert store.read_raw_continuation(conn, "sa1", 1, 1) is None

    def test_concurrent_double_spend_single_winner(self, actors):
        """公共链路确定性模拟：同 token 两次翻页，第二次必须
        CONTINUATION_INVALID（并发双花的时序退化即此形态）。"""
        from mariposa.source import importer
        import json as _json
        import tempfile
        import pathlib
        old = _grant_source_excerpt()
        try:
            memory.hold(
                actors["jiaming"], text="崧蓝事件正文", memory_date="2026-09-25",
                date_confidence="exact", original_title="sa",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False,
                our_words=[{"speaker": "qiaosheng", "text": "复述：崧蓝的傍晚",
                            "expression_kind": "paraphrase"}])
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "我当时的原话",
                               "channels": ["words"],
                               "lexical_terms": ["崧蓝"],
                               "evidence_requirement":
                                   "verbatim_required"}})
            sid = p["recall_session_id"]
            tmp = pathlib.Path(tempfile.mkdtemp())
            convs = [{"uuid": f"c-sa-{i}", "chat_messages": [{
                "uuid": f"m-sa-{i}", "sender": "human",
                "created_at": "2026-09-28T10:00:00.000Z",
                "content": [{"type": "text",
                             "text": f"崧蓝备忘第{i}条"}]}]}
                for i in range(25)]
            f = tmp / "sa.json"
            f.write_text(_json.dumps(convs, ensure_ascii=False),
                         encoding="utf-8")
            importer.import_file("jiaming", str(f))
            p1 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "operation_id": "op-sa-p1"})
            tok = p1["continuation"]["continuation_token"]
            # 第一次翻页成功（消费 tok，签新 token）
            recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "continuation_token": tok,
                "operation_id": "op-sa-p2a"})
            # 同 tok 再来（并发双花败者的确定性形态）→ 拒
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                    "continuation_token": tok,
                    "operation_id": "op-sa-p2b"})
            assert ei.value.code == "CONTINUATION_INVALID"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old


class TestFirstPageOffsetClamped:
    def test_client_offset_ignored_on_first_page(self, actors):
        """审计洞 2：首页传 offset=20（自由起跳）被服务端忽略——
        仍从 0 扫，下一页游标=20 而不是 40。"""
        from mariposa.source import importer
        import json as _json
        import tempfile
        import pathlib
        old = _grant_source_excerpt()
        try:
            memory.hold(
                actors["jiaming"], text="崧蓝事件正文", memory_date="2026-09-25",
                date_confidence="exact", original_title="sa2",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False,
                our_words=[{"speaker": "qiaosheng", "text": "复述：崧蓝的清晨",
                            "expression_kind": "paraphrase"}])
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "我当时的原话",
                               "channels": ["words"],
                               "lexical_terms": ["崧蓝"],
                               "evidence_requirement":
                                   "verbatim_required"}})
            sid = p["recall_session_id"]
            tmp = pathlib.Path(tempfile.mkdtemp())
            convs = [{"uuid": f"c-sb-{i}", "chat_messages": [{
                "uuid": f"m-sb-{i}", "sender": "human",
                "created_at": "2026-09-28T10:00:00.000Z",
                "content": [{"type": "text",
                             "text": f"崧蓝备忘第{i}条"}]}]}
                for i in range(25)]
            f = tmp / "sb.json"
            f.write_text(_json.dumps(convs, ensure_ascii=False),
                         encoding="utf-8")
            importer.import_file("jiaming", str(f))
            p1 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "offset": 20,          # 自由起跳企图
                "operation_id": "op-sb-p1"})
            cont = p1["continuation"]
            assert cont and cont["offset"] == 20, \
                "首页必须从 0 开始（客户端 offset 不采纳），下页=20"
            assert p1["candidates"], "首页正常交付（非空窗）"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old
