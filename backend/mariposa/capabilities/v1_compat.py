"""保留的旧公开名字：三个维护别名和两个能力发现入口。

退役的 blocked/reserved 占位与已转正的 plan handlers 不再装配。
由 registry 初始化时调用一次；再次调用仍幂等。
"""
from __future__ import annotations

from dataclasses import replace

_ALIASES = {
    "activity.list": "maintenance.activity.list",
    "jobs.status": "maintenance.jobs.status",
    "settings.get": "maintenance.settings.get",
}


def register_v1_compat() -> dict:
    # registry 完成主表后晚绑定，避免模块导入成环。
    from . import registry as R
    added = {"alias": 0, "thin": 0}
    for alias, target in _ALIASES.items():
        if alias not in R.REGISTRY:
            cap = R.REGISTRY[target]
            R.REGISTRY[alias] = replace(
                cap, name=alias, description=f"[规格别名] {cap.description}")
            added["alias"] += 1

    handlers = {
        "capabilities.list": lambda p, a: {
            "capabilities": R.list_capabilities(p)},
        "capabilities.status": lambda p, a: {
            "total": len(R.REGISTRY),
            "blocked": sorted(n for n, c in R.REGISTRY.items()
                              if c.status == "blocked"),
            "reserved": sorted(n for n, c in R.REGISTRY.items()
                               if c.status == "reserved")},
    }
    for name, handler in handlers.items():
        if name not in R.REGISTRY:
            R.REGISTRY[name] = R.Capability(
                name, handler, {"qiaosheng", "jiaming"}, False,
                description="当前能力清单" if name.endswith("list")
                            else "当前能力可用状态统计")
            added["thin"] += 1
    return added
