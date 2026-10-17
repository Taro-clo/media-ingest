"""「日志清理」的回归测试。

两件套（应用日志文件本身不清理、不轮转，见 ``docs/guides/TROUBLESHOOTING.md`` 场景 24）：

* **单任务上限** ``max_task_log_lines``（默认 5000，``app/logbuf.py``）：写满后不再落库，
  并且**只留一条**说明原因的行（这条提示不占配额，否则用户永远看不到原因），后续写入直接丢弃。
* **历史裁剪** ``max_history``（默认 2000，``app/db.py:trim_history``）：每个任务收尾时删掉最旧的
  任务记录，它们的 ``task_logs`` 靠 ``ON DELETE CASCADE`` 一起删；``todos`` 表故意没有
  ``tasks`` 外键（``app/db.py:54``），所以待办看板的台账不受影响。

第三种断言在**执行器**层：裁剪发生了要能在本次任务的日志里看见。
"""

from __future__ import annotations

import pytest

import processor_helpers as P


@pytest.fixture
def logbuf_mod():
    """被测模块（与 `tests/test_todos.py` 同一约定：缺失即 SKIP，不做模块级 importorskip）。"""
    return pytest.importorskip("app.logbuf")


# --------------------------------------------------------------------------- 小工具


def _insert_task(db_mod, *, kind="rename", library_id=None, status="success"):
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        return int(
            conn.execute(
                "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,"
                "result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (library_id, "日志清理库", kind, "manual", status, "{}", "{}", "{}", stamp),
            ).lastrowid
        )


def _task_ids(db_mod):
    return [
        r["id"]
        for r in db_mod.get_conn().execute("SELECT id FROM tasks ORDER BY id").fetchall()
    ]


def _log_count(db_mod, task_id):
    return db_mod.get_conn().execute(
        "SELECT COUNT(*) AS n FROM task_logs WHERE task_id=?", (task_id,)
    ).fetchone()["n"]


def _logs(db_mod, task_id):
    return db_mod.get_conn().execute(
        "SELECT level, message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()


def _insert_library_with_processor(db_mod, name, source, target, processor_id):
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        return int(
            conn.execute(
                "INSERT INTO libraries(name,type,processor,processor_id,log_file,enabled,"
                "source_dir,target_dir,match_categories,match_tags,match_path_prefixes,"
                "orphan_cleanup,permission_mode,rule_override,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, "Bangumi", processor_id, processor_id, None, 1, source, target,
                 "[]", "[]", "[]", 0, 0, None, stamp, stamp),
            ).lastrowid
        )


# --------------------------------------------------------------------------- 单任务上限


def test_task_log_cap_stops_writing_and_leaves_exactly_one_notice(db_mod, logbuf_mod):
    """写满 3 行后：第 4、5 次写入被丢弃，库里恰好 3 行 + 1 行提示。"""
    db_mod.set_setting("max_task_log_lines", "3")
    task_id = _insert_task(db_mod)
    buffer = logbuf_mod.LogBuffer()

    ids = [buffer.add_log(task_id, f"line-{index}") for index in range(5)]

    assert all(log_id > 0 for log_id in ids[:3]), ids
    assert ids[3:] == [0, 0], "配额用尽后必须返回 0（丢弃，而不是悄悄写进去）"

    rows = _logs(db_mod, task_id)
    assert [row["message"] for row in rows[:3]] == ["line-0", "line-1", "line-2"], rows
    notices = [row for row in rows if "上限" in str(row["message"])]
    assert len(notices) == 1, rows
    assert len(rows) == 4, "3 条正文 + 1 条提示（提示本身不占配额）"
    assert notices[0]["level"] == "warning", notices[0]
    assert "max_task_log_lines" in str(notices[0]["message"]), notices[0]


