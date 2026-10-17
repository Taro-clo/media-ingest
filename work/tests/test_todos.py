"""待办看板（``todos``）：去重语义、容错、API 往返与设置开关。

这个文件的断言只打在两处**真实的边界**上：

* ``app.todos`` 的公开函数（``record_task_events`` / ``list_todos`` / ``resolve_todo`` /
  ``reopen_todo`` / ``todo_summary`` / ``detect_resolved`` / ``resolve_detected``）——它们直接读写
  ``todos`` 表；
* ``/api/todos`` 的端点（列表 / 计数 / 打钩 / 恢复 / 一键检测 / 检测后打钩）——走真实 ASGI 应用
  （含鉴权），而不是把中间层换掉。

要守住的核心语义（写错任何一条，看板就会变成"数据黑洞"）：

1. 同一 ``(kind, dedupe_key)`` 已有 ``open`` ⇒ **只累加** ``occurrences``、刷新"最近一次"上下文，
   ``first_seen_at`` 永不变，绝不新增第二行；
2. 该键曾 ``done`` 而再次发生 ⇒ **新插一条 ``open``**，原来的 ``done`` 原样留着（出现次数有历史）；
3. 打钩（``resolve``）**只改状态、一行都不删**，随时能 ``reopen`` 恢复；
4. ``result`` 缺键 / ``None`` / 坏类型一律不抛异常（"读取器再稳也不该把任务本身拖挂"）；
5. ``todo_kinds`` 关掉的种类不写库；非法 / 未知值静默忽略并回退"全部启用"。
"""

from __future__ import annotations

import json
import re

import pytest

#: 对外时间戳一律本地 ``YYYY-MM-DD HH:MM:SS``（与 ``/api/tasks`` 一致，不退回 UTC ISO8601）。
LOCAL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@pytest.fixture
def todos(db_mod):
    """被测模块。模块缺失 ⇒ 用例 SKIP（绝不在模块级 importorskip）。"""
    return pytest.importorskip("app.todos")


# --------------------------------------------------------------------------- 造样本


def _link_failed(dst: str = "/downloads/Show A/Season 1/ep01.mkv",
                 src: str = "/downloads/Show.A.S01E01.mkv",
                 error: str = "OSError: [Errno 18] 无效的跨设备链接"):
    """一次"硬链接没建上"的任务结果（形状照抄 ``app/executor.py`` 的 ``result``）。"""
    return {
        "linked": 0,
        "skipped": 0,
        "link_failed": 1,
        "already_linked": 0,
        "renamed": 0,
        "samples": {"link_failed": [{"src": src, "dst": dst, "error": error}]},
        "samples_truncated": False,
        "warnings": [],
        "source_integrity": {"ok": True, "changed": []},
    }


def _record(todos, result, **overrides):
    """``record_task_events`` 的固定接口（关键字传参）+ 少量覆盖，省掉每次重复五行。"""
    kwargs = {
        "result": result,
        "library_id": 1,
        "library_name": "动画",
        "task_id": 11,
        "task_kind": "run",
    }
    kwargs.update(overrides)
    return todos.record_task_events(**kwargs)


# --------------------------------------------------------------------------- 去重语义


def test_repeat_of_open_entry_accumulates_instead_of_inserting(todos):
    """语义 1：同一键重复发生 ⇒ 同一行、``occurrences`` 累加、``first_seen_at`` 不变。"""
    ids = _record(todos, _link_failed())
    assert len(ids) == 1
    todo_id = ids[0]
    first = todos.get_todo(todo_id)
    assert first["kind"] == "link_failed"
    assert first["dedupe_key"] == "/downloads/Show A/Season 1/ep01.mkv"
    assert first["status"] == "open"
    assert first["occurrences"] == 1
    assert first["severity"] == "error"
    assert "无效的跨设备链接" in first["detail"]
    assert first["task_id"] == 11 and first["task_kind"] == "run"

    # 第二次（另一个库、另一个任务）报同一个目标路径
    again = _record(todos, _link_failed(), library_id=2, library_name="电影",
                    task_id=22, task_kind="rescan")
    assert again == ids, "同一键必须落回同一行，不能新增"

    second = todos.get_todo(todo_id)
    assert second["occurrences"] == 2
    assert second["first_seen_at"] == first["first_seen_at"], "首次出现时间不许被刷新"
    assert second["status"] == "open"
    # "最近一次"的上下文要更新成最新的来源（否则看板上会一直显示老库老任务）
    assert (second["library_id"], second["library_name"]) == (2, "电影")
    assert (second["task_id"], second["task_kind"]) == (22, "rescan")
    assert todos.list_todos(status="all")["total"] == 1


def test_recurrence_after_done_opens_new_row_and_keeps_history(todos):
    """语义 2：``done`` 之后再次发生 ⇒ 新开一条 ``open``，历史 ``done`` 原样保留。"""
    ids = _record(todos, _link_failed())
    old_id = ids[0]
    resolved = todos.resolve_todo(old_id)
    assert resolved["status"] == "done" and resolved["resolved_at"]

    fresh = _record(todos, _link_failed(), task_id=33)
    assert fresh != [old_id], "打完钩再发生必须是一条新条目"
    new_id = fresh[0]
    assert new_id != old_id

    old = todos.get_todo(old_id)
    assert old["status"] == "done"
    assert old["occurrences"] == 1, "历史条目的出现次数不能被新一次覆盖"
    assert old["resolved_at"] is not None

    new = todos.get_todo(new_id)
    assert new["status"] == "open" and new["occurrences"] == 1
    assert new["dedupe_key"] == old["dedupe_key"], "两条指向同一个对象，只是周期不同"

    assert todos.list_todos(status="all")["total"] == 2
    assert todos.list_todos(status="open")["total"] == 1
    assert todos.list_todos(status="done")["total"] == 1
    # 默认排序：未完成在前
    assert [item["id"] for item in todos.list_todos(status="all")["items"]] == [new_id, old_id]


