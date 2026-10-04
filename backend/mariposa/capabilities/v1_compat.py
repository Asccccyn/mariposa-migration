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
from ..identity import Principal  # 类型来源与 registry 同一对象

#: registry 名字晚绑定（registry 底部装配本模块——顶部互 import 成环）
def _R():
    from . import registry
    return registry


def _Capability(*args, **kwargs):
    return _R().Capability(*args, **kwargs)


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
    cap = _R().REGISTRY[target]
    return _Capability(spec_name, cap.handler, cap.allowed_principals, cap.write,
                      cap.idempotent, f"[规格别名] {cap.description}")


_ALIASES = {
    "activity.list": "maintenance.activity.list",
    "jobs.status": "maintenance.jobs.status",
    "presence.handoff.latest": "handoff.latest",
    "presence.handoff.write": "handoff.write",
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
    R = _R()
    REG = R.REGISTRY
    added = {"alias": 0, "blocked": 0, "reserved": 0, "thin": 0}
    for spec_name, target in _ALIASES.items():
        if spec_name not in REG and target in REG:
            REG[spec_name] = _alias(spec_name)
            added["alias"] += 1
    for name, (reason, unblock) in _BLOCKED_CAPS.items():
        if name not in REG:
            REG[name] = R.Capability(
                name, _blocked(reason, unblock), _OWNERS, False,
                description=f"[blocked] {reason}")
            added["blocked"] += 1
    for name, note in _RESERVED_CAPS.items():
        if name not in REG:
            REG[name] = R.Capability(
                name, _reserved(note), _OWNERS, False,
                description=f"[reserved] {note}")
            added["reserved"] += 1
    added["thin"] = _register_thin()
    return added


def _register_thin() -> int:
    from . import registry as R
    REG = R.REGISTRY
    Cap = R.Capability
    n = 0

    def add(name, handler, allowed=_OWNERS, write=False):
        nonlocal n
        if name not in REG:
            REG[name] = Cap(name, handler, set(allowed), write,
                            description=f"[v1.1 薄实现] {name}")
            n += 1

    add("capabilities.list", lambda p, a: {
        "capabilities": R.list_capabilities(p)})
    add("capabilities.status", lambda p, a: {
        "total": len(REG),
        "blocked": sorted(k for k, v in REG.items()
                          if v.description.startswith("[blocked]")),
        "reserved": sorted(k for k, v in REG.items()
                           if v.description.startswith("[reserved]"))})



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






    # v1.7：遗忘提案兼容层（workspace.proposals.get/withdraw 与
    # memory.forgetting.*）已随遗忘链整体退役，不再注册。







    # v1.7：memory.forgetting.request / proposals.list / proposals.get
    # 已随遗忘链退役（2026-09-28 决策），不再注册。


    return n



