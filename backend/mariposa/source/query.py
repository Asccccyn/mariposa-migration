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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")

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
           limit: int = 20, offset: int = 0,
           fts_expr: str | None = None,
           speaker: str | None = None,
           speakers_excluded: list[str] | None = None,
           date_ranges_excluded: list[dict] | None = None,
           anchor_terms: list[str] | None = None) -> dict:
    """原文专项搜索：关键词 + 说话人 + 日期区间 + 会话过滤（同集分页）。

    fts_expr：已编译的 FTS 表达式（含 OR 等布尔组合）直接使用，不再
    二次安全编译（复审#2：调用方的多词 OR 不能被吃掉）。
    speaker：正式身份（qiaosheng/jiaming）硬过滤。
    speakers_excluded / date_ranges_excluded（{from,to}）：同一候选集
    内的负向硬过滤（三轮复审#4：与 words 通道同一 source_scope）。
    anchor_terms：excerpt 命中窗口的定位锚词（三轮复审#5：检索用的
    FTS 表达式与摘录定位分离——表达式含 OR/引号，正文不会原样包含，
    不能拿它当关键词找命中位置）。
    """
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    sender_filter = _resolve_senders(senders)
    where, params = _base_filters(provider, conversation_id,
                                  date_from, date_to)
    where.append("m.published=1")
    if speaker:
        where.append("m.speaker=?")
        params.append(speaker)
    sp_ex = [s for s in (speakers_excluded or [])
             if isinstance(s, str) and s]
    if sp_ex:
        marks = ",".join("?" * len(sp_ex))
        where.append(f"m.speaker NOT IN ({marks})")
        params += sp_ex
    for rng in (date_ranges_excluded or []):
        if isinstance(rng, dict) and rng.get("from"):
            where.append("(m.occurred_date IS NULL OR"
                         " m.occurred_date NOT BETWEEN ? AND ?)")
            params += [rng["from"], rng.get("to", "9999-12-31")]

    keyword = (fts_expr or (query or "").strip())
    # 三轮复审#5：excerpt 锚词独立于检索表达式——fts_expr 时只用
    # 调用方给的 anchor_terms；普通 query 时锚词即 query 本身
    if fts_expr:
        excerpt_anchors = [a for a in (anchor_terms or [])
                           if isinstance(a, str) and a.strip()]
    else:
        excerpt_anchors = [keyword] if keyword else []
    matched_by = None
    if keyword:
        if set(sender_filter) <= set(_DEFAULT_SENDERS):
            # FTS 匹配作为主查询条件参与同一候选集（SL-08），不先截断取 ID
            where.append("m.id IN (SELECT message_id FROM source_fts"
                         " WHERE source_fts MATCH ?)")
            params.append(fts_expr if fts_expr
                          else projection.compile_query(keyword))
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
    hits = [_serialize(r, keyword=keyword, matched_by=matched_by,
                       anchors=excerpt_anchors)
            for r in rows[:limit]]
    return {"hits": hits, "has_more": has_more, "limit": limit,
            "offset": offset, "senders": sender_filter,
            "source": "source_layer",
            "content_role": "retrieved_memory",
            "instruction_authority": "none",
            "boundary": "原文专项检索；不参与普通 Recall，不代表记忆结论；"
                        "默认只含已发布（成功批次）数据"}


def get_message(message_id: str | None = None,
                provider_message_id: str | None = None,
                context: int = 5, include_content: bool = False,
                include_unpublished: bool = False,
                provider: str | None = None) -> dict:
    """按内部 ID 或 provider UUID 精确打开一条消息（含上下文）。

    include_unpublished=True 为诊断开关：可读未发布（失败批次）数据，
    响应显式标注。
    """
    context = max(0, min(int(context), 50))
    with db.formal() as conn:
        row = _find_message(conn, message_id, provider_message_id,
                           provider=provider)
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
        "content_role": "retrieved_memory",
        "instruction_authority": "none",
    }



def covered_path_ids(conversation_id: str, start_message_id: str,
                     end_message_id: str,
                     include_unpublished: bool = False) -> set[str] | None:
    """绑定区间的实际 parent 路径成员集合（SRC-07 共享解析器）。

    供正查/逆查共用：成员身份以 parent 链为准，不以跨快照 sequence
    大小近似——sibling 不在路径上就不算覆盖。解析失败返回 None
    （调用方按保守跳过该绑定，不猜）。
    """
    order = ordered_path_ids(conversation_id, start_message_id,
                             end_message_id, include_unpublished)
    return set(order) if order is not None else None


