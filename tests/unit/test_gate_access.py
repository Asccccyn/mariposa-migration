"""门禁三件套之一：应用层限速 + 认证失败锁定（2026-10-04）。

语义回归：同来源连续认证失败 → 锁定（正确 token 也拒，防在线
枚举）+ 指数升级 + 成功清零 + audit 留痕；限速按 principal 分读/
写两档 + 匿名按 IP；HTTP 与 MCP 同语义；CF-Connecting-IP 只在
可信代理形态采信。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    return {"jiaming": "tok-j"}  # 只需要 token 值；主体由服务解析


def _client():
    from fastapi.testclient import TestClient
    from mariposa.app import app
    return TestClient(app)


AUTH_J = {"Authorization": f"Bearer {TOKENS['jiaming']}"}
AUTH_BAD = {"Authorization": "Bearer wrong-token"}


class TestAuthLockout:

    def test_five_failures_lock_even_valid_token(self, actors):
        with _client() as c:
            for _ in range(5):
                r = c.get("/api/capabilities", headers=AUTH_BAD)
                assert r.status_code == 401
            # 第 6 次：来源已锁——正确 token 也不放行（防在线枚举）
            r = c.get("/api/capabilities", headers=AUTH_J)
            assert r.status_code == 423
            assert r.json()["error"]["code"] == "AUTH_LOCKED"
            assert r.json()["error"]["detail"]["retry_after"] > 0

    def test_lockout_recorded_in_audit(self, actors):
        from mariposa import db
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
        with db.formal() as conn:
            rows = conn.execute(
                "SELECT event_type, resource_id FROM audit_events WHERE"
                " event_type='auth.locked'").fetchall()
        assert rows, "锁定必须留审计痕迹（谁被锁、锁多久）"
        assert rows[-1]["resource_id"]  # 来源 IP

    def test_success_resets_failure_count(self, actors):
        with _client() as c:
            for _ in range(4):
                c.get("/api/capabilities", headers=AUTH_BAD)
            assert c.get("/api/capabilities",
                         headers=AUTH_J).status_code == 200
            # 成功清零：再失败 4 次不应触发锁定（阈值 5）
            for _ in range(4):
                c.get("/api/capabilities", headers=AUTH_BAD)
            assert c.get("/api/capabilities",
                         headers=AUTH_J).status_code == 200

    def test_lock_level_escalates_exponentially(self, actors):
        from mariposa import gate
        from mariposa import config as cfg
        old = cfg.GATE_LOCKOUT_BASE_SECONDS
        cfg.GATE_LOCKOUT_BASE_SECONDS = 1  # 秒级便于断言升级
        try:
            for _ in range(5):
                gate.note_auth_failure("9.9.9.9")
            lvl1 = gate._auth_failures["9.9.9.9"]["lock_level"]
            for _ in range(5):
                gate.note_auth_failure("9.9.9.9")
            lvl2 = gate._auth_failures["9.9.9.9"]["lock_level"]
            assert (lvl1, lvl2) == (1, 2), "继续失败必须升级锁定层级"
        finally:
            cfg.GATE_LOCKOUT_BASE_SECONDS = old
            gate.reset_for_tests()

    def test_mcp_same_lockout_semantics(self, actors):
        with _client() as c:
            for _ in range(5):
                c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"},
                       headers=AUTH_BAD)
            r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"},
                       headers=AUTH_J)
            assert r.status_code == 423
            assert "AUTH_LOCKED" in r.text


class TestRateLimits:

    def test_read_limit_429(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 3)
        with _client() as c:
            codes = [c.get("/api/capabilities",
                           headers=AUTH_J).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429 and codes[4] == 429

    def test_write_limit_tighter_and_read_unaffected(self, actors,
                                                     monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_WRITE_PER_MIN", 2)
        with _client() as c:
            codes = [c.post("/api/capability/memory.hold",
                            json={"arguments": {"text": "t"}},
                            headers=AUTH_J).status_code for _ in range(4)]
        # 写档超限 429（业务/schema 错误码在前几次出现不算失败——
        # 断言只针对限速：最后两次必须是 429）
        assert codes[2] == 429 and codes[3] == 429, codes
        # 读档不受写档挤占
        with _client() as c:
            assert c.get("/api/capabilities",
                         headers=AUTH_J).status_code == 200

    def test_anonymous_limit_before_auth(self, actors, monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 3)
        with _client() as c:
            codes = [c.get("/api/capabilities").status_code
                     for _ in range(5)]
        assert codes[:3] == [401, 401, 401]
        assert codes[3] == 429, "匿名打点必须先于认证失败计数被限速"

    def test_reset_for_tests_clears_state(self, actors):
        from mariposa import gate
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
        gate.reset_for_tests()
        with _client() as c:
            assert c.get("/api/capabilities",
                         headers=AUTH_J).status_code == 200


class TestClientIPResolution:

    @staticmethod
    def _req(peer, headers):
        return SimpleNamespace(client=SimpleNamespace(host=peer),
                               headers=headers)

    def test_trusted_proxy_uses_cf_header(self):
        from mariposa import gate
        req = self._req("127.0.0.1", {"CF-Connecting-IP": "1.2.3.4"})
        assert gate.client_ip(req) == "1.2.3.4"

    def test_public_peer_ignores_spoofed_headers(self):
        from mariposa import gate
        req = self._req("203.0.113.9", {"CF-Connecting-IP": "1.2.3.4",
                                        "X-Forwarded-For": "6.6.6.6"})
        # 公网直连：可伪造头不参与，按真实 socket 地址计数
        assert gate.client_ip(req) == "203.0.113.9"

    def test_xff_fallback_when_loopback_without_cf(self):
        # P3（2026-10-05 审计）：取最后一项（可信代理追加位）——首项
        # 可由客户端自带 XFF 伪造轮换假 IP 逃失败锁定
        from mariposa import gate
        req = self._req("127.0.0.1", {"X-Forwarded-For": "5.5.5.5, 1.1.1.1"})
        assert gate.client_ip(req) == "1.1.1.1"


class TestREGATE01WindowQuotaPreserved:

    def test_maintain_keeps_in_window_counts(self, monkeypatch):
        """RE-GATE-01：回收不得清掉窗口内仍有效的计数（旧实现按
        最老时间戳删整个 deque，提前释放配额）。"""
        from mariposa import gate
        from mariposa import config as cfg
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 3)
        gate.check_rate("read", "p1")             # t0
        fake_now[0] += 59
        gate.check_rate("read", "p1")             # t59
        gate.check_rate("read", "p1")             # t59（满 3）
        assert gate.check_rate("read", "p1") > 0  # 第 4 次超限
        fake_now[0] += 1                          # t60：触发维护
        gate._maintain()
        # 复审反例（旧实现）：t60 能"再计 3 次"（维护删整窗提前放
        # 配额，5 次全过）。正确行为：t0 恰满 60s 合法滑出，窗口
        # 剩 t59×2 → t60 只能再放 1 次，第 2 次即超限
        assert gate.check_rate("read", "p1") == 0
        assert gate.check_rate("read", "p1") > 0, \
            "窗口内有效计数不得被维护整窗清掉（t60 不得重新放 3 次）"

    def test_capacity_overflow_does_not_evict_active(self, monkeypatch):
        """RE-GATE-01：容量压力下活跃 principal 窗口不被其他来源
        挤掉（新键走溢出桶）。"""
        from mariposa import gate
        monkeypatch.setattr(gate, "_MAX_TRACKED_KEYS", 3)
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        # 先建 principal-a 的活跃窗口（2 次计数），再施容量压力
        gate.check_rate("read", "principal-a")
        gate.check_rate("read", "principal-a")
        gate.check_rate("anon", "10.0.0.2")
        gate.check_rate("anon", "10.0.0.3")
        # 新来源键超容 → 溢出桶；principal-a 的键与计数不受影响
        assert gate.check_rate("anon", "10.0.0.4") == 0
        assert ("read", "principal-a") in gate._rate_windows, \
            "容量压力不得挤掉既有主体的活跃窗口"
        assert len(gate._rate_windows[("read", "principal-a")]) == 2, \
            "主体计数不得被容量压力清掉"


class TestREGATE02LockExpirySurvivesMaintain:

    def test_uncosumed_expiry_not_reclaimed_by_other_sources(
            self, actors, monkeypatch):
        """RE-GATE-02：锁定到期后，其他来源触发的维护不得回收该
        状态（TTL 锚 = max(last_fail, locked_until)）；来源下次请求
        仍能产出一次性 auth.lock.expired 审计。"""
        from mariposa import db, gate
        from mariposa import config as cfg
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        monkeypatch.setattr(cfg, "GATE_LOCKOUT_BASE_SECONDS", 10)
        gate.reset_for_tests()
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
            fake_now[0] += 11  # 锁到期
            # 其他来源触发限速 → _maintain 跑过
            gate.check_rate("anon", "8.8.8.8")
            gate._maintain()
            st = gate._auth_failures.get("testclient")
            assert st is not None, "到期未消费状态不得被维护回收"
            # 原来源成功认证 → 一次性到期事件落审计
            r = c.get("/api/capabilities", headers=AUTH_J)
            assert r.status_code == 200
        with db.formal() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE"
                " event_type='auth.lock.expired'").fetchone()["c"]
        assert n == 1


class TestREGATE03MCPMetaMethods:

    def test_initialize_and_unknown_method_rate_limited(self, actors,
                                                        monkeypatch):
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 1)
        with _client() as c:
            first = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                         "method": "initialize"},
                           headers=AUTH_J)
            second = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                          "method": "initialize"},
                            headers=AUTH_J)
            unknown = c.post("/mcp", json={"jsonrpc": "2.0", "id": 3,
                                           "method": "no/such"},
                             headers=AUTH_J)
        assert first.status_code == 200
        assert second.status_code == 429, "initialize 必须计读档"
        assert unknown.status_code == 429, "未知 method 必须计读档"


class TestREGATE04SubsecondConsistency:

    def test_lock_subsecond_retry_after_consistent(self, actors,
                                                   monkeypatch):
        """RE-GATE-04：亚秒锁定剩余（如 0.4s）时 body 的
        retry_after、message 与 Retry-After 头必须是同一个 ceil 值
        （≥1），不再出现 body=0 / header=1 分裂。"""
        from mariposa import gate
        from mariposa import config as cfg
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        monkeypatch.setattr(cfg, "GATE_LOCKOUT_BASE_SECONDS", 1)
        gate.reset_for_tests()
        with _client() as c:
            for _ in range(5):
                c.get("/api/capabilities", headers=AUTH_BAD)
            fake_now[0] += 0.6  # 锁还剩 0.4s
            r = c.get("/api/capabilities", headers=AUTH_J)
        assert r.status_code == 423
        header = int(r.headers["Retry-After"])
        detail = r.json()["error"]["detail"]["retry_after"]
        assert header == detail == 1, \
            f"Retry-After({header}) 与 detail({detail}) 必须同为 ceil≥1"
        assert "约 1 秒" in r.json()["error"]["message"]


class TestFifthRoundMetering:
    """五轮复审：单请求单次计档 + 锁内 claim + 有界成员集。"""

    def test_wrong_profile_single_charge(self, actors, monkeypatch):
        """AF-GATE-02：meta 计档与 profile 拒绝共享一次（原双扣）。"""
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_READ_PER_MIN", 2)
        with _client() as c:
            codes = [c.post("/mcp/maintenance", json={
                "jsonrpc": "2.0", "id": i, "method": "initialize"},
                headers={"Authorization":
                         f"Bearer {TOKENS['worker']}"}).status_code
                for i in range(3)]
        assert codes == [200, 200, 429], \
            f"一次请求只扣一次读档：{codes}"

    def test_unmanaged_404_single_charge(self, actors, monkeypatch):
        """AF-GATE-02：middleware 预检计过则 404 不补扣（原双扣）。"""
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 2)
        with _client() as c:
            codes = [c.get("/apiX").status_code for _ in range(3)]
        assert codes == [404, 404, 429], \
            f"预检+补计只算一次：{codes}"

    def test_managed_trailing_slash_307_metered(self, actors,
                                                monkeypatch):
        """AF-GATE-02：managed 前缀的 307 重定向纳入匿名档补计。"""
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 2)
        with _client() as c:
            codes = [c.get("/api/capabilities/",
                           follow_redirects=False).status_code
                     for _ in range(3)]
        assert codes[0] == 307
        assert codes[2] == 429, "307 打点必须计档"

    def test_concurrent_maintain_single_claim(self, monkeypatch):
        """MIN-GATE-01：两个维护并发收集同一到期周期，只一个消费。"""
        import threading
        from mariposa import gate
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        sink_calls = []

        def sink(events):
            sink_calls.append(len(events))

        gate.set_expiry_sink(sink)
        for _ in range(5):
            gate.note_auth_failure("10.5.0.1")
        fake_now[0] += 7 * 86400
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()
            gate._maintain()

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start(); t2.start(); t1.join(); t2.join()
        total = sum(sink_calls)
        assert total == 1, f"同一锁周期只允许一个消费者：{sink_calls}"

    def test_denied_source_leaves_no_member(self, monkeypatch):
        """MIN-GATE-02：被拒（超限）的来源不留粘性成员；放行才记。"""
        from mariposa import gate
        gate.reset_for_tests()  # 前序用例的桶/成员残留隔离
        fake_now = [1000.0]
        monkeypatch.setattr(gate, "_now", lambda: fake_now[0])
        monkeypatch.setattr(gate, "_MAX_TRACKED_KEYS", 3)
        from mariposa import config as cfg
        monkeypatch.setattr(cfg, "GATE_RATE_ANON_PER_MIN", 1)
        gate.check_rate("anon", "10.4.0.1")
        gate.check_rate("anon", "10.4.0.2")
        gate.check_rate("anon", "10.4.0.3")     # 容量满
        assert gate.check_rate("anon", "10.4.0.4") == 0   # 放行→记成员
        assert gate.check_rate("anon", "10.4.0.5") > 0    # 溢出桶满被拒
        assert ("anon", "10.4.0.5") not in gate._ovf_members, \
            "被拒来源不得留成员记录"
        assert ("anon", "10.4.0.4") in gate._ovf_members, \
            "放行消费的来源记录粘性成员（原始键）"
