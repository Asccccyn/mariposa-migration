"""安全（§18）与并发/恢复（§23.1 Integration）。"""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from mariposa import db
from mariposa.app import app
from mariposa.capabilities import registry
from mariposa.identity import service as identity
from mariposa.maintenance import service as maintenance
from mariposa.media import service as media
from mariposa.memory import service as memory
from mariposa.migration import apply_from_report, dry_run
from tests.conftest import TOKENS, reset_all


@pytest.fixture()
def c(actors):
    with TestClient(app) as client:
        yield client


def auth(pid):
    return {"Authorization": f"Bearer {TOKENS[pid]}"}


class TestSecurity:
    def test_xss_payload_is_data_not_html(self, c):
        """存储型内容按 JSON 返回，Content-Type 不触发 HTML 解析。"""
        payload = {"arguments": {"text": "<img src=x onerror=alert(1)>药水",
                                 "original_title": "XSS 载荷桶",
                                 "memory_date": "2026-06-01", "date_confidence": "exact", "raw_pending": False,
                                 "categories": ["daily"]}}
        r = c.post("/api/capability/memory.hold", json=payload,
                   headers=auth("jiaming"))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        mid = r.json()["data"]["memory_id"]
        r2 = c.post("/api/capability/memory.get",
                    json={"arguments": {"memory_id": mid}}, headers=auth("qiaosheng"))
        assert "<img" in r2.json()["data"]["text"]  # 原样存储；渲染层转义
        assert r2.headers["content-type"].startswith("application/json")

    def test_media_path_traversal_inert(self, c):
        r = c.get("/api/media/object/..%2F..%2Fwindows%2Fwin.ini",
                  headers=auth("qiaosheng"))
        assert r.status_code == 404  # hash 查库不存在即 404，无文件拼接

    def test_media_endpoints_require_auth(self, c):
        r = c.put("/api/media/stage/anytoken", content=b"x")
        assert r.status_code == 401
        r2 = c.get("/api/media/object/abc")
        assert r2.status_code == 401

    def test_stage_size_mismatch(self, c):
        prep = c.post("/api/capability/media.upload.prepare",
                      json={"arguments": {"mime": "image/png", "size": 10}},
                      headers=auth("qiaosheng")).json()["data"]
        r = c.put(f"/api/media/stage/{prep['upload_token']}", content=b"12345",
                  headers=auth("qiaosheng"))
        assert r.status_code == 403  # 字节数不符被拒（FORBIDDEN）

    def test_cross_principal_idempotency_isolated(self, actors):
        """幂等键按 (principal, capability, key) 隔离：不同主体同 key 各自生效。"""
        args = {"text": "幂等隔离测试", "original_title": "幂等隔离",
                "memory_date": "2026-06-01", "date_confidence": "exact", "raw_pending": False, "categories": ["daily"]}
        a = registry.invoke(actors["jiaming"], "memory.hold", args, "same-key")
        b = registry.invoke(actors["qiaosheng"], "memory.hold", args, "same-key")
        ida, idb = a["data"]["memory_id"], b["data"]["memory_id"]
        assert ida != idb  # 两个独立桶，不互相重放