def ordered_path_ids(conversation_id: str, start_message_id: str,
                     end_message_id: str,
                     include_unpublished: bool = False) -> list[str] | None:
    """区间的实际 parent 路径（start→end 有序；RSRC-07 共享解析器）。

    重叠判定需要路径**次序与端点身份**（字符偏移只在共享边界消息
    上比较），set 不够用。解析失败返回 None（调用方保守跳过，不猜）。
    """
    try:
        resolved = validate_range(
            conversation_id, start_message_id, end_message_id,
            include_unpublished=include_unpublished)
        return list(resolved["path_order"])
    except Exception:
        return None


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
        # CB-029（2026-10-02 审计 P2）：先计数后取行——巨大候选区间在
        # 拒绝前不再全量载入内存
        n_rows = conn.execute(
            f"SELECT COUNT(*) AS c FROM source_messages WHERE"
            f" conversation_id=? AND sequence>=? AND sequence<=?{pubflt}",
            (start["conversation_id"], start["sequence"],
             end["sequence"])).fetchone()["c"]
        if n_rows > config.SOURCE_RANGE_MAX_MESSAGES:
            raise Forbidden(
                f"区间消息数超限（{n_rows} > "
                f"{config.SOURCE_RANGE_MAX_MESSAGES}）；请缩小范围或分页",
                code="SOURCE_RANGE_TOO_LARGE")
        rows = list(conn.execute(
            f"SELECT {_COLS}, content_json FROM source_messages WHERE"
            f" conversation_id=?"
            f" AND sequence>=? AND sequence<=?{pubflt}"
            " ORDER BY sequence ASC",
            (start["conversation_id"], start["sequence"],
             end["sequence"])).fetchall())
        # SRC-01（2026-10-04 二批）：path 成员按身份取齐——跨快照
        # sequence 冲突会让中间消息的序号落在窗口外，按序号截取会把
        # 已验证 parent 路径的成员丢掉；补齐后再按链序输出
        window_ids = {r["id"] for r in rows}
        path_missing = [i for i in path_ids if i not in window_ids]
        if path_missing:
            marks = ",".join("?" * len(path_missing))
            rows += list(conn.execute(
                f"SELECT {_COLS}, content_json FROM source_messages WHERE"
                f" conversation_id=? AND id IN ({marks}){pubflt}"
                " ORDER BY sequence ASC",
                (start["conversation_id"], *path_missing)).fetchall())
            # SRC-01-R2（2026-10-04 复审）：补齐的 path 成员同样计入
            # 数量预算——不得借补齐绕过区间资源门
            if len(rows) > config.SOURCE_RANGE_MAX_MESSAGES:
                raise Forbidden(
                    f"含 parent 补齐的区间消息数超限（{len(rows)} > "
                    f"{config.SOURCE_RANGE_MAX_MESSAGES}）；请缩小范围",
                    code="SOURCE_RANGE_TOO_LARGE")
    # CB-029：字节预算按 UTF-8 实际编码计——预算名义单位是字节，
    # len(str) 按 code point 计数会让中文/emoji 输出达 3/4 倍预算
    #（char offset 口径不变，仍是 code point 半开区间）
    text_budget = sum(len((r["text"] or "").encode("utf-8")) for r in rows)
    if text_budget > config.SOURCE_RANGE_MAX_TEXT_BYTES:
        raise Forbidden("区间正文字节超限；请缩小范围或分页",
                        code="SOURCE_RANGE_TOO_LARGE")

    start_row_id, end_row_id = start["id"], end["id"]
    # SRC-01：输出顺序 = parent 链序（start→end），不是跨快照 sequence
    by_id = {r["id"]: r for r in rows}
    by_pmid = {r["provider_message_id"]: r for r in rows}
    chain = []
    _cur = by_id[end_row_id]
    _guard = 0
    while True:
        chain.append(_cur)
        if _cur["id"] == start_row_id or _guard > 10000:
            break
        _pid = _cur["parent_provider_message_id"]
        if not _pid or _pid not in by_pmid:
            break
        _cur = by_pmid[_pid]
        _guard += 1
    chain.reverse()
    path_row_ids = [r["id"] for r in chain]

    def _item(r):
        s_off = start_char_offset if r["id"] == start_row_id else None
        e_off = end_char_offset if r["id"] == end_row_id else None
        return _serialize(
            r, include_content=include_content and r["id"] in path_ids,
            char_offsets=(s_off, e_off),
            is_start=r["id"] == start_row_id,
            is_end=r["id"] == end_row_id,
            slice_offsets=(s_off, e_off))

    messages = [_item(by_id[i]) for i in path_row_ids]
    off_path = []
    for r in rows:
        if r["id"] not in path_ids:
            item = _item(r)
            item["sibling_branch"] = True  # 区间内但不在 parent 路径（SL-06）
            off_path.append(item)
    return {
        # RA-009：sqlite Row 统一转 dict（MCP json.dumps 不再 500）
        "conversation": dict(resolved["conversation"])
        if not isinstance(resolved["conversation"], dict)
        else resolved["conversation"],
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
        "content_role": "retrieved_memory",
        "instruction_authority": "none",
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
        start = _find_message_any(conn, start_message_id, pubflt,
                                  provider=conv["provider"])
        end = _find_message_any(conn, end_message_id, pubflt,
                                provider=conv["provider"])
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
            "path_ids": set(path_ids), "path_order": path_ids,
            "start_content_hash": start["content_hash"],
            "end_content_hash": end["content_hash"]}


