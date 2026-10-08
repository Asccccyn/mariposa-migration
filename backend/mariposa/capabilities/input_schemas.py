"""严格输入 Schema 接入（v1.1 contracts/core_input_schemas.json 的 12 项）。

最小校验器（不引第三方依赖）：required / 类型 / additionalProperties:false /
enum。schema 从规格文件加载（同源），tools/list 与 HTTP/MCP invoke 共用。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..errors import Forbidden
from .. import config

_TYPES = {"string": str, "integer": int, "number": (int, float),
          "boolean": bool, "object": dict, "array": list, "null": type(None)}

# 心情词表单一事实源（审计 1005B）：schema 枚举运行时从 MOOD_CATEGORIES
# 构建——词表调整一处生效，不再维护第二份硬编码副本（7/8 漂移已实际发生）
from ..memory.service import (MOOD_CATEGORIES as _MOOD_CATEGORIES,
                              MOOD_TAGS_MAX as _MOOD_TAGS_MAX)
_MOOD_ENUM = list(_MOOD_CATEGORIES)

_CACHE: dict | None = None


def load_legacy_execution_pack_schemas() -> dict:
    """【纯历史资料读取器】execution_pack v1.1 的 9 月合同（考古/文档用）。

    ⚠️ 裁定（她 2026-10-06，"删门不删档案"）：**禁止参与 runtime
    validation**——公开校验唯一门卫=V2_INPUT_SCHEMAS，schema_for 不调用
    本函数、也不得有任何运行时路径调用它。文件保留作历史证据。
    """
    global _CACHE
    if _CACHE is None:
        candidates = [
            config.PROJECT_ROOT / "docs" / "execution_pack_v1.1" /
            "contracts" / "core_input_schemas.json",
            Path(__file__).resolve().parents[3] / "docs" /
            "execution_pack_v1.1" / "contracts" / "core_input_schemas.json",
        ]
        path = next((p for p in candidates if p.exists()), candidates[0])
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            _CACHE = data.get("schemas", data)
        else:
            _CACHE = {}
    return _CACHE


def _resolve(spec: dict, root: dict) -> dict:
    """解 $ref（仅支持本文件 #/$defs/x）。"""
    ref = spec.get("$ref")
    if not ref:
        return spec
    name = ref.split("/")[-1]
    d = root.get("$defs", {}).get(name, {})
    return {**d, **{k: v for k, v in spec.items() if k != "$ref"}}


def _matches(spec: dict, value, root: dict) -> bool:
    """字段值是否满足该（已解析 $ref 的）spec；anyOf 任一即可。

    深度校验（OPS-02）：嵌套对象 required、数组 minItems/maxItems、
    数值 minimum/maximum 与字符串长度同等生效。
    """
    spec = _resolve(spec, root)
    if "anyOf" in spec:
        return any(_matches(sub, value, root) for sub in spec["anyOf"])
    t = spec.get("type")
    if t:
        py = _TYPES.get(t)
        if py is None:
            return True
        if isinstance(value, bool) and t in ("integer", "number"):
            return False  # bool 不是数字
        if not isinstance(value, py):
            return False
    if "enum" in spec and value not in spec["enum"]:
        return False
    if isinstance(value, str):
        if "minLength" in spec and len(value) < spec["minLength"]:
            return False
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            return False
        if "pattern" in spec:
            import re
            if not re.match(spec["pattern"], value):
                return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            return False
        if "maximum" in spec and value > spec["maximum"]:
            return False
    if isinstance(value, list):
        if "minItems" in spec and len(value) < spec["minItems"]:
            return False
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            return False
        if "items" in spec and not all(
                _matches(spec["items"], item, root) for item in value):
            return False
    if isinstance(value, dict) and "properties" in spec:
        sub_props = spec.get("properties", {})
        if "minProperties" in spec and len(value) < spec["minProperties"]:
            return False
        if spec.get("additionalProperties") is False:
            for k in value:
                if k not in sub_props:
                    return False
        for req in spec.get("required", []):
            if req not in value or value[req] in (None, ""):
                return False
        for k, sub in sub_props.items():
            if k in value and not _matches(sub, value[k], root):
                return False
    return True


