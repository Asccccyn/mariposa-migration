"""Semantic Source Binding（复核 v1.1 §4.3 强化）。

- 区间/偏移语义校验全部委托 query.validate_range（同 parent 路径、
  半开区间 code point 口径、整数校验）——绑定与读取同一套契约。
- 绑定固定证据版本：记录 start/end content_hash，读取时校验防漂移。
- Memory 引用原文不复制原文；一条 Memory 多 range（多行）不变。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

from .. import audit, config, db
from ..errors import Forbidden, NotFound
from . import query as source_query


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bind(principal_id: str, memory_id: str, conversation_id: str,
         start_message_id: str, end_message_id: str,
         start_char_offset: int | None = None,
         end_char_offset: int | None = None,
         confidence: str = "exact", conn=None,
         members: list[dict] | None = None) -> dict:
    """把 memory 绑定到一个连续消息区间（可重复调用叠加多个 range）。

    conn 由纠错改绑传入：插入与纠错同事务（§5.4 原子操作）。
    members（迁移 31/WP2）：钉住成员 manifest——中间每条消息的修订与
    次序入库（memory_source_binding_members），不只钉首尾 hash。
    start/end 此时取首/末成员（旧字段继续填充兼容）；片段内成员必须
    都在首→末 parent 路径上且按路径序出现（多段间隙由多片段表达，
    不能靠 min/max sequence 伪造连续覆盖——契约 §4.4）。
    """
    if confidence not in ("exact", "high", "low"):
        raise Forbidden("confidence must be exact/high/low")

    def _insert(conn):
        if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                            (memory_id,)).fetchone():
            raise NotFound("memory not found", memory_id=memory_id)
        start_id, end_id = start_message_id, end_message_id
        if members is not None:
            _validate_members(conn, conversation_id, members,
                              start_char_offset, end_char_offset)
            start_id = members[0]["source_message_id"]
            end_id = members[-1]["source_message_id"]
        # 同一套区间契约（路径/偏移/发布可见性全部由 query 层校验）
        resolved = source_query.validate_range(
            conversation_id, start_id, end_id,
            start_char_offset, end_char_offset, conn=conn)
        binding_id = f"msb_{_uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO memory_source_bindings(binding_id, memory_id,"
            " conversation_id, start_message_id, end_message_id,"
            " start_char_offset, end_char_offset, bind_confidence,"
            " start_content_hash, end_content_hash, created_by,"
            " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (binding_id, memory_id, resolved["conversation"]["id"],
             resolved["start"]["id"], resolved["end"]["id"],
             start_char_offset, end_char_offset, confidence,
             resolved["start_content_hash"],
             resolved["end_content_hash"], principal_id, _now()))
        if members is not None:
            # 钉住的成员 hash 以 manifest 为准（validate 已核与行一致）
            for ordinal, m in enumerate(members):
                conn.execute(
                    "INSERT INTO memory_source_binding_members(binding_id,"
                    " ordinal, source_message_id, content_hash,"
                    " start_char_offset, end_char_offset)"
                    " VALUES(?,?,?,?,?,?)",
                    (binding_id, ordinal, m["source_message_id"],
                     m["content_hash"],
                     start_char_offset if ordinal == 0 else None,
                     end_char_offset if ordinal == len(members) - 1
                     else None))
        audit.record(conn, "source.bound", principal_id,
                     resource_id=memory_id,
                     payload={"binding_id": binding_id,
                              "conversation_id":
                                  resolved["conversation"]["id"],
                              "start": resolved["start"]["id"],
                              "end": resolved["end"]["id"],
                              "confidence": confidence,
                              "members_pinned": len(members) if members
                              is not None else None,
                              "offset_convention":
                                  source_query.OFFSET_CONVENTION})
        return {"binding_id": binding_id, "memory_id": memory_id,
                "conversation_id": resolved["conversation"]["id"],
                "start_message_id": resolved["start"]["id"],
                "end_message_id": resolved["end"]["id"],
                "start_content_hash": resolved["start_content_hash"],
                "end_content_hash": resolved["end_content_hash"],
                "offset_convention": source_query.OFFSET_CONVENTION,
                "confidence": confidence,
                "members_pinned": len(members) if members is not None
                else None}

    if conn is not None:
        return _insert(conn)
    with db.formal() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            out = _insert(c)
            c.execute("COMMIT")
            return out
        except Exception:
            c.execute("ROLLBACK")
            raise


def _validate_members(conn, conversation_id: str, members: list[dict],
                      start_char_offset, end_char_offset) -> None:
    """成员 manifest 校验（契约 §4.4）：形状/归属/发布/路径序/钉 hash。"""
    if not isinstance(members, list) or not members:
        raise Forbidden("members 必须是非空数组", code="INVALID_ARGUMENT")
    ids: list[str] = []
    for i, m in enumerate(members):
        if not isinstance(m, dict):
            raise Forbidden(f"members[{i}] 不是对象", code="INVALID_ARGUMENT")
        sid, chash = m.get("source_message_id"), m.get("content_hash")
        if not isinstance(sid, str) or not sid:
            raise Forbidden(f"members[{i}].source_message_id 必填",
                            code="INVALID_ARGUMENT")
        if not isinstance(chash, str) or not chash:
            raise Forbidden(f"members[{i}].content_hash 必填（钉住证据"
                            "版本）", code="INVALID_ARGUMENT")
        if sid in ids:
            raise Forbidden(f"members[{i}] 重复成员：{sid}",
                            code="INVALID_ARGUMENT")
        ids.append(sid)
    rows = []
    for sid in ids:
        row = conn.execute(
            "SELECT * FROM source_messages WHERE id=?", (sid,)).fetchone()
        if row is None or not row["published"]:
            raise NotFound("member not found or unpublished",
                           source_message_id=sid)
        rows.append(row)
    conv_id = rows[0]["conversation_id"]
    for row in rows:
        if row["conversation_id"] != conv_id:
            raise Forbidden("members 跨会话", code="INVALID_ARGUMENT")
    # 成员必须都在目标会话内（conversation_id 可传行 id 或 provider id）
    target = source_query._find_conversation(conn, conversation_id)
    if target is None or target["id"] != conv_id:
        raise NotFound("member not in selection conversation",
                       conversation_id=conversation_id)
    # 成员必须在首→末 parent 路径上且按路径序（复用区间解析器）
    resolved = source_query.validate_range(
        conversation_id, ids[0], ids[-1], None, None, conn=conn)
    order = {mid: idx for idx, mid in enumerate(resolved["path_order"])}
    last_idx = -1
    for sid in ids:
        if sid not in order:
            raise Forbidden(
                "成员不在首→末 parent 路径上（sibling/分支成员不能入"
                "manifest）", code="SOURCE_RANGE_NOT_PATH",
                source_message_id=sid)
        if order[sid] <= last_idx:
            raise Forbidden("成员次序与 parent 路径序不符",
                            code="SOURCE_RANGE_NOT_PATH",
                            source_message_id=sid)
        last_idx = order[sid]
    # 钉住的 hash 必须与当前行一致（漂移在此拒绝，不静默改绑）
    for m, row in zip(members, rows):
        if row["content_hash"] is not None and \
                row["content_hash"] != m["content_hash"]:
            raise Forbidden(
                "成员 content_hash 与当前行不符（消息已修订；按新修订"
                "重新生成 manifest）", code="SOURCE_HASH_MISMATCH",
                source_message_id=m["source_message_id"])


def open_selection(selection: dict, include_content: bool = False) -> dict:
    """按 manifest 读回片段（source.selection.open；契约 §4.4）。

    与旧 range.open 的差别：逐成员核对钉住的 content_hash——当前行与
    manifest 不符时该成员显式标 version_drift（结构性披露，不静默给
    新正文也不拒整段）；旧修订证据可另走 source.message.get。
    """
    if not isinstance(selection, dict):
        raise Forbidden("selection 必须是对象", code="INVALID_ARGUMENT")
    members = selection.get("members")
    conversation_id = selection.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise Forbidden("selection.conversation_id 必填",
                        code="INVALID_ARGUMENT")
    if not isinstance(members, list) or not members:
        raise Forbidden("selection.members 必须是非空数组",
                        code="INVALID_ARGUMENT")
    with db.formal() as conn:
        conv = source_query._find_conversation(conn, conversation_id)
        if conv is None:
            raise NotFound("source conversation not found",
                           conversation_id=conversation_id)
        out_members = []
        drifted = []
        for i, m in enumerate(members):
            sid = m.get("source_message_id") if isinstance(m, dict) else None
            if not isinstance(sid, str):
                raise Forbidden(f"members[{i}].source_message_id 必填",
                                code="INVALID_ARGUMENT")
            row = conn.execute(
                f"SELECT {source_query._COLS}, conversation_id,"
                " content_json FROM source_messages WHERE id=? AND"
                " published=1",
                (sid,)).fetchone()
            if row is None or row["conversation_id"] != conv["id"]:
                raise NotFound("member not found in conversation",
                               source_message_id=sid)
            item = source_query._serialize(
                row, include_content=include_content,
                char_offsets=(
                    selection.get("start_char_offset") if i == 0 else None,
                    selection.get("end_char_offset")
                    if i == len(members) - 1 else None))
            pinned = m.get("content_hash")
            member_drift = None
            if isinstance(pinned, str):
                if row["content_hash"] is not None and \
                        row["content_hash"] != pinned:
                    # 行级不符（理论上不可变行不该发生；防御性披露）
                    member_drift = {"pinned": pinned,
                                    "current": row["content_hash"]}
            if member_drift is None and isinstance(pinned, str):
                # 在线修订换代：钉住行不可变，漂移信号=同 origin 消息已
                # 有更新修订（谱系表判定，不比对不可变行自身）
                lr = conn.execute(
                    "SELECT l.revision, (SELECT MAX(l2.revision) FROM"
                    " source_live_revisions l2 WHERE l2.stream_id="
                    " l.stream_id AND l2.origin_message_id="
                    " l.origin_message_id) AS max_rev, (SELECT"
                    " l3.content_hash FROM source_live_revisions l3"
                    " WHERE l3.stream_id=l.stream_id AND"
                    " l3.origin_message_id=l.origin_message_id ORDER BY"
                    " l3.revision DESC LIMIT 1) AS latest_hash FROM"
                    " source_live_revisions l WHERE l.source_message_id=?",
                    (sid,)).fetchone()
                if lr is not None and lr["max_rev"] is not None and \
                        lr["max_rev"] > lr["revision"]:
                    member_drift = {"pinned": pinned,
                                    "current": lr["latest_hash"],
                                    "pinned_revision": lr["revision"],
                                    "latest_revision": lr["max_rev"]}
            if member_drift is not None:
                item["version_drift"] = member_drift
                drifted.append(sid)
            out_members.append(item)
    return {"conversation": dict(conv), "members": out_members,
            "drifted_members": drifted, "include_content": include_content,
            "source": "source_layer", "content_role": "retrieved_memory",
            "instruction_authority": "none"}


def ranges_of(memory_id: str) -> list[dict]:
    """memory 的全部有效绑定（全部行均有效——纠错历史在别处，§5.2）。"""
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_source_bindings WHERE memory_id=?"
            " ORDER BY created_at",
            (memory_id,)).fetchall()
    return [dict(r) for r in rows]


def memories_referencing(message_id: str) -> list[dict]:
    """§6.1 Source 反查：这段原文消息被哪些绑定区间实际覆盖。

    精确判定按消息序范围（start..end 区间含该消息），不把"同
    conversation"当"同一段"；返回绑定身份与范围供两端继续追查。
    """
    with db.formal() as conn:
        msg = conn.execute(
            "SELECT id, provider_message_id, conversation_id, sequence,"
            " provider FROM source_messages WHERE id=? OR"
            " provider_message_id=?",
            (message_id, message_id)).fetchone()
        if msg is None:
            return []
        seqs = conn.execute(
            "SELECT sequence FROM source_messages WHERE conversation_id=?"
            " AND (id=? OR provider_message_id=?)",
            (msg["conversation_id"], message_id, message_id)).fetchall()
        seq_set = {r["sequence"] for r in seqs}
        rows = conn.execute(
            "SELECT b.*, s.sequence AS start_seq, e.sequence AS end_seq,"
            " s.id AS s_id, s.provider_message_id AS s_pid,"
            " e.id AS e_id, e.provider_message_id AS e_pid"
            " FROM memory_source_bindings b"
            " JOIN source_messages s ON s.id=b.start_message_id"
            " JOIN source_messages e ON e.id=b.end_message_id"
            " WHERE b.conversation_id=?", (msg["conversation_id"],)).fetchall()
    out = []
    from . import query as _query
    for r in rows:
        # SRC-07（2026-10-04 全量审计）：覆盖以共享范围解析器的实际
        # parent 路径成员为准——sibling 序号落点不等于路径身份；
        # 解析失败（None）保守跳过该绑定
        path_ids = _query.covered_path_ids(
            msg["conversation_id"], r["start_message_id"],
            r["end_message_id"])
        if path_ids is None:
            continue
        if msg["id"] in path_ids:
            d = {k: v for k, v in dict(r).items()
                 if k not in ("start_seq", "end_seq", "s_id", "s_pid",
                              "e_id", "e_pid")}
            out.append(d)
    return out


def correct(principal_id: str, binding_id: str,
            correction_action: str, note: str | None = None,
            replacement: dict | None = None, conn=None) -> dict:
    """§P-R02 纠错：撤销/改绑错误区间绑定（替代旧 revoke）。

    同一事务：登记纠错历史→删该条有效绑定→（改绑）经完整区间校验
    创建新绑定。不删除/改写原文母本或消息（§5.5 Source）。
    """
    from ..relations.corrections import record_correction
    if correction_action not in ("remove_wrong_binding",
                                 "replace_wrong_binding"):
        raise Forbidden("correction_action must be remove_wrong_binding/"
                        "replace_wrong_binding")
    # CB-006（2026-10-02 审计 P1）：replace 必带 replacement、remove 禁带
    if correction_action == "remove_wrong_binding" and replacement:
        raise Forbidden("remove_wrong_binding 不接受 replacement（移除"
                        "语义；改绑请用 replace_wrong_binding）",
                        code="INVALID_ARGUMENT")
    if correction_action == "replace_wrong_binding" and not replacement:
        raise Forbidden("replace_wrong_binding 必须携带完整 replacement"
                        "——缺新绑定的替换即撤销", code="INVALID_ARGUMENT")

    def _do(conn):
        row = conn.execute(
            "SELECT * FROM memory_source_bindings WHERE binding_id=?",
            (binding_id,)).fetchone()
        if row is None:
            raise NotFound("source binding not found",
                           binding_id=binding_id)
        # CB-006：先删旧实例再建新实例——bind 命中同键旧绑定时复用再
        # 删除会把替换变成撤销，回执指向不存在的 replacement
        conn.execute(
            "DELETE FROM memory_source_bindings WHERE binding_id=?",
            (binding_id,))
        replacement_id = None
        if replacement:
            rep = bind(principal_id,
                       replacement.get("memory_id", row["memory_id"]),
                       replacement.get("conversation_id", ""),
                       replacement.get("start_message_id", ""),
                       replacement.get("end_message_id", ""),
                       start_char_offset=replacement.get(
                           "start_char_offset"),
                       end_char_offset=replacement.get(
                           "end_char_offset"),
                       confidence=replacement.get("confidence", "exact"),
                       conn=conn)
            replacement_id = rep.get("binding_id", binding_id)
        cid = record_correction(
            conn, domain="source_binding",
            original_instance_id=binding_id,
            endpoint_a=row["memory_id"],
            endpoint_b=row["conversation_id"],
            original_meta={
                "start_message_id": row["start_message_id"],
                "end_message_id": row["end_message_id"],
                "start_char_offset": row["start_char_offset"],
                "end_char_offset": row["end_char_offset"],
                "bind_confidence": row["bind_confidence"]},
            original_created_by=row["created_by"],
            original_created_at=row["created_at"],
            corrected_by=principal_id, note=note,
            replacement_instance_id=replacement_id)
        audit.record(conn, "source.binding.corrected", principal_id,
                     resource_id=binding_id,
                     payload={"correction_id": cid})
        return {"correction_id": cid, "removed_binding_id": binding_id,
                "replacement_binding_id": replacement_id,
                "action": correction_action}

    if conn is not None:
        return _do(conn)
    with db.formal() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            out = _do(c)
            c.execute("COMMIT")
            return out
        except Exception:
            c.execute("ROLLBACK")
            raise


def open_for_memory(memory_id: str, include_content: bool = False) -> dict:
    """按绑定动态读取原文区间（裁切片段；版本漂移显式标注不静默）。"""
    ranges = ranges_of(memory_id)
    if not ranges:
        return {"memory_id": memory_id, "ranges": [], "source": "source_layer"}
    opened = []
    for rng in ranges:
        try:
            payload = source_query.open_range(
                rng["conversation_id"], rng["start_message_id"],
                rng["end_message_id"],
                start_char_offset=rng["start_char_offset"],
                end_char_offset=rng["end_char_offset"],
                include_content=include_content)
        except NotFound:
            opened.append({"binding_id": rng["binding_id"],
                           "status": "unresolvable",
                           "confidence": rng["bind_confidence"],
                           "bound_at": rng["created_at"]})
            continue
        # 版本漂移检测（当前行内容与绑定时 hash 不同 → 显式标注）
        drift = None
        for label, bound_hash, key in (
                ("start", rng["start_content_hash"],
                 "start_provider_message_id"),
                ("end", rng["end_content_hash"],
                 "end_provider_message_id")):
            current = _current_hash(rng["conversation_id"], payload[key])
            if bound_hash and current and current != bound_hash:
                drift = drift or {}
                drift[label] = {"bound": bound_hash, "current": current}
        opened.append({"binding_id": rng["binding_id"],
                       "confidence": rng["bind_confidence"],
                       "bound_at": rng["created_at"],
                       "version_drift": drift,
                       **payload})
    return {"memory_id": memory_id, "ranges": opened,
            "source": "source_layer"}


def _current_hash(conversation_row_id: str, provider_message_id: str):
    with db.formal() as conn:
        row = conn.execute(
            "SELECT content_hash FROM source_messages WHERE conversation_id=?"
            " AND provider_message_id=?",
            (conversation_row_id, provider_message_id)).fetchone()
    return row["content_hash"] if row else None


def reindex_search_docs(batch: int = 500) -> dict:
    """source_search_docs / source_fts 由 source_messages 游标分批重建。"""
    import hashlib
    from ..retrieval import projection
    n = 0
    last_id = ""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM source_fts")
            conn.execute("DELETE FROM source_search_docs")
            while True:
                rows = conn.execute(
                    "SELECT id, provider_message_id, text FROM"
                    " source_messages WHERE id>? AND text<>'' AND"
                    " normalized_sender IN ('human','assistant')"
                    " ORDER BY id LIMIT ?", (last_id, batch)).fetchall()
                if not rows:
                    break
                for r in rows:
                    text_norm = projection.normalize_search_text(r["text"])
                    conn.execute(
                        "INSERT INTO source_search_docs(message_id,"
                        " provider_message_id, text_norm, text_hash,"
                        " projection_version, built_at) VALUES(?,?,?,?,?,?)",
                        (r["id"], r["provider_message_id"], text_norm,
                         hashlib.sha256(r["text"].encode()).hexdigest(),
                         config.SOURCE_PROJECTION_VERSION, _now()))
                    conn.execute(
                        "INSERT INTO source_fts(message_id, text_norm)"
                        " VALUES(?,?)", (r["id"], text_norm))
                    n += 1
                last_id = rows[-1]["id"]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"rebuilt_docs": n, "batch_size": batch}
