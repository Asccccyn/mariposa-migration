"""类型化查询编译与 AllowedScope（v1.4 §4.3/§5.2）。

两种清楚的模式：`terms`（多个独立检索词，可非连续命中）与 `phrase`
（明确逐字片段，保持顺序）。只有类型化计划能生成 OR/AND 组合；用户
输入先经字符级分词与引号消毒，任何 FTS 语法字符都失去特殊含义，
不能把全句编成必须连续出现的单一 phrase。
"""
from __future__ import annotations

from ..errors import Forbidden
from . import projection


def _quote_token(tok: str) -> str:
    return '"' + tok.replace('"', "") + '"'


def compile_terms(terms: list[str]) -> str:
    """多词 terms → OR 组合的安全 FTS5 表达式（任一词命中即可）。"""
    exprs = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        toks = [t.replace('"', "").lower() for t in projection.tokenize(term)]
        toks = [t for t in toks if t]
        if not toks:
            continue
        # 单个 term 内部按 phrase 序列（FTS5 相邻短语隐式 AND，保持顺序）；
        # term 之间 OR。
        exprs.append(" ".join(_quote_token(t) for t in toks))
    if not exprs:
        return ""
    return " OR ".join(exprs)


def compile_phrase(phrase: str) -> str:
    """逐字片段 → 单一 phrase（顺序敏感）。"""
    return projection.compile_query(phrase)


def compile_plan_lexical(plan: dict) -> str:
    """QueryPlan → FTS 表达式：exact_phrases(AND) + terms(OR 组)。

    exact phrase 之间 AND（都要出现）；terms 组 OR 任一命中；两类同时
    存在时 phrase 为必须项、terms 为任选项。空表达式返回 ""（浏览模式）。
    """
    and_parts = []
    for p in plan.get("exact_phrases") or []:
        c = compile_phrase(p)
        if c:
            and_parts.append(c)
    terms_expr = compile_terms(plan.get("lexical_terms") or [])
    if terms_expr:
        and_parts.append(f"({terms_expr})")
    return " AND ".join(and_parts)


class AllowedScope:
    """同一过滤范围对象：约束 SQL 池、向量评分与 Top-K（v1.4 §5.2）。

    过滤必须发生在 Top-K 之前——不能向量先全库 Top-5 再过滤日期。
    复用 search._pool_where 的结构化筛选（分类/心情/日期），并叠加
    session 排除集合与负向日期条件。
    """

    def __init__(self, where: list[str], params: list, rejected: set[str],
                 plan: dict):
        self.where = list(where)
        self.params = list(params)
        self.rejected = rejected
        self.plan = plan

    @classmethod
    def for_plan(cls, plan: dict, rejected: set[str] | None = None) -> tuple:
        """从 QueryPlan 的明确条件构建（event 通道白名单字段）。"""
        from . import search as search_mod

        ec = dict(plan.get("explicit_constraints") or {})
        filters: dict = {}
        if ec.get("categories"):
            filters["categories"] = ec["categories"]
        if ec.get("mood_tags"):
            filters["mood_tags"] = ec["mood_tags"]
        if ec.get("event_date"):
            filters["event_date"] = ec["event_date"]
        where, params = search_mod._pool_where(filters)

        neg = plan.get("explicit_negative_constraints") or {}
        for key in ("event_date_excluded",):
            for rng in (neg.get(key) or []):
                if not isinstance(rng, dict):
                    continue
                if rng.get("from"):
                    where.append("m.memory_date NOT BETWEEN ? AND ?")
                    params += [rng["from"], rng.get("to", "9999-12-31")]
        cats_ex = neg.get("categories_excluded") or []
        if cats_ex:
            marks = ",".join("?" * len(cats_ex))
            where.append(
                f"m.memory_id NOT IN (SELECT memory_id FROM memory_categories"
                f" WHERE category IN ({marks}))")
            params += cats_ex
        return where, params, (rejected or set()), plan


def source_scope(plan: dict) -> dict:
    """三轮复审#4：words/raw 通道统一的正/负说话人与日期条件。

    一套语义三处复用（words 稀疏、words dense、raw Round2），不再各写
    一套：正向 speaker / date(from,to) 硬过滤，负向 speaker_excluded /
    source_date_excluded({from,to} 区间) 同样硬过滤。
    """
    ec = plan.get("explicit_constraints") or {}
    neg = plan.get("explicit_negative_constraints") or {}
    dr = ec.get("source_date") or ec.get("event_date")
    dr = dr if isinstance(dr, dict) else {}
    return {
        "speaker": ec.get("speaker"),
        "speakers_excluded": [s for s in (neg.get("speaker_excluded") or [])
                              if isinstance(s, str) and s],
        "date_from": dr.get("from"),
        "date_to": dr.get("to"),
        "date_ranges_excluded": [
            r for r in (neg.get("source_date_excluded")
                        or neg.get("event_date_excluded") or [])
            if isinstance(r, dict) and r.get("from")],
    }


def source_scope_sql(scope: dict, speaker_col: str,
                     date_col: str) -> tuple[list[str], list]:
    """source_scope → 说话人/日期列上的过滤 SQL（words 两侧与 raw 同构）。

    date_col 上 NULL 安全：日期缺失的行不被负向区间误伤（无日期 =
    不在任何排除区间内，不等于命中区间）。
    """
    where: list[str] = []
    params: list = []
    if scope.get("speaker"):
        where.append(f"{speaker_col}=?")
        params.append(scope["speaker"])
    sp_ex = scope.get("speakers_excluded") or []
    if sp_ex:
        marks = ",".join("?" * len(sp_ex))
        where.append(f"{speaker_col} NOT IN ({marks})")
        params += sp_ex
    if scope.get("date_from"):
        where.append(f"{date_col} >= ?")
        params.append(scope["date_from"])
    if scope.get("date_to"):
        where.append(f"{date_col} <= ?")
        params.append(scope["date_to"])
    for rng in scope.get("date_ranges_excluded") or []:
        where.append(f"({date_col} IS NULL OR"
                     f" {date_col} NOT BETWEEN ? AND ?)")
        params += [rng["from"], rng.get("to", "9999-12-31")]
    return where, params


def plan_token_groups(plan: dict) -> tuple[list[list[str]], list[list[str]]]:
    """QueryPlan → (terms token 组, phrases token 组)。

    terms 组内 token 相邻（词内顺序）、组间 OR；phrases 组间 AND、组内
    逐字顺序（HYBRID-04）。两组都空而原始请求非空时，把原始请求整句
    作为 phrase 尝试（不凭空造词）。
    """
    terms: list[list[str]] = []
    for t in plan.get("lexical_terms") or []:
        toks = [x.lower() for x in projection.tokenize(t) if x]
        if toks:
            terms.append(toks)
    phrases: list[list[str]] = []
    for p in plan.get("exact_phrases") or []:
        toks = [x.lower() for x in projection.tokenize(p) if x]
        if toks:
            phrases.append(toks)
    # 两组都空、原始请求非空、且没有任何明确条件时才把整句当 phrase
    # 尝试（有明确条件 = 浏览场景，不构造词面表达，HYBRID-09）。
    if (not terms and not phrases and plan.get("original_request")
            and not plan.get("explicit_constraints")):
        toks = [x.lower() for x in projection.tokenize(
            plan["original_request"]) if x]
        if toks:
            phrases = [toks]
    return terms, phrases


def validate_fts_inert(user_input: str) -> str:
    """防御性检查：编译结果不含未消毒的语法字符（测试探针用）。"""
    compiled = compile_terms([user_input])
    return compiled
