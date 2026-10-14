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
    "settings.get": "maintenance.settings.get",
}

# 2026-10-13 她裁定退役清空：blocked 27 项（chat/voice/group/wishstar/
# wakeup/settings.update）与 reserved 9 项（listening）全部删除——这些是
# 2026-09-22 v1.1"150 项最低契约"凑全覆盖的占位壳，从未接通（功能在
# estómago/Siren/扎西德勒/Superposition 各自项目实现，est 侧对本层零引用）。
# emotion.context.get 是主表 capability（非本层），按她裁定保留并改薄实现
# （情绪引擎将来取心情数据）。
_BLOCKED_CAPS: dict[str, tuple[str, str]] = {}

_RESERVED_CAPS: dict[str, str] = {}

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



