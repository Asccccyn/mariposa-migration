"""2026-10-06 审计 1005B 根因修复回归。

修复面（全部是根因修复，非补丁）：
- R1 词表文案从 MOOD_CATEGORIES 动态生成——此前词表 7 类、5 处文案
  写"固定 8 个"（a1d58fe 换词表时漏改），词表正本自相矛盾；
- R2 compact._JUDGE_DROP 字段名修正（confidence_kind 曾误写
  provider_confidence_kind——删除是空操作）+ 恒 null 的
  provider_confidence 一并删除；
- R3 compact_v1 成为 MCP 面默认（HTTP 面保持 legacy）——此前纯
  opt-in 且无任何消费者传参，瘦身从未生效；显式 profile 语义不变；
- R4 带心情的 hold 必须显式 creation_mode（缺省不再默认同期——
  防补记心情绕过 V2-REC-04 冻结裁定）；
- R5 estómago 内置绑定白名单补 memory.hold.status（此前恢复对账
  403，一次 unknown 效果即卡死换窗）。
"""
from __future__ import annotations

import pytest

from mariposa.capabilities import compact, registry
from mariposa.errors import Forbidden
from mariposa.identity import service as identity
from mariposa.memory import service as memory
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "estomago": identity.Principal(
            "jiaming", "周家明", "agent", "estomago_builtin", "b3",
            capabilities_allowlist=frozenset({
                "memory.hold", "memory.hold.status",
                "memory.mood.vocab"})),
    }


def _mood_args(**over):
    base = {"text": "晚饭与植物", "original_title": "晚饭",
            "categories": ["daily"],
            "creation_mode": "contemporaneous"}  # F-J-22：公开面显式声明
    base.update(over)
    return base


# ---------------------------------------------------------------- R1

class TestMoodVocabCopyConsistency:
    def test_error_copy_matches_table_size(self, actors):
        """R1：报错文案 N=len(MOOD_CATEGORIES)，词表变更后随动
        （此前 7 类配"固定 8 个"文案）。服务层直调——registry 入口
        会被 schema 枚举先拦，文案在服务层兜底路径。"""
        n = len(memory.MOOD_CATEGORIES)
        with pytest.raises(Forbidden) as ei:
            memory.hold(actors["jiaming"], text="晚饭与植物",
                        original_title="晚饭", categories=["daily"],
                        mood={"tags": ["不存在的词"]},
                        creation_mode="contemporaneous")
        assert f"固定 {n} 个" in str(ei.value)

    def test_schema_enum_single_source(self):
        """R1：schema 枚举从 MOOD_CATEGORIES 构建（单一事实源），
        不再是第二份硬编码副本。"""
        from mariposa.capabilities import input_schemas
        n = len(memory.MOOD_CATEGORIES)
        be = input_schemas.schema_for("memory.by_emotion")
        assert be["properties"]["tag"]["enum"] == list(memory.MOOD_CATEGORIES)
        hold = input_schemas.schema_for("memory.hold")
        mood = hold["properties"]["mood"]["properties"]["tags"]
        assert mood["items"]["enum"] == list(memory.MOOD_CATEGORIES)
        assert mood["maxItems"] == memory.MOOD_TAGS_MAX
        assert n >= 1  # 词表为空即本断言失效——显式失败好过静默漂移

    def test_vocab_payload_copy_and_field_name(self, actors):
        """R1/H：vocab 正本文案数量=词表长度；子心情字段名与 hold
        schema 一致（mood.text，不再误导 mood_note）。"""
        out = registry.invoke(actors["jiaming"], "memory.mood.vocab",
                              {}, None)
        data = out["data"]
        n = len(data["categories"])
        assert n == len(memory.MOOD_CATEGORIES)
        assert f"固定 {n} 个" in data["rules"]["stored"]
        assert "mood.text" in data["rules"]["sub_mood"]
        assert data["rules"]["max_tags"] == memory.MOOD_TAGS_MAX

    def test_tags_limit_message_sourced_from_constant(self, actors):
        """R1：标签上限文案与服务端常量同源（服务层直调——schema
        maxItems 会先拦合法调用方，文案在服务层兜底路径）。"""
        with pytest.raises(Forbidden) as ei:
            memory.hold(actors["jiaming"], text="晚饭与植物",
                        original_title="晚饭", categories=["daily"],
                        mood={"tags": ["开心", "爱", "生气", "悲伤"]},
                        creation_mode="contemporaneous")
        assert f"最多 {memory.MOOD_TAGS_MAX} 个" in str(ei.value)


# ---------------------------------------------------------------- R2

class TestCompactJudgeDrop:
    def test_judge_fields_actually_dropped(self):
        """R2：confidence_kind/provider_confidence 真的被删（此前
        字段名写错——删除是空操作）。"""
        payload = {"candidates": [{
            "candidate_ref": "memory:00001",
            "judge": {"model_id": "m", "prompt_version": "p",
                      "confidence_kind": "provider",
                      "provider_confidence": 0.9,
                      "relevance_signal": 5,
                      "evaluation_status": "evaluated"},
        }]}
        out = compact.project("memory.recall.start", payload)
        j = out["candidates"][0]["judge"]
        assert "confidence_kind" not in j
        assert "provider_confidence" not in j
        assert "model_id" not in j and "prompt_version" not in j
        assert j["relevance_signal"] == 5, "有语义的字段不动"


