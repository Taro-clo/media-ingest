"""``hard_links.log`` 只记记账行（「日志隔离」）。

背景：``app.script_adapter.ensure_file_logger`` 把 ``FileHandler`` 挂在 **root logger**
上（为了接管在 root.handlers 非空时变成 no-op 的
``basicConfig``）。root logger 是进程级单例，于是**整个应用**的日志记录 —— uvicorn 访问
日志、执行器任务日志、异常回溯 —— 都会顺着这个 handler 被灌进每个库自己的
``hard_links.log``，真实实例上表现为该文件无限膨胀。

本组用例的口径：

* 这个文件是**数据型**的：只用来判断"是否跳过、是否改名"；
* 与判断无关的信息都不应记录进来；
* 数据型日志**完全不清理** —— 已存在的行一个字节都不能删。

因此本文件钉住三件事：

1. 两类记账行照旧落盘，格式逐字不变（``_parse_log_line`` 仍能解析它们）；
2. 其它记录**在这个 handler 上**被丢弃，并且**不影响别的 handler**（控制台、任务日志照旧）；
3. 反复 ``ensure`` 同一个文件不会丢历史行、不会重复挂 handler —— 过滤不是清理。
"""

from __future__ import annotations

import logging
import os

import pytest

from app.script_adapter import ensure_file_logger

SOURCE_LINE = "Source: /vol1/1000/src - Folder: Season 1 - Folder size: 123"
ORPHAN_LINE = "Deleted orphaned hard link folder: /vol1/1000/tgt/old"


@pytest.fixture
def log_file(log_dir):
    """应用数据卷里的 ``hard_links.log``（日志落在 /data）。"""
    return str(log_dir / "hard_links.log")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _same_file_handlers(log_file: str) -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
        and os.path.abspath(getattr(handler, "baseFilename", "")) == os.path.abspath(log_file)
    ]


def test_bookkeeping_lines_are_written_verbatim(log_file):
    ensure_file_logger(log_file)
    logger = logging.getLogger("app.processors.hardlink")
    logger.info(SOURCE_LINE)
    logger.info(ORPHAN_LINE)

    text = _read(log_file)
    # 格式必须是 "%(asctime)s - %(message)s"（_parse_log_line 依赖这个形状）。
    assert f" - {SOURCE_LINE}\n" in text
    assert f" - {ORPHAN_LINE}\n" in text


def test_unrelated_records_never_reach_the_file(log_file):
    ensure_file_logger(log_file)
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /api/tasks HTTP/1.1" 200')
    logging.getLogger("uvicorn.error").warning("Started server process [1]")
    logging.getLogger("app.executor").warning("任务 7：3 个文件因为目标名被占用而加了后缀保留")
    logging.getLogger("app.routers.tasks").error("读任务列表失败")

    assert _read(log_file) == "", "记账行之外的记录不该进库日志文件"


def test_near_misses_are_dropped(log_file):
    """形似而实非的记账行也要丢 —— 判据是两类行的完整形状，不是前缀巧合。"""
    ensure_file_logger(log_file)
    logger = logging.getLogger("app.processors.hardlink")
    logger.info("Source: /vol1/1000/src")
    logger.info("Source: /vol1/1000/src - Folder: Season 1")
    logger.info("Deleted orphaned hard link folder")
    logger.info("folder size: 123")

    assert _read(log_file) == ""


def test_filter_is_scoped_to_that_handler(log_file):
    """过滤只作用于库日志 handler：别的 handler 必须照旧收到全部记录。"""
    seen: list[str] = []

    class _Spy(logging.Handler):
        def emit(self, record):  # noqa: D102 - 测试替身
            seen.append(record.getMessage())

    spy = _Spy(level=logging.INFO)
    root = logging.getLogger()
    root.addHandler(spy)
    try:
        ensure_file_logger(log_file)
        logging.getLogger("app.executor").info("只在别的 handler 里出现")
    finally:
        root.removeHandler(spy)

    assert seen == ["只在别的 handler 里出现"]
    assert _read(log_file) == ""


def test_existing_lines_survive_and_the_handler_is_not_duplicated(log_file):
    """过滤不是清理：历史行原样保留；重复 ensure 不叠 handler。"""
    existing = "2026-10-13 20:00:00,000 - Source: /old - Folder: Season 1 - Folder size: 7\n"
    with open(log_file, "w", encoding="utf-8") as handle:
        handle.write(existing)

    first = ensure_file_logger(log_file)
    second = ensure_file_logger(log_file)
    assert first is second
    assert _same_file_handlers(log_file) == [first]

    logging.getLogger("app.processors.hardlink").info(SOURCE_LINE)
    text = _read(log_file)
    assert text.startswith(existing)
    assert text.count(SOURCE_LINE) == 1


def test_app_logging_pollution_can_be_reproduced_and_is_filtered(log_file):
    """回归：先证明旧症状（root handler 收到全应用记录）确实存在，再证明它被挡住。"""
    handler = ensure_file_logger(log_file)
    assert any(isinstance(f, logging.Filter) for f in handler.filters), "handler 必须挂过滤器"

    # 这条记录会到达 root（证明污染路径存在），但被 handler 的过滤器挡在文件之外。
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%s - %s", args=("127.0.0.1", '"GET / HTTP/1.1" 200'), exc_info=None,
    )
    assert handler.filter(record) is False
    logging.getLogger("uvicorn.access").handle(record)
    assert _read(log_file) == ""

    # 记账行则必须放行（getMessage() 支持 %-参数，_parse_log_line 拿到的是成品）。
    # 注意 Python 3.12 起 ``Handler.filter()`` 放行时返回的是 **LogRecord 本身**
    # （3.11 及更早返回 True），所以断言"不是 False"而不是"is True"。
    allowed = handler.filter(
        logging.LogRecord(
            name="app.processors.hardlink", level=logging.INFO, pathname=__file__, lineno=1,
            msg=SOURCE_LINE, args=(), exc_info=None,
        )
    )
    assert allowed is not False, "记账行必须放行"
