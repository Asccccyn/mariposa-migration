"""OAuth 请求体与同步工作边界：不使用真实凭据、模型或慢密码哈希。"""
from __future__ import annotations

import asyncio
import importlib
import threading

import httpx
import pytest
from starlette.requests import Request

web = importlib.import_module("mariposa.app")


def _request(chunks, headers=()):
    read = []
    iterator = iter(chunks)

    async def receive():
        chunk = next(iterator)
        read.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    return Request({"type": "http", "method": "POST", "path": "/oauth/token",
                    "headers": list(headers), "query_string": b"",
                    "client": ("127.0.0.1", 1234)}, receive), read


@pytest.mark.parametrize("handler", [web.oauth_register, web.oauth_token,
                                     web.oauth_authorize_post, web.oauth_revoke])
def test_oauth_chunked_body_stops_at_limit(monkeypatch, handler):
    monkeypatch.setattr(web, "_OAUTH_BODY_MAX_BYTES", 64)
    request, read = _request([b"x" * 32] * 5)
    response = asyncio.run(handler(request))
    assert response.status_code == 413
    assert b"BODY_TOO_LARGE" in response.body
    assert len(read) == 3, "超限后不能继续读剩余字节"


def test_oauth_declared_oversize_rejected_before_read(monkeypatch):
    monkeypatch.setattr(web, "_OAUTH_BODY_MAX_BYTES", 64)
    request, read = _request([], [(b"content-length", b"65")])
    response = asyncio.run(web.oauth_token(request))
    assert response.status_code == 413
    assert read == []


def test_password_verification_does_not_block_health(monkeypatch):
    from mariposa import gate
    gate.reset_for_tests()
    entered, release = threading.Event(), threading.Event()

    def blocked_password(password):
        entered.set()
        assert release.wait(2), "测试必须在健康请求完成后释放工作线程"
        return "jiaming"

    monkeypatch.setattr(web._oauth, "verify_password", blocked_password)
    monkeypatch.setattr(web._oauth, "issue_access_token",
                        lambda *a, **kw: {"access_token": "synthetic"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                     base_url="http://test") as client:
            login = asyncio.create_task(client.post("/oauth/token", data={
                "grant_type": "password", "password": "synthetic"}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                health = await asyncio.wait_for(client.get("/health"), .75)
                assert health.status_code == 200
                assert not login.done(), "健康端点必须在慢哈希未结束时完成"
            finally:
                release.set()
                response = await login
            assert response.status_code == 200

    asyncio.run(scenario())


def test_oauth_sync_work_has_four_worker_bound(monkeypatch):
    release = threading.Event()
    lock = threading.Lock()
    active = peak = started = 0

    def blocked_handler(request, form):
        nonlocal active, peak, started
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
        try:
            assert release.wait(2)
            return {"ok": True}
        finally:
            with lock:
                active -= 1

    async def empty_form(request):
        return {}

    monkeypatch.setattr(web, "_oauth_token_handle", blocked_handler)
    monkeypatch.setattr(web, "_oauth_form_body", empty_form)

    async def scenario():
        tasks = [asyncio.create_task(web.oauth_token(None)) for _ in range(7)]
        try:
            for _ in range(100):
                if started == 4:
                    break
                await asyncio.sleep(.005)
            assert started == 4
            assert peak == 4
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert started == 7
        assert peak <= 4

    asyncio.run(scenario())


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/capability/time.now"),
    ("PUT", "/api/media/stage/synthetic"),
    ("PUT", "/api/source/upload"),
])
def test_async_route_authentication_yields_loop(monkeypatch, method, path):
    from mariposa import gate
    from mariposa.errors import MariposaError
    gate.reset_for_tests()
    entered, release = threading.Event(), threading.Event()

    def blocked_auth(request):
        entered.set()
        assert release.wait(2)
        raise MariposaError("synthetic rejected", http_status=401)

    monkeypatch.setattr(web, "_authenticate_tracked", blocked_auth)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                     base_url="http://test") as client:
            pending = asyncio.create_task(client.request(method, path))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                response = await asyncio.wait_for(client.get("/health"), .75)
                assert response.status_code == 200
                assert not pending.done()
            finally:
                release.set()
                result = await pending
            assert result.status_code == 401

    asyncio.run(scenario())
