"""Source Adapter 抽象（实施规格 §3）。

每个 provider 一个 adapter：detect 识别格式，normalize 把 provider 原始
conversation 标准化为可入库结构。本轮实现 Claude；base + registry 结构
保证未来可接入其他 provider（如 ChatGPT），不需要改 importer/query。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

NORMALIZED_SENDERS = ("human", "assistant", "system", "tool", "unknown")

#: speaker 映射（实施规格 §4）：只有 human/assistant 有正式说话人；
#: system/tool/unknown 一律 NULL，绝不猜测。
SPEAKER_MAP = {"human": "qiaosheng", "assistant": "jiaming"}


@dataclass
class NormalizedMessage:
    provider_message_id: str
    id_synthetic: bool
    parent_provider_message_id: str | None
    raw_sender: str | None
    normalized_sender: str
    provider_conversation_id: str = ""
    created_at: str | None = None          # provider 原始时间字符串，不改写
    updated_at: str | None = None
    occurred_date: str | None = None       # 业务时区自然日
    text: str = ""                         # 仅 human/assistant 可见正文
    content_json: str | None = None        # 原消息 content 证据（大 base64 截断）
    attachments: list[dict] = field(default_factory=list)
    has_thinking: bool = False
    has_tool_content: bool = False
    sequence: int = 0


@dataclass
class ConversationDraft:
    provider_conversation_id: str
    id_synthetic: bool
    title: str | None
    created_at: str | None
    updated_at: str | None
    messages: list[Any] = field(default_factory=list)


class SourceAdapter(Protocol):
    provider: str

    def detect(self, element: Any) -> bool:
        """顶层元素形状是否属于本 provider。"""
        ...

    def normalize_conversation(self, element: Any) -> ConversationDraft:
        """原始 conversation dict -> ConversationDraft（messages 保持原始形态，
        由 normalize_message 逐条标准化）。"""
        ...

    def normalize_messages(self, draft: ConversationDraft,
                           ) -> Iterator[NormalizedMessage]:
        ...
