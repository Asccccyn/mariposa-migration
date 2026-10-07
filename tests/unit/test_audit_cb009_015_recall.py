"""Recall 域审计修复回归（2026-10-02 基线审计 P1 批）。

- CB-009：commit-at-end 最终事务内重验 session 状态/revision
  （审计反例 refine_reopens_closed_session / round2_commits_stale_revision）。
- CB-010：Raw continuation 与旧 operation 重放复核当前 Raw 开关
  （raw_permission_toggle_ignored）。
- CB-011：raw 深搜跨进程租约（two_process_round2_duplicate_cost 的
  互斥层；本文件验证租约语义与占用时零昂贵调用）。
- CB-012：judge 完成证明只计有效判断（wrong_judge_version_signed_
  complete / round2_duplicate_judge_ref_accepted）。
- CB-013：words 浏览截断如实报 partial（words_topk_complete_receipt）。
- CB-014：查询指纹绑定 + source_msg 卡尊重 reject
  （replay_old_plan_as_current_revision / round2_replay_ignores_
  rejected_source）。
- CB-015：exact_phrases 进入 Raw 深搜 FTS 硬约束
  （round2_exact_phrase_dropped）。

全部走真实链路（fake judge），计算/提交交错的审计反例以同步 hook
在真实事务边界之间插入另一项完整 service 操作（不替换 DB/CAS）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa import config as cfg
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import pipeline, service as recall_service, store
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _hold(principal, text, **kw):
    base = dict(text=text, memory_date="2026-09-25",
                date_confidence="exact", original_title="cb",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
    base.update(kw)
    return memory.hold(principal, **base)


def _seed_source(text="原文里的崧蓝染色记忆", tag="cb009"):
    from mariposa.source import importer
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    f = tmp / f"{tag}.json"
    f.write_text(_json.dumps([{
        "uuid": f"c-{tag}",
        "chat_messages": [{
            "uuid": f"{tag}-m1", "sender": "human",
            "created_at": "2026-09-20T10:00:00.000Z",
            "content": [{"type": "text", "text": text}]}]}],
        ensure_ascii=False), encoding="utf-8")
    return importer.import_file("jiaming", str(f))


def _start(actors, terms=("崧蓝",), op="op-cb-s", **plan_extra):
    plan = {"original_request": "我当时的原话", "channels": ["words"],
            "lexical_terms": list(terms),
            "evidence_requirement": "verbatim_required"}
    plan.update(plan_extra)
    return registry.invoke(actors["jiaming"], "memory.recall.start",
                           {"query_plan": plan, "operation_id": op}, None)


def _round2(actors, sid, reason="EVIDENCE_INSUFFICIENT", op="op-cb-r2"):
    return registry.invoke(actors["jiaming"], "memory.recall.round2",
                           {"session_id": sid, "reason": reason,
                            "operation_id": op}, None)


def _grant_source_excerpt():
    """wp04 同款：带 source_excerpt 许可的 fake judge。"""
    from mariposa.retrieval.judges import base as jb
    from mariposa.retrieval.judges import typesafe_jev

    class GrantedTypeSafe(typesafe_jev.TypeSafeJevJudge):
        name = "cb_granted_typesafe"
        _api_key = "test-key"

        def __init__(self):
            super().__init__()
            self._data_profile = frozenset(
                {"event_excerpt", "title_cue", "word_excerpt",
                 "source_excerpt"})
            self._disabled_reason = None

        def judge(self, plan, candidates, ctx):
            items = [jb.JudgeItem(
                candidate_ref=c.get("candidate_ref") or c["resource_ref"],
                candidate_version=str(c.get("content_version") or ""),
                relevance_signal=0.8,
                evaluation_status="evaluated", model_id=self.name,
                prompt_version="t") for c in candidates]
            return jb.JudgeBatchResult(
                items=items, provider_status="evaluated",
                degraded_reason=None, cache_hits=0,
                cache_misses=len(candidates), request_count=1)

    jb.register_for_tests("cb_granted_typesafe", GrantedTypeSafe())
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = "cb_granted_typesafe"
    return old


def _seed_word(principal, text="复述：崧蓝染色的傍晚"):
    return _hold(principal, "崧蓝事件正文",
                 our_words=[{"speaker": "qiaosheng", "text": text,
                             "expression_kind": "paraphrase"}])


# ---------------------------------------------------------------- CB-009

class TestCommitTimeRevalidation:

    def test_refine_cannot_reopen_closed_session(self, actors, monkeypatch):
        """审计反例：refine 计算后 close(cancelled)，最终事务曾把
        session 复活为 ACTIVE/revision2——现在提交时重验拒绝。"""
        _seed_word(actors["jiaming"])
        r1 = _start(actors)
        sid = r1["data"]["recall_session_id"]

        orig = recall_service._run_round_compute

        def hooked(session, plan, principal):
            out = orig(session, plan, principal)
            # 合法事务交错：计算完成后、提交前，另一流程 close
            recall_service.close(actors["jiaming"],
                                 {"session_id": sid,
                                  "reason": "并发关闭"}, None)
            return out

        monkeypatch.setattr(recall_service, "_run_round_compute", hooked)
        with pytest.raises(Forbidden) as ei:
            recall_service.refine(actors["jiaming"], {
                "session_id": sid,
                "query_plan": {"original_request": "改词",
                               "channels": ["words"],
                               "lexical_terms": ["别的"],
                               "evidence_requirement":
                                   "verbatim_required"},
                "expected_revision": 1}, None)
        assert ei.value.code == "SESSION_STATE_CHANGED"
        with db.recall_runtime() as conn:
            row = conn.execute(
                "SELECT status, current_revision FROM recall_sessions"
                " WHERE session_id=?", (sid,)).fetchone()
        assert row["status"] == "CANCELLED", "终态不得被提交复活"
        assert row["current_revision"] == 1, "拒绝路径零 revision 前进"

    def test_accept_cannot_overwrite_concurrent_close(self, actors,
                                                      monkeypatch):
        """F03（2026-10-03 审计 P1）：accept 锁外读 ACTIVE → 对端
        close 提交 CANCELLED（close 不推进 revision，revision CAS
        拦不住）→ accept(close=True) 曾把终态覆盖成 RESOLVED。
        现在最终写锁内重读行重跑状态机，拒绝且 CANCELLED 保留。"""
        _seed_word(actors["jiaming"])
        r1 = _start(actors, op="op-f03-acc")
        sid = r1["data"]["recall_session_id"]
        original = recall_service.require_owned_session
        armed = [True]

        def interleave(*a, **kw):
            result = original(*a, **kw)
            if armed[0]:
                armed[0] = False
                recall_service.close(actors["jiaming"], {
                    "session_id": sid, "outcome": "cancelled"}, None)
            return result

        monkeypatch.setattr(recall_service, "require_owned_session",
                            interleave)
        with pytest.raises(Forbidden):
            recall_service.accept(actors["jiaming"], {
                "session_id": sid, "close": True}, None)
        assert store.get_session(sid)["status"] == "CANCELLED", \
            "并发 close 的终态不得被 accept 覆盖"

    def test_reject_after_terminal_rejected_in_tx(self, actors,
                                                  monkeypatch):
        """F03 同面：reject 的候选写入同样在写锁内重验——终态
        session 上不再落 rejected 标记。"""
        _seed_word(actors["jiaming"])
        r1 = _start(actors, op="op-f03-rej")
        sid = r1["data"]["recall_session_id"]
        cand = r1["data"]["candidates"][0]["candidate_ref"]
        original = recall_service.require_owned_session
        armed = [True]

        def interleave(*a, **kw):
            result = original(*a, **kw)
            if armed[0]:
                armed[0] = False
                recall_service.close(actors["jiaming"], {
                    "session_id": sid, "outcome": "cancelled"}, None)
            return result

        monkeypatch.setattr(recall_service, "require_owned_session",
                            interleave)
        with pytest.raises(Forbidden):
            recall_service.reject(actors["jiaming"], {
                "session_id": sid, "candidate_ref": cand}, None)
        states = {c["candidate_ref"]: c["state"]
                  for c in store.list_candidates(sid)}
        assert states[cand] != "rejected", "终态后不得再写候选状态"

    def test_round2_cannot_commit_stale_revision(self, actors, monkeypatch):
        """审计反例：Round2 raw 计算后 refine 换词（revision2），旧
        Round2 仍提交并交付 revision1 的结果——现在提交时重验拒绝。"""
        old = _grant_source_excerpt()
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors)
            sid = r1["data"]["recall_session_id"]
            _seed_source()

            orig_raw = pipeline.raw_deep_search

            def hooked_raw(principal, plan, **kw):
                out = orig_raw(principal, plan, **kw)
                # 合法事务交错：raw 计算完成后、提交前 refine 换词
                recall_service.refine(actors["jiaming"], {
                    "session_id": sid,
                    "query_plan": {"original_request": "完全换了",
                                   "channels": ["words"],
                                   "lexical_terms": ["另一个词"],
                                   "evidence_requirement":
                                       "verbatim_required"},
                    "expected_revision": 1}, None)
                return out

            monkeypatch.setattr(pipeline, "raw_deep_search", hooked_raw)
            with pytest.raises(Forbidden) as ei:
                _round2(actors, sid)
            assert ei.value.code in ("REVISION_CONFLICT",
                                     "SESSION_STATE_CHANGED")
            with db.recall_runtime() as conn:
                rounds = conn.execute(
                    "SELECT COUNT(*) c FROM recall_rounds WHERE"
                    " session_id=? AND kind='raw'", (sid,)).fetchone()["c"]
                rev = conn.execute(
                    "SELECT current_revision FROM recall_sessions WHERE"
                    " session_id=?", (sid,)).fetchone()["current_revision"]
            assert rounds == 0, "旧 revision 的 raw 轮不得落库"
            assert rev == 2, "refine 的前进保持有效"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


# ---------------------------------------------------------------- CB-010

class TestRawToggleLifecycle:

    def test_continuation_blocked_after_toggle_off(self, actors):
        """审计反例：首页取得游标后关闭 Raw，翻页仍交付原文。

        游标经服务端 store.issue_raw_continuation 签发（翻页入口的
        修复点是 continuation 分支的开关复核，不依赖首页 has_more）。"""
        old = _grant_source_excerpt()
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors)
            sid = r1["data"]["recall_session_id"]
            _seed_source()
            r2 = _round2(actors, sid)
            assert r2["data"]["round"] == 2
            s = store.get_session(sid)
            with db.recall_runtime() as conn:
                token = store.issue_raw_continuation(
                    conn, session_id=sid,
                    revision=s["current_revision"],
                    burst_no=s["current_burst"], next_offset=20)
            assert token
            old_flag = cfg.RECALL_RAW_FALLBACK_ENABLED
            cfg.RECALL_RAW_FALLBACK_ENABLED = False
            try:
                with pytest.raises(Forbidden) as ei:
                    registry.invoke(
                        actors["jiaming"], "memory.recall.round2",
                        {"session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                         "continuation_token": token,
                         "operation_id": "op-cb-cont"}, None)
                assert ei.value.code == "RAW_DISABLED"
            finally:
                cfg.RECALL_RAW_FALLBACK_ENABLED = old_flag
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_replay_blocked_after_raw_toggle_off(self, actors):
        """审计反例：同一旧包经 revalidate_replayed 仍交付 3 张 Raw 卡。"""
        from mariposa.errors import StaleOperation
        old = _grant_source_excerpt()
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors)
            sid = r1["data"]["recall_session_id"]
            _seed_source()
            r2 = _round2(actors, sid)
            saved = r2["data"]
            assert saved.get("round") == 2
            old_flag = cfg.RECALL_RAW_FALLBACK_ENABLED
            cfg.RECALL_RAW_FALLBACK_ENABLED = False
            try:
                with pytest.raises(StaleOperation):
                    recall_service.revalidate_replayed(
                        "memory.recall.round2", saved, None)
            finally:
                cfg.RECALL_RAW_FALLBACK_ENABLED = old_flag
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_replay_blocked_after_words_toggle_off(self, actors):
        """F02（2026-10-03 审计 P1）：words 关闭后，旧 find_words 包
        整体拒绝重放——通道开关对 fresh/continuation/replay 无例外。"""
        from mariposa.errors import StaleOperation
        _seed_word(actors["jiaming"])
        r1 = _start(actors, op="op-f02-words")
        saved = r1["data"]
        assert saved.get("intent") == "find_words"
        assert saved["candidates"], "前置：开启时至少一张 words 卡"
        old_flag = cfg.RECALL_WORDS_ENABLED
        cfg.RECALL_WORDS_ENABLED = False
        try:
            with pytest.raises(StaleOperation):
                recall_service.revalidate_replayed(
                    "memory.recall.start", saved, None)
        finally:
            cfg.RECALL_WORDS_ENABLED = old_flag

    def test_mixed_replay_strips_words_cards_after_toggle_off(self, actors):
        """F02 混合通道：event+words 旧包重放时 words 卡剔除、
        event 卡保留——开关关的是通道，不是整个混合查询。"""
        _seed_word(actors["jiaming"], text="复述：崧蓝染色的话语")
        _hold(actors["jiaming"], "崧蓝染色的事件正文")
        r1 = _start(actors, terms=("崧蓝",), op="op-f02-mix",
                    channels=["event", "words"])
        saved = r1["data"]
        channels_seen = {c.get("channel") for c in saved["candidates"]}
        assert "words" in channels_seen, "前置：混合包里有 words 卡"
        old_flag = cfg.RECALL_WORDS_ENABLED
        cfg.RECALL_WORDS_ENABLED = False
        try:
            out = recall_service.revalidate_replayed(
                "memory.recall.start", saved, None)
            left = {c.get("channel") for c in out["candidates"]}
            assert "words" not in left, "关闭后 words 卡不得出站"
            assert left, "event 卡应保留"
        finally:
            cfg.RECALL_WORDS_ENABLED = old_flag


# ---------------------------------------------------------------- CB-011

class TestRawLease:

    def test_lease_acquire_release_preempt(self):
        """租约语义：独占、释放后可抢、TTL 过期可抢占（异常退出恢复）。"""
        sid, rev, burst = "cb011-s", 3, 2
        t1 = recall_service._acquire_raw_lease(sid, rev, burst)
        assert t1
        assert recall_service._acquire_raw_lease(sid, rev, burst) is None, \
            "未过期租约必须互斥"
        recall_service._release_raw_lease(sid, rev, burst, t1)
        t2 = recall_service._acquire_raw_lease(sid, rev, burst)
        assert t2 and t2 != t1, "释放后可立即再抢"
        # 模拟持有者异常退出：租约超 TTL 后可抢占
        with db.recall_runtime() as conn:
            conn.execute(
                "UPDATE recall_raw_leases SET created_at='2020-01-01T00:00:"
                "00+00:00' WHERE session_id=?", (sid,))
        t3 = recall_service._acquire_raw_lease(sid, rev, burst)
        assert t3, "过期租约可抢占（崩溃恢复）"
        recall_service._release_raw_lease(sid, rev, burst, t3)

    def test_round2_blocked_while_lease_held(self, actors, monkeypatch):
        """租约被占（另一进程在算）→ 本进程拒绝且零昂贵调用。"""
        calls = {"n": 0}
        orig_raw = pipeline.raw_deep_search

        def counting_raw(*a, **kw):
            calls["n"] += 1
            return orig_raw(*a, **kw)

        monkeypatch.setattr(pipeline, "raw_deep_search", counting_raw)
        old = _grant_source_excerpt()
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors)
            sid = r1["data"]["recall_session_id"]
            _seed_source()
            s = store.get_session(sid)
            held = recall_service._acquire_raw_lease(
                sid, s["current_revision"], s["current_burst"])
            assert held
            try:
                with pytest.raises(Forbidden) as ei:
                    _round2(actors, sid)
                assert ei.value.code == "RAW_ROUND_IN_PROGRESS"
                assert calls["n"] == 0, "输家不得执行 Raw 深搜"
            finally:
                recall_service._release_raw_lease(
                    sid, s["current_revision"], s["current_burst"], held)
            # 租约释放后正常通过
            r2 = _round2(actors, sid, op="op-cb-r2b")
            assert r2["data"]["round"] == 2
            assert calls["n"] >= 1
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


# ---------------------------------------------------------------- CB-012

class TestJudgeProofValidity:

    def test_wrong_version_judge_not_counted_complete(self, actors):
        """审计反例：judge 全部 evaluated 但 candidate_version 错——
        回执不得签 judged（Raw gate fail-open）。"""
        from mariposa.retrieval.judges import base as jb

        class WrongVersionJudge(jb.JudgeProvider):
            name = "cb_wrong_version"
            _api_key = "test-key"

            def judge(self, plan, candidates, ctx):
                items = [jb.JudgeItem(
                    candidate_ref=c.get("candidate_ref")
                    or c["resource_ref"],
                    candidate_version="999",  # 恒错版本
                    relevance_signal=0.8,
                    evaluation_status="evaluated", model_id=self.name,
                    prompt_version="t") for c in candidates]
                return jb.JudgeBatchResult(
                    items=items, provider_status="evaluated",
                    degraded_reason=None, cache_hits=0,
                    cache_misses=len(candidates), request_count=1)

        jb.register_for_tests("cb_wrong_version", WrongVersionJudge())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "cb_wrong_version"
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors)
            sid = r1["data"]["recall_session_id"]
            with db.recall_runtime() as conn:
                receipt = store.read_round1_receipt(conn, sid, 1)
            assert receipt is not None
            assert receipt["judged_count"] == 0, \
                "版本错配的判断不得计入 judged"
            assert receipt["unavailable_count"] > 0, \
                "无效判断按 unavailable 入账"
            with pytest.raises(Forbidden):
                _round2(actors, sid)
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_round2_duplicate_judge_ref_marks_unavailable(self, actors):
        """审计反例：Raw provider 对一张卡返回两个相同 ref 仍
        coverage=evaluated——现在标 unavailable。"""
        from mariposa.retrieval.judges import base as jb
        from mariposa.retrieval.judges import typesafe_jev

        class DupRefJudge(typesafe_jev.TypeSafeJevJudge):
            name = "cb_dup_ref"
            _api_key = "test-key"

            def __init__(self):
                super().__init__()
                self._data_profile = frozenset(
                    {"event_excerpt", "title_cue", "word_excerpt",
                     "source_excerpt"})
                self._disabled_reason = None

            def judge(self, plan, candidates, ctx):
                raw_only = [c for c in candidates
                            if c.get("channel") == "raw"]
                if not raw_only:
                    # Round1 正常判断（words 候选），不触发基数违例
                    items = [jb.JudgeItem(
                        candidate_ref=c.get("candidate_ref")
                        or c["resource_ref"],
                        candidate_version=str(
                            c.get("content_version") or ""),
                        relevance_signal=0.8,
                        evaluation_status="evaluated",
                        model_id=self.name, prompt_version="t")
                        for c in candidates]
                    return jb.JudgeBatchResult(
                        items=items, provider_status="evaluated",
                        degraded_reason=None, cache_hits=0,
                        cache_misses=len(candidates), request_count=1)
                # Round2 raw 候选：同一张卡返回两个相同 ref
                ref = (raw_only[0].get("candidate_ref")
                       or raw_only[0]["resource_ref"])
                ver = str(raw_only[0].get("content_version") or "")
                return jb.JudgeBatchResult(
                    items=[
                        jb.JudgeItem(
                            candidate_ref=ref, candidate_version=ver,
                            relevance_signal=0.8,
                            evaluation_status="evaluated",
                            model_id=self.name, prompt_version="t"),
                        jb.JudgeItem(
                            candidate_ref=ref, candidate_version=ver,
                            relevance_signal=0.7,
                            evaluation_status="evaluated",
                            model_id=self.name, prompt_version="t")],
                    provider_status="evaluated", degraded_reason=None,
                    cache_hits=0, cache_misses=len(candidates),
                    request_count=1)

        jb.register_for_tests("cb_dup_ref", DupRefJudge())
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = "cb_dup_ref"
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors)
            sid = r1["data"]["recall_session_id"]
            _seed_source()
            r2 = _round2(actors, sid)
            cov = r2["data"]["coverage"]
            assert cov.get("judge") == "unavailable", \
                "重复 ref 的判断集合不得签 evaluated"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


# ---------------------------------------------------------------- CB-013

class TestWordsBrowseCoverage:

    def test_truncated_words_window_reports_partial(self, actors):
        """审计反例：25 个可见 words 只送判 20 仍签
        complete_within_scope 且 Raw gate=true。"""
        words = [{"speaker": "qiaosheng",
                  "text": f"第{i}句：崧蓝染色的话语",
                  "expression_kind": "paraphrase"} for i in range(25)]
        _hold(actors["jiaming"], "二十五句话语的正文", our_words=words)
        r1 = _start(actors, terms=(), op="op-cb013",
                    exact_phrases=["崧蓝染色"])
        data = r1["data"]
        cov = data["coverage"]
        assert cov.get("words_lexical") == "partial_topk_window", \
            "截断浏览窗口必须如实报 partial"
        sid = data["recall_session_id"]
        with db.recall_runtime() as conn:
            receipt = store.read_round1_receipt(conn, sid, 1)
        assert receipt["unjudged_count"] > 0, \
            "窗口外的 words 不得从 unjudged 凭空消失"
        with pytest.raises(Forbidden):
            _round2(actors, sid)


# ---------------------------------------------------------------- CB-014

class TestReplayGuards:

    def test_replay_rejected_after_refine(self, actors):
        """审计反例：refine 换词后旧 start 包重放仍交旧候选并标新
        revision——现在查询指纹失配拒绝重放。"""
        from mariposa.errors import StaleOperation
        _seed_word(actors["jiaming"])
        r1 = _start(actors, op="op-cb014a")
        saved = r1["data"]
        sid = saved["recall_session_id"]
        assert saved["candidates"] or saved.get("coverage"), "前置"
        registry.invoke(actors["jiaming"], "memory.recall.refine", {
            "session_id": sid,
            "query_plan": {"original_request": "换了查询",
                           "channels": ["words"],
                           "lexical_terms": ["新词"],
                           "evidence_requirement": "verbatim_required"},
            "expected_revision": 1, "operation_id": "op-cb014f"}, None)
        with pytest.raises(StaleOperation):
            recall_service.revalidate_replayed("memory.recall.start",
                                               saved, None)

    def test_replay_same_plan_still_allowed(self, actors):
        """正路径：plan 未变的重放不被指纹误伤。"""
        _seed_word(actors["jiaming"])
        r1 = _start(actors, op="op-cb014b")
        saved = r1["data"]
        out = recall_service.revalidate_replayed("memory.recall.start",
                                                 saved, None)
        assert out is not None

    def test_round2_replay_rejects_rejected_source(self, actors):
        """审计反例：Raw 交付 source_msg 后 reject，重放仍返同卡。"""
        old = _grant_source_excerpt()
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors)
            sid = r1["data"]["recall_session_id"]
            _seed_source()
            r2 = _round2(actors, sid, op="op-cb014r")
            saved = r2["data"]
            refs = [c["resource_ref"] for c in saved["candidates"]]
            assert any(r.startswith("source_msg:") for r in refs), \
                "前置：round2 交付了 source 卡"
            registry.invoke(actors["jiaming"], "memory.recall.reject", {
                "session_id": sid,
                "candidate_ref": next(c["candidate_ref"]
                                      for c in saved["candidates"]
                                      if c["resource_ref"].startswith(
                                          "source_msg:")),
                "reject_target": "source_selection",
                "operation_id": "op-cb014rej"}, None)
            replayed = recall_service.revalidate_replayed(
                "memory.recall.round2", saved, None)
            left = [c["resource_ref"] for c in replayed["candidates"]]
            rejected_refs = store.rejected_resource_refs(sid)
            assert not [r for r in left if r in rejected_refs], \
                "已拒绝的 source 不得经重放出站"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old


# ---------------------------------------------------------------- CB-015

class TestExactPhraseConstraint:

    def test_round2_exact_only_phrase_filters_raw(self, actors):
        """审计反例：exact_phrases=['UNMATCHABLE_PHRASE'] 且 terms=[]
        时 Raw 仍把无关 source 送判并交付——现在 AND 约束零命中。"""
        old = _grant_source_excerpt()
        try:
            _seed_word(actors["jiaming"])
            r1 = _start(actors, terms=(), op="op-cb015",
                        exact_phrases=["UNMATCHABLE_PHRASE"])
            sid = r1["data"]["recall_session_id"]
            _seed_source(text="完全无关的日常原文")
            # exact-only Round1 零交付 → 升级理由是 NO_DELIVERABLE_CANDIDATE
            r2 = _round2(actors, sid, reason="NO_DELIVERABLE_CANDIDATE",
                         op="op-cb015r")
            packet = r2["data"]
            assert packet["candidates"] == [], \
                "exact-only 查询不得交付不满足逐字约束的原文"
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old
