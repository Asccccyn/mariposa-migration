"""Source Adapter 注册表（实施规格 §3）。

DeepSeek 按用户指示本轮不做；接入新 provider 时在此注册 detect 函数即可，
importer / query / binding 层不感知具体 provider。
"""
from __future__ import annotations

from typing import Any, Callable

from . import claude, gemini

#: provider -> detect(element) -> bool（按注册顺序探测）
_ADAPTERS: dict[str, Callable[[Any], bool]] = {
    claude.PROVIDER: claude.detect,
    gemini.PROVIDER: gemini.detect,
}

#: provider -> 模块（normalize_* / synthetic_* / speaker_of）
_MODULES: dict[str, Any] = {
    claude.PROVIDER: claude,
    gemini.PROVIDER: gemini,
}


def detect_provider(first_element: Any) -> str | None:
    if first_element is None:
        return None
    for provider, detect in _ADAPTERS.items():
        try:
            if detect(first_element):
                return provider
        except Exception:
            continue
    return None


def module_for(provider: str) -> Any:
    mod = _MODULES.get(provider)
    if mod is None:
        raise KeyError(f"no source adapter registered for provider: {provider}")
    return mod

