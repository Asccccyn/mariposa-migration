"""语义事件（episode）服务——她批准 2026-10-07"做吧，我需要这个功能"。

蓝图 `MARIPOSA_LIFECYCLE_v1.0/00_GLM执行方案.md` §6 最小实施：
- 主模型判断语义（不用规则/阈值/沉默时长/后台模型替代）
- 三态 OPEN / QUIESCENT / CLOSED；CLOSED 拒绝普通 continue
- 纠错 correct 恢复非 CLOSED；旧闭合留审计历史
- 短标签≤80 码点仅导航；无叙事正文、不参与检索
- 同 op 幂等；expected_revision CAS；scope 服务端校验
- 不自动合并/不自动关闭/技术断开不等于语义暂停

复审返修（2026-10-07，LINSHIJIAN_REVIEW R05-R09）：
- R09：批准范围→身份→动作 服务端映射（episode_scope_grants 数据表），
  读/写/列表统一核验；她裁定=qiaosheng 读全量（通配）+改类动作
  （continue/pause/close/correct）+连线，start（新建=写）仅 jiaming
- R06：operation_id 领域操作身份——业务事务内原子完成状态+审计+完成
  回执；同 op 重试回放原结果（历史结果与当前 revision 分列）
- R07：来源证据核验——成员/终点/判断依据必须是真实存在、已发布、
  hash 钉住的 Source 行；终点属于合并后的事件成员
- R08：correct 真正应用成员/边界纠错（note+判断依据必填）；纠正到
  CLOSED 须满足闭合不变量；审计保留纠正前后完整证据身份
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, IdempotencyConflict, NotFound
from ..memory import service as memory

_STATES = ("OPEN", "QUIESCENT", "CLOSED")
# 蓝图 §2.2 转移合同：动作×当前状态→结果
_TRANSITIONS: dict[str, dict[str, str]] = {
    "continue": {"OPEN": "OPEN", "QUIESCENT": "OPEN"},
    "pause": {"OPEN": "QUIESCENT", "QUIESCENT": "QUIESCENT"},
    "close": {"OPEN": "CLOSED", "QUIESCENT": "CLOSED"},
}
_REASON_CODES = {
    "start": ("tracking_requested",),
    "continue": ("same_episode",),
    "pause": ("waiting",),
    "close": ("explicit_completion", "goal_resolved", "episode_concluded"),
    "correct": ("boundary_error",),
}
_ALLOWED_LABEL_LEN = 80
#: 改类动作（她 1007 裁定"改的权利除 i 外全开"）；start=新建=写，仅 jiaming
_DEFAULT_SCOPE_GRANTS = {
    ("primary", "jiaming"):
        ["read", "list", "start", "continue", "pause", "close", "correct"],
    ("primary", "qiaosheng"):
        ["read", "list", "continue", "pause", "close", "correct"],
    ("*", "qiaosheng"): ["read", "list"],  # 读全量通配（读同步她一份）
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _code_point_len(text: str) -> int:
    return len(text)


# ---------- R09：范围→身份→动作 授权映射 ----------

def _ensure_default_grants(conn) -> None:
    now = _now()
    for (scope, pid), actions in _DEFAULT_SCOPE_GRANTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO episode_scope_grants(scope_id,"
            " principal_id, actions, created_at) VALUES(?,?,?,?)",
            (scope, pid, json.dumps(actions), now))


def _grants_for(conn, principal_id: str, scope_id: str) -> set:
    rows = conn.execute(
        "SELECT actions FROM episode_scope_grants WHERE principal_id=?"
        " AND scope_id IN (?,'*')", (principal_id, scope_id)).fetchall()
    allowed: set = set()
    for r in rows:
        allowed.update(json.loads(r["actions"]))
    return allowed


def _authorize(conn, principal, scope_id: str, action: str) -> None:
    _ensure_default_grants(conn)
    if action not in _grants_for(conn, principal.principal_id, scope_id):
        raise Forbidden(
            f"身份 {principal.principal_id} 无权在范围 {scope_id} 执行"
            f" {action}（R09 授权映射核验）",
            code="EPISODE_SCOPE_FORBIDDEN", scope_id=scope_id,
            action=action, principal=principal.principal_id)


# ---------- R07：来源证据核验（事务内，对齐 source 层既有口径） ----------

def _published_row(conn, sid: str, field: str) -> dict:
    row = conn.execute(
        "SELECT content_hash, conversation_id, published, text FROM"
        " source_messages WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise Forbidden(f"{field} 指向不存在的 Source：{sid}",
                        code="EPISODE_SOURCE_NOT_FOUND",
                        source_message_id=sid)
    if row["published"] != 1:
        raise Forbidden(f"{field} 指向未发布 Source：{sid}",
                        code="EPISODE_SOURCE_UNPUBLISHED",
                        source_message_id=sid)
    return dict(row)


def _verify_member(conn, seg_i: int, member, conv_id: str) -> None:
    sid = member.get("source_message_id")
    pinned = member.get("content_hash")
    if not isinstance(sid, str) or not sid:
        raise Forbidden(f"segment[{seg_i}] member 缺 source_message_id",
                        code="INVALID_ARGUMENT")
    if not isinstance(pinned, str) or not pinned:
        raise Forbidden(
            f"segment[{seg_i}] member 缺 content_hash（钉住证据版本）",
            code="EPISODE_SOURCE_HASH_MISMATCH", source_message_id=sid)
    row = _published_row(conn, sid, f"segment[{seg_i}] member")
    if row["content_hash"] != pinned:
        raise Forbidden(
            f"segment[{seg_i}] member content_hash 与已发布行不符：{sid}",
            code="EPISODE_SOURCE_HASH_MISMATCH", source_message_id=sid)
    if conv_id and row["conversation_id"] != conv_id:
        raise Forbidden(
            f"segment[{seg_i}] member 不属于段声明的会话：{sid}",
            code="EPISODE_SOURCE_CONV_MISMATCH", source_message_id=sid)


def _verify_segments_tx(conn, raw) -> list[dict]:
    """形状校验（原 _validate_segments）+ 事务内核验真实已发布 Source。"""
    if not isinstance(raw, list) or not raw:
        raise Forbidden("source_selections 非空数组必填", code="INVALID_ARGUMENT")
    out = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, dict):
            raise Forbidden(f"segment[{i}] 必须是对象", code="INVALID_ARGUMENT")
        conv = seg.get("conversation_id")
        members = seg.get("members")
        if not isinstance(conv, str) or not conv:
            raise Forbidden(f"segment[{i}].conversation_id 必填",
                            code="INVALID_ARGUMENT")
        if not isinstance(members, list) or not members:
            raise Forbidden(f"segment[{i}].members 非空", code="INVALID_ARGUMENT")
        for m in members:
            if not isinstance(m, dict):
                raise Forbidden(f"segment[{i}] member 必须是对象",
                                code="INVALID_ARGUMENT")
            _verify_member(conn, i, m, conv)
        # 偏移口径对齐 binding：start 在首成员文本、end 在末成员文本（码点半开区间）
        for label, off in (("start_char_offset", seg.get("start_char_offset")),
                           ("end_char_offset", seg.get("end_char_offset"))):
            if off is None:
                continue
            bound_i = 0 if label == "start_char_offset" else len(members) - 1
            text = _published_row(
                conn, members[bound_i]["source_message_id"],
                f"segment[{i}]").get("text") or ""
            if isinstance(off, bool) or not isinstance(off, int) \
                    or not (0 <= off <= len(text)):
                raise Forbidden(
                    f"segment[{i}].{label} 超出成员文本范围（码点半开区间）",
                    code="EPISODE_SOURCE_OFFSET", value=repr(off),
                    text_len=len(text))
        # WP-08（CX-05/C-011）+RRA-002（2026-10-09 复审）：start<end 只在
        # 首末成员为**同一消息**时成立（同一坐标轴）——start 偏移属于首
        # 成员文本、end 偏移属于末成员文本（Source 绑定口径），跨消息
        # 片段各偏移只需各自界内且成员路径有序；跨消息时比较两个局部
        # 坐标是误拒合法片段（如 first offset7 + last offset2）
        s_off = seg.get("start_char_offset")
        e_off = seg.get("end_char_offset")
        if s_off is not None and e_off is not None:
            first_mid = members[0]["source_message_id"]
            last_mid = members[-1]["source_message_id"]
            if first_mid == last_mid and not (s_off < e_off):
                raise Forbidden(
                    f"segment[{i}] start_char_offset 必须 < "
                    "end_char_offset（同一消息的码点半开区间）",
                    code="INVALID_RANGE", start=s_off, end=e_off)
        item = {"conversation_id": conv, "members": members}
        for k in ("start_char_offset", "end_char_offset"):
            if seg.get(k) is not None:
                item[k] = seg[k]
        out.append(item)
    if len(out) > 16:
        raise Forbidden("source_selections 最多 16 段", code="INVALID_ARGUMENT")
    return out


def _verify_ref(conn, raw, field: str) -> dict:
    """terminal/decision 通用核验：真实、已发布、hash 钉住、偏移界内。"""
    if not isinstance(raw, dict) or not raw.get("source_message_id"):
        raise Forbidden(f"{field} 缺 source_message_id", code="INVALID_ARGUMENT")
    sid = raw["source_message_id"]
    if not raw.get("content_hash"):
        raise Forbidden(f"{field} 缺 content_hash（钉住证据版本）",
                        code="EPISODE_SOURCE_HASH_MISMATCH",
                        source_message_id=sid)
    row = _published_row(conn, sid, field)
    if row["content_hash"] != raw["content_hash"]:
        raise Forbidden(f"{field} content_hash 与已发布行不符：{sid}",
                        code="EPISODE_SOURCE_HASH_MISMATCH",
                        source_message_id=sid)
    off = raw.get("char_offset")
    if off is not None:
        text = row["text"] or ""
        if isinstance(off, bool) or not isinstance(off, int) \
                or not (0 <= off <= len(text)):
            raise Forbidden(f"{field}.char_offset 超出文本范围",
                            code="EPISODE_SOURCE_OFFSET", value=repr(off),
                            text_len=len(text))
    out = {"source_message_id": sid, "content_hash": raw["content_hash"]}
    if off is not None:
        out["char_offset"] = off
    return out


def _member_ids(segments: list[dict]) -> set:
    return {m["source_message_id"] for seg in segments for m in seg["members"]}


def _validate_segments(raw) -> list[dict]:
    """纯形状校验（无连接版本；事务内核验走 _verify_segments_tx）。"""
    if not isinstance(raw, list) or not raw:
        raise Forbidden("source_selections 非空数组必填", code="INVALID_ARGUMENT")
    out = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, dict):
            raise Forbidden(f"segment[{i}] 必须是对象", code="INVALID_ARGUMENT")
        conv = seg.get("conversation_id")
        members = seg.get("members")
        if not isinstance(conv, str) or not conv:
            raise Forbidden(f"segment[{i}].conversation_id 必填",
                            code="INVALID_ARGUMENT")
        if not isinstance(members, list) or not members:
            raise Forbidden(f"segment[{i}].members 非空", code="INVALID_ARGUMENT")
        for m in members:
            if not isinstance(m, dict) or not m.get("source_message_id") \
                    or not m.get("content_hash"):
                raise Forbidden(
                    f"segment[{i}] member 缺 source_message_id/content_hash",
                    code="INVALID_ARGUMENT")
        # WP-08（CX-05/C-011）+RRA-002（2026-10-09 复审）：start<end 只在
        # 首末成员为**同一消息**时成立（同一坐标轴）——start 偏移属于首
        # 成员文本、end 偏移属于末成员文本（Source 绑定口径），跨消息
        # 片段各偏移只需各自界内且成员路径有序；跨消息时比较两个局部
        # 坐标是误拒合法片段（如 first offset7 + last offset2）
        s_off = seg.get("start_char_offset")
        e_off = seg.get("end_char_offset")
        if s_off is not None and e_off is not None:
            first_mid = members[0]["source_message_id"]
            last_mid = members[-1]["source_message_id"]
            if first_mid == last_mid and not (s_off < e_off):
                raise Forbidden(
                    f"segment[{i}] start_char_offset 必须 < "
                    "end_char_offset（同一消息的码点半开区间）",
                    code="INVALID_RANGE", start=s_off, end=e_off)
        item = {"conversation_id": conv, "members": members}
        for k in ("start_char_offset", "end_char_offset"):
            if seg.get(k) is not None:
                item[k] = seg[k]
        out.append(item)
    if len(out) > 16:
        raise Forbidden("source_selections 最多 16 段", code="INVALID_ARGUMENT")
    return out


def _dedup_append(current: list[dict], new: list[dict]) -> list[dict]:
    """蓝图 §6.2：continue/close 的 selections 只能去重追加，不覆盖历史。"""
    seen = set()
    for seg in current:
        key = json.dumps(seg, ensure_ascii=False, sort_keys=True)
        seen.add(key)
    out = list(current)
    for seg in new:
        key = json.dumps(seg, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            out.append(seg)
            seen.add(key)
    return out


def _get_episode(conn, episode_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM semantic_episodes WHERE episode_id=?",
        (episode_id,)).fetchone()
    if row is None:
        raise NotFound("episode not found", episode_id=episode_id)
    return dict(row)


def _emit(row: dict) -> dict:
    """出站投影：状态+证据引用+导航信息；无正文展开。"""
    return {
        "episode_id": row["episode_id"],
        "scope_id": row["scope_id"],
        "label": row["label"],
        "state": row["state"],
        "revision": row["revision"],
        "source_selections": json.loads(row["source_selections_json"]),
        "terminal_source_ref": json.loads(row["terminal_source_ref"])
        if row["terminal_source_ref"] else None,
        "decision_source_refs": json.loads(row["decision_source_refs_json"]),
        "last_reason_code": row["last_reason_code"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_recorded_at": row["closed_recorded_at"],
    }


def apply(principal, a: dict) -> dict:
    """episode.apply：start/continue/pause/close/correct + 授权 + 幂等 + CAS。"""
    action = a.get("action")
    if action not in ("start", "continue", "pause", "close", "correct"):
        raise Forbidden("action 必须是 start/continue/pause/close/correct",
                        code="INVALID_ARGUMENT")
    reason = a.get("reason_code")
    if reason not in _REASON_CODES.get(action, ()):
        raise Forbidden(
            f"action={action} 的合法 reason_code：{_REASON_CODES[action]}",
            code="INVALID_ARGUMENT", got=reason)
    scope_id = a.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id:
        raise Forbidden("scope_id 必填（服务端授权映射的范围）",
                        code="INVALID_ARGUMENT")
    note = a.get("note") or ""
    if len(note) > 500:
        raise Forbidden("note 最长 500 字符", code="INVALID_ARGUMENT")

    # R06：宿主稳定操作身份——授权核验之后、业务事务之内完成回执；
    # 同 op 同 payload 回放原结果（历史结果与当前 revision 分列），
    # 同 op 异 payload 结构化冲突。不带 operation_id 的一次性调用不受影响。
    operation_id = a.get("operation_id")
    op_key = f"episode.apply:{operation_id}" if isinstance(
        operation_id, str) and operation_id else None
    ph = memory.canonical_hash(
        {k: v for k, v in a.items() if k != "operation_id"})

    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 授权先行（重放也须当前授权有效——撤权后不得回放旧结果）
            _authorize(conn, principal, scope_id, action)
            if op_key is not None:
                row = conn.execute(
                    "SELECT * FROM episode_operations WHERE operation_key=?",
                    (op_key,)).fetchone()
                if row is not None:
                    if row["payload_hash"] != ph:
                        raise IdempotencyConflict(
                            "同 operation_id 已绑定不同内容的请求",
                            operation_id=operation_id)
                    out = json.loads(row["result_json"])
                    replay = dict(out)
                    replay["idempotent_replay"] = True
                    cur = conn.execute(
                        "SELECT state, revision FROM semantic_episodes"
                        " WHERE episode_id=?",
                        (out.get("episode_id"),)).fetchone()
                    if cur is not None:
                        # 分列：历史操作结果 vs 当前最新状态
                        replay["current_state"] = cur["state"]
                        replay["current_revision"] = cur["revision"]
                    return replay
            out = _apply_tx(conn, principal, action, scope_id, reason, a, note)
            if op_key is not None:
                conn.execute(
                    "INSERT INTO episode_operations(operation_key,"
                    " principal_id, payload_hash, result_json, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (op_key, principal.principal_id, ph,
                     json.dumps(out, ensure_ascii=False), _now()))
            conn.execute("COMMIT")
            return out
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _apply_tx(conn, principal, action, scope_id, reason, a, note) -> dict:
    now = _now()

    if action == "start":
        label = a.get("label")
        if not isinstance(label, str) or not label.strip():
            raise Forbidden("start 必须带 label（短标签）", code="INVALID_ARGUMENT")
        if _code_point_len(label) > _ALLOWED_LABEL_LEN:
            raise Forbidden(f"label 最长 {_ALLOWED_LABEL_LEN} 码点",
                            code="INVALID_ARGUMENT")
        segments = _verify_segments_tx(conn, a.get("source_selections"))
        eid = f"ep_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO semantic_episodes(episode_id, scope_id, label,"
            " state, revision, source_selections_json, decision_source_refs_json,"
            " last_reason_code, created_by, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (eid, scope_id, label.strip(), "OPEN", 1,
             json.dumps(segments, ensure_ascii=False),
             json.dumps([], ensure_ascii=False),
             reason, principal.principal_id, now, now))
        audit.record(conn, "episode.started", principal.principal_id,
                     resource_id=eid,
                     payload={"scope": scope_id, "label": label.strip(),
                              "reason": reason, "segments": len(segments),
                              "members": sorted(_member_ids(segments))})
        return {"episode_id": eid, "state": "OPEN", "revision": 1,
                "label": label.strip(), "scope_id": scope_id}

    # 以下动作都需要 episode_id + expected_revision（CAS）
    eid = a.get("episode_id")
    if not eid:
        raise Forbidden(f"{action} 必须带 episode_id", code="INVALID_ARGUMENT")
    row = _get_episode(conn, eid)
    if row["scope_id"] != scope_id:
        raise Forbidden("scope 不匹配（跨 scope 操作拒绝）",
                        code="FORBIDDEN", episode=eid)
    expected = a.get("expected_revision")
    if not isinstance(expected, int) or expected < 1:
        raise Forbidden("expected_revision 必须为正整数（CAS）",
                        code="INVALID_ARGUMENT")
    if row["revision"] != expected:
        raise Forbidden(
            f"版本冲突：期望 r{expected}，当前 r{row['revision']}"
            "（先读新状态再判断，不自动盖旧判断）",
            code="VERSION_CONFLICT", current_revision=row["revision"])

    prev_state = row["state"]
    prev_members = json.loads(row["source_selections_json"])
    prev_decision = json.loads(row["decision_source_refs_json"])

    if action == "correct":
        # R08：correct 真正应用成员/边界纠错；note 与判断依据必填（合同
        # §221-225）；纠正到 CLOSED 须满足闭合不变量；旧证据完整留痕
        corrected = a.get("corrected_state")
        if corrected not in _STATES:
            raise Forbidden("corrected_state 必须是 OPEN/QUIESCENT/CLOSED",
                            code="INVALID_ARGUMENT")
        if not note or not note.strip():
            raise Forbidden("correct 必须带 note（纠错理由留痕，合同必填）",
                            code="INVALID_ARGUMENT")
        decision = a.get("decision_source_refs")
        if not isinstance(decision, list) or not decision:
            raise Forbidden(
                "correct 必须带 decision_source_refs（判断依据，合同必填）",
                code="INVALID_ARGUMENT")
        new_decision = [_verify_ref(conn, d, "decision_source_refs")
                        for d in decision]
        new_members = prev_members
        if a.get("source_selections") is not None:
            new_members = _verify_segments_tx(conn, a.get("source_selections"))
        if corrected == "CLOSED":
            terminal = a.get("terminal_source_ref")
            if not terminal:
                raise Forbidden(
                    "纠正到 CLOSED 必须带 terminal_source_ref"
                    "（闭合不变量：来源证据+成员终点+闭合时刻）",
                    code="INVALID_ARGUMENT")
            new_terminal = _verify_ref(conn, terminal,
                                       "terminal_source_ref")
            if new_terminal["source_message_id"] not in _member_ids(new_members):
                raise Forbidden(
                    "terminal 不属于合并后的事件成员（纠正后的成员集合）",
                    code="EPISODE_TERMINAL_NOT_MEMBER",
                    source_message_id=new_terminal["source_message_id"])
            terminal_ref = json.dumps(new_terminal, ensure_ascii=False)
            new_closed = now
        else:
            # 蓝图 §6.2：纠错恢复非 CLOSED → 清空闭合记录；旧值留审计
            terminal_ref = None
            new_closed = None
        conn.execute(
            "UPDATE semantic_episodes SET state=?, revision=?,"
            " source_selections_json=?, terminal_source_ref=?,"
            " decision_source_refs_json=?, last_reason_code=?,"
            " closed_recorded_at=?, updated_at=? WHERE episode_id=?",
            (corrected, row["revision"] + 1,
             json.dumps(new_members, ensure_ascii=False),
             terminal_ref,
             json.dumps(new_decision, ensure_ascii=False),
             reason, new_closed, now, eid))
        audit.record(conn, "episode.corrected", principal.principal_id,
                     resource_id=eid,
                     payload={
                         "from": prev_state, "to": corrected,
                         "prev_revision": row["revision"],
                         "prev_members": sorted(_member_ids(prev_members)),
                         "prev_terminal": json.loads(
                             row["terminal_source_ref"])
                         if row["terminal_source_ref"] else None,
                         "prev_decision_refs": sorted(
                             d["source_message_id"] for d in prev_decision),
                         "prev_closed_at": row["closed_recorded_at"],
                         "corrected_members": sorted(
                             _member_ids(new_members)),
                         "corrected_decision_refs": sorted(
                             d["source_message_id"] for d in new_decision),
                         "reason": reason, "note": note[:200]})
        return {"episode_id": eid, "state": corrected,
                "revision": row["revision"] + 1,
                "corrected_from": prev_state,
                "source_selections": new_members,
                "decision_source_refs": new_decision}

    # continue / pause / close
    allowed_starts = _TRANSITIONS.get(action, {})
    if prev_state not in allowed_starts:
        raise Forbidden(
            f"{action} 不允许当前状态 {prev_state}"
            + ("（CLOSED 不接受普通 continue；确为误关走 correct）"
               if action == "continue" and prev_state == "CLOSED" else ""),
            code="INVALID_TRANSITION", current=prev_state)

    new_state = allowed_starts[prev_state]
    segments_now = prev_members

    if action in ("continue", "close"):
        new_segs = a.get("source_selections")
        if new_segs:
            validated = _verify_segments_tx(conn, new_segs)
            segments_now = _dedup_append(segments_now, validated)

    terminal_ref = row["terminal_source_ref"]
    closed_at = row["closed_recorded_at"]
    decision_refs = prev_decision

    if action == "close":
        terminal = a.get("terminal_source_ref")
        if not terminal:
            raise Forbidden("close 必须带 terminal_source_ref"
                            "（截至何处的原文锚点）", code="INVALID_ARGUMENT")
        new_terminal = _verify_ref(conn, terminal, "terminal_source_ref")
        if new_terminal["source_message_id"] not in _member_ids(segments_now):
            raise Forbidden(
                "terminal 不属于合并后的事件成员（含本次 continue 追加）",
                code="EPISODE_TERMINAL_NOT_MEMBER",
                source_message_id=new_terminal["source_message_id"])
        terminal_ref = json.dumps(new_terminal, ensure_ascii=False)
        decision = a.get("decision_source_refs")
        if not decision:
            raise Forbidden("close 必须带 decision_source_refs"
                            "（至少一个判断依据）", code="INVALID_ARGUMENT")
        if isinstance(decision, list):
            decision_refs = [_verify_ref(conn, d, "decision_source_refs")
                             for d in decision]
        else:
            decision_refs = [_verify_ref(conn, decision,
                                         "decision_source_refs")]
        closed_at = now  # 记录闭合决定时刻，不是事件实际结束时间

    # pause 可返回 no_change（不制造无意义版本历史）
    if action == "pause" and prev_state == "QUIESCENT":
        audit.record(conn, "episode.pause.no_change",
                     principal.principal_id, resource_id=eid,
                     payload={"reason": reason})
        return {"episode_id": eid, "state": "QUIESCENT",
                "revision": row["revision"], "no_change": True}

    conn.execute(
        "UPDATE semantic_episodes SET state=?, revision=?,"
        " source_selections_json=?, terminal_source_ref=?,"
        " decision_source_refs_json=?, last_reason_code=?,"
        " closed_recorded_at=?, updated_at=? WHERE episode_id=?",
        (new_state, row["revision"] + 1,
         json.dumps(segments_now, ensure_ascii=False),
         terminal_ref,
         json.dumps(decision_refs, ensure_ascii=False),
         reason, closed_at, now, eid))
    audit.record(conn, f"episode.{action}d", principal.principal_id,
                 resource_id=eid,
                 payload={"from": prev_state, "to": new_state,
                          "reason": reason, "note": note[:200],
                          "segments_total": len(segments_now),
                          "decision_refs": sorted(
                              d["source_message_id"] for d in decision_refs)
                          if action == "close" else None})
    return {"episode_id": eid, "state": new_state,
            "revision": row["revision"] + 1}


def get(principal, a: dict) -> dict:
    eid = a.get("episode_id")
    if not eid:
        raise Forbidden("episode_id 必填", code="INVALID_ARGUMENT")
    with db.formal() as conn:
        _ensure_default_grants(conn)
        row = _get_episode(conn, eid)
        # R09：按事件归属范围核验读权（通配=读全量；无授权=拒）
        _authorize(conn, principal, row["scope_id"], "read")
        return _emit(row)


def list_episodes(principal, a: dict) -> dict:
    """蓝图 §6：scope 内分页读取状态；不给全库未完故事。"""
    scope = a.get("scope_id")
    if not scope:
        raise Forbidden("scope_id 必填（不默认全库）", code="INVALID_ARGUMENT")
    with db.formal() as conn:
        _authorize(conn, principal, scope, "list")
        states = a.get("states")
        if states is not None:
            if not isinstance(states, list) or \
                    not all(s in _STATES for s in states):
                raise Forbidden(f"states 只能是 {_STATES} 子集",
                                code="INVALID_ARGUMENT")
        limit = a.get("limit", 20)
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            limit = 20
        where = "scope_id=?"
        params: list = [scope]
        if states:
            marks = ",".join("?" * len(states))
            where += f" AND state IN ({marks})"
            params += states
        # 默认最近更新在前；游标=updated_at+episode_id keyset
        cursor = a.get("cursor")
        if isinstance(cursor, dict) and cursor.get("updated_before"):
            where += " AND (updated_at < ? OR (updated_at = ? AND episode_id < ?))"
            params += [cursor["updated_before"], cursor["updated_before"],
                       cursor.get("episode_id_before", "")]
        where += " ORDER BY updated_at DESC, episode_id DESC LIMIT ?"
        params.append(limit + 1)
        rows = conn.execute(
            f"SELECT * FROM semantic_episodes WHERE {where}",
            params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_emit(r) for r in rows]
    out: dict = {"scope_id": scope, "episodes": items, "count": len(items),
                 "has_more": has_more}
    if has_more and rows:
        last = rows[-1]
        out["next_cursor"] = {"updated_before": last["updated_at"],
                              "episode_id_before": last["episode_id"]}
    return out
