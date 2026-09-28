"""Claude Export adapter（实施规格 §3—§6）。

解析规则改编自《聊天记录查看器 v4.9》的 detectFormat / getMessages /
extractMessageText（前端 UI 版），按数据保存语义重写：

- 消息身份 = provider message UUID；绝不用「文本+时间」。
- raw_sender 保存原值；normalized_sender 只做**精确**枚举映射，
  查看器「未知角色降级成 assistant」的 UI 规则**禁止**进入本层。
- text 只从 content 数组的 text block（及顶层 text 回退）提取，且仅当
  normalized_sender ∈ {human, assistant}；thinking / tool_use / tool_result
  只置标志 + 留 content_json 证据，不进正文。
- created_at 保存原始字符串；occurred_date 用业务时区（config.
  RELATIONSHIP_TIMEZONE）换算自然日，禁止 date→toISOString 跨日错位。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from ... import config
from .base import ConversationDraft, NormalizedMessage, SPEAKER_MAP

PROVIDER = "claude"

#: 精确映射；除这四个值外（含大小写/空白差异后）一律 unknown。
_SENDER_MAP = {"human": "human", "assistant": "assistant",
               "system": "system", "tool": "tool"}

#: content block 中 base64 payload 超过该长度即在 content_json 副本中
#: 截断（母本完整保留在 Raw Archive；截断只为防库膨胀）。
_BASE64_OMIT_OVER = 512


def detect(element: Any) -> bool:
    return (isinstance(element, dict)
            and isinstance(element.get("chat_messages"), list))


def normalize_conversation(element: Any) -> ConversationDraft:
    if not isinstance(element, dict):
        raise ValueError("conversation element must be an object")
    uuid = element.get("uuid")
    synthetic = not (isinstance(uuid, str) and uuid.strip())
    conv_id = uuid.strip() if not synthetic else None
    name = element.get("name") or element.get("title")
    return ConversationDraft(
        provider_conversation_id=conv_id if conv_id else "",
        id_synthetic=synthetic,
        title=name.strip() if isinstance(name, str) and name.strip() else None,
        created_at=_as_original_str(element.get("created_at")),
        updated_at=_as_original_str(element.get("updated_at")),
        messages=element.get("chat_messages") or [],
    )


def synthetic_conversation_id(batch_id: str, index: int) -> str:
    """缺失 conv UUID 时的确定性兜底身份：锚定批次+序号，不锚定文本。"""
    h = hashlib.sha256(f"{batch_id}:conv:{index}".encode()).hexdigest()[:20]
    return f"missing-conv-{h}"


def synthetic_message_id(batch_id: str, conv_id: str, seq: int) -> str:
    h = hashlib.sha256(
        f"{batch_id}:msg:{conv_id}:{seq}".encode()).hexdigest()[:20]
    return f"missing-msg-{h}"


def normalize_messages(draft: ConversationDraft, batch_id: str,
                       tz_name: str | None = None) -> Iterator[NormalizedMessage]:
    from ... import config as _config
    tz = ZoneInfo(tz_name or config.RELATIONSHIP_TIMEZONE)
    for seq, raw in enumerate(draft.messages):
        if not isinstance(raw, dict):
            # 元素必须是消息对象；42/字符串/null 不允许伪装成空消息
            raise ValueError(f"chat_messages[{seq}] is not an object")
        msg = _normalize_one(raw, draft, seq, batch_id, tz)
        # 消息级大小上限（复核 v1.1 §6）：超限拒绝该消息，不允许无界入库
        if len(msg.text.encode("utf-8")) > _config.SOURCE_MAX_MESSAGE_TEXT_BYTES:
            raise ValueError(
                f"chat_messages[{seq}] text exceeds "
                f"SOURCE_MAX_MESSAGE_TEXT_BYTES")
        if msg.content_json and len(msg.content_json.encode("utf-8")) > \
                _config.SOURCE_MAX_MESSAGE_CONTENT_BYTES:
            raise ValueError(
                f"chat_messages[{seq}] content exceeds "
                f"SOURCE_MAX_MESSAGE_CONTENT_BYTES")
        yield msg


def _normalize_one(raw: Any, draft: ConversationDraft, seq: int,
                   batch_id: str, tz: ZoneInfo) -> NormalizedMessage:
    if not isinstance(raw, dict):
        raw = {}
    uuid = raw.get("uuid")
    synthetic = not (isinstance(uuid, str) and uuid.strip())
    msg_id = (uuid.strip() if not synthetic
              else synthetic_message_id(batch_id,
                                        draft.provider_conversation_id, seq))

    raw_sender = raw.get("sender")
    normalized = _map_sender(raw_sender)

    created = _as_original_str(raw.get("created_at"))
    updated = _as_original_str(raw.get("updated_at"))
    occurred = _occurred_date(created, tz)

    text, attachments, has_thinking, has_tool = _extract_body(raw, normalized)
    content_json = _content_evidence(raw.get("content"))

    parent = raw.get("parent_message_uuid")
    return NormalizedMessage(
        provider_message_id=msg_id,
        id_synthetic=synthetic,
        parent_provider_message_id=(
            parent.strip() if isinstance(parent, str) and parent.strip()
            else None),
        raw_sender=(raw_sender if isinstance(raw_sender, str) else None),
        normalized_sender=normalized,
        provider_conversation_id=draft.provider_conversation_id,
        created_at=created,
        updated_at=updated,
        occurred_date=occurred,
        text=text,
        content_json=content_json,
        attachments=attachments,
        has_thinking=has_thinking,
        has_tool_content=has_tool,
        sequence=seq,
    )


def _map_sender(raw_sender: Any) -> str:
    if not isinstance(raw_sender, str):
        return "unknown"
    return _SENDER_MAP.get(raw_sender.strip().lower(), "unknown")


def speaker_of(normalized_sender: str) -> str | None:
    """human→江乔生(qiaosheng)，assistant→周家明(jiaming)，其余 NULL。"""
    return SPEAKER_MAP.get(normalized_sender)


def _as_original_str(value: Any) -> str | None:
    """时间字段保存 provider 原值（字符串）；非字符串丢弃。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_created_at(value: str | None) -> datetime | None:
    """ISO 时间解析；无时区按 UTC。解析失败返回 None（不猜）。"""
    if not value:
        return None
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _occurred_date(created_raw: str | None, tz: ZoneInfo) -> str | None:
    dt = parse_created_at(created_raw)
    if dt is None:
        return None
    return dt.astimezone(tz).date().isoformat()


