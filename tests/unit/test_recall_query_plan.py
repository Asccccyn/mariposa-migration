"""查询分层与编译安全（QUERY-01..03 / HYBRID-04 / HYBRID-09）。

推测只作软提示，永不进硬过滤；用户明确条件可硬过滤（白名单内）；
alternate query 不新增事实；FTS 编译对恶意输入安全。
"""
from __future__ import annotations

import pytest

from mariposa.identity import service as identity
from mariposa.memory import service as memory
from mariposa.recall import service as recall_service
from mariposa.retrieval import query_plan as qp
from mariposa.errors import Forbidden
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": identity.Principal("jiaming", "周家明", "agent",
                                          "claude_chat", "bj")}


def hold(actors, text, date, cats=("daily",)):
    return memory.hold(actors["jiaming"], text=text, memory_date=date,
                       date_confidence="exact", original_title="t",
                       categories=list(cats),
                       creation_mode="contemporaneous", raw_pending=False)


class TestQueryLayering:
    def test_query01_inferred_hint_not_hard_filter(self, actors):
        """QUERY-01：推测 plan 只作软提示，普通事件仍能进候选。"""
        hold(actors, "普通日常事件没有计划字样", "2026-08-10")
        packet = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "找那件事",
            "channels": ["event"],
            "lexical_terms": ["普通"],
            "inferred_hints": ["plan"]}})
        assert packet["candidates"]
        assert packet["coverage"]["event"] == "complete_within_scope"
        # inferred_hints 不改变明确条件（未变成 category=plan 硬过滤）
        plan_used = packet.get("query_plan_applied")  # 不透传也行——验证行为
        assert plan_used is None or "plan" not in str(
            packet["coverage"].get("event"))

    def test_query02_explicit_constraints_hard_filter(self, actors):
        """QUERY-02：明确日期+分类 → 合法硬过滤。"""
        hold(actors, "八月约会事件", "2026-08-19", cats=("date",))
        hold(actors, "九月约会事件", "2026-09-19", cats=("date",))
        hold(actors, "八月日常事件", "2026-08-20", cats=("daily",))
        packet = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "八月份的约会",
            "channels": ["event"],
            "lexical_terms": ["约会"],
            "explicit_constraints": {
                "event_date": {"from": "2026-08-01", "to": "2026-08-31"},
                "categories": ["date"]}}})
        dates = [c["memory_date"] for c in packet["candidates"]]
        assert dates and all(d.startswith("2026-08") for d in dates)

    def test_query02_negative_constraint_excludes(self, actors):
        """QUERY-02（负向）：'不是九月那次' 形成硬排除。"""
        hold(actors, "八月事件", "2026-08-10")
        hold(actors, "九月事件", "2026-09-10")
        packet = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "那件事，不是九月",
            "channels": ["event"],
            "lexical_terms": ["事件"],
            "explicit_negative_constraints": {
                "event_date_excluded": [
                    {"from": "2026-09-01", "to": "2026-09-30"}]}}})
        dates = [c["memory_date"] for c in packet["candidates"]]
        assert all(not d.startswith("2026-09") for d in dates)

    def test_query03_alternate_adds_no_facts(self, actors):
        """QUERY-03：alternate_queries 不新增项目名等未确认事实。"""
        hold(actors, "搬家事件属于日常", "2026-08-10")
        packet = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "找那件事",
            "channels": ["event"],
            "lexical_terms": ["搬家"],
            "alternate_queries": ["Ombre 项目搬家"]}})
        # alternate 只作为同义检索表达：不会硬写"Ombre"进过滤，也不排除
        # 其他候选（服务端仅存储 alternate，不据此硬过滤）
        assert packet["candidates"]

    def test_unknown_filter_field_rejected(self, actors):
        """§4.2：白名单外过滤字段必须可诊断错误，不静默忽略。"""
        with pytest.raises(Forbidden) as ei:
            recall_service.start(actors["jiaming"], {"query_plan": {
                "original_request": "x", "channels": ["event"],
                "lexical_terms": ["x"],
                "explicit_constraints": {"project": "Ombre"}}})
        assert ei.value.code == "INVALID_ARGUMENT"

    def test_conflicting_explicit_conditions_rejected(self, actors):
        """§4.2：两个明确条件互相冲突 → CONFLICT，不自动挑一个。"""
        with pytest.raises(Forbidden) as ei:
            recall_service.start(actors["jiaming"], {"query_plan": {
                "original_request": "x", "channels": ["event"],
                "lexical_terms": ["x"],
                "explicit_constraints": {
                    "event_date": {"from": "2026-09-01",
                                   "to": "2026-09-30"}},
                "explicit_negative_constraints": {
                    "event_date_excluded": [
                        {"from": "2026-09-01", "to": "2026-09-30"}]}}})
        assert ei.value.code == "CONFLICT"


class TestCompilationSafety:
    def test_hybrid04_fts_injection_inert(self):
        """HYBRID-04：恶意 FTS 输入不变查询语法。"""
        evil = '搬家" OR 1=1 -- NEAR(a b) * ^ :'
        compiled = qp.compile_terms([evil])
        # 恒真断言修正（审计 2026-10-03）：or True 让本行永不失败
        assert 'OR 1=1' not in compiled.replace('" OR "', '§')
        # 所有 token 都被引号包裹成 phrase，无裸露操作符
        import re
        assert not re.search(r'(?<!")\b(OR|AND|NOT|NEAR)\b(?!")',
                             compiled.replace('" OR "', ''))

    def test_hybrid04_terms_noncontiguous_vs_phrase_order(self, actors):
        """HYBRID-04：terms 可非连续命中；exact phrase 保持顺序。"""
        hold(actors, "先说窗帘搬家这件事，再谈安排", "2026-08-10")
        # terms 非连续：两个词都出现但不相邻
        p1 = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "窗帘 搬家", "channels": ["event"],
            "lexical_terms": ["窗帘", "搬家"]}})
        # phrase 顺序敏感："窗帘搬家"连续出现才算
        p2 = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "窗帘搬家连着说", "channels": ["event"],
            "exact_phrases": ["窗帘搬家"]}})
        assert p1["candidates"]
        assert p2["candidates"]

    def test_hybrid09_empty_query_browses(self, actors):
        """HYBRID-09：空 query+分类=浏览，不构造随机/空语义向量。"""
        hold(actors, "事件甲", "2026-08-10", cats=("daily",))
        hold(actors, "事件乙", "2026-08-11", cats=("sweet",))
        packet = recall_service.start(actors["jiaming"], {"query_plan": {
            "original_request": "翻翻日常",
            "channels": ["event"],
            "explicit_constraints": {"categories": ["daily"]}}})
        assert packet["candidates"]
        assert packet["coverage"].get("dense_event") == "not_requested"
