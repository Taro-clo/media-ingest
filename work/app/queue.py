"""单工作线程串行任务队列（ARCHITECTURE §6.6）。

``create_hard_links`` 在体积变化时会**整份读-改-写**日志文件，两个任务同时跑就会互相覆盖。
因此队列只有**一个**工作线程，任何情况下都不并发调用建链。

任务在 ``submit`` 时即刻 INSERT（``status='queued'``），所以 WebUI 提交后立刻能看到它，
而不是等工作线程轮询到才开始记录。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

from .db import get_conn, now_iso, tx
from .logbuf import logbuf

logger = logging.getLogger("app.queue")

try:  # 避免与 ``queue`` 标准库同名导致的混淆：本模块对外只暴露下列名字。
    from queue import Empty as _Empty  # noqa: F401  (未使用，仅作说明)
    del _Empty
except Exception:  # pragma: no cover
    pass


@dataclass
class TaskSpec:
    """一次任务的结构化描述（库、类型、触发来源、范围、附加载荷）。"""

    library_id: int | None
    kind: str
    trigger: str
    scope: dict
    payload: dict = field(default_factory=dict)


class TaskQueue:
    """数据库即队列：``tasks.status`` 就是队列状态，工作线程串行消费。"""

    def __init__(self) -> None:
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._running_task_id: int | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动后台工作线程（幂等；daemon 线程随进程退出）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._run, name="mlm-task-worker", daemon=True
            )
            self._worker.start()
            logger.info("任务队列工作线程已启动（单线程串行，A12）")

    def stop(self, timeout: float = 10) -> None:
        """请求停止并等待线程退出；正在执行的任务会跑完当前一次「建链 + 重命名」调用。"""
        self._stop_event.set()
        self._wake.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
        self._worker = None

    # ------------------------------------------------------------------ 入队/取消

    def submit(self, spec: TaskSpec) -> int:
        """把任务写入 ``tasks``（``status='queued'``）并唤醒工作线程，返回 task_id。"""
        library_name = None
        if spec.library_id is not None:
            row = get_conn().execute(
                "SELECT name FROM libraries WHERE id=?", (spec.library_id,)
            ).fetchone()
            if row is not None:
                library_name = row["name"]
            else:
                # 库已被删除：保留 library_id=None 但记录当时的名字（若有），避免悬挂外键。
                library_name = str(spec.payload.get("library_name") or "") or None
                spec = TaskSpec(
                    library_id=None,
                    kind=spec.kind,
                    trigger=spec.trigger,
                    scope=spec.scope,
                    payload=spec.payload,
                )
        stamp = now_iso()
        with tx() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks(library_id, library_name, kind, trigger, status, scope, "
                "payload, result, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    spec.library_id,
                    library_name,
                    spec.kind,
                    spec.trigger,
                    "queued",
                    json.dumps(spec.scope or {}, ensure_ascii=False),
                    json.dumps(spec.payload or {}, ensure_ascii=False),
                    "{}",
                    stamp,
                ),
            )
            task_id = int(cursor.lastrowid)
        logbuf.add_log(task_id, f"任务已入队（类型={spec.kind}，触发={spec.trigger}）", "INFO")
        logbuf.emit_task(task_id, "queued", library_id=spec.library_id)
        self._wake.set()
        return task_id

    def cancel(self, task_id: int) -> bool:
        """取消一个**尚未开始**的任务；``running`` 状态不可中断（串行队列无取消点）。"""
        with tx() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["status"] != "queued":
                return False
            conn.execute(
                "UPDATE tasks SET status='cancelled', finished_at=?, error=? WHERE id=?",
                (now_iso(), "已由用户在开始执行前取消。", task_id),
            )
        logbuf.add_log(task_id, "任务已取消（尚未开始执行）。", "WARNING")
        logbuf.emit_task(task_id, "cancelled")
        return True

    # ------------------------------------------------------------------ 状态查询

    @property
    def running_task_id(self) -> int | None:
        """当前正在执行的任务 id（``None`` 表示空闲）。"""
        return self._running_task_id

    def queued_count(self) -> int:
        """排队中的任务数。"""
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='queued'"
        ).fetchone()
        return int(row["n"]) if row else 0

    def wake(self) -> None:
        """手动唤醒工作线程（扫描线程发现新目标时用，避免等轮询周期）。"""
        self._wake.set()

    # ------------------------------------------------------------------ 工作循环

    def _next_task_id(self) -> int | None:
        """取最早入队的任务（FIFO，保证同一目录的处理顺序可预期）。"""
        row = get_conn().execute(
            "SELECT id FROM tasks WHERE status='queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None

    def _run(self) -> None:
        """工作线程主循环：取一个、执行一个、再取下一个。"""
        from .executor import execute_task

        while not self._stop_event.is_set():
            task_id = self._next_task_id()
            if task_id is None:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            self._running_task_id = task_id
            try:
                execute_task(task_id)
            except Exception as exc:  # 执行器自身的意外错误也不能让线程死掉
                logger.exception("任务 %s 执行器抛出未处理异常", task_id)
                try:
                    with tx() as conn:
                        conn.execute(
                            "UPDATE tasks SET status='failed', finished_at=?, error=? WHERE id=?",
                            (now_iso(), f"执行器内部错误：{type(exc).__name__}: {exc}", task_id),
                        )
                    logbuf.add_log(
                        task_id, f"执行器内部错误：{type(exc).__name__}: {exc}", "ERROR"
                    )
                    logbuf.emit_task(task_id, "failed")
                except Exception:  # pragma: no cover - 最后兜底
                    logger.exception("回写任务 %s 失败状态时再次出错", task_id)
            finally:
                self._running_task_id = None
        logger.info("任务队列工作线程已退出")


queue = TaskQueue()