def test_resolve_and_reopen_never_delete_any_data(todos):
    """语义 3/4：打钩只改状态；恢复后内容与出现次数一字不差。"""
    ids = _record(todos, _link_failed())
    todo_id = ids[0]
    before = todos.get_todo(todo_id)

    done = todos.resolve_todo(todo_id)
    assert done["status"] == "done" and done["resolved_at"]
    assert todos.list_todos(status="all")["total"] == 1, "打钩不许删数据"

    # 幂等：对已经 done 的条目再打一次钩，不刷新完成时间
    assert todos.resolve_todo(todo_id)["resolved_at"] == done["resolved_at"]

    back = todos.reopen_todo(todo_id)
    assert back["status"] == "open"
    assert back["resolved_at"] is None
    for field in ("kind", "dedupe_key", "title", "detail", "severity", "occurrences",
                  "first_seen_at", "library_id", "library_name", "task_id"):
        assert back[field] == before[field], f"恢复后 {field} 必须与打钩前一致"

    assert todos.resolve_todo(999999) is None
    assert todos.reopen_todo(999999) is None


def test_open_filter_is_the_default_for_listing(todos):
    """列表默认只给未完成的；``limit`` / ``offset`` 就地收敛（路由还会再走一次 clamp）。"""
    for index in range(3):
        _record(todos, _link_failed(dst=f"/downloads/Show/ep0{index}.mkv"), task_id=100 + index)
    ids = [item["id"] for item in todos.list_todos(status="all", limit=99)["items"]]
    todos.resolve_todo(ids[0])

    assert todos.list_todos()["total"] == 2
    assert todos.list_todos(status="all")["total"] == 3
    page = todos.list_todos(status="all", limit=1, offset=1)
    assert page["limit"] == 1 and page["offset"] == 1 and len(page["items"]) == 1
    # 排序：未完成在前（id 倒序），已打钩的沉底 ⇒ 三行里第 2 行是第二新的那条 open
    assert [item["id"] for item in todos.list_todos(status="all")["items"]] == [
        ids[1], ids[2], ids[0],
    ]
    assert page["items"][0]["id"] == ids[2]
    assert todos.list_todos(status="all", limit=0)["limit"] == 50, "非法 limit 回默认"
    assert todos.list_todos(status="all", offset=-5)["offset"] == 0


def test_list_todos_query_filters_by_literal_substring(todos):
    """``query``（路由上的 ``q``）= "按作品筛选"：``title`` / ``detail`` /
    ``dedupe_key`` 三列的子串 OR 匹配，**不是**"按作品精确归属"（见 KI-10）。"""
    _record(todos, _link_failed(dst="/downloads/Work 1/Season 1/ep01.mkv"), task_id=201)
    _record(todos, _link_failed(dst="/downloads/Work 02/Season 1/ep01.mkv"), task_id=202)
    _record(todos, _link_failed(dst="/downloads/Work 12/Season 1/ep01.mkv"), task_id=203)

    assert todos.list_todos(status="all")["total"] == 3
    # 子串口径的第一层含义：``Work 1`` 同时命中 ``Work 12``（精确归属只会命中 1 条）
    assert todos.list_todos(status="all", query="Work 1")["total"] == 2
    assert todos.list_todos(status="all", query="Work 12")["total"] == 1
    # 第二层含义：它**不是**"按作品名归一化" —— ``Work 1`` 不匹配 ``Work 02``，且 ``Work 0`` 只匹配 ``Work 02``
    assert todos.list_todos(status="all", query="Work 0")["total"] == 1
    # ASCII 大小写不敏感
    assert todos.list_todos(status="all", query="work 12")["total"] == 1
    # 空 / 纯空白 = 不过滤；没命中就是 0（不许"回落到全部"）
    assert todos.list_todos(status="all", query="")["total"] == 3
    assert todos.list_todos(status="all", query="   ")["total"] == 3
    assert todos.list_todos(status="all", query="Work 99")["total"] == 0
    assert todos.list_todos(status="all", query="Work 99")["items"] == []
    # 命中条目必须真能在某一列里找到这段文字（钉住口径，别靠"碰巧没筛掉"）
    for item in todos.list_todos(status="all", query="Work 12")["items"]:
        haystack = f"{item['title']}\n{item['detail']}\n{item['dedupe_key']}"
        assert "Work 12" in haystack


def test_list_todos_query_escapes_like_wildcards(todos):
    """``%`` / ``_`` / ``\\`` 按**字面**匹配：否则搜 ``Work_`` 会把每个作品都列出来。"""
    _record(todos, _link_failed(dst="/downloads/Work_01/Season 1/ep01.mkv"), task_id=211)
    _record(todos, _link_failed(dst="/downloads/WorkA01/Season 1/ep01.mkv"), task_id=212)

    assert todos.list_todos(status="all", query="Work_")["total"] == 1
    assert todos.list_todos(status="all", query="Work_01")["total"] == 1
    assert todos.list_todos(status="all", query="%")["total"] == 0


