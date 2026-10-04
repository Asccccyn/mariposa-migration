"""gemini 提供方适配器（md 转写，裁定 2026-10-04 四）。

md_transcript 的 gemini 方言产出与 Claude JSON 契约同形的元素
（uuid/chat_messages/sender/created_at/content），normalize 链直接
复用 claude 实现——sender 映射、时间解析、synthetic 兜底、speaker
归属全部一致，不另立合同。
"""
from __future__ import annotations

from . import claude

PROVIDER = "gemini"

detect = claude.detect
normalize_conversation = claude.normalize_conversation
normalize_messages = claude.normalize_messages
synthetic_conversation_id = claude.synthetic_conversation_id
synthetic_message_id = claude.synthetic_message_id
speaker_of = claude.speaker_of
parse_created_at = claude.parse_created_at
