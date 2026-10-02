"""Source 域 UI 审计修复回归（CB-023/CB-053，离线 VM 执行当前真实
页面 JS——审计取证同款方式；node 不可用时显式 skip）。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "fixtures" / "cb023_source_ui.cjs"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node 不可用：离线 JS 验证无法执行（显式 skip，不虚报通过）")
def test_source_ui_pagination_direction_and_hold_date():
    r = subprocess.run(["node", str(_SCRIPT)], capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    lines = [json.loads(x) for x in r.stdout.strip().splitlines()
             if x.startswith("{")]
    by_case = {o["case"]: o for o in lines}
    paging = by_case["cb023-ui-pagination-direction"]
    assert paging["used_after_cursor"] is True, \
        "续页必须使用服务端复合游标向后翻"
    assert paging["used_before_seq"] is False, \
        "续页不得再发送 before_seq（方向接反 + 整数游标覆盖不了同序号组）"
    hold = by_case["cb053-ui-hold-date"]
    assert hold["fixed_test_fixture_submitted"] is False, \
        "默认写入不得再提交固定 40 天前的 exact 测试日期"
    assert hold["unknown_when_no_input"] is True, \
        "空日期输入必须提交 unknown/null"