def test_list_todos_query_combines_with_other_filters_and_paging(todos):
    """``query`` 与 ``status`` / ``library_id`` 是 AND 关系，``total`` 与翻页都按过滤后算。"""
    for index in range(4):
        _record(todos, _link_failed(dst=f"/downloads/Work 0{index}/Season 1/ep01.mkv"),
                task_id=220 + index, library_id=1 if index < 3 else 2)
    first_id = todos.list_todos(status="all", query="Work 00")["items"][0]["id"]
    todos.resolve_todo(first_id)

    assert todos.list_todos(status="all", query="Work 0")["total"] == 4
    assert todos.list_todos(status="open", query="Work 0")["total"] == 3
    assert todos.list_todos(status="done", query="Work 0")["total"] == 1
    assert todos.list_todos(status="all", query="Work 0", library_id=2)["total"] == 1
    assert todos.list_todos(status="all", query="Work 0", library_id=99)["total"] == 0
    page = todos.list_todos(status="all", query="Work 0", limit=1, offset=1)
    assert page["total"] == 4, "total 必须是过滤后的条数，不是整表条数"
    assert page["limit"] == 1 and len(page["items"]) == 1


def test_api_todos_query_param_is_forwarded_and_trimmed(api_client, api_login, todos):
    """API 层：``?q=`` 透传到 ``list_todos(query=…)``，前后空白 strip、纯空白当没传。"""
    api_login(api_client)
    _record(todos, _link_failed(dst="/downloads/Work 01/Season 1/ep01.mkv"))
    _record(todos, _link_failed(dst="/downloads/Work 02/Season 1/ep01.mkv"))

    assert api_client.get("/api/todos", params={"q": "Work 01"}).json()["total"] == 1
    assert api_client.get("/api/todos", params={"q": "  Work 01  "}).json()["total"] == 1
    assert api_client.get("/api/todos", params={"q": "   "}).json()["total"] == 2
    assert api_client.get("/api/todos", params={"q": "Work 99"}).json()["total"] == 0
    # 过滤只影响列出，不影响统计口径之外的东西：summary 仍是整表
    assert api_client.get("/api/todos/summary").json()["open"] == 2


def test_todo_rows_are_not_chained_to_task_history(todos, db_mod):
    """``todos`` 不许挂 ``tasks`` 外键：任务历史被 ``trim_history`` 裁掉时待办必须留着。"""
    sql = db_mod.get_conn().execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='todos'"
    ).fetchone()["sql"]
    assert "REFERENCES tasks" not in sql.replace("\n", " ")
    assert _record(todos, _link_failed(), task_id=4242)[0]


# --------------------------------------------------------------------------- 容错


def test_record_tolerates_missing_and_broken_keys(todos):
    """语义 5：``result`` 缺键 / 坏类型 / ``None`` 都不抛异常，也不留脏条目。"""
    # (result, 期望新增行数)
    cases = [
        (None, 0),
        ({}, 0),
        ({"linked": 3, "renamed": 2}, 0),
        ({"link_failed": 0, "samples": {"link_failed": [{"dst": "/x/a.mkv"}]}}, 0),
        ({"link_failed": None, "samples": None}, 0),
        ({"samples": {"link_failed": None}}, 0),
        ({"samples": "坏字符串"}, 0),
        ({"link_failed": 2, "samples": "坏字符串"}, 1),        # 计数真实、样本坏 ⇒ 聚合一条
        ({"link_failed": 3, "samples": {}}, 1),
        ({"link_failed": "3"}, 1),                              # 计数是字符串，仍能识别
        ({"link_failed": [1, 2]}, 0),                           # 计数不是数字 ⇒ 当 0，不猜
        ({"samples": {"link_failed": ["不是字典"]}}, 0),
        ({"samples": {"link_failed": [{"note": "没有可用的键"}]}}, 0),  # 形状认识、但没有键
        ({"source_integrity": None}, 0),
        ({"source_integrity": {"ok": True, "changed": ["a"]}}, 0),
        ({"source_integrity": {"ok": "false"}}, 0),             # ``"false"`` 不是 False ⇒ 不误报
        ({"status": 123, "error": []}, 0),
        ({"status": "success", "error": ""}, 0),
    ]
    created = 0
    for index, (result, expected) in enumerate(cases):
        ids = _record(todos, result, library_id=index + 1, library_name=f"库{index + 1}",
                      task_id=index + 1)
        assert isinstance(ids, list), f"第 {index} 个用例返回了非列表"
        created += len(ids)
        assert len(ids) == expected, f"第 {index} 个用例的条目数不对：{result!r} -> {ids}"
    assert todos.list_todos(status="all")["total"] == created


def test_count_without_usable_samples_still_records_one_entry(todos):
    """样本缺失不等于没发生：计数 > 0 时按来源库聚合成一条，并把计数写进详情。"""
    ids = _record(todos, {"link_failed": 7}, library_id=9, library_name="纪录片", task_id=77)
    item = todos.get_todo(ids[0])
    assert item["kind"] == "link_failed"
    assert item["dedupe_key"] == "library:9", "拿不到具体目标时按库聚合"
    assert "7" in item["detail"] and "样本缺失" in item["detail"]
    assert item["title"] == "纪录片"

    again = _record(todos, {"link_failed": 2}, library_id=9, library_name="纪录片", task_id=78)
    assert again == ids and todos.get_todo(ids[0])["occurrences"] == 2

    # 连库都没有（手动/后台任务）：退到 no-sample 键，同样只累加
    orphan = _record(todos, {"link_failed": 1}, library_id=None, library_name=None, task_id=None)
    assert todos.get_todo(orphan[0])["dedupe_key"] == "no-sample"
    assert _record(todos, {"link_failed": 1}, library_id=None, library_name=None,
                   task_id=None) == orphan


# --------------------------------------------------------------------------- 事件读取


