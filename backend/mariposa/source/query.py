"""Source Layer 专项查询（复核 v1.1 定点补修版）。

- 分层边界不变：独立于普通 Recall；结果标 source=source_layer。
- 默认检索面 = 已发布（published=1）的 human/assistant 正文；失败/未
  发布批次数据默认不可见（诊断需显式 include_unpublished）。
- SL-03：excerpt/命中信息只从同一正文版本生成，绝不回退 evidence。
- SL-05：char offset 口径 = Unicode code point、半开区间 [start,end)；
  range 读取按口径裁切，响应不夹带未选内容。
- SL-06：语义连续区间必须位于同一 parent 路径；sibling 分支单列。
- SL-08：关键词匹配与所有过滤在同一候选集内完成后分页；LIKE 显式
  ESCAPE；has_more 来自 limit+1 真实下一条。
"""
from __future__ import annotations

import json

from .. import config, db
from ..errors import Forbidden, NotFound
from ..retrieval import projection

SPEAKER_DISPLAY = {"qiaosheng": "江乔生", "jiaming": "周家明"}

_DEFAULT_SENDERS = ("human", "assistant")
_ALL_SENDERS = ("human", "assistant", "system", "tool", "unknown")

#: 偏移口径声明（前端 JS 为 UTF-16，跨语言一律以本口径为准）
OFFSET_CONVENTION = "unicode_code_point_half_open"

_COLS = ("id, provider, provider_conversation_id, provider_message_id,"
         " id_synthetic, parent_provider_message_id, raw_sender,"
         " normalized_sender, speaker, created_at, updated_at, occurred_date,"
         " text, attachments, has_thinking, has_tool_content, sequence,"
         " import_batch_id, published, content_hash")


def search(query: str | None = None, *, senders: list[str] | None = None,
           provider: str | None = None, conversation_id: str | None = None,
           date_from: str | None = None, date_to: str | None = None,
           limit: int = 20, offset: int = 0) -> dict:
    """原文专项搜索：关键词 + 说话人 + 日期区间 + 会话过滤（同集分页）。"""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    sender_filter = _resolve_senders(senders)
    where, params = _base_filters(provider, conversation_id,
                                  date_from, date_to)
    where.append("m.published=1")

    keyword = (query or "").strip()
    matched_by = None
    if keyword:
        if set(sender_filter) <= set(_DEFAULT_SENDERS):
            # FTS 匹配作为主查询条件参与同一候选集（SL-08），不先截断取 ID
            where.append("m.id IN (SELECT message_id FROM source_fts"
                         " WHERE source_fts MATCH ?)")
            params.append(projection.compile_query(keyword))
            matched_by = "fts_text"
        else:
            like = "%" + _escape_like(keyword) + "%"
            where.append("m.content_json LIKE ? ESCAPE '\\'")
            params.append(like)
            matched_by = "evidence_like"
    where.append("m.normalized_sender IN (%s)"
                 % ",".join("?" * len(sender_filter)))
    params += list(sender_filter)

    sql = (f"SELECT m.*, c.title AS conversation_title, c.id AS conv_row_id"
           f" FROM source_messages m JOIN source_conversations c"
           f" ON c.id=m.conversation_id WHERE {' AND '.join(where)}"
           f" ORDER BY m.occurred_date DESC, m.created_at DESC, m.sequence"
           f" DESC, m.id DESC LIMIT ? OFFSET ?")
    params += [limit + 1, offset]
    with db.formal() as conn:
        rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    hits = [_serialize(r, keyword=keyword, matched_by=matched_by)
            for r in rows[:limit]]
    return {"hits": hits, "has_more": has_more, "limit": limit,
            "offset": offset, "senders": sender_filter,
            "source": "source_layer",
            "boundary": "原文专项检索；不参与普通 Recall，不代表记忆结论；"
                        "默认只含已发布（成功批次）数据"}


