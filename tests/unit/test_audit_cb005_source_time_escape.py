"""CB-005（2026-10-02 基线审计 P1）回归：Source 会话/消息时间
metadata 不得作为 HTML 进入默认静态页。

审计反例（证据 source-ui-metadata-html）：会话 created_at 原值
`<b data-audit="metadata">synthetic</b>` 经 renderSourceConv 写入
innerHTML 时保留为可解释标签（存储型注入入口，页面同源）。

修复：srcTime() 统一 esc()——所有消费点都是 HTML 模板插值。
验证方式与审计取证相同：离线 VM 执行当前真实页面 JS（无浏览器/
服务/网络）；node 不可用时显式 skip（不虚报通过）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "fixtures" / "cb005_source_time_escape.cjs"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node 不可用：离线 JS 验证无法执行（显式 skip，不虚报通过）")
def test_source_time_metadata_escaped_in_static_page():
    r = subprocess.run(["node", str(_SCRIPT)], capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["raw_markup_in_innerHTML"] is False, \
        "原始 markup 仍以可解释 HTML 进入 innerHTML"
    assert out["escaped_text_present"] is True, \
        "未找到转义后的文本形式"
