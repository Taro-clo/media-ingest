"""首页待办看板的前端契约。

后端行为在 `tests/test_todos.py`（分页由 `GET /api/todos` 的 `limit`/`offset` 承载、一键检测
`GET /api/todos/detect` 只读、`POST /api/todos/detect-resolve` 确认后才打钩）；这里只钉**前端**：

* ② 分页真的带了 `offset`，上一页/下一页按钮都在；
* ③ 一键检测弹窗存在、复选框默认勾选、确认按钮走"先核对再打钩"的端点；
* ④ 行首是**圆圈图标**（不是汉字「打钩」按钮），默认筛选是「未完成」，刚打钩的行**不立刻消失**
  （`board.pinned`），只在换筛选 / 手动刷新 / 翻页时才放下去。

静态断言与 `tests/test_library_overrides.py:110` 同款：直接读源码文本 —— 为 4 个 JS 文件引一套
浏览器测试的代价远大于收益（本仓库没有前端测试运行器）。
"""

from __future__ import annotations

from pathlib import Path

DASHBOARD = Path("web/js/pages/dashboard.js")
I18N = Path("web/js/i18n.js")
API = Path("web/js/api.js")
CSS = Path("web/css/app.css")


def _text(path: Path) -> str:
    assert path.is_file(), f"{path} 不见了"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- ② 分页


def test_board_pagination_sends_an_offset_and_offers_both_buttons():
    source = _text(DASHBOARD)
    assert "offset: board.page * BOARD_PAGE_SIZE" in source, "只带 limit 的话永远只能看第一页"
    assert 'data-act="board-prev"' in source and 'data-act="board-next"' in source
    assert "board-pager" in source, "分页条要能看见"
    assert "board.page = 0" in source, "换筛选必须回到第一页"


# --------------------------------------------------------------------------- ④ 圆圈打钩


def test_the_row_starts_with_a_circle_icon_instead_of_a_hanzi_button():
    source = _text(DASHBOARD)
    assert "todo-check" in source
    assert 'data-act="toggle"' in source
    assert "icon(open ? 'circle' : 'check_circle', 18)" in source, "未完成=空心圈、完成=绿勾圈"
    assert "dash.todo.check" in source and "dash.todo.uncheck" in source
    assert 'data-act="resolve"' not in source, "行里不许再有汉字「打钩」按钮"
    assert 'data-act="reopen"' not in source, "恢复也走同一个圆圈（再点一下）"


def test_board_defaults_to_the_unfinished_filter():
    source = _text(DASHBOARD)
    assert "status: 'open'," in source, "默认筛选必须是「未完成」"


def test_resolved_rows_stay_on_screen_until_paging_or_refresh():
    source = _text(DASHBOARD)
    assert "pinned: new Map()" in source
    assert "board.pinned.set(" in source and "board.pinned.delete(" in source
    assert source.count("board.pinned.clear()") >= 3, (
        "三个「才放下去」的时机：换筛选、手动刷新、翻页（打钩后不许立刻消失）"
    )


# --------------------------------------------------------------------------- ③ 一键检测


def test_detect_modal_reads_first_and_only_marks_the_checked_ids():
    source = _text(DASHBOARD)
    assert "function openDetectModal()" in source
    assert "api.detectTodos(" in source, "检测必须先走只读端点"
    assert "api.resolveDetectedTodos(" in source
    assert "detect-check" in source, "每条候选要有复选框，用户可以取消勾选"
    assert 'data-detect="all"' in source and 'data-detect="none"' in source
    assert "detect-confirm" in source
    assert "detect_evidence" in source, "勾选前要看得到判定依据（哪个文件不在了）"
    api_source = _text(API)
    assert "/api/todos/detect-resolve" in api_source
    assert "/api/todos/detect" in api_source


# --------------------------------------------------------------------------- i18n 与样式


def test_every_new_board_key_exists_in_both_dictionaries():
    source = _text(I18N)
    keys = [
        "dash.todo.check",
        "dash.todo.uncheck",
        "dash.todo.page.prev",
        "dash.todo.page.next",
        "dash.todo.page.info",
        "dash.todo.detect",
        "dash.todo.detect.title",
        "dash.todo.detect.summary",
        "dash.todo.detect.none",
        "dash.todo.detect.manual",
        "dash.todo.detect.select_all",
        "dash.todo.detect.select_none",
        "dash.todo.detect.confirm",
        "dash.todo.detect.none_selected",
        "dash.todo.detect.done",
    ]
    for key in keys:
        assert source.count("'" + key + "':") == 2, f"{key} 必须在 zh 与 en 两份字典里各有一条"


def test_board_styles_are_declared():
    source = _text(CSS)
    for selector in (
        ".todo-check",
        ".todo-check.is-done",
        ".board-pager",
        ".detect-list",
        ".detect-item",
    ):
        assert selector in source, selector
