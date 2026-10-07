"""语义事件（episode）服务——她批准 2026-10-07"做吧，我需要这个功能"。

蓝图 `MARIPOSA_LIFECYCLE_v1.0/00_GLM执行方案.md` §6 最小实施：
- 主模型判断语义（不用规则/阈值/沉默时长/后台模型替代）
- 三态 OPEN / QUIESCENT / CLOSED；CLOSED 拒绝普通 continue
- 纠错 correct 恢复非 CLOSED；旧闭合留审计历史
- 短标签≤80 码点仅导航；无叙事正文、不参与检索
- 同 op 幂等；expected_revision CAS；scope 服务端校验
- 不自动合并/不自动关闭/技术断开不等于语义暂停
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .. import audit, db
from ..errors import Forbidden, NotFound

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _code_point_len(text: str) -> int:
    return len(text)


def _validate_segments(raw) -> list[dict]:
    """蓝图 §2.4：A→B→A 不合并——segments 独立保存，不自动扩成 m1–m11。"""
    if not isinstance(raw, list) or not raw:
        raise Forbidden("source_selections 非空数组必填", code="INVALID_ARGUMENT")
    out = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, dict):
            raise Forbidden(f"segment[{i}] 必须是对象", code="INVALID_ARGUMENT")
        conv = seg.get("conversation_id")
        members = seg.get("members")
        if not isinstance(conv, str) or not conv:
            raise Forbidden(f"segment[{i}].conversation_id 必填", code="INVALID_ARGUMENT")
        if not isinstance(members, list) or not members:
            raise Forbidden(f"segment[{i}].members 非空", code="INVALID_ARGUMENT")
        for m in members:
            if not isinstance(m, dict) or not m.get("source_message_id") \
                    or not m.get("content_hash"):
                raise Forbidden(f"segment[{i}] member 缺 source_message_id/content_hash",
                                code="INVALID_ARGUMENT")
        item = {"conversation_id": conv, "members": members}
        for k in ("start_char_offset", "end_char_offset"):
            if seg.get(k) is not None:
                item[k] = seg[k]
        out.append(item)
    if len(out) > 16:
        raise Forbidden("source_selections 最多 16 段", code="INVALID_ARGUMENT")
    return out


def _validate_source_ref(raw, field: str) -> dict:
    if not isinstance(raw, dict) or not raw.get("source_message_id"):
        raise Forbidden(f"{field} 缺 source_message_id", code="INVALID_ARGUMENT")
    out = {"source_message_id": raw["source_message_id"]}
    if raw.get("content_hash"):
        out["content_hash"] = raw["content_hash"]
    if raw.get("char_offset") is not None:
        out["char_offset"] = raw["char_offset"]
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
        "last_reason_code": row["last_reason_code"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_recorded_at": row["closed_recorded_at"],
    }


def apply(principal, a: dict) -> dict:
    """episode.apply：start/continue/pause/close/correct + 幂等 + CAS。"""
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

    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = _apply_tx(conn, principal, action, scope_id, reason, a, note)
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
        segments = _validate_segments(a.get("source_selections"))
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
                              "reason": reason, "segments": len(segments)})
        return {"episode_id": eid, "state": "OPEN", "revision": 1,
                "label": label.strip(), "scope_id": scope_id}

    # 以下动作都需要 episode_id + expected_revision（CAS）
    eid = a.get("episode_id")
    if not eid:
        raise Forbidden(f"{action} 必须带 episode_id", code="INVALID_ARGUMENT")
    row = _get_episode(conn, eid)
    if row["scope_id"] != scope_id:
        raise Forbidden("scope 不匹配（跨 scope 操作拒绝）",
                        code="FORBIDDEN", episode= eid)
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

    if action == "correct":
        corrected = a.get("corrected_state")
        if corrected not in _STATES:
            raise Forbidden("corrected_state 必须是 OPEN/QUIESCENT/CLOSED",
                            code="INVALID_ARGUMENT")
        new_state = corrected
        new_reason = reason
        new_terminal = row["terminal_source_ref"]
        new_closed = row["closed_recorded_at"]
        if corrected != "CLOSED":
            # 蓝图 §6.2：纠错恢复非 CLOSED → 清空闭合记录；旧值留审计
            new_terminal = None
            new_closed = None
        conn.execute(
            "UPDATE semantic_episodes SET state=?, revision=?,"
            " last_reason_code=?, terminal_source_ref=?,"
            " closed_recorded_at=?, updated_at=? WHERE episode_id=?",
            (new_state, row["revision"] + 1, new_reason,
             new_terminal, new_closed, now, eid))
        audit.record(conn, "episode.corrected", principal.principal_id,
                     resource_id=eid,
                     payload={"from": prev_state, "to": new_state,
                              "prev_terminal": row["terminal_source_ref"],
                              "prev_closed_at": row["closed_recorded_at"],
                              "reason": reason, "note": note[:200]})
        return {"episode_id": eid, "state": new_state,
                "revision": row["revision"] + 1, "corrected_from": prev_state}

    # continue / pause / close
    allowed_starts = _TRANSITIONS.get(action, {})
    if prev_state not in allowed_starts:
        raise Forbidden(
            f"{action} 不允许当前状态 {prev_state}"
            + ("（CLOSED 不接受普通 continue；确为误关走 correct）"
               if action == "continue" and prev_state == "CLOSED" else ""),
            code="INVALID_TRANSITION", current=prev_state)

    new_state = allowed_starts[prev_state]
    segments_now = json.loads(row["source_selections_json"])

    if action in ("continue", "close"):
        new_segs = a.get("source_selections")
        if new_segs:
            validated = _validate_segments(new_segs)
            segments_now = _dedup_append(segments_now, validated)

    terminal_ref = row["terminal_source_ref"]
    closed_at = row["closed_recorded_at"]
    decision_refs = json.loads(row["decision_source_refs_json"])

    if action == "close":
        terminal = a.get("terminal_source_ref")
        if not terminal:
            raise Forbidden("close 必须带 terminal_source_ref"
                            "（截至何处的原文锚点）", code="INVALID_ARGUMENT")
        terminal_ref = json.dumps(
            _validate_source_ref(terminal, "terminal_source_ref"),
            ensure_ascii=False)
        decision = a.get("decision_source_refs")
        if not decision:
            raise Forbidden("close 必须带 decision_source_refs"
                            "（至少一个判断依据）", code="INVALID_ARGUMENT")
        if isinstance(decision, list):
            decision_refs = [_validate_source_ref(d, "decision_source_refs")
                             for d in decision]
        else:
            decision_refs = [_validate_source_ref(decision,
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
                          "segments_total": len(segments_now)})
    return {"episode_id": eid, "state": new_state,
            "revision": row["revision"] + 1}


def get(principal, a: dict) -> dict:
    eid = a.get("episode_id")
    if not eid:
        raise Forbidden("episode_id 必填", code="INVALID_ARGUMENT")
    with db.formal() as conn:
        row = _get_episode(conn, eid)
        return _emit(row)


def list_episodes(principal, a: dict) -> dict:
    """蓝图 §6：scope 内分页读取状态；不给全库未完故事。"""
    scope = a.get("scope_id")
    if not scope:
        raise Forbidden("scope_id 必填（不默认全库）", code="INVALID_ARGUMENT")
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
    with db.formal() as conn:
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
