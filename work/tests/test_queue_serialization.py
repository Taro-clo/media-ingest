"""任务队列：单工作线程、全局串行（A12）、DB 即队列。

A12 的原文是"同一 (source_dir, target_dir) 同时仅一个任务、全局串行"。这两条里**全局串行更
强**，而实现选的正是后者：``TaskQueue`` 只有一个 daemon 工作线程（ARCHITECTURE §6.6），
``_run`` 取一个执行一个。本文件的重点因此是**并发不变量**——

* 任何时刻最多 1 个任务在"执行中"（连重叠窗口都不允许，不只是"不重入"）；
* 任务严格按入队顺序（FIFO）执行；
* 执行期间 ``running_task_id`` 可见、``queued_count()`` 递减；
* 队列线程炸了不许死（执行器抛异常要被兜住、写 failed、继续跑下一个）。

这些都是"看不见的"性质：真出问题时不会报错，只会**偶发**文件损坏（建链在体积变化时整份
读-改-写日志文件），所以必须由测试钉住。
"""

from __future__ import annotations

import json
import threading
import time

import pytest


def _insert_library(db_mod, name: str, source: str = "/downloads/Bangumi",
                    target: str = "/library/Bangumi") -> int:
    """插一条库。

    v2 起 ``processor_id`` 是 NOT NULL 外键（``app/db.py`` 的 ``_LIBRARIES_COLUMNS``），
    旧的 ``processor`` 列保留为 v1 兼容别名；这里两列写同一个值。
    """
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO libraries(name,type,processor,processor_id,enabled,source_dir,target_dir,"
            "match_categories,match_tags,match_path_prefixes,orphan_cleanup,permission_mode,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "Bangumi", "bangumi", "bangumi", 1, source, target, "[]", "[]", "[]", 1, 0,
             stamp, stamp),
        )
        return int(cursor.lastrowid)


