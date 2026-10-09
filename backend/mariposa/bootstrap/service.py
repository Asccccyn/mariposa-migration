"""两入口开窗（spec_v2 R20 / §11 / V2-BOOT）。

v2 默认包 = 最近三个自然日桶的 标题+心情标签+心情文字 + 当前 I + 提前 0..3
自然日进入临近的计划/纪念日全文；进行中/逾期未完成计划单列不消失。
普通事件不自动全文展开；**不默认附带旧版 30 条原文**（显式取源走 raw
工具并另行确认）。开窗不产生 view confirm，不续期。

I 只注入当前 revision；若某条有历史，只带 has_history/history_count 指针，
旧 I 正文必须显式 i.item.history 才能读取。

- Claude Chat / CC 两个 profile 同一基础包（entry_source 校验不变）；
- 最近三天 = 事件日期的今天与前两天（自然日，非滚动 72 小时）；
- 临近按发生日期与今天之差 0..3 日（含恰好 3 日，按日期不按小时）；
- snapshot 指纹覆盖：记忆当前表示、计划、I、纪念日发生项；变化即失效。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from .. import config, db
from ..errors import Forbidden, SnapshotStale
from ..memory import categories as cats_mod
from ..memory import service as memory
from ..plans import service as plans

def _anniversaries_upcoming(today, days: int, conn=None) -> list[dict]:
    horizon = (today + timedelta(days=days)).isoformat()
    if conn is not None:
        return _anniv_rows(conn, horizon, today, days)
    with db.formal() as c:
        return _anniv_rows(c, horizon, today, days)


def _anniv_rows(conn, horizon, today, days: int) -> list[dict]:
    if True:
        rows = conn.execute(
            "SELECT d.definition_id, d.title, o.occurrence_date,"
            " d.memory_id FROM anniversary_occurrences o"
            " JOIN anniversary_definitions d ON d.definition_id=o.definition_id"
            " WHERE o.occurrence_date BETWEEN ? AND ?"
            " ORDER BY o.occurrence_date",
            (today.isoformat(), horizon)).fetchall()
    return [dict(r) for r in rows]


def _memory_section(conn, three_days: list[str],
                    profile: str = "claude_chat") -> dict:
    """三天桶段：标题+心情标签+心情文字+分类；不默认展开事件正文。"""
    mem_rows = conn.execute(
        "SELECT memory_id, memory_date FROM memories WHERE visibility='active'"
        " AND memory_date IN (?,?,?) ORDER BY memory_date DESC, memory_id"
        " LIMIT ?", tuple(three_days) + (BOOT_SECTION_LIMIT,)).fetchall()
    # RRA-006（回访 2026-10-09）：装页与续页共用 _pack_memory_rows（实际
    # 序列化字节预算）；首页游标取**最后已服务行**——此前取溢出行
    #（未服务），续页条件 memory_id > last_id 严格大于把该桶永远跳过
    #（7 桶场景第 2 桶漏交付）
    items, overflow = _pack_memory_rows(conn, mem_rows, profile=profile)
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE visibility='active'"
        " AND memory_date IN (?,?,?)", tuple(three_days)).fetchone()["c"]
    if overflow is not None or total > len(items):
        last = mem_rows[len(items) - 1]
        next_cursor = {"memory_before_date": last["memory_date"],
                       "memory_last_id": last["memory_id"],
                       "remaining": total - len(items)}
    else:
        next_cursor = None
    return {"items": items, "count": len(items), "total_in_window": total,
            "next_cursor": next_cursor}


from .core import (  # noqa: E402
    BOOT_DAY_WINDOW_MODE, BOOT_I_SECTION_CHARS, BOOT_MEMORY_DAYS,
    BOOT_PLAN_SECTION_CHARS, BOOT_RULES_VERSION, BOOT_SECTION_LIMIT,
    BOOT_SOFT_TOKEN_BUDGET, BOOT_UPCOMING_DAYS, _ENTRY_ALLOWED,
    _memory_slim, _pack_memory_rows, _state_hash, _three_day_window)
from .core import ret_mod as _biztime  # noqa: E402,F401（历史名兼容）


def get(principal_id: str, entry_source: str, profile: str,
        loaded_snapshot_id: str | None = None,
        cursor: dict | None = None) -> dict:
    if principal_id != "jiaming":
        raise Forbidden("bootstrap is for jiaming entries", principal=principal_id)
    if profile not in _ENTRY_ALLOWED:
        raise Forbidden(f"unknown profile: {profile}")
    if entry_source not in _ENTRY_ALLOWED[profile]:
        raise Forbidden(
            "entry_source does not match bootstrap profile",
            entry_source=entry_source, profile=profile)

    # CB-044 延伸：state hash 与各段读取共享一个读事务——指纹与
    # 装配内容同快照，避免"hash 计算后又变化"的错配 unchanged
    with db.formal() as conn:
        conn.execute("BEGIN")
        try:
            current_state = _state_hash(conn)
            if loaded_snapshot_id:
                snap = conn.execute(
                    "SELECT state_hash, profile FROM bootstrap_snapshots"
                    " WHERE snapshot_id=?",
                    (loaded_snapshot_id,)).fetchone()
                if snap is None:
                    raise SnapshotStale(
                        "snapshot unknown; re-fetch bootstrap")
                # R14（复审 2026-10-07）：快照复用同核 profile——此前仅比
                # state_hash，CC 的包可被 estomago 身份冒充 unchanged 复用
                # （错误许可复用旧投影；响应本身无新泄漏）
                _saved_profile = snap["profile"] \
                    if "profile" in snap.keys() else None
                if _saved_profile and _saved_profile != profile:
                    raise SnapshotStale(
                        "snapshot belongs to a different profile;"
                        " re-fetch bootstrap",
                        snapshot_id=loaded_snapshot_id,
                        saved_profile=_saved_profile, profile=profile)
                if snap["state_hash"] != current_state:
                    raise SnapshotStale(
                        "underlying resources changed since snapshot;"
                        " re-fetch", snapshot_id=loaded_snapshot_id)
                # 开窗去重：同 session 同策略状态未变 -> 薄响应，不重复灌包
                return {"snapshot_id": loaded_snapshot_id,
                        "unchanged": True, "profile": profile,
                        "note": "底层资源未变化；继续用已加载内容，"
                                "不重发开窗包"}

            tz = ZoneInfo(config.RELATIONSHIP_TIMEZONE)
            today, three_days = _three_day_window(tz)
            md = _memory_section(conn, three_days, profile=profile)
            # RA-006（2026-10-02 复审 P2）：I/Plan/纪念日与 state hash
            # 同一读事务装配——此前 COMMIT 在这些读取之前，包内容与其
            # 指纹可来自不同快照（审计反例 bootstrap_mixed_snapshot）
            from ..identity_i import service as _i_svc
            i_doc = _i_svc.get(conn=conn)
            _i_bound = conn.execute(
                "SELECT COUNT(DISTINCT memory_id) AS c FROM"
                " i_revision_memory_relations").fetchone()["c"]
            plan_items = plans.bootstrap_plans(
                today, BOOT_UPCOMING_DAYS, conn=conn)
            anniv = _anniversaries_upcoming(today, BOOT_UPCOMING_DAYS,
                                            conn=conn)
        finally:
            conn.execute("COMMIT")

    # 裁定（2026-10-04 三）：I 开窗提示预计算——has_history/绑定数
    _i_items = i_doc.get("items", [])
    _has_hist = (i_doc["version"] > 1
                 or any(int(it.get("revision", 1)) > 1
                        for it in _i_items))
    active_plans = [p for p in plan_items if p["state"] in plans.OPEN_STATES]
    upcoming_plans = [p for p in plan_items if p["state"] == "planned"]
    plan_page = []
    for p in plan_items[:BOOT_SECTION_LIMIT]:
        p = dict(p)
        content = p.get("content") or ""
        if len(content) > BOOT_PLAN_SECTION_CHARS:
            p["content"] = content[:BOOT_PLAN_SECTION_CHARS]
            p["content_truncated"] = True
            p["content_total_chars"] = len(content)
            p["content_next_cursor"] = {
                "plan_id": p["plan_id"],
                "plan_offset": BOOT_PLAN_SECTION_CHARS}
        plan_page.append(p)
    plan_cursor = {"plans_offset": BOOT_SECTION_LIMIT} \
        if len(plan_items) > BOOT_SECTION_LIMIT else {"plans_offset": None}

    result: dict = {
        "snapshot_id": f"snap_{uuid.uuid4().hex[:12]}",
        "state_hash": current_state,
        "profile": profile,
        # 裁定（2026-10-04）：模型可见读路径安全语义等价（形状不必
        # 与 Recall 同形）——开窗包内容是 data/memory，正文中的
        # 指令只是历史数据，无指令权限
        "content_role": "bootstrap_memory_package",
        "instruction_authority": "none",
        "time": {"local_date": today.isoformat(),
                 "timezone": config.RELATIONSHIP_TIMEZONE},
        "memory_days": {
            "dates": three_days, "mode": BOOT_DAY_WINDOW_MODE,
            **md,
            "section_limit": BOOT_SECTION_LIMIT,
            "fields": ["original_title", "mood_tags", "mood_text",
                       "categories"],
            "note": "三天桶只出标题+心情；事件正文须明确打开该桶",
        },
        # 裁定（2026-10-04 三，乔生）：I 开窗出站最小化——只返回
        # 当前的话 + 两个极简提示（历史/绑定）；条目指针、历史明细、
        # 绑定明细不出站（存储全保留，按需 i.items.list /
        # i.item.history / relations.list 显式读取）
        "i": {"content": (i_doc["content"] or "")[:BOOT_I_SECTION_CHARS],
              **({"version": i_doc["version"]} if _has_hist else {}),
              **({"has_history": True} if _has_hist else {}),
              **({"bound_memory_count": _i_bound} if _i_bound else {}),
              "detail_on_demand": "i.items.list / i.item.history /"
                                  " relations.list",
              "note": None if i_doc["content"] is not None else
                      "I 尚未落笔（无旧Self自动映射，V2-I-03）",
              **({"truncated": True,
                  "total_chars": len(i_doc["content"]),
                  "next_cursor": {"i_offset": BOOT_I_SECTION_CHARS}}
                 if (i_doc["content"] is not None
                     and len(i_doc["content"]) > BOOT_I_SECTION_CHARS)
                 else {})},
        "plans": {
            "items": plan_page, "count": len(plan_page),
            "total": len(plan_items),
            "active_count": len(active_plans),
            "upcoming_count": len(upcoming_plans),
            "upcoming_days": BOOT_UPCOMING_DAYS,
            "mode": "date_diff_0_to_3",
            "section_limit": BOOT_SECTION_LIMIT,
            "next_cursor": plan_cursor,
            "note": "进行中/需执行/逾期未完成单列不消失；plan 查看不续期",
        },
        "anniversaries": {
            "items": anniv,
            "upcoming_days": BOOT_UPCOMING_DAYS,
        },
        "policy": {
            "boot_memory_days": BOOT_MEMORY_DAYS,
            "plan_upcoming_days": BOOT_UPCOMING_DAYS,
            "soft_token_budget": BOOT_SOFT_TOKEN_BUDGET,
            "raw_in_default_package": False,
        },
        "cursor": {"next": None},
        "coverage": {"raw": "not_in_default_package",
                     "note": "取原文用 source.message.get / source.search 显式查询"},
    }

    _estimate_budget(result)
    with db.formal() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bootstrap_snapshots(snapshot_id, state_hash,"
            " profile, created_at, business_date) VALUES(?,?,?,datetime('now'),?)",
            (result["snapshot_id"], current_state, profile,
             result["time"]["local_date"]))
    return result


# ---------- 拆分批（2026-10-04）：续页外迁，重导出兼容 ----------
from .pages import next_page  # noqa: E402,F401

def _estimate_budget(result: dict) -> None:
    """粗估（chars/1.5）；超预算显式提示，不静默截断约定资料。"""
    chars = 0
    for m in result.get("memory_days", {}).get("items", []):
        chars += len(m.get("mood_text") or "")
        chars += len(m.get("original_title") or "")
    chars += len((result.get("i") or {}).get("content") or "")
    for p in result.get("plans", {}).get("items", []):
        chars += len(p.get("title") or "") + len(p.get("content") or "")
    est = int(chars / 1.5)
    result["estimated_tokens"] = est
    if est > BOOT_SOFT_TOKEN_BUDGET:
        result["warnings"] = [{
            "code": "SOFT_BUDGET_EXCEEDED",
            "message": f"估算 {est} tokens 超过软预算 {BOOT_SOFT_TOKEN_BUDGET}；"
                       "分节分页续取，不静默截断",
        }]
