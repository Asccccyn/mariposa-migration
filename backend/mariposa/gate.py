"""访问门禁（2026-10-04 收口计划三件套之一）。

应用层限速 + 认证失败锁定，纯内存态（部署形态：uvicorn 单 worker，
本机 cloudflared 回环转发）。重启清零是 fail-soft：真实攻击者重启后
重新累计即重新触发，不构成绕过。

语义：
- 失败锁定：同一来源 IP 连续认证失败达阈值 → 锁定该 IP（期间一切
  认证请求拒绝，包括正确 token——防在线枚举）；锁定时长指数升级
  （base → base*4 → … 封顶 max），认证成功清零计数。触发
  （auth.locked）与到期（auth.lock.expired，GATE-05 一次性事件）
  留 audit 痕迹。
- 限速：滑动窗口三档——匿名档按 IP 只计**认证失败**的请求
  （GATE-01：成功请求不占匿名档）；读/写档按 principal 由调用方
  按能力分类扣减。超限 429 + Retry-After（结构化 RATE_LIMITED）。
- 回收（GATE-03）：门禁字典惰性清理 + 有界容量——滑动窗口键在
  窗口滑过后删除；失败状态在锁定到期后保留一段 TTL（维持指数
  升级层级），TTL 过后删除；容量超限时淘汰最旧的未锁状态。
- 客户端 IP：Cloudflare Tunnel 部署下直连地址是本机回环，真实
  客户端在 CF-Connecting-IP。该头**只在直连为回环时**采信
  （公网直连伪造此头不生效，按真实 socket 地址计数）。

不做：跨进程共享计数、持久化封禁名单、全局限流——单进程单机
部署下不需要；换部署形态时本模块整体替换。
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque

from . import config

_LOCK = threading.Lock()

# {ip: {"fails": int, "locked_until": float, "lock_level": int,
#        "last_fail": float, "expired_reported": bool}}
_auth_failures: dict[str, dict] = {}
# {(kind, key): deque[timestamps]}——kind: read/write/anon
_rate_windows: dict[tuple[str, str], deque] = {}

_WINDOW_SECONDS = 60.0
# 失败状态 TTL：锁定到期后再保留 24h（维持指数升级层级），期间
# 无新失败则在回收时删除（GATE-03）
_STATE_TTL_S = 86400.0
_MAX_TRACKED_KEYS = 10000
_MAINTAIN_INTERVAL_S = 60.0
_MAINTAIN_EVERY_OPS = 256

_last_maintain = 0.0
_ops_since_maintain = 0

_now = time.monotonic


def reset_for_tests() -> None:
    """测试隔离：清空全部计数（conftest reset_all 调用；生产无入口
    可达——HTTP/MCP 路由不触达本函数）。"""
    global _last_maintain, _ops_since_maintain
    with _LOCK:
        _auth_failures.clear()
        _rate_windows.clear()
        _last_maintain = _now()
        _ops_since_maintain = 0


def _maybe_maintain() -> None:
    """惰性清理触发：每 N 次门禁操作或每 60s 一次（无锁快检）。"""
    global _last_maintain, _ops_since_maintain
    _ops_since_maintain += 1
    if (_ops_since_maintain < _MAINTAIN_EVERY_OPS
            and _now() - _last_maintain < _MAINTAIN_INTERVAL_S):
        return
    _maintain()


def _maintain() -> None:
    """锁内全局清理（GATE-03）：

    - 滑动窗口：空 deque 或整窗滑出（最老时间戳已出窗）→ 删键；
    - 失败状态：锁定中保留；未锁且窗口内仍有活跃失败计数保留；
      其余（含锁已到期的）在 TTL 内保留升级层级，TTL 过后删除；
    - 容量：清理后仍超上限 → 淘汰最旧的未锁状态（dict 保插入序）；
      全在锁中（极端）时不淘汰——锁定状态是安全语义，不做牺牲。
    """
    global _last_maintain, _ops_since_maintain
    now = _now()
    with _LOCK:
        _last_maintain = now
        _ops_since_maintain = 0
        for key in list(_rate_windows.keys()):
            window = _rate_windows[key]
            if not window or window[0] <= now - _WINDOW_SECONDS:
                del _rate_windows[key]
        for ip in list(_auth_failures.keys()):
            st = _auth_failures[ip]
            if st.get("locked_until", 0.0) > now:
                continue
            last = st.get("last_fail", 0.0)
            if st.get("fails", 0) > 0 and now - last < _WINDOW_SECONDS:
                continue
            if now - last < _STATE_TTL_S:
                continue
            del _auth_failures[ip]
        if len(_auth_failures) > _MAX_TRACKED_KEYS:
            overflow = len(_auth_failures) - _MAX_TRACKED_KEYS
            for ip in list(_auth_failures.keys()):
                if overflow <= 0:
                    break
                st = _auth_failures[ip]
                if st.get("locked_until", 0.0) > now:
                    continue
                del _auth_failures[ip]
                overflow -= 1
        if len(_rate_windows) > _MAX_TRACKED_KEYS:
            overflow = len(_rate_windows) - _MAX_TRACKED_KEYS
            for key in list(_rate_windows.keys()):
                if overflow <= 0:
                    break
                del _rate_windows[key]
                overflow -= 1


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
    """锁定校验；被锁时返回剩余秒数（>0），未锁/已到期返回 0。"""
    _maybe_maintain()
    with _LOCK:
        state = _auth_failures.get(ip)
        if not state:
            return 0.0
        remain = state.get("locked_until", 0.0) - _now()
        if remain > 0:
            return remain
    return 0.0


def take_lock_expired_event(ip: str) -> dict | None:
    """锁定**到期**的一次性识别（GATE-05，锁内状态转换）。

    首次发现某来源的锁已到期（且未报告过）→ 标记并返回事件数据；
    之后同一锁不再重复报告。新锁定周期（再次升级）会重置标记。
    调用方在锁外写审计（auth.lock.expired）。
    """
    with _LOCK:
        state = _auth_failures.get(ip)
        if not state:
            return None
        locked_until = state.get("locked_until", 0.0)
        if (locked_until <= 0 or _now() < locked_until
                or state.get("expired_reported")):
            return None
        state["expired_reported"] = True
        return {"lock_level": state.get("lock_level", 0),
                "locked_until": locked_until}


def note_auth_failure(ip: str, record_audit=None) -> None:
    """认证失败计数；达阈值进入/升级锁定。record_audit 可选回调
    （隔离事务的 audit 写入视图，避免本模块直接开库事务）。"""
    _maybe_maintain()
    with _LOCK:
        state = _auth_failures.setdefault(
            ip, {"fails": 0, "locked_until": 0.0, "lock_level": 0,
                 "last_fail": _now(), "expired_reported": False})
        state["fails"] += 1
        state["last_fail"] = _now()
        if state["fails"] < config.GATE_AUTH_FAIL_THRESHOLD:
            return
        level = state["lock_level"] + 1
        state["lock_level"] = level
        state["fails"] = 0
        state["expired_reported"] = False  # 新锁定周期（GATE-05）
        seconds = min(config.GATE_LOCKOUT_BASE_SECONDS * (4 ** (level - 1)),
                      config.GATE_LOCKOUT_MAX_SECONDS)
        state["locked_until"] = _now() + seconds
    if record_audit is not None:
        try:
            record_audit(level, seconds)
        except Exception as e:
            # 审计失败不撤销锁定（安全优先）；显式留痕到 stderr
            # （uvicorn error log），不静默（GATE-06）
            sys.stderr.write(
                f"[gate] auth.locked audit write failed: {e!r}\n")


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


# ---------------------------------------------------------------- 限速

def _limit_for(kind: str) -> int:
    if kind == "write":
        return config.GATE_RATE_WRITE_PER_MIN
    if kind == "read":
        return config.GATE_RATE_READ_PER_MIN
    return config.GATE_RATE_ANON_PER_MIN


def check_rate(kind: str, key: str) -> float:
    """滑动窗口计数；放行返回 0，超限返回建议等待秒数（Retry-After）。

    GATE-04：非正 limit（零/负配置）恒拒绝（60s 等待）——不依赖
    非空 deque 假设，杜绝 IndexError 500。
    """
    _maybe_maintain()
    limit = _limit_for(kind)
    now = _now()
    if limit <= 0:
        return _WINDOW_SECONDS
    with _LOCK:
        window = _rate_windows.setdefault((kind, key), deque())
        while window and window[0] <= now - _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            wait = _WINDOW_SECONDS - (now - window[0])
            return max(0.5, wait)
        window.append(now)
    return 0.0