#: v2 能力输入 schema（spec_v2 §12；与包内 v1.1 契约分层，schema_for 优先取此层）
V2_INPUT_SCHEMAS: dict[str, dict] = {
    # —— episode（她批 2026-10-07"做吧"）：蓝图 §6 三个能力 ——
    "episode.apply": {
        "type": "object",
        "required": ["action", "scope_id", "reason_code"],
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string",
                        "enum": ["start", "continue", "pause", "close",
                                  "correct"]},
            "scope_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "episode_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "expected_revision": {"type": "integer", "minimum": 1},
            "label": {"type": "string", "minLength": 1, "maxLength": 80},
            "source_selections": {"type": "array", "minItems": 1,
                                   "maxItems": 16},
            "terminal_source_ref": {"type": "object"},
            "decision_source_refs": {"type": "array", "minItems": 1,
                                      "maxItems": 16},
            "reason_code": {"type": "string",
                             "enum": ["tracking_requested", "same_episode",
                                       "waiting", "explicit_completion",
                                       "goal_resolved", "episode_concluded",
                                       "boundary_error"]},
            "note": {"type": "string", "maxLength": 500},
            "corrected_state": {"type": "string",
                                 "enum": ["OPEN", "QUIESCENT", "CLOSED"]},
            "operation_id": {"type": "string", "minLength": 1,
                             "maxLength": 256}},
    },
    "episode.get": {
        "type": "object", "required": ["episode_id"],
        "additionalProperties": False,
        "properties": {"episode_id": {"type": "string", "minLength": 1,
                                       "maxLength": 256}},
    },
    "episode.list": {
        "type": "object", "required": ["scope_id"],
        "additionalProperties": False,
        "properties": {"scope_id": {"type": "string", "minLength": 1,
                                     "maxLength": 256},
                        "states": {"type": "array",
                                    "items": {"type": "string",
                                               "enum": ["OPEN", "QUIESCENT",
                                                         "CLOSED"]}},
                        "limit": {},
                        "cursor": {"type": "object"}},
    },

    # —— 裁定（她 2026-10-06"删门不删档案"终版）：以下注册能力此前无
    #    schema（走零校验），按现行 handler 合同补齐——注册即可调用 ⇒
    #    必须有现行 schema，测试锁死（test_schema_single_source 无 backlog）
        # —— R13（复审 2026-10-07）：真实可见入口补齐（测试改按真实 registry
    #    断言，不再靠源码正则漏数）。activity.list 与 maintenance.activity.list
    #    同一形状（alias 复用现行 canonical 合同）
    "activity.list": {"type": "object", "additionalProperties": False,
                      "properties": {"event_type": {"type": "string"},
                                     "limit": {}}},
    "capabilities.list": {"type": "object",
                          "additionalProperties": False, "properties": {}},
    "capabilities.status": {"type": "object",
                            "additionalProperties": False, "properties": {}},
    "jobs.status": {"type": "object", "additionalProperties": False,
                    "properties": {}},
    "plan.get": {"type": "object", "required": ["plan_id"],
                 "additionalProperties": False,
                 "properties": {"plan_id": {"type": "string",
                                            "minLength": 1,
                                            "maxLength": 256}}},
    "presence.handoff.latest": {"type": "object",
                                "additionalProperties": False,
                                "properties": {}},
    "settings.get": {"type": "object", "additionalProperties": False,
                     "properties": {}},
    # R13 占位：blocked/reserved 能力——参数被忽略、能力被拒，占位显式
    # 合同（不恢复历史 fallback；handler 的 blocked 响应仍权威）
    "chat.cancel": {"type": "object"},
    "chat.conversations.create": {"type": "object"},
    "chat.conversations.get": {"type": "object"},
    "chat.conversations.list": {"type": "object"},
    "chat.conversations.messages": {"type": "object"},
    "chat.messages.list": {"type": "object"},
    "chat.send": {"type": "object"},
    "group.archive.get": {"type": "object"},
    "group.archive.list": {"type": "object"},
    "group.history": {"type": "object"},
    "group.list": {"type": "object"},
    "group.profile": {"type": "object"},
    "group.send": {"type": "object"},
    "group.send_message": {"type": "object"},
    "group.status": {"type": "object"},
    "listening.enqueue": {"type": "object"},
    "listening.join": {"type": "object"},
    "listening.leave": {"type": "object"},
    "listening.pause": {"type": "object"},
    "listening.play": {"type": "object"},
    "listening.queue": {"type": "object"},
    "listening.seek": {"type": "object"},
    "listening.sync": {"type": "object"},
    "settings.update": {"type": "object"},
    "voice.call.end": {"type": "object"},
    "voice.call.get": {"type": "object"},
    "voice.call.start": {"type": "object"},
    "voice.send": {"type": "object"},
    "voice.status": {"type": "object"},
    "wakeup.configure": {"type": "object"},
    "wakeup.status": {"type": "object"},
    "wishstar.get": {"type": "object"},
    "wishstar.list": {"type": "object"},
    "wishstar.respond": {"type": "object"},
    "wishstar.write": {"type": "object"},
