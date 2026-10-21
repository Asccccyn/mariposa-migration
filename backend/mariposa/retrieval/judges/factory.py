"""provider 工厂（R09）：与纯协议拆开——base 只留协议与数据模型，
工厂单向依赖 providers，不是动态 import 掩环。"""
from __future__ import annotations

from .base import DisabledJudge, JudgeProvider


#: 测试/评测注入点（不进生产配置；离线对照用同一接口）
_INJECTED: dict[str, JudgeProvider] = {}


def register_for_tests(name: str, provider: JudgeProvider) -> None:
    _INJECTED[name] = provider


def clear_injected() -> None:
    _INJECTED.clear()



def get_provider_by_name(name: str | None):
    """按政策所选 provider 名构造（未配置/未安装 → DisabledJudge）。

    调用方负责先读政策：enabled=false 时不进入本函数。
    WP-02（JFA-002 接线）：codex_sdk 真构造（与 readiness 同源；SDK
    未装 → DisabledJudge，细因由 provider_readiness 的 sdk_not_installed
    披露——不静默换 provider）。
    """
    if name in _INJECTED:
        return _INJECTED[name]
    if not name or name in ("disabled",):
        return DisabledJudge()
    if name == "typesafe_jev":
        from . import typesafe_jev
        return typesafe_jev.TypeSafeJevJudge()
    if name == "codex_sdk":
        try:
            from . import codex_sdk
        except ImportError:
            return DisabledJudge()
        if not codex_sdk.sdk_available():
            # SDK 未装 → DisabledJudge（阻断语义）；细因由
            # provider_readiness 的 sdk_not_installed 披露
            return DisabledJudge()
        return codex_sdk.CodexSdkJudge()
    return DisabledJudge()