def test_sample_kinds_and_their_dedupe_keys(todos):
    """``rename_suffix`` / ``unresolved_episode`` 的键与详情；``name_collisions`` 不再进看板。

    一次重命名只推**一条**「冲突文件已加后缀保留」——
    「后到的自己加后缀」（``renamed_with_suffix``）与「占位者让位被加后缀」
    （``displaced_targets``）是同一个问题的两条路径，合并读；而 ``name_collisions``
    （"目标名已被占用"）与它记的是同一次重命名，**不再计入待办**（任务结果里照旧有）。
    """
    result = {
        "renamed_with_suffix": 2,
        # 争名的事实清单仍在任务结果里（任务详情页照旧渲染）——但**不再**产生待办。
        "name_collisions": 2,
        # 「占位者让位」：同一个 rename_suffix 种类的另一条产生路径。
        "displaced_targets": 1,
        "kept_unresolved_episode": 1,
        "samples": {
            # L2 的真实形状（app/processors/engine.py 的 _suffix_entry）：
            # {dir, old, target, clean, size, reason, displaced}
            "renamed_with_suffix": [
                {"dir": "Show/Season 1", "old": "ep05.mkv", "target": "Show S01E05 (2).mkv",
                 "clean": "Show S01E05.mkv", "size": 10485760, "reason": "within_run",
                 "displaced": ""},
                # 另一个作品下的同名目标 ⇒ 必须是**另一条**待办（键里带 dir）
                {"dir": "Other/Season 1", "old": "ep05.mkv", "target": "Show S01E05 (2).mkv",
                 "clean": "Show S01E05.mkv", "size": 2048, "reason": "within_run",
                 "displaced": ""},
            ],
            "name_collisions": [
                {"dir": "Movie", "target": "a.mkv", "previous": "b.mkv", "current": "a.mkv",
                 "kind": "existing_target"},
                # 同一个 dir + target 在一次任务里撞了两次 ⇒ 只记一条
                {"dir": "Movie", "target": "a.mkv", "previous": "b.mkv", "current": "a.mkv",
                 "kind": "existing_target"},
            ],
            # 「占位者让位」的真实形状（engine.py 的 displaced_targets 调用点）：
            # {dir, old, target, clean, size} —— **没有** reason / displaced 字段。
            "displaced_targets": [
                {"dir": "Movie/Season 1", "old": "Movie S01E03.mkv",
                 "target": "Movie S01E03 (2).mkv", "clean": "Movie S01E03.mkv",
                 "size": 1451528340},
            ],
            "kept_unresolved_episode": ["/downloads/Show/SP01.mkv", "/downloads/Show/观后感.mkv"],
        },
    }
    ids = _record(todos, result, library_id=3, library_name="动画", task_id=55)
    items = [todos.get_todo(todo_id) for todo_id in ids]
    assert sorted(item["kind"] for item in items) == [
        "rename_suffix", "rename_suffix", "rename_suffix",
        "unresolved_episode", "unresolved_episode",
    ], (
        "加后缀两条（不同作品，键带 dir）+ 让位一条，都归到 rename_suffix；"
        "name_collisions 已不再产生待办（它和加后缀记的是同一次重命名）"
    )

    by_kind = {}
    for item in items:
        by_kind.setdefault(item["kind"], []).append(item)

    suffix = [item for item in by_kind["rename_suffix"]
              if item["dedupe_key"].startswith("Other/Season 1")][0]
    assert suffix["dedupe_key"] == "Other/Season 1 | Show S01E05 (2).mkv", (
        "去重键必须带上作用域目录，否则两个作品下的同名目标会被并成一条"
    )
    assert suffix["dedupe_key"] != "Show/Season 1 | Show S01E05 (2).mkv"
    assert "源文件名：ep05.mkv" in suffix["detail"]
    assert "最终文件名：Show S01E05 (2).mkv" in suffix["detail"]
    assert "本来想要的名字：Show S01E05.mkv" in suffix["detail"]
    assert "原因：within_run" in suffix["detail"]
    assert suffix["severity"] == "warning"

    displaced = [item for item in by_kind["rename_suffix"]
                 if item["dedupe_key"].startswith("Movie/Season 1")][0]
    assert displaced["dedupe_key"] == "Movie/Season 1 | Movie S01E03 (2).mkv"
    assert "让位文件（原名）：Movie S01E03.mkv" in displaced["detail"]
    assert "让位后文件名：Movie S01E03 (2).mkv" in displaced["detail"]
    assert "原因：displaced" in displaced["detail"], displaced["detail"]
    assert displaced["severity"] == "warning"

    assert sorted(item["dedupe_key"] for item in by_kind["rename_suffix"]) == [
        "Movie/Season 1 | Movie S01E03 (2).mkv",
        "Other/Season 1 | Show S01E05 (2).mkv",
        "Show/Season 1 | Show S01E05 (2).mkv",
    ]

    unresolved = sorted(item["dedupe_key"] for item in by_kind["unresolved_episode"])
    assert unresolved == ["/downloads/Show/SP01.mkv", "/downloads/Show/观后感.mkv"]

    # 再次发生（同一批）⇒ 五条各自累加，不新增
    assert sorted(_record(todos, result, library_id=3, library_name="动画", task_id=56)) == sorted(ids)
    assert todos.list_todos(status="open")["total"] == 5
    assert all(todos.get_todo(todo_id)["occurrences"] == 2 for todo_id in ids)


