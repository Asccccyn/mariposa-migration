"""复审 R13/R14/R15（2026-10-07）回归：schema 真实覆盖与合法参数、
跨 profile 快照门、钳制溢出与游标字段裸异常。

- R13：memory.relations.list direction=both 合法（handler 支持且默认）；
  memory.by_tag namespace 缺省合法（handler 默认 tag）；blocked/reserved
  能力占位 schema 放行形状、handler 的 blocked 响应仍权威
- R14：快照复用（loaded_snapshot_id）与全部续页 section 统一核 profile
  ——CC 的包不得被 estomago 身份冒充 unchanged 复用；拒绝原因=profile
  不匹配（不是被别的检查先拦）
- R15：JSON 非有限值（1e400→inf）经 int() 的溢出=结构化 INVALID_ARGUMENT
  （此前裸 OverflowError 逃逸成 500）；bootstrap.next 游标字段按类型校验
  （字符串偏移/对象 offset/数组/非字符串日期——此前 ValueError/TypeError/
  SQLite ProgrammingError 裸异常）
"""
from __future__ import annotations

import pytest

from mariposa.capabilities import input_schemas as sc
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, SnapshotStale
from mariposa.identity import service as identity
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "jiaming_estomago": identity.Principal(
            "jiaming", "周家明", "agent", "estomago", "be"),
    }


class TestR13SchemaCoverage:
    def test_relations_list_direction_both_accepted(self):
        sc.validate("memory.relations.list",
                    {"memory_id": "00001", "direction": "both"})

    def test_by_tag_namespace_optional(self):
        sc.validate("memory.by_tag", {"tag": "daily"})
        sc.validate("memory.by_tag", {"tag": "daily", "namespace": "tag"},
                    )  # 显式等价值同样合法

    def test_blocked_capability_placeholder_shape(self, actors):
        """blocked 能力：占位 schema 放行形状，handler 的 blocked 权威。"""
        out = registry.invoke(actors["jiaming"], "chat.send",
                              {"anything": "ignored"}, None)
        assert out["ok"] is True
        assert out["data"]["status"] == "blocked"

    def test_real_registry_full_coverage(self):
        missing = [n for n in registry.REGISTRY
                   if n not in sc.V2_INPUT_SCHEMAS]
        assert missing == [], f"真实注册表缺 schema: {missing}"


class TestR14CrossProfileSnapshot:
    def test_cross_profile_loaded_snapshot_rejected(self, actors):
        b1 = registry.invoke(actors["jiaming"], "bootstrap.get",
                             {"profile": "claude_chat"}, None)["data"]
        with pytest.raises(SnapshotStale) as ei:
            registry.invoke(actors["jiaming_estomago"], "bootstrap.get", {
                "profile": "estomago",
                "loaded_snapshot_id": b1["snapshot_id"]}, None)
        # 目标拒绝原因=profile 不匹配（state_hash 本未变——不得冒充 unchanged）
        assert ei.value.detail.get("saved_profile") == "claude_chat"
        assert ei.value.detail.get("profile") == "estomago"

    def test_cross_profile_next_page_unified_gate(self, actors):
        """续页身份检查统一前置：plans/i 等 section 同样核 profile，
        用真实有效游标触发目标拒绝原因（不是被游标检查先拦）。"""
        b1 = registry.invoke(actors["jiaming"], "bootstrap.get",
                             {"profile": "claude_chat"}, None)["data"]
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming_estomago"], "bootstrap.next", {
                "snapshot_id": b1["snapshot_id"],
                "section": "plans", "cursor": {"plans_offset": 0}}, None)
        assert ei.value.detail.get("entry_source") == "estomago"
        assert ei.value.detail.get("profile") == "claude_chat", \
            "目标拒绝原因=profile 不匹配（真实有效游标触发）"


class TestR15RawExceptions:
    def test_nonfinite_int_argument_structured(self, actors):
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "activity.list",
                            {"limit": float("inf")}, None)
        assert ei.value.detail.get("code") == "INVALID_ARGUMENT"

    def test_cursor_field_types_validated(self, actors):
        b1 = registry.invoke(actors["jiaming"], "bootstrap.get",
                             {"profile": "claude_chat"}, None)["data"]
        snap = b1["snapshot_id"]
        for section, cursor in (
            ("i", {"i_offset": "5"}),
            ("plans", {"plans_offset": {}}),
            ("memory_days", {"memory_before_date": 5}),
            ("plan_content", {"plan_id": "p1", "plan_offset": []}),
        ):
            with pytest.raises(Forbidden) as ei:
                registry.invoke(actors["jiaming"], "bootstrap.next", {
                    "snapshot_id": snap, "section": section,
                    "cursor": cursor}, None)
            assert ei.value.detail.get("code") == "INVALID_ARGUMENT", \
                f"{section} 游标类型错误须结构化拒绝（got {ei.value.detail.get('code')}）"
            assert ei.value.detail.get("cursor_field"), \
                f"{section} 拒绝须携带字段定位"
