"""复审 UI/docs/脚本收尾回归（RA-011/030）。RA-029 为 README 静态更新。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "fixtures" / "ra011_deletion_ui.cjs"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node 不可用：离线 JS 验证无法执行")
def test_deletion_ui_v2_contract():
    r = subprocess.run(["node", str(_SCRIPT)], capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["v2_contract"] is True, \
        "默认页删除申请必须按 v2.0 合同回调"


def test_verify_script_structured_on_zero_candidates():
    """RA-030：验证脚本 0 候选时结构化通过而非 IndexError。"""
    import os
    import tempfile
    env = dict(os.environ,
               MARIPOSA_ROOT=tempfile.mkdtemp(prefix="vr-"),
               MARIPOSA_ALLOW_CREATE="1",
               MARIPOSA_RECALL_ENABLED="1",
               MARIPOSA_WORDS_RECALL_ENABLED="1",
               MARIPOSA_RAW_FALLBACK_ENABLED="1",
               MARIPOSA_RECALL_JUDGE_PROVIDER="disabled")
    r = subprocess.run(
        ["./.venv/bin/python", "scripts/verify_recall_runtime.py"],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(Path(__file__).parents[2]))
    assert "IndexError" not in r.stderr, \
        f"0 候选不得未捕获 IndexError：{r.stderr[-400:]}"
    assert "Traceback" not in r.stderr or r.returncode != 1, \
        r.stderr[-400:]