"emotion.context.get": {"type": "object", "additionalProperties": False,
                             "properties": {}},
    "handoff.latest": {"type": "object", "additionalProperties": False,
                        "properties": {}},
    "listening.status": {"type": "object", "additionalProperties": False,
                          "properties": {}},
    "maintenance.activity.list": {
        "type": "object", "additionalProperties": False,
        "properties": {"event_type": {"type": "string"},
                       "limit": {}}},
    "maintenance.jobs.status": {"type": "object",
                                 "additionalProperties": False,
                                 "properties": {}},
    "maintenance.outbox.status": {"type": "object",
                                   "additionalProperties": False,
                                   "properties": {}},
    "maintenance.settings.get": {"type": "object",
                                  "additionalProperties": False,
                                  "properties": {}},
    "media.get": {"type": "object", "required": ["content_hash"],
                   "additionalProperties": False,
                   "properties": {"content_hash": {"type": "string",
                                                    "minLength": 64,
                                                    "maxLength": 64}}},
    "media.list": {"type": "object", "additionalProperties": False,
                    "properties": {"limit": {}}},
    "memory.by_tag": {"type": "object", "required": ["tag"],
                       "additionalProperties": False,
                       "properties": {"namespace": {"type": "string",
                                                     "minLength": 1},
                                      "tag": {"type": "string",
                                               "minLength": 1},
                                      "whose": {"type": "string",
                                                 "enum": ["jiaming",
                                                          "qiaosheng"]}}},
    "memory.keeps.list": {"type": "object", "additionalProperties": False,
                           "properties": {"memory_id": {"type": "string",
                                                         "minLength": 1}}},
    "memory.list": {"type": "object", "additionalProperties": False,
                     "properties": {"limit": {},
                                    "cursor": {"type": "object"},
                                    "cursor_date": {"anyOf": [
                                        {"type": "string"},
                                        {"type": "null"}]},
                                    "state": {"type": "string"}}},
    "memory.relations.list": {"type": "object",
                               "required": ["memory_id"],
                               "additionalProperties": False,
                               "properties": {"memory_id": {"type": "string",
                                                             "minLength": 1},
                                               "direction": {"type": "string",
                                                              "enum": ["out",
                                                                        "in",
                                                                        "both"]}}},
    "memory.relations.trace": {"type": "object",
                                "required": ["memory_id"],
                                "additionalProperties": False,
                                "properties": {"memory_id": {"type": "string",
                                                              "minLength": 1},
                                                "max_depth": {}}},
    "plan.list": {"type": "object", "additionalProperties": False,
                   "properties": {"states": {"type": "array",
                                              "items": {"type": "string"}}}},
    "presence.status": {"type": "object", "additionalProperties": False,
                         "properties": {}},
    "source.binding.list": {"type": "object", "additionalProperties": False,
                             "properties": {"memory_id": {"type": "string",
                                                           "minLength": 1}}},
    "source.conversation.get": {"type": "object",
                                 "required": ["conversation_id"],
                                 "additionalProperties": False,
                                 "properties": {"conversation_id": {"type": "string",
                                                                     "minLength": 1},
                                                 "limit": {},
                                                 "after_seq": {"type": "integer"},
                                                 "before_seq": {"type": "integer"},
                                                 "around_seq": {"type": "integer"},
                                                 "after_cursor": {"type": "string"},
                                                 "before_cursor": {"type": "string"}}},
    "source.conversations.list": {"type": "object",
                                   "additionalProperties": False,
                                   "properties": {"limit": {},
                                                    "offset": {},
                                                    "provider": {"type": "string"}}},
    "source.import.batches": {"type": "object",
                               "additionalProperties": False,
                               "properties": {"limit": {}}},
    "source.import.status": {"type": "object", "required": ["batch_id"],
                              "additionalProperties": False,
                              "properties": {"batch_id": {"type": "string",
                                                           "minLength": 1}}},
    "source.memory.open": {"type": "object", "required": ["memory_id"],
                            "additionalProperties": False,
                            "properties": {"memory_id": {"type": "string",
                                                          "minLength": 1},
                                            "include_content": {"type": "boolean"}}},
    "source.message.get": {"type": "object",
                            "additionalProperties": False,
                            "properties": {"message_id": {"type": "string",
                                                           "minLength": 1},
                                            "provider": {"type": "string"},
                                            "provider_message_id": {"type": "string"},
                                            "context": {},
                                            "include_content": {"type": "boolean"}}},
    "source.range.open": {"type": "object",
                           "required": ["conversation_id",
                                         "start_message_id",
                                         "end_message_id"],
                           "additionalProperties": False,
                           "properties": {"conversation_id": {"type": "string",
                                                               "minLength": 1},
                                           "start_message_id": {"type": "string",
                                                                 "minLength": 1},
                                           "end_message_id": {"type": "string",
                                                               "minLength": 1},
                                           "start_char_offset": {"type": "integer",
                                                                   "minimum": 0},
                                           "end_char_offset": {"type": "integer",
                                                                "minimum": 0},
                                           "include_content": {"type": "boolean"}}},
    "source.search": {"type": "object", "required": ["query"],
                       "additionalProperties": False,
                       "properties": {"query": {"type": "string",
                                                 "minLength": 1,
                                                 "maxLength": 2000},
                                       "limit": {},
                                       "offset": {},
                                       "provider": {"type": "string"},
                                       "conversation_id": {"type": "string"},
                                       "senders": {"type": "array",
                                                     "items": {"type": "string"}},
                                       "date_from": {"type": "string"},
                                       "date_to": {"type": "string"}}},
    "time.context": {"type": "object", "additionalProperties": False,
                      "properties": {}},
    "time.now": {"type": "object", "additionalProperties": False,
                  "properties": {}},
    "time.since": {"type": "object", "additionalProperties": False,
                    "properties": {}},
    "memory.deletion.request": {
        "type": "object",
        "required": ["memory_id", "reason", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.deletion.withdraw": {
        "type": "object", "required": ["request_id"],
        "additionalProperties": False,
        "properties": {"request_id": {"type": "string", "minLength": 1}},
    },
    "memory.deletion.decide": {
        "type": "object", "required": ["request_id", "decision"],
        "additionalProperties": False,
        "properties": {
            "request_id": {"type": "string", "minLength": 1},
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "rejection_reason": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.deletion.get": {
        "type": "object", "required": ["request_id"],
        "additionalProperties": False,
        "properties": {"request_id": {"type": "string", "minLength": 1}},
    },
    "memory.deletion.list": {
        "type": "object", "additionalProperties": False,
        "properties": {"status": {"type": "string"},
                       "memory_id": {"type": "string"}},
    },
    "memory.delete": {
        "type": "object", "required": ["memory_id", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.relations.correct": {
        "type": "object",
        "required": ["relation_id", "correction_action", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "relation_id": {"type": "string", "minLength": 1},
            "correction_action": {"type": "string",
                "enum": ["remove_wrong_binding",
                         "replace_wrong_binding"]},
            "replacement": {"type": "object"},
            "note": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "i.item.relations.correct": {
        "type": "object",
        "required": ["relation_id", "correction_action", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "relation_id": {"type": "string", "minLength": 1},
            "correction_action": {"type": "string",
                "enum": ["remove_wrong_binding",
                         "replace_wrong_binding"]},
            "replacement": {"type": "object"},
            "note": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "source.binding.correct": {
        "type": "object",
        "required": ["binding_id", "correction_action", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "binding_id": {"type": "string", "minLength": 1},
            "correction_action": {"type": "string",
                "enum": ["remove_wrong_binding",
                         "replace_wrong_binding"]},
            "replacement": {"type": "object"},
            "note": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "plan.memory.correct": {
        "type": "object",
        "required": ["link_id", "correction_action", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "link_id": {"type": "string", "minLength": 1},
            "correction_action": {"type": "string",
                "enum": ["remove_wrong_binding",
                         "replace_wrong_binding"]},
            "replacement": {"type": "object"},
            "note": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    # ===== CB-050（2026-10-02 审计 P2）：全部无 schema 的 write 能力
    # 补齐严格入参合同——错误布尔/类型形状在公开边界拒绝，不再被
    # handler 的 bool()/int() 强转静默改写正式标记 =====
    "memory.pin": {
        "type": "object", "required": ["memory_id"],
        "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1},
                       "value": {"type": "boolean"}},
    },
    "memory.protect": {
        "type": "object", "required": ["memory_id"],
        "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1},
                       "value": {"type": "boolean"}},
    },
    "memory.anchor": {
        "type": "object", "required": ["memory_id"],
        "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1},
                       "value": {"type": "boolean"}},
    },
    "memory.update": {
        "type": "object",
        "required": ["memory_id", "expected_version"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "expected_version": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "minLength": 1},
            "operation_id": {"type": "string"},
            "memory_date": {"anyOf": [{"type": "string"},
                                     {"type": "null"}]},
            "date_confidence": {"type": "string",
                                "enum": ["exact", "inferred",
                                         "unknown"]}},
    },
    "memory.mood.vocab": {
        "type": "object", "additionalProperties": False, "properties": {},
    },
    "memory.by_date": {
        "type": "object", "required": ["date"],
        "additionalProperties": False,
        "properties": {
            "date": {"type": "string", "minLength": 10, "maxLength": 10},
            "limit": {}},
    },
    "memory.by_category": {
        "type": "object", "required": ["category"],
        "additionalProperties": False,
        "properties": {
            "category": {"type": "string", "enum": [
                "daily", "milestone", "sad", "sweet", "date", "plan",
                "sex", "anniversary", "reloplay"]},
            "limit": {},
            "next_cursor": {"type": "object"}},
    },
    "memory.by_emotion": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "tag": {"type": "string", "enum": _MOOD_ENUM},
            "whose": {"type": "string", "enum": ["jiaming", "qiaosheng"]},
            "limit": {},
            "next_cursor": {"type": "object"}},
    },
    "memory.tags.add": {
        "type": "object", "required": ["memory_id", "tags"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "tags": {"type": "array", "minItems": 1, "maxItems": 20,
                     "items": {"type": "string", "minLength": 1}}},
    },
    "memory.keep.revoke": {
        "type": "object", "required": ["mark_id"],
        "additionalProperties": False,
        "properties": {"mark_id": {"type": "string", "minLength": 1}},
    },
    "memory.relations.link": {
        "type": "object",
        "required": ["from_memory", "to_memory", "relation_type"],
        "additionalProperties": False,
        "properties": {
            "from_memory": {"type": "string", "minLength": 1},
            "to_memory": {"type": "string", "minLength": 1},
            "relation_type": {"type": "string",
                              "enum": ["continuation_of", "related_to",
                                       "contradicts", "custom"]},
            "custom_label": {"type": "string"},
            "reverse_label": {"type": "string"}},
    },
    "memory.reengagement.record": {
        "type": "object",
        "required": ["memory_id", "evidence_kind", "occurred_at"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "evidence_kind": {"type": "string", "minLength": 1},
            "occurred_at": {"type": "string", "minLength": 1},
            "evidence_ref": {"anyOf": [{"type": "string"},
                                      {"type": "null"}]}},
    },
    "plan.create": {
        "type": "object", "required": ["title"],
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "content": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "state": {"type": "string",
                      "enum": ["planned", "active", "waiting", "blocked",
                               "done", "cancelled"]},
            "starts_at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "due_at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "date_start": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "date_end": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "weight": {"type": "integer"},
            "link_memory_ids": {"type": "array",
                                "items": {"type": "string",
                                          "minLength": 1}}},
    },
    "plan.update": {
        # RA-001（2026-10-02 复审 P1）：与 handler 真实合同对齐——
        # 受支持变更字段是顶层平铺（handler/UI 均发顶层），不是嵌套
        # changes；state 枚举与 plans.STATES 一致
        "type": "object",
        "required": ["plan_id", "expected_version"],
        "additionalProperties": False,
        "properties": {
            "plan_id": {"type": "string", "minLength": 1},
            "expected_version": {"type": "integer", "minimum": 1},
            "title": {"type": "string", "minLength": 1},
            "content": {"anyOf": [{"type": "string"},
                                  {"type": "null"}]},
            "state": {"type": "string",
                      "enum": ["planned", "active", "waiting", "blocked",
                               "done", "cancelled"]},
            "starts_at": {"anyOf": [{"type": "string"},
                                    {"type": "null"}]},
            "due_at": {"anyOf": [{"type": "string"},
                                 {"type": "null"}]},
            "date_start": {"anyOf": [{"type": "string"},
                                     {"type": "null"}]},
            "date_end": {"anyOf": [{"type": "string"},
                                   {"type": "null"}]},
            "weight": {"type": "integer"},
            "operation_id": {"type": "string"}},
    },
    "plan.complete": {
        "type": "object", "required": ["plan_id", "expected_version"],
        "additionalProperties": False,
        "properties": {
            "plan_id": {"type": "string", "minLength": 1},
            "expected_version": {"type": "integer", "minimum": 1}},
    },
    "plan.cancel": {
        "type": "object", "required": ["plan_id", "expected_version"],
        "additionalProperties": False,
        "properties": {
            "plan_id": {"type": "string", "minLength": 1},
            "expected_version": {"type": "integer", "minimum": 1}},
    },
    "handoff.write": {
        "type": "object", "required": ["content"],
        "additionalProperties": False,
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "topic": {"type": "string"}},
    },
    "presence.touch": {
        "type": "object", "additionalProperties": False,
        "properties": {},
    },
    "presence.handoff.write": {
        "type": "object", "required": ["content"],
        "additionalProperties": False,
        "properties": {"content": {"type": "string", "minLength": 1}},
    },
    "identity.bindings.revoke": {
        "type": "object", "required": ["binding_id"],
        "additionalProperties": False,
        "properties": {"binding_id": {"type": "string", "minLength": 1}},
    },
    "maintenance.rebuild_index": {
        "type": "object", "additionalProperties": False,
        "properties": {"memory_id": {"type": "string"}},
    },
    "maintenance.semantic.warmup": {
        "type": "object", "additionalProperties": False,
        "properties": {},
    },
    "maintenance.outbox.drain": {
        "type": "object", "additionalProperties": False,
        "properties": {"limit": {}},
    },
    "maintenance.source.cleanup": {
        "type": "object", "additionalProperties": False,
        "properties": {"max_age_hours": {"type": "integer", "minimum": 1,
                                         "maximum": 24 * 30}},
    },
    "maintenance.idempotency.reconcile": {
        "type": "object",
        "required": ["capability", "idempotency_key"],
        "additionalProperties": False,
        "properties": {
            "capability": {"type": "string", "minLength": 1},
            "idempotency_key": {"type": "string", "minLength": 1},
            "record_principal": {"type": "string"},
            "stale_seconds": {"type": "integer", "minimum": 1}},
    },
    "source.import": {
        "type": "object", "required": ["path"],
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1},
            # SRC-04：未显式给 filename 时调用方传 null——importer
            # 回退 src.name，schema 必须接受
            "filename": {"anyOf": [{"type": "string"},
                                   {"type": "null"}]}},
    },
    "source.ingest": {
        # estómago 生命周期 WP1（迁移 30）：在线 live_delta。50 条/1MiB
        # 工程初值在 config（SOURCE_LIVE_*）；schema 限形状，服务端再
        # 校验 hash/链/授权
        "type": "object",
        "required": ["operation_id", "stream_id", "origin_instance",
                     "origin_conversation_id", "messages"],
        "additionalProperties": False,
        "properties": {
            "operation_id": {"type": "string", "minLength": 1,
                             "maxLength": 200},
            "stream_id": {"type": "string", "minLength": 1,
                          "maxLength": 200},
            "origin_instance": {"type": "string", "minLength": 1,
                                "maxLength": 100},
            "origin_conversation_id": {"type": "string", "minLength": 1,
                                       "maxLength": 200},
            "messages": {"type": "array", "minItems": 1, "maxItems": 50,
                         "items": {
                "type": "object",
                "required": ["origin_message_id", "revision",
                             "conversation_sequence", "sender",
                             "published_kind", "text", "content_hash"],
                "additionalProperties": False,
                "properties": {
                    "origin_message_id": {"type": "string",
                                          "minLength": 1, "maxLength": 200},
                    "revision": {"type": "integer", "minimum": 1},
                    "previous_revision": {"anyOf": [
                        {"type": "integer", "minimum": 1},
                        {"type": "null"}]},
                    "conversation_sequence": {"type": "integer",
                                              "minimum": 0},
                    "predecessor": {"anyOf": [{
                        "type": "object",
                        "required": ["origin_message_id", "revision"],
                        "additionalProperties": False,
                        "properties": {
                            "origin_message_id": {"type": "string",
                                                  "minLength": 1,
                                                  "maxLength": 200},
                            "revision": {"type": "integer",
                                         "minimum": 1}}}, {"type": "null"}]},
                    "sender": {"type": "string",
                               "enum": ["user", "assistant"]},
                    "published_kind": {"type": "string",
                                       "enum": ["chat_message",
                                                 "sticker_message",
                                                 "voice_message"]},
                    "occurred_at": {"anyOf": [{"type": "string"},
                                              {"type": "null"}]},
                    "received_at": {"anyOf": [{"type": "string"},
                                              {"type": "null"}]},
                    "published_at": {"anyOf": [{"type": "string"},
                                               {"type": "null"}]},
                    "text": {"type": "string"},
                    "assets": {"type": "array",
                               "items": {"type": "string"}},
                    "content_hash": {"type": "string", "minLength": 64,
                                     "maxLength": 64}}}}},
    },
    "source.ingest.status": {
        "type": "object", "required": ["operation_id"],
        "additionalProperties": False,
        "properties": {"operation_id": {"type": "string", "minLength": 1,
                                        "maxLength": 200}},
    },
    "source.binding.bind": {
        "type": "object",
        "required": ["memory_id", "conversation_id", "start_message_id",
                     "end_message_id"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "conversation_id": {"type": "string", "minLength": 1},
            "start_message_id": {"type": "string", "minLength": 1},
            "end_message_id": {"type": "string", "minLength": 1},
            "start_char_offset": {"type": "integer", "minimum": 0},
            "end_char_offset": {"type": "integer", "minimum": 0},
            # RA-026（2026-10-02 复审 P2）：与 service/DB CHECK 统一
            # ——现行置信度为 exact/high/low（inferred 非现行值）
            "confidence": {"type": "string",
                           "enum": ["exact", "high", "low"]}},
    },
    "bootstrap.get": {
        # CB-045：与现行 handler/service 对齐——loaded_snapshot_id 是
        # 去重快照参数（v1 pack schema 缺此字段致公开路径被拒）
        "type": "object", "required": ["profile"],
        "additionalProperties": False,
        "properties": {"profile": {"type": "string",
                                   "enum": ["claude_chat", "cc", "estomago"]},
                       "known_snapshot_id": {"type": "string"},
                       "loaded_snapshot_id": {"type": "string"},
                       "cursor": {"type": "object"}},
    },
    "bootstrap.next": {
        # CB-045：cursor 是服务端返回的 object 复合游标（v1 pack 要
        # 求 string 致续页 SCHEMA_VIOLATION）；section 枚举为现行
        # v2 开窗段（raw 已退役，不再作为默认值）
        "type": "object", "required": ["snapshot_id", "cursor", "section"],
        "additionalProperties": False,
        "properties": {"snapshot_id": {"type": "string", "minLength": 1},
                       "cursor": {"type": "object"},
                       # MEM-02（2026-10-04 二批）：枚举必须包含服务端
                       # 实际签发的续取段——i / plan_content 游标否则
                       # 被 schema 拒绝，续取承诺不可消费
                       "section": {"type": "string",
                                   "enum": ["memory_days", "plans", "i",
                                            "plan_content"]}},
    },
    "memory.our_words.source.correct": {
        "type": "object",
        "required": ["word_id", "correction_action", "operation_id",
                     "expected_source_version"],
        "additionalProperties": False,
        "properties": {
            "word_id": {"type": "string", "minLength": 1},
            "expected_source_ref": {"anyOf": [{"type": "string"},
                                              {"type": "null"}]},
            # CB-007：word 来源换代计数进入 CAS——仅比 source_ref 无法
            # 识别 A→(撤销)→A 的实例换代，旧请求会删掉新绑定
            "expected_source_version": {"type": "integer",
                                        "minimum": 0},
            "correction_action": {"type": "string",
                "enum": ["remove_wrong_binding",
                         "replace_wrong_binding"]},
            "replacement": {"type": "object"},
            "note": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "relations.list": {
        "type": "object", "additionalProperties": False,
        "properties": {
            # F21（2026-10-03 审计 P2）：resource 嵌套字段定型——id 为
            # 数组/未知字段此前越过 schema 在路由层变 500；未知 type
            # 由路由层结构化 4xx 拒绝
            "resource": {"type": "object", "additionalProperties": False,
                         "properties": {
                             "type": {"type": "string"},
                             "memory_id": {"type": "string"},
                             "item_id": {"type": "string"},
                             "revision": {"type": "integer"},
                             "plan_id": {"type": "string"},
                             "word_id": {"type": "string"},
                             "conversation_id": {"type": "string"},
                             "start_message_id": {"type": "string"},
                             "end_message_id": {"type": "string"},
                             "start_char_offset": {"type": "integer"},
                             "end_char_offset": {"type": "integer"},
                             "ref": {"type": "string"}}},
            "memory_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "word_id": {"type": "string"},
            "item_id": {"type": "string"},
            "revision": {"type": "integer"},
            "direction": {"type": "string",
                          "enum": ["in", "out", "both"]},
            "domains": {"type": "array",
                        "items": {"type": "string"}},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"}},
    },
    "relations.trace": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "resource": {"type": "object"},
            "memory_id": {"type": "string"},
            "max_depth": {"type": "integer"}},
    },
    "relations.corrections.list": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "endpoint": {"type": "string"},
            "instance_id": {"type": "string"},
            "domain": {"type": "string"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"}},
    },
    "media.upload.prepare": {
        "type": "object", "required": ["mime", "size"],
        "additionalProperties": False,
        "properties": {"mime": {"type": "string", "minLength": 1},
                       "size": {"type": "integer"}},
    },
    "media.upload.finalize": {
        "type": "object", "required": ["upload_token"],
        "additionalProperties": False,
        "properties": {"upload_token": {"type": "string",
                                        "minLength": 1,
                                        # CB-001：token 只允许服务端
                                        # 签发字符集，路径元字符在公开
                                        # 边界即拒绝（服务层另有同款
                                        # 校验兜底）
                                        "pattern": "^[A-Za-z0-9_-]{8,128}$"}},
    },
    "memory.get": {
        # 按现行 handler 合同（registry._get：仅 memory_id——version 不是
        # 公开参数；旧 execution_pack schema 的 filters/mode 形状是 9 月口径）
        "type": "object", "required": ["memory_id"],
        "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1}},
    },
    "memory.search": {
        # 按现行 handler 合同（registry._search：query+limit；结构化筛选走
        # memory.recall 的 filters，不是本入口）
        "type": "object", "required": ["query"],
        "additionalProperties": False,
        "properties": {"query": {"type": "string", "minLength": 1,
                                  "maxLength": 2000},
                       "limit": {}},
    },
    "memory.hold": {
        # F-J-22（她批准 2026-10-06）：creation_mode 公开面显式必填——
        # 当下/补记由调用方声明，服务端不猜；D1 当天自动日期保留
        # （显式 contemporaneous 且未给日期时同事务补 held_at 上海日）
        "type": "object",
        "required": ["text", "original_title",
                     "categories", "creation_mode"],
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "memory_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "date_confidence": {"type": "string",
                                 "enum": ["exact", "inferred", "unknown"]},
            "occurred_start": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "occurred_end": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "raw_pending": {"type": "boolean"},
            "original_title": {"type": "string", "minLength": 1,
                               "maxLength": 30},
            "categories": {"type": "array", "items": {"type": "string", "enum": [
                "daily", "milestone", "sad", "sweet", "date", "plan", "sex",
                "anniversary", "reloplay"]}},
            "plan_ids": {"type": "array",
                         "items": {"type": "string", "minLength": 1}},
            "mood": {"type": "object", "additionalProperties": False,
                      "properties": {"text": {"type": "string"},
                                     "tags": {"type": "array",
                                              "maxItems": _MOOD_TAGS_MAX,
                                              "items": {"type": "string",
                                              "enum": _MOOD_ENUM}}}},
            "our_words": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["speaker", "text"],
                "properties": {
                    "speaker": {"type": "string", "enum": ["jiaming", "qiaosheng"]},
                    "text": {"type": "string", "minLength": 1},
                    "expression_kind": {"type": "string", "enum": [
                        "verbatim", "paraphrase", "unspecified"]},
                    "source_ref": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}},
            "creation_mode": {"type": "string",
                               "enum": ["contemporaneous", "retrospective"]},
            # WP2（迁移 31）：宿主自动化路径——operation_id+可选钉住成员
            # 的来源片段（带 selections 必带 op；旧调用不带两字段不变）
            "operation_id": {"type": "string", "minLength": 1,
                             "maxLength": 200},
            "source_selections": {"type": "array", "maxItems": 20, "items": {
                "type": "object",
                "required": ["conversation_id", "members"],
                "additionalProperties": False,
                "properties": {
                    "conversation_id": {"type": "string", "minLength": 1},
                    "members": {"type": "array", "minItems": 1,
                                "maxItems": 500, "items": {
                        "type": "object",
                        "required": ["source_message_id", "content_hash"],
                        "additionalProperties": False,
                        "properties": {
                            "source_message_id": {"type": "string",
                                                  "minLength": 1},
                            "content_hash": {"type": "string",
                                             "minLength": 64,
                                             "maxLength": 64}}}},
                    "start_char_offset": {"type": "integer", "minimum": 0},
                    "end_char_offset": {"type": "integer", "minimum": 0}}}},
        },
    },
    "memory.hold.status": {
        "type": "object", "required": ["operation_id"],
        "additionalProperties": False,
        "properties": {"operation_id": {"type": "string", "minLength": 1,
                                        "maxLength": 200}},
    },
    "source.selection.open": {
        "type": "object", "required": ["selection"],
        "additionalProperties": False,
        "properties": {
            "include_content": {"type": "boolean"},
            "selection": {
                "type": "object",
                "required": ["conversation_id", "members"],
                "additionalProperties": False,
                "properties": {
                    "conversation_id": {"type": "string", "minLength": 1},
                    "members": {"type": "array", "minItems": 1,
                                "maxItems": 500, "items": {
                        "type": "object",
                        "required": ["source_message_id"],
                        "additionalProperties": False,
                        "properties": {
                            "source_message_id": {"type": "string",
                                                  "minLength": 1},
                            "content_hash": {"type": "string",
                                             "maxLength": 64}}}},
                    "start_char_offset": {"type": "integer", "minimum": 0},
                    "end_char_offset": {"type": "integer", "minimum": 0}}}},
    },
    "memory.open": {
        "type": "object", "required": ["memory_id"], "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1}},
    },
    "memory.view.confirm": {
        "type": "object", "required": ["memory_id", "receipt_id"],
        "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1},
                        "receipt_id": {"type": "string", "minLength": 1},
                        "confirm_key": {"type": "string"}},
    },
    "memory.recollections.append": {
        "type": "object", "required": ["memory_id", "receipt_id", "text"],
        "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1},
                        "receipt_id": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "minLength": 1},
                        "keep_wide": {"type": "boolean"}},
    },
    "memory.recollections.revise": {
        "type": "object", "required": ["recollection_id", "text"],
        "additionalProperties": False,
        "properties": {"recollection_id": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "minLength": 1}},
    },
    "memory.recollections.list": {
        "type": "object", "required": ["memory_id"], "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1},
                        "include_history": {"type": "boolean"}},
    },
    "memory.our_words.append": {
        "type": "object", "required": ["memory_id", "words"],
        "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1},
                        "words": {"type": "array", "minItems": 1, "items": {
                            "type": "object",
                            "required": ["speaker", "text"],
                            "properties": {
                                "speaker": {"type": "string",
                                             "enum": ["jiaming", "qiaosheng"]},
                                "text": {"type": "string", "minLength": 1},
                                # F-J-24（她批准 2026-10-06）：开放事后追加的
                                # 两字段——服务端 provenance 校验不放松
                                # （source_msg: 必须解析到已发布消息；概括
                                # 不得标 verbatim）
                                "expression_kind": {"type": "string",
                                                     "enum": ["verbatim",
                                                              "paraphrase",
                                                              "unspecified"]},
                                "source_ref": {"anyOf": [
                                    {"type": "string"},
                                    {"type": "null"}]}}}}},
    },
    "memory.our_words.list": {
        "type": "object", "required": ["memory_id"], "additionalProperties": False,
        "properties": {"memory_id": {"type": "string", "minLength": 1}},
    },
    "memory.categories.replace": {
        "type": "object", "required": ["memory_id", "categories"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "categories": {"type": "array", "minItems": 1, "items": {
                "type": "string", "enum": ["daily", "milestone", "sad", "sweet",
                                            "date", "plan", "sex", "anniversary",
                                            "reloplay"]}}},
    },
    "i.get": {"type": "object", "properties": {}, "additionalProperties": False},
    "i.write": {
        "type": "object", "required": ["content"], "additionalProperties": False,
        "properties": {"content": {"type": "string", "minLength": 1},
                        "expected_version": {"type": "integer"}},
    },
    "i.versions.read": {"type": "object", "properties": {},
                         "additionalProperties": False},
    "i.items.list": {"type": "object", "properties": {},
                       "additionalProperties": False},
    "i.item.get": {
        "type": "object", "required": ["item_id"],
        "additionalProperties": False,
        "properties": {"item_id": {"type": "string", "minLength": 1}},
    },
    "i.item.history": {
        "type": "object", "required": ["item_id"],
        "additionalProperties": False,
        "properties": {"item_id": {"type": "string", "minLength": 1}},
    },
    "i.item.create": {
        "type": "object", "required": ["content"],
        "additionalProperties": False,
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "change_reason": {"type": "string"},
            "relations": {"type": "array", "items": {
                "type": "object", "required": ["memory_id", "relation_type"],
                "additionalProperties": False,
                "properties": {
                    "memory_id": {"type": "string", "minLength": 1},
                    # CB-035：canonical 为 related_to（与读结果/
                    # relations.list 一致）；related 保留为旧客户端
                    # alias（service 层映射到 related_to）
                    "relation_type": {"type": "string", "enum": [
                        "changed_because_of", "clarified_by",
                        "informed_by", "related_to", "related"]}}}},
        },
    },
    "i.item.revise": {
        "type": "object",
        "required": ["item_id", "content", "expected_revision"],
        "additionalProperties": False,
        "properties": {
            "item_id": {"type": "string", "minLength": 1},
            "content": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 1},
            "change_reason": {"type": "string"},
            "informed_by_revision": {"type": "integer", "minimum": 1},
            "relations": {"type": "array", "items": {
                "type": "object", "required": ["memory_id", "relation_type"],
                "additionalProperties": False,
                "properties": {
                    "memory_id": {"type": "string", "minLength": 1},
                    # CB-035：canonical 为 related_to（与读结果/
                    # relations.list 一致）；related 保留为旧客户端
                    # alias（service 层映射到 related_to）
                    "relation_type": {"type": "string", "enum": [
                        "changed_because_of", "clarified_by",
                        "informed_by", "related_to", "related"]}}}},
        },
    },
    "i.item.restore": {
        "type": "object",
        "required": ["item_id", "restore_revision", "expected_revision"],
        "additionalProperties": False,
        "properties": {
            "item_id": {"type": "string", "minLength": 1},
            "restore_revision": {"type": "integer", "minimum": 1},
            "expected_revision": {"type": "integer", "minimum": 1},
            "change_reason": {"type": "string"},
            "relations": {"type": "array", "items": {
                "type": "object", "required": ["memory_id", "relation_type"],
                "additionalProperties": False,
                "properties": {
                    "memory_id": {"type": "string", "minLength": 1},
                    # CB-035：canonical 为 related_to（与读结果/
                    # relations.list 一致）；related 保留为旧客户端
                    # alias（service 层映射到 related_to）
                    "relation_type": {"type": "string", "enum": [
                        "changed_because_of", "clarified_by",
                        "informed_by", "related_to", "related"]}}}},
        },
    },
    "i.suggest": {
        "type": "object", "required": ["content"], "additionalProperties": False,
        "properties": {"content": {"type": "string", "minLength": 1}},
    },
    "i.suggestions.list": {
        "type": "object", "additionalProperties": False,
        "properties": {"status": {"type": "string",
                                   "enum": ["open", "accepted", "dismissed"]}},
    },
    "memory.recall": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "cursor": {"type": "array", "items": {"type": "string"}},
            "filters": {"type": "object", "additionalProperties": False,
                         "properties": {
                             "categories": {"type": "array", "items": {
                                 "type": "string"}},
                             "category_match": {"type": "string",
                                                 "enum": ["any", "all"]},
                             "mood_tags": {"type": "array", "items": {
                                 "type": "string"}},
                             "mood_match": {"type": "string",
                                             "enum": ["any", "all"]},
                             "event_date": {"type": "object",
                                             "additionalProperties": False,
                                             "properties": {
                                                 "from": {"type": "string"},
                                                 "to": {"type": "string"}}}}}},
    },
    # ---------- v1.3/v1.4 召回运行时（Recall Session + words 通道） ----------
    # query_plan 为自由对象：字段级校验（通道白名单/枚举/冲突检测）由
    # recall.models.validate_query_plan 在服务端执行。
    "memory.recall.round2": {
        "type": "object", "required": ["session_id", "reason", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "conversation_scope": {"type": "string"},
            "offset": {"type": "integer"},
            "continuation_token": {"type": "string", "minLength": 1},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.start": {
        "type": "object",
        # 裁定（2026-10-04）：operation_id 与 request_ref 二选一——
        # request_ref 本身就是一次具体请求的幂等身份
        "required_oneof": [["operation_id", "request_ref"]],
        "additionalProperties": False,
        "properties": {
            "query_plan": {"type": "object"},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1},
            "request_ref": {"type": "string", "minLength": 1}},
    },
    # MANUAL_HANDOFF_JUDGE_SWITCH_V1（2026-10-08）：关闭模式续页——
    # 游标由服务端签发；result_set_id 必填防"猜集合"枚举
    "memory.recall.page": {
        "type": "object", "required": ["result_set_id"],
        "additionalProperties": False,
        "properties": {
            "result_set_id": {"type": "string", "minLength": 1},
            "session_id": {"type": "string", "minLength": 1},
            "conversation_scope": {"type": "string"},
            "cursor": {"type": "string", "minLength": 1}},
    },
    "maintenance.recall_policy.get": {
        "type": "object", "additionalProperties": False,
        "properties": {},
    },
    "maintenance.recall_policy.update": {
        "type": "object",
        "required": ["expected_revision", "enabled"],
        "additionalProperties": False,
        "properties": {
            "expected_revision": {"type": "integer", "minimum": 0},
            "enabled": {"type": "boolean"},
            # null = 未选择 provider（关闭态保留配置的清空写法）
            "provider": {"anyOf": [
                {"type": "string", "enum": ["typesafe_jev",
                                            "codex_sdk", ""]},
                {"type": "null"}]},
            "model_id": {"anyOf": [{"type": "string"},
                                   {"type": "null"}]},
            "idempotency_key": {"type": "string", "minLength": 1}},
    },
    "memory.recall.refine": {
        "type": "object",
        "required": ["session_id", "query_plan"],
        "required_oneof": [["operation_id", "request_ref"]],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "query_plan": {"type": "object"},
            "expected_revision": {"type": "integer"},
            "continue_request_ref": {"type": "string", "minLength": 1},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1},
            "request_ref": {"type": "string", "minLength": 1}},
    },
    "memory.recall.reject": {
        "type": "object", "required": ["session_id", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "candidate_ref": {"type": "string"},
            "resource_ref": {"type": "string"},
            "reject_target": {"type": "string",
                               "enum": ["candidate", "event", "word",
                                         "source_selection"]},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.accept": {
        "type": "object", "required": ["session_id", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "candidate_ref": {"type": "string"},
            "close": {"type": "boolean"},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.navigate": {
        "type": "object", "required": ["session_id", "direction", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "direction": {"type": "string",
                           "enum": ["earlier", "later"]},
            "anchor_candidate_ref": {"type": "string"},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.status": {
        "type": "object", "required": ["session_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "conversation_scope": {"type": "string"}},
    },
    "memory.recall.close": {
        "type": "object", "required": ["session_id", "operation_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "outcome": {"type": "string",
                         "enum": ["resolved", "cancelled"]},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.words.recall": {
        "type": "object", "required": ["operation_id"], "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "original_request": {"type": "string"},
            "semantic_query": {"type": "string", "minLength": 1},
            "lexical_terms": {"type": "array",
                               "items": {"type": "string", "minLength": 1}},
            "exact_phrases": {"type": "array",
                               "items": {"type": "string", "minLength": 1}},
            "explicit_constraints": {"type": "object"},
            "explicit_negative_constraints": {"type": "object"},
            "operation_id": {"type": "string", "minLength": 1},
            "limit": {"type": "integer"}},
    },
    "memory.find_words": {
        "type": "object", "required": ["operation_id"],
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "original_request": {"type": "string"},
            "query_plan": {"type": "object"},
            "semantic_query": {"type": "string", "minLength": 1},
            "lexical_terms": {"type": "array",
                               "items": {"type": "string", "minLength": 1}},
            "exact_phrases": {"type": "array",
                               "items": {"type": "string", "minLength": 1}},
            "explicit_constraints": {"type": "object"},
            "explicit_negative_constraints": {"type": "object"},
            "limit": {"type": "integer"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.words.get": {
        "type": "object", "required": ["word_id"],
        "additionalProperties": False,
        "properties": {"word_id": {"type": "string", "minLength": 1}},
    },
    "memory.context.validate": {
        "type": "object", "required": ["resource_refs"],
        "additionalProperties": False,
        "properties": {"resource_refs": {"type": "array", "minItems": 1,
                                          "items": {"type": "string",
                                                     "minLength": 1}}},
    },
    # workspace.forgetting.* / workspace.review.*（v1.7 2026-09-28 整体
    # 退役）的 4 个死 schema 键已删（审计 1005B：schema_for 永远查不到
    # 它们——REGISTRY 不再注册这些能力）
}


def schema_for(capability: str) -> dict | None:
    # 裁定（2026-10-06 她/林石见批准）：V2_INPUT_SCHEMAS 是公开输入校验的
    # **唯一运行时正本**——不再回退历史 execution_pack v1.1 schema（历史合同
    # 绝不能重新成为当前运行时真源）。load_schemas() 仅存考古用途。
    return V2_INPUT_SCHEMAS.get(capability)


def validate(capability: str, arguments: dict) -> None:
    """按规格 schema 严格校验；违规抛 FORBIDDEN(SCHEMA_VIOLATION)。"""
    sch = schema_for(capability)
    if not sch:
        return
    if not isinstance(arguments, dict):
        raise Forbidden("arguments must be an object",
                        code="SCHEMA_VIOLATION", capability=capability)
    props = sch.get("properties", {})
    # 裁定（2026-10-04）：二选一必填组——组内至少一个非空字段；
    # request_ref 的既有合法位置含 query_plan 内（QueryPlan 字段）
    def _group_value(field: str):
        v = arguments.get(field)
        if v in (None, "") and field == "request_ref" and                 isinstance(arguments.get("query_plan"), dict):
            v = arguments["query_plan"].get("request_ref")
        return v

    for group in sch.get("required_oneof", []):
        if not any(_group_value(g) not in (None, "") for g in group):
            raise Forbidden(
                f"missing required field (one of {group})",
                code="SCHEMA_VIOLATION", capability=capability,
                fields=list(group))
    for req in sch.get("required", []):
        if req not in arguments or arguments[req] in (None, ""):
            raise Forbidden(f"missing required field: {req}",
                            code="SCHEMA_VIOLATION", capability=capability,
                            field=req)
    if sch.get("additionalProperties") is False:
        for k in arguments:
            if k not in props:
                raise Forbidden(f"unexpected field: {k}",
                                code="SCHEMA_VIOLATION", capability=capability,
                                field=k)
    for k, v in arguments.items():
        spec = props.get(k)
        if spec is None:
            continue
        if not _matches(spec, v, sch):
            raise Forbidden(f"field {k} does not match schema",
                            code="SCHEMA_VIOLATION", capability=capability,
                            field=k)
