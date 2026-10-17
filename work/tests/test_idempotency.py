"""幂等性（A12 的调度前提 + 硬链接日志这一唯一幂等来源）。

本模块回答一个产品级问题：**同一个库反复跑，会不会不断重复劳动/不断写日志/不断重建硬链接？**
幂等来源只有一个 —— 日志里记录的 folder 体积与当前递归体积一致就整folder跳过。
所以这里要钉死的是：

* 第二遍及以后必须"什么都不做"：不建链、不重写日志、不动 inode；
* 体积变了才重新处理，而且此时**旧记录行必须先被删掉**，否则日志会重复累积；
* ``force=True`` 会删记录 ⇒ 必定重跑；此时目标侧**已经是同一 inode 的硬链接**，
  现在改判为 ``already_linked``（幂等跳过、不算失败）—— 但原生实现照旧打印的
  ``创建硬链接失败: … File exists`` 行一个字都不改，日志仍是"一行一 folder"；
* 孤儿清理是稳定的：目标已被删掉后再次运行不报错、不重复删；
* **已知漏洞（L2，历史解析缺陷）**：folder 名字里含 ``" - "`` 时日志行永远解析不回来
  ⇒ 每跑一次都新增一行日志、每次都重试建链。这是历史遗留的解析缺陷，不是本应用引入的。
  后续只修掉了它的**账面代价**：失败项改走结构化通道（``last_failures()``）+
  ``[Errno``/``[WinError`` 锚点切分，于是这些文件被正确改判成 ``already_linked``、
  ``failed`` 不再多出无害失败；日志仍照旧增长（那是既有的解析局限）。
"""

from __future__ import annotations

import os
import shutil

import pytest

import mlm_helpers as H


@pytest.fixture
def hardlink_log(log_dir):
    return str(log_dir / "hard_links.log")


def _run(adapter, source_dir, target_dir, log_file, **kwargs):
    return adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file, **kwargs
    )


def _log_text(log_file) -> str:
    with open(log_file, "r", encoding="utf-8") as handle:
        return handle.read()


def _records(log_file) -> list[str]:
    """真正能被日志解析回来的日志行（按预筛与三段 split 判定）。"""
    out = []
    for line in _log_text(log_file).splitlines():
        if " - Source: " not in line or " - Folder: " not in line or " - Folder size: " not in line:
            continue
        _timestamp, _sep, message = line.strip().partition(" - ")
        if len(message.split(" - ")) == 3:
            out.append(line)
    return out


def test_second_run_is_a_pure_noop(adapter, build_tree, source_dir, target_dir, hardlink_log):
    build_tree(source_dir, {"Show A/ep01.mkv": 10, "Show A/ep02.mkv": 20, "Show B/ep03.mkv": 30})
    first = _run(adapter, source_dir, target_dir, hardlink_log)
    assert len(first.created) == 3
    assert sorted(first.logged) == [("Show A", 30), ("Show B", 30)]

    before_log = H.read_bytes(hardlink_log)
    before_tree = H.tree_snapshot(target_dir)

    second = _run(adapter, source_dir, target_dir, hardlink_log)
    assert second.exception is None, second.exception
    assert second.created == [] and second.failed == [] and second.orphans_removed == []
    assert sorted(second.skipped) == ["Show A", "Show B"]
    assert second.logged == [], "跳过的 folder 不该被重新记录"
    assert H.read_bytes(hardlink_log) == before_log, "日志必须逐字节不变（否则会无限膨胀）"
    assert H.tree_snapshot(target_dir) == before_tree