def _task_row(db_mod, task_id: int) -> dict:
    row = db_mod.get_conn().execute(
        "SELECT * FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    assert row is not None, f"任务 {task_id} 不存在"
    return dict(row)


def _raw_column(db_mod, task_id: int, column: str) -> str:
    row = db_mod.get_conn().execute(
        f"SELECT {column} AS value FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    return row["value"]


class _Recorder:
    """替换 ``app.executor.execute_task``：记录"谁在什么时候跑"，并制造可观测的重叠窗口。"""

    def __init__(self, queue, db_mod, duration: float = 0.05, boom_on: set[int] | None = None):
        self.queue = queue
        self.db_mod = db_mod
        self.duration = duration
        self.boom_on = set(boom_on or ())
        self.calls: list[dict] = []
        self.inflight = 0
        self.max_inflight = 0
        self.overlaps = 0
        self._lock = threading.Lock()

    def __call__(self, task_id: int) -> None:
        thread = threading.current_thread()
        # 先复刻真执行器的第一步：把任务标成 running（否则 queued_count 会把"正在跑的"也数进去）。
        stamp = self.db_mod.now_iso()
        with self.db_mod.tx() as conn:
            conn.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE id=?", (stamp, task_id)
            )
        with self._lock:
            self.inflight += 1
            if self.inflight > 1:
                self.overlaps += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        record = {
            "task_id": task_id,
            "thread": thread.name,
            "daemon": thread.daemon,
            "running_task_id": self.queue.running_task_id,
            "queued_count": self.queue.queued_count(),
            "status": _task_row(self.db_mod, task_id)["status"],
            "started": time.monotonic(),
        }
        try:
            time.sleep(self.duration)
            if task_id in self.boom_on:
                raise ValueError(f"模拟执行器崩溃 {task_id}")
            stamp = self.db_mod.now_iso()
            with self.db_mod.tx() as conn:
                conn.execute(
                    "UPDATE tasks SET status='success', finished_at=? WHERE id=?",
                    (stamp, task_id),
                )
            record["status_after"] = "success"
        except ValueError:
            raise
        finally:
            record["finished"] = time.monotonic()
            with self._lock:
                self.inflight -= 1
            self.calls.append(record)


@pytest.fixture
def task_queue(queue_mod):
    """每个用例一个**全新**队列实例，用完一定停掉工作线程（避免线程泄漏到下一个用例）。"""
    instance = queue_mod.TaskQueue()
    yield instance
    instance.stop(timeout=5)


@pytest.fixture
def spec(queue_mod):
    def build(library_id=None, kind="hardlink", trigger="manual", scope=None, payload=None):
        return queue_mod.TaskSpec(
            library_id=library_id,
            kind=kind,
            trigger=trigger,
            scope=scope if scope is not None else {"mode": "full"},
            payload=payload or {},
        )
    return build


# --------------------------------------------------------------------------- 入队


def test_submit_persists_a_queued_task_immediately(queue_mod, task_queue, db_mod, spec):
    library_id = _insert_library(db_mod, "番剧库")
    task_id = task_queue.submit(
        spec(library_id, kind="rename_hardlink", trigger="manual", payload={"note": "番剧 A"})
    )

    row = _task_row(db_mod, task_id)
    assert row["status"] == "queued"
    assert row["library_id"] == library_id
    assert row["library_name"] == "番剧库"
    assert row["kind"] == "rename_hardlink"
    assert row["trigger"] == "manual"
    assert row["created_at"], "入队即写 created_at（WebUI 立刻能看到任务）"
    assert row["started_at"] is None and row["finished_at"] is None
    assert json.loads(row["scope"]) == {"mode": "full"}
    assert json.loads(row["payload"]) == {"note": "番剧 A"}
    assert "番剧 A" in _raw_column(db_mod, task_id, "payload"), (
        "JSON 必须 ensure_ascii=False（日志/DB 里要看得到中文原文）"
    )
    assert task_queue.queued_count() == 1

    logs = queue_mod.logbuf.logs(task_id)
    assert logs, "入队必须留下一条任务日志"
    assert "任务已入队" in logs[0]["message"]
    assert logs[0]["task_id"] == task_id


def test_submit_keeps_fifo_order_and_increasing_ids(task_queue, spec):
    first = task_queue.submit(spec(payload={"n": 1}))
    second = task_queue.submit(spec(payload={"n": 2}))
    third = task_queue.submit(spec(payload={"n": 3}))
    assert first < second < third
    assert task_queue.queued_count() == 3
    assert task_queue._next_task_id() == first, "必须取最早入队的（FIFO）"


def test_submit_for_a_deleted_library_keeps_the_name_but_drops_the_id(
    task_queue, db_mod, spec
):
    library_id = _insert_library(db_mod, "会被删掉的库")
    first = task_queue.submit(spec(library_id))
    assert _task_row(db_mod, first)["library_id"] == library_id

    with db_mod.tx() as conn:
        conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))

    second = task_queue.submit(spec(library_id, payload={"library_name": "会被删掉的库"}))
    row = _task_row(db_mod, second)
    assert row["library_id"] is None, "库已不在，不许留下悬挂外键"
    assert row["library_name"] == "会被删掉的库", "名字要保住，否则历史任务无从追溯"


# --------------------------------------------------------------------------- 取消


def test_cancel_only_works_for_queued_tasks(task_queue, db_mod, spec):
    task_id = task_queue.submit(spec())

    assert task_queue.cancel(task_id) is True
    row = _task_row(db_mod, task_id)
    assert row["status"] == "cancelled"
    assert row["finished_at"], "取消也要写 finished_at"
    assert "取消" in (row["error"] or "")
    assert task_queue.cancel(task_id) is False, "已取消的任务不能再取消"
    assert task_queue.queued_count() == 0

    running = task_queue.submit(spec())
    with db_mod.tx() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running,))
    assert task_queue.cancel(running) is False, "running 状态不可中断（串行队列没有取消点）"
    assert _task_row(db_mod, running)["status"] == "running"

    assert task_queue.cancel(999999) is False


# --------------------------------------------------------------------------- 串行不变量


