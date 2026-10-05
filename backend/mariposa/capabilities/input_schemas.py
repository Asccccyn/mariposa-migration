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

_CACHE: dict | None = None


def load_schemas() -> dict:
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
            "why_remember": {"anyOf": [{"type": "string"},
                                      {"type": "null"}]},
            "memory_date": {"anyOf": [{"type": "string"},
                                     {"type": "null"}]},
            "date_confidence": {"type": "string",
                                "enum": ["exact", "inferred",
                                         "unknown"]}},
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
        "properties": {"limit": {"type": "integer", "minimum": 1,
                                 "maximum": 1000}},
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
                                       "enum": ["chat_message"]},
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
                                   "enum": ["claude_chat", "cc"]},
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
    "memory.hold": {
        "type": "object",
        "required": ["text"],
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "why_remember": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "memory_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "date_confidence": {"type": "string",
                                 "enum": ["exact", "inferred", "unknown"]},
            "occurred_start": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "occurred_end": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "raw_pending": {"type": "boolean"},
            "original_title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "categories": {"type": "array", "items": {"type": "string", "enum": [
                "daily", "milestone", "sad", "sweet", "date", "plan", "sex",
                "anniversary", "reloplay"]}},
            "plan_ids": {"type": "array",
                         "items": {"type": "string", "minLength": 1}},
            "mood": {"type": "object", "additionalProperties": False,
                      "properties": {"text": {"type": "string"},
                                     "tags": {"type": "array",
                                              "items": {"type": "string"}}}},
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
        },
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
                                "text": {"type": "string", "minLength": 1}}}}},
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
    "memory.mood.write": {
        "type": "object", "required": ["memory_id"],
        "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string", "minLength": 1},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "tags": {"type": "array", "items": {"type": "string"}}
        }
    },
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
    # v1.1 包 schema 曾要求 policy_version 必填，与处理器"缺省=按当前策略"
    # 的行为不符且打断既有调用点；v2 层对齐处理器语义（提供则校验）。
    "workspace.forgetting.scan": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "policy_version": {"type": "string"},
            "min_idle_days": {"type": "integer"},
            "cursor": {"type": "array", "items": {"type": "string"}},
        },
    },
    "workspace.forgetting.generate": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "memory_id": {"type": "string"},
            "candidate_summary": {"type": "string"},
            "candidate_tags": {"type": "array", "items": {"type": "string"}},
        },
    },
    "workspace.review.submit": {
        "type": "object", "required": ["item_id", "decision",
                                        "expected_revision"],
        "additionalProperties": False,
        "properties": {
            "item_id": {"type": "string", "minLength": 1},
            "decision": {"type": "string", "enum": ["release", "escalate_retain",
                                                     "escalate_owner",
                                                     "escalate_jiaming"]},
            "expected_revision": {"type": "integer"},
            "candidate_hash": {"type": "string"},
        },
    },
    "workspace.review.revise": {
        "type": "object", "required": ["item_id", "changes"],
        "additionalProperties": False,
        "properties": {
            "item_id": {"type": "string", "minLength": 1},
            "changes": {
                "type": "object", "minProperties": 1,
                "additionalProperties": False,
                "properties": {
                    "summary_body": {"type": "string", "minLength": 1},
                    "forget_tags": {"type": "array",
                                    "items": {"type": "string"}},
                },
            },
        },
    },
}


def schema_for(capability: str) -> dict | None:
    if capability in V2_INPUT_SCHEMAS:
        return V2_INPUT_SCHEMAS[capability]
    return load_schemas().get(capability)


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