def test_source_integrity_and_task_failed_readers(todos):
    """``source_integrity`` 只在 ``ok is False`` 时记；``task_failed`` 用错误摘要当键。"""
    failed_integrity = {"source_integrity": {"ok": False, "changed": ["a/1.mkv", "a/2.mkv"]}}
    ids = _record(todos, failed_integrity, library_id=4, library_name="电影", task_id=66)
    assert len(ids) == 1
    item = todos.get_todo(ids[0])
    assert item["kind"] == "source_integrity"
    assert item["dedupe_key"] == "library:4"
    assert "a/1.mkv" in item["detail"] and "a/2.mkv" in item["detail"]

    # 同库再次完整性失败 ⇒ 累加（按库聚合的代价：不同变动清单会并到一条）
    assert _record(todos, {"source_integrity": False}, library_id=4, library_name="电影",
                   task_id=67) == ids
    assert todos.get_todo(ids[0])["occurrences"] == 2

    error = 'Traceback (most recent call last):\n  File "x.py", line 1\nOSError: 跨设备链接'
    first = _record(todos, {"status": "failed", "error": error}, library_id=4,
                    library_name="电影", task_id=68, status="failed", error=error)
    assert len(first) == 1
    task_entry = todos.get_todo(first[0])
    assert task_entry["kind"] == "task_failed"
    assert task_entry["dedupe_key"] == "Traceback (most recent call last):", "只取第一行，堆栈偏移不会换键"
    assert "跨设备链接" in task_entry["detail"]

    # executor 只把 status 写进 tasks 表：调用方通过关键字补上也能记
    only_status = _record(todos, {}, library_id=4, library_name="电影", task_id=69,
                          status="failed")
    assert todos.get_todo(only_status[0])["kind"] == "task_failed"
    # 成功的任务不记
    assert _record(todos, {"status": "success"}, library_id=4, library_name="电影",
                   task_id=70) == []


# --------------------------------------------------------------------------- 设置开关


def test_todo_kinds_switch_and_invalid_values_fall_back(todos, db_mod):
    """``todo_kinds``：空 = 全部启用；只列已知种类；非法/未知值忽略并回退默认。"""
    assert todos.enabled_kinds() == list(todos.KNOWN_KINDS), "缺省（空串）= 全部启用"
    assert todos.normalize_kinds("LINK_FAILED， 不存在的种类; task_failed") == [
        "link_failed", "task_failed",
    ], "大小写不敏感、中文逗号/分号都能当分隔符、未知值静默忽略"
    assert todos.normalize_kinds("完全不认识") == list(todos.KNOWN_KINDS), "全非法 ⇒ 回退全开"
    assert todos.normalize_kinds(None) == list(todos.KNOWN_KINDS)
    assert todos.normalize_kinds("") == list(todos.KNOWN_KINDS)
    assert todos.normalize_kinds(["source_integrity", "bogus"]) == ["source_integrity"]

    # 只留硬链接失败：任务失败那种必须写不进去
    db_mod.set_setting("todo_kinds", "link_failed")
    assert todos.enabled_kinds() == ["link_failed"]
    mixed = _link_failed(dst="/downloads/Show/ep09.mkv")
    mixed.update({"status": "failed", "error": "boom"})
    ids = _record(todos, mixed, library_id=5, library_name="动画", task_id=90)
    assert {todos.get_todo(todo_id)["kind"] for todo_id in ids} == {"link_failed"}

    # 设置被写成垃圾 ⇒ 回退"全部启用"，看板不会因为一个手改的键就空掉
    db_mod.set_setting("todo_kinds", "???")
    assert todos.enabled_kinds() == list(todos.KNOWN_KINDS)
    summary = todos.todo_summary()
    assert summary["enabled_kinds"] == list(todos.KNOWN_KINDS) and summary["disabled_kinds"] == []

    db_mod.set_setting("todo_kinds", "link_failed")
    assert todos.todo_summary()["disabled_kinds"] == [
        kind for kind in todos.KNOWN_KINDS if kind != "link_failed"
    ]


def test_todo_summary_counts_by_status_kind_and_library(todos):
    """汇总：状态 / 种类 / 媒体库三个维度，一次查询给全。"""
    a = _record(todos, _link_failed(dst="/x/a.mkv"), library_id=1, library_name="动画", task_id=1)
    b = _record(todos, _link_failed(dst="/x/b.mkv"), library_id=2, library_name="电影", task_id=2)
    todos.resolve_todo(b[0])

    summary = todos.todo_summary()
    assert (summary["open"], summary["done"], summary["total"]) == (1, 1, 2)
    assert summary["by_status"] == {"open": 1, "done": 1}

    link_failed = next(entry for entry in summary["by_kind"] if entry["kind"] == "link_failed")
    assert (link_failed["open"], link_failed["done"], link_failed["total"]) == (1, 1, 2)
    assert link_failed["title"] == "硬链接创建失败" and link_failed["severity"] == "error"
    assert [entry["kind"] for entry in summary["by_kind"]] == list(todos.KNOWN_KINDS), "顺序稳定"

    libraries = {entry["library_id"]: entry for entry in summary["libraries"]}
    assert libraries[1]["open"] == 1 and libraries[2]["done"] == 1
    assert libraries[1]["library_name"] == "动画"


# --------------------------------------------------------------------------- API


def test_api_todos_requires_user(api_client):
    """四个端点都要鉴权：匿名一律 401（不能出现"401 其实来自不存在的路由"）。"""
    assert api_client.get("/api/todos").status_code == 401
    assert api_client.get("/api/todos/summary").status_code == 401
    assert api_client.post("/api/todos/1/resolve").status_code == 401
    assert api_client.post("/api/todos/1/reopen").status_code == 401