def get_message(message_id: str | None = None,
                provider_message_id: str | None = None,
                context: int = 5, include_content: bool = False,
                include_unpublished: bool = False) -> dict:
    """按内部 ID 或 provider UUID 精确打开一条消息（含上下文）。

    include_unpublished=True 为诊断开关：可读未发布（失败批次）数据，
    响应显式标注。
    """
    context = max(0, min(int(context), 50))
    with db.formal() as conn:
        row = _find_message(conn, message_id, provider_message_id)
        if row is None or (not include_unpublished
                           and not row["published"]):
            raise NotFound("source message not found",
                           message_id=message_id,
                           provider_message_id=provider_message_id)
        pub = row["published"]
        conv = conn.execute(
            "SELECT id, provider, provider_conversation_id, title,"
            " created_at, updated_at, message_count, first_message_at,"
            " last_message_at FROM source_conversations WHERE id=?",
            (row["conversation_id"],)).fetchone()
        pubflt = "" if include_unpublished else " AND published=1"
        prev = conn.execute(
            f"SELECT {_COLS} FROM source_messages WHERE conversation_id=?"
            f" AND sequence<?{pubflt} ORDER BY sequence DESC LIMIT ?",
            (row["conversation_id"], row["sequence"], context)).fetchall()
        nxt = conn.execute(
            f"SELECT {_COLS} FROM source_messages WHERE conversation_id=?"
            f" AND sequence>?{pubflt} ORDER BY sequence ASC LIMIT ?",
            (row["conversation_id"], row["sequence"], context)).fetchall()
    return {
        "message": _serialize(row, include_content=include_content),
        "context_before": [_serialize(r) for r in reversed(prev)],
        "context_after": [_serialize(r) for r in nxt],
        "conversation": _conv_summary(conv),
        "published": bool(pub),
        "source": "source_layer",
    }


def open_range(conversation_id: str, start_message_id: str,
               end_message_id: str, start_char_offset: int | None = None,
               end_char_offset: int | None = None,
               include_content: bool = False,
               include_unpublished: bool = False) -> dict:
    """打开语义绑定指向的连续消息区间（parent 路径验证 + 半开区间裁切）。"""
    resolved = validate_range(conversation_id, start_message_id,
                              end_message_id, start_char_offset,
                              end_char_offset, include_unpublished)
    start, end = resolved["start"], resolved["end"]
    path_ids = resolved["path_ids"]

    pubflt = "" if include_unpublished else " AND published=1"
    with db.formal() as conn:
        rows = conn.execute(
            f"SELECT {_COLS}, content_json FROM source_messages WHERE"
            f" conversation_id=?"
            f" AND sequence>=? AND sequence<=?{pubflt}"
            " ORDER BY sequence ASC",
            (start["conversation_id"], start["sequence"],
             end["sequence"])).fetchall()
    if len(rows) > config.SOURCE_RANGE_MAX_MESSAGES:
        raise Forbidden(
            f"区间消息数超限（{len(rows)} > "
            f"{config.SOURCE_RANGE_MAX_MESSAGES}）；请缩小范围或分页",
            code="SOURCE_RANGE_TOO_LARGE")
    text_budget = sum(len(r["text"] or "") for r in rows)
    if text_budget > config.SOURCE_RANGE_MAX_TEXT_BYTES:
        raise Forbidden("区间正文字节超限；请缩小范围或分页",
                        code="SOURCE_RANGE_TOO_LARGE")

    start_row_id, end_row_id = start["id"], end["id"]
    messages, off_path = [], []
    for r in rows:
        s_off = start_char_offset if r["id"] == start_row_id else None
        e_off = end_char_offset if r["id"] == end_row_id else None
        on_path = r["id"] in path_ids
        item = _serialize(
            r, include_content=include_content and on_path,
            char_offsets=(s_off, e_off),
            is_start=r["id"] == start_row_id,
            is_end=r["id"] == end_row_id,
            slice_offsets=(s_off, e_off))
        if on_path:
            messages.append(item)
        else:
            item["sibling_branch"] = True  # 区间内但不在 parent 路径（SL-06）
            off_path.append(item)
    return {
        "conversation": resolved["conversation"],
        "start_message_id": start_row_id,
        "end_message_id": end_row_id,
        "start_provider_message_id": start["provider_message_id"],
        "end_provider_message_id": end["provider_message_id"],
        "char_offsets": {"start": start_char_offset, "end": end_char_offset,
                         "convention": OFFSET_CONVENTION},
        "messages": messages,
        "off_path_messages": off_path,
        "include_content": include_content,
        "source": "source_layer",
    }