# ---------------------------------------------------------------- R3

class TestMcpDefaultCompact:
    def test_default_profile_applies_to_supported(self, actors):
        """R3：MCP 面默认 compact_v1——recall 家族出站无诊断字段。"""
        out = registry.invoke(actors["jiaming"], "memory.find_words",
                              {"operation_id": "op-rc1",
                               "query": "晚饭"},
                              None, default_output_profile="compact_v1")
        assert "query_fingerprint" not in out["data"]["data"]
        assert "token_count" not in out["data"]["data"]

    def test_default_profile_silent_fallback_unsupported(self, actors):
        """R3：默认提升对不支持 compact 的能力静默回退 legacy
        （memory.get 不在白名单——不能因面的默认值被拒）。"""
        registry.invoke(actors["jiaming"], "memory.hold",
                        _mood_args(memory_date="2026-10-05"), None)
        out = registry.invoke(actors["jiaming"], "memory.get",
                              {"memory_id": "00001"}, None,
                              default_output_profile="compact_v1")
        assert out["ok"] is True
        assert "text" in out["data"]

    def test_explicit_unsupported_still_rejected(self, actors):
        """R3：显式请求 compact_v1 对不支持的能力照旧 fail fast。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.get",
                            {"memory_id": "00001",
                             "output_profile": "compact_v1"}, None)
        assert ei.value.code == "OUTPUT_PROFILE_UNSUPPORTED"

    def test_explicit_legacy_overrides_default(self, actors):
        """R3：显式 legacy 永远可退回全量（默认只是默认）。"""
        out = registry.invoke(actors["jiaming"], "memory.find_words",
                              {"operation_id": "op-rc2", "query": "晚饭",
                               "output_profile": "legacy"},
                              None, default_output_profile="compact_v1")
        assert "query_fingerprint" in out["data"]["data"]

    def test_http_face_default_unchanged(self, actors):
        """R3：不传 default_output_profile（HTTP 面路径）默认 legacy
        ——estómago/网页契约零变化。"""
        out = registry.invoke(actors["jiaming"], "memory.find_words",
                              {"operation_id": "op-rc3", "query": "晚饭"},
                              None)
        assert "query_fingerprint" in out["data"]["data"]


# ---------------------------------------------------------------- R4

class TestMoodRequiresExplicitCreationMode:
    def test_mood_without_creation_mode_rejected(self, actors):
        """R4+F-J-22：mood 给了而 creation_mode 缺省 → 拒。
        公开面：schema 必填先拦（SCHEMA_VIOLATION——比 R4 的窗口拦截更早更强）；
        服务层：绕过 schema 的内部调用仍按 R4 拒 MOOD_WINDOW_REQUIRED（不猜）。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.hold",
                            _mood_args(creation_mode=None,
                                       mood={"tags": ["开心"]}), None)
        assert ei.value.code in ("SCHEMA_VIOLATION",)
        from mariposa.memory import service as _mem
        with pytest.raises(Forbidden) as ei2:
            _mem.hold(actors["jiaming"], text="x", original_title="t",
                      categories=["daily"], mood={"tags": ["开心"]},
                      raw_pending=False)
        assert ei2.value.code == "MOOD_WINDOW_REQUIRED"
        assert "creation_mode" in str(ei2.value)

    def test_mood_with_explicit_contemporaneous_ok(self, actors):
        """R4：显式 contemporaneous 照常写入。"""
        out = registry.invoke(actors["jiaming"], "memory.hold",
                              _mood_args(mood={"tags": ["开心"]},
                                         creation_mode="contemporaneous"),
                              None)
        assert out["ok"] is True

    def test_no_mood_default_still_fine(self, actors):
        """R4：不带心情的 hold 缺省照旧（不扩大打击面——事件补记
        本身合法，只有当时心情受窗口约束）。"""
        out = registry.invoke(actors["jiaming"], "memory.hold",
                              _mood_args(memory_date="2025-01-01"), None)
        assert out["ok"] is True

    def test_retrospective_with_mood_rejected(self, actors):
        """R4（既有语义回归）：显式 retrospective + 心情仍拒。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "memory.hold",
                            _mood_args(mood={"tags": ["开心"]},
                                       creation_mode="retrospective"),
                            None)
        assert ei.value.code == "MOOD_WINDOW_REQUIRED"


# ---------------------------------------------------------------- R5

class TestEstomagoAllowlistRecovery:
    def test_hold_status_reachable_for_builtin_binding(self, actors):
        """R5：白名单含 memory.hold.status——estómago 恢复对账可达
        （此前 403，unknown 效果后换窗被 WRITES_UNRESOLVED 卡死）。"""
        out = registry.invoke(actors["estomago"], "memory.hold.status",
                              {"operation_id": "op-no-such"}, None)
        assert out["ok"] is True

    def test_allowlist_still_confines(self, actors):
        """R5：补项不放宽——白名单外（读检索面）照旧 403。"""
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["estomago"], "memory.get",
                            {"memory_id": "00001"}, None)
        assert ei.value.code == "FORBIDDEN"