def test_api_todos_round_trip(api_client, api_login, todos):
    """列表 / 汇总 / 打钩 / 恢复走真实 HTTP；筛选与错误码都要对。"""
    api_login(api_client)
    ids = _record(todos, _link_failed(), library_id=7, library_name="动画", task_id=101)
    todo_id = ids[0]
    _record(todos, _link_failed(dst="/downloads/Show A/Season 1/ep02.mkv"), library_id=8,
            library_name="电影", task_id=102)

    listing = api_client.get("/api/todos")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert set(body) >= {"items", "total", "limit", "offset"}, "信封格式与 /api/tasks 一致"
    assert body["total"] == 2 and len(body["items"]) == 2
    item = next(entry for entry in body["items"] if entry["id"] == todo_id)
    assert item["kind"] == "link_failed" and item["status"] == "open"
    assert item["kind_title"] == "硬链接创建失败"
    assert item["kind_description"]
    assert item["is_open"] is True
    assert LOCAL_TIME_RE.match(item["last_seen_at"]), item["last_seen_at"]
    assert LOCAL_TIME_RE.match(item["first_seen_at"]), item["first_seen_at"]
    assert item["occurrences"] == 1

    # 筛选：种类 / 媒体库 / 状态
    assert api_client.get("/api/todos", params={"kind": "task_failed"}).json()["total"] == 0
    assert api_client.get("/api/todos", params={"kind": "link_failed"}).json()["total"] == 2
    assert api_client.get("/api/todos", params={"library_id": 7}).json()["total"] == 1
    assert api_client.get("/api/todos", params={"library_id": 99}).json()["total"] == 0
    assert api_client.get("/api/todos", params={"status": "done"}).json()["total"] == 0
    assert api_client.get("/api/todos", params={"status": "all"}).json()["total"] == 2
    paged = api_client.get("/api/todos", params={"limit": 1, "offset": 1}).json()
    assert paged["limit"] == 1 and paged["offset"] == 1 and len(paged["items"]) == 1

    # 汇总
    summary = api_client.get("/api/todos/summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["open"] == 2 and payload["done"] == 0 and payload["total"] == 2
    counts = {entry["kind"]: entry for entry in payload["by_kind"]}
    assert counts["link_failed"]["open"] == 2
    assert {entry["library_id"] for entry in payload["libraries"]} == {7, 8}

    # 打钩 → open 列表清空，但数据一行没少
    done = api_client.post(f"/api/todos/{todo_id}/resolve")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done" and done.json()["resolved_at"]
    assert LOCAL_TIME_RE.match(done.json()["resolved_at"])
    assert api_client.get("/api/todos").json()["total"] == 1
    assert api_client.get("/api/todos", params={"status": "all"}).json()["total"] == 2
    assert api_client.get("/api/todos", params={"status": "done"}).json()["total"] == 1

    # 恢复
    reopened = api_client.post(f"/api/todos/{todo_id}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open" and reopened.json()["resolved_at"] is None
    assert api_client.get("/api/todos").json()["total"] == 2

    # 错误码：不存在的条目 404、非法筛选值 400
    assert api_client.post("/api/todos/999999/resolve").status_code == 404
    assert api_client.post("/api/todos/999999/reopen").status_code == 404
    assert api_client.get("/api/todos", params={"status": "bogus"}).status_code == 400
    assert api_client.get("/api/todos", params={"kind": "bogus"}).status_code == 400
    assert "未知的待办状态" in api_client.get("/api/todos", params={"status": "bogus"}).json()["detail"]
    assert "未知的待办种类" in api_client.get("/api/todos", params={"kind": "bogus"}).json()["detail"]


def test_api_todos_after_done_recurrence_is_visible_as_two_entries(api_client, api_login, todos):
    """看板上"打完钩又发生"要看到两条：一条 done（历史）+ 一条 open（新的）。"""
    api_login(api_client)
    todo_id = _record(todos, _link_failed(), library_id=1, library_name="动画", task_id=1)[0]
    assert api_client.post(f"/api/todos/{todo_id}/resolve").status_code == 200
    new_id = _record(todos, _link_failed(), library_id=1, library_name="动画", task_id=2)[0]
    assert new_id != todo_id

    body = api_client.get("/api/todos", params={"status": "all"}).json()
    assert body["total"] == 2
    by_id = {entry["id"]: entry for entry in body["items"]}
    assert by_id[todo_id]["status"] == "done"
    assert by_id[new_id]["status"] == "open"
    assert body["items"][0]["id"] == new_id, "未完成排在前面"


# ------------------------------------------- 一键检测


def _seed_detect_scene(todos, db_mod, tmp_path, *, result, files=()):
    """造一个最小现场：媒体库（带 target_dir）+ 任务（``result`` 落库）+ 磁盘上的文件。

    返回 ``(library_id, task_id, target_dir)``。这里的 ``result`` 必须是**执行器真实形状**
    （样本里有 ``dir`` / ``target`` / ``dst`` 这类可核对的字段），检测就是靠它 + 磁盘实况判定的。
    """
    target = tmp_path / "library"
    for rel in files:
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        library_id = int(conn.execute(
            "INSERT INTO libraries(name,type,processor,processor_id,log_file,enabled,source_dir,"
            "target_dir,match_categories,match_tags,match_path_prefixes,orphan_cleanup,"
            "permission_mode,rule_override,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("检测库", "Bangumi", "bangumi", "bangumi", None, 1, str(tmp_path / "downloads"),
             str(target), "[]", "[]", "[]", 0, 0, None, stamp, stamp),
        ).lastrowid)
        task_id = int(conn.execute(
            "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,result,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (library_id, "检测库", "rename", "manual", "success", json.dumps({"mode": "full"}),
             "{}", json.dumps(result, ensure_ascii=False), stamp),
        ).lastrowid)
    todos.record_task_events(result=result, library_id=library_id, library_name="检测库",
                             task_id=task_id, task_kind="run")
    return library_id, task_id, target


def _suffix_result(*pairs):
    """L2 加后缀的真实样本形状（``dir`` + ``target`` 就是检测要核对的路径）。"""
    return {
        "renamed_with_suffix": len(pairs),
        "samples": {
            "renamed_with_suffix": [
                {"dir": directory, "old": "ep.mkv", "target": name, "clean": name,
                 "size": 10, "reason": "within_run", "displaced": ""}
                for directory, name in pairs
            ]
        },
    }


