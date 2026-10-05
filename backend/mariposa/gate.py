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

#: AF-GATE-01（四轮复审）：曾因容量满被路由进溢出桶的键——溢出桶
#: 滑空前保持粘性（继续路由溢出桶，不建独立桶），否则旧容量腾出
#: 后同主体获得"溢出计数 + 新独立桶"双份配额。
#: MIN-GATE-02（五轮复审）：有序 dict 保插入序、容量有界（超限
#: 淘汰最旧成员——保守恢复独立资格，溢出桶计数仍在）；只在**实际
#: 放行消费**时加入成员，被拒（超限）的来源不留成员记录
_ovf_members: dict = {}

#: AF-GATE-03（四轮复审）：到期事件消费回调——维护流程发现有界批量
#: 的未消费到期事件时经此落审计（app 启动时注入 record_isolated
#: 包装）；None 时（测试/未装配）直接标记消费，不写审计
_expiry_sink = None


def set_expiry_sink(fn) -> None:
    """注入到期事件消费回调（fn(events: list[tuple[ip, payload]])）。"""
    global _expiry_sink
    _expiry_sink = fn


_now = time.monotonic


def reset_for_tests() -> None:
    """测试隔离：清空全部计数（conftest reset_all 调用；生产无入口
    可达——HTTP/MCP 路由不触达本函数）。"""
    global _last_maintain, _ops_since_maintain, _ovf_members, _expiry_sink
    with _LOCK:
        _auth_failures.clear()
        _rate_windows.clear()
        _ovf_members.clear()  # dict.clear 同义
        _last_maintain = _now()
        _ops_since_maintain = 0
    _expiry_sink = None


def _maybe_maintain() -> None:
    """惰性清理触发：每 N 次门禁操作或每 60s 一次（无锁快检）。"""
    global _last_maintain, _ops_since_maintain
    _ops_since_maintain += 1
    if (_ops_since_maintain < _MAINTAIN_EVERY_OPS
            and _now() - _last_maintain < _MAINTAIN_INTERVAL_S):
        return
    _maintain()


def _maintain() -> None:
    """锁内全局清理（GATE-03；RE-GATE-01/02 三轮修订）：

    - 滑动窗口：**逐项**弹出已滑出窗口的时间戳，全部弹出后才删键
      （此前"最老时间戳过期即删整个 deque"，把窗口内仍有效的计数
      一起清掉，提前释放配额——RE-GATE-01）；
    - 失败状态：锁定中保留；TTL 锚 = max(last_fail, locked_until)
      （此前只看 last_fail，锁刚到期就被回收，承诺的锁后保留期、
      到期事件与升级层级一并丢失——RE-GATE-02）；**未消费的到期
      事件（expired_reported=False 且锁已到期）不回收**——等待该
      来源下一次请求触发一次性审计，容量淘汰同样绕过；
    - 容量：滑动窗口只删空键（活跃计数永不被容量挤掉，配额安全
      优先于内存上限；新键超容走溢出桶，见 check_rate）；失败状态
      超容时淘汰最旧的未锁且无未消费到期事件的状态。
    """
    global _last_maintain, _ops_since_maintain
    now = _now()
    # AF-GATE-03（四轮复审）：有界批量消费"已到期未报告"事件——
    # 锁内收集，锁外交付 sink（audit 隔离事务），成功后标记并按
    # TTL 正常回收；原来源之后再来时 take_lock_expired_event 返回
    # None，不重复。无 sink（测试/未装配）直接标记（重启等价语义）
    pending_expiry: list[tuple[str, dict]] = []
    with _LOCK:
        _last_maintain = now
        _ops_since_maintain = 0
        for key in list(_rate_windows.keys()):
            window = _rate_windows[key]
            cutoff = now - _WINDOW_SECONDS
            while window and window[0] <= cutoff:
                window.popleft()
            if not window:
                del _rate_windows[key]
                if key[1] == "\x00_overflow":
                    # 溢出桶滑空：粘性成员恢复独立桶资格（AF-GATE-01）
                    for m in [m for m in _ovf_members if m[0] == key[0]]:
                        _ovf_members.pop(m, None)
        for ip in list(_auth_failures.keys()):
            st = _auth_failures[ip]
            locked_until = st.get("locked_until", 0.0)
            if locked_until > now:
                continue
            last = st.get("last_fail", 0.0)
            if st.get("fails", 0) > 0 and now - last < _WINDOW_SECONDS:
                continue
            if locked_until > 0 and not st.get("expired_reported"):
                # MIN-GATE-01（五轮复审）：锁内即 claim（置位）——
                # 并发维护/原来源 take 都看不到已 claim 的周期，
                # 同一锁周期只允许一个消费者；sink 失败在锁内回滚
                st["expired_reported"] = True
                pending_expiry.append(
                    (ip, {"lock_level": st.get("lock_level", 0),
                          "locked_until": locked_until}))
                continue
            anchor = max(last, locked_until)
            if now - anchor < _STATE_TTL_S:
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
                if (st.get("locked_until", 0.0) > 0
                        and not st.get("expired_reported")):
                    continue  # 交由 pending 消费流程处理，不静默丢
                del _auth_failures[ip]
                overflow -= 1
    if pending_expiry:
        if _expiry_sink is not None:
            try:
                _expiry_sink(pending_expiry)
            except Exception as e:
                # 消费失败：锁内回滚 claim（该周期下轮可重试）；
                # stderr 留痕（不静默）。ccbe12c 起单条失败由 app 端
                # sink 自行留痕不 raise，此处仅整体性异常防御
                import sys
                sys.stderr.write(
                    f"[gate] expiry sink failed ({len(pending_expiry)} "
                    f"events): {e!r}\n")
                with _LOCK:
                    for ip, payload in pending_expiry:
                        st = _auth_failures.get(ip)
                        if (st is not None
                                and st.get("locked_until") == payload.get(
                                    "locked_until")):
                            st["expired_reported"] = False
                return
        # 成功：claim 保持置位（已在收集时完成）


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
    RE-GATE-01：新键插入在容量满时计入**共享溢出桶**——活跃键的
    计数永不被容量挤掉（配额安全优先）；溢出桶本身有界（多源
    共享一个窗口，攻击面收窄为聚合限速）。
    """
    _maybe_maintain()
    limit = _limit_for(kind)
    now = _now()
    if limit <= 0:
        return _WINDOW_SECONDS
    ovf_key = (kind, "\x00_overflow")
    with _LOCK:
        bucket_key = (kind, key)
        routed_ovf = False
        if bucket_key not in _rate_windows:
            # AF-GATE-01：曾进溢出桶的键保持粘性（该 kind 的溢出桶
            # 滑空前不建独立桶）；或容量满时新键入溢出桶
            ovf_window = _rate_windows.get(ovf_key)
            sticky = bucket_key in _ovf_members and ovf_window
            if sticky or len(_rate_windows) >= _MAX_TRACKED_KEYS:
                bucket_key = ovf_key
                routed_ovf = True
        window = _rate_windows.setdefault(bucket_key, deque())
        while window and window[0] <= now - _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            wait = _WINDOW_SECONDS - (now - window[0])
            return max(0.5, wait)  # 被拒：不留成员记录（MIN-GATE-02）
        window.append(now)
        if routed_ovf:
            # 实际获准消费才记粘性（记**原始键**——sticky 检查按
            # 原始键）；成员集有界（淘汰最旧）
            _ovf_members[(kind, key)] = now
            while len(_ovf_members) > _MAX_TRACKED_KEYS:
                _ovf_members.popitem(last=False)
    return 0.0