def test_task_log_cap_falls_back_to_the_default_for_broken_settings(db_mod, logbuf_mod):
    """非法/非正的上限值回退内置默认（5000）—— 不能因为一个坏设置把日志关掉。"""
    buffer = logbuf_mod.LogBuffer()
    db_mod.set_setting("max_task_log_lines", "not-a-number")
    assert buffer._limit() == logbuf_mod.DEFAULT_MAX_TASK_LOG_LINES
    db_mod.set_setting("max_task_log_lines", "0")
    assert buffer._limit() == logbuf_mod.DEFAULT_MAX_TASK_LOG_LINES
    db_mod.set_setting("max_task_log_lines", "-5")
    assert buffer._limit() == logbuf_mod.DEFAULT_MAX_TASK_LOG_LINES
    db_mod.set_setting("max_task_log_lines", "17")
    assert buffer._limit() == 17


# --------------------------------------------------------------------------- 历史裁剪


def test_trim_history_keeps_the_newest_tasks_and_cascades_their_logs(db_mod, logbuf_mod):
    db_mod.set_setting("max_history", "3")
    buffer = logbuf_mod.LogBuffer()
    ids = [_insert_task(db_mod, kind=f"k{index}") for index in range(5)]
    for index, task_id in enumerate(ids):
        buffer.add_log(task_id, f"line-{index}")
    todos_before = db_mod.get_conn().execute("SELECT COUNT(*) AS n FROM todos").fetchone()["n"]

    assert db_mod.trim_history() == 2, "5 条任务、上限 3 ⇒ 删最旧的 2 条"

    assert _task_ids(db_mod) == ids[-3:], "留下的必须是**最新**的 3 条"
    assert _log_count(db_mod, ids[0]) == 0 and _log_count(db_mod, ids[1]) == 0, (
        "被裁掉的任务，其日志必须级联删除"
    )
    assert _log_count(db_mod, ids[2]) == 1, "留下的任务日志一条不少"
    assert db_mod.trim_history() == 0, "已经在上限内 ⇒ 再裁 0 条（幂等）"
    todos_after = db_mod.get_conn().execute("SELECT COUNT(*) AS n FROM todos").fetchone()["n"]
    assert todos_after == todos_before, "任务历史被裁掉不许动待办台账"


def test_trim_history_is_disabled_when_the_limit_is_not_positive(db_mod):
    """``max_history <= 0``（或坏值）⇒ 不裁剪，历史留着。"""
    ids = [_insert_task(db_mod) for _ in range(3)]

    db_mod.set_setting("max_history", "0")
    assert db_mod.trim_history() == 0
    db_mod.set_setting("max_history", "broken")
    assert db_mod.trim_history() == 0, "坏值回退默认 2000，仍然不删任何东西"

    assert _task_ids(db_mod) == ids


# --------------------------------------------------------------------------- 执行器留痕


def test_executor_announces_the_history_trim_in_the_task_log(
    executor_mod, db_mod, proc_registry, proc_defaults, tmp_path
):
    """``max_history=1`` ⇒ 更早的任务被裁掉，而且本次任务的日志里能看到这件事。"""
    db_mod.set_setting("max_history", "1")
    old_task = _insert_task(db_mod, kind="rename")

    proc_registry.ensure_builtins()
    spec = proc_defaults.builtin_spec("bangumi")
    spec["id"] = "logcleanup-bangumi"
    spec["name"] = "番剧（日志清理）"
    spec["log_file"] = "logcleancache.log"
    proc_registry.create_processor(spec)

    source = tmp_path / "downloads"
    target = tmp_path / "library"
    tree = {"Show A/Season 1/Show.EP01.mkv": 10}
    P.write_tree(source, tree)
    P.write_tree(target, tree)
    library_id = _insert_library_with_processor(
        db_mod, "清理库", str(source), str(target), "logcleanup-bangumi"
    )
    task_id = _insert_task(db_mod, kind="rename", library_id=library_id, status="queued")

    executor_mod.execute_task(task_id)

    row = db_mod.get_conn().execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row is not None and row["status"] == "success", dict(row) if row else None
    assert _task_ids(db_mod) == [task_id], "max_history=1 ⇒ 只留最新那条任务"
    assert _log_count(db_mod, old_task) == 0, "被裁掉的任务，其日志一并级联删除"

    messages = [str(item["message"]) for item in _logs(db_mod, task_id)]
    announced = [text for text in messages if "裁剪任务历史" in text and "max_history" in text]
    assert announced, messages
    assert "1 条" in announced[0], announced[0]
