"""r2 S09 成对反例：中秋+约会（03 验收点名）。

验证 payload 角色而非字符串比较：
- 标题命中候选的 Jev 输入同时保留"真实命中线索"（title_cue/
  match_evidence）与"事件事实"（event_evidence）——不是二选一；
- 标题不强迫通过（判断输入完整，反候选可被排低——由角色结构
  保证，非自动阈值）；
- 同一 event 段双标 match+event 不复制；
- CORE 候选不夹带 title/words 段。
"""
from __future__ import annotations

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa.memory import service as memory
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


class PayloadCapture(jb.JudgeProvider):
    name = "payload_capture"

    def __init__(self):
        self.payloads: list[dict] = []

    def judge(self, plan, candidates, ctx):
        # 用真实 typesafe 投影组装 payload（不外发）
        inner = typesafe_jev.TypeSafeJevJudge()
        inner._data_profile = frozenset(
            {"event_excerpt", "title_cue", "word_excerpt",
             "source_excerpt", "structured_metadata"})
        inner._current_terms = plan.get("lexical_terms") or []
        self.payloads.append(inner._payload(plan, candidates))
        items = [jb.JudgeItem(
            candidate_ref=c.get("candidate_ref") or c["resource_ref"],
            candidate_version=c.get("content_version"),
            relevance_signal=0.8,
            evaluation_status="evaluated", model_id="capture",
            prompt_version="t") for c in candidates]
        return jb.JudgeBatchResult(items=items,
                                   provider_status="evaluated")


def _hold(principal, text, title, date):
    return memory.hold(principal, text=text, memory_date=date,
                       date_confidence="exact", original_title=title,
                       categories=["date"],
                       creation_mode="contemporaneous", raw_pending=False)


class TestMidAutumnPair:
    def test_payload_roles_distinguish_same_title(self, actors):
        """同标题、不同正文的两候选：Jev 输入同含 title cue 与
        event 事实，正文差异可见——判断依据完整。"""
        cap = PayloadCapture()
        jb.register_for_tests(cap.name, cap)
        from mariposa import config as cfg
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            pos = _hold(actors["jiaming"], "我们一起出去约会了。", "中秋",
                        "2026-09-29")
            neg = _hold(actors["jiaming"], "那天我一个人在公司加班。",
                        "中秋", "2026-09-29")
            from mariposa.capabilities import registry as reg
            reg.invoke(actors["jiaming"], "memory.recall.start",
                       { "operation_id": "op-auto-test_c-2","query_plan": {
                           "original_request": "前天中秋我们一起约会你还记得不",
                           "channels": ["event"],
                           "lexical_terms": ["中秋", "约会"]}}, None)
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old
        assert cap.payloads, "前置：judge 被真实调用"
        cands = cap.payloads[-1]["state"]["candidates"]
        by_mid = {}
        for c in cands:
            by_mid[c["metadata"]["memory_id"]] = c
        assert pos["memory_id"] in by_mid and neg["memory_id"] in by_mid, \
            "前置：两候选均进入 judge 输入"
        for mid, tag in ((pos["memory_id"], "pos"), (neg["memory_id"], "neg")):
            segs = by_mid[mid]["segments"]
            title_segs = [s for s in segs if s["field"] == "original_title"]
            ev_segs = [s for s in segs if s["field"] == "event_text"]
            assert title_segs and "中秋" in title_segs[0]["text"], \
                f"{tag}：标题命中保留真实 cue（不丢弃）"
            assert "title_cue" in title_segs[0]["roles"] and \
                "match_evidence" in title_segs[0]["roles"]
            assert ev_segs, f"{tag}：事件事实主体必须在场"
            assert "event_evidence" in ev_segs[0]["roles"]
        # 正文差异对 Jev 可见（这是 r2 的全部目的）
        pos_ev = [s for s in by_mid[pos["memory_id"]]["segments"]
                  if s["field"] == "event_text"][0]["text"]
        neg_ev = [s for s in by_mid[neg["memory_id"]]["segments"]
                  if s["field"] == "event_text"][0]["text"]
        pos_c = pos_ev.replace(" ", "")
        neg_c = neg_ev.replace(" ", "")
        assert "约会" in pos_c and "加班" in neg_c, \
            "正文差异对 Jev 可见（分词文本按去空格比较）"
        # prompt 角色契约在场
        contract = cap.payloads[-1]["state"]["candidate_role_contract"]
        assert "title_cue" in contract and "event_evidence" in contract
        task = cap.payloads[-1]["questions"]["candidate_0_relevance"] \
            if "candidate_0_relevance" in cap.payloads[-1]["questions"] \
            else cap.payloads[-1]["questions"]["candidate_0"]
        assert "title_cue" in task["instructions"]["task"]

    def test_event_hit_segment_dual_role_no_copy(self, actors):
        """event 自身命中：同一正文段双标 match+event，不复制两段。"""
        cap = PayloadCapture()
        jb.register_for_tests(cap.name, cap)
        from mariposa import config as cfg
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            m = _hold(actors["jiaming"], "我们在河边放了一只纸鸢", "纸鸢",
                      "2026-09-28")
            from mariposa.capabilities import registry as reg
            reg.invoke(actors["jiaming"], "memory.recall.start",
                       { "operation_id": "op-auto-test_c-1","query_plan": {
                           "original_request": "找纸鸢",
                           "channels": ["event"],
                           "lexical_terms": ["纸鸢"]}}, None)
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old
        cands = {c["metadata"]["memory_id"]: c
                 for c in cap.payloads[-1]["state"]["candidates"]}
        segs = cands[m["memory_id"]]["segments"]
        ev_segs = [s for s in segs if s["field"] == "event_text"]
        assert len(ev_segs) == 1, "同段双标，不复制"
        assert set(ev_segs[0]["roles"]) >= {"match_evidence",
                                            "event_evidence"}

    def test_core_candidate_carries_no_title_segment(self, actors):
        """CORE：matched_fields 不含 original_title（检索层阶段过滤），
        payload 不附 title/words 段——只有 event 事实。"""
        cap = PayloadCapture()
        jb.register_for_tests(cap.name, cap)
        from mariposa import config as cfg
        old = cfg.RECALL_JUDGE_PROVIDER
        cfg.RECALL_JUDGE_PROVIDER = cap.name
        try:
            m = _hold(actors["jiaming"], "尘封多年的青苔井栏正文", "青苔井栏",
                      "2026-09-27")
            with db.formal() as conn:
                conn.execute(
                    "UPDATE memories SET held_at='2026-01-01T00:00:00+00:00'"
                    " WHERE memory_id=?", (m["memory_id"],))
            from mariposa.capabilities import registry as reg
            reg.invoke(actors["jiaming"], "memory.recall.start",
                       { "operation_id": "op-auto-test_c-0","query_plan": {
                           "original_request": "找青苔",
                           "channels": ["event"],
                           "lexical_terms": ["青苔"]}}, None)
        finally:
            cfg.RECALL_JUDGE_PROVIDER = old
        cands = {c["metadata"]["memory_id"]: c
                 for c in cap.payloads[-1]["state"]["candidates"]}
        assert m["memory_id"] in cands, "前置：CORE 正文命中在场"
        segs = cands[m["memory_id"]]["segments"]
        assert not [s for s in segs if s["field"] == "original_title"], \
            "CORE 不夹带标题段"
        assert not [s for s in segs if s["field"] == "our_words"]
