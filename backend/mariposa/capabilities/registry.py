"""Capability Registry：一份注册表，多个适配器（§17.1）。

HTTP / MCP / CC 都调这里注册的 handler；权限按 principal 声明，
幂等键统一在本层落库。路径、传输不赋予角色。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .. import db
from ..errors import (Forbidden, IdempotencyConflict, MariposaError,
                      NotFound, OutcomeUnknown, StaleOperation)
from ..identity import Principal
from ..identity import service as identity
from ..memory import service as memory
from ..memory import relations as relations_mod
from ..retrieval import search as retrieval_search
from ..recall import service as recall_service
from ..recall import store as recall_store
from ..retrieval import words as words_mod
from ..plans import service as plans
from ..time_context import service as time_ctx
from ..bootstrap import service as bootstrap
from ..deletion import service as deletion
from ..memory import extras, listing, relations, reengagement
from ..memory import service as memory
from ..retrieval import rebuild as rebuild_mod
from ..maintenance import service as maintenance
from ..media import service as media
from ..source import importer as source_importer
from ..source import query as source_query
from ..source import binding as source_binding


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


def _maintainers() -> set[str]:
    """P1-5（2026-10-02 接续复审）：maintenance 工具的授权集——
    /mcp/maintenance profile 只放行 worker，而此前这些能力只授权
    owners，形成死入口（owner 进不去、worker 拿不到）。owners 经
    HTTP 仍可用，worker 经 maintenance profile 可用。"""
    return {"qiaosheng", "jiaming", "worker"}


def _register() -> dict[str, Capability]:
    caps: dict[str, Capability] = {}

    def add(name, handler, allowed, write, idempotent=False, description=""):
        caps[name] = Capability(name, handler, set(allowed), write, idempotent, description)

    add("memory.hold", _hold, {"qiaosheng", "jiaming"}, True,
        description="写入一条正式记忆（v1.7 分层：标题/九分类/事件/同期心情/我们的话）")
    add("memory.get", _get, _owners(), False, description="读取当前表示（遗忘桶只返回摘要）")
    add("memory.open", _open, _owners(), True,
        description="明确打开：返回当前表示并签发一次性查看票据（不自动确认）")
    add("memory.view.confirm", _view_confirm, _owners(), True, True,
        description="确认本次明确查看（明开回温：explicit-open basis，刷新回温基准；plan 不适用）")
    add("memory.recollections.append", _recollect_append, _owners(), True, True,
        description="凭有效查看回执追加本人回忆（不索引；触发保留线索）")
    add("memory.recollections.revise", _recollect_revise, _owners(), True,
        description="修订本人回忆（原话留底，supersedes 链）")
    add("memory.recollections.list", _recollect_list, _owners(), False,
        description="列出桶的回忆（当前版；include_history 含修订历史）")
    add("memory.keep.revoke", _keep_revoke, _owners(), True,
        description="撤销本人的「留」标记（谁留谁撤；撤销不重置年龄）")
    add("memory.keeps.list", _keeps_list, _owners(), False,
        description="桶的留标记列表（含已撤销；阶段用有效标记）")
    add("memory.our_words.append", _our_words_append, _owners(), True,
        description="追加我们的话（speaker/ordinal；参与 WIDE 阶段召回）")
    add("memory.mood.write", _mood_write, {"jiaming"}, True,
        description="补写/修正当前心情（标签+一段自由文字；非原文、"
                    "不参与检索；允许后补）")
    add("memory.our_words.list", _our_words_list, _owners(), False,
        description="列出桶内双方话语（按 ordinal）")
    add("memory.categories.replace", _categories_replace, _owners(), True,
        description="整组替换九分类（平行多选，非空必填）")
    add("i.get", _i_get, _owners(), False, description="I 正本当前版（周家明写）")
    add("i.write", _i_write, {"jiaming"}, True,
        description="兼容单条 i_main 写入；进入多条目模式后拒绝整篇覆盖")
    add("i.versions.read", _i_versions, _owners(), False,
        description="兼容整篇 I 快照历史；条目历史优先用 i.item.history")
    add("i.items.list", _i_items_list, _owners(), False,
        description="I 当前生效条目；旧版需显式读取")
    add("i.item.get", _i_item_get, _owners(), False,
        description="读取单条当前 I")
    add("i.item.create", _i_item_create, {"jiaming"}, True,
        description="新增一条 I")
    add("i.item.revise", _i_item_revise, {"jiaming"}, True,
        description="修改 I：追加 revision，可记录理由和旧版启发")
    add("i.item.restore", _i_item_restore, {"jiaming"}, True,
        description="恢复旧 I：复制旧版形成新 revision")
    add("i.item.history", _i_item_history, _owners(), False,
        description="显式读取某条 I 的历史、理由和记忆关联")
    add("i.suggest", _i_suggest, {"qiaosheng"}, True,
        description="乔生提建议（待提议材料，不改正本）")
    add("i.suggestions.list", _i_suggestions, _owners(), False,
        description="I 建议列表")
    add("memory.search", _search, _owners(), False, description="关键词检索有效投影")
    add("memory.recall", _recall, _owners(), False,
        description="v2 统一召回：query可空浏览；分类/心情标签/事件日期筛选；any/all；去重分页")
    # v1.3/v1.4 召回运行时：共享 Recall Session 七动作 + 独立 words 通道。
    # session 正本只在 Mariposa runtime 库；Chat 与 CC 等权同入口。
    add("memory.recall.start", _recall_start, _owners(), True,
        description="新建 Recall Session 并执行首轮检索（0—3 条证据包）")
    add("memory.recall.refine", _recall_refine, _owners(), True,
        description="同 session 纠正：改条件/证据需求/继续申请新 burst（revision+1）")
    add("memory.recall.reject", _recall_reject, _owners(), True,
        description="session-local 拒绝候选（candidate/event/word/source_selection）")
    add("memory.recall.accept", _recall_accept, _owners(), True,
        description="确认目标候选；可选 close 一并解决")
    add("memory.recall.navigate", _recall_navigate, _owners(), True,
        description="沿 temporal_axis 导航前后候选（earlier/later）")
    add("memory.recall.status", _recall_status, _owners(), False,
        description="当前 session 状态+引用重校验（不重放旧正文）")
    add("memory.recall.close", _recall_close, _owners(), True,
        description="显式结束：resolved/cancelled；终止本次自动补查")
    # 全量审计 P1-05：两者都创建 Recall Session/Round（有状态写），
    # write=False 会把 readOnlyHint=true 暴露给 MCP 客户端、诱导自动重试
    add("memory.words.recall", _words_recall, _owners(), True,
        description="独立 words 通道检索（我们的话；不混入 event ranking）")
    add("memory.find_words", _find_words, _owners(), True,
        description="v1.7 找话专项：全量可见 our_words 跨阶段检索")
    add("memory.recall.round2", _recall_round2, _owners(), True,
        description="v1.7 Round 2 原文深搜：同 session+revision、"
                    "ROUND1_COMPLETE 回执、judge 完成且 reason 属闭集")
    add("memory.words.get", _words_get, _owners(), False,
        description="按 word_id 读单条话语（当前表示校验；遗忘=disabled）")
    add("memory.context.validate", _context_validate, _owners(), False,
        description="装配上下文的资源引用+版本重查（estómago/CC 换窗用）")
    add("memory.versions.read", _versions, _owners(), False,
        description="明确展开历史版本，不自动 restore")
    # v1.7：遗忘/摘要/审查链已整体退役（决策 2026-09-28）——
    # memory.restore、workspace.forgetting.*、workspace.proposals.*、
    # memory.forgetting.decide、workspace.review.*、memory.retention.decide、
    # workspace.memory.inspect 不再注册；旧请求获 UNKNOWN_CAPABILITY。
    add("maintenance.idempotency.reconcile", _idem_reconcile, _maintainers(), True,
        description="崩溃窗口对账：核实业务结果后清除 running 幂等占位")
    add("source.import", _source_import, _owners(), True, True,
        description="导入 Claude conversations 导出（.json/.zip；流式解析；"
                    "Raw Archive 留只读母本；同 provider+sha256 幂等）")
    add("source.import.status", _source_import_status, _owners(), False,
        description="导入批次状态与统计（batch_id；失败含错误信息）")
    add("source.import.batches", _source_import_batches, _owners(), False,
        description="导入批次列表")
    add("source.search", _source_search, _owners(), False,
        description="原文专项检索：关键词/说话人/日期区间/会话。"
                    "不是普通 Recall，不参与记忆召回（分层边界）")
    add("source.message.get", _source_message_get, _owners(), False,
        description="按 provider UUID 精确打开原文消息（含前后上下文）")
    add("source.range.open", _source_range_open, _owners(), False,
        description="打开原文消息区间（语义绑定的动态查看入口）")
    add("source.conversation.get", _source_conversation_get, _owners(), False,
        description="按 sequence 游标分页读原文会话（超长会话不整段拉取）")
    add("source.conversations.list", _source_conversations_list, _owners(),
        False, description="原文会话列表（标题/时间/条数）")
    add("source.binding.bind", _source_bind, _owners(), True,
        description="把 memory 绑定到原文消息区间（可叠加多个 range；"
                    "默认消息边界，句内片段用可选 char offset）")
    add("source.binding.list", _source_bindings, _owners(), False,
        description="memory 的原文绑定列表")
    add("source.memory.open", _source_memory_open, _owners(), False,
        description="按 memory 动态打开其绑定的原文区间（原文不复制进记忆）")
    add("handoff.write", _handoff_write, {"jiaming"}, True,
        description="周家明写给另一入口的交接便签（72h 过期不删除）")
    add("handoff.latest", _handoff_latest, _owners(), False,
        description="最新便签（过期标注 expired）")
    add("plan.create", _plan_create, _owners(), True,
        description="创建计划（唯一真源，记忆经链接引用）")
    add("plan.update", _plan_update, _owners(), True,
        description="修改计划（expected_version 乐观锁）")
    add("plan.list", _plan_list, _owners(), False, description="列出计划")
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
    add("memory.deletion.request", _del_request, {"qiaosheng"}, True,
        description="提交删除申请（reason 必填；daily=10/lifetime=5 与旧系统一致）")
    add("memory.deletion.withdraw", _del_withdraw, _owners(), True,
        description="撤回 pending 删除申请")
    add("memory.deletion.decide", _del_decide, {"jiaming"}, True,
        description="审批删除申请（仅周家明；approve 执行物理删除——CB-055：archive 已退役，描述与 v2.0 语义对齐）")
    add("memory.deletion.get", _del_get, _owners(), False,
        description="读删除申请（含人类/拒绝理由；桶删除后仍可查）")
    add("memory.deletion.list", _del_list, _owners(), False,
        description="删除申请列表")
    add("memory.tags.add", _tags_add, _owners(), True,
        description="加标签（情绪标签 whose 必填）")
    add("memory.by_emotion", _by_emotion, _owners(), False,
        description="按情绪查（结构化入口，遗忘桶仍可查）")
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
    add("memory.relations.list", _rel_list, _owners(), False, description="列出关联")
    # ===== 规格 v2.0：纠错 + 直删 + 反查路由（§5.4/§6.2/§7.1）=====
    add("memory.relations.correct", _rel_correct, _owners(), True,
        description="纠错桶间关系（binding_error；remove/replace_wrong_binding）")
    add("i.item.relations.correct", _i_rel_correct, {"jiaming"}, True,
        description="纠错 I 修订与桶的错误关系（不改 I 正文）")
    add("source.binding.correct", _source_bind_correct, _owners(), True,
        description="纠错原文区间绑定（remove/replace_wrong_binding）")
    add("plan.memory.correct", _plan_link_correct, _owners(), True,
        description="纠错 Plan↔Memory 成员链接（Plan 状态变化不解绑）")
    add("memory.our_words.source.correct", _word_source_correct, _owners(), True,
        description="纠错话语来源引用（旧证据/指纹随之失效）")
    add("memory.delete", _memory_delete, {"jiaming"}, True,
        description="周家明直删（无申请/理由/配额；有效关系一律挡住）")
    add("relations.list", _relations_list, _owners(), False,
        description="跨域有效关系正/反查（方向语义不颠倒）")
    add("relations.trace", _relations_trace, _owners(), False,
        description="沿关系继续追链（有界、披露截断）")
    add("relations.corrections.list", _corrections_list, _owners(), False,
        description="关系纠错历史（只读；目标已删返回原始身份）")
    add("memory.relations.trace", _rel_trace, _owners(), False,
        description="沿 continuation_of 追事件链")
    add("maintenance.outbox.drain", _outbox_drain, _maintainers(), True,
        description="消费 outbox（至少一次+幂等标记）")
    add("maintenance.outbox.status", _outbox_status, _maintainers(), False,
        description="outbox 待处理统计")
    add("maintenance.activity.list", _activity_list, _maintainers(), False,
        description="审计查询（管理接口，不参与召回）")
    add("media.upload.prepare", _media_prepare, _owners(), True,
        description="申请上传 token（字节走专用 HTTP 端点，不进工具参数）")
    add("media.upload.finalize", _media_finalize, _owners(), True, True,
        description="完成上传（hash 去重）")
    add("media.list", _media_list, _owners(), False, description="媒体对象列表")
    add("media.get", _media_get_meta, _owners(), False, description="媒体元数据")
    add("memory.list", _memory_list, _owners(), False,
        description="记忆倒序列表（遗忘桶只给摘要表示）")
    add("memory.by_date", _by_date, _owners(), False, description="按事件日期查")
    add("memory.by_tag", _by_tag, _owners(), False, description="按标签查（结构化入口）")
    add("maintenance.rebuild_index", _rebuild_index, _maintainers(), True,
        description="按当前版本重建全部派生索引（旧投影+分字段+words+source）")
    add("maintenance.source.cleanup", _source_cleanup, _maintainers(), True,
        description="清理 Source 暂存残留（过期 staging/part 文件）；"
                    "返回清理计数")
    add("maintenance.semantic.warmup", _semantic_warmup, _maintainers(), True,
        description="全量预热语义向量（冷启动/重建后一次；查询路径仅限流补算）")
    add("presence.status", _presence_status, _everyone(), False,
        description="当前活动状态（三时间线概览）")
    add("maintenance.jobs.status", _jobs_status, _maintainers(), False,
        description="维护任务状态总览")
    add("maintenance.settings.get", _settings_get, _maintainers(), False,
        description="只读配置快照（时区/开窗/遗忘/provider 状态）")
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
    # 召回运行时能力例外（v1.4 §9.4）：不走 formal 幂等（不把候选包长期
    # 缓存进正式库）；operation_id 幂等由 runtime 库 recall_operation_keys
    # 承担，且重放前重检权限与版本。
    # 审计 F10：读取能力（write=False）不做长期幂等响应缓存——读取无
    # 副作用、天然可重复执行；缓存完整响应会让 I current 读取在修订/
    # rollback 后重放旧正文。读请求携带的 key 被忽略，每次按当前状态
    # 现算。
    if (idempotency_key and not _is_recall_runtime(capability)
            and cap.write):
        return _idempotent_invoke(principal, cap, arguments, idempotency_key)
    return {"ok": True, "data": cap.handler(principal, arguments)}


#: 召回运行时能力集：runtime 幂等隔离（§9.4）
_RECALL_RUNTIME_CAPS = frozenset({
    "memory.recall.start", "memory.recall.refine", "memory.recall.reject",
    "memory.recall.accept", "memory.recall.navigate", "memory.recall.status",
    "memory.recall.close", "memory.recall.round2", "memory.words.recall",
    "memory.words.get", "memory.context.validate", "memory.find_words",
})


def _is_recall_runtime(capability: str) -> bool:
    return capability in _RECALL_RUNTIME_CAPS


def _payload_hash(arguments: dict) -> str:
    return memory.canonical_hash(arguments)


_IDEMPOTENCY_STALE_SECONDS = 60


def _transport_key(key: str) -> str:
    """RA-004（2026-10-02 复审 P1）：transport 幂等键统一无歧义编码。
    领域层 operation 键为 op:<key>；此前本层存裸字符串，客户端
    transport="op:k" 会与 body operation_id=k 的领域记录碰撞。两类
    键分别固定 t:/op: 前缀，任何输入字符串都不产生跨层相等。"""
    return f"t:{key}"


def _claim_idempotency(principal_id: str, capability: str, key: str,
                       payload_hash: str) -> bool:
    """认领语义：新记录插入即占位；对账后 failed 的记录可被原子转移回
    running 重新执行；completed/running 一律不占。"""
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO idempotency_records(principal_id,"
                " capability, idempotency_key, payload_hash, status,"
                " result_ref, created_at)"
                " VALUES(?,?,?,?, 'running', NULL, datetime('now'))"
                " ON CONFLICT(principal_id, capability, idempotency_key)"
                " DO UPDATE SET status='running',"
                " payload_hash=excluded.payload_hash, result_ref=NULL,"
                " created_at=excluded.created_at"
                " WHERE idempotency_records.status='failed'",
                (principal_id, capability, key, payload_hash))
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _read_idempotency(principal_id: str, capability: str, key: str):
    with db.formal() as conn:
        return conn.execute(
            "SELECT * FROM idempotency_records WHERE principal_id=? AND"
            " capability=? AND idempotency_key=?",
            (principal_id, capability, key)).fetchone()


def _idempotency_stale(row) -> bool:
    from datetime import datetime as _dt, timezone as _tz
    try:
        created = _dt.fromisoformat(row["created_at"])
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=_tz.utc)
    return (_dt.now(_tz.utc) - created).total_seconds() > _IDEMPOTENCY_STALE_SECONDS


def _idempotent_invoke(principal: Principal, cap: Capability, arguments: dict,
                       key: str) -> dict:
    """原子 claim 幂等：先 INSERT 占位（status=running），占位成功者才执行副作用。

    并发同 key：仅一方能占位；另一方有界等待后读终态重放。
    崩溃窗口（§13.2）：running 残留超过阈值时不盲删盲重放——副作用是否
    已发生不明，返回 OUTCOME_UNKNOWN，由显式对账（maintenance.idempotency.
    reconcile）核实业务结果后才能放行重试。
    """
    ph = _payload_hash(arguments)
    tkey = _transport_key(key)
    if not _claim_idempotency(principal.principal_id, cap.name, tkey, ph):
        replay = _await_completion(principal, cap, tkey, ph, arguments)
        if replay is not None:
            return {"ok": True, "data": replay, "idempotent_replay": True}

    try:
        result = cap.handler(principal, arguments)
    except Exception:
        # CB-008：handler 异常（业务拒绝 MariposaError 或未预期错误）
        # 一律把本层占位置 failed 再抛——副作用在各服务事务内已回滚，
        # 残留 running 会把干净失败伪装成 OUTCOME_UNKNOWN 逼人工对账；
        # 真正的进程崩溃不经此处，由 stale→OUTCOME_UNKNOWN 兜底。
        with db.formal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE idempotency_records SET status='failed'"
                    " WHERE principal_id=? AND capability=? AND"
                    " idempotency_key=? AND status='running'",
                    (principal.principal_id, cap.name, tkey))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        raise
    with db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE idempotency_records SET status='completed', result_ref=?"
                " WHERE principal_id=? AND capability=? AND idempotency_key=?"
                " AND status='running'",
                (json.dumps(result, ensure_ascii=False), principal.principal_id,
                 cap.name, tkey))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"ok": True, "data": result}


def _revalidate_replayed_response(principal: Principal, capability: str,
                                  replay: dict) -> None:
    """CB-016（2026-10-02 审计 P1）：幂等缓存的重放前资源重验。

    write 回执缓存的是完整响应——写幂等的"同 key 同结果"不等于
    "当前资源仍处于该状态"。memory.open 的缓存响应带完整正文与票据：
    memory 已更新/物理删除、票据行已消失或归属他人时，重放必须返回
    结构化 stale，不得重发旧正文或已失效票据（物理删除 ≠ 出站不可
    再取）。其他能力按需登记。
    """
    if capability != "memory.open":
        return
    mid = replay.get("memory_id")
    if not mid:
        return
    with db.formal() as conn:
        m = conn.execute(
            "SELECT visibility, current_version_no FROM memories"
            " WHERE memory_id=?", (mid,)).fetchone()
        rid = replay.get("view_receipt")
        r = (conn.execute(
            "SELECT principal_id, binding_id FROM memory_view_receipts"
            " WHERE receipt_id=?", (rid,)).fetchone() if rid else None)
    if m is None or m["visibility"] != "active":
        raise StaleOperation(
            "memory 已删除/不可见，旧 open 响应拒绝重放", memory_id=mid)
    if str(replay.get("version")) != str(m["current_version_no"]):
        raise StaleOperation(
            "memory 已更新到新版本，旧 open 响应拒绝重放",
            memory_id=mid, current_version=m["current_version_no"])
    if (r is None or r["principal_id"] != principal.principal_id
            or r["binding_id"] != principal.binding_id):
        raise StaleOperation(
            "查看票据已失效或不属于当前身份，旧 open 响应拒绝重放",
            memory_id=mid)


#: 走 relations.corrections.atomic_write 的能力（领域回执键
#: op:<operation_id>，与业务副作用同事务）——F23 崩溃窗口恢复面
_DOMAIN_IDEMPOTENT_CAPS = frozenset({
    "memory.relations.correct", "i.item.relations.correct",
    "source.binding.correct"})


def _recover_transport_from_domain(principal: Principal, cap: Capability,
                                   tkey: str, arguments: dict):
    """t 层 running 残留时按领域回执恢复外层（F23）。

    返回 (verdict, result)：completed=业务已落地（外层补 completed
    并重放领域结果）；not_executed=领域零痕迹（外层转 failed 放行
    重试）；None=不可判定（body 无 operation_id 或领域侧同在
    running）。不放松未知结果保护——非 atomic_write 能力不走此路。
    """
    op = arguments.get("operation_id")
    if not isinstance(op, str) or not op:
        return None, None
    with db.formal() as conn:
        row = conn.execute(
            "SELECT status, result_ref FROM idempotency_records WHERE"
            " principal_id=? AND capability=? AND idempotency_key=?",
            (principal.principal_id, cap.name, f"op:{op}")).fetchone()
        if row is not None and row["status"] == "completed":
            try:
                result = json.loads(row["result_ref"])
            except (ValueError, TypeError):
                return None, None
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE idempotency_records SET status='completed',"
                    " result_ref=? WHERE principal_id=? AND capability=?"
                    " AND idempotency_key=? AND status='running'",
                    (json.dumps(result, ensure_ascii=False),
                     principal.principal_id, cap.name, tkey))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return "completed", result
        if row is None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE idempotency_records SET status='failed',"
                    " result_ref=NULL WHERE principal_id=? AND"
                    " capability=? AND idempotency_key=? AND"
                    " status='running'",
                    (principal.principal_id, cap.name, tkey))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return "not_executed", None
    return None, None


def _await_completion(principal: Principal, cap: Capability, key: str,
                      ph: str, arguments: dict | None = None) -> dict | None:
    """占位失败方：等待占位方终态。

    返回 dict = 已完成（调用方按幂等重放返回）；None = 记录消失或已由
    本方重新认领（调用方继续执行 handler）。异常：同键异内容冲突 /
    崩溃窗口 OUTCOME_UNKNOWN / 仍在进行 Busy。
    """
    import time as _time
    for _ in range(40):  # <=8s
        _time.sleep(0.2)
        row = _read_idempotency(principal.principal_id, cap.name, key)
        if row is None:
            break  # 记录消失，可重试 claim
        if row["payload_hash"] != ph:
            raise IdempotencyConflict(
                "same key with different payload",
                capability=cap.name, key=key)
        if row["status"] == "completed":
            replay = json.loads(row["result_ref"])
            # CB-016：重放的完整响应先过资源重验，失效结构化拒绝
            _revalidate_replayed_response(principal, cap.name, replay)
            return replay
    row = _read_idempotency(principal.principal_id, cap.name, key)
    if row is not None and row["status"] == "running":
        if _idempotency_stale(row):
            # F23（2026-10-03 审计 P2）：atomic_write 能力的崩溃窗口按
            # 领域回执恢复外层——领域记录与业务副作用同事务，completed
            # 即已落地（补写外层并重放同一结果），无记录即零痕迹（转
            # failed 放行重试）；不可判定才维持 OUTCOME_UNKNOWN
            if cap.name in _DOMAIN_IDEMPOTENT_CAPS:
                verdict, recovered = _recover_transport_from_domain(
                    principal, cap, key, arguments or {})
                if verdict == "completed":
                    return recovered
                if verdict == "not_executed":
                    if _claim_idempotency(principal.principal_id, cap.name,
                                          key, ph):
                        return None
            # 疑似崩溃残留：不盲目重放副作用（B02 崩溃窗口）
            raise OutcomeUnknown(
                "idempotent execution likely crashed mid-flight; "
                "verify business outcome and reconcile before retrying",
                capability=cap.name, key=key)
        raise Busy()
    if not _claim_idempotency(principal.principal_id, cap.name, key, ph):
        raise Busy()
    return None





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
        original_title=a.get("original_title"),
        categories=a.get("categories"),
        plan_ids=a.get("plan_ids"),
        mood=a.get("mood"),
        our_words=a.get("our_words"),
        creation_mode=a.get("creation_mode"),
        occurred_start=a.get("occurred_start"),
        occurred_end=a.get("occurred_end"),
    )


def _open(principal: Principal, a: dict) -> dict:
    from ..memory import views as views_mod
    return views_mod.open_memory(principal, str(a.get("memory_id", "")))


def _view_confirm(principal: Principal, a: dict) -> dict:
    from ..memory import views as views_mod
    return views_mod.confirm_view(
        principal, str(a.get("memory_id", "")),
        str(a.get("receipt_id", "")), a.get("confirm_key"))


def _recollect_append(principal: Principal, a: dict) -> dict:
    from ..memory import recollections as rec_mod
    return rec_mod.append(principal, str(a.get("memory_id", "")),
                          str(a.get("receipt_id", "")),
                          str(a.get("text", "")),
                          keep_wide=bool(a.get("keep_wide", False)))


def _recollect_revise(principal: Principal, a: dict) -> dict:
    from ..memory import recollections as rec_mod
    return rec_mod.revise(principal, str(a.get("recollection_id", "")),
                          str(a.get("text", "")))


def _recollect_list(principal: Principal, a: dict) -> dict:
    from ..memory import recollections as rec_mod
    return {"recollections": rec_mod.list_for(
        str(a.get("memory_id", "")), bool(a.get("include_history", False)))}


def _our_words_append(principal: Principal, a: dict) -> dict:
    from ..memory import our_words as ow_mod
    return ow_mod.append(principal.principal_id, str(a.get("memory_id", "")),
                         a.get("words") or [])


def _our_words_list(principal: Principal, a: dict) -> dict:
    from ..memory import our_words as ow_mod
    return {"words": ow_mod.list_for(str(a.get("memory_id", "")))}


def _categories_replace(principal: Principal, a: dict) -> dict:
    # v1.7：分类替换不再触发遗忘到期重算（retention 已退役）；
    # 阶段由查询时按分类 H 现算（P2 接 phase_policy）。
    from ..memory import categories as cats_mod
    from .. import db as _db
    with _db.formal() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?",
                                (str(a.get("memory_id", "")),)).fetchone():
                raise NotFound("memory not found",
                               memory_id=a.get("memory_id"))
            cats_mod.replace(conn, str(a.get("memory_id", "")),
                             a.get("categories") or [],
                             principal.principal_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"memory_id": a.get("memory_id"),
            "categories": a.get("categories")}


def _i_get(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.get()


def _i_write(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.write(principal.principal_id, str(a.get("content", "")),
                       a.get("expected_version"))


def _i_versions(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return {"versions": i_svc.versions_read()}


def _i_items_list(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return {"items": i_svc.items_list()}


def _i_item_get(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.item_get(str(a.get("item_id", "")))


def _i_item_create(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.item_create(
        principal.principal_id, str(a.get("content", "")),
        a.get("change_reason"), a.get("relations"))


def _i_item_revise(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.item_revise(
        principal.principal_id, str(a.get("item_id", "")),
        str(a.get("content", "")), int(a.get("expected_revision")),
        a.get("change_reason"), a.get("informed_by_revision"),
        a.get("relations"))


def _i_item_restore(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.item_restore(
        principal.principal_id, str(a.get("item_id", "")),
        int(a.get("restore_revision")), int(a.get("expected_revision")),
        a.get("change_reason"), a.get("relations"))


def _i_item_history(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.item_history(str(a.get("item_id", "")))


def _i_suggest(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return i_svc.suggest(principal.principal_id, str(a.get("content", "")))


def _i_suggestions(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    return {"suggestions": i_svc.suggestions_list(a.get("status"))}


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


def _recall(principal: Principal, a: dict) -> dict:
    with db.formal() as conn:
        return retrieval_search.recall(
            conn, str(a.get("query", "")), a.get("filters") or {},
            int(a.get("limit", 20)), a.get("cursor"))


# ---------- 召回运行时 handlers（runtime 幂等：operation_id） ----------

def _with_operation_id(principal: Principal, a: dict, fn) -> dict:
    """RUNTIME-02：同 operation_id 重试不重复建 session/扣预算/排除候选。

    commit-at-end 模型：不写任何中间态；已完成 operation 直接重放
    （重放经 recall 域 guard 按当前 session/版本/可见性/phase 重校验，
    不原样返回旧正文——审计 F07）；未完成的同 key 重试从头重新计算。
    payload_hash 区分同 key 异请求。设为型动作（reject/accept/navigate/
    close）由 run_operation 在执行成功后补写响应记录（重复执行结果
    不变，崩溃窗口重放安全）。
    """
    op = a.get("operation_id")
    if not op:
        return fn(principal, a)
    key = f"{fn.__name__}:{a.get('session_id', 'new')}:{op}"
    ph = memory.canonical_hash(a)
    ctx = {"principal_id": principal.principal_id, "operation_key": key,
           "payload_hash": ph}
    return recall_store.run_operation(
        principal.principal_id, key,
        lambda: fn(principal, a, op_ctx=ctx),
        payload_hash=ph,
        replay_guard=lambda saved: recall_service.revalidate_replayed(
            fn.__name__, saved, a))


def _recall_start(principal: Principal, a: dict) -> dict:
    return _with_operation_id(principal, a, recall_service.start)


def _recall_refine(principal: Principal, a: dict) -> dict:
    return _with_operation_id(principal, a, recall_service.refine)


def _recall_reject(principal: Principal, a: dict) -> dict:
    return _with_operation_id(principal, a, recall_service.reject)


def _recall_accept(principal: Principal, a: dict) -> dict:
    return _with_operation_id(principal, a, recall_service.accept)


def _recall_navigate(principal: Principal, a: dict) -> dict:
    return _with_operation_id(principal, a, recall_service.navigate)


def _mood_write(principal: Principal, a: dict) -> dict:
    return memory.mood_write(
        principal.principal_id, str(a.get("memory_id", "")),
        a.get("note"), a.get("tags"))


def _recall_status(principal: Principal, a: dict) -> dict:
    return recall_service.status(principal, a)


def _recall_close(principal: Principal, a: dict) -> dict:
    return _with_operation_id(principal, a, recall_service.close)


def _words_recall(principal: Principal, a: dict) -> dict:
    # 复审#5：operation_id 幂等（runtime 集；重放按专项 intent 重校验）
    return _with_operation_id(principal, a, recall_service.words_recall)


def _words_get(principal: Principal, a: dict) -> dict:
    return words_mod.get_word(str(a.get("word_id", "")))


def _context_validate(principal: Principal, a: dict) -> dict:
    return recall_service.validate_context(principal, a)


def _idem_reconcile(principal: Principal, a: dict) -> dict:
    return maintenance.idempotency_reconcile(
        principal.principal_id,
        str(a.get("record_principal", principal.principal_id)),
        str(a.get("capability", "")),
        _transport_key(str(a.get("idempotency_key", ""))),
        int(a.get("stale_seconds", 60)))


def _source_cleanup(principal: Principal, a: dict) -> dict:
    from ..source import archive as src_archive
    hours = int(a.get("max_age_hours", 48))
    return {"removed": src_archive.cleanup_staging(hours),
            "max_age_hours": hours}


def _find_words(principal: Principal, a: dict) -> dict:
    """S08/WP05：find_words 与 words.recall 同一统一入口——session 化
    （预算/回执/同一层 Jev/≤3 交付），不另写一套 pipeline。
    全量审计 P1-05：与 words.recall 同样走 operation_id 幂等（此前
    完全绕开 _with_operation_id——响应丢失后的重试会另建 session
    重新检索重新 Jev）。"""
    return _with_operation_id(principal, a, _find_words_core)


def _find_words_core(principal: Principal, a: dict,
                     op_ctx: dict | None = None) -> dict:
    from .. import config as _cfg
    from ..errors import Forbidden as _FW
    if not _cfg.RECALL_WORDS_ENABLED:
        raise _FW("find_words 受 MARIPOSA_WORDS_RECALL_ENABLED 控制（默认关）",
                  code="WORDS_CHANNEL_DISABLED")
    plan = dict(a.get("query_plan") or {})
    # 三轮复审#6：只传 query 的调用以 query 回填 original_request
    #（此前留空 → validate_query_plan 报"original_request 必填"）
    plan.setdefault("original_request",
                    a.get("original_request") or a.get("query") or "")
    # RA-014（2026-10-02 复审 P2）：顶层 lexical_terms 是字符串数组，
    # 直接使用（此前被包成嵌套列表致 INVALID_ARGUMENT）
    if not plan.get("lexical_terms"):
        top_terms = a.get("lexical_terms")
        if isinstance(top_terms, list) and top_terms:
            plan["lexical_terms"] = [str(t) for t in top_terms]
        else:
            plan["lexical_terms"] = [a.get("query", "") or
                                     a.get("original_request", "")]
    # CB-047（2026-10-02 审计 P2）：schema 接受的顶层参数全部进入统一
    # plan——query_plan 内显式字段优先，顶层仅回填缺失（此前
    # exact_phrases/semantic_query/正负 constraints 被静默丢弃，
    # 用户的明确过滤不生效）
    if not plan.get("exact_phrases") and a.get("exact_phrases"):
        plan["exact_phrases"] = a["exact_phrases"]
    if not plan.get("semantic_query") and a.get("semantic_query"):
        plan["semantic_query"] = a["semantic_query"]
    if not plan.get("delivery_limit") and a.get("limit"):
        plan["delivery_limit"] = a["limit"]
    ec = dict(plan.get("explicit_constraints") or {})
    for k in ("categories", "mood_tags", "event_date", "source_date",
              "speaker", "category_match", "mood_match"):
        if k in (a.get("explicit_constraints") or {}) and k not in ec:
            ec[k] = a["explicit_constraints"][k]
    if ec:
        plan["explicit_constraints"] = ec
    neg = dict(plan.get("explicit_negative_constraints") or {})
    for k in ("event_date_excluded", "speaker_excluded",
              "source_date_excluded"):
        if k in (a.get("explicit_negative_constraints") or {}) \
                and k not in neg:
            neg[k] = a["explicit_negative_constraints"][k]
    if neg:
        plan["explicit_negative_constraints"] = neg
    plan["channels"] = ["words"]
    return recall_service.start(principal, {"query_plan": plan},
                                op_ctx=op_ctx)


def _recall_round2(principal: Principal, a: dict) -> dict:
    """S13/WP04：服务端 plan + 六条件门禁 + raw 候选过同一层 Jev +
    commit-at-end 单事务；operation_id 幂等（runtime 集，不走 formal
    响应缓存）。"""
    return _with_operation_id(principal, a, recall_service.round2)


def _keep_revoke(principal: Principal, a: dict) -> dict:
    from ..memory import keep as keep_mod
    return keep_mod.revoke(principal, str(a.get("mark_id", "")))


def _keeps_list(principal: Principal, a: dict) -> dict:
    from ..memory import keep as keep_mod
    return {"marks": keep_mod.marks_of(str(a.get("memory_id", ""))),
            "active_keepers": keep_mod.active_keepers(
                str(a.get("memory_id", "")))}





# ===== Source Layer handlers =====

def _source_import(principal: Principal, a: dict) -> dict:
    path = str(a.get("path", "")).strip()
    if not path:
        raise Forbidden("path required（宿主本地文件路径或上传返回的路径）",
                        code="SCHEMA_VIOLATION", capability="source.import")
    return source_importer.import_file(
        principal.principal_id, path, a.get("filename"))


def _source_import_status(principal: Principal, a: dict) -> dict:
    return source_importer.batch_status(str(a.get("batch_id", "")))


def _source_import_batches(principal: Principal, a: dict) -> dict:
    return {"batches": source_importer.batches_list(int(a.get("limit", 50)))}


def _source_search(principal: Principal, a: dict) -> dict:
    senders = a.get("senders")
    return source_query.search(
        a.get("query") or None,
        senders=senders if isinstance(senders, list) else None,
        provider=a.get("provider"),
        conversation_id=a.get("conversation_id"),
        date_from=a.get("date_from"), date_to=a.get("date_to"),
        limit=int(a.get("limit", 20)), offset=int(a.get("offset", 0)))


def _source_message_get(principal: Principal, a: dict) -> dict:
    if not a.get("message_id") and not a.get("provider_message_id"):
        raise Forbidden("message_id 或 provider_message_id 必填",
                        code="SCHEMA_VIOLATION",
                        capability="source.message.get")
    return source_query.get_message(
        message_id=a.get("message_id"),
        provider_message_id=a.get("provider_message_id"),
        context=int(a.get("context", 5)),
        include_content=bool(a.get("include_content")),
        provider=a.get("provider"))


def _source_range_open(principal: Principal, a: dict) -> dict:
    return source_query.open_range(
        str(a.get("conversation_id", "")),
        str(a.get("start_message_id", "")),
        str(a.get("end_message_id", "")),
        start_char_offset=a.get("start_char_offset"),
        end_char_offset=a.get("end_char_offset"),
        include_content=bool(a.get("include_content")))


def _source_conversation_get(principal: Principal, a: dict) -> dict:
    return source_query.get_conversation(
        str(a.get("conversation_id", "")),
        after_seq=a.get("after_cursor", a.get("after_seq")),
        before_seq=a.get("before_cursor", a.get("before_seq")),
        after_id=(a.get("after_cursor") or {}).get("id")
        if isinstance(a.get("after_cursor"), dict) else None,
        before_id=(a.get("before_cursor") or {}).get("id")
        if isinstance(a.get("before_cursor"), dict) else None,
        around_seq=a.get("around_seq"), limit=int(a.get("limit", 100)))


def _source_conversations_list(principal: Principal, a: dict) -> dict:
    return source_query.conversations_list(
        int(a.get("limit", 50)), int(a.get("offset", 0)), a.get("provider"))


def _source_bind(principal: Principal, a: dict) -> dict:
    return source_binding.bind(
        principal.principal_id, str(a.get("memory_id", "")),
        str(a.get("conversation_id", "")),
        str(a.get("start_message_id", "")),
        str(a.get("end_message_id", "")),
        start_char_offset=a.get("start_char_offset"),
        end_char_offset=a.get("end_char_offset"),
        confidence=str(a.get("confidence", "exact")))


def _source_bindings(principal: Principal, a: dict) -> dict:
    return {"bindings": source_binding.ranges_of(str(a.get("memory_id", "")))}





def _source_memory_open(principal: Principal, a: dict) -> dict:
    return source_binding.open_for_memory(
        str(a.get("memory_id", "")),
        include_content=bool(a.get("include_content")))





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





def _reengage(principal: Principal, a: dict) -> dict:
    return reengagement.record(
        principal.principal_id, str(a.get("memory_id", "")),
        str(a.get("evidence_kind", "")), str(a.get("occurred_at", "")),
        a.get("evidence_ref"))


def _binding_revoke(principal: Principal, a: dict) -> dict:
    return identity.revoke_binding(principal.principal_id,
                                   str(a.get("binding_id", "")))


def _bootstrap(principal: Principal, a: dict) -> dict:
    # RA-007（2026-10-02 复审 P2）：known_snapshot_id 是旧公开参数——
    # 明确映射为 loaded_snapshot_id（去重语义）；两者同时携带且不一致
    # 结构化拒绝，不再静默忽略
    loaded = a.get("loaded_snapshot_id")
    known = a.get("known_snapshot_id")
    if known and not loaded:
        loaded = known
    if known and loaded and known != loaded:
        raise Forbidden(
            "known_snapshot_id 与 loaded_snapshot_id 不一致",
            code="INVALID_ARGUMENT")
    return bootstrap.get(principal.principal_id, principal.entry_source,
                         str(a.get("profile", "")),
                         loaded, a.get("cursor"))


def _bootstrap_next(principal: Principal, a: dict) -> dict:
    # CB-045：section 必填（schema 枚举 memory_days/plans）——raw 已
    # 退役不再是默认段
    return bootstrap.next_page(principal.principal_id, principal.entry_source,
                               str(a.get("snapshot_id", "")),
                               a.get("cursor") or {},
                               str(a.get("section", "memory_days")))


def _time_now(principal: Principal, a: dict) -> dict:
    return time_ctx.now()


def _time_ctx(principal: Principal, a: dict) -> dict:
    return time_ctx.context(principal.principal_id)


def _time_since(principal: Principal, a: dict) -> dict:
    return time_ctx.since(principal.principal_id)


def _presence_touch(principal: Principal, a: dict) -> dict:
    return time_ctx.presence_touch(principal.principal_id, principal.kind)


def _del_request(principal: Principal, a: dict) -> dict:
    # v2.0：人类申请仅 qiaosheng、Memory-only（action/resource_kind 退役）
    return deletion.deletion_request(
        principal.principal_id, str(a.get("memory_id", "")),
        str(a.get("reason", "")),
        operation_key=str(a.get("operation_id") or ""))


def _del_withdraw(principal: Principal, a: dict) -> dict:
    return deletion.deletion_withdraw(principal.principal_id,
                                      str(a.get("request_id", "")))


def _del_decide(principal: Principal, a: dict) -> dict:
    # v2.0：reject 必填拒绝理由（rejection_reason）；approve 无理由
    return deletion.deletion_decide(
        principal.principal_id, str(a.get("request_id", "")),
        str(a.get("decision", "")),
        rejection_reason=a.get("rejection_reason"))


def _del_list(principal: Principal, a: dict) -> dict:
    return {"requests": deletion.deletion_list(
        a.get("status"), memory_id=a.get("memory_id"))}


def _del_get(principal: Principal, a: dict) -> dict:
    return deletion.deletion_get(str(a.get("request_id", "")))





def _tags_add(principal: Principal, a: dict) -> dict:
    tags = a.get("tags") or []
    if not isinstance(tags, list):
        raise Forbidden("tags must be a list")
    return listing.tags_add(principal.principal_id,
                            str(a.get("memory_id", "")), tags)


def _by_emotion(principal: Principal, a: dict) -> dict:
    return listing.by_emotion(str(a.get("tag", "")),
                              a.get("whose") or None)





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





def _rel_list(principal: Principal, a: dict) -> dict:
    return {"relations": relations.list_for(str(a.get("memory_id", "")),
                                            str(a.get("direction", "both")))}


def _rel_trace(principal: Principal, a: dict) -> dict:
    return relations.trace(str(a.get("memory_id", "")),
                           int(a.get("max_depth", 5)))







def _rel_correct(principal: Principal, a: dict) -> dict:
    from ..relations.corrections import atomic_write
    op = str(a.get("operation_id", ""))
    if not op:
        raise Forbidden("operation_id 必填（纠错为有状态写）",
                        code="SCHEMA_VIOLATION")
    return atomic_write(
        principal.principal_id, "memory.relations.correct", op,
        {"relation_id": a.get("relation_id"),
         "correction_action": a.get("correction_action"),
         "replacement": a.get("replacement"), "note": a.get("note")},
        lambda conn: relations_mod.correct(
            principal.principal_id, str(a.get("relation_id", "")),
            str(a.get("correction_action", "")),
            note=a.get("note"), replacement=a.get("replacement"),
            conn=conn))


def _i_rel_correct(principal: Principal, a: dict) -> dict:
    from ..identity_i import service as i_svc
    from ..relations.corrections import atomic_write
    op = str(a.get("operation_id", ""))
    if not op:
        raise Forbidden("operation_id 必填", code="SCHEMA_VIOLATION")
    return atomic_write(
        principal.principal_id, "i.item.relations.correct", op,
        {"relation_id": a.get("relation_id"),
         "correction_action": a.get("correction_action"),
         "replacement": a.get("replacement"), "note": a.get("note")},
        lambda conn: i_svc.correct_item_relation(
            principal.principal_id, str(a.get("relation_id", "")),
            str(a.get("correction_action", "")),
            note=a.get("note"), replacement=a.get("replacement"),
            conn=conn))


def _source_bind_correct(principal: Principal, a: dict) -> dict:
    from ..relations.corrections import atomic_write
    op = str(a.get("operation_id", ""))
    if not op:
        raise Forbidden("operation_id 必填", code="SCHEMA_VIOLATION")
    return atomic_write(
        principal.principal_id, "source.binding.correct", op,
        {"binding_id": a.get("binding_id"),
         "correction_action": a.get("correction_action"),
         "replacement": a.get("replacement"), "note": a.get("note")},
        lambda conn: source_binding.correct(
            principal.principal_id, str(a.get("binding_id", "")),
            str(a.get("correction_action", "")),
            note=a.get("note"), replacement=a.get("replacement"),
            conn=conn))


def _plan_link_correct(principal: Principal, a: dict) -> dict:
    from ..plans import service as plans_svc
    from ..relations.corrections import atomic_write
    op = str(a.get("operation_id", ""))
    if not op:
        raise Forbidden("operation_id 必填", code="SCHEMA_VIOLATION")
    return atomic_write(
        principal.principal_id, "plan.memory.correct", op,
        {"link_id": a.get("link_id"), "correction_action":
         a.get("correction_action"), "replacement": a.get("replacement"),
         "note": a.get("note")},
        lambda conn: plans_svc.correct_memory_link(
            principal.principal_id, str(a.get("link_id", "")),
            str(a.get("correction_action", "")),
            note=a.get("note"), replacement=a.get("replacement"),
            conn=conn))


def _word_source_correct(principal: Principal, a: dict) -> dict:
    from ..memory import our_words as words_mod
    from ..relations.corrections import atomic_write
    op = str(a.get("operation_id", ""))
    if not op:
        raise Forbidden("operation_id 必填", code="SCHEMA_VIOLATION")
    if a.get("expected_source_version") is None:
        raise Forbidden("expected_source_version 必填（CB-007 来源绑定"
                        "换代计数）", code="SCHEMA_VIOLATION")
    return atomic_write(
        principal.principal_id, "memory.our_words.source.correct", op,
        {"word_id": a.get("word_id"),
         "expected_source_ref": a.get("expected_source_ref"),
         "expected_source_version": a.get("expected_source_version"),
         "correction_action": a.get("correction_action"),
         "replacement": a.get("replacement"), "note": a.get("note")},
        lambda conn: words_mod.correct_source(
            principal.principal_id, str(a.get("word_id", "")),
            a.get("expected_source_ref"),
            str(a.get("correction_action", "")),
            replacement=a.get("replacement"), note=a.get("note"),
            conn=conn,
            expected_source_version=int(a["expected_source_version"])))


def _memory_delete(principal: Principal, a: dict) -> dict:
    from ..deletion import service as deletion_svc
    from ..relations.corrections import atomic_write
    op = str(a.get("operation_id", ""))
    if not op:
        raise Forbidden("operation_id 必填（技术幂等，非审批）",
                        code="SCHEMA_VIOLATION")
    return atomic_write(
        principal.principal_id, "memory.delete", op,
        {"memory_id": a.get("memory_id")},
        lambda conn: deletion_svc.direct_delete(
            principal.principal_id, str(a.get("memory_id", "")), conn=conn))


def _relations_list(principal: Principal, a: dict) -> dict:
    from .. import relations as relations_hub
    return relations_hub.list_relations(a)


def _relations_trace(principal: Principal, a: dict) -> dict:
    from .. import relations as relations_hub
    return relations_hub.trace_relations(a)


def _corrections_list(principal: Principal, a: dict) -> dict:
    from ..relations.corrections import list_corrections
    return list_corrections(
        endpoint=a.get("endpoint"), instance_id=a.get("instance_id"),
        domain=a.get("domain"), limit=int(a.get("limit", 50)),
        offset=int(a.get("offset", 0)))

def _outbox_drain(principal: Principal, a: dict) -> dict:
    return maintenance.outbox_drain(int(a.get("limit", 100)))


def _outbox_status(principal: Principal, a: dict) -> dict:
    return maintenance.outbox_status()


def _activity_list(principal: Principal, a: dict) -> dict:
    return {"events": maintenance.activity_list(int(a.get("limit", 50)),
                                                a.get("event_type"))}





def _media_prepare(principal: Principal, a: dict) -> dict:
    return media.upload_prepare(principal.principal_id,
                                str(a.get("mime", "")), int(a.get("size", 0)))


def _media_finalize(principal: Principal, a: dict) -> dict:
    # P1-07：finalize 只收 token；字节已在 stage 端点落盘暂存，
    # data_b64 被 schema（additionalProperties=False）结构性拒绝
    return media.upload_finalize(principal.principal_id,
                                 str(a.get("upload_token", "")))


def _media_list(principal: Principal, a: dict) -> dict:
    return {"objects": media.list_media(int(a.get("limit", 50)))}


def _media_get_meta(principal: Principal, a: dict) -> dict:
    meta, _ = media.get_media(principal.principal_id,
                              str(a.get("content_hash", "")))
    return meta








def _memory_list(principal: Principal, a: dict) -> dict:
    # CB-049：复合游标（date+id）——cursor_date 单独传入时按旧式
    # 语义（该日期前全部），完整 next_cursor 原样透传
    cur = a.get("cursor")
    cursor_date = a.get("cursor_date")
    cursor_id = None
    if isinstance(cur, dict):
        cursor_date = cur.get("memory_date", cursor_date)
        cursor_id = cur.get("memory_id")
    return listing.list_memories(a.get("state"), int(a.get("limit", 50)),
                                 cursor_date, cursor_id)


def _by_date(principal: Principal, a: dict) -> dict:
    return listing.by_date(str(a.get("date", "")))


def _by_tag(principal: Principal, a: dict) -> dict:
    return listing.by_tag(str(a.get("namespace", "tag")), str(a.get("tag", "")),
                          a.get("whose"))





def _rebuild_index(principal: Principal, a: dict) -> dict:
    return rebuild_mod.rebuild_index(principal.principal_id)


def _semantic_warmup(principal: Principal, a: dict) -> dict:
    from ..retrieval import semantic as _sem
    with db.formal() as conn:
        return _sem.warmup(conn)





def _presence_status(principal: Principal, a: dict) -> dict:
    return time_ctx.context(principal.principal_id)


def _jobs_status(principal: Principal, a: dict) -> dict:
    return maintenance.jobs_status()


def _settings_get(principal: Principal, a: dict) -> dict:
    from .. import config as _cfg
    from ..bootstrap import service as _bs
    return {
        "relationship_timezone": _cfg.RELATIONSHIP_TIMEZONE,
        "http": {"bind": _cfg.HTTP_BIND, "port": _cfg.HTTP_PORT},
        "forgetting": {"status": "retired_v1_7"},
        "bootstrap": {
            "memory_days": _bs.BOOT_MEMORY_DAYS,
            "plan_upcoming_days": _bs.BOOT_UPCOMING_DAYS,
            "soft_token_budget": _bs.BOOT_SOFT_TOKEN_BUDGET,
        },
        "retrieval": {
            "projection_revision": _cfg.PROJECTION_REVISION,
            "semantic_provider": _cfg.SEMANTIC_PROVIDER or None,
            "semantic_status": "unavailable" if not _cfg.SEMANTIC_PROVIDER
                               else "configured",
        },
        "quote_semantic_auto_apply": getattr(
            _cfg, "QUOTE_SEMANTIC_AUTO_APPLY", None),
        # RA-010：calendar/quote 已退役——不再引用不存在常量
        "note": "只读快照；修改经部署参数（policy version 变更入审计）",
    }





def _st_tok(text: str) -> str:
    from ..retrieval import projection as _pj
    return _pj.normalize_search_text(text)





def _emotion_reserved(principal: Principal, a: dict) -> dict:
    return {"enabled": False, "status": "reserved",
            "note": "EMOTION_RETRIEVAL_ENABLED 默认 false；启用也只调整补充召回，"
                    "不篡改基础时间/原文/三天桶/计划",
            "config_key": "EMOTION_RETRIEVAL_ENABLED", "configured": False}


def _listening_reserved(principal: Principal, a: dict) -> dict:
    return {"status": "reserved", "provider": None,
            "note": "一起听歌未选供应商；不承诺第三方曲库；capability 标 reserved"}


REGISTRY = _register()

# 旧规格兼容层（plan.get 等薄实现）随 registry 一并装配——不依赖
# app 导入（单元测试/工具直用 REGISTRY 时同样可用；register 内部
# 幂等，app.py 的再次调用无害）
from . import v1_compat as _v1_compat  # noqa: E402
_v1_compat.register_v1_compat()


def list_capabilities(principal: Principal) -> list[dict]:
    return [
        {"name": c.name, "write": c.write, "description": c.description}
        for c in REGISTRY.values()
        if principal.principal_id in c.allowed_principals
    ]
