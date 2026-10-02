"""召回质量评测入口（§14 / P5）：A/B 对照与分层指标计算。

只读评测：不修改正式库、不外发数据、不启用任何外部 provider。
评测集为 JSONL（tests/fixtures/recall_eval/ 或显式路径），每行：
  {"qid": "...", "request": "...", "gold_resource_refs": ["memory:..."],
   "query_plan": {...}, "notes": "..."}

用法：
  MARIPOSA_ROOT=$(mktemp -d) MARIPOSA_ALLOW_CREATE=1 \
    python scripts/eval_recall.py --dataset path/to/eval.jsonl [--groups A,B]

A=现状 keyword-only（retrieval.search/recall）；B=同 scope hybrid 证据包
（recall_service.start）。输出 Hit@1/Hit@3/MRR/Recall@10 与分桶计数；
真实 Jev（C 组）与外部数据授权未获准前不执行，只留接口。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def enforce_isolated_root(prefix: str) -> str:
    """A10：强制隔离根——评测脚本绝不继承服务/开发根，也绝不对它 seed。

    外部显式传入的 MARIPOSA_ROOT 一律拒绝（含符号链接解析后指向
    服务根的情况）；脚本自建随机临时根。真实库只读评测请走独立的
    只读连接入口，不经本脚本。
    """
    external = os.environ.get("MARIPOSA_ROOT")
    if external:
        real = os.path.realpath(external)
        print(f"REFUSED: MARIPOSA_ROOT={external} (realpath={real})："
              "评测脚本拒绝继承外部根（A10）；请去掉该环境变量重跑。",
              file=sys.stderr)
        raise SystemExit(2)
    root = tempfile.mkdtemp(prefix=prefix)
    os.environ["MARIPOSA_ROOT"] = root
    return root


def run_group_a(principal, item) -> list[str]:
    """现状基线：旧 keyword-only recall。"""
    from mariposa import db
    from mariposa.retrieval import search as rs
    q = item.get("query_plan", {})
    terms = " ".join(q.get("lexical_terms") or
                     [item.get("request", "")])
    filters = {}
    ec = q.get("explicit_constraints") or {}
    if ec.get("categories"):
        filters["categories"] = ec["categories"]
    if ec.get("event_date"):
        filters["event_date"] = ec["event_date"]
    with db.formal() as conn:
        out = rs.recall(conn, terms, filters, 10, None)
    return [f"memory:{h['memory_id']}" for h in out["hits"]]


def run_group_b(principal, item) -> list[str]:
    """同 scope hybrid 证据包（无 Jev）。"""
    from mariposa.recall import service as rs
    plan = dict(item.get("query_plan") or {})
    plan.setdefault("original_request", item.get("request", ""))
    plan.setdefault("channels", ["event"])
    if not plan.get("lexical_terms") and not plan.get("exact_phrases"):
        plan["lexical_terms"] = [item.get("request", "")]
    packet = rs.start(principal, {"query_plan": plan})
    return [c["resource_ref"] for c in packet["candidates"]]


def metrics(runs: list[tuple[set[str], list[str]]]) -> dict:
    hit1 = hit3 = 0
    rr_sum = 0.0
    recall10 = 0.0
    for gold, ranked in runs:
        if not gold:
            continue
        pos = None
        for i, ref in enumerate(ranked[:10], start=1):
            if ref in gold:
                if pos is None:
                    pos = i
        if pos == 1:
            hit1 += 1
        if pos is not None and pos <= 3:
            hit3 += 1
        if pos is not None:
            rr_sum += 1.0 / pos
        recall10 += (len(set(ranked[:10]) & gold) / len(gold))
    n = len(runs) or 1
    return {"n": len(runs), "Hit@1": round(hit1 / n, 4),
            "Hit@3": round(hit3 / n, 4), "MRR": round(rr_sum / n, 4),
            "Recall@10": round(recall10 / n, 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--groups", default="A,B")
    args = ap.parse_args()

    enforce_isolated_root("mr-eval-")
    os.environ.setdefault("MARIPOSA_ALLOW_CREATE", "1")
    os.environ.setdefault("MARIPOSA_RECALL_ENABLED", "1")
    os.environ.setdefault("MARIPOSA_WORDS_RECALL_ENABLED", "1")

    rows = [json.loads(l) for l in
            Path(args.dataset).read_text(encoding="utf-8").splitlines()
            if l.strip()]
    if not rows:
        print("empty dataset")
        return 1

    # CB-056：隔离根先建库再 seed（此前未 migrate 即 seed/检索）
    from mariposa import schema as _schema
    _schema.migrate()
    _schema.migrate_runtime()
    from mariposa.identity import service as identity
    identity.seed({"qiaosheng": "tok-q", "jiaming": "tok-j",
                   "worker": "tok-w"})  # CB-056：退役主体移除
    principal = identity.Principal("jiaming", "周", "agent", "cc", "b")

    runners = {"A": run_group_a, "B": run_group_b}
    results: dict[str, dict] = {}
    for g in args.groups.split(","):
        g = g.strip().upper()
        if g == "C":
            print("C（+Jev）：真实 provider 未授权，跳过（接口保留）")
            continue
        runs = [(set(r.get("gold_resource_refs") or []),
                 runners[g](principal, r)) for r in rows]
        results[g] = metrics(runs)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
