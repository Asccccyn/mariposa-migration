"""可检索投影：唯一检索入口（§8.1）。

active+full        -> hold_text/event_text 事件正文（S03 禁检：解释类文字不进投影）
active+forgotten   -> 仅审批通过的 compressed_summary，其余一律不进 search_text
hidden             -> 无投影、无 FTS 行

中文预分词：投影与查询统一按字符级切分后空格拼接（子串语义），
原始正文保持不变；FTS 查询以短语形式编译，用户输入不直接拼进 FTS 语法。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .. import config


def _is_cjk(ch: str) -> bool:
    return "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"


def tokenize(text: str) -> list[str]:
    """中文按单字、拉丁按连续词切分；输出即 FTS token 序列。"""
    tokens: list[str] = []
    buf: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(ch)
        elif ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf))
                buf = []
    if buf:
        tokens.append("".join(buf))
    return tokens


def normalize_search_text(text: str) -> str:
    return " ".join(tokenize(text)).lower()


def compile_query(query: str) -> str:
    """把用户输入编译成安全的 FTS5 短语：整体作为一个带引号的 phrase。

    输入内的双引号被剔除，任何 FTS 语法字符都失去特殊含义。
    """
    toks = [t.replace('"', "") for t in tokenize(query)]
    toks = [t for t in toks if t]
    if not toks:
        return ""
    return '"' + " ".join(t.lower() for t in toks) + '"'


def build_forgotten(compressed_summary: str) -> str:
    """遗忘桶：只有批准摘要可作为文本检索依据。"""
    return normalize_search_text(compressed_summary)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upsert(
    conn,
    memory_id: str,
    memory_version_no: int,
    projection_kind: str,
    search_text: str,
    whitelist_body: str | None = None,
) -> None:
    """替换该桶的有效投影并同步 FTS。必须在正式库事务内调用。

    whitelist_body：本投影的白名单主字段正文（full=事件正文、
    forgotten=summary_body），已 normalize。检索层据此如实标注
    matched_fields（event_text / summary_body / forget_tags /
    legacy_projection），不必也不允许回读 memory_versions。
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM search_fts WHERE memory_id=?", (memory_id,))
    conn.execute("DELETE FROM retrieval_documents WHERE memory_id=?", (memory_id,))
    conn.execute(
        "INSERT INTO retrieval_documents(memory_id, memory_version_no, projection_kind,"
        " search_text, search_text_hash, projection_revision, policy_version, updated_at,"
        " whitelist_body)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (
            memory_id, memory_version_no, projection_kind, search_text,
            sha256_text(search_text), config.PROJECTION_REVISION,
            config.POLICY_VERSION, now, whitelist_body,
        ),
    )
    conn.execute(
        "INSERT INTO search_fts(memory_id, search_text) VALUES(?,?)",
        (memory_id, search_text),
    )


def remove(conn, memory_id: str) -> None:
    """隐藏/归档时移除投影与 FTS 行。"""
    conn.execute("DELETE FROM search_fts WHERE memory_id=?", (memory_id,))
    conn.execute("DELETE FROM retrieval_documents WHERE memory_id=?", (memory_id,))