def validate_range(conversation_id: str, start_message_id: str,
                   end_message_id: str,
                   start_char_offset=None, end_char_offset=None,
                   include_unpublished: bool = False) -> dict:
    """区间语义校验（binding 与 range.open 共用；SL-05/06）。

    - 消息必须存在且属于同一会话；
    - 跨消息区间必须满足 end 沿 parent 链可回溯到 start（同 parent 路径，
      无断链/循环）；sibling 分支拒绝（SOURCE_RANGE_NOT_PATH）；
    - 偏移必须是非布尔整数，且 0 <= off <= len(text)；同一条消息上
      start 偏移不得大于 end 偏移。
    """
    with db.formal() as conn:
        conv = _find_conversation(conn, conversation_id)
        if conv is None:
            raise NotFound("source conversation not found",
                           conversation_id=conversation_id)
        pubflt = "" if include_unpublished else " AND published=1"
        start = _find_message_any(conn, start_message_id, pubflt)
        end = _find_message_any(conn, end_message_id, pubflt)
        if start is None or end is None:
            raise NotFound("range message not found",
                           start=start_message_id, end=end_message_id)
        for label, m in (("start", start), ("end", end)):
            if m["conversation_id"] != conv["id"]:
                raise NotFound(f"{label} message not in conversation")
        # 偏移口径校验（SL-05）：非布尔整数拒绝，不做 int() 静默截断
        for label, off in (("start_char_offset", start_char_offset),
                           ("end_char_offset", end_char_offset)):
            if off is not None and (isinstance(off, bool)
                                    or not isinstance(off, int)):
                raise Forbidden(
                    f"{label} 必须是整数（Unicode code point 口径）",
                    code="SOURCE_RANGE_OFFSET", value=repr(off))
        for label, m, off in (("start", start, start_char_offset),
                              ("end", end, end_char_offset)):
            if off is not None and not (0 <= off <= len(m["text"] or "")):
                raise Forbidden(
                    f"{label}_char_offset 超出消息文本范围（半开区间口径）",
                    code="SOURCE_RANGE_OFFSET", offset=off,
                    text_len=len(m["text"] or ""))
        if (start["id"] == end["id"] and start_char_offset is not None
                and end_char_offset is not None
                and start_char_offset > end_char_offset):
            raise Forbidden("同一条消息上 start 偏移不得大于 end 偏移",
                            code="SOURCE_RANGE_OFFSET")
        path_ids = _parent_path_ids(conn, start, end, pubflt)
    return {"conversation": conv, "start": start, "end": end,
            "path_ids": path_ids,
            "start_content_hash": start["content_hash"],
            "end_content_hash": end["content_hash"]}


#: parent 回溯步数上限（防循环；与响应预算 SOURCE_RANGE_MAX_MESSAGES 解耦：
#: 回溯是校验成本不是输出大小）
_PATH_WALK_LIMIT = 10000


def _parent_path_ids(conn, start, end, pubflt: str) -> set[str]:
    """从 end 沿 parent 回溯到 start；失败即 SOURCE_RANGE_NOT_PATH。"""
    if start["id"] == end["id"]:
        return {start["id"]}
    if start["sequence"] > end["sequence"]:
        raise Forbidden("start message is after end message",
                        code="SOURCE_RANGE_NOT_PATH", kind="order")
    path = {end["id"]}
    cur = end
    steps = 0
    while cur["id"] != start["id"]:
        pid = cur["parent_provider_message_id"]
        if not pid:
            raise Forbidden(
                "区间不在同一 parent 路径（断链：消息缺 parent）",
                code="SOURCE_RANGE_NOT_PATH", kind="broken_parent")
        nxt = conn.execute(
            f"SELECT {_COLS}, conversation_id FROM source_messages"
            " WHERE provider=? AND provider_conversation_id=? AND"
            f" provider_message_id=?{pubflt}",
            (cur["provider"], cur["provider_conversation_id"], pid)
        ).fetchone()
        if nxt is None:
            raise Forbidden(
                "区间不在同一 parent 路径（parent 不在库/会话/发布范围）",
                code="SOURCE_RANGE_NOT_PATH", kind="missing_parent")
        if nxt["id"] in path:
            raise Forbidden("parent 链存在循环",
                            code="SOURCE_RANGE_NOT_PATH", kind="cycle")
        path.add(nxt["id"])
        cur = nxt
        steps += 1
        if steps > _PATH_WALK_LIMIT:
            raise Forbidden("parent 回溯超上限（疑似循环/超长）",
                            code="SOURCE_RANGE_NOT_PATH", kind="too_long")
    return path


