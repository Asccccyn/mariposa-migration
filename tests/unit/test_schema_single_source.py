"""公开输入校验单一正本（裁定 2026-10-06 她/林石见批准）：

V2_INPUT_SCHEMAS 是唯一运行时 schema 正本——历史 execution_pack v1.1
schema 不再参与 runtime validation（考古保留）。同步锁 F-J-22（hold
公开面 creation_mode 显式必填）与 F-J-24（append 开放 provenance 字段）。

根因级防回归（林的第 4 条）：**注册能力不得悄悄无 schema / 不得从历史包
获得 schema**——存量缺口进 SCHEMA_BACKLOG 白名单（只许收缩），新增能力
忘写 schema 测试直接红。
"""
from __future__ import annotations

import pytest

from mariposa.capabilities import registry
from mariposa.capabilities import input_schemas as sc
from mariposa.errors import Forbidden
from tests.conftest import reset_all

import re
import inspect

# 裁定（她 2026-10-06 终版"删门不删档案"）：**无 backlog**——所有注册
# 可调用能力必须在现行正本有 schema；历史包只是档案不是门卫。
def _registered() -> set[str]:
    src = inspect.getsource(registry)
    return set(re.findall(r'add\("([a-z_.]+)"', src))


class TestSingleSource:
    def test_no_capability_served_from_historical_pack(self):
        """裁定：历史 execution_pack schema 不得参与 runtime 校验。

        键重叠合法（V2 覆盖取胜）；锁的是行为不变量：schema_for 的返回
        恒等于 V2 条目（同一对象），历史包独有的键一律 None。
        """
        hist = set(sc.load_legacy_execution_pack_schemas())
        for cap in hist | set(sc.V2_INPUT_SCHEMAS):
            got = sc.schema_for(cap)
            assert got is sc.V2_INPUT_SCHEMAS.get(cap), \
                f"{cap} 的 schema 不是 V2 正本（历史包回流）"
        for cap in hist - set(sc.V2_INPUT_SCHEMAS):
            assert sc.schema_for(cap) is None, \
                f"{cap} 仍从历史包获得 schema"

    def test_registered_coverage_no_new_schemaless(self):
        """根因锁：注册能力必须有现行 schema 或在显式收缩的 backlog 里。"""
        reg = _registered()
        v2 = set(sc.V2_INPUT_SCHEMAS)
        schemaless = reg - v2
        assert schemaless == set(), \
            f"注册能力缺现行 schema（写进 V2——没有 fallback，没有 backlog）: {sorted(schemaless)}"


class TestFJ22CreationModeRequired:
    def test_public_hold_without_creation_mode_rejected(self):
        with pytest.raises(Forbidden) as ei:
            sc.validate("memory.hold", {
                "text": "正文", "original_title": "标题",
                "categories": ["daily"]})
        assert ei.value.detail.get("code") == "SCHEMA_VIOLATION"

    @pytest.mark.parametrize("mode", ["contemporaneous", "retrospective"])
    def test_public_hold_with_explicit_mode_passes_schema(self, mode):
        args = {"text": "正文", "original_title": "标题",
                "categories": ["daily"], "creation_mode": mode}
        if mode == "retrospective":
            args["memory_date"] = "2026-09-01"
        sc.validate("memory.hold", args)  # 不抛即通过


class TestHandlerContractSchemas:
    def test_memory_search_accepts_current_shape(self):
        sc.validate("memory.search", {"query": "搬家", "limit": 20})
        with pytest.raises(Forbidden):
            sc.validate("memory.search",
                        {"query": "x", "mode": "hybrid"})  # 旧包残留字段：拒

    def test_memory_get_memory_id_only(self):
        sc.validate("memory.get", {"memory_id": "00001"})
        with pytest.raises(Forbidden):
            sc.validate("memory.get",
                        {"memory_id": "00001", "version": 2})  # 非公开参数


class TestFJ24AppendFields:
    def test_append_accepts_provenance_fields_at_schema(self):
        sc.validate("memory.our_words.append", {
            "memory_id": "00001",
            "words": [{"speaker": "qiaosheng", "text": "一句原话",
                       "expression_kind": "verbatim",
                       "source_ref": "source_msg:sm_xxx"}]})
        sc.validate("memory.our_words.append", {
            "memory_id": "00001",
            "words": [{"speaker": "jiaming", "text": "一句",
                       "expression_kind": "paraphrase",
                       "source_ref": None}]})

    def test_append_bad_source_still_rejected_by_domain(self):
        """schema 放行 ≠ provenance 放松：坏来源由领域层拒（公开面级）。"""
        reset_all()
        from mariposa.identity import service as identity
        from mariposa.memory import service as memory
        pr = identity.Principal("jiaming", "周家明", "agent",
                                "claude_chat", "bj")
        out = memory.hold(pr, text="目标桶", original_title="目标",
                          categories=["daily"], creation_mode="contemporaneous",
                          raw_pending=False)
        with pytest.raises(Forbidden) as ei:
            registry.invoke(pr, "memory.our_words.append", {
                "memory_id": out["memory_id"],
                "words": [{"speaker": "qiaosheng", "text": "x",
                           "expression_kind": "verbatim",
                           "source_ref": "本地消息id不是正式来源"}]}, None)
        assert ei.value.detail.get("code") == "INVALID_SOURCE_REF"