def _extract_body(raw: dict, normalized: str) -> tuple[str, list[dict],
                                                       bool, bool]:
    """text/附件/标志提取（对照查看器 extractMessageText 的 content 分支）。

    正文只有 content 数组的 text block；**顶层 text 仅在 content 缺失/为空
    时回退**（复核 v1.1 SL-04：tool-only/thinking-only content 的顶层 text
    不得晋升为双方正文——那通常是工具输出/系统注入的镜像）。
    未知 block type 的文本不进正文（证据仍在 content_json）。
    正文仅对 human/assistant 开放，其余 sender 的 text 置空。
    """
    text_parts: list[str] = []
    attachments: list[dict] = []
    has_thinking = False
    has_tool = False
    content = raw.get("content")
    if isinstance(content, list) and content:
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type") or part.get("kind")
            if t == "text":
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
            elif t == "thinking":
                has_thinking = True
            elif t in ("tool_use", "tool_result", "server_tool_use",
                       "web_search_tool_result", "search_result"):
                has_tool = True
            elif t in ("document", "image", "attachment"):
                attachments.append(_attachment_ref(part))
    elif isinstance(content, str) and content:
        text_parts.append(content)  # 字符串 content 本身即正文
    elif not content:
        # content 缺失/空数组：顶层 text 才可作为正文回退
        if isinstance(raw.get("text"), str):
            text_parts.append(raw["text"])

    # 消息级 attachments 字段（部分导出存在）
    for att in raw.get("attachments") or []:
        if isinstance(att, dict):
            attachments.append(_attachment_ref({**att, "type": "attachment"}))

    text = "".join(text_parts)
    if normalized not in ("human", "assistant"):
        text = ""  # system/tool/unknown 的可见文本不进正文（规格 §5）
    return text, attachments, has_thinking, has_tool


def _attachment_ref(part: dict) -> dict:
    """附件引用：保留文件名/标识/尺寸，不带 base64 字节。"""
    ref: dict = {"block_type": str(part.get("type") or "attachment")}
    for key in ("file_id", "file_name", "filename", "title", "name",
                "mime_type", "media_type", "url"):
        if isinstance(part.get(key), str):
            ref[key] = part[key]
    src = part.get("source")
    if isinstance(src, dict):
        for key in ("type", "media_type", "file_id", "filename"):
            if isinstance(src.get(key), str):
                ref[f"source_{key}"] = src[key]
    return ref


def _content_evidence(content: Any) -> str | None:
    """content 原始结构入库（截断超长 base64；母本在 Raw Archive）。"""
    if content is None:
        return None
    cleaned = _strip_base64(content)
    return json.dumps(cleaned, ensure_ascii=False)


def _strip_base64(node: Any) -> Any:
    if isinstance(node, dict):
        src = node.get("source")
        if isinstance(src, dict) and isinstance(src.get("data"), str) \
                and len(src["data"]) > _BASE64_OMIT_OVER:
            node = {**node, "source": {
                **{k: v for k, v in src.items() if k != "data"},
                "omitted_base64_bytes": len(src["data"]),
                "note": "base64 payload omitted; full copy in Raw Archive",
            }}
        return {k: _strip_base64(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_base64(x) for x in node]
    return node