def get_conversation(conversation_id: str, after_seq=None,
                     before_seq=None, around_seq: int | None = None,
                     limit: int = 100, after_id: str | None = None,
                     before_id: str | None = None) -> dict:
    """按 (sequence, id) 复合游标分页（A11：sequence 非唯一不漏消息）。

    游标接受旧式纯整数（兼容）或 {"seq": n, "id": "..."}；返回
    next_after_cursor/prev_before_cursor 复合游标 + 兼容的 *_seq 字段。
    """
    limit = max(1, min(int(limit), 500))

    def _cursor(raw, cid):
        """dict 游标 → ("cmp", seq, id)；纯整数 → ("gt", seq, None) 兼容。"""
        if raw is None:
            return None
        if isinstance(raw, dict):
            return ("cmp", int(raw.get("seq", 0)), str(raw.get("id", "")))
        return ("gt", int(raw), None)

    after_cur = _cursor(after_seq, after_id)
    before_cur = _cursor(before_seq, before_id)
    with db.formal() as conn:
        conv = _find_conversation(conn, conversation_id)
        if conv is None:
            raise NotFound("source conversation not found",
                           conversation_id=conversation_id)
        pub = " AND published=1"
        if around_seq is not None:
            half = limit // 2
            before_rows = conn.execute(
                f"SELECT {_COLS} FROM source_messages WHERE conversation_id=?"
                f" AND sequence<?{pub} ORDER BY sequence DESC, id DESC"
                " LIMIT ?",
                (conv["id"], int(around_seq), half)).fetchall()
            exact = conn.execute(
                f"SELECT {_COLS} FROM source_messages WHERE conversation_id=?"
                f" AND sequence=?{pub} ORDER BY id",
                (conv["id"], int(around_seq))).fetchall()
            after = conn.execute(
                f"SELECT {_COLS} FROM source_messages WHERE conversation_id=?"
                f" AND sequence>?{pub} ORDER BY sequence ASC, id ASC LIMIT ?",
                (conv["id"], int(around_seq), half)).fetchall()
            msgs = list(reversed(before_rows)) + list(exact) + list(after)
            has_more = bool(after)
        elif before_cur is not None:
            if before_cur[0] == "cmp":
                cond = " AND (sequence<? OR (sequence=? AND id<?))"
                params = [conv["id"], before_cur[1], before_cur[1],
                          before_cur[2], limit + 1]
            else:
                cond = " AND sequence<?"
                params = [conv["id"], before_cur[1], limit + 1]
            rows = conn.execute(
                f"SELECT {_COLS} FROM source_messages WHERE conversation_id=?"
                f"{pub}{cond}"
                " ORDER BY sequence DESC, id DESC LIMIT ?", params
            ).fetchall()
            has_more = len(rows) > limit
            msgs = list(reversed(rows[:limit]))
        else:
            if after_cur is not None and after_cur[0] == "cmp":
                cond = " AND (sequence>? OR (sequence=? AND id>?))"
                params = [conv["id"], after_cur[1], after_cur[1],
                          after_cur[2], limit + 1]
            elif after_cur is not None:  # 旧式整数：严格大于
                cond = " AND sequence>?"
                params = [conv["id"], after_cur[1], limit + 1]
            else:
                cond = ""
                params = [conv["id"], limit + 1]
            rows = conn.execute(
                f"SELECT {_COLS} FROM source_messages WHERE conversation_id=?"
                f"{pub}{cond}"
                " ORDER BY sequence ASC, id ASC LIMIT ?", params).fetchall()
            has_more = len(rows) > limit
            msgs = rows[:limit]
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM source_messages WHERE conversation_id=?"
            " AND published=1", (conv["id"],)).fetchone()["c"]
    first = msgs[0] if msgs else None
    last = msgs[-1] if msgs else None
    return {
        "conversation": _conv_summary(conv),
        "messages": [_serialize(r) for r in msgs],
        "next_after_seq": last["sequence"] if last else None,
        "prev_before_seq": first["sequence"] if first else None,
        "next_after_cursor": ({"seq": last["sequence"], "id": last["id"]}
                              if last else None),
        "prev_before_cursor": ({"seq": first["sequence"], "id": first["id"]}
                               if first else None),
        "has_more": has_more,
        "published_total": total,
        "source": "source_layer",
    }


