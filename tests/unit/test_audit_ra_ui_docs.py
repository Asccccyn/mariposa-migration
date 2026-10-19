"""复审 UI/docs/脚本收尾回归（RA-011/030）。RA-029 为 README 静态更新。"""
from __future__ import annotations

import subprocess
from pathlib import Path


def test_current_acceptance_evidence_points_to_existing_nodes():
    """验证所有 evidence 路径，包含 Python 之外的新前端测试资产。"""
    import ast
    import json
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "docs/memory_runtime/ACCEPTANCE.json").read_text())
    for requirement in manifest["requirements"]:
        if requirement["status"] == "PASS":
            assert requirement["evidence"], requirement["id"]
        for reference in requirement["evidence"]:
            filename, *names = reference.split("::")
            path = root / filename
            assert path.is_file(), reference
            if path.suffix != ".py":
                continue
            nodes = ast.parse(path.read_text()).body
            for name in names:
                node = next((n for n in nodes if isinstance(
                    n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == name.split("[")[0]), None)
                assert node is not None, reference
                nodes = node.body


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
