"""v1.1 最低能力契约（150 项）兼容层。

三类：
1. 别名——实现名与规格名不同但同一 handler；
2. blocked/reserved——外部依赖未就绪（chat/voice/group/wishstar/wakeup/listening/
   settings.update/sticker.send）：能力**存在**且如实返回状态，不静默缺失也不假实现；
3. 薄实现——单条查询/快捷动作等小 handler。

决策记录见 docs/DECISIONS.md（D13-D15）。
"""
from __future__ import annotations

from .. import db
from ..errors import Forbidden, NotFound
from .registry import REGISTRY, Capability, Principal


def _blocked(reason: str, unblock: str):
    def handler(principal: Principal, a: dict) -> dict:
        return {"status": "blocked", "reason": reason, "unblock": unblock}
    return handler


def _reserved(note: str):
    def handler(principal: Principal, a: dict) -> dict:
        return {"status": "reserved", "note": note}
    return handler


def _alias(spec_name: str):
    """规格名 -> 既有实现能力的别名。"""
    target = _ALIASES[spec_name]
    cap = REGISTRY[target]
    return Capability(spec_name, cap.handler, cap.allowed_principals, cap.write,
                      cap.idempotent, f"[规格别名] {cap.description}")


_ALIASES = {
    "activity.list": "maintenance.activity.list",
    "jobs.status": "maintenance.jobs.status",
    "presence.handoff.latest": "handoff.latest",
    "presence.handoff.write": "handoff.write",
    "letter.lock_update": "letter.edit",
    "settings.get": "maintenance.settings.get",
}

_BLOCKED_CAPS = {
    # chat.*：CC 依赖
    "chat.conversations.list": ("claude CLI 未安装", "安装官方 Claude Code CLI 并核验订阅"),
    "chat.conversations.create": ("claude CLI 未安装", "安装官方 Claude Code CLI 并核验订阅"),
    "chat.conversations.get": ("claude CLI 未安装", "安装官方 Claude Code CLI 并核验订阅"),
    "chat.conversations.messages": ("claude CLI 未安装", "安装官方 Claude Code CLI 并核验订阅"),
    "chat.messages.list": ("claude CLI 未安装", "安装官方 Claude Code CLI 并核验订阅"),
    "chat.send": ("claude CLI 未安装", "安装官方 Claude Code CLI 并核验订阅"),
    "chat.cancel": ("claude CLI 未安装", "安装官方 Claude Code CLI 并核验订阅"),
    # voice.*：Siren
    "voice.send": ("Siren 未接入（其语音 provider 亦为 dev 回退）", "提供 Siren 服务凭据"),
    "voice.call.start": ("Siren 未接入", "提供 Siren 服务凭据"),
    "voice.call.get": ("Siren 未接入", "提供 Siren 服务凭据"),
    "voice.call.end": ("Siren 未接入", "提供 Siren 服务凭据"),
    "voice.status": ("Siren 未接入", "提供 Siren 服务凭据"),
    # group.*：扎西德勒
    "group.list": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    "group.history": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    "group.archive.list": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    "group.archive.get": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    "group.send": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    "group.send_message": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    "group.profile": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    "group.status": ("扎西德勒 8787 未运行", "启动服务后核验契约"),
    # wishstar.*：Superposition
    "wishstar.list": ("Superposition 写入凭据未配置", "为 mariposa 建独立服务凭据"),
    "wishstar.get": ("Superposition 写入凭据未配置", "为 mariposa 建独立服务凭据"),
    "wishstar.write": ("Superposition 写入凭据未配置", "为 mariposa 建独立服务凭据"),
    "wishstar.respond": ("Superposition 写入凭据未配置", "为 mariposa 建独立服务凭据"),
    # wakeup
    "wakeup.status": ("AUTO_WAKEUP_ENABLED=false（§21 默认）", "配置并真实验收后开启"),
    "wakeup.configure": ("AUTO_WAKEUP_ENABLED=false（§21 默认）", "配置并真实验收后开启"),
    # 其他
    "sticker.send": ("依赖 chat 通道", "chat 接入后实施"),
    "settings.update": ("配置修改走部署参数（policy version 入审计）",
                        "需要在线配置面板时另做产品修订"),
}

