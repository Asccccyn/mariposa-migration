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
from ..quotes import semantic_review
from ..plans import service as plans
from ..calendar import service as calendar
from ..time_context import service as time_ctx
from ..bootstrap import service as bootstrap
from ..letters import service as letters
from ..content import service as content
from ..memory import extras, listing, relations, reengagement
from ..memory import service as memory
from ..retrieval import rebuild as rebuild_mod
from ..workspace import tasks as ws_tasks
from ..raw import binding
from ..maintenance import service as maintenance
from ..media import service as media
from ..moments import service as moments
from ..reminders import service as reminders


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
    add("calendar.undated", _cal_undated, _owners(), False,
        description="待定日期区（日期未知不冒充今天）")
    add("memory.reengagement.record", _reengage, _owners(), True,
        description="记录真实再提起（按证据原时刻；扫描/访问不算）")
    add("identity.bindings.revoke", _binding_revoke, {"qiaosheng"}, True,
        description="撤销客户端绑定（旧 token 立即失效）")
    add("bootstrap.get", _bootstrap, {"jiaming"}, False,
        description="两入口开窗（entry_source 校验 profile；worker 拒绝）")
    add("bootstrap.next", _bootstrap_next, {"jiaming"}, False,
        description="续取开窗分页（snapshot 变化返回 SNAPSHOT_STALE）")
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
    add("home.get", _home_get, _owners(), False, description="共同 Home 正本")
    add("home.update", _home_update, _owners(), True,
        description="修改 Home（唯一正本，版本乐观锁）")
    add("self.write", _self_write, {"jiaming"}, True,
        description="周家明写 Self（立即正式，pending=隔日待回看）")
    add("self.list", _self_list, _owners(), False,
        description="Self 列表（retired 不主动浮现）")
    add("self.review", _self_review, {"jiaming"}, True,
        description="隔日回看 Self（另一共同当地日起）")
    add("self.revise", _self_revise, {"jiaming"}, True,
        description="修订 Self（隔日，版本留底）")
    add("self.retire", _self_retire, {"jiaming"}, True,
        description="退役 Self（不物理删，历史可查）")
    add("diary.write", _diary_write, _owners(), True,
        description="写日记（本人作品，全文保留）")
    add("diary.list", _diary_list, _owners(), False, description="日记列表")
    add("diary.search", _diary_search, _owners(), False,
        description="日记独立检索（source=diary）")
    add("diary.hide", _diary_hide, _owners(), True,
        description="隐藏/显示日记（仅作者）")
    add("memory.tags.add", _tags_add, _owners(), True,
        description="加标签（情绪标签 whose 必填）")
    add("memory.by_emotion", _by_emotion, _owners(), False,
        description="按情绪查（结构化入口，遗忘桶仍可查）")
    add("workspace.quotes.review.run", _quote_review_run,
        {"worker", "jiaming", "qiaosheng"}, True,
        description="执行她的话语义校对（provider 未配置一律挂起不写）")
    add("workspace.quotes.reviews.list", _quote_reviews_list, _owners(), False,
        description="校对工作项列表")
    add("memory.update", _memory_update, _owners(), True,
        description="修改桶正文（新版本，不就地覆盖；遗忘桶先恢复）")
    add("memory.pin", lambda pr, a: extras.set_flag(
        pr.principal_id, str(a.get("memory_id", "")), "pin",
        bool(a.get("value", True))), _owners(), True,
        description="置顶/取消（排除自动候选）")
    add("memory.protect", lambda pr, a: extras.set_flag(
        pr.principal_id, str(a.get("memory_id", "")), "protect",
        bool(a.get("value", True))), _owners(), True, description="保护/取消")
    add("memory.anchor", lambda pr, a: extras.set_flag(
        pr.principal_id, str(a.get("memory_id", "")), "anchor",
        bool(a.get("value", True))), _owners(), True, description="锚定/取消")
    add("memory.versions.list", _versions_list, _owners(), False,
        description="版本列表（不含正文；正文走 versions.read）")
    add("memory.relations.link", _rel_link, _owners(), True,
        description="建立关联（单向存储，显式反向查询）")
    add("memory.relations.detach", _rel_detach, _owners(), True,
        description="断开关联（留历史）")
    add("memory.relations.list", _rel_list, _owners(), False, description="列出关联")
    add("memory.relations.trace", _rel_trace, _owners(), False,
        description="沿 continuation_of 追事件链")
    add("raw.provisional.report", _prov_report, {"jiaming"}, True,
        description="周家明报告现场复述片段（非已验证原文）")
    add("raw.binding.bind", _raw_bind, _owners(), True,
        description="原文范围绑定到桶（不改 Hold 内容；重复范围 DEDUPE_NEEDS_REVIEW）")
    add("raw.binding.revoke", _raw_bind_revoke, _owners(), True,
        description="撤销绑定（留历史，桶回 raw_pending）")
    add("raw.binding.refs", _raw_refs, _owners(), False, description="桶的原文绑定列表")
    add("maintenance.outbox.drain", _outbox_drain, _owners(), True,
        description="消费 outbox（至少一次+幂等标记）")
    add("maintenance.outbox.status", _outbox_status, _owners(), False,
        description="outbox 待处理统计")
    add("maintenance.activity.list", _activity_list, _owners(), False,
        description="审计查询（管理接口，不参与召回）")
    add("maintenance.reminders.fire_due", _fire_due,
        {"worker", "qiaosheng", "jiaming"}, True,
        description="到期提醒结算（幂等；无常驻 scheduler，无外部副作用）")
    add("media.upload.prepare", _media_prepare, _owners(), False,
        description="申请上传 token（字节走专用 HTTP 端点，不进工具参数）")
    add("media.upload.finalize", _media_finalize, _owners(), True, True,
        description="完成上传（hash 去重）")
    add("media.list", _media_list, _owners(), False, description="媒体对象列表")
    add("media.get", _media_get_meta, _owners(), False, description="媒体元数据")
    add("moments.post", _moments_post, _owners(), True, description="发朋友圈（post）")
    add("moments.list", _moments_list, _owners(), False, description="朋友圈列表")
    add("moments.comment", _moments_comment, _owners(), True, description="评论")
    add("moments.react", _moments_react, _owners(), True, description="回应")
    add("reminder.create", _reminder_create, _owners(), True, description="创建提醒")
    add("reminder.list", _reminder_list, _owners(), False, description="提醒列表")
    add("reminder.cancel", _reminder_cancel, _owners(), True, description="取消提醒")
    add("workspace.proposals.decide_batch", _decide_batch, _owners(), True, True,
        description="批量决议（逐项冻结 ID/hash；拒绝写冷却）")
    add("memory.list", _memory_list, _owners(), False,
        description="记忆倒序列表（遗忘桶只给摘要表示）")
    add("memory.by_date", _by_date, _owners(), False, description="按事件日期查")
    add("memory.by_tag", _by_tag, _owners(), False, description="按标签查（结构化入口）")
    add("memory.meanings.append", _meaning_append, {"jiaming"}, True,
        description="追加 meaning 层（只周家明；纳入 full 投影）")
    add("memory.meanings.replace", _meaning_replace, {"jiaming"}, True,
        description="替换 meaning 层（旧层留底，层号归档）")
    add("memory.meanings.list", _meaning_list, _owners(), False,
        description="列出当前 meaning 层")
    add("maintenance.rebuild_index", _rebuild_index, _owners(), True,
        description="按当前版本重建全部投影与 FTS（遗忘桶仅摘要）")
    add("maintenance.semantic.warmup", _semantic_warmup, _owners(), True,
        description="全量预热语义向量（冷启动/重建后一次；查询路径仅限流补算）")
    add("workspace.tasks.list", _tasks_list, {"worker", "qiaosheng", "jiaming"}, False,
        description="可认领工作项列表")
    add("workspace.tasks.claim", _task_claim, {"worker", "qiaosheng", "jiaming"}, True,
        description="认领任务租约（30 分钟，持久化）")
    add("workspace.tasks.release", _task_release, {"worker", "qiaosheng", "jiaming"}, True,
        description="释放租约（仅认领人）")
    add("workspace.memory.inspect", _memory_inspect, {"worker", "qiaosheng", "jiaming"}, False,
        description="读取授权任务材料（不默认全库）")
    add("memory.quotes.get", _quote_get, _owners(), False, description="单条她的话")
    add("memory.quotes.by_memory", _quote_by_memory, _owners(), False,
        description="按记忆找相关她的话")
    add("diary.read", _diary_read, _owners(), False, description="读单篇日记")
    add("diary.revise", _diary_revise, _owners(), True,
        description="修订日记（版本留底，全文不摘要）")
    add("calendar.providers", _cal_providers, _owners(), False,
        description="已注册日历源列表")
    add("presence.status", _presence_status, _everyone(), False,
        description="当前活动状态（三时间线概览）")
    add("maintenance.jobs.status", _jobs_status, _owners(), False,
        description="维护任务状态总览")
    add("maintenance.settings.get", _settings_get, _owners(), False,
        description="只读配置快照（时区/开窗/遗忘/provider 状态）")
    add("sticker.list", _sticker_list, _owners(), False, description="表情列表")
    add("sticker.add", _sticker_add, {"qiaosheng", "jiaming"}, True,
        description="添加表情（复用媒体 hash 去重）")
    add("sticker.search", _sticker_search, _owners(), False, description="按标签搜表情")
    add("raw.import.prepare", _raw_import_prepare, {"worker", "qiaosheng", "jiaming"}, True,
        description="两阶段导入：登记任务（解析器版本化）")
    add("raw.import.status", _raw_import_status, {"worker", "qiaosheng", "jiaming"}, False,
        description="导入任务状态")
    add("raw.read", _raw_read, _owners(), False,
        description="按 ID 读单条原文（source=raw）")
    add("emotion.context.get", _emotion_reserved, _owners(), False,
        description="情绪补充召回（reserved，默认禁用）")
    add("listening.status", _listening_reserved, _owners(), False,
        description="一起听歌（reserved，未选供应商）")
    return caps


