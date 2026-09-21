"""Capability Registry：一份注册表，多个适配器（§17.1）。

HTTP / MCP / CC 都调这里注册的 handler；权限按 principal 声明，
幂等键统一在本层落库。路径、传输不赋予角色。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .. import db
from ..errors import Forbidden, IdempotencyConflict, MariposaError, NotFound
from ..identity import Principal
from ..identity import service as identity
from ..memory import service as memory
from ..retrieval import search as retrieval_search
from ..workspace import service as workspace
from ..raw import service as raw
from ..quotes import service as quotes
from ..plans import service as plans
from ..calendar import service as calendar
from ..time_context import service as time_ctx
from ..bootstrap import service as bootstrap
from ..letters import service as letters


@dataclass(frozen=True)
class Capability:
    name: str
    handler: Callable
    allowed_principals: set[str]
    write: bool
    idempotent: bool = False
    description: str = ""


def _everyone() -> set[str]:
    return {"qiaosheng", "jiaming", "worker"}


def _owners() -> set[str]:
    return {"qiaosheng", "jiaming"}


def _register() -> dict[str, Capability]:
    caps: dict[str, Capability] = {}

    def add(name, handler, allowed, write, idempotent=False, description=""):
        caps[name] = Capability(name, handler, set(allowed), write, idempotent, description)

    add("memory.hold", _hold, {"qiaosheng", "jiaming"}, True,
        description="写入一条正式记忆（full 表示，version 1）")
    add("memory.get", _get, _owners(), False, description="读取当前表示（遗忘桶只返回摘要）")
    add("memory.search", _search, _owners(), False, description="关键词检索有效投影")
    add("memory.versions.read", _versions, _owners(), False,
        description="明确展开历史版本，不自动 restore")
    add("memory.restore", _restore, _owners(), True,
        description="恢复到最近压缩前版本（或指定历史 full 版本）")
    add("workspace.forgetting.scan", _scan, {"worker", "qiaosheng", "jiaming"}, False,
        description="扫描遗忘候选并落工作区草稿；不改正式桶")
    add("workspace.proposals.revise", _revise, {"worker", "qiaosheng", "jiaming"}, True,
        description="修订未提交草稿")
    add("workspace.proposals.submit", _submit, {"worker", "qiaosheng", "jiaming"}, True,
        True, description="提交提案：冻结版本并登记正式 envelope")
    add("workspace.proposals.list", _list_items, _everyone(), False,
        description="列出工作区提案")
    add("memory.forgetting.decide", _decide, _owners(), True, True,
        description="审批遗忘提案（worker 拒绝）")
    add("raw.import", _raw_import, {"worker", "qiaosheng", "jiaming"}, True, True,
        description="导入原文（同源同消息 ID 幂等，不覆盖已存在消息）")
    add("raw.messages.list", _raw_list, _owners(), False,
        description="最新 N 条真实消息（默认 30 条，按消息计）")
    add("raw.search", _raw_search, _owners(), False,
        description="独立原文查询，命中标 source=raw")
    add("raw.conversations.list", _raw_convs, _owners(), False,
        description="已收录会话列表")
    add("memory.quotes.keep", _quote_keep, {"jiaming"}, True,
        description="周家明选取保留她的话（允许复述）")
    add("memory.quotes.list", _quote_list, _owners(), False,
        description="列出她的话（独立资源）")
    add("memory.quotes.search", _quote_search, _owners(), False,
        description="独立 quotes 检索；不得反向算作记忆命中")
    add("memory.quotes.withdraw", _quote_withdraw, {"qiaosheng", "jiaming"}, True,
        description="撤下一条（撤下后校对不得重新浮现）")
    add("handoff.write", _handoff_write, {"jiaming"}, True,
        description="周家明写给另一入口的交接便签（72h 过期不删除）")
    add("handoff.latest", _handoff_latest, _owners(), False,
        description="最新便签（过期标注 expired）")
    add("plan.create", _plan_create, _owners(), True,
        description="创建计划（唯一真源，记忆经链接引用）")
    add("plan.update", _plan_update, _owners(), True,
        description="修改计划（expected_version 乐观锁）")
    add("plan.list", _plan_list, _owners(), False, description="列出计划")
    add("calendar.day", _cal_day, _owners(), False, description="单日聚合视图")
    add("calendar.range", _cal_range, _owners(), False, description="日期区间聚合（端点含）")
    add("calendar.month", _cal_month, _owners(), False, description="月视图聚合")
    add("bootstrap.get", _bootstrap, {"jiaming"}, False,
        description="两入口开窗（entry_source 校验 profile；worker 拒绝）")
    add("time.now", _time_now, _everyone(), False, description="真实 now + 共同时区")
    add("time.context", _time_ctx, _everyone(), False,
        description="三条时间线分开的活动证据")
    add("time.since", _time_since, _everyone(), False, description="自最后已知联系")
    add("presence.touch", _presence_touch, _everyone(), True,
        description="轻量活动登记（actor 由凭据决定，不可参数自报）")
    add("letter.write", _letter_write, _owners(), True,
        description="写信（锁参数经归一化校验）")
    add("letter.list", _letter_list, _owners(), False,
        description="信件列表（metadata-only，锁信不返回正文）")
    add("letter.read", _letter_read, _owners(), False,
        description="读信正文（锁中返回 LOCKED_RESOURCE；过期锁读时归一）")
    add("letter.edit", _letter_edit, _owners(), True,
        description="编辑/锁更新（作者本人，版本乐观锁）")
    add("memory.deletion.request", _del_request, _owners(), True,
        description="提交删除申请（reason 必填；daily=10/lifetime=5 与旧系统一致）")
    add("memory.deletion.withdraw", _del_withdraw, _owners(), True,
        description="撤回 pending 删除申请")
    add("memory.deletion.decide", _del_decide, {"jiaming"}, True,
        description="审批删除申请（仅周家明；approve 才执行 archive/delete）")
    add("memory.deletion.list", _del_list, _owners(), False,
        description="删除申请列表")
    return caps


def invoke(principal: Principal, capability: str, arguments: dict,
           idempotency_key: str | None) -> dict:
    cap = REGISTRY.get(capability)
    if cap is None:
        raise NotFound("unknown capability", capability=capability)
    identity.require_any(principal, cap.allowed_principals)

    if cap.idempotent and idempotency_key:
        return _idempotent_invoke(principal, cap, arguments, idempotency_key)
    return {"ok": True, "data": cap.handler(principal, arguments)}


def _payload_hash(arguments: dict) -> str:
    return memory.canonical_hash(arguments)


def _idempotent_invoke(principal: Principal, cap: Capability, arguments: dict,
                       key: str) -> dict:
    ph = _payload_hash(arguments)
    with db.formal() as conn:
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE principal_id=? AND capability=?"
            " AND idempotency_key=?",
            (principal.principal_id, cap.name, key),
        ).fetchone()
        if row:
            if row["payload_hash"] != ph:
                raise IdempotencyConflict(
                    "same key with different payload",
                    capability=cap.name, key=key,
                )
            return {"ok": True, "data": json.loads(row["result_ref"]), "idempotent_replay": True}
    result = cap.handler(principal, arguments)
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO idempotency_records(principal_id, capability,"
                " idempotency_key, payload_hash, status, result_ref, created_at)"
                " VALUES(?,?,?,?, 'completed', ?, datetime('now'))",
                (principal.principal_id, cap.name, key, ph,
                 json.dumps(result, ensure_ascii=False)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"ok": True, "data": result}


# ---------- handlers：纯领域逻辑，不做传输层判断 ----------

def _hold(principal: Principal, a: dict) -> dict:
    return memory.hold(
        principal,
        text=str(a.get("text", "")),
        why_remember=a.get("why_remember"),
        memory_date=a.get("memory_date"),
        date_confidence=a.get("date_confidence", "unknown"),
        entry_source=principal.entry_source,
    )


def _get(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return memory.get(conn, str(a.get("memory_id", "")))


def _versions(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return {"versions": memory.versions_read(conn, str(a.get("memory_id", "")))}


def _search(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return retrieval_search.search(conn, str(a.get("query", "")),
                                       int(a.get("limit", 20)))


def _restore(principal: Principal, a: dict) -> dict:
    return memory.restore(
        principal,
        memory_id=str(a.get("memory_id", "")),
        expected_current_version=int(a["expected_current_version"]),
        target_history_version=a.get("target_history_version"),
    )


def _scan(principal: Principal, a: dict) -> dict:
    return workspace.scan_candidates(principal, a.get("min_idle_days"))


def _revise(principal: Principal, a: dict) -> dict:
    return workspace.revise_draft(
        principal,
        proposal_id=str(a.get("proposal_id", "")),
        compressed_summary=str(a.get("compressed_summary", "")),
        reason=str(a.get("reason", "")),
    )


def _submit(principal: Principal, a: dict) -> dict:
    return workspace.submit(
        principal,
        proposal_id=str(a.get("proposal_id", "")),
        revision=int(a.get("revision", 1)),
    )


def _decide(principal: Principal, a: dict) -> dict:
    return workspace.decide(
        principal,
        proposal_id=str(a.get("proposal_id", "")),
        proposal_revision=int(a.get("proposal_revision", 0)),
        proposal_hash=str(a.get("proposal_hash", "")),
        expected_memory_version=int(a.get("expected_memory_version", 0)),
        decision=str(a.get("decision", "")),
    )


def _list_items(principal: Principal, a: dict) -> dict:
    states = a.get("states")
    return {"items": workspace.list_items(states)}


def _raw_import(principal: Principal, a: dict) -> dict:
    return raw.import_payload(principal.principal_id, a)


def _raw_list(principal: Principal, a: dict) -> dict:
    return {"messages": raw.list_recent(int(a.get("limit", raw.BOOT_RAW_MESSAGES)),
                                         a.get("before")),
            "counts_messages_not_turns": True}


def _raw_search(principal: Principal, a: dict) -> dict:
    return raw.search(str(a.get("query", "")), int(a.get("limit", 20)))


def _raw_convs(principal: Principal, a: dict) -> dict:
    return {"conversations": raw.conversations_list(int(a.get("limit", 50)))}


def _quote_keep(principal: Principal, a: dict) -> dict:
    return quotes.keep(principal.principal_id, str(a.get("text", "")),
                       a.get("said_at"), a.get("said_at_confidence", "unknown"),
                       a.get("raw_ref"))


def _quote_list(principal: Principal, a: dict) -> dict:
    return {"quotes": quotes.list_quotes(bool(a.get("include_withdrawn", False)),
                                         int(a.get("limit", 100)))}


def _quote_search(principal: Principal, a: dict) -> dict:
    return quotes.search(str(a.get("query", "")), int(a.get("limit", 20)))


def _quote_withdraw(principal: Principal, a: dict) -> dict:
    return quotes.withdraw(principal.principal_id, str(a.get("quote_id", "")))


def _handoff_write(principal: Principal, a: dict) -> dict:
    return time_ctx.handoff_write(principal.principal_id, principal.entry_source,
                                  str(a.get("content", "")))


def _handoff_latest(principal: Principal, a: dict) -> dict:
    return time_ctx.handoff_latest()


def _plan_create(principal: Principal, a: dict) -> dict:
    return plans.create(
        principal.principal_id,
        title=str(a.get("title", "")), content=a.get("content"),
        state=str(a.get("state", "planned")),
        starts_at=a.get("starts_at"), due_at=a.get("due_at"),
        date_start=a.get("date_start"), date_end=a.get("date_end"),
        timezone_name=a.get("timezone"), all_day=bool(a.get("all_day", False)),
        weight=a.get("weight"), link_memory_ids=a.get("link_memory_ids"),
    )


def _plan_update(principal: Principal, a: dict) -> dict:
    return plans.update(
        principal.principal_id, str(a.get("plan_id", "")),
        int(a.get("expected_version", 0)), **{
            k: a[k] for k in
            ("title", "content", "state", "starts_at", "due_at", "date_start",
             "date_end", "weight") if k in a})


def _plan_list(principal: Principal, a: dict) -> dict:
    return {"plans": plans.list_plans(a.get("states"))}


def _cal_day(principal: Principal, a: dict) -> dict:
    return calendar.day(str(a.get("date", "")), a.get("types"))


def _cal_range(principal: Principal, a: dict) -> dict:
    return calendar.range_items(str(a.get("start_date", "")),
                                str(a.get("end_date", "")), a.get("types"))


def _cal_month(principal: Principal, a: dict) -> dict:
    return calendar.month(int(a.get("year", 0)), int(a.get("month", 0)), a.get("types"))


def _bootstrap(principal: Principal, a: dict) -> dict:
    return bootstrap.get(principal.principal_id, principal.entry_source,
                         str(a.get("profile", "")))


def _time_now(principal: Principal, a: dict) -> dict:
    return time_ctx.now()


def _time_ctx(principal: Principal, a: dict) -> dict:
    return time_ctx.context(principal.principal_id)


def _time_since(principal: Principal, a: dict) -> dict:
    return time_ctx.since(principal.principal_id)


def _presence_touch(principal: Principal, a: dict) -> dict:
    return time_ctx.presence_touch(principal.principal_id, principal.kind)


def _letter_write(principal: Principal, a: dict) -> dict:
    return letters.write_letter(principal.principal_id, str(a.get("content", "")),
                                a.get("letter_date"),
                                str(a.get("lock_type", "none")), a.get("unlock_date"))


def _letter_list(principal: Principal, a: dict) -> dict:
    return {"letters": letters.list_letters(a.get("author"))}


def _letter_read(principal: Principal, a: dict) -> dict:
    return letters.read_letter(principal.principal_id, str(a.get("letter_id", "")))


def _letter_edit(principal: Principal, a: dict) -> dict:
    return letters.edit_letter(
        principal.principal_id, str(a.get("letter_id", "")),
        int(a.get("expected_version", 0)), a.get("content"),
        a.get("lock_type"), a.get("unlock_date"))


def _del_request(principal: Principal, a: dict) -> dict:
    return letters.deletion_submit(
        principal.principal_id, str(a.get("resource_id", "")),
        str(a.get("reason", "")), str(a.get("action", "delete")),
        str(a.get("resource_kind", "memory")))


def _del_withdraw(principal: Principal, a: dict) -> dict:
    return letters.deletion_withdraw(principal.principal_id,
                                     str(a.get("resource_id", "")))


def _del_decide(principal: Principal, a: dict) -> dict:
    return letters.deletion_decide(
        principal.principal_id, str(a.get("request_id", "")),
        str(a.get("decision", "")), str(a.get("ai_reason", "")),
        str(a.get("expected_resource_id", "")))


def _del_list(principal: Principal, a: dict) -> dict:
    return {"requests": letters.deletion_list(a.get("status"))}


REGISTRY = _register()


def list_capabilities(principal: Principal) -> list[dict]:
    return [
        {"name": c.name, "write": c.write, "description": c.description}
        for c in REGISTRY.values()
        if principal.principal_id in c.allowed_principals
    ]
