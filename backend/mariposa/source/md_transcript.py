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


def _fence_tokens(line: str):
    """行首围栏（` 或 ~ 同字符≥3）→ (字符, 长度)；否则 None。"""
    m = re.match(r"^(`{3,}|~{3,})", line)
    return (m.group(1)[0], len(m.group(1))) if m else None


def _valid_ts_claude(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _valid_ts_gemini(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def _iter_blocks(lines: list[str], headings: dict[str, str]):
    """围栏感知切块：按说话人标题切，代码围栏内的标题行不切。

    SRC-01（2026-10-04 全量审计 P1）：闭合围栏必须与开栏同字符且
    长度不小于开栏（CommonMark 规则）——此前把开栏统一压成 3 个
    反引号，四反引号外栏会被内部三反引号示例提前关闭，围栏内的
    ## User 被误切成新消息、正文归属错换。
    """
    fence = None  # (char, length)
    current = None
    body: list[str] = []
    for lineno, raw in enumerate(lines, 1):
        stripped = raw.strip()
        tok = _fence_tokens(stripped)
        if fence is None:
            if tok:
                fence = tok
        else:
            # 闭合：同字符且长度 ≥ 开栏
            if tok and tok[0] == fence[0] and tok[1] >= fence[1]:
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


def _split_fence(body: list[str]) -> tuple[list[str], list[str], bool]:
    """在首个闭合栏处切分围栏体（SRC-01）。

    仅当首非空行是合法开栏、且其后存在同字符且长度足够的闭合栏
    时成立；返回 (围栏内行, 围栏后的剩余行, 是否真围栏)。普通正文
    里的 ### Thinking 标题没有紧邻围栏 → 不成立，正文原样保留。
    """
    i = 0
    while i < len(body) and not body[i].strip():
        i += 1
    if i >= len(body):
        return [], body, False
    tok = _fence_tokens(body[i].strip())
    if not tok:
        return [], body, False
    depth = tok[1]
    for j in range(i + 1, len(body)):
        close = _fence_tokens(body[j].strip())
        if close and close[0] == tok[0] and close[1] >= depth:
            return body[i + 1:j], body[j + 1:], True
    return [], body, False


def _det_id(kind: str, conv_id: str, anchor: str) -> str:
    """内容锚定确定性 id（SRC-02：位置无关）。

    anchor = sender+时间+正文 的标准化串——相同消息重导复用身份、
    前插新消息不改变既有消息身份；同会话内逐字重复的消息以出现
    次序消歧（对相同内容稳定）。
    """
    h = hashlib.sha256(
        f"{kind}:{conv_id}:{anchor}".encode()).hexdigest()[:16]
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
    _seen_anchors: dict[str, int] = {}
    for (sender, _), mlines in _iter_blocks(body_lines, _CLAUDE_HEADINGS):
        created = None
        thinking = None
        text_lines: list[str] = []
        j = 0
        first_content = True  # 导出元数据位：时间戳只可能在块首
        while j < len(mlines):
            stripped = mlines[j].strip()
            ts = _TS_CLAUDE.match(stripped)
            if ts and first_content:
                if not _valid_ts_claude(ts.group(1)):
                    raise MariposaError(
                        f"claude md 时间戳非法: {ts.group(1)}",
                        code="SOURCE_MD_FORMAT")
                created = ts.group(1)
                j += 1
                first_content = False
                continue
            if (stripped == "### Thinking" and thinking is None
                    and sender == "assistant"):
                # SRC-01：Thinking 必须紧邻有效围栏——否则这只是
                # 普通正文标题，原样保留
                inner, rest, fenced = _split_fence(mlines[j + 1:])
                if fenced:
                    thinking = "\n".join(inner).strip() or None
                    mlines = rest
                    j = 0
                    first_content = False
                    continue
            text_lines.append(mlines[j])
            if stripped:
                first_content = False
            j += 1
        text = "\n".join(text_lines).strip()
        if not text and not thinking:
            continue
        content = []
        if thinking:
            content.append({"type": "thinking", "thinking": thinking})
        content.append({"type": "text", "text": text})
        from .. import config as _cfg
        _tbytes = len((text or "").encode("utf-8"))
        if _tbytes > _cfg.SOURCE_MAX_MESSAGE_TEXT_BYTES:
            raise MariposaError(
                f"md 消息正文超限（{_tbytes} > "
                f"{_cfg.SOURCE_MAX_MESSAGE_TEXT_BYTES}）",
                code="SOURCE_MD_FORMAT")
        _anchor = f"{sender}|{created or ''}|{text or ''}|{thinking or ''}"
        _occ = _seen_anchors.get(_anchor, 0)
        _seen_anchors[_anchor] = _occ + 1
        mid = _det_id("claude", conv_uuid,
                      f"{_anchor}#{_occ}" if _occ else _anchor)
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
    _seen_anchors: dict[str, int] = {}
    for (sender, _), mlines in _iter_blocks(lines, _GEMINI_HEADINGS):
        created = None
        text_lines = []
        first_content = True  # 导出元数据位：时间戳只可能在块首
        for raw in mlines:
            m = _TS_GEMINI.match(raw.strip())
            if m and first_content:
                if not _valid_ts_gemini(m.group(1)):
                    raise MariposaError(
                        f"gemini md 时间戳非法: {m.group(1)}",
                        code="SOURCE_MD_FORMAT")
                dt = datetime.fromisoformat(m.group(1))
                dt = dt.replace(tzinfo=_TZ_SHANGHAI)
                created = _to_utc_iso(dt)
                first_content = False
                continue
            if raw.strip():
                first_content = False
            text_lines.append(raw)
        text = "\n".join(text_lines).strip()
        if not text:
            continue
        from .. import config as _cfg
        _tbytes = len((text or "").encode("utf-8"))
        if _tbytes > _cfg.SOURCE_MAX_MESSAGE_TEXT_BYTES:
            raise MariposaError(
                f"md 消息正文超限（{_tbytes} > "
                f"{_cfg.SOURCE_MAX_MESSAGE_TEXT_BYTES}）",
                code="SOURCE_MD_FORMAT")
        _anchor = f"{sender}|{created or ''}|{text or ''}"
        _occ = _seen_anchors.get(_anchor, 0)
        _seen_anchors[_anchor] = _occ + 1
        mid = _det_id("gemini", conv_uuid,
                      f"{_anchor}#{_occ}" if _occ else _anchor)
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
    """解析 md 转写 → (provider, elements)。不识别的结构化拒绝。

    SRC-06：文件级字节上限在读入前按 stat 拒绝（json 路径的元素
    流式限额不覆盖 md）；逐消息文本在构造时即刻对照
    SOURCE_MAX_MESSAGE_TEXT_BYTES，超限先于全量装配失败。
    """
    from .. import config as _config
    _size = staged.stat().st_size if staged.exists() else 0
    if _size > _config.SOURCE_MD_MAX_BYTES:
        from ..errors import MariposaError as _ME
        raise _ME(
            f"md 转写超文件级上限（{_size} > "
            f"{_config.SOURCE_MD_MAX_BYTES}）", code="SOURCE_MD_FORMAT")
    try:
        text = staged.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError) as e:
        # SRC-09：坏字节/不可读在方言边界结构化拒绝（此前裸
        # ValueError/UnicodeError 逃逸，失败导入无留痕）
        raise MariposaError(f"md 转写不可读: {type(e).__name__}",
                            code="SOURCE_MD_FORMAT") from e
    dialect = _detect_dialect(text)
    if dialect is None:
        raise MariposaError(
            "无法识别 md 转写方言（本轮支持 claude/gemini 导出格式）",
            code="SOURCE_MD_FORMAT")
    try:
        if dialect == "claude":
            return "claude", _parse_claude(text.split("\n"), filename)
        return "gemini", _parse_gemini(text.split("\n"), filename)
    except MariposaError:
        raise
    except (ValueError, OverflowError) as e:
        raise MariposaError(
            f"md 转写内容非法（{type(e).__name__}: {e}）",
            code="SOURCE_MD_FORMAT") from e


def provider_module_for(provider: str):
    """md 两种方言产出与 claude 契约同形的元素——统一走 claude
    适配器的 normalize 链（sender/时间/synthetic 兜底一致）。"""
    from .adapters import claude
    return claude