def conversations_list(limit: int = 50, offset: int = 0,
                       provider: str | None = None) -> dict:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where = ["EXISTS (SELECT 1 FROM source_messages m WHERE"
             " m.conversation_id=source_conversations.id AND m.published=1)"]
    params: list = []
    if provider:
        where.append("provider=?")
        params.append(provider)
    cond = "WHERE " + " AND ".join(where)
    with db.formal() as conn:
        rows = conn.execute(
            f"SELECT id, provider, provider_conversation_id, title,"
            f" created_at, updated_at,"
            f" (SELECT COUNT(*) FROM source_messages m WHERE"
            f"  m.conversation_id=source_conversations.id AND m.published=1)"
            f" AS message_count,"
            f" (SELECT MIN(created_at) FROM source_messages m WHERE"
            f"  m.conversation_id=source_conversations.id AND m.published=1)"
            f" AS first_message_at,"
            f" (SELECT MAX(created_at) FROM source_messages m WHERE"
            f"  m.conversation_id=source_conversations.id AND m.published=1)"
            f" AS last_message_at"
            f" FROM source_conversations {cond}"
            f" ORDER BY COALESCE(updated_at, created_at) DESC"
            f" LIMIT ? OFFSET ?", params + [limit + 1, offset]).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM source_conversations {cond}",
            params).fetchone()["c"]
    return {"conversations": [_conv_summary(r) for r in rows[:limit]],
            "total": total, "has_more": len(rows) > limit, "limit": limit,
            "offset": offset}


# ---------------------------------------------------------------- 内部

def _resolve_senders(senders: list[str] | None) -> list[str]:
    if not senders:
        return list(_DEFAULT_SENDERS)
    out = [s for s in senders if s in _ALL_SENDERS]
    if not out:
        raise NotFound(f"senders 必须是 {list(_ALL_SENDERS)} 的子集")
    return out


def _base_filters(provider, conversation_id, date_from, date_to):
    where, params = [], []
    if provider:
        where.append("m.provider=?")
        params.append(provider)
    if conversation_id:
        where.append("(m.conversation_id=? OR"
                     " m.provider_conversation_id=?)")
        params += [conversation_id, conversation_id]
    if date_from:
        where.append("m.occurred_date>=?")
        params.append(date_from)
    if date_to:
        where.append("m.occurred_date<=?")
        params.append(date_to)
    return where, params


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F)


def _token_spans(text: str) -> list[tuple[str, int, int]]:
    """与 projection.tokenize 同规则的 token 序列 + 原文位置（SL-03）。

    归一化命中必须映射回原字符串安全上下文，不得退回 evidence。
    """
    spans: list[tuple[str, int, int]] = []
    buf: list[str] = []
    buf_start = 0
    for i, ch in enumerate(text):
        if _is_cjk(ch):
            if buf:
                spans.append(("".join(buf).lower(), buf_start, i))
                buf = []
            spans.append((ch.lower(), i, i + 1))
        elif ch.isalnum():
            if not buf:
                buf_start = i
            buf.append(ch)
        else:
            if buf:
                spans.append(("".join(buf).lower(), buf_start, i))
                buf = []
    if buf:
        spans.append(("".join(buf).lower(), buf_start, len(text)))
    return spans


