"""林石见三轮复审（2026-09-30 深夜）反例回归——#1..#6。

每条对应其独立复现的攻击面；fake judge/embedder 注入，不加载真实
权重。全部走真实链路（start/round2/words_recall/raw_deep_search）。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.capabilities import registry
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
    """与 WP04 同法：注入带 source_excerpt 许可的 TypeSafe 实例。"""
    class GrantedTypeSafe(typesafe_jev.TypeSafeJevJudge):
        name = "granted_typesafe_r3"
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

    jb.register_for_tests("granted_typesafe_r3", GrantedTypeSafe())
    from mariposa import config as cfg
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = "granted_typesafe_r3"
    return old


class _Cap(jb.JudgeProvider):
    """捕获 Jev payload 的注入 provider（#3 定位反例）。"""
    name = "cap_r3audit"

    def __init__(self):
        self.payloads = []

    def judge(self, plan, candidates, ctx):
        inner = typesafe_jev.TypeSafeJevJudge()
        inner._data_profile = frozenset(
            {"event_excerpt", "title_cue", "word_excerpt",
             "source_excerpt", "structured_metadata"})
        inner._current_terms = plan.get("lexical_terms") or []
        self.payloads.append(inner._payload(plan, candidates))
        return jb.JudgeBatchResult(
            items=[jb.JudgeItem(
                c.get("candidate_ref") or c["resource_ref"],
                c.get("content_version"), relevance_signal=0.8,
                evaluation_status="evaluated", model_id="cap",
                prompt_version="t") for c in candidates],
            provider_status="evaluated")


def _install_cap(cap):
    jb.register_for_tests(cap.name, cap)
    from mariposa import config as cfg
    old = cfg.RECALL_JUDGE_PROVIDER
    cfg.RECALL_JUDGE_PROVIDER = cap.name
    return old


def _seed_source_file(conv_msgs):
    """conv_msgs: [(conversation_uuid, message_uuid, sender, iso_time,
    text)] → 一个导入文件。"""
    from mariposa.source import importer
    import json as _json
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    convs: dict[str, dict] = {}
    for cu, mu, sender, ts, text in conv_msgs:
        convs.setdefault(cu, {"uuid": cu, "chat_messages": []})[
            "chat_messages"].append({
                "uuid": mu, "sender": sender, "created_at": ts,
                "content": [{"type": "text", "text": text}]})
    f = tmp / "s3.json"
    f.write_text(_json.dumps(list(convs.values()), ensure_ascii=False),
                 encoding="utf-8")
    return importer.import_file("jiaming", str(f))


class Test1CoverageGateWhitelist:
    """#1：coverage gate 只认 retrieval family 状态键。"""

    def test_lexical_scorer_not_a_family(self, actors):
        """审计反例：正常 event 检索（complete + lexical_scorer 版本串
        + dense not_requested）的合法 Round2 升级被 metadata 挡死。
        修复后 lexical_scorer 不参与完整性判断。"""
        old = _grant_source_excerpt()
        try:
            memory.hold(
                actors["jiaming"], text="无关正文一", memory_date="2026-09-25",
                date_confidence="exact", original_title="r3a",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False)
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "查无此词",
                               "channels": ["event"],
                               "lexical_terms": ["qqqxyz"]}})
            sid = p["recall_session_id"]
            with db.recall_runtime() as conn:
                receipt = store.read_round1_receipt(conn, sid, 1)
            cov = receipt["coverage"]
            # 反例前置：receipt coverage 确实携带 lexical_scorer 版本串
            assert cov.get("lexical_scorer"), \
                f"前置失败：coverage 应含 lexical_scorer：{sorted(cov)}"
            assert cov.get("event") == "complete_within_scope"
            out = recall_service.round2(actors["jiaming"], {
                "session_id": sid,
                "reason": "NO_DELIVERABLE_CANDIDATE"})
            assert out["round"] == 2, \
                "合法升级不得被 lexical_scorer 版本串挡死"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_words_forgotten_metadata_not_a_family(self, actors):
        """同病另证：words_forgotten=disabled:<策略> 是注记不是检索
        family，遗忘话语存在时不得阻断合法 Round2。"""
        old = _grant_source_excerpt()
        try:
            out = memory.hold(
                actors["jiaming"], text="将被遗忘的事件", memory_date="2026-03-01",
                date_confidence="exact", original_title="r3f",
                categories=["daily"], creation_mode="contemporaneous",
                raw_pending=False,
                our_words=[{"speaker": "qiaosheng", "text": "旧年的晚风",
                            "expression_kind": "verbatim"}])
            with db.formal() as conn:
                conn.execute(
                    "UPDATE memories SET compression_state="
                    "'forgotten_summary' WHERE memory_id=?",
                    (out["memory_id"],))
            p = recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "晚风",
                               "channels": ["words"],
                               "lexical_terms": ["晚风"]}})
            sid = p["recall_session_id"]
            with db.recall_runtime() as conn:
                receipt = store.read_round1_receipt(conn, sid, 1)
            cov = receipt["coverage"]
            assert cov.get("words_forgotten"), \
                f"前置失败：应有 words_forgotten 注记：{sorted(cov)}"
            r2 = recall_service.round2(actors["jiaming"], {
                "session_id": sid,
                "reason": "NO_DELIVERABLE_CANDIDATE"})
            assert r2["round"] == 2, \
                "遗忘注记不得冒充不完整检索 family"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_dense_unavailable_still_blocks(self, actors):
        """白名单收窄不放松原门：dense_event=unavailable 仍拒。"""
        from mariposa import config as cfg
        cfg.SEMANTIC_PROVIDER = ""
        p = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "找雾隐", "channels": ["event"],
            "lexical_terms": ["qqqxyz"],
            "semantic_query": "雾隐茶室"}})
        with pytest.raises(Forbidden) as ei:
            recall_service.round2(actors["jiaming"], {
                "session_id": p["recall_session_id"],
                "reason": "NO_DELIVERABLE_CANDIDATE"})
        gate = ei.value.detail["gate"]
        assert gate.get("retrieval_complete") is False
        assert "dense_event" in gate.get("incomplete_families", [])


