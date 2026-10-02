"""Media 域审计修复回归（CB-019，2026-10-02 基线审计 P2）。

审计反例 media-dedupe-mime-self-heal：同字节先 image/png 上传、删除
canonical object 后改 image/jpeg 重传——finalize 声称 deduplicated=
true，但 DB 行仍指向缺失的 .png（get_media NOT_FOUND），磁盘另留一个
无 DB 引用的 .jpg。修复：canonical 路径以 DB 行首见身份为准，自愈修
复行所指文件；同 hash 异 MIME 不产生新扩展名身份。
"""
from __future__ import annotations

import hashlib

import pytest

from mariposa import config as cfg
from mariposa.identity import service as identity
from mariposa.media import service as media
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    media._staging.clear()
    return {
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
    }


def _upload(principal: str, data: bytes, mime: str) -> dict:
    prep = media.upload_prepare(principal, mime, len(data))
    media.stage_bytes(principal, prep["upload_token"], data)
    return media.upload_finalize(principal, prep["upload_token"])


class TestDedupeMimeSelfHeal:

    def test_mime_change_repairs_canonical_object(self, actors):
        data = b"\x89PNG\r\n\x1a\n" + b"m" * 64
        h = hashlib.sha256(data).hexdigest()
        out1 = _upload("jiaming", data, "image/png")
        assert out1["deduplicated"] is False
        canonical = cfg.RUNTIME_DIR / "objects" / f"{h}.png"
        assert canonical.exists()

        # 损坏 canonical object，随后同字节改 MIME 重传
        canonical.unlink()
        out2 = _upload("jiaming", data, "image/jpeg")
        assert out2["deduplicated"] is True
        assert out2["mime"] == "image/png", "首见 MIME 身份不漂移"
        # DB 所指对象被真实修复（不是另写一个 .jpg）
        assert canonical.exists(), "自愈必须修复 DB 所指的 canonical 文件"
        assert canonical.read_bytes() == data
        row, path = media.get_media("jiaming", h)
        assert row["storage_key"] == f"{h}.png"
        assert path.name == f"{h}.png"
        # 磁盘不残留无 DB 引用的异 MIME 副本
        assert not (cfg.RUNTIME_DIR / "objects" / f"{h}.jpg").exists()

    def test_same_mime_corrupt_repair_unchanged(self, actors):
        """同 MIME 损坏自愈（既有行为）不回归。"""
        data = b"\x89PNG\r\n\x1a\n" + b"r" * 48
        h = hashlib.sha256(data).hexdigest()
        _upload("jiaming", data, "image/png")
        canonical = cfg.RUNTIME_DIR / "objects" / f"{h}.png"
        canonical.write_bytes(data[:10])  # 半截
        out = _upload("jiaming", data, "image/png")
        assert out["deduplicated"] is True
        assert canonical.read_bytes() == data
        row, _ = media.get_media("jiaming", h)
        assert row["storage_key"] == f"{h}.png"