def _body_excerpt(text: str, keyword: str, radius: int = 60) -> str:
    """正文内命中窗口：token 序列子串匹配映射回原文；绝不取自 evidence。"""
    if not text:
        return ""
    if not keyword:
        return text[:radius * 2] + ("…" if len(text) > radius * 2 else "")
    spans = _token_spans(text)
    q_tokens = [t for t in projection.tokenize(keyword)]
    if not q_tokens:
        return text[:radius * 2]
    ql = [t.lower() for t in q_tokens]
    n, m = len(spans), len(ql)
    for i in range(n - m + 1):
        if [s[0] for s in spans[i:i + m]] == ql:
            start = spans[i][1]
            end = spans[i + m - 1][2]
            lo = max(0, start - radius)
            hi = min(len(text), end + radius)
            return (("…" if lo > 0 else "") + text[lo:hi]
                    + ("…" if hi < len(text) else ""))
    p = text.find(keyword)
    if p >= 0:
        lo = max(0, p - radius)
        hi = min(len(text), p + len(keyword) + radius)
        return (("…" if lo > 0 else "") + text[lo:hi]
                + ("…" if hi < len(text) else ""))
    return text[:radius * 2] + ("…" if len(text) > radius * 2 else "")


def _evidence_excerpt(row, keyword: str, radius: int = 60) -> str:
    """证据面（显式 system/tool/unknown）专用摘录；正文路径绝不调用。"""
    raw = row["content_json"] if "content_json" in row.keys() else None
    if not raw:
        return ""
    try:
        node = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    fragments: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            for key in ("text", "thinking"):
                if isinstance(n.get(key), str) and n[key]:
                    fragments.append(n[key])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    for frag in fragments:
        p = frag.find(keyword)
        if p >= 0:
            lo = max(0, p - radius)
            hi = min(len(frag), p + len(keyword) + radius)
            return (("…" if lo > 0 else "") + frag[lo:hi]
                    + ("…" if hi < len(frag) else ""))
    return ""


def _excerpt_for(row, text: str, keyword: str, matched_by, sliced: bool):
    """excerpt 生成规则（SL-03）：
    - 默认（fts_text/无关键词）：只从正文生成，绝不回退 evidence；
    - 显式证据面（evidence_like）：正文优先，正文空才取证据摘录。
    """
    if sliced:
        return text
    if matched_by == "evidence_like":
        if keyword and not text:
            return _evidence_excerpt(row, keyword)
        return _body_excerpt(text, keyword)
    return _body_excerpt(text, keyword)


def _find_message(conn, message_id=None, provider_message_id=None):
    cols = f"{_COLS}, conversation_id, content_json"
    if message_id:
        return conn.execute(
            f"SELECT {cols} FROM source_messages WHERE id=?",
            (message_id,)).fetchone()
    if provider_message_id:
        return conn.execute(
            f"SELECT {cols} FROM source_messages WHERE provider_message_id=?",
            (provider_message_id,)).fetchone()
    return None


def _find_message_any(conn, id_or_uuid: str, pubflt: str = ""):
    """按内部行 ID 或 provider UUID 解析（可选招发布过滤）。"""
    cols = f"{_COLS}, conversation_id, content_json"
    return conn.execute(
        f"SELECT {cols} FROM source_messages WHERE (id=? OR"
        f" provider_message_id=?){pubflt}",
        (id_or_uuid, id_or_uuid)).fetchone()


def _find_conversation(conn, conversation_id: str):
    return conn.execute(
        "SELECT id, provider, provider_conversation_id, title, created_at,"
        " updated_at, message_count, first_message_at, last_message_at"
        " FROM source_conversations WHERE id=? OR provider_conversation_id=?",
        (conversation_id, conversation_id)).fetchone()


def _conv_summary(conv) -> dict:
    if conv is None:
        return {}
    out = {"id": conv["id"], "provider": conv["provider"],
           "provider_conversation_id": conv["provider_conversation_id"],
           "title": conv["title"] or "(未命名对话)",
           "created_at": conv["created_at"], "updated_at": conv["updated_at"],
           "message_count": conv["message_count"]}
    if "first_message_at" in conv.keys():
        out["first_message_at"] = conv["first_message_at"]
        out["last_message_at"] = conv["last_message_at"]
    return out


