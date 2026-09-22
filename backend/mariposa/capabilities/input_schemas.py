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


def schema_for(capability: str) -> dict | None:
    return load_schemas().get(capability)


def _resolve(spec: dict, root: dict) -> dict:
    """解 $ref（仅支持本文件 #/$defs/x）。"""
    ref = spec.get("$ref")
    if not ref:
        return spec
    name = ref.split("/")[-1]
    d = root.get("$defs", {}).get(name, {})
    return {**d, **{k: v for k, v in spec.items() if k != "$ref"}}


def _matches(spec: dict, value, root: dict) -> bool:
    """字段值是否满足该（已解析 $ref 的）spec；anyOf 任一即可。"""
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
    return True


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