def test_single_daemon_worker_never_overlaps_tasks(task_queue, db_mod, spec, monkeypatch):
    """A12 的核心：一个 daemon 线程、严格 FIFO、任何时刻最多一个任务在执行。"""
    library_id = _insert_library(db_mod, "串行验证库")
    recorder = _Recorder(task_queue, db_mod, duration=0.05)
    monkeypatch.setattr("app.executor.execute_task", recorder)

    submitted = [task_queue.submit(spec(library_id, payload={"n": n})) for n in range(4)]
    # 同一库：同时最多一个任务，正是 A12 要求的那一半；全局单线程给出更强保证。

    task_queue.start()
    deadline = time.monotonic() + 20
    while len(recorder.calls) < len(submitted) and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(recorder.calls) == len(submitted), f"只跑了 {len(recorder.calls)} 个"
    assert recorder.overlaps == 0, "出现了任务重叠窗口"
    assert recorder.max_inflight == 1
    assert [call["task_id"] for call in recorder.calls] == submitted, "必须严格 FIFO"

    for call in recorder.calls:
        assert call["thread"] == "mlm-task-worker", call
        assert call["daemon"] is True, "工作线程必须是 daemon（随进程退出）"
        assert call["running_task_id"] == call["task_id"], "执行期间要能查到当前在跑哪个任务"
        assert call["status"] == "running", call

    # 每个任务"完整跑完"才轮到下一个：后一个的开始时间不早于前一个的结束时间。
    for earlier, later in zip(recorder.calls, recorder.calls[1:]):
        assert later["started"] >= earlier["finished"], (earlier, later)

    assert task_queue.queued_count() == 0
    task_queue.stop(timeout=5)
    assert task_queue.running_task_id is None


def test_queued_count_decreases_one_task_at_a_time(task_queue, db_mod, spec, monkeypatch):
    recorder = _Recorder(task_queue, db_mod, duration=0.03)
    monkeypatch.setattr("app.executor.execute_task", recorder)

    for _ in range(3):
        task_queue.submit(spec())
    task_queue.start()
    deadline = time.monotonic() + 20
    while len(recorder.calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(recorder.calls) == 3
    assert [call["queued_count"] for call in recorder.calls] == [2, 1, 0]
    assert task_queue.queued_count() == 0


def test_worker_survives_an_executor_crash_and_keeps_draining(
    task_queue, db_mod, spec, monkeypatch
):
    """执行器自身抛异常：写 failed、线程不许死、后面的任务照跑。"""
    recorder = _Recorder(task_queue, db_mod, duration=0.01)
    monkeypatch.setattr("app.executor.execute_task", recorder)

    first = task_queue.submit(spec())
    recorder.boom_on.add(first)
    second = task_queue.submit(spec())
    task_queue.start()

    deadline = time.monotonic() + 20
    while _task_row(db_mod, second)["status"] not in ("success", "failed") and time.monotonic() < deadline:
        time.sleep(0.01)

    assert _task_row(db_mod, first)["status"] == "failed", "执行器崩溃的任务必须落 failed"
    error = _task_row(db_mod, first)["error"]
    assert "执行器内部错误" in error and "模拟执行器崩溃" in error, error
    assert _task_row(db_mod, second)["status"] == "success", "崩溃不许拖垮队列"
    assert task_queue.queued_count() == 0


def test_queue_is_durable_across_a_worker_restart(task_queue, db_mod, spec, monkeypatch):
    """队列存在 DB 里：工作线程停掉期间提交的任务，重新 start 后照样被消费。"""
    recorder = _Recorder(task_queue, db_mod, duration=0.01)
    monkeypatch.setattr("app.executor.execute_task", recorder)

    task_queue.start()
    task_queue.stop(timeout=5)
    assert task_queue.running_task_id is None

    first = task_queue.submit(spec())
    second = task_queue.submit(spec())
    time.sleep(0.05)
    assert recorder.calls == [], "工作线程已停，不该有人执行"
    assert [_task_row(db_mod, tid)["status"] for tid in (first, second)] == ["queued", "queued"]

    task_queue.start()
    task_queue.start()  # 幂等：不许起第二个工作线程
    workers = [t for t in threading.enumerate() if t.name == "mlm-task-worker"]
    assert len(workers) == 1, f"只允许一个工作线程，实际 {workers}"

    deadline = time.monotonic() + 20
    while len(recorder.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [call["task_id"] for call in recorder.calls] == [first, second]

    task_queue.stop(timeout=5)
    task_queue.stop(timeout=5)  # 幂等
    assert task_queue.running_task_id is None
    assert [t for t in threading.enumerate() if t.name == "mlm-task-worker"] == []
