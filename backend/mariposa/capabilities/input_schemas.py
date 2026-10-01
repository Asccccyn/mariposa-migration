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
            "raw_refs": {"type": "array"},
            "raw_pending": {"type": "boolean"},
            "original_title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "categories": {"type": "array", "items": {"type": "string", "enum": [
                "daily", "milestone", "sad", "sweet", "date", "plan", "sex",
                "anniversary", "reloplay"]}},
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
                    "relation_type": {"type": "string", "enum": [
                        "changed_because_of", "clarified_by",
                        "informed_by", "related"]}}}},
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
                    "relation_type": {"type": "string", "enum": [
                        "changed_because_of", "clarified_by",
                        "informed_by", "related"]}}}},
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
                    "relation_type": {"type": "string", "enum": [
                        "changed_because_of", "clarified_by",
                        "informed_by", "related"]}}}},
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
    "memory.recall.start": {
        "type": "object", "required": ["query_plan"],
        "additionalProperties": False,
        "properties": {
            "query_plan": {"type": "object"},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.refine": {
        "type": "object", "required": ["session_id", "query_plan"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "query_plan": {"type": "object"},
            "expected_revision": {"type": "integer"},
            "continue_request_ref": {"type": "string", "minLength": 1},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.reject": {
        "type": "object", "required": ["session_id"],
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
        "type": "object", "required": ["session_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "candidate_ref": {"type": "string"},
            "close": {"type": "boolean"},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.navigate": {
        "type": "object", "required": ["session_id", "direction"],
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
        "properties": {"session_id": {"type": "string", "minLength": 1}},
    },
    "memory.recall.close": {
        "type": "object", "required": ["session_id"],
        "additionalProperties": False,
        "properties": {
            "session_id": {"type": "string", "minLength": 1},
            "outcome": {"type": "string",
                         "enum": ["resolved", "cancelled"]},
            "conversation_scope": {"type": "string"},
            "operation_id": {"type": "string", "minLength": 1}},
    },
    "memory.words.recall": {
        "type": "object", "additionalProperties": False,
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
            "limit": {"type": "integer"}},
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