class Busy(MariposaError):
    """同幂等键的执行仍在进行（并发的另一方尚未回填终态）。"""
    code = "IDEMPOTENCY_IN_PROGRESS"
    http_status = 409

    def __init__(self):
        super().__init__("idempotent execution still in progress; retry")


def invoke(principal: Principal, capability: str, arguments: dict,
           idempotency_key: str | None) -> dict:
    cap = REGISTRY.get(capability)
    if cap is None:
        raise NotFound("unknown capability", capability=capability)
    identity.require_any(principal, cap.allowed_principals)
    from . import input_schemas
    input_schemas.validate(capability, arguments)

    # 幂等键由调用方显式给出即生效（与 cap.idempotent hint 无关）：
    # 同 key 同 payload 必须可安全重试（U16/OPS-02；原子 claim 防并发双副作用）
    if idempotency_key:
        return _idempotent_invoke(principal, cap, arguments, idempotency_key)
    return {"ok": True, "data": cap.handler(principal, arguments)}


def _payload_hash(arguments: dict) -> str:
    return memory.canonical_hash(arguments)


def _idempotent_invoke(principal: Principal, cap: Capability, arguments: dict,
                       key: str) -> dict:
    """原子 claim 幂等：先 INSERT 占位（status=running），占位成功者才执行副作用。

    并发同 key：仅一方能占位；另一方等待后读终态重放。
    崩溃残留的 running（>60s）视为可重占（对账语义：副作用由业务幂等键兜底）。
    """
    ph = _payload_hash(arguments)

    def claim() -> bool:
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 清理崩溃残留（超时 running）
                conn.execute(
                    "DELETE FROM idempotency_records WHERE principal_id=? AND"
                    " capability=? AND idempotency_key=? AND status='running'"
                    " AND created_at < datetime('now', '-60 seconds')",
                    (principal.principal_id, cap.name, key))
                cur = conn.execute(
                    "INSERT OR IGNORE INTO idempotency_records(principal_id,"
                    " capability, idempotency_key, payload_hash, status,"
                    " result_ref, created_at)"
                    " VALUES(?,?,?,?, 'running', NULL, datetime('now'))",
                    (principal.principal_id, cap.name, key, ph))
                conn.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def read_record():
        with db.formal() as conn:
            return conn.execute(
                "SELECT * FROM idempotency_records WHERE principal_id=? AND"
                " capability=? AND idempotency_key=?",
                (principal.principal_id, cap.name, key)).fetchone()

    if not claim():
        # 另一方持有：等待其完成（bounded）
        import time as _time
        for _ in range(40):  # <=8s
            _time.sleep(0.2)
            row = read_record()
            if row is None:
                break  # 被清理，重试 claim
            if row["payload_hash"] != ph:
                raise IdempotencyConflict(
                    "same key with different payload",
                    capability=cap.name, key=key)
            if row["status"] == "completed":
                return {"ok": True,
                        "data": json.loads(row["result_ref"]),
                        "idempotent_replay": True}
        # 仍在 running（执行方超时未回填）：拒绝盲重放，让调用方重试
        raise Busy()

    result = cap.handler(principal, arguments)
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE idempotency_records SET status='completed', result_ref=?"
                " WHERE principal_id=? AND capability=? AND idempotency_key=?"
                " AND status='running'",
                (json.dumps(result, ensure_ascii=False), principal.principal_id,
                 cap.name, key))
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
        raw_refs=a.get("raw_refs"),
        raw_pending=bool(a.get("raw_pending", True)),
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
    from .. import config as _cfg
    pv = a.get("policy_version")
    if pv and pv != _cfg.POLICY_VERSION:
        raise Forbidden("policy_version mismatch",
                        code="VERSION_CONFLICT",
                        expected=_cfg.POLICY_VERSION, got=pv)
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
        revision=int(a.get("proposal_revision", a.get("revision", 1))),
        expected_hash=a.get("proposal_hash"),
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
                       a.get("said_at"),
                       a.get("date_confidence")
                       or a.get("said_at_confidence", "unknown"),
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