def _slice_text_offsets(text: str, s_off, e_off) -> tuple[str, dict]:
    """按半开区间口径裁切；返回 (裁切文本, 实际应用区间)。"""
    applied = {"start": s_off, "end": e_off}
    if s_off is None and e_off is None:
        return text, applied
    s = 0 if s_off is None else s_off
    e = len(text) if e_off is None else e_off
    s = max(0, min(s, len(text)))
    e = max(0, min(e, len(text)))
    return text[s:e], applied


def _slice_content_json(content_json: str | None, s_off, e_off) -> str | None:
    """裁切模式下 content_json 只保留所选片段（SL-05：不夹带未选内容）。

    text block 按消息 text 的拼接顺序定位并裁切；其余 block 以省略摘要
    保留结构（内容不外带，母本在 Raw Archive）。
    """
    if content_json is None:
        return None
    if s_off is None and e_off is None:
        return content_json
    try:
        blocks = json.loads(content_json)
    except (ValueError, TypeError):
        return json.dumps({"note": "content not sliceable; see Raw Archive"},
                          ensure_ascii=False)
    if not isinstance(blocks, list):
        return json.dumps({"note": "content not sliceable; see Raw Archive"},
                          ensure_ascii=False)
    out_blocks = []
    cursor = 0
    s = s_off if s_off is not None else 0
    e_max = 1 << 30
    e = e_off if e_off is not None else e_max
    for b in blocks:
        if not (isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str)):
            out_blocks.append({"omitted_block": True,
                               "type": b.get("type") if isinstance(b, dict)
                               else type(b).__name__,
                               "reason": "outside_requested_char_range"})
            continue
        t = b["text"]
        bs, be = cursor, cursor + len(t)
        cursor = be
        lo = max(s, bs)
        hi = min(e, be)
        if hi > lo:
            out_blocks.append({**b, "text": t[lo - bs:hi - bs],
                               "char_range": [lo, hi],
                               "convention": OFFSET_CONVENTION})
    return json.dumps(out_blocks, ensure_ascii=False)


def _serialize(row, *, keyword: str = "", include_content: bool = False,
               char_offsets=None, is_start=False, is_end=False,
               slice_offsets=None, matched_by: str | None = None) -> dict:
    text = row["text"] or ""
    sliced = False
    if slice_offsets and (slice_offsets[0] is not None
                          or slice_offsets[1] is not None):
        text, _applied = _slice_text_offsets(
            text, slice_offsets[0], slice_offsets[1])
        sliced = True
    out = {
        "message_id": row["id"],
        "provider": row["provider"],
        "provider_conversation_id": row["provider_conversation_id"],
        "provider_message_id": row["provider_message_id"],
        "id_synthetic": bool(row["id_synthetic"]),
        "parent_provider_message_id": row["parent_provider_message_id"],
        "raw_sender": row["raw_sender"],
        "normalized_sender": row["normalized_sender"],
        "speaker": row["speaker"],
        "speaker_display": SPEAKER_DISPLAY.get(row["speaker"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "occurred_date": row["occurred_date"],
        "text": text,
        "excerpt": _excerpt_for(row, text, keyword, matched_by, sliced),
        "attachments": json.loads(row["attachments"] or "[]"),
        "has_thinking": bool(row["has_thinking"]),
        "has_tool_content": bool(row["has_tool_content"]),
        "sequence": row["sequence"],
        "published": bool(row["published"]),
        "conversation_title": row["conversation_title"]
        if "conversation_title" in row.keys() else None,
    }
    if matched_by:
        out["matched_fields"] = ["text" if matched_by == "fts_text"
                                 else "content_json"]
    if include_content:
        if sliced:
            out["content_json"] = _slice_content_json(
                row["content_json"] if "content_json" in row.keys() else None,
                slice_offsets[0], slice_offsets[1])
        else:
            raw = row["content_json"] if "content_json" in row.keys() else None
            out["content_json"] = raw
    if char_offsets is not None:
        start_off, end_off = char_offsets
        if is_start and start_off is not None:
            out["char_offset_start"] = start_off
        if is_end and end_off is not None:
            out["char_offset_end"] = end_off
        out["offset_convention"] = OFFSET_CONVENTION
    return out
