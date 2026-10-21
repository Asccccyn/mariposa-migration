"""正文表示解析正本（R09/HA-04.1 拆环）：纯规则零依赖——event_text 优先、
v1 回退 hold_text、forgotten 只认审批摘要。兼容 fallback 只允许在此处。"""
from __future__ import annotations


def version_body(v) -> str | None:
    """当前 memory revision 正文的唯一解析入口（审计 F04/F14 统一收敛）。

    - full 表示：event_text 优先；v1 旧数据（无 event_text / event_text
      为空）回退 hold_text。兼容 fallback 只允许集中在此处实现，
      update/meaning/rebuild/history/检索投影等业务路径一律经本函数
      取正文，不得各自决定来源，也不得让旧 hold_text 覆盖已升级的
      event_text。
    - forgotten_summary 表示：只认审批摘要 compressed_summary。
    """
    if v["representation"] == "forgotten_summary":
        return v["compressed_summary"]
    return v["event_text"] or v["hold_text"]