_RESERVED_CAPS = {
    "listening.play": "一起听歌未选供应商；不承诺第三方曲库",
    "listening.queue": "同上",
    "listening.seek": "同上",
    "listening.pause": "同上",
    "listening.enqueue": "同上",
    "listening.join": "同上",
    "listening.status": "同上",  # 与既有 listening.status 重名时由既有优先
    "listening.sync": "同上",
    "listening.leave": "同上",
}

_OWNERS = {"qiaosheng", "jiaming"}


def register_v1_compat() -> dict:
    """把规格缺失项注册进 REGISTRY；返回注册统计。"""
    added = {"alias": 0, "blocked": 0, "reserved": 0, "thin": 0}
    for spec_name, target in _ALIASES.items():
        if spec_name not in REGISTRY and target in REGISTRY:
            REGISTRY[spec_name] = _alias(spec_name)
            added["alias"] += 1
    for name, (reason, unblock) in _BLOCKED_CAPS.items():
        if name not in REGISTRY:
            REGISTRY[name] = Capability(
                name, _blocked(reason, unblock), _OWNERS, False,
                description=f"[blocked] {reason}")
            added["blocked"] += 1
    for name, note in _RESERVED_CAPS.items():
        if name not in REGISTRY:
            REGISTRY[name] = Capability(
                name, _reserved(note), _OWNERS, False,
                description=f"[reserved] {note}")
            added["reserved"] += 1
    added["thin"] = _register_thin()
    _register_create_alias()
    return added


