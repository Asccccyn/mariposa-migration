"""v2 计划终结语义（P3）：V2-PLAN-01/02/03/05/08/09/10/11 + 到期队列。

S4 已确认：完成/放弃后固定 20 自然日；打开/阅读/刷新不续期、不改
terminal_revision；只有明确状态更新回活跃（重启执行）才取消旧周期。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mariposa import db
from mariposa.identity import service as identity
from mariposa import biztime as ret_mod
from mariposa.plans import service as plans
from tests.conftest import reset_all


@pytest.fixture()
def actors():
    reset_all()
    return {
        "qiaosheng": identity.Principal("qiaosheng", "江乔生", "human", "web", "bq"),
        "jiaming": identity.Principal("jiaming", "周家明", "agent",
                                      "claude_chat", "bj"),
        "worker": identity.Principal("worker", "维护工具人", "agent",
                                     "gpt_chat", "bw"),
    }


def _due_of(terminal_date_iso: str) -> str:
    return (datetime.fromisoformat(terminal_date_iso)
            + timedelta(days=20)).date().isoformat()