def test_repeated_runs_never_rebuild_links(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    build_tree(source_dir, {"Show A/ep01.mkv": 10, "Show A/Sub/ep02.mkv": 20})
    _run(adapter, source_dir, target_dir, hardlink_log)
    linked = target_dir / "Show A" / "Sub" / "ep02.mkv"
    inode_before = os.stat(linked).st_ino

    for _ in range(4):
        outcome = _run(adapter, source_dir, target_dir, hardlink_log)
        assert outcome.skipped == ["Show A"]
        assert outcome.created == []
    assert os.stat(linked).st_ino == inode_before, "没有重建 ⇒ inode 不变"
    assert len(_records(hardlink_log)) == 1, "一行一 folder，重复运行不追加"


def _simulate_process_restart(adapter) -> None:
    """丢掉适配层与原生实现模块的一切内存状态，等价于"应用重启后第一次跑"。

    适配层不再缓存任何模块，所以这里只剩"重载原生实现"这一件事；
    "重启之后仍然跳过"依据的是**磁盘上的日志/账本**，不是内存缓存。
    """
    loaded = getattr(adapter, "_loaded", None)
    if isinstance(loaded, dict):
        loaded.clear()
    import importlib

    importlib.reload(adapter.runtime_module())


def test_process_restart_still_skips(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """重新 import 原生实现模块（等价于应用重启后第一次跑）也必须继续跳过。"""
    build_tree(source_dir, {"Show A/ep01.mkv": 10})
    assert len(_run(adapter, source_dir, target_dir, hardlink_log).created) == 1

    _simulate_process_restart(adapter)
    after_restart = _run(adapter, source_dir, target_dir, hardlink_log)
    assert after_restart.skipped == ["Show A"]
    assert after_restart.created == [] and after_restart.failed == []
    assert len(_records(hardlink_log)) == 1


def test_size_change_removes_the_old_record_and_keeps_one_line(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    build_tree(source_dir, {"Show A/ep01.mkv": 10, "Show A/ep02.mkv": 20})
    _run(adapter, source_dir, target_dir, hardlink_log)
    assert [line for line in _records(hardlink_log) if "Folder: Show A " in line]

    build_tree(source_dir, {"Show A/ep03.mkv": 5})  # 体积 30 -> 35
    second = _run(adapter, source_dir, target_dir, hardlink_log)

    assert second.skipped == [], "体积不一致 ⇒ 不能跳过"
    assert [H.norm(target_dir / "Show A" / "ep03.mkv")] == [H.norm(p) for p in second.created]
    # ep01/ep02 已经在目标侧、而且是**源文件自己的硬链接**（同一 inode）⇒ 幂等跳过。
    # 这类现在不再进 ``failed``：否则"体积一变就整个任务变红"，而目标里躺着的
    # 明明是正确的硬链接。（原生实现的 stdout 仍然照旧打印 File exists 行。）
    assert sorted(os.path.basename(p) for p in second.already_linked) == ["ep01.mkv", "ep02.mkv"]
    assert second.failed == [], second.failed
    records = _records(hardlink_log)
    assert len(records) == 1, "旧记录行必须先删掉再加新行，否则日志重复累积"
    assert "Folder size: 35" in records[0]
    assert "已删除旧记录: Show A" in second.stdout


def test_force_rerun_counts_already_linked_as_skipped(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """``force`` 重跑：目标里已建好的硬链接改判为"已存在"，任务不再被判失败。"""
    build_tree(source_dir, {"Show A/ep01.mkv": 10, "Show A/ep02.mkv": 20})
    _run(adapter, source_dir, target_dir, hardlink_log)

    forced = _run(adapter, source_dir, target_dir, hardlink_log, force=True)
    assert forced.skipped == [], "force 必须先删记录，不能再走文件夹级跳过"
    assert forced.created == []
    assert forced.failed == [], "同 inode 的已存在文件必须改判，不算失败"
    assert sorted(os.path.basename(p) for p in forced.already_linked) == ["ep01.mkv", "ep02.mkv"]
    # 失败行一个字都不许改：照样打印（stdout 等价性契约）。
    assert "创建硬链接失败: " in forced.stdout
    assert len(_records(hardlink_log)) == 1


def test_empty_folder_stays_skipped_forever(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    build_tree(source_dir, {"Empty Show": None, "Show A/ep01.mkv": 10})
    first = _run(adapter, source_dir, target_dir, hardlink_log)
    assert ("Empty Show", 0) in first.logged
    assert (target_dir / "Empty Show").is_dir()

    for _ in range(3):
        outcome = _run(adapter, source_dir, target_dir, hardlink_log)
        assert "Empty Show" in outcome.skipped and "Show A" in outcome.skipped


def test_orphan_cleanup_is_stable_across_repeated_runs(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    build_tree(source_dir, {"Show A/ep01.mkv": 10, "Show B/ep02.mkv": 20})
    _run(adapter, source_dir, target_dir, hardlink_log)
    assert (target_dir / "Show B").is_dir()

    shutil.rmtree(source_dir / "Show B")  # 源没了，库里还留着孤儿目录
    second = _run(adapter, source_dir, target_dir, hardlink_log)
    assert [H.norm(p) for p in second.orphans_removed] == [H.norm(target_dir / "Show B")]
    assert "Deleted orphaned hard link folder: " in second.stdout
    assert not (target_dir / "Show B").exists()
    assert "Folder: Show B" in _log_text(hardlink_log), "只删内存字典，日志文件不该被改写"

    log_after_second = H.read_bytes(hardlink_log)
    third = _run(adapter, source_dir, target_dir, hardlink_log)
    assert third.exception is None, third.exception
    assert third.orphans_removed == [], "目标已经不存在，不该重复删"
    assert "Deleted orphaned" not in third.stdout
    assert H.read_bytes(hardlink_log) == log_after_second
    assert (target_dir / "Show A").is_dir(), "在册且体积一致的 folder 不受孤儿清理影响"


def test_folder_name_containing_dash_no_longer_reports_spurious_failures(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """**L2 的一半（历史解析缺陷）**：folder 名含 ``" - "`` ⇒ 日志行永远解析失败。

    后果：该 folder 每次都走"重新处理"分支 ⇒ 每次都在日志末尾追加一行（"日志只增不减"
    的真实来源）。修复它会改变既有解析行为（既有契约禁止），所以本应用只能靠
    ``force``/手动清理兜底。测试钉住现状，避免将来被"顺手修好"漂移。

    **失败账目侧的修复**：失败的**账**不再被截断的文本连累 —— 原生实现把失败项按
    ``(目标路径, 异常文本)`` 结构化暴露（``hardlink.last_failures()``），适配层优先用它，
    文本通道的切分也改成用 ``[Errno`` / ``[WinError`` 锚点。所以第二遍起这些文件会被
    正确改判成 ``already_linked``（同 inode），``failed`` 不再凭空多出无害的失败。
    """
    build_tree(source_dir, {"Show - Dash/ep01.mkv": 10})
    first = _run(adapter, source_dir, target_dir, hardlink_log)
    assert len(first.created) == 1
    assert _records(hardlink_log) == [], "含 ' - ' 的行连解析都过不去"

    ep01 = str(target_dir / "Show - Dash" / "ep01.mkv")
    second = _run(adapter, source_dir, target_dir, hardlink_log)
    assert second.skipped == [], "解析失败 ⇒ 永远不跳过"
    # 既有行为不变：原生实现照旧打印失败行（日志/标准输出契约一个字没改）。
    assert "创建硬链接失败: " in second.stdout
    # 修复点：账面上不再算失败，而是"目标已存在且就是本文件的硬链接"。
    assert second.failed == [], second.failed
    # 实现用 os.path.relpath 取相对路径，一级目录得到 "." ⇒ 目标路径里带 "/./"
    # （stdout 也是这个形状），这里按规范化后的同一个文件比对。
    assert [os.path.normpath(p) for p in second.already_linked] == [ep01]
    assert _log_text(hardlink_log).count("Folder: Show - Dash") == 2, "日志每次多一行"

    third = _run(adapter, source_dir, target_dir, hardlink_log)
    assert third.failed == []
    assert [os.path.normpath(p) for p in third.already_linked] == [ep01]
    assert _log_text(hardlink_log).count("Folder: Show - Dash") == 3


def test_size_change_in_a_dashed_folder_never_dedupes_the_record(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """L2 的连带后果：解析失败 ⇒ 连"删旧记录"分支都进不去，只能一行行往上堆。"""
    build_tree(source_dir, {"Show - Dash/ep01.mkv": 10})
    _run(adapter, source_dir, target_dir, hardlink_log)
    build_tree(source_dir, {"Show - Dash/ep02.mkv": 5})

    second = _run(adapter, source_dir, target_dir, hardlink_log)
    assert "已删除旧记录" not in second.stdout, "解析失败 ⇒ 走不到删除分支"
    assert "文件夹已记录且体积一致，跳过" not in second.stdout
    assert _log_text(hardlink_log).count("Folder: Show - Dash") == 2