#: parent 回溯步数上限（防循环；与响应预算 SOURCE_RANGE_MAX_MESSAGES 解耦：
#: 回溯是校验成本不是输出大小）
_PATH_WALK_LIMIT = 10000


def _parent_path_ids(conn, start, end, pubflt: str) -> list[str]:
    """从 end 沿 parent 回溯到 start；失败即 SOURCE_RANGE_NOT_PATH。

    RSRC-07：返回 start→end **有序**列表——重叠判定与字符偏移比较
    需要端点身份与次序，跨快照 sequence 大小不参与（序号倒置的
    真实路径仍按链序成立）。
    """
    if start["id"] == end["id"]:
        return [start["id"]]
    path = [end["id"]]
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
        path.append(nxt["id"])
        cur = nxt
        steps += 1
        if steps > _PATH_WALK_LIMIT:
            raise Forbidden("parent 回溯超上限（疑似循环/超长）",
                            code="SOURCE_RANGE_NOT_PATH", kind="too_long")
    path.reverse()
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
            # CB-020 + RA-022（2026-10-02 复审 P2）：窗口在复合顺序
            # (sequence, id) 上**连续**且有界——整体按 DESC 复合序取
            # limit 条（含 exact 组全部成员的连续窗），再对窗口首条
            # 判 has_more_before（窗口外更早侧）、尾条判 has_more_after；
            # 两侧游标都指向窗口边缘，被跨过的中间成员可续取。
            anchor_rows = conn.execute(
                f"SELECT {_COLS} FROM source_messages WHERE"
                f" conversation_id=? AND sequence=?{pub} ORDER BY id",
                (conv["id"], int(around_seq))).fetchall()
            if not anchor_rows:
                # 无精确命中：退化为双侧各 half 的传统窗口
                half = limit // 2
                before_rows = conn.execute(
                    f"SELECT {_COLS} FROM source_messages WHERE"
                    f" conversation_id=? AND sequence<?{pub}"
                    " ORDER BY sequence DESC, id DESC LIMIT ?",
                    (conv["id"], int(around_seq), half)).fetchall()
                after = conn.execute(
                    f"SELECT {_COLS} FROM source_messages WHERE"
                    f" conversation_id=? AND sequence>?{pub}"
                    " ORDER BY sequence ASC, id ASC LIMIT ?",
                    (conv["id"], int(around_seq), half)).fetchall()
                first = (before_rows[-1] if before_rows else
                         (after[0] if after else None))
                last = (after[-1] if after else
                        (before_rows[0] if before_rows else None))
                msgs = list(reversed(before_rows)) + list(after)
                has_more = len(msgs) >= limit
            else:
                first_anchor = anchor_rows[0]
                # 以 exact 组为中心的连续窗：含组全体 + 复合序两侧补满
                rows_desc = conn.execute(
                    f"SELECT {_COLS} FROM source_messages WHERE"
                    f" conversation_id=?{pub} AND (sequence<? OR"
                    f" (sequence=? AND id<=?))"
                    " ORDER BY sequence DESC, id DESC LIMIT ?",
                    (conv["id"], int(around_seq), int(around_seq),
                     first_anchor["id"], limit)).fetchall()
                msgs = list(reversed(rows_desc))
                first = msgs[0] if msgs else None
                last = msgs[-1] if msgs else None
                has_more = False

            def _more_outside(row, later_side: bool) -> bool:
                if row is None:
                    return False
                op = ">" if later_side else "<"
                r = conn.execute(
                    f"SELECT 1 FROM source_messages WHERE"
                    f" conversation_id=?{pub} AND (sequence{op}? OR"
                    f" (sequence=? AND id{op}?)) LIMIT 1",
                    (conv["id"], row["sequence"], row["sequence"],
                     row["id"])).fetchone()
                return r is not None
            more_before = _more_outside(first, False)
            more_after = _more_outside(last, True)
            if has_more is False:
                has_more = more_before or more_after
            else:
                has_more = True
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
        "content_role": "retrieved_memory",
        "instruction_authority": "none",
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


