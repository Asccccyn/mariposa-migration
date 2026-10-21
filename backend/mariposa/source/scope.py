"""来源范围语义正本（R09）：binding 与 query 单向引用，事务不变。"""
from __future__ import annotations

from .. import db
from ..errors import Forbidden, NotFound


#: 偏移口径声明（前端 JS 为 UTF-16，跨语言一律以本口径为准）
OFFSET_CONVENTION = "unicode_code_point_half_open"


_COLS = ("id, provider, provider_conversation_id, provider_message_id,"
         " id_synthetic, parent_provider_message_id, raw_sender,"
         " normalized_sender, speaker, created_at, updated_at, occurred_date,"
         " text, attachments, has_thinking, has_tool_content, sequence,"
         " import_batch_id, published, content_hash")


def _find_conversation(conn, conversation_id: str):
    """CB-022（2026-10-02 审计 P2）：内部行 ID 精确优先；外部会话 ID
    命中多个 provider = 身份歧义，明确拒绝——fetchone 静默选行会在
    多源存量下取错会话（range/binding 复用此 resolver）。"""
    row = conn.execute(
        "SELECT id, provider, provider_conversation_id, title, created_at,"
        " updated_at, message_count, first_message_at, last_message_at,"
        " summary"
        " FROM source_conversations WHERE id=?",
        (conversation_id,)).fetchone()
    if row is not None:
        return row
    rows = conn.execute(
        "SELECT id, provider, provider_conversation_id, title, created_at,"
        " updated_at, message_count, first_message_at, last_message_at,"
        " summary"
        " FROM source_conversations WHERE provider_conversation_id=?",
        (conversation_id,)).fetchall()
    if len(rows) > 1:
        raise Forbidden(
            "外部会话 ID 命中多个 provider（身份歧义）：请使用内部"
            " conversation id 消歧",
            code="AMBIGUOUS_CONVERSATION_ID", conversation_id=conversation_id,
            providers=sorted({r["provider"] for r in rows}))
    return rows[0] if rows else None




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




def _validate_range_on(conn, conversation_id: str, start_message_id: str,
                       end_message_id: str,
                       start_char_offset, end_char_offset,
                       include_unpublished: bool) -> dict:
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




def validate_range(conversation_id: str, start_message_id: str,
                   end_message_id: str,
                   start_char_offset=None, end_char_offset=None,
                   include_unpublished: bool = False,
                   conn=None) -> dict:
    """区间语义校验（binding 与 range.open 共用；SL-05/06）。

    - 消息必须存在且属于同一会话；
    - 跨消息区间必须满足 end 沿 parent 链可回溯到 start（同 parent 路径，
      无断链/循环）；sibling 分支拒绝（SOURCE_RANGE_NOT_PATH）；
    - 偏移必须是非布尔整数，且 0 <= off <= len(text)；同一条消息上
      start 偏移不得大于 end 偏移。

    conn（迁移 31/WP2）：调用方事务注入——hold 与来源绑定合并提交时
    在同一写事务内校验，不在写锁内另读一份旧快照（方案 §3.3）。
    """
    if conn is not None:
        return _validate_range_on(conn, conversation_id, start_message_id,
                                  end_message_id, start_char_offset,
                                  end_char_offset, include_unpublished)
    with db.formal() as c:
        return _validate_range_on(c, conversation_id, start_message_id,
                                  end_message_id, start_char_offset,
                                  end_char_offset, include_unpublished)




def references_for_messages(message_ids: list[str], conn=None) -> dict[str, list[dict]]:
    """Batch inverse references by canonical message ID, using one snapshot.

    Pinned bindings use their exact member manifest; legacy bindings use the
    validated parent path. Each relevant binding is resolved once per batch.
    """
    if conn is None:
        with db.formal() as c:
            c.execute("BEGIN")
            return references_for_messages(message_ids, conn=c)
    out = {mid: [] for mid in message_ids}
    if not out:
        return out
    by_conversation: dict[str, set[str]] = {}
    ids = list(out)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(
                f"SELECT id, conversation_id FROM source_messages WHERE id IN ({marks})", chunk):
            by_conversation.setdefault(row["conversation_id"], set()).add(row["id"])
    for cid, wanted in by_conversation.items():
        bindings = conn.execute(
            "SELECT * FROM memory_source_bindings WHERE conversation_id=?"
            " ORDER BY created_at, binding_id", (cid,)).fetchall()
        pinned = {}
        for member in conn.execute(
                "SELECT m.binding_id, m.source_message_id FROM memory_source_binding_members m"
                " JOIN memory_source_bindings b ON b.binding_id=m.binding_id"
                " WHERE b.conversation_id=?", (cid,)):
            pinned.setdefault(member["binding_id"], set()).add(member["source_message_id"])
        for row in bindings:
            members = pinned.get(row["binding_id"])
            if members is None:
                try:
                    resolved = validate_range(
                        cid, row["start_message_id"], row["end_message_id"], conn=conn)
                    members = set(resolved["path_order"])
                except (NotFound, Forbidden):
                    continue
            for mid in wanted & members:
                out[mid].append(dict(row))
    return out