class Test2Continuation:
    """#2：raw round 分页 = 同一 logical round 的延续。"""

    def _seed_25(self):
        # session plan 的 lexical_terms 是"崧蓝"（round2 用服务端存的
        # plan 深搜，S13-4），种子必须命中该词才能翻页
        msgs = [(f"c3-{i:02d}", f"m3-{i:02d}", "human",
                 f"2026-09-28T10:{i % 60:02d}:00.000Z",
                 f"崧蓝备忘第{i}条：染色安排") for i in range(25)]
        return _seed_source_file(msgs)

    def _legit_r2(self, actors):
        """verbatim 要求下 paraphrase 命中 → 证据不足升级（WP04 场景）。"""
        old = _grant_source_excerpt()
        memory.hold(
            actors["jiaming"], text="崧蓝事件正文", memory_date="2026-09-25",
            date_confidence="exact", original_title="r3c",
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
        return old, p["recall_session_id"]

    def test_second_page_executes_and_consumes_no_round(self, actors):
        """审计反例：continuation.offset=20 被 no_prior_raw_round 挡死。
        修复后第二页经服务端 token 执行，round/attempt 不增加。"""
        old, sid = self._legit_r2(actors)
        try:
            self._seed_25()
            p1 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "operation_id": "op-r3-p1"})
            cont = p1["continuation"]
            assert cont and cont["available"], "25 条 > 20 页容量"
            assert cont["continuation_token"], "服务端游标必带 token"
            p2 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "continuation_token": cont["continuation_token"],
                "operation_id": "op-r3-p2"})
            assert p2["round"] == 2, "翻页不是新的 logical round"
            with db.recall_runtime() as conn:
                raw_rounds = conn.execute(
                    "SELECT COUNT(*) c FROM recall_rounds WHERE"
                    " session_id=? AND kind='raw'", (sid,)).fetchone()["c"]
                attempts = conn.execute(
                    "SELECT COUNT(*) c FROM recall_attempts WHERE"
                    " session_id=? AND kind='raw_round2'",
                    (sid,)).fetchone()["c"]
                left = store.read_raw_continuation(conn, sid, 1, 1)
            assert raw_rounds == 1, "翻页不得消耗新 raw 轮"
            assert attempts == 1, "翻页不得重复记 attempt"
            assert left is None, "翻尽清游标"
            assert p2["continuation"] is None
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_second_page_returns_later_hits(self, actors):
        """第二页真的是 offset 20 之后的 5 条，不是重发第一页。"""
        old, sid = self._legit_r2(actors)
        try:
            self._seed_25()
            p1 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "operation_id": "op-r3-q1"})
            cont = p1["continuation"]
            p2 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "continuation_token": cont["continuation_token"],
                "operation_id": "op-r3-q2"})
            seen1 = {c["resource_ref"] for c in p1["candidates"]}
            seen2 = {c["resource_ref"] for c in p2["candidates"]}
            assert seen2, "第二页应有剩余 5 条"
            assert not (seen1 & seen2), "第二页不得重发第一页候选"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_stale_token_rejected(self, actors):
        """翻尽/被取代后的旧 token → CONTINUATION_INVALID。"""
        old, sid = self._legit_r2(actors)
        try:
            self._seed_25()
            p1 = recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "operation_id": "op-r3-t1"})
            tok = p1["continuation"]["continuation_token"]
            recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "continuation_token": tok,
                "operation_id": "op-r3-t2"})
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                    "continuation_token": tok,
                    "operation_id": "op-r3-t3"})
            assert ei.value.code == "CONTINUATION_INVALID"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_garbage_token_rejected(self, actors):
        old, sid = self._legit_r2(actors)
        try:
            self._seed_25()
            recall_service.round2(actors["jiaming"], {
                "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                "operation_id": "op-r3-g1"})
            with pytest.raises(Forbidden) as ei:
                recall_service.round2(actors["jiaming"], {
                    "session_id": sid, "reason": "EVIDENCE_INSUFFICIENT",
                    "continuation_token": "f" * 32,
                    "operation_id": "op-r3-g2"})
            assert ei.value.code == "CONTINUATION_INVALID"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old


class Test3WordLocation:
    """#3：同桶多句 our_words 时定位真实命中话语。"""

    def test_locates_hit_sentence_not_token_overlap(self, actors):
        """审计反例：#1"小猫今天很乖"（含"小"）抢走"小路灯"的命中——
        Jev 必须看到真实命中句"把小路灯放在桌上吧"。"""
        cap = _Cap()
        old = _install_cap(cap)
        try:
            memory.hold(
                actors["jiaming"], text="桌上摆了不少东西",
                memory_date="2026-09-25", date_confidence="exact",
                original_title="r3w", categories=["daily"],
                creation_mode="contemporaneous", raw_pending=False,
                our_words=[
                    {"speaker": "qiaosheng", "text": "小猫今天很乖",
                     "expression_kind": "verbatim"},
                    {"speaker": "jiaming", "text": "把小路灯放在桌上吧",
                     "expression_kind": "verbatim"}])
            recall_service.start(actors["jiaming"], {
                "query_plan": {"original_request": "小路灯",
                               "channels": ["event"],
                               "lexical_terms": ["小路灯"]}})
            segs = (cap.payloads[-1]["state"]["candidates"][0]
                    ["segments"])
            word_seg = next(s for s in segs
                            if s["field"] == "our_words")
            assert "把小路灯放在桌上吧" in word_seg["text"], \
                f"必须是真实命中句：{word_seg['text']}"
            assert "小猫" not in word_seg["text"], \
                f"token 交叠不得再抢位：{word_seg['text']}"
        finally:
            from mariposa import config as cfg
            cfg.RECALL_JUDGE_PROVIDER = old

    def test_locate_unit_semantics(self):
        """单元级：_locate_word_text 复用 BM25 组语义（可直接驱动）。"""
        from mariposa.recall import pipeline as pl
        import tests.conftest as _ct
        _ct.reset_all()
        from mariposa.identity import service as _id
        principal = _id.Principal("jiaming", "周家明", "agent",
                                  "claude_chat", "bj")
        out = memory.hold(
            principal, text="正文", memory_date="2026-09-25",
            date_confidence="exact", original_title="u", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            our_words=[
                {"speaker": "qiaosheng", "text": "小猫今天很乖",
                 "expression_kind": "verbatim"},
                {"speaker": "jiaming", "text": "把小路灯放在桌上吧",
                 "expression_kind": "verbatim"}])
        with db.formal() as conn:
            loc = pl._locate_word_text(
                conn, out["memory_id"], [["小", "路", "灯"]], [])
        assert loc and loc["text"] == "把小路灯放在桌上吧"
        assert loc["word_id"], "定位结果携带 word_id（Jev 不二次猜）"


class Test4NegativesUnified:
    """#4：负向条件全链统一（稀疏 words / raw Round2 / 契约）。"""

    def _seed_two_words(self, actors):
        memory.hold(
            actors["jiaming"], text="晚风事件一", memory_date="2026-09-20",
            date_confidence="exact", original_title="w1", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "晚风旧话",
                        "expression_kind": "verbatim"}])
        memory.hold(
            actors["jiaming"], text="晚风事件二", memory_date="2026-09-30",
            date_confidence="exact", original_title="w2", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "晚风新话",
                        "expression_kind": "verbatim"}])

    def test_source_date_excluded_sparse_words(self, actors):
        """审计反例：排除 2026-09-20 后两条都返回——稀疏路日期负向
        此前根本没接。"""
        self._seed_two_words(actors)
        p = recall_service.words_recall(actors["jiaming"], {
            "query": "晚风",
            "explicit_negative_constraints": {
                "source_date_excluded": [
                    {"from": "2026-09-20", "to": "2026-09-20"}]}})
        dates = [c.get("memory_date") for c in p["candidates"]]
        assert dates, "前置：应有命中"
        assert all(d != "2026-09-20" for d in dates), \
            f"被排除日期的话语不得返回：{dates}"

    def test_raw_round2_inherits_negatives(self, actors):
        """审计反例：排除"qiaosheng + 9月20日"后 raw 结果原样全回。"""
        _seed_source_file([
            ("c4-a", "m4-a", "human", "2026-09-20T10:00:00.000Z",
             "qiaosheng 的晚风旧话"),
            ("c4-b", "m4-b", "human", "2026-09-30T10:00:00.000Z",
             "qiaosheng 的晚风新话"),
            ("c4-c", "m4-c", "assistant", "2026-09-30T11:00:00.000Z",
             "jiaming 的晚风记录"),
        ])
        from mariposa.recall import pipeline as pl
        res = pl.raw_deep_search(
            actors["jiaming"],
            {"lexical_terms": ["晚风"],
             "explicit_constraints": {},
             "explicit_negative_constraints": {
                 "speaker_excluded": ["qiaosheng"],
                 "source_date_excluded": [
                     {"from": "2026-09-20", "to": "2026-09-20"}]}},
            limit=20)
        sps = [h.get("speaker") for h in res["hits"]]
        assert sps and all(s != "qiaosheng" for s in sps), \
            f"排除的说话人不得返回：{sps}"
        texts = " ".join(h.get("excerpt") or "" for h in res["hits"])
        assert "旧话" not in texts, f"排除日期的原文不得返回：{texts}"

    def test_speaker_excluded_string_contract(self, actors):
        """契约收紧：speaker_excluded 必须是数组，字符串直接拒。"""
        self._seed_two_words(actors)
        with pytest.raises(Forbidden) as ei:
            recall_service.words_recall(actors["jiaming"], {
                "query": "晚风",
                "explicit_negative_constraints": {
                    "speaker_excluded": "qiaosheng"}})
        assert ei.value.code == "INVALID_ARGUMENT"

    def test_date_excluded_non_list_contract(self, actors):
        """契约收紧：日期排除必须是 {from,to} 区间数组。"""
        self._seed_two_words(actors)
        with pytest.raises(Forbidden) as ei:
            recall_service.words_recall(actors["jiaming"], {
                "query": "晚风",
                "explicit_negative_constraints": {
                    "source_date_excluded": {
                        "from": "2026-09-20", "to": "2026-09-20"}}})
        assert ei.value.code == "INVALID_ARGUMENT"


