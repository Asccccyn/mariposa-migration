"""时间感知（§13）与交接便签（§12.3）。

三条时间线严格分开：
- last_user_message_at：真实用户对话（raw user 消息 / user_message activity）
- last_ui_activity_at：浏览/点击/审批（presence.touch，人类主体）
- last_agent_or_system_activity_at：工具或程序（agent touch、扫描、Host）
扫描/开窗/检索不刷新任何一条。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, db
from ..errors import Forbidden

HANDOFF_ACTIVE_HOURS = 72


def _now() -> datetime:
    return datetime.now(timezone.utc)


def local_date(now: datetime | None = None) -> str:
    # RA-009（2026-10-02 复审 P2）：返回 ISO 字符串——date 对象会让
    # MCP 输出序列化直接 500
    tz = ZoneInfo(config.RELATIONSHIP_TIMEZONE)
    return (now or _now()).astimezone(tz).date().isoformat()


def _parse_instant(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _max_by_instant(values) -> str | None:
    """F15（2026-10-03 审计 P2）：时间比较按规范化瞬时，不按字符串。

    `20:00+08`（12UTC）字符串大于同日 `15:00Z`（15UTC），SQL MAX /
    Python max 都会选错；混合 offset 的消息事实必须先归一再比。"""
    best = None
    for v in values:
        if not v:
            continue
        try:
            key = _parse_instant(v)
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, v)
    return best[1] if best else None


def _latest_activity(conn, kinds: list[str]) -> str | None:
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"SELECT occurred_at FROM activity_events WHERE kind IN ({marks})",
        tuple(kinds)).fetchall()
    return _max_by_instant(r["occurred_at"] for r in rows)


def _latest_raw_user(conn) -> str | None:
    # D13（2026-10-01）：legacy raw_messages 退役——最近用户消息改查
    # 现行 Source 层（normalized_sender='human'）
    # F14（2026-10-03 审计 P2）：只认已发布消息——导入校验中/失败
    # 清场前的未发布行不得产生"用户刚联系过"的假时间事实
    rows = conn.execute(
        "SELECT m.created_at FROM source_messages m"
        " WHERE m.normalized_sender='human' AND m.published=1").fetchall()
    return _max_by_instant(r["created_at"] for r in rows)


def _max(*vals):
    return _max_by_instant(vals)


def now() -> dict:
    n = _now()
    return {
        "now_utc": n.isoformat(),
        "timezone": config.RELATIONSHIP_TIMEZONE,
        "local_date": local_date(n),
    }


def context(principal_id: str) -> dict:
    with db.formal() as conn:
        last_user = _max(_latest_activity(conn, ["user_message"]), _latest_raw_user(conn))
        last_ui = _latest_activity(conn, ["ui_activity"])
        last_agent = _latest_activity(conn, ["agent_or_system_activity"])
    return {
        **now(),
        "principal": principal_id,
        "last_user_message_at": last_user,
        "last_ui_activity_at": last_ui,
        "last_agent_or_system_activity_at": last_agent,
        "coverage": "complete" if last_user else "unknown_gap",
    }


def since(principal_id: str) -> dict:
    """从最后已知联系到现在的时长；覆盖缺失时明确标注，不冒充精确“没见面多久”。"""
    ctx = context(principal_id)
    anchor = ctx["last_user_message_at"]
    if not anchor:
        return {**ctx, "since_last_contact": None,
                "note": "无已收录联系记录，中间记录可能未齐"}
    try:
        t = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
    except ValueError:
        return {**ctx, "since_last_contact": None, "note": "时间戳不可解析"}
    # SRC-02（2026-10-04 二批）：合法的无时区时间戳按 UTC 归一（与
    # context/_parse_instant 同一规则），不再 TypeError
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return {**ctx, "since_last_contact": (_now() - t).total_seconds(),
            "note": "以已收录记录为准，未导入期间不计"}


def presence_touch(principal_id: str, principal_kind: str) -> dict:
    """轻量活动登记。actor 由服务端从凭据决定，参数不能自报（§13）。

    人类主体记 ui_activity；agent/system 主体记 agent_or_system_activity。
    工具人 touch 不会伪装成“乔生来了”。
    """
    kind = "ui_activity" if principal_kind == "human" else "agent_or_system_activity"
    aid = f"act_{uuid.uuid4().hex[:12]}"
    with db.formal() as conn:
        conn.execute(
            "INSERT INTO activity_events(id, kind, principal, occurred_at, detail)"
            " VALUES(?,?,?,?,?)",
            (aid, kind, principal_id, _now().isoformat(), "presence.touch"),
        )
    return {"recorded": kind}


def handoff_write(principal_id: str, entry_source: str | None, content: str) -> dict:
    """周家明写给另一入口的短便签；不由 Worker/Host 代写（§12.3）。"""
    if principal_id != "jiaming":
        raise Forbidden("only jiaming writes handoffs", principal=principal_id)
    if not content or not str(content).strip():
        raise Forbidden("handoff content required")
    hid = f"ho_{uuid.uuid4().hex[:10]}"
    now = _now()
    expires = now + timedelta(hours=HANDOFF_ACTIVE_HOURS)
    # P1-04（2026-10-05 审计）：交接便签是跨入口的业务动作，与插入
    # 同事务留审计（§19）
    from ..audit import service as _audit
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO handoffs(id, author, content, entry_source, created_at, expires_at)"
                " VALUES(?,?,?,?,?,?)",
                (hid, principal_id, str(content), entry_source, now.isoformat(),
                 expires.isoformat()),
            )
            _audit.record(conn, "handoff.written", principal_id,
                          resource_id=hid,
                          payload={"entry_source": entry_source})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"handoff_id": hid, "expires_at": expires.isoformat()}


def handoff_latest() -> dict:
    """保留最新一条；过期只标注 expired，不物理删除。"""
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM handoffs ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return {"handoff": None}
    expired = row["expires_at"] < _now().isoformat()
    return {"handoff": {
        "id": row["id"], "content": row["content"], "author": row["author"],
        "created_at": row["created_at"], "expires_at": row["expires_at"],
        "expired": expired,
        "note": "过期便签不进默认检索；不自动 purge 正文",
    }}
