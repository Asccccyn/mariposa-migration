"""md 对话转写解析器（裁定 2026-10-04 四——合同见 CURRENT §6）。

把两种 md 导出方言解析成与 Claude JSON 导入**同形**的元素契约
（{"uuid", "title", "created_at", "chat_messages": [...]}），下游
（归档/门禁/批次/绑定）零改动复用。

方言：
- claude：`## User`/`## Claude` 说话人标题 + `**ISO-Z**` 时间戳行 +
  头部 `**Created:**`/`**Updated:**`/`**Link:**` + `### Thinking`
  围栏块；
- gemini：`# you asked`/`# gemini response` 标题 + `message time:`
  裸时间（按 UTC+8 解读）+ `> From:` 来源 URL。

口径：时间戳行剥离出原文；human=qiaosheng / assistant=jiaming；
Thinking 分离不进正文；消息 id 内容锚定确定性合成；围栏感知切块。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..errors import MariposaError

_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

_CLAUDE_HEADINGS = {"## User": "human", "## Claude": "assistant"}
_GEMINI_HEADINGS = {"# you asked": "human", "# gemini response": "assistant"}

_TS_CLAUDE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
                        r"(?:\.\d+)?Z)\*\*\s*$")
_TS_GEMINI = re.compile(r"^message time:\s*(\d{4}-\d{2}-\d{2}"
                        r"[ T]\d{2}:\d{2}:\d{2})\s*$")
_LINK = re.compile(r"\*\*Link:\*\*\s*\[?([^\]\s]+)")
_FROM = re.compile(r"^>\s*From:\s*(\S+)")
_HEADER_KV = re.compile(r"^\*\*(Created|Updated|Exported|Model):\*\*"
                        r"\s*(.+)$")


def _detect_dialect(text: str) -> str | None:
    if any(line.strip() in _CLAUDE_HEADINGS for line in text.split("\n")):
        return "claude"
    if any(line.strip() in _GEMINI_HEADINGS for line in text.split("\n")):
        return "gemini"
    return None


def _iter_blocks(lines: list[str], headings: dict[str, str]):
    """围栏感知切块：按说话人标题切，代码围栏内的标题行不切。"""
    fence = None
    current = None  # (sender, header_lineno)
    body: list[str] = []
    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        # 围栏状态机（```/````/~~~ 同字符≥3 连续）
        if fence is None:
            m = re.match(r"^(`{3,}|~{3,})", stripped)
            if m:
                fence = m.group(1)[0] * 3
        elif re.match(rf"^{fence}+", stripped):
            fence = None
        if fence is None and stripped in headings:
            if current is not None:
                yield current, body
            current = (headings[stripped], lineno)
            body = []
            continue
        if current is not None:
            body.append(raw)
    if current is not None:
        yield current, body


def _det_id(kind: str, conv_id: str, index: int) -> str:
    h = hashlib.sha256(f"{kind}:{conv_id}:{index}".encode()).hexdigest()[:16]
    return f"mdm-{h}"


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_claude(lines: list[str], filename: str) -> list[dict]:
    title = None
    conv_created = conv_updated = None
    conv_uuid = None
    i = 0
    # 头部：标题行 + **K:** V 元数据，直到首个说话人标题或 ---
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped in _CLAUDE_HEADINGS:
            break
        if stripped.startswith("# ") and title is None:
            title = stripped[2:].strip()
            continue
        m = _HEADER_KV.match(stripped)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if key in ("Created", "Updated"):
                try:
                    dt = datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
                    dt = dt.replace(tzinfo=_TZ_SHANGHAI)
                except ValueError:
                    dt = None
                if dt is not None:
                    if key == "Created":
                        conv_created = _to_utc_iso(dt)
                    else:
                        conv_updated = _to_utc_iso(dt)
            continue
        lm = _LINK.match(stripped)
        if lm:
            conv_uuid = lm.group(1).rstrip("/").split("/")[-1]
    body_lines = lines[i:] if i < len(lines) else []
    # 会话 id 兜底：文件名锚定
    if not conv_uuid:
        conv_uuid = f"mdc-{hashlib.sha256(filename.encode())
                                  .hexdigest()[:16]}"
    messages = []
    prev_uuid = None
    for (sender, _), mlines in _iter_blocks(body_lines, _CLAUDE_HEADINGS):
        created = None
        thinking = None
        text_lines: list[str] = []
        j = 0
        while j < len(mlines):
            stripped = mlines[j].strip()
            ts = _TS_CLAUDE.match(stripped)
            if ts and created is None:
                created = ts.group(1)
                j += 1
                continue
            if stripped == "### Thinking" and thinking is None:
                j += 1
                fence_lines = []
                opened = False
                while j < len(mlines):
                    s2 = mlines[j].strip()
                    if not opened and re.match(r"^`{3,}", s2):
                        opened = True
                        j += 1
                        continue
                    if opened and re.match(r"^`{3,}", s2):
                        j += 1
                        break
                    if opened:
                        fence_lines.append(mlines[j])
                    j += 1
                thinking = "\n".join(fence_lines).strip() or None
                continue
            text_lines.append(mlines[j])
            j += 1
        text = "\n".join(text_lines).strip()
        if not text and not thinking:
            continue
        content = []
        if thinking:
            content.append({"type": "thinking", "thinking": thinking})
        content.append({"type": "text", "text": text})
        mid = _det_id("claude", conv_uuid, len(messages))
        messages.append({
            "uuid": mid,
            "sender": sender,
            "created_at": created or conv_created,
            "content": content,
            **({"parent_message_uuid": prev_uuid} if prev_uuid else {}),
        })
        prev_uuid = mid
    if not messages:
        raise MariposaError(
            "claude md 转写未解析出任何消息（需 ## User/## Claude 与"
            " **ISO-Z** 时间戳行的规整导出）", code="SOURCE_MD_FORMAT")
    element = {
        "uuid": conv_uuid,
        "title": title,
        "created_at": conv_created,
        "updated_at": conv_updated,
        "chat_messages": messages,
    }
    return [element]


def _parse_gemini(lines: list[str], filename: str) -> list[dict]:
    conv_uuid = None
    for raw in lines:
        m = _FROM.match(raw.strip())
        if m:
            conv_uuid = m.group(1).rstrip("/").split("/")[-1]
            break
    if not conv_uuid:
        conv_uuid = f"mdg-{hashlib.sha256(filename.encode())
                                  .hexdigest()[:16]}"
    messages = []
    prev_uuid = None
    for (sender, _), mlines in _iter_blocks(lines, _GEMINI_HEADINGS):
        created = None
        text_lines = []
        for raw in mlines:
            m = _TS_GEMINI.match(raw.strip())
            if m and created is None:
                dt = datetime.fromisoformat(m.group(1))
                dt = dt.replace(tzinfo=_TZ_SHANGHAI)
                created = _to_utc_iso(dt)
                continue
            text_lines.append(raw)
        text = "\n".join(text_lines).strip()
        if not text:
            continue
        mid = _det_id("gemini", conv_uuid, len(messages))
        messages.append({
            "uuid": mid,
            "sender": sender,
            "created_at": created,
            "content": [{"type": "text", "text": text}],
            **({"parent_message_uuid": prev_uuid} if prev_uuid else {}),
        })
        prev_uuid = mid
    if not messages:
        raise MariposaError(
            "gemini md 转写未解析出任何消息（需 # you asked/"
            " # gemini response 与 message time: 行的规整导出）",
            code="SOURCE_MD_FORMAT")
    stem = Path(filename).stem
    return [{
        "uuid": conv_uuid,
        "title": stem or None,
        "created_at": messages[0].get("created_at"),
        "updated_at": messages[-1].get("created_at"),
        "chat_messages": messages,
    }]


def parse(staged: Path, filename: str) -> tuple[str, list[dict]]:
    """解析 md 转写 → (provider, elements)。不识别的结构化拒绝。"""
    text = staged.read_text(encoding="utf-8", errors="strict")
    dialect = _detect_dialect(text)
    if dialect is None:
        raise MariposaError(
            "无法识别 md 转写方言（本轮支持 claude/gemini 导出格式）",
            code="SOURCE_MD_FORMAT")
    lines = text.split("\n")
    if dialect == "claude":
        return "claude", _parse_claude(lines, filename)
    return "gemini", _parse_gemini(lines, filename)


def provider_module_for(provider: str):
    """md 两种方言产出与 claude 契约同形的元素——统一走 claude
    适配器的 normalize 链（sender/时间/synthetic 兜底一致）。"""
    from .adapters import claude
    return claude
