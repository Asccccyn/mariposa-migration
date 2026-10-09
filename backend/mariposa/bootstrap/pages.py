"""Bootstrap 续页（从 service.py 拆出——2026-10-04 深耦合拆分批）。

四段续取（memory_days/plans/i/plan_content）+ snapshot 校验。
service 底部重导出 next_page，既有调用方与测试零改动。
依赖 service 的 _state_hash/_three_day_window/BOOT 常量经顶部导入
（service 的重导出在底部，执行到此已就绪）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, db
from ..errors import Forbidden, SnapshotStale
from ..identity_i import service as _i_svc
from ..plans import service as plans
from ..plans import service as _plans_svc  # noqa: F401
from .core import (BOOT_I_SECTION_CHARS, BOOT_MEMORY_DAYS,
                   BOOT_PLAN_SECTION_CHARS, BOOT_SECTION_LIMIT,
                   BOOT_UPCOMING_DAYS, _state_hash, _three_day_window)

def next_page(principal_id: str, entry_source: str, snapshot_id: str,
              cursor: dict, section: str = "plans") -> dict:
    """续取开窗分页（memory_days / plans）。snapshot 变化即 SNAPSHOT_STALE。

    v2 起 raw 不再是开窗 section（V2-BOOT-03）：显式取源走 raw 工具。
    """
    if principal_id != "jiaming":
        raise Forbidden("bootstrap is for jiaming entries", principal=principal_id)
    if section not in ("memory_days", "plans", "i", "plan_content",
                       "memory_item"):
        raise Forbidden(f"unknown section: {section}（v2 开窗无 raw 段）")
    # P1 复审（2026-10-02 接续）：校验 state_hash 与取页在同一读事务
    # ——此前校验连接先关、取页/Plan 各自重开连接，交错窗口内旧
    # snapshot 可拿到新数据
    from .core import _ENTRY_ALLOWED
    with db.formal() as conn:
        conn.execute("BEGIN")
        try:
            snap = conn.execute(
                "SELECT * FROM bootstrap_snapshots WHERE snapshot_id=?",
                (snapshot_id,)).fetchone()
            if snap is None:
                raise SnapshotStale("snapshot unknown; re-fetch bootstrap")
            if snap["state_hash"] != _state_hash(conn):
                raise SnapshotStale(
                    "underlying resources changed; re-fetch",
                    snapshot_id=snapshot_id)
            # R14（复审 2026-10-07）：续页身份检查统一前置（此前仅在
            # memory_days 分支——i/plans/plan_content 续页不核 profile）
            _snap_profile = snap["profile"] if "profile" in snap.keys() \
                else None
            if _snap_profile and entry_source not in _ENTRY_ALLOWED.get(
                    _snap_profile, {entry_source}):
                raise Forbidden(
                    "entry_source 与快照 profile 不匹配",
                    entry_source=entry_source, profile=_snap_profile)
            tz = ZoneInfo(config.RELATIONSHIP_TIMEZONE)
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).astimezone(tz).date()
            snap_day = (snap["business_date"]
                        if "business_date" in snap.keys() else None)
            if snap_day and snap_day != today.isoformat():
                raise SnapshotStale(
                    "crossed business day; re-fetch bootstrap for the new"
                    " window", snapshot_id=snapshot_id,
                    snapshot_day=snap_day, current_day=today.isoformat())
            three_days = [(today - timedelta(days=i)).isoformat()
                          for i in range(BOOT_MEMORY_DAYS)]

            def _cur_str(key: str, *, required: bool = True):
                v = (cursor or {}).get(key)
                if v is None:
                    if required:
                        raise Forbidden(f"cursor.{key} required",
                                        code="INVALID_ARGUMENT",
                                        cursor_field=key)
                    return None
                if not isinstance(v, str):
                    raise Forbidden(f"cursor.{key} must be a string",
                                    code="INVALID_ARGUMENT",
                                    cursor_field=key, got=repr(v)[:40])
                return v

            def _cur_int(key: str, *, default: int = 0, minimum: int = 0):
                v = (cursor or {}).get(key)
                if v is None:
                    v = default
                if isinstance(v, bool) or not isinstance(v, int):
                    raise Forbidden(f"cursor.{key} must be an integer",
                                    code="INVALID_ARGUMENT",
                                    cursor_field=key, got=repr(v)[:40])
                return max(v, minimum)

            if section == "memory_days":
                before_date = _cur_str("memory_before_date")
                last_id = _cur_str("memory_last_id", required=False) or ""
                rows = conn.execute(
                    "SELECT memory_id, memory_date FROM memories WHERE"
                    " visibility='active' AND memory_date IN (?,?,?)"
                    " AND (memory_date < ? OR (memory_date = ? AND"
                    " memory_id > ?))"
                    " ORDER BY memory_date DESC, memory_id LIMIT ?",
                    tuple(three_days) + (before_date, before_date,
                                         last_id or "",
                                         BOOT_SECTION_LIMIT)).fetchall()
                # D2（2026-10-06）+R14：分页与首页同 profile（身份检查
                # 已统一前置；此处仅按快照 profile 装配内容）。
                # RRA-006（回访 2026-10-09）：续页与首页共用字节预算装桶
                # （此前续页按条数装页无预算，5 桶 31250B 击穿 24576）；
                # 游标取**最后已服务行**（严格 > 从未服务桶继续）
                from .core import _pack_memory_rows
                items, overflow = _pack_memory_rows(
                    conn, rows,
                    profile=_snap_profile or "claude_chat")
                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM memories WHERE"
                    " visibility='active' AND memory_date IN (?,?,?)",
                    tuple(three_days)).fetchone()["c"]
                nxt = None
                if overflow is not None or len(rows) >= BOOT_SECTION_LIMIT:
                    last = rows[len(items) - 1]
                    nxt = {"memory_before_date": last["memory_date"],
                           "memory_last_id": last["memory_id"]}
                return {"snapshot_id": snapshot_id,
                        "section": "memory_days",
                        "content_role": "bootstrap_memory_package",
                        "instruction_authority": "none",
                        "items": items, "count": len(items),
                        "total_in_window": total, "next_cursor": nxt}

            # plan_content：单条 Plan 正文分节续取（裁定 2026-10-04）
            if section == "plan_content":
                from ..plans import service as _plans_svc
                plan_id = _cur_str("plan_id")
                plan_off = _cur_int("plan_offset")
                plan = _plans_svc.get(conn, plan_id)
                content = plan.get("content") or ""
                if plan_off < 0 or plan_off > len(content):
                    raise Forbidden(
                        "cursor.plan_offset 超出当前正文范围",
                        code="INVALID_ARGUMENT", got=plan_off,
                        total_chars=len(content))
                nxt_pc = (plan_off + BOOT_PLAN_SECTION_CHARS
                          if plan_off + BOOT_PLAN_SECTION_CHARS
                          < len(content) else None)
                return {"snapshot_id": snapshot_id,
                        "section": "plan_content", "plan_id": plan_id,
                        "content_role": "bootstrap_memory_package",
                        "instruction_authority": "none",
                        "content": content[plan_off:plan_off
                                           + BOOT_PLAN_SECTION_CHARS],
                        "offset": plan_off,
                        "total_chars": len(content),
                        "next_cursor": ({"plan_id": plan_id,
                                         "plan_offset": nxt_pc}
                                        if nxt_pc else None)}

            # RRA-006（2026-10-09 复审）：estomago 桶正文分节续取——
            # cursor={memory_id, field(event_text|our_words), offset}；
            # 与 plan_content 同构（码点分节、钉当前内容、无截断冒充）
            if section == "memory_item":
                mid = _cur_str("memory_id")
                fld = _cur_str("field")
                if fld not in ("event_text", "our_words"):
                    raise Forbidden(
                        f"memory_item.field 必须是 event_text/our_words",
                        code="INVALID_ARGUMENT", field=fld)
                off = _cur_int("offset")
                vv = conn.execute(
                    "SELECT event_text, hold_text FROM memory_versions"
                    " WHERE memory_id=? AND version_no="
                    "(SELECT current_version_no FROM memories WHERE"
                    " memory_id=?)", (mid, mid)).fetchone()
                if vv is None:
                    raise Forbidden("memory 不存在或无表示版本",
                                    code="INVALID_ARGUMENT",
                                    memory_id=mid)
                from .core import BOOT_MEMITEM_SECTION_CHARS as _W  # noqa: F401
                if fld == "event_text":
                    content = (vv["event_text"] or vv["hold_text"]) or ""
                    if off > len(content):
                        raise Forbidden("cursor.offset 超出正文范围",
                                        code="INVALID_ARGUMENT",
                                        got=off, total_chars=len(content))
                    # RRA-006（二次回访 2026-10-10）：续取分节同**字节**
                    # 口径——控制字符 JSON 6B/字符时码点定宽顶穿信封
                    from .core import _byte_width
                    w = _byte_width(content, start=off)
                    nxt = (off + w if off + w < len(content) else None)
                    return {"snapshot_id": snapshot_id,
                            "section": "memory_item", "memory_id": mid,
                            "field": fld,
                            "content_role": "bootstrap_memory_package",
                            "instruction_authority": "none",
                            "content": content[off:off + w],
                            "offset": off, "total_chars": len(content),
                            "next_cursor": ({"memory_id": mid,
                                             "field": fld,
                                             "offset": nxt}
                                            if nxt else None)}
                # our_words：按条目续取（cursor.item_offset/char_offset；
                # 形状=数组。RRA-006 回访 2026-10-09：单条超宽经
                # char_offset 条内字符续取——_section_words 与首页同口径）
                ioff = _cur_int("item_offset")
                coff = _cur_int("char_offset")
                rows = conn.execute(
                    "SELECT w.speaker, w.text FROM memory_our_words w"
                    " JOIN memories mm ON mm.memory_id=w.memory_id"
                    " WHERE w.memory_id=? AND mm.visibility='active'",
                    (mid,)).fetchall()
                if ioff > len(rows):
                    raise Forbidden("cursor.item_offset 超出条目范围",
                                    code="INVALID_ARGUMENT",
                                    got=ioff, total_items=len(rows))
                from .core import _section_words
                _kept, _nxt_item, _nxt_char = _section_words(
                    rows, ioff, coff)
                return {"snapshot_id": snapshot_id,
                        "section": "memory_item", "memory_id": mid,
                        "field": fld,
                        "content_role": "bootstrap_memory_package",
                        "instruction_authority": "none",
                        "items": _kept, "item_offset": ioff,
                        "char_offset": coff,
                        "total_items": len(rows),
                        "next_cursor": ({"memory_id": mid, "field": fld,
                                         "item_offset": _nxt_item,
                                         "char_offset": _nxt_char}
                                        if _nxt_item is not None
                                        else None)}

            # i：正文分节续取（F16）——同事务读当前 I，超长默认包
            # 只给首节，warning 承诺的分节续取在这里兑现
            if section == "i":
                from ..identity_i import service as _i_svc
                i_cur = _i_svc.get(conn=conn)
                content = i_cur["content"] or ""
                i_off = _cur_int("i_offset")
                if i_off > len(content):
                    raise Forbidden("cursor.i_offset 超出当前正文范围",
                                    code="INVALID_ARGUMENT",
                                    got=i_off, total_chars=len(content))
                nxt_i = (i_off + BOOT_I_SECTION_CHARS
                         if i_off + BOOT_I_SECTION_CHARS < len(content)
                         else None)
                return {"snapshot_id": snapshot_id, "section": "i",
                        "content_role": "bootstrap_memory_package",
                        "instruction_authority": "none",
                        "content": content[i_off:i_off
                                           + BOOT_I_SECTION_CHARS],
                        "offset": i_off, "total_chars": len(content),
                        "version": i_cur["version"],
                        "next_cursor": ({"i_offset": nxt_i}
                                        if nxt_i else None)}

            # plans：offset 游标（同事务经 conn 装配）
            offset = _cur_int("plans_offset")
            all_plans = plans.bootstrap_plans(
                today, BOOT_UPCOMING_DAYS, conn=conn)
            page = []
            for p_ in all_plans[offset:offset + BOOT_SECTION_LIMIT]:
                p_ = dict(p_)
                content = p_.get("content") or ""
                # MEM-04（2026-10-04 二批）：列表续页与首页同款长正文
                # 分节——后续正文走 plan_content 游标，不得绕过预算
                if len(content) > BOOT_PLAN_SECTION_CHARS:
                    p_["content"] = content[:BOOT_PLAN_SECTION_CHARS]
                    p_["content_truncated"] = True
                    p_["content_total_chars"] = len(content)
                    p_["content_next_cursor"] = {
                        "plan_id": p_["plan_id"],
                        "plan_offset": BOOT_PLAN_SECTION_CHARS}
                page.append(p_)
            nxt = (offset + BOOT_SECTION_LIMIT
                   if offset + BOOT_SECTION_LIMIT < len(all_plans)
                   else None)
            # MEM-06（2026-10-04 二批）：所有续页与首页同权——进模型
            # 上下文边界必须有 data 身份与无指令权限标记
            return {"snapshot_id": snapshot_id, "section": "plans",
                    "content_role": "bootstrap_memory_package",
                    "instruction_authority": "none",
                    "items": page, "count": len(page),
                    "total": len(all_plans),
                    "next_cursor": {"plans_offset": nxt}}
        finally:
            conn.execute("COMMIT")
