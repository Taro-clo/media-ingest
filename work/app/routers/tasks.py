"""``/api/tasks/*``（ARCHITECTURE §4 与 §5.1）。

三条容易写错的语义，这里明确固定下来：

- ``retry`` 只允许 ``failed`` / ``cancelled``（否则 409）。``success``/``skipped`` 不许重试，
  **不是**小心眼：成功任务重试会把整轮处理再跑一遍，虽然硬链接层面幂等，
  但会让任务历史里出现两条一模一样的成功记录，掩盖真正的失败。
- ``cancel`` 只对 ``queued`` 生效。``running`` 返回 409 —— 工作线程正在 os.link，
  中途打断只会留下半套硬链接，用户还得自己收拾；要停止请等它跑完。
- ``logs`` 用 ``after_id`` 递增拉取，返回 ``last_id`` 让前端原样回传（§5.3）。
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from .. import security
from ..db import get_conn
from ..logbuf import logbuf
from ..queue import TaskSpec, queue
from ..serializers import get_task_row, log_dict, normalize_kind, task_dict
from .common import bad_request, clamp_limit, clamp_offset, conflict, not_found

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

#: 允许出现在 ``status`` 过滤参数里的状态。
VALID_STATUSES = ("queued", "running", "success", "failed", "skipped", "cancelled")
#: 允许重试的状态（§5.1）。
RETRYABLE_STATUSES = ("failed", "cancelled")

SSE_HEARTBEAT_SECONDS = 15.0


@router.get("")
def list_tasks(
    _: str = Depends(security.require_user),
    status: str | None = Query(default=None),
    library_id: int | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
) -> dict:
    """任务列表（信封格式；``total`` 是**过滤后**总数，§5.0）。"""
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    conditions: list[str] = []
    params: list = []
    if status:
        if status not in VALID_STATUSES:
            raise bad_request(
                f"未知的任务状态：{status}。可选值：" + "、".join(VALID_STATUSES) + "。"
            )
        conditions.append("status=?")
        params.append(status)
    if library_id is not None:
        conditions.append("library_id=?")
        params.append(int(library_id))
    if q:
        # §5.0 第 2 条：对 library_name / error 做**大小写不敏感**子串匹配（OR）。
        # SQLite 的 LIKE 默认只对 ASCII 大小写不敏感，因此显式 LOWER() 两端，
        # 让中文/混合场景行为一致、也便于测试断言。
        keyword = f"%{q.strip().lower()}%"
        conditions.append(
            "(LOWER(COALESCE(library_name,'')) LIKE ? OR LOWER(COALESCE(error,'')) LIKE ?)"
        )
        params.extend([keyword, keyword])
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = get_conn()
    total = int(
        conn.execute(f"SELECT COUNT(*) AS c FROM tasks{where}", params).fetchone()["c"]
    )
    rows = conn.execute(
        f"SELECT * FROM tasks{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "items": [task_dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{task_id}")
def get_task(task_id: int, _: str = Depends(security.require_user)) -> dict:
    """任务详情。"""
    row = get_task_row(task_id)
    if row is None:
        raise not_found(f"任务 {task_id} 不存在。")
    return task_dict(row)


@router.get("/{task_id}/logs")
def get_task_logs(
    task_id: int,
    _: str = Depends(security.require_user),
    after_id: int = Query(default=0),
    limit: int = Query(default=500),
) -> dict:
    """增量拉取任务日志（§5.3）。

    ``after_id`` 是**日志主键**而不是时间戳：任务日志在同一毫秒内可能写多条，
    用时间戳做游标会漏行或重复。
    """
    row = get_task_row(task_id)
    if row is None:
        raise not_found(f"任务 {task_id} 不存在。")
    limit = min(max(int(limit or 500), 1), 2000)
    try:
        after = max(int(after_id or 0), 0)
    except (TypeError, ValueError):
        after = 0
    items = [
        log_dict(item)
        for item in logbuf.logs(task_id, after_id=after, limit=limit)
    ]
    last_id = items[-1]["id"] if items else after
    return {"items": items, "last_id": last_id, "task_id": task_id}


@router.get("/{task_id}/stream")
def stream_task(
    task_id: int, request: Request, _: str = Depends(security.require_user)
) -> StreamingResponse:
    """SSE 实时流（§5.3）：``event: task`` 推送状态变化，``event: log`` 推送新日志行。

    只在**内存环形缓冲**（``logbuf``）上做长轮询式等待，不轮询数据库：
    任务线程写日志时直接广播，SSE 端 ``wait_for_events`` 被唤醒，
    所以即使一个任务一秒内写几百行日志也不会有查询风暴。
    """
    row = get_task_row(task_id)
    if row is None:
        raise not_found(f"任务 {task_id} 不存在。")

    def event_source():
        # 先补发一次当前状态，再补发已有日志的尾部，避免前端订阅到一个"空白"任务。
        cursor = 0
        yield _sse("task", task_dict(get_task_row(task_id)))
        backlog = logbuf.logs(task_id, after_id=cursor, limit=500)
        for item in backlog:
            cursor = item["id"]
            yield _sse("log", log_dict(item))
        last_keepalive = time.monotonic()
        while True:
            fresh = [
                e for e in logbuf.wait_for_events(0, timeout=1.0)
                if _event_task_id(e) == task_id
            ]
            for event in fresh:
                cursor = max(cursor, int(event.get("data", {}).get("id", 0) or 0))
                yield _sse(event["type"], event["data"])
            if fresh:
                continue
            if time.monotonic() - last_keepalive >= SSE_HEARTBEAT_SECONDS:
                # 注释行做心跳：有些反代会把长时间静默的连接掐掉。
                last_keepalive = time.monotonic()
                yield ": keepalive\n\n"
            current = get_task_row(task_id)
            if current is not None and current["status"] in (
                "success",
                "failed",
                "skipped",
                "cancelled",
            ):
                # 终态后把已写入但尚未推送的日志补完，然后结束——前端收到 done 即可停订。
                for item in logbuf.logs(task_id, after_id=cursor, limit=1000):
                    cursor = item["id"]
                    yield _sse("log", log_dict(item))
                yield _sse("done", {"task_id": task_id, "status": current["status"]})
                break

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{task_id}/retry", status_code=202)
def retry_task(task_id: int, _: str = Depends(security.require_user)) -> dict:
    """重试失败/已取消的任务（202 + Task）。"""
    row = get_task_row(task_id)
    if row is None:
        raise not_found(f"任务 {task_id} 不存在。")
    if row["status"] not in RETRYABLE_STATUSES:
        raise conflict(
            f"只有失败或已取消的任务可以重试，当前状态为 {row['status']}。"
            f"如需重新执行该媒体库，请使用「手动执行」。"
        )
    library_id = row["library_id"]
    if library_id is None:
        raise conflict("原任务的媒体库已被删除，无法重试。请重新创建媒体库后手动运行。")
    scope = _json_loads(row["scope"], {})
    payload = _json_loads(row["payload"], {})
    # 重试 = 把那次调用**原样**再排队一次：类型直接沿用原任务的 kind（入队时
    # 就由处理器的 kind 决定），不要在这里重新猜处理器/枚举。
    kind = normalize_kind(row["kind"]) or "hardlink"
    processor_id = (payload.get("processor_id") or payload.get("processor") or "").strip()
    new_id = queue.submit(
        TaskSpec(
            library_id=int(library_id),
            kind=kind,
            trigger="retry",
            scope=scope or {"mode": "full"},
            payload={
                "library_name": row["library_name"],
                "processor_id": processor_id,
                "force": bool(payload.get("force")),
                "retry_of": task_id,
            },
        )
    )
    return task_dict(get_task_row(new_id))


@router.post("/{task_id}/cancel", status_code=204)
def cancel_task(task_id: int, _: str = Depends(security.require_user)):
    """取消仍在排队的任务（204）。运行中的任务不可取消（409）。"""
    row = get_task_row(task_id)
    if row is None:
        raise not_found(f"任务 {task_id} 不存在。")
    if row["status"] == "running":
        raise conflict(
            "任务正在执行中，无法取消。硬链接创建到一半中断会留下不完整的目录，"
            "请等待本次执行结束（队列是串行的，取消它也不会让下一个任务更早开始）。"
        )
    if row["status"] != "queued":
        raise conflict(f"只有排队中的任务可以取消，当前状态为 {row['status']}。")
    if not queue.cancel(task_id):
        raise conflict("取消失败：该任务可能已经开始执行，请刷新后查看最新状态。")
    return None


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: int, _: str = Depends(security.require_user)):
    """删除单条任务历史（204）。日志随外键级联删除。

    **只删记录，不动文件系统**：目标目录里的硬链接与源目录里的下载都保持原样。
    """
    row = get_task_row(task_id)
    if row is None:
        raise not_found(f"任务 {task_id} 不存在。")
    if row["status"] == "running":
        raise conflict("任务正在执行中，无法删除其历史记录。")
    if row["status"] == "queued" and queue.running_task_id != task_id:
        # 排队中的任务还在队列里，直接删行会让工作线程拿到一个不存在的任务。
        queue.cancel(task_id)
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    # 内存里的日志计数是按键累积的，历史删掉后必须同步清掉，
    # 否则同一 id 被新任务复用时（虽然 AUTOINCREMENT 不会复用，但手工导库可能会）会立刻被截断。
    logbuf.reset_counters(task_id)
    return None


def _json_loads(raw, fallback):
    try:
        value = json.loads(raw) if raw else fallback
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _sse(event: str, data) -> str:
    """按 SSE 规范拼一条事件（``data`` 单行 JSON，避免多行 data 被拼接语义坑到）。"""
    import json as _json

    return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _event_task_id(event: dict) -> int | None:
    """从 logbuf 事件里取出 task_id（两种事件都带）。"""
    data = event.get("data") or {}
    try:
        return int(data.get("task_id"))
    except (TypeError, ValueError):
        return None