def _register_thin() -> int:
    from . import registry as R
    n = 0

    def add(name, handler, allowed=_OWNERS, write=False):
        nonlocal n
        if name not in REGISTRY:
            REGISTRY[name] = Capability(name, handler, set(allowed), write,
                                        description=f"[v1.1 薄实现] {name}")
            n += 1

    add("capabilities.list", lambda p, a: {
        "capabilities": R.list_capabilities(p)})
    add("capabilities.status", lambda p, a: {
        "total": len(REGISTRY),
        "blocked": sorted(k for k, v in REGISTRY.items()
                          if v.description.startswith("[blocked]")),
        "reserved": sorted(k for k, v in REGISTRY.items()
                           if v.description.startswith("[reserved]"))})

    def _moments_get(p, a):
        mid = str(a.get("moment_id", ""))
        with db.formal() as conn:
            row = conn.execute(
                "SELECT m.id, m.author, m.kind, v.content FROM moments m JOIN"
                " moment_versions v ON v.moment_id=m.id AND"
                " v.version_no=m.current_version_no WHERE m.id=?", (mid,)).fetchone()
        if row is None:
            raise NotFound("moment not found", moment_id=mid)
        return dict(row)

    add("moments.get", _moments_get)

    def _plan_get(p, a):
        from ..plans import service as plans
        with db.formal() as conn:
            return plans.get(conn, str(a.get("plan_id", "")))

    add("plan.get", _plan_get)
    add("plan.complete", lambda p, a: _plan_set_state(p, a, "done"),
        write=True)
    add("plan.cancel", lambda p, a: _plan_set_state(p, a, "cancelled"),
        write=True)

    def _plan_set_state(p, a, state):
        from ..plans import service as plans
        return plans.update(p.principal_id, str(a.get("plan_id", "")),
                            int(a.get("expected_version", 0)), state=state)

    def _reminder_get(p, a):
        rid = str(a.get("reminder_id", ""))
        with db.formal() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id=?",
                               (rid,)).fetchone()
        if row is None:
            raise NotFound("reminder not found", reminder_id=rid)
        return dict(row)

    def _reminder_update(p, a):
        from ..reminders import service as reminders
        rid = str(a.get("reminder_id", ""))
        with db.formal() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id=?",
                               (rid,)).fetchone()
        if row is None:
            raise NotFound("reminder not found", reminder_id=rid)
        if row["status"] != "scheduled":
            raise Forbidden("only scheduled can update", status=row["status"])
        sets, vals = [], []
        for k in ("title", "note", "remind_at"):
            if k in a:
                sets.append(f"{k}=?")
                vals.append(a[k])
        if not sets:
            raise Forbidden("nothing to update")
        with db.formal() as conn:
            conn.execute(f"UPDATE reminders SET {', '.join(sets)},"
                         " updated_at=datetime('now') WHERE id=?",
                         (*vals, rid))
        return {"reminder_id": rid, "updated": True}

    add("reminder.get", _reminder_get)
    add("reminder.update", _reminder_update, write=True)

    def _self_read(p, a):
        sid = str(a.get("self_id", ""))
        with db.formal() as conn:
            row = conn.execute(
                "SELECT s.id, s.aspect, s.review_state, s.written_at, v.content"
                " FROM self_entries s JOIN self_versions v ON v.self_id=s.id"
                " AND v.version_no=s.current_version_no WHERE s.id=?",
                (sid,)).fetchone()
        if row is None:
            raise NotFound("self entry not found", self_id=sid)
        return dict(row)

    add("self.read", _self_read)

    def _deletion_get(p, a):
        from ..letters import service as letters
        rid = str(a.get("request_id", ""))
        for r in letters.deletion_list():
            if r["id"] == rid:
                return r
        raise NotFound("deletion request not found", request_id=rid)

    add("memory.deletion.get", _deletion_get)

    # v1.7：遗忘提案兼容层（workspace.proposals.get/withdraw 与
    # memory.forgetting.*）已随遗忘链整体退役，不再注册。

    def _runs_get(p, a):
        rid = str(a.get("run_id", ""))
        with db.workspace() as conn:
            row = conn.execute("SELECT * FROM worker_runs WHERE run_id=?",
                               (rid,)).fetchone()
        if row is None:
            raise NotFound("run not found", run_id=rid)
        return dict(row)

    add("workspace.runs.get", _runs_get)

    # hold_candidate 最小机制（§6.1；confirm 仅周家明——§4.11）
    def _candidates_create(p, a):
        import uuid
        from datetime import datetime, timezone
        mid = str(a.get("target_memory_id", "")) or f"cand_{uuid.uuid4().hex[:8]}"
        text = str(a.get("text", ""))
        if not text.strip():
            raise Forbidden("candidate text required")
        item_id = f"hc_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        payload = {"proposal_type": "hold_candidate", "text": text,
                   "why_remember": a.get("why_remember"),
                   "memory_date": a.get("memory_date")}
        with db.workspace() as conn:
            conn.execute(
                "INSERT INTO work_items(item_id, item_type, target_memory_id,"
                " state, current_revision, created_by, created_at, updated_at)"
                " VALUES(?, 'hold_candidate', ?, 'draft', 1, ?, ?, ?)",
                (item_id, mid, p.principal_id, now, now))
            conn.execute(
                "INSERT INTO proposal_versions(proposal_id, revision, payload,"
                " payload_hash, created_by, submitted_at) VALUES(?,1,?,?,?,NULL)",
                (item_id, __import__("json").dumps(payload, ensure_ascii=False),
                 __import__("hashlib").sha256(__import__("json").dumps(
                     payload, ensure_ascii=False, sort_keys=True).encode()
                 ).hexdigest(), p.principal_id))
        return {"candidate_id": item_id, "state": "draft"}

    add("workspace.candidates.create", _candidates_create,
        {"worker", "qiaosheng", "jiaming"}, write=True)

    def _candidate_confirm(p, a):
        if p.principal_id != "jiaming":
            raise Forbidden("仅周家明确认 Hold 候选（§4.11）")
        from ..memory import service as memory
        cid = str(a.get("candidate_id", ""))
        text, why, mdate = _candidate_payload(cid)
        out = memory.hold(p, text=text, why_remember=why, memory_date=mdate)
        _resolve_candidate(cid, "confirmed")
        return out

    def _candidate_reject(p, a):
        _resolve_candidate(str(a.get("candidate_id", "")), "rejected")
        return {"candidate_id": a.get("candidate_id"), "state": "rejected"}

    def _candidate_rewrite(p, a):
        if p.principal_id != "jiaming":
            raise Forbidden("仅周家明改写并确认候选（§4.11）")
        from ..memory import service as memory
        cid = str(a.get("candidate_id", ""))
        _, why, mdate = _candidate_payload(cid)
        out = memory.hold(p, text=str(a.get("text", "")), why_remember=why,
                          memory_date=mdate)
        _resolve_candidate(cid, "confirmed")
        return out

    def _candidate_payload(cid):
        import json as _json
        with db.workspace() as conn:
            row = conn.execute(
                "SELECT payload FROM proposal_versions WHERE proposal_id=? AND"
                " revision=1", (cid,)).fetchone()
        if row is None:
            raise NotFound("candidate not found", candidate_id=cid)
        pl = _json.loads(row["payload"])
        return pl.get("text", ""), pl.get("why_remember"), pl.get("memory_date")

    def _resolve_candidate(cid, state):
        with db.workspace() as conn:
            cur = conn.execute(
                "UPDATE work_items SET state=?, updated_at=datetime('now')"
                " WHERE item_id=? AND item_type='hold_candidate'", (state, cid))
            if cur.rowcount == 0:
                raise NotFound("candidate not found", candidate_id=cid)

    add("memory.candidates.confirm", _candidate_confirm)
    add("memory.candidates.reject", _candidate_reject, write=True)
    add("memory.candidates.rewrite", _candidate_rewrite)

    def _deletion_restore(p, a):
        """撤销/恢复删除申请的 pending（语义=撤回；旧系统 restore 未核验到独立端点）。"""
        from ..letters import service as letters
        return letters.deletion_withdraw(p.principal_id,
                                         str(a.get("resource_id", "")))

    add("memory.deletion.restore", _deletion_restore, write=True)

    def _emotions_set(p, a):
        from ..content import service as content
        tags = a.get("tags") or []
        if not isinstance(tags, list):
            raise Forbidden("tags must be a list")
        return content.tags_add(p.principal_id, str(a.get("memory_id", "")),
                                tags)

    add("memory.emotions.set", _emotions_set, write=True)

    # v1.7：memory.forgetting.request / proposals.list / proposals.get
    # 已随遗忘链退役（2026-09-28 决策），不再注册。

    def _runs_report(p, a):
        """工具人上报运行结果（worker_runs 补录 + 状态）。"""
        rid = str(a.get("run_id", "")) or f"run_manual_{p.principal_id}"
        stats = a.get("stats") or {}
        with db.workspace() as conn:
            cur = conn.execute(
                "UPDATE worker_runs SET finished_at=datetime('now'), stats=?"
                " WHERE run_id=?", (__import__("json").dumps(stats,
                                                   ensure_ascii=False), rid))
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO worker_runs(run_id, run_type, started_by,"
                    " started_at, finished_at, stats) VALUES(?, 'manual', ?,"
                    " datetime('now'), datetime('now'), ?)",
                    (rid, p.principal_id, __import__("json").dumps(stats,
                                                                   ensure_ascii=False)))
        return {"run_id": rid, "recorded": True}

    add("workspace.runs.report", _runs_report, write=True)
    return n


# 规格清单中与薄实现名不同的：workspace.proposals.create == candidates 创建
def _register_create_alias():
    cap = REGISTRY.get("workspace.candidates.create")
    if cap and "workspace.proposals.create" not in REGISTRY:
        REGISTRY["workspace.proposals.create"] = Capability(
            "workspace.proposals.create", cap.handler, cap.allowed_principals,
            cap.write, cap.idempotent,
            "[规格别名] 创建 Hold 候选（同 workspace.candidates.create）")
