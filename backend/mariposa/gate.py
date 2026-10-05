"""访问门禁（2026-10-04 收口计划三件套之一）。

应用层限速 + 认证失败锁定，纯内存态（部署形态：uvicorn 单 worker，
本机 cloudflared 回环转发）。重启清零是 fail-soft：真实攻击者重启后
重新累计即重新触发，不构成绕过。

语义：
- 失败锁定：同一来源 IP 连续认证失败达阈值 → 锁定该 IP（期间一切
  认证请求拒绝，包括正确 token——防在线枚举）；锁定时长指数升级
  （base → base*4 → … 封顶 max），认证成功清零计数。触发与到期
  留 audit 痕迹（auth.locked）。
- 限速：认证成功按 principal 分读/写两档滑动窗口；未认证请求按
  IP 记匿名档。超限 429 + Retry-After（结构化 RATE_LIMITED）。
- 客户端 IP：Cloudflare Tunnel 部署下直连地址是本机回环，真实
  客户端在 CF-Connecting-IP。该头**只在直连为回环/内网时**采信
  （公网直连伪造此头不生效，按真实 socket 地址计数）。

不做：跨进程共享计数、持久化封禁名单、全局限流——单进程单机
部署下不需要；换部署形态时本模块整体替换。
"""
from __future__ import annotations

import threading
import time
from collections import deque

from . import config

_LOCK = threading.Lock()

# {ip: {"fails": int, "locked_until": float, "lock_level": int}}
_auth_failures: dict[str, dict] = {}
# {(kind, key): deque[timestamps]}——kind: read/write/anon
_rate_windows: dict[tuple[str, str], deque] = {}

_WINDOW_SECONDS = 60.0

_now = time.monotonic


def reset_for_tests() -> None:
    """测试隔离：清空全部计数（conftest reset_all 不自动调用——门禁
    状态跨用例保留是生产语义，测试按需显式重置）。"""
    with _LOCK:
        _auth_failures.clear()
        _rate_windows.clear()


def client_ip(request) -> str:
    """认证来源 IP（锁定/匿名限速的计数维度）。

    代理头只在直连地址为**回环**（本机 cloudflared 部署形态）时采信
    ——is_private 会把文档/保留段（如 203.0.113.0/24）也判私网，
    那些地址出现在公网直连时伪造头不应生效；分机部署形态再扩
    显式白名单。公网直连按 socket 真实地址，伪造头不参与。
    """
    peer = request.client.host if request.client else "unknown"
    try:
        import ipaddress
        trusted_proxy = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        trusted_proxy = False
    if trusted_proxy:
        cf = request.headers.get("CF-Connecting-IP", "").strip()
        if cf:
            return cf
        xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if xff:
            return xff
    return peer


# ---------------------------------------------------------------- 锁定

def assert_not_locked(ip: str) -> float:
    """锁定校验；被锁时抛 AuthLocked（附剩余秒数），未锁返回 0。"""
    with _LOCK:
        state = _auth_failures.get(ip)
        if not state:
            return 0.0
        remain = state.get("locked_until", 0.0) - _now()
        if remain > 0:
            return remain
    return 0.0


def note_auth_failure(ip: str, record_audit=None) -> None:
    """认证失败计数；达阈值进入/升级锁定。record_audit 可选回调
    （audit.record 视图，避免本模块直接开库事务）。"""
    with _LOCK:
        state = _auth_failures.setdefault(
            ip, {"fails": 0, "locked_until": 0.0, "lock_level": 0})
        state["fails"] += 1
        if state["fails"] < config.GATE_AUTH_FAIL_THRESHOLD:
            return
        level = state["lock_level"] + 1
        state["lock_level"] = level
        state["fails"] = 0
        seconds = min(config.GATE_LOCKOUT_BASE_SECONDS * (4 ** (level - 1)),
                      config.GATE_LOCKOUT_MAX_SECONDS)
        state["locked_until"] = _now() + seconds
    if record_audit is not None:
        try:
            record_audit(level, seconds)
        except Exception:
            pass  # 审计失败不回滚门禁动作


def note_auth_success(ip: str) -> None:
    """认证成功清零该 IP 的失败计数（锁定已到期时同时解除层级）。"""
    with _LOCK:
        state = _auth_failures.get(ip)
        if not state:
            return
        state["fails"] = 0
        if state.get("locked_until", 0.0) <= _now():
            state["lock_level"] = 0
            state["locked_until"] = 0.0


class AuthLocked(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"auth locked, retry after {retry_after:.0f}s")
        self.retry_after = max(1, int(retry_after))


# ---------------------------------------------------------------- 限速

def _limit_for(kind: str) -> int:
    if kind == "write":
        return config.GATE_RATE_WRITE_PER_MIN
    if kind == "read":
        return config.GATE_RATE_READ_PER_MIN
    return config.GATE_RATE_ANON_PER_MIN


def check_rate(kind: str, key: str) -> float:
    """滑动窗口计数；放行返回 0，超限返回建议等待秒数（Retry-After）。"""
    limit = _limit_for(kind)
    now = _now()
    with _LOCK:
        window = _rate_windows.setdefault((kind, key), deque())
        while window and window[0] <= now - _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            wait = _WINDOW_SECONDS - (now - window[0])
            return max(0.5, wait)
        window.append(now)
    return 0.0
