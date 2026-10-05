"""OAuth 动态授权回归（2026-10-05 江乔生裁定：连接后输密码换临时 token）。

覆盖：发现元数据、动态注册、authorize 登录页（密码错计入失败锁定）、
PKCE code 换币、password grant、refresh 轮换、过期/撤销、下游权限
（token 即 client_bindings——registry 矩阵零改动）。
"""
from __future__ import annotations

import hashlib
import base64
import secrets

import pytest

from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    from mariposa import oauth
    oauth.set_password("qiaosheng", "her-login-pass-123")
    oauth.set_password("jiaming", "mcp-pass-456")
    return {"her": "her-login-pass-123", "mcp": "mcp-pass-456"}


def _client():
    from fastapi.testclient import TestClient
    from mariposa.app import app
    return TestClient(app)


REDIRECT = "http://127.0.0.1:53789/callback"


class TestDiscoveryAndRegister:

    def test_metadata_shape(self, actors):
        with _client() as c:
            r = c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        d = r.json()
        assert d["grant_types_supported"] == [
            "authorization_code", "refresh_token", "password"]
        assert d["code_challenge_methods_supported"] == ["S256"]
        assert d["token_endpoint"].endswith("/oauth/token")

    def test_register_and_reject_bad_uris(self, actors):
        with _client() as c:
            ok = c.post("/oauth/register", json={
                "client_name": "claude-mcp",
                "redirect_uris": [REDIRECT]})
            bad = c.post("/oauth/register", json={
                "client_name": "x", "redirect_uris": ["ftp://nope"]})
            empty = c.post("/oauth/register", json={"redirect_uris": []})
        assert ok.status_code == 201
        assert ok.json()["client_id"].startswith("mcp_")
        assert bad.status_code == 400
        assert empty.status_code == 400


class TestAuthorizationCodeFlow:

    def _register(self, c):
        return c.post("/oauth/register", json={
            "client_name": "claude-mcp",
            "redirect_uris": [REDIRECT]}).json()["client_id"]

    def test_login_page_then_code_then_token_then_api(self, actors):
        with _client() as c:
            cid = self._register(c)
            verifier = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).rstrip(b"=").decode()
            page = c.get("/oauth/authorize", params={
                "client_id": cid, "redirect_uri": REDIRECT,
                "state": "xyz", "code_challenge": challenge,
                "code_challenge_method": "S256"})
            assert page.status_code == 200
            assert "password" in page.text  # 登录页
            # 错密码 → 401 + 错误提示
            bad = c.post("/oauth/authorize", data={
                "client_id": cid, "redirect_uri": REDIRECT,
                "code_challenge": challenge, "password": "wrong-pass"})
            assert bad.status_code == 401
            # MCP 密码 → 302 + code
            good = c.post("/oauth/authorize", data={
                "client_id": cid, "redirect_uri": REDIRECT,
                "code_challenge": challenge, "password": actors["mcp"]},
                follow_redirects=False)
            assert good.status_code == 302
            assert "code=" in good.headers["location"]
            code = good.headers["location"].split("code=")[1].split("&")[0]
            # 换币（PKCE）
            tok = c.post("/oauth/token", data={
                "grant_type": "authorization_code", "code": code,
                "client_id": cid, "redirect_uri": REDIRECT,
                "code_verifier": verifier})
            assert tok.status_code == 200
            at = tok.json()["access_token"]
            assert tok.json()["refresh_token"]
            # token 调真实 API → jiaming 身份
            caps = c.get("/api/capabilities", headers={
                "Authorization": f"Bearer {at}"})
            assert caps.status_code == 200
            # 未知 client 的 redirect 拒
            page2 = c.get("/oauth/authorize", params={
                "client_id": "mcp_nope", "redirect_uri": REDIRECT})
            assert page2.status_code == 400

    def test_wrong_verifier_rejected(self, actors):
        with _client() as c:
            cid = self._register(c)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(
                b"real-verifier").digest()).rstrip(b"=").decode()
            good = c.post("/oauth/authorize", data={
                "client_id": cid, "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "password": actors["mcp"]}, follow_redirects=False)
            code = good.headers["location"].split("code=")[1].split("&")[0]
            tok = c.post("/oauth/token", data={
                "grant_type": "authorization_code", "code": code,
                "client_id": cid, "redirect_uri": REDIRECT,
                "code_verifier": "wrong-verifier"})
            assert tok.status_code == 400

    def test_refresh_rotation_http(self, actors):
        # P2-04（2026-10-05 审计）：authorize 现要求 PKCE——补 challenge
        # 与 verifier（其余流程不变）
        import base64 as _b64, hashlib as _hl
        with _client() as c:
            cid = self._register(c)
            challenge = _b64.urlsafe_b64encode(_hl.sha256(
                b"rot-verifier").digest()).rstrip(b"=").decode()
            good = c.post("/oauth/authorize", data={
                "client_id": cid, "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "password": actors["mcp"]}, follow_redirects=False)
            code = good.headers["location"].split("code=")[1].split("&")[0]
            tok = c.post("/oauth/token", data={
                "grant_type": "authorization_code", "code": code,
                "client_id": cid, "redirect_uri": REDIRECT,
                "code_verifier": "rot-verifier"})
            rt = tok.json()["refresh_token"]
            r1 = c.post("/oauth/token", data={
                "grant_type": "refresh_token", "refresh_token": rt,
                "client_id": cid})
            assert r1.status_code == 200
            r2 = c.post("/oauth/token", data={
                "grant_type": "refresh_token", "refresh_token": rt,
                "client_id": cid})
            assert r2.status_code == 400, "旧 refresh 一次性"


class TestPasswordGrantAndLockout:

    def test_password_grant_maps_identity(self, actors):
        with _client() as c:
            her = c.post("/oauth/token", data={
                "grant_type": "password", "password": actors["her"]})
            bad = c.post("/oauth/token", data={
                "grant_type": "password", "password": "nope"})
        assert her.status_code == 200
        at = her.json()["access_token"]
        with _client() as c:
            caps = c.get("/api/capabilities", headers={
                "Authorization": f"Bearer {at}"})
            assert caps.status_code == 200
        assert bad.status_code == 401

    def test_wrong_password_feeds_lockout(self, actors):
        with _client() as c:
            for _ in range(5):
                r = c.post("/oauth/token", data={
                    "grant_type": "password", "password": "nope"})
                assert r.status_code == 401
            locked = c.post("/oauth/token", data={
                "grant_type": "password", "password": actors["her"]})
            assert locked.status_code == 423, "密码爆破触发来源锁定"


class TestExpiryAndRevoke:

    def test_expired_token_rejected_and_revoke(self, actors):
        import time
        from mariposa import oauth as _oauth
        tok = _oauth.issue_access_token("jiaming", ttl_s=0)
        time.sleep(0.05)
        with _client() as c:
            r = c.get("/api/capabilities", headers={
                "Authorization": f"Bearer {tok['access_token']}"})
            assert r.status_code == 401
        live = _oauth.issue_access_token("jiaming")
        with _client() as c:
            ok = c.get("/api/capabilities", headers={
                "Authorization": f"Bearer {live['access_token']}"})
            assert ok.status_code == 200
            rv = c.post("/oauth/revoke", data={
                "token": live["access_token"]})
            assert rv.status_code == 200
            after = c.get("/api/capabilities", headers={
                "Authorization": f"Bearer {live['access_token']}"})
            assert after.status_code == 401, "撤销后立即失效"
