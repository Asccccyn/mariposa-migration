"""CB-001（2026-10-02 基线审计 P0）验收回归：无效上传 token 不得
产生 staging 目录外的文件删除。

审计反例（证据 media-token-path-absolute / media-token-path-relative）：
已认证 owner 经公开 Registry 调 media.upload.finalize，token 传临时
sentinel 的绝对路径、或相对 staging 的 `..` 路径——文件先被 unlink，
然后才收到 NOT_FOUND（_sentinel_survived=false）。

修复后边界（两层）：
- 服务层：token 清理只由服务端可信 staging 记录驱动；格式不合法
  （含任何路径元字符）或无记录的 token 零文件系统副作用。
- 公开边界：Registry schema 对 upload_token 施加签发字符集 pattern，
  路径形状在入参校验即拒绝。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mariposa import config as cfg
from mariposa.errors import Forbidden, NotFound
from mariposa.identity import service as identity
from mariposa.media import service as media
from tests.conftest import reset_all, TOKENS


@pytest.fixture()
def actors():
    reset_all()
    media._staging.clear()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human",
                                        "web", "binding_qiaosheng"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "binding_jiaming"),
    }


def _sentinel_outside_runtime() -> Path:
    """staging 目录之外的哨兵文件（隔离根的上一级，绝不在 staging 内）。"""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="cb001-sentinel-"))
    p = d / "sentinel.bin"
    p.write_bytes(b"cb001-survivor")
    return p


def _sentinel_in_runtime_root() -> Path:
    """RUNTIME_DIR 根下（staging 的上级目录）——`..` 逃逸的第一跳。"""
    cfg.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    p = cfg.RUNTIME_DIR / "cb001-outside-staging.bin"
    p.write_bytes(b"cb001-survivor")
    return p


class TestFinalizeUnknownTokenNoSideEffect:
    """未知 token 的所有拒绝路径：目录外文件必须原样存活。"""

    def test_absolute_path_token(self, actors):
        s = _sentinel_outside_runtime()
        with pytest.raises(NotFound):
            media.upload_finalize("jiaming", str(s))
        assert s.exists(), "绝对路径 token 不得删除目录外文件"

    def test_relative_traversal_token(self, actors):
        s = _sentinel_in_runtime_root()
        with pytest.raises(NotFound):
            media.upload_finalize("jiaming", "../cb001-outside-staging.bin")
        assert s.exists(), "`..` 相对路径 token 不得删除目录外文件"

    def test_windows_path_forms(self, actors):
        s = _sentinel_in_runtime_root()
        for bad in (r"..\..\cb001-outside-staging.bin",
                    "C:\\Windows\\evil", "\\\\server\\share"):
            with pytest.raises(NotFound):
                media.upload_finalize("jiaming", bad)
        assert s.exists(), "Windows 路径形式不得产生删除或未结构化异常"

    def test_empty_and_short_tokens(self, actors):
        for bad in ("", "x", "a" * 7):
            with pytest.raises(NotFound):
                media.upload_finalize("jiaming", bad)

    def test_wellformed_unknown_token_no_side_effect(self, actors):
        """格式合法但从未签发的 token：穿透到服务层拒绝，无副作用。"""
        s = _sentinel_in_runtime_root()
        unknown = secrets.token_urlsafe(24)
        with pytest.raises(NotFound):
            media.upload_finalize("jiaming", unknown)
        assert s.exists()
        assert (media._staging_dir() / unknown).exists() is False


class TestFinalizeKnownTokenBoundaries:
    """合法 token 生命周期行为不回归（清理仍发生，但只清自己的文件）。"""

    def _prepare_staged(self, principal="jiaming") -> str:
        data = b"\x89PNG\r\n\x1a\n" + b"q" * 128
        prep = media.upload_prepare(principal, "image/png", len(data))
        media.stage_bytes(principal, prep["upload_token"], data)
        return prep["upload_token"]

    def test_expired_token_cleans_only_own_staging_file(self, actors):
        token = self._prepare_staged()
        s = _sentinel_in_runtime_root()
        meta = media._staging[token]
        meta["created"] = (datetime.now(timezone.utc)
                           - timedelta(seconds=media._STAGING_TTL_S * 2)
                           ).isoformat()
        with pytest.raises(NotFound):
            media.upload_finalize("jiaming", token)
        assert not (media._staging_dir() / token).exists(), \
            "过期 token 的可信暂存文件应被清理"
        assert s.exists(), "清理不得越出 staging 目录"
        assert token not in media._staging

    def test_unstaged_token_rejected(self, actors):
        prep = media.upload_prepare("jiaming", "image/png", 64)
        with pytest.raises(NotFound):
            media.upload_finalize("jiaming", prep["upload_token"])
        assert prep["upload_token"] not in media._staging

    def test_other_owner_token_forbidden_keeps_file(self, actors):
        token = self._prepare_staged("jiaming")
        with pytest.raises(Forbidden):
            media.upload_finalize("qiaosheng", token)
        assert (media._staging_dir() / token).exists(), \
            "他人 token 被 403 拒绝时不得作废（MEDIA-02）"
        assert token in media._staging

    def test_symlink_staging_entry_deletes_link_not_target(self, actors):
        """staging 条目被换成指向外部的符号链接：清理只删链接本身。"""
        s = _sentinel_outside_runtime()
        prep = media.upload_prepare("jiaming", "image/png", 4096)
        token = prep["upload_token"]
        link = media._staging_dir() / token
        link.unlink(missing_ok=True)
        link.symlink_to(s)
        # 未 staged → finalize 拒绝并作废 token（清理路径覆盖 symlink）
        with pytest.raises(NotFound):
            media.upload_finalize("jiaming", token)
        assert s.exists(), "unlink 不得跟随符号链接删除目标"
        assert not link.exists()


class TestStageBytesUnknownTokenNoSideEffect:
    """stage 端点同一清理路径：未知 token 零文件系统副作用。"""

    def test_stage_unknown_traversal_token(self, actors):
        s = _sentinel_in_runtime_root()
        with pytest.raises(NotFound):
            media.stage_bytes("jiaming",
                              "../cb001-outside-staging.bin", b"x" * 4)
        assert s.exists()

    def test_stage_unknown_absolute_token(self, actors):
        s = _sentinel_outside_runtime()
        with pytest.raises(NotFound):
            media.stage_bytes("jiaming", str(s), b"x" * 4)
        assert s.exists()


class TestPublicRegistryBoundary:
    """公开 Registry 入口：路径形状在 schema 即拒绝，sentinel 存活。"""

    def test_registry_rejects_absolute_path_token(self, actors):
        from mariposa.capabilities import registry
        s = _sentinel_outside_runtime()
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "media.upload.finalize",
                            {"upload_token": str(s)}, None)
        assert ei.value.code == "SCHEMA_VIOLATION"
        assert s.exists()

    def test_registry_rejects_traversal_token(self, actors):
        from mariposa.capabilities import registry
        s = _sentinel_in_runtime_root()
        with pytest.raises(Forbidden) as ei:
            registry.invoke(actors["jiaming"], "media.upload.finalize",
                            {"upload_token": "../cb001-outside-staging.bin"},
                            None)
        assert ei.value.code == "SCHEMA_VIOLATION"
        assert s.exists()

    def test_registry_unknown_but_wellformed_token_not_found(self, actors):
        from mariposa.capabilities import registry
        s = _sentinel_in_runtime_root()
        unknown = secrets.token_urlsafe(24)
        with pytest.raises(NotFound):
            registry.invoke(actors["jiaming"], "media.upload.finalize",
                            {"upload_token": unknown}, None)
        assert s.exists()

    def test_registry_normal_flow_still_works(self, actors):
        """正路径回归：prepare→stage→finalize 经公开 Registry 完成。"""
        from mariposa.capabilities import registry
        import hashlib
        data = b"\x89PNG\r\n\x1a\n" + b"r" * 96
        prep = registry.invoke(actors["jiaming"], "media.upload.prepare",
                               {"mime": "image/png", "size": len(data)}, None)
        token = prep["data"]["upload_token"]
        from fastapi.testclient import TestClient
        from mariposa.app import app
        with TestClient(app) as c:
            r = c.put(prep["data"]["stage_url"], content=data,
                      headers={"Authorization":
                               f"Bearer {TOKENS['jiaming']}"})
            assert r.status_code == 200
        out = registry.invoke(actors["jiaming"], "media.upload.finalize",
                              {"upload_token": token}, None)
        assert out["data"]["content_hash"] == \
            hashlib.sha256(data).hexdigest()
        assert not (media._staging_dir() / token).exists()