def _locate_window(text: str, kw: str, radius: int = 60) -> str | None:
    """单个锚词的正文命中窗：token 序列匹配映射回原文；未命中 None。

    与检索表达式无关（三轮复审#5）——锚词是普通词面（如"中秋"），
    不是含 OR/引号的 FTS 表达式。
    """
    spans = _token_spans(text)
    q_tokens = [t for t in projection.tokenize(kw) if t]
    if not q_tokens:
        return None
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
    p = text.find(kw)
    if p >= 0:
        lo = max(0, p - radius)
        hi = min(len(text), p + len(kw) + radius)
        return (("…" if lo > 0 else "") + text[lo:hi]
                + ("…" if hi < len(text) else ""))
    return None


def _anchors_excerpt(text: str, keyword: str, anchors: list[str] | None,
                     radius: int = 60) -> tuple[str, bool]:
    """锚词序列的命中窗：按序尝试，第一个能定位的锚词给出窗口。

    全量审计 P2-05：有锚词而全部定位失败 → 返回 ("", False)——
    头部窗是"真的但无关"的文本，冒充命中证据比没有证据更危险；
    调用方标 excerpt_locator=failed，候选按 fail-closed 处理。
    无锚词（浏览/无关键词）时头部窗合法，返回 (head, True)。"""
    if not text:
        return "", True
    head = text[:radius * 2] + ("…" if len(text) > radius * 2 else "")
    cand = [a for a in (anchors or []) if a and a.strip()]
    if not cand and keyword:
        cand = [keyword]
    if not cand:
        return head, True
    for a in cand:
        win = _locate_window(text, a, radius)
        if win is not None:
            return win, True
    return "", False


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


def _excerpt_for(row, text: str, keyword: str, matched_by, sliced: bool,
                 anchors: list[str] | None = None) -> str:
    """excerpt 生成规则（SL-03 + 三轮复审#5）：
    - 默认（fts_text/无关键词）：只从正文生成，绝不回退 evidence；
      检索用 FTS 表达式与摘录锚词分离（anchors），命中窗围绕真实
      命中的锚词（长消息尾部命中不再给出无关开头）；
    - 显式证据面（evidence_like）：正文优先，正文空才取证据摘录。
    """
    if sliced:
        return text, True
    if matched_by == "evidence_like":
        if keyword and not text:
            return _evidence_excerpt(row, keyword), True
        return _anchors_excerpt(text, keyword, anchors)
    return _anchors_excerpt(text, keyword, anchors)


def _find_message(conn, message_id=None, provider_message_id=None,
                  provider: str | None = None):
    cols = f"{_COLS}, conversation_id, content_json"
    if message_id:
        return conn.execute(
            f"SELECT {cols} FROM source_messages WHERE id=?",
            (message_id,)).fetchone()
    if provider_message_id:
        # 全量审计 P2-04：provider_message_id 唯一性是
        # (provider, provider_message_id)——UUID 跨 provider 可碰撞，
        # 多命中必须显式 AMBIGUOUS，不得 fetchone 静默挑一条
        pv = " AND provider=?" if provider else ""
        rows = conn.execute(
            f"SELECT {cols} FROM source_messages WHERE"
            f" provider_message_id=?{pv}",
            ((provider_message_id,) + ((provider,) if provider else ()))
        ).fetchall()
        if len(rows) > 1:
            raise Forbidden(
                "provider_message_id 跨 provider 命中多条；携带 provider"
                " 精确指定",
                code="SOURCE_MESSAGE_AMBIGUOUS",
                providers=sorted({r["provider"] for r in rows}))
        return rows[0] if rows else None
    return None


