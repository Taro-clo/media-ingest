"""``/api/stats/overview``（ARCHITECTURE §5.3）。

仪表盘唯一的聚合接口，刻意做成**一次查询搞定**：首页每次刷新都会调它，
拆成多个 HTTP 请求只会让"打开就转圈"更容易发生。

``recent`` 是裸数组（§5.0 的例外之一），直接给最近的 ``Task`` 对象，
前端点一下就能进详情，不用再回查一次列表。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import security
from ..db import get_conn, get_setting, hours_ago_iso
from ..serializers import RECENT_TASKS_LIMIT, task_dict
from .common import clamp_limit

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/overview")
def overview(
    _: str = Depends(security.require_user),
    recent_limit: int = RECENT_TASKS_LIMIT,
) -> dict:
    """任务/媒体库/队列的总览。"""
    limit = clamp_limit(recent_limit)
    conn = get_conn()
    since = hours_ago_iso(24)

    def _count(sql: str, params: tuple = ()) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row["c"]) if row else 0

    tasks = {
        "queued": _count("SELECT COUNT(*) AS c FROM tasks WHERE status='queued'"),
        "running": _count("SELECT COUNT(*) AS c FROM tasks WHERE status='running'"),
        # 24h 窗口按 finished_at 算：用户关心的是"最近一天跑完的结果"，
        # 而不是"最近一天创建的任务"（排队很久的失败任务不该算进今天）。
        "success_24h": _count(
            "SELECT COUNT(*) AS c FROM tasks WHERE status='success' AND finished_at >= ?", (since,)
        ),
        "failed_24h": _count(
            "SELECT COUNT(*) AS c FROM tasks WHERE status='failed' AND finished_at >= ?", (since,)
        ),
        "total": _count("SELECT COUNT(*) AS c FROM tasks"),
    }
    libraries = {
        "total": _count("SELECT COUNT(*) AS c FROM libraries"),
        "enabled": _count("SELECT COUNT(*) AS c FROM libraries WHERE enabled=1"),
    }
    recent_rows = conn.execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()

    from ..queue import queue

    return {
        "tasks": tasks,
        "libraries": libraries,
        "recent": [task_dict(row) for row in recent_rows],
        "queue": {
            "running_task_id": queue.running_task_id,
            "queued_count": int(tasks["queued"]),
        },
        # 附加项（不在 §5.3 表格里，但仪表盘的 qB 状态灯需要）：由 scanner 每 5 个
        # 周期刷新一次写进 settings。取值 "ok" / "error" / null（从未探测过）。
        "qbittorrent": {
            "status": (get_setting("qbittorrent_last_status") or None),
            "version": (get_setting("qbittorrent_version") or None),
        },
    }
