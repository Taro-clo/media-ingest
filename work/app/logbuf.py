"""任务日志缓冲与 SSE 事件广播。

两个职责：

1. **落库**：所有面向任务的结构化日志都写进 ``task_logs``，前端用
   ``GET /api/tasks/{id}/logs?after_id=`` 增量轮询（ARCHITECTURE §7）。
2. **广播**：任务状态变化与新增日志推给 SSE（``GET /api/tasks/stream``）订阅者。
   前端可降级为轮询，所以这里的订阅者列表用 ``threading.Condition`` 做有界等待，
   断开/超时都不会留下悬挂订阅。

日志行本身**不落 stdout 管道**：原生实现的 ``print()`` 被 ``redirect_stdout`` 捕获后，
由适配层与执行器显式 ``add_log(..., stream="stdout")`` 写入，保证格式统一且可解析。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable

from .db import get_conn, get_setting, now_iso, tx

#: 单任务日志行数上限的**兜底**默认值（真实值读 settings 的 ``max_task_log_lines``）。
DEFAULT_MAX_TASK_LOG_LINES = 5000


class LogBuffer:
    """任务日志写入 + 事件广播（单进程，内存态）。"""

    def __init__(self, max_events: int = 2000) -> None:
        self._condition = threading.Condition()
        self._events: deque[dict] = deque(maxlen=max_events)
        self._sequence = 0
        #: task_id -> 已写入 ``task_logs`` 的行数（首次写入时从库里捞一次真实值）。
        self._written: dict[int, int] = {}
        #: task_id -> 是否已经写过「日志已截断」那条提示，保证只写一次。
        self._truncation_notice: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 截断

    def _limit(self) -> int:
        """单任务日志上限（§5.0 第 3 条，默认 5000）。非法配置回退默认值。"""
        raw = get_setting("max_task_log_lines", str(DEFAULT_MAX_TASK_LOG_LINES))
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return DEFAULT_MAX_TASK_LOG_LINES
        return value if value > 0 else DEFAULT_MAX_TASK_LOG_LINES

    def _reserve(self, task_id: int, count: int) -> tuple[int, bool]:
        """申请 ``count`` 行配额，返回 ``(可写行数, 是否需要写截断提示)``。

        为什么在内存里计数而不是每次 ``SELECT COUNT(*)``：一个任务可能写入上千行日志，
        逐行查库会把 sqlite 变成瓶颈；首次从库里校准一次即可（进程重启后计数自然重置，
        重启场景下最多多写一点点，不会失控）。

        截断提示本身只写一次，并且**不占配额**（它是在配额用尽之后才写的那一条），
        否则提示会把自己也截掉，用户永远看不到原因。
        """
        limit = self._limit()
        with self._lock:
            if task_id not in self._written:
                row = get_conn().execute(
                    "SELECT COUNT(*) AS c FROM task_logs WHERE task_id=?", (task_id,)
                ).fetchone()
                self._written[task_id] = int(row["c"]) if row is not None else 0
            used = self._written[task_id]
            granted = min(count, max(limit - used, 0))
            self._written[task_id] = used + granted
            if granted >= count:
                return granted, False
            if task_id in self._truncation_notice:
                # 已经提示过就不再重复（否则每次超限调用都追加一条，反而撑爆库）。
                return granted, False
            self._truncation_notice.add(task_id)
            return granted, True

    def truncate_notice(self, task_id: int) -> int:
        """写入「日志已截断」提示。返回日志行 id。"""
        message = (
            "本任务日志已达上限（设置项 max_task_log_lines），后续日志不再写入数据库；"
            "任务本身的执行不受影响，完整的逐条明细请查看应用日志文件。"
        )
        stamp = now_iso()
        with tx() as conn:
            cursor = conn.execute(
                "INSERT INTO task_logs(task_id, ts, level, message, stream) VALUES(?,?,?,?,?)",
                (task_id, stamp, "warning", message, "app"),
            )
            log_id = int(cursor.lastrowid)
        self.emit_log(task_id, "warning", message, "app")
        return log_id

    def reset_counters(self, task_id: int | None = None) -> None:
        """清掉内存计数（删除任务历史后调用，避免内存里留着已删除任务的条目）。"""
        with self._lock:
            if task_id is None:
                self._written.clear()
                self._truncation_notice.clear()
            else:
                self._written.pop(task_id, None)
                self._truncation_notice.discard(task_id)

    # ------------------------------------------------------------------ 事件

    def _publish(self, event_type: str, data: dict) -> dict:
        with self._condition:
            self._sequence += 1
            event = {"seq": self._sequence, "event": event_type, **data}
            self._events.append(event)
            self._condition.notify_all()
        return event

    def emit_task(self, task_id: int, status: str, **extra) -> dict:
        """广播一次任务状态变更（SSE ``event: task``）。"""
        payload = {"task_id": task_id, "status": status, "ts": now_iso()}
        payload.update(extra)
        return self._publish("task", payload)

    def emit_log(self, task_id: int, level: str, message: str, stream: str | None = None) -> dict:
        """广播一条新日志（SSE ``event: log``）。"""
        return self._publish(
            "log",
            {
                "task_id": task_id,
                "level": level,
                "message": message,
                "stream": stream,
                "ts": now_iso(),
            },
        )

    def since(self, sequence: int, limit: int = 200) -> list[dict]:
        """取 ``sequence`` 之后的事件（SSE 断线重连补发用）。"""
        with self._condition:
            events = [event for event in self._events if event["seq"] > sequence]
        return events[:limit]

    def wait_for_events(self, sequence: int, timeout: float = 15.0) -> list[dict]:
        """有界等待新事件；超时返回空列表（让 SSE 生成器能发心跳并检查断连）。"""
        with self._condition:
            latest = self._events[-1]["seq"] if self._events else 0
            if latest <= sequence:
                self._condition.wait(timeout=timeout)
            return [event for event in self._events if event["seq"] > sequence]

    # ------------------------------------------------------------------ 落库

    def add_log(
        self,
        task_id: int,
        message: str,
        level: str = "INFO",
        stream: str | None = None,
    ) -> int:
        """写一条任务日志并广播；返回日志行 id（被截断时返回 0）。"""
        if task_id is None:
            return 0
        granted, need_notice = self._reserve(task_id, 1)
        if need_notice:
            self.truncate_notice(task_id)
        if granted <= 0:
            return 0
        stamp = now_iso()
        with tx() as conn:
            cursor = conn.execute(
                "INSERT INTO task_logs(task_id, ts, level, message, stream) VALUES(?,?,?,?,?)",
                (task_id, stamp, level, message, stream),
            )
            log_id = int(cursor.lastrowid)
        self.emit_log(task_id, level, message, stream)
        return log_id

    def add_logs(self, task_id: int, rows: Iterable[tuple[str, str, str | None]]) -> int:
        """批量写日志，``rows`` 为 ``(message, level, stream)``；返回实际写入条数。"""
        rows = list(rows)
        if not rows or task_id is None:
            return 0
        granted, need_notice = self._reserve(task_id, len(rows))
        if need_notice:
            self.truncate_notice(task_id)
        if granted <= 0:
            return 0
        accepted = rows[:granted]
        stamp = now_iso()
        with tx() as conn:
            conn.executemany(
                "INSERT INTO task_logs(task_id, ts, level, message, stream) VALUES(?,?,?,?,?)",
                [(task_id, stamp, level, message, stream) for message, level, stream in accepted],
            )
        for message, level, stream in accepted:
            self.emit_log(task_id, level, message, stream)
        return len(accepted)

    def logs(self, task_id: int, after_id: int = 0, limit: int = 500) -> list[dict]:
        """增量读日志（轮询入口）。

        取出的行**带 ``task_id``**：``GET /api/tasks/{id}/logs``（docs/contracts/API.md §4.4）
        的每一项都要回显它，SSE 的 ``event: log`` 载荷也复用同一份 dict。
        """
        rows = get_conn().execute(
            "SELECT id, task_id, ts, level, message, stream FROM task_logs "
            "WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (task_id, after_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


logbuf = LogBuffer()