def test_no_per_kind_cap_so_one_full_scan_records_every_event(todos, db_mod, tmp_path):
    """指令 ②：每类明细上限取消 —— 60 条同类事件必须全部落账（旧实现只留 50）。"""
    result = _suffix_result(
        *[("Season 1", f"Show S01E{index:02} (2).mkv") for index in range(1, 61)]
    )

    _seed_detect_scene(todos, db_mod, tmp_path, result=result)

    rows = todos.list_todos(status="all", kind="rename_suffix", limit=200)["items"]
    assert len(rows) == 60, len(rows)
    assert len({row["dedupe_key"] for row in rows}) == 60, "每条明细各自一条待办（键 = dir | target）"
    assert all(row["status"] == "open" for row in rows), "新出现的都必须是未完成"


def test_no_sample_cap_so_every_failed_file_gets_its_own_todo(todos):
    """取消待办数量上限 —— 150 个建链失败必须落 150 条待办（旧版样本只留 100）。"""
    failures = [
        {
            "src": f"/downloads/Show A/Season 1/ep{index:03}.mkv",
            "dst": f"/library/Show A/Season 1/ep{index:03}.mkv",
            "error": "OSError: [Errno 18] 无效的跨设备链接",
        }
        for index in range(1, 151)
    ]
    result = _link_failed()
    result["link_failed"] = len(failures)
    result["samples"] = {"link_failed": failures}

    _record(todos, result)

    rows = todos.list_todos(status="all", kind="link_failed", limit=200)["items"]
    assert len(rows) == 150, f"样本不再截断 ⇒ 150 条待办，实际 {len(rows)}"
    assert len({row["dedupe_key"] for row in rows}) == 150, "按目标路径去重，条条独立"


def test_a_flat_work_folder_lands_on_the_board_as_unscoped_works(todos):
    """KI-1：结构不可识别的作品要**看得见** —— 一条 `unscoped_works` 待办。

    这条待办的用处就是"不留痕就完全静默"：引擎不改名也不报错，执行器只会把它算进
    `folders_scanned`。KI-1 的问题以待办的形式做提示。
    """
    assert "unscoped_works" in todos.KNOWN_KINDS, "第六种 kind 必须登记"
    spec = todos.KNOWN_KINDS["unscoped_works"]
    assert spec["severity"] == "warning"
    assert "结构不可识别" in spec["title"]

    result = _link_failed()
    result["linked"] = 12
    result["link_failed"] = 0
    result["unscoped_works"] = 2
    result["samples"] = {
        "link_failed": [],
        "unscoped_works": [
            {"dir": "/downloads/Flat Show", "folder": "Flat Show", "files": 2, "samples": "ep01.mkv、ep02.mkv"},
            {"dir": "/downloads/Another Flat", "folder": "Another Flat", "files": 1, "samples": "movie.mkv"},
        ],
    }

    ids = _record(todos, result)

    assert len(ids) == 2, ids
    rows = todos.list_todos(status="all", kind="unscoped_works", limit=50)["items"]
    assert len(rows) == 2, rows
    assert all(row["status"] == "open" for row in rows)
    assert all(row["severity"] == "warning" for row in rows)
    assert {row["dedupe_key"] for row in rows} == {"/downloads/Flat Show", "/downloads/Another Flat"}
    first = next(row for row in rows if row["dedupe_key"] == "/downloads/Flat Show")
    assert "Flat Show" in first["detail"] and "ep01.mkv" in first["detail"]
    # 重复出现只累加，不新增行（去重键 = 作品目录）。
    _record(todos, result, task_id=12)
    again = todos.list_todos(status="all", kind="unscoped_works", limit=50)["items"]
    assert len(again) == 2, again
    assert {row["occurrences"] for row in again} == {2}


def test_sample_collector_is_unbounded_by_default(executor_mod):
    """``SAMPLE_LIMIT`` 默认 ``None`` —— 收多少留多少、``truncated`` 恒假。

    待办条数的**源头**在这个收集器里（待办 = 样本逐条落账），不在看板里。哪天把
    ``SAMPLE_LIMIT`` 改回数字，这条断言会失败 —— 那正是提醒你要同步改
    ``docs/contracts/API.md`` §4.1 与 ``docs/contracts/ARCHITECTURE.md`` 的"不设上限"口径。
    """
    assert executor_mod.SAMPLE_LIMIT is None, "默认必须不设上限"

    collector = executor_mod.SampleCollector()
    for index in range(150):
        collector.add({"dst": f"/library/Show S01E{index:03}.mkv"})
    assert len(collector.items) == 150, len(collector.items)
    assert collector.truncated is False

    # 显式给 limit 时旧语义仍在（留一条"哪天要退回限量"的后路，行为可预期）
    capped = executor_mod.SampleCollector(limit=100)
    for index in range(150):
        capped.add({"dst": f"/library/Show S01E{index:03}.mkv"})
    assert len(capped.items) == 100, len(capped.items)
    assert capped.truncated is True