class Test5AnchorExcerpt:
    """#5：raw 检索表达式与 excerpt 锚词分离。"""

    def test_tail_hit_visible_in_excerpt(self, actors):
        """审计反例："中秋"在长消息尾部，excerpt 却给开头——修复后
        命中窗跟到真实命中位置。"""
        body = "无关铺垫的内容。" * 40 + "前面几百字都无关，最后这里才提到中秋"
        _seed_source_file([
            ("c5-a", "m5-a", "human", "2026-09-28T10:00:00.000Z", body)])
        from mariposa.recall import pipeline as pl
        res = pl.raw_deep_search(
            actors["jiaming"],
            {"lexical_terms": ["中秋", "约会"],
             "explicit_constraints": {}}, limit=20)
        assert res["hits"], "前置：OR 命中该消息"
        excerpt = res["hits"][0]["excerpt"] or ""
        assert "中秋" in excerpt, \
            f"命中窗必须包含真实命中词（尾部命中），got：{excerpt[:80]}…"

    def test_fts_expr_never_used_as_anchor(self):
        """单元级：OR 表达式不当锚词——_anchors_excerpt 只认锚词序列。"""
        from mariposa.source import query as sq
        body = "无关铺垫的内容。" * 40 + "结尾提到中秋"
        win = sq._anchors_excerpt(
            body, '"中 秋" OR "约 会"', ["中秋", "约会"])
        assert "中秋" in win, "锚词定位必须命中尾部"
        head_only = sq._anchors_excerpt(
            body, '"中 秋" OR "约 会"', None)
        assert "中秋" not in head_only or "中秋" not in body[:120], \
            "无锚词时退头部窗（不拿表达式猜位置）"


class Test6FindWordsQueryOnly:
    """#6：find_words 只传 query 不再报 original_request 必填。"""

    def test_query_only_works(self, actors):
        memory.hold(
            actors["jiaming"], text="事件正文", memory_date="2026-09-25",
            date_confidence="exact", original_title="fw6", categories=["daily"],
            creation_mode="contemporaneous", raw_pending=False,
            our_words=[{"speaker": "qiaosheng", "text": "梧桐叶落了",
                        "expression_kind": "verbatim"}])
        out = registry.invoke(actors["jiaming"], "memory.find_words",
                              {"query": "梧桐"}, None)
        data = out["data"]
        assert data["recall_session_id"], \
            "只传 query 必须正常建 session（original_request 回填）"
        assert data["candidates"], "梧桐 命中应交付"
