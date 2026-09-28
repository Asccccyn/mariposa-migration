"""顶层 JSON 数组的增量解析（复核 v1.1 §2.1 严格化）。

严格文法，不静默修复母本：
- UTF-8 严格解码（容忍 BOM）；坏字节抛错，不替换 U+FFFD 继续成功。
- 完整检查顶层文法：拒绝尾逗号、缺逗号、NaN/Infinity、闭合后非空白。
- 单元素大小受 SOURCE_MAX_ELEMENT_BYTES 约束（超限拒绝，不吃满内存）。

仍然增量：1MB 块 + raw_decode 逐元素，内存上界 = 单元素 + 1 块。
"""
from __future__ import annotations

import io
import json
from typing import Any, Iterator

from .. import config

_CHUNK = 1 << 20


class JsonStreamError(ValueError):
    """流不是合法的顶层 JSON 数组（含坏 UTF-8 / 超限）。"""

    def __init__(self, message: str, code: str = "SOURCE_BAD_JSON"):
        super().__init__(message)
        self.code = code


#: 空数组哨兵：与「首元素为 JSON null」区分
EMPTY_ARRAY = object()


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant not allowed: {name}")


def iter_top_level_array(stream) -> Iterator[Any]:
    """逐元素产出顶层 JSON 数组的元素；任何不合规立即抛 JsonStreamError。"""
    raw = stream
    try:
        is_bytes = isinstance(raw.read(0), bytes)
    except Exception as e:  # noqa: BLE001 —— 不可读流统一结构化拒绝
        raise JsonStreamError(f"unreadable stream: {type(e).__name__}") from e
    if is_bytes:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="strict")
    else:
        text = raw  # 文本流由调用方保证严格解码

    dec = json.JSONDecoder(parse_constant=_reject_constant)
    buf = ""
    pos = 0
    eof = False
    max_element = config.SOURCE_MAX_ELEMENT_BYTES

    def fill() -> bool:
        nonlocal buf, eof, pos
        if eof:
            return False
        try:
            chunk = text.read(_CHUNK)
        except UnicodeDecodeError as e:
            raise JsonStreamError(f"invalid UTF-8: {e}") from e
        if not chunk:
            eof = True
            return False
        buf = buf[pos:] + chunk
        pos = 0
        return True

    def skip_ws() -> bool:
        """跳过空白；返回 False 表示到达 EOF。"""
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos < len(buf):
                return True
            if not fill():
                return False

    if not skip_ws():
        raise JsonStreamError("empty stream: not a JSON array")
    if buf[pos] != "[":
        raise JsonStreamError(
            f"top-level JSON must be an array, got {buf[pos]!r}")
    pos += 1

    yielded = 0
    first = True
    while True:
        if not skip_ws():
            raise JsonStreamError("unterminated JSON array")
        if buf[pos] == "]":
            if not first:
                raise JsonStreamError(
                    f"trailing comma before ']' at element {yielded}")
            pos += 1
            break
        while True:
            if len(buf) - pos > max_element:
                raise JsonStreamError(
                    f"element {yielded} exceeds SOURCE_MAX_ELEMENT_BYTES",
                    code="SOURCE_ELEMENT_TOO_LARGE")
            try:
                obj, end = dec.raw_decode(buf, pos)
                break
            except json.JSONDecodeError:
                if fill():
                    continue
                raise JsonStreamError(
                    f"malformed JSON at element {yielded}") from None
            except ValueError as e:  # parse_constant 拒绝 NaN/Infinity
                raise JsonStreamError(
                    f"invalid JSON value at element {yielded}: {e}") from e
        yield obj
        yielded += 1
        pos = end
        first = False
        # 元素后必须是 ',' 或 ']'（拒绝缺逗号）
        if not skip_ws():
            raise JsonStreamError("unterminated JSON array")
        if buf[pos] == ",":
            pos += 1
            continue
        if buf[pos] == "]":
            pos += 1
            break
        raise JsonStreamError(
            f"expected ',' or ']' after element {yielded}, got {buf[pos]!r}")

    # 闭合后仅允许空白直到 EOF（拒绝尾随垃圾）
    while True:
        if pos < len(buf):
            rest = buf[pos:]
            if rest.strip():
                raise JsonStreamError("trailing content after closing ']'")
        if not fill():
            return
        pos = 0


def peek_first_element(stream):
    """取出第一个元素与继续消费同一流的迭代器。

    空数组返回 (EMPTY_ARRAY, it)；首元素为 JSON null 返回 (None, it)——
    两者必须区分（null 是非法元素，不是空数组）。
    """
    it = iter_top_level_array(stream)
    try:
        first = next(it)
    except StopIteration:
        return EMPTY_ARRAY, it
    return first, it
