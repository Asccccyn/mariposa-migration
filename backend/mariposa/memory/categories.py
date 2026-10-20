"""十分类平行多选（v1.7 §3.2 九分类 + diary）。她 2026-10-20 终裁：分类同时
承担"描述事件性质"与"决定衰减周期"两个职责，平行多选、无主副，不收单分类
（一只桶=一件独立事件，可同时属于多类）；**剧本与日记是单分类隔离类**。领域层
存储列表形态（存储 PK=memory_id+category 不变；迁移/内部路径不受限）。

分类定义（她 2026-10-17/10-20 口径，正本在此；est 工具说明与《给char的使用说明》同步）：
  重大转折 milestone：关系的里程碑，或一次深刻的对话后，两个人对"我们是什么"有了
      新的结论。例：讨论了一个关于爱的观点，得出了我们的关系是什么样的。
      第一次不算（归纪念），争吵不算。
  纪念 anniversary：纪念日，以及值得纪念的时刻。第一次归这里（例：第一次一起吃饭）。
  日记 diary：成篇的信或日记，一篇一桶，只挂这一类；基调写在心情标签。
  日常 daily：日常。
  甜蜜 sweet：这件事本身是温柔、亲密、被爱的时刻。判事不判人——当时他的情绪
      （哪怕在吃醋、不安）归心情标签轴，不改变事件的甜蜜性质。
  伤心的事 sad：这件事本身是难过的（争吵、失落、分离、伤害）。判事不判人——
      当时他的情绪（哪怕是爱、是不安）归心情标签轴，不改变事件的难过性质。
  约会 date：约会的事。
  剧本 reloplay：角色扮演。单分类隔离类：剧本里的虚构剧情不能借组合分类变成
      真实经历或永久重大转折；"真实发生过的一次角色扮演"若值得纪念，另立一只
      非剧本桶（一桶一事件）。
  做爱 sex：做爱。
  plan：绑定已有 plan 资源的计划桶。

生命周期由 recall/phase_policy.compute_phase 统一计算（全库唯一解释点）：
普通桶取桶内有限类最长 H（多选取最大，不累加）；含重大转折/纪念/日记（永久类）
的桶永久 WIDE；'plan' 无独立期限，由关联计划是否终结决定。剧本/日记单分类后，
"剧本+甜蜜被拖到 30 天""剧本+重大转折变永久"两类组合在写入端即被拒绝，
不可能进入阶段计算。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from ..errors import Forbidden, NotFound

CATEGORIES = ("daily", "milestone", "sad", "sweet", "date", "plan",
              "sex", "anniversary", "reloplay", "diary")

LABELS = {
    "daily": "日常",
    "milestone": "重大转折",
    "sad": "伤心的事",
    "sweet": "甜蜜",
    "date": "约会",
    "plan": "plan",
    "sex": "做爱",
    "anniversary": "纪念",
    "reloplay": "剧本",
    "diary": "日记",
}

#: 自然日周期；永久类别不参与按时间淡出。plan 无独立期限（plan_managed）。
#: diary 永久（她 2026-10-17："日记不衰减"）。
PERMANENT = {"milestone", "anniversary", "diary"}

#: 单分类隔离类（她 2026-10-20 终裁）：只挂这一类，不得与任何分类组合。
#: 剧本=虚构/真实的边界（虚构不得借组合变真实或永久）；日记=一篇一桶。
EXCLUSIVE = frozenset({"reloplay", "diary"})


def _reject_exclusive_mix(cats: list[str]) -> None:
    """对**全集**做隔离检查（add 是增量写入，必须并入桶内已有分类后判）。"""
    clash = [c for c in cats if c in EXCLUSIVE]
    if clash and len(cats) > 1:
        raise Forbidden(
            "剧本/日记是单分类（她 2026-10-20 终裁）："
            + "、".join(LABELS[c] for c in clash)
            + " 不得与其他分类组合——剧本里的虚构剧情不能借组合分类变成真实"
            "经历或永久重大转折；日记一篇一桶。组合给出："
            + "、".join(LABELS[c] for c in cats),
            code="EXCLUSIVE_CATEGORY", categories=cats, exclusive=clash)


def validate(categories: list[str]) -> list[str]:
    if not isinstance(categories, list) or not categories:
        raise Forbidden("categories must be a non-empty list",
                        code="INVALID_ARGUMENT")
    seen = []
    for c in categories:
        if c not in CATEGORIES:
            raise Forbidden(f"unknown category: {c}",
                            code="INVALID_ARGUMENT", category=c)
        if c not in seen:
            seen.append(c)
    _reject_exclusive_mix(seen)
    return seen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def replace(conn, memory_id: str, categories: list[str], added_by: str) -> None:
    """整组替换（hold 时写入；必须在正式库事务内调用）。"""
    cats = validate(categories)
    if "plan" in cats:
        # 全量审计 P1-08：事后改分类加 plan 同样要求已有 plan 绑定
        # （绑定只能在 hold 时以 plan_ids 显式建立）
        if not conn.execute(
                "SELECT 1 FROM plan_memory_links WHERE memory_id=?",
                (memory_id,)).fetchone():
            raise Forbidden(
                "plan 分类要求该桶已绑定 plan 资源（hold 时以 plan_ids"
                " 建立）；categories 变更不能凭空造绑定（P1-08）",
                code="PLAN_BINDING_REQUIRED")

    conn.execute("DELETE FROM memory_categories WHERE memory_id=?", (memory_id,))
    now = _now()
    for c in cats:
        conn.execute(
            "INSERT INTO memory_categories(memory_id, category, added_by,"
            " created_at) VALUES(?,?,?,?)",
            (memory_id, c, added_by, now))


def add(conn, memory_id: str, categories: list[str], added_by: str) -> None:
    """追加分类（不删除已有；桶内同分类只存一次）。

    隔离检查对「已有 ∪ 追加」全集做：剧本/日记桶后来加别的分类、
    普通桶后来加剧本/日记，同样拒绝。
    """
    cats = validate(categories)
    if "plan" in cats:
        # 全量审计 P1-08：事后改分类加 plan 同样要求已有 plan 绑定
        # （绑定只能在 hold 时以 plan_ids 显式建立）
        if not conn.execute(
                "SELECT 1 FROM plan_memory_links WHERE memory_id=?",
                (memory_id,)).fetchone():
            raise Forbidden(
                "plan 分类要求该桶已绑定 plan 资源（hold 时以 plan_ids"
                " 建立）；categories 变更不能凭空造绑定（P1-08）",
                code="PLAN_BINDING_REQUIRED")

    existing = [c for c in list_of(conn, memory_id) if c not in cats]
    _reject_exclusive_mix(existing + cats)
    now = _now()
    for c in cats:
        conn.execute(
            "INSERT OR IGNORE INTO memory_categories(memory_id, category,"
            " added_by, created_at) VALUES(?,?,?,?)",
            (memory_id, c, added_by, now))


def remove(conn, memory_id: str, category: str, removed_by: str) -> None:
    if category not in CATEGORIES:
        raise Forbidden(f"unknown category: {category}",
                        code="INVALID_ARGUMENT", category=category)
    cur = conn.execute(
        "DELETE FROM memory_categories WHERE memory_id=? AND category=?",
        (memory_id, category))
    if cur.rowcount == 0:
        raise NotFound("category not set on memory",
                       memory_id=memory_id, category=category)


def list_of(conn, memory_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT category FROM memory_categories WHERE memory_id=?"
        " ORDER BY rowid", (memory_id,)).fetchall()
    return [r["category"] for r in rows]