def test_detect_flags_only_records_whose_file_is_gone(todos, db_mod, tmp_path):
    """``rename_suffix``：目标文件已经不在了 ⇒ 候选；还在的 ⇒ 不碰（**只读**，一个状态都不改）。"""
    result = _suffix_result(("Show/Season 1", "Show S01E05 (2).mkv"),
                            ("Show/Season 1", "Show S01E06 (2).mkv"))
    library_id, _task_id, _target = _seed_detect_scene(
        todos, db_mod, tmp_path, result=result, files=["Show/Season 1/Show S01E06 (2).mkv"]
    )

    report = todos.detect_resolved(library_id=library_id)

    assert report["checked"] == 2 and report["candidate_total"] == 1
    assert [item["dedupe_key"] for item in report["candidates"]] == [
        "Show/Season 1 | Show S01E05 (2).mkv"
    ]
    item = report["candidates"][0]
    assert item["detect_mode"] == "missing"
    assert "已不存在" in item["detect_evidence"]
    assert item["kind"] == "rename_suffix"
    # 检测是只读的：两条待办都还是未完成。
    assert todos.list_todos(status="open")["total"] == 2
    assert todos.list_todos(status="done")["total"] == 0


def test_detect_matches_by_path_suffix_because_samples_lack_the_title(todos, db_mod, tmp_path):
    """样本里的 ``dir`` 只有 ``Season 1``（不含作品名）⇒ 必须按路径**尾巴**匹配。"""
    result = _suffix_result(("Season 1", "Show S01E05 (2).mkv"))
    library_id, _task_id, _target = _seed_detect_scene(
        todos, db_mod, tmp_path, result=result, files=["Show A/Season 1/Show S01E05 (2).mkv"]
    )

    report = todos.detect_resolved(library_id=library_id)

    assert report["candidate_total"] == 0, "文件还在（只是路径前缀多了作品名），不该被判成已消失"


def test_detect_covers_link_failed_when_the_missing_file_shows_up(todos, db_mod, tmp_path):
    """``link_failed`` 的 ``dst`` 是绝对路径：那条链接后来补上了 ⇒ 值得打钩。"""
    target = tmp_path / "library"
    result = {
        "link_failed": 2,
        "samples": {"link_failed": [
            {"src": "/downloads/a.mkv", "dst": str(target / "Show A" / "Season 1" / "a.mkv"),
             "error": "boom"},
            {"src": "/downloads/b.mkv", "dst": str(target / "Show A" / "Season 1" / "b.mkv"),
             "error": "boom"},
        ]},
    }
    library_id, _task_id, _target = _seed_detect_scene(
        todos, db_mod, tmp_path, result=result, files=["Show A/Season 1/a.mkv"]
    )

    report = todos.detect_resolved(library_id=library_id)

    assert [item["dedupe_key"] for item in report["candidates"]] == [
        str(target / "Show A" / "Season 1" / "a.mkv")
    ]
    assert report["candidates"][0]["detect_mode"] == "present"
    assert "现在已经存在" in report["candidates"][0]["detect_evidence"]
    # 没有可核对文件路径的种类要如实标出来（前端据此提示"只能人工核对"）。
    assert "link_failed" in report["detectable_kinds"]
    assert "task_failed" in report["undetectable_kinds"]
    assert "source_integrity" in report["undetectable_kinds"]


def test_resolve_detected_rechecks_every_entry_before_marking_done(todos, db_mod, tmp_path):
    """弹窗可能已经打开很久：打钩前**再过一遍**，文件回来了就跳过、不许误标。"""
    result = _suffix_result(("Show/Season 1", "Show S01E05 (2).mkv"))
    library_id, _task_id, target = _seed_detect_scene(todos, db_mod, tmp_path, result=result)
    todo_id = todos.list_todos(status="open")["items"][0]["id"]
    assert [item["id"] for item in todos.detect_resolved(library_id=library_id)["candidates"]] == [todo_id]

    # 文件又回来了 ⇒ 跳过
    restored = target / "Show" / "Season 1" / "Show S01E05 (2).mkv"
    restored.parent.mkdir(parents=True, exist_ok=True)
    restored.write_bytes(b"x")
    skipped = todos.resolve_detected([todo_id])
    assert skipped["resolved"] == [] and len(skipped["skipped"]) == 1
    assert "不再支持自动打钩" in skipped["skipped"][0]["reason"]
    assert todos.get_todo(todo_id)["status"] == "open"

    # 又没了 ⇒ 这次才真打钩（且只是改状态，数据一行不少）
    restored.unlink()
    done = todos.resolve_detected([todo_id])
    assert [item["id"] for item in done["resolved"]] == [todo_id]
    assert todos.get_todo(todo_id)["status"] == "done"
    assert todos.list_todos(status="all")["total"] == 1


def test_detect_endpoints_round_trip(api_client, api_login, todos, db_mod, tmp_path):
    """``GET /api/todos/detect``（只读）→ ``POST /api/todos/detect-resolve``（确认后打钩）。"""
    api_login(api_client)
    result = _suffix_result(("Show/Season 1", "Show S01E05 (2).mkv"))
    library_id, _task_id, _target = _seed_detect_scene(todos, db_mod, tmp_path, result=result)

    report = api_client.get("/api/todos/detect", params={"library_id": library_id})
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["candidate_total"] == 1, payload
    assert payload["detectable_kinds"] and payload["undetectable_kinds"]
    todo_id = payload["candidates"][0]["id"]
    assert api_client.get("/api/todos/summary").json()["open"] == 1, "检测不许改状态"

    done = api_client.post("/api/todos/detect-resolve", json={"ids": [todo_id]})
    assert done.status_code == 200, done.text
    assert [item["id"] for item in done.json()["resolved"]] == [todo_id]
    assert done.json()["requested"] == 1

    summary = api_client.get("/api/todos/summary").json()
    assert summary["open"] == 0 and summary["done"] == 1

    # 非法 kind 仍然是 400；空 ids 是"什么都没做"，不是错误。
    assert api_client.get("/api/todos/detect", params={"kind": "bogus"}).status_code == 400
    empty = api_client.post("/api/todos/detect-resolve", json={"ids": []})
    assert empty.status_code == 200 and empty.json()["resolved"] == []