def _find_message_any(conn, id_or_uuid: str, pubflt: str = "",
                      provider: str | None = None):
    """按内部行 ID 或 provider UUID 解析（可选招发布过滤）。

    F13：provider 维度限定——多 provider 接入后 UUID 碰撞不再取错行。
    """
    cols = f"{_COLS}, conversation_id, content_json"
    pv = " AND provider=?" if provider else ""
    params = [id_or_uuid, id_or_uuid] + ([provider] if provider else [])
    return conn.execute(
        f"SELECT {cols} FROM source_messages WHERE (id=? OR"
        f" provider_message_id=?){pubflt}{pv}",
        params).fetchone()


def _find_conversation(conn, conversation_id: str):
    """CB-022（2026-10-02 审计 P2）：内部行 ID 精确优先；外部会话 ID
    命中多个 provider = 身份歧义，明确拒绝——fetchone 静默选行会在
    多源存量下取错会话（range/binding 复用此 resolver）。"""
    row = conn.execute(
        "SELECT id, provider, provider_conversation_id, title, created_at,"
        " updated_at, message_count, first_message_at, last_message_at"
        " FROM source_conversations WHERE id=?",
        (conversation_id,)).fetchone()
    if row is not None:
        return row
    rows = conn.execute(
        "SELECT id, provider, provider_conversation_id, title, created_at,"
        " updated_at, message_count, first_message_at, last_message_at"
        " FROM source_conversations WHERE provider_conversation_id=?",
        (conversation_id,)).fetchall()
    if len(rows) > 1:
        raise Forbidden(
            "外部会话 ID 命中多个 provider（身份歧义）：请使用内部"
            " conversation id 消歧",
            code="AMBIGUOUS_CONVERSATION_ID", conversation_id=conversation_id,
            providers=sorted({r["provider"] for r in rows}))
    return rows[0] if rows else None


def _conv_summary(conv) -> dict:
    if conv is None:
        return {}
    out = {"id": conv["id"], "provider": conv["provider"],
           "provider_conversation_id": conv["provider_conversation_id"],
           "title": conv["title"] or "(未命名对话)",
           "created_at": _fmt_min(conv["created_at"]),
           "updated_at": _fmt_min(conv["updated_at"]),
           "message_count": conv["message_count"]}
    if "first_message_at" in conv.keys():
        out["first_message_at"] = _fmt_min(conv["first_message_at"])
        out["last_message_at"] = _fmt_min(conv["last_message_at"])
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



def _fmt_min(value) -> str | None:
    """出站时间口径（裁定 2026-10-04 四）：只到分钟、按上海显示。

    存储仍是完整 UTC ISO；出站统一 年-月-日 时:分——秒/毫秒/Z
    不给模型侧，省 token 且消除时区误读。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")


def _serialize(row, *, keyword: str = "", include_content: bool = False,
               char_offsets=None, is_start=False, is_end=False,
               slice_offsets=None, matched_by: str | None = None,
               anchors: list[str] | None = None) -> dict:
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
        "created_at": _fmt_min(row["created_at"]),
        "updated_at": _fmt_min(row["updated_at"]),
        "occurred_date": row["occurred_date"],
        "text": text,
        "excerpt": "",
        # CB-030（2026-10-02 审计 P2）：检索内容统一无指令权——与
        # memory.get/Recall 的资料身份约定一致（retrieval/evidence）
        "content_role": "retrieved_memory",
        "instruction_authority": "none",
        # 占位——真实值在下方统一装配（P2-05：locator 状态外显）
        "excerpt_locator": "ok",
        "attachments": json.loads(row["attachments"] or "[]"),
        "has_thinking": bool(row["has_thinking"]),
        "has_tool_content": bool(row["has_tool_content"]),
        "sequence": row["sequence"],
        "published": bool(row["published"]),
        "conversation_title": row["conversation_title"]
        if "conversation_title" in row.keys() else None,
    }
    _exc_text, _exc_located = _excerpt_for(
        row, text, keyword, matched_by, sliced, anchors=anchors)
    out["excerpt"] = _exc_text
    if not _exc_located:
        # P2-05：有锚词而全部定位失败——不拿无关头部文本冒充命中
        # 证据（excerpt 空 + 显式标记，候选侧 fail-closed）
        out["excerpt_locator"] = "failed"
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