def _cal_undated(principal: Principal, a: dict) -> dict:
    return calendar.undated(a.get("types"))


def _reengage(principal: Principal, a: dict) -> dict:
    return reengagement.record(
        principal.principal_id, str(a.get("memory_id", "")),
        str(a.get("evidence_kind", "")), str(a.get("occurred_at", "")),
        a.get("evidence_ref"))


def _binding_revoke(principal: Principal, a: dict) -> dict:
    return identity.revoke_binding(principal.principal_id,
                                   str(a.get("binding_id", "")))


def _bootstrap(principal: Principal, a: dict) -> dict:
    return bootstrap.get(principal.principal_id, principal.entry_source,
                         str(a.get("profile", "")),
                         a.get("loaded_snapshot_id"), a.get("cursor"))


def _bootstrap_next(principal: Principal, a: dict) -> dict:
    return bootstrap.next_page(principal.principal_id, principal.entry_source,
                               str(a.get("snapshot_id", "")),
                               a.get("cursor") or {},
                               str(a.get("section", "raw")))


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


def _home_get(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return content.home_get(conn)


def _home_update(principal: Principal, a: dict) -> dict:
    return content.home_update(principal.principal_id, str(a.get("content", "")),
                               int(a.get("expected_version", 0)))


def _self_write(principal: Principal, a: dict) -> dict:
    return content.self_write(principal.principal_id, str(a.get("content", "")),
                              str(a.get("aspect", "")))


def _self_list(principal: Principal, a: dict) -> dict:
    return {"entries": content.self_list(bool(a.get("include_retired", False)))}


def _self_review(principal: Principal, a: dict) -> dict:
    return content.self_review(principal.principal_id, str(a.get("self_id", "")))


def _self_revise(principal: Principal, a: dict) -> dict:
    return content.self_revise(principal.principal_id, str(a.get("self_id", "")),
                               str(a.get("content", "")),
                               int(a.get("expected_version", 0)))


def _self_retire(principal: Principal, a: dict) -> dict:
    return content.self_retire(principal.principal_id, str(a.get("self_id", "")))


def _diary_write(principal: Principal, a: dict) -> dict:
    return content.diary_write(principal.principal_id, str(a.get("title", "")),
                               str(a.get("content", "")),
                               a.get("covers_from"), a.get("covers_to"))


def _diary_list(principal: Principal, a: dict) -> dict:
    return {"entries": content.diary_list(bool(a.get("include_hidden", False)),
                                          a.get("author"))}


def _diary_search(principal: Principal, a: dict) -> dict:
    return content.diary_search(str(a.get("query", "")), int(a.get("limit", 20)))


def _diary_hide(principal: Principal, a: dict) -> dict:
    return content.diary_hide(principal.principal_id, str(a.get("diary_id", "")),
                              bool(a.get("hide", True)))


def _tags_add(principal: Principal, a: dict) -> dict:
    tags = a.get("tags") or []
    if not isinstance(tags, list):
        raise Forbidden("tags must be a list")
    return content.tags_add(principal.principal_id, str(a.get("memory_id", "")), tags)


def _by_emotion(principal: Principal, a: dict) -> dict:
    return content.by_emotion(str(a.get("tag", "")), str(a.get("whose", "")))


def _quote_review_run(principal: Principal, a: dict) -> dict:
    return semantic_review.run_review(str(a.get("quote_id", "")),
                                      a.get("raw_text"))


def _quote_reviews_list(principal: Principal, a: dict) -> dict:
    return {"items": semantic_review.reviews_list(a.get("states"))}


def _memory_update(principal: Principal, a: dict) -> dict:
    return extras.update_text(
        principal.principal_id, str(a.get("memory_id", "")),
        int(a.get("expected_version", 0)), a.get("text"), a.get("why_remember"),
        a.get("memory_date"), a.get("date_confidence"))


def _versions_list(principal: Principal, a: dict) -> dict:
    return {"versions": extras.versions_list(str(a.get("memory_id", "")))}


def _rel_link(principal: Principal, a: dict) -> dict:
    return relations.link(principal.principal_id,
                          str(a.get("from_memory", "")), str(a.get("to_memory", "")),
                          str(a.get("relation_type", "")),
                          a.get("custom_label"), a.get("reverse_label"))


def _rel_detach(principal: Principal, a: dict) -> dict:
    return relations.detach(principal.principal_id,
                            str(a.get("from_memory", "")), str(a.get("to_memory", "")),
                            str(a.get("relation_type", "")))


def _rel_list(principal: Principal, a: dict) -> dict:
    return {"relations": relations.list_for(str(a.get("memory_id", "")),
                                            str(a.get("direction", "both")))}


def _rel_trace(principal: Principal, a: dict) -> dict:
    return relations.trace(str(a.get("memory_id", "")),
                           int(a.get("max_depth", 5)))


def _prov_report(principal: Principal, a: dict) -> dict:
    return binding.report_fragment(principal.principal_id,
                                   str(a.get("fragment", "")), a.get("memory_id"))


def _raw_bind(principal: Principal, a: dict) -> dict:
    return binding.bind(
        principal.principal_id, str(a.get("memory_id", "")),
        str(a.get("conversation_id", "")), str(a.get("message_from", "")),
        str(a.get("message_to", "")), str(a.get("confidence", "high")))


def _raw_bind_revoke(principal: Principal, a: dict) -> dict:
    return binding.revoke(principal.principal_id, str(a.get("memory_id", "")),
                          str(a.get("conversation_id", "")))


def _raw_refs(principal: Principal, a: dict) -> dict:
    return {"refs": binding.refs_of(str(a.get("memory_id", "")))}


def _outbox_drain(principal: Principal, a: dict) -> dict:
    return maintenance.outbox_drain(int(a.get("limit", 100)))


def _outbox_status(principal: Principal, a: dict) -> dict:
    return maintenance.outbox_status()


def _activity_list(principal: Principal, a: dict) -> dict:
    return {"events": maintenance.activity_list(int(a.get("limit", 50)),
                                                a.get("event_type"))}


def _fire_due(principal: Principal, a: dict) -> dict:
    return maintenance.reminders_fire_due(a.get("now"))


def _media_prepare(principal: Principal, a: dict) -> dict:
    return media.upload_prepare(principal.principal_id,
                                str(a.get("mime", "")), int(a.get("size", 0)))


def _media_finalize(principal: Principal, a: dict) -> dict:
    import base64
    data = base64.b64decode(str(a.get("data_b64", "")))
    return media.upload_finalize(principal.principal_id,
                                 str(a.get("upload_token", "")), data)


def _media_list(principal: Principal, a: dict) -> dict:
    return {"objects": media.list_media(int(a.get("limit", 50)))}


def _media_get_meta(principal: Principal, a: dict) -> dict:
    meta, _ = media.get_media(principal.principal_id,
                              str(a.get("content_hash", "")))
    return meta


def _moments_post(principal: Principal, a: dict) -> dict:
    return moments.post(principal.principal_id, str(a.get("content", "")),
                        a.get("media_hash"))


def _moments_list(principal: Principal, a: dict) -> dict:
    return {"moments": moments.list_moments(str(a.get("kind", "post")),
                                             int(a.get("limit", 50)))}


def _moments_comment(principal: Principal, a: dict) -> dict:
    return moments.comment(principal.principal_id, str(a.get("moment_id", "")),
                           str(a.get("content", "")))


def _moments_react(principal: Principal, a: dict) -> dict:
    return moments.react(principal.principal_id, str(a.get("moment_id", "")),
                         str(a.get("reaction", "")))


def _reminder_create(principal: Principal, a: dict) -> dict:
    return reminders.create(principal.principal_id, str(a.get("title", "")),
                            str(a.get("remind_at", "")), a.get("note"),
                            a.get("timezone"))


def _reminder_list(principal: Principal, a: dict) -> dict:
    return {"reminders": reminders.list_reminders(a.get("states"))}


def _reminder_cancel(principal: Principal, a: dict) -> dict:
    return reminders.cancel(principal.principal_id, str(a.get("reminder_id", "")))


def _decide_batch(principal: Principal, a: dict) -> dict:
    items = a.get("items") or []
    if not isinstance(items, list):
        raise Forbidden("items must be a list")
    return workspace.decide_batch(principal, items)


def _memory_list(principal: Principal, a: dict) -> dict:
    return listing.list_memories(a.get("state"), int(a.get("limit", 50)),
                                 a.get("cursor_date"))


def _by_date(principal: Principal, a: dict) -> dict:
    return listing.by_date(str(a.get("date", "")))


def _by_tag(principal: Principal, a: dict) -> dict:
    return listing.by_tag(str(a.get("namespace", "tag")), str(a.get("tag", "")),
                          a.get("whose"))


def _meaning_append(principal: Principal, a: dict) -> dict:
    return listing.meanings_append(principal.principal_id,
                                   str(a.get("memory_id", "")),
                                   str(a.get("content", "")))


def _meaning_replace(principal: Principal, a: dict) -> dict:
    layers = a.get("new_layers") or []
    if not isinstance(layers, list):
        raise Forbidden("new_layers must be a list")
    return listing.meanings_replace(principal.principal_id,
                                    str(a.get("memory_id", "")), layers)


def _meaning_list(principal: Principal, a: dict) -> dict:
    return {"layers": listing.meanings_list(str(a.get("memory_id", "")))}


def _rebuild_index(principal: Principal, a: dict) -> dict:
    return rebuild_mod.rebuild_index(principal.principal_id)


def _semantic_warmup(principal: Principal, a: dict) -> dict:
    from ..retrieval import semantic as _sem
    with db.formal() as conn:
        return _sem.warmup(conn)


def _tasks_list(principal: Principal, a: dict) -> dict:
    return {"tasks": ws_tasks.tasks_list()}


def _task_claim(principal: Principal, a: dict) -> dict:
    return ws_tasks.task_claim(principal.principal_id,
                               str(a.get("task_key", "")))


def _task_release(principal: Principal, a: dict) -> dict:
    return ws_tasks.task_release(principal.principal_id,
                                 str(a.get("lease_id", "")))


def _memory_inspect(principal: Principal, a: dict) -> dict:
    return ws_tasks.memory_inspect(principal.principal_id,
                                   str(a.get("memory_id", "")))


def _quote_get(principal: Principal, a: dict) -> dict:
    return quotes.get_quote(str(a.get("quote_id", "")))


def _quote_by_memory(principal: Principal, a: dict) -> dict:
    return {"quotes": quotes.by_memory(str(a.get("memory_id", "")))}


def _diary_read(principal: Principal, a: dict) -> dict:
    return content.diary_read(str(a.get("diary_id", "")))


def _diary_revise(principal: Principal, a: dict) -> dict:
    return content.diary_revise(principal.principal_id,
                                str(a.get("diary_id", "")),
                                int(a.get("expected_version", 0)),
                                a.get("title"), a.get("content"))


def _cal_providers(principal: Principal, a: dict) -> dict:
    return {"providers": content.calendar_providers()}


def _presence_status(principal: Principal, a: dict) -> dict:
    return time_ctx.context(principal.principal_id)


def _jobs_status(principal: Principal, a: dict) -> dict:
    return maintenance.jobs_status()


def _settings_get(principal: Principal, a: dict) -> dict:
    from .. import config as _cfg
    from ..calendar import service as _cal
    from ..raw import service as _raw
    from ..bootstrap import service as _bs
    return {
        "relationship_timezone": _cfg.RELATIONSHIP_TIMEZONE,
        "http": {"bind": _cfg.HTTP_BIND, "port": _cfg.HTTP_PORT},
        "forgetting": {
            "idle_days": _cfg.FORGET_IDLE_DAYS,
            "schedule_enabled": _cfg.FORGET_SCHEDULE_ENABLED,
            "reject_cooldown_days": _cfg.FORGET_REJECT_COOLDOWN_DAYS,
            "scan_batch_size": _cfg.FORGET_SCAN_BATCH_SIZE,
            "policy_version": _cfg.POLICY_VERSION,
        },
        "bootstrap": {
            "raw_messages": _raw.BOOT_RAW_MESSAGES,
            "memory_days": _bs.BOOT_MEMORY_DAYS,
            "plan_upcoming_days": _bs.PLAN_UPCOMING_DAYS,
            "soft_token_budget": _bs.BOOT_SOFT_TOKEN_BUDGET,
        },
        "retrieval": {
            "projection_revision": _cfg.PROJECTION_REVISION,
            "semantic_provider": _cfg.SEMANTIC_PROVIDER or None,
            "semantic_status": "unavailable" if not _cfg.SEMANTIC_PROVIDER
                               else "configured",
        },
        "quote_semantic_auto_apply": _cfg.QUOTE_SEMANTIC_AUTO_APPLY,
        "calendar_providers": sorted(_cal.PROVIDERS),
        "note": "只读快照；修改经部署参数（policy version 变更入审计）",
    }


def _sticker_list(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT content_hash, label, mime FROM stickers ORDER BY label"
        ).fetchall()
    return {"stickers": [dict(r) for r in rows]}


def _sticker_add(principal: Principal, a: dict) -> dict:
    import hashlib as _hl
    from .. import config as _cfg
    from datetime import datetime as _dt
    label = str(a.get("label", "")).strip()
    content_hash = str(a.get("content_hash", ""))
    if not label:
        raise Forbidden("sticker label required")
    with db.formal() as conn:
        exists_media = conn.execute(
            "SELECT 1 FROM media_objects WHERE content_hash=?",
            (content_hash,)).fetchone()
        if not exists_media:
            raise Forbidden("sticker must reference an uploaded media object")
        conn.execute(
            "INSERT OR REPLACE INTO stickers(content_hash, label, mime,"
            " storage_key, created_at) VALUES(?,?,?,?,?)",
            (content_hash, label,
             conn.execute("SELECT mime FROM media_objects WHERE content_hash=?",
                          (content_hash,)).fetchone()["mime"],
             conn.execute("SELECT storage_key FROM media_objects WHERE"
                          " content_hash=?", (content_hash,)).fetchone()["storage_key"],
             _dt.utcnow().isoformat()))
    return {"content_hash": content_hash, "label": label}


def _sticker_search(principal: Principal, a: dict) -> dict:
    q = str(a.get("query", ""))
    with db.formal() as conn:
        rows = conn.execute(
            "SELECT content_hash, label, mime FROM stickers ORDER BY label"
        ).fetchall()
    target = _st_tok(q)  # normalize_search_text 已是规范化字符串
    hits = [dict(r) for r in rows if not q or target in _st_tok(r["label"])]
    return {"stickers": hits}


def _st_tok(text: str) -> str:
    from ..retrieval import projection as _pj
    return _pj.normalize_search_text(text)


def _raw_import_prepare(principal: Principal, a: dict) -> dict:
    return raw.import_prepare(principal.principal_id,
                              str(a.get("source_channel", "")),
                              str(a.get("external_id", "")),
                              int(a.get("message_count", 0)))


def _raw_import_status(principal: Principal, a: dict) -> dict:
    return raw.import_status(str(a.get("job_id", "")))


def _raw_read(principal: Principal, a: dict) -> dict:
    return raw.read_message(str(a.get("message_id", "")))


def _emotion_reserved(principal: Principal, a: dict) -> dict:
    return {"enabled": False, "status": "reserved",
            "note": "EMOTION_RETRIEVAL_ENABLED 默认 false；启用也只调整补充召回，"
                    "不篡改基础时间/原文/三天桶/计划",
            "config_key": "EMOTION_RETRIEVAL_ENABLED", "configured": False}


def _listening_reserved(principal: Principal, a: dict) -> dict:
    return {"status": "reserved", "provider": None,
            "note": "一起听歌未选供应商；不承诺第三方曲库；capability 标 reserved"}


REGISTRY = _register()


def list_capabilities(principal: Principal) -> list[dict]:
    return [
        {"name": c.name, "write": c.write, "description": c.description}
        for c in REGISTRY.values()
        if principal.principal_id in c.allowed_principals
    ]
