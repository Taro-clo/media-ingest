"""孤儿清理（BASELINE §1.3 / A8）与日志条目摘除-回写的测试。

原生实现有一个**破坏性**分支：

    日志里有 ``source_dir/<folder>`` 的记录，但该源文件夹已经不存在
    → 若 ``target_dir/<folder>`` 存在，就 ``shutil.rmtree`` 掉它。

应用必须能关掉它（``orphan_cleanup=False``）。做法是：把"源已不存在"的日志行在本次
运行期间临时摘出，让实现看不到它们，跑完再原样合并回写。本文件把这三件事都钉死：

* 默认（``orphan_cleanup=True``）确实清理，且**日志行留在磁盘上**（实现只删内存字典项）；
* ``orphan_cleanup=False`` 时目标目录一根汗毛都不动，且日志文件逐字节复原；
* ``remove_log_entries`` 的定向/全量删除语义（``force`` 重跑的基础）。
"""

from __future__ import annotations

import os
import shutil

import pytest

import mlm_helpers as H

TREE = {"Show A/x.mkv": 10, "Show A/Sub/y.mkv": 5, "Show B/z.mkv": 20}
TS = "2026-01-01 00:00:00,000"


@pytest.fixture
def hardlink_log(log_dir):
    return str(log_dir / "hard_links.log")


def _line(source_dir, folder, size, ts=TS):
    return f"{ts} - Source: {H.norm(source_dir)} - Folder: {folder} - Folder size: {size}"


def _write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _run(adapter, source_dir, target_dir, log_file, **kwargs):
    return adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file, **kwargs
    )


# --------------------------------------------------------------------------- 默认：清理


def test_default_cleanup_removes_an_orphaned_target_folder(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    build_tree(source_dir, TREE)
    first = _run(adapter, source_dir, target_dir, hardlink_log)
    assert first.exception is None, first.exception
    assert len(first.created) == 3
    lines_after_first = H.read_text(hardlink_log).splitlines()
    assert len([line for line in lines_after_first if line.strip()]) == 2

    shutil.rmtree(source_dir / "Show A")  # 模拟下载目录被删/搬走

    second = _run(adapter, source_dir, target_dir, hardlink_log)
    assert second.exception is None, second.exception
    assert len(second.orphans_removed) == 1, second.stdout
    assert H.same_path(second.orphans_removed[0], target_dir / "Show A")
    assert "Deleted orphaned hard link folder: " in second.stdout

    assert not (target_dir / "Show A").exists(), "默认行为确实会递归删除孤儿目标目录"
    assert (target_dir / "Show B" / "z.mkv").exists(), "不许误删还在的库目录"
    assert sorted(second.skipped) == ["Show B"]

    # 实现只删内存字典项，**不重写日志文件**。
    text = H.read_text(hardlink_log)
    assert "Folder: Show A" in text, "脚本不会重写日志，Show A 的记录仍在磁盘上"


def test_orphan_cleanup_false_leaves_the_library_and_the_log_untouched(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """``orphan_cleanup=False``：不删目标、日志逐字节复原（摘出 → 回写）。"""
    build_tree(source_dir, TREE)
    first = _run(adapter, source_dir, target_dir, hardlink_log)
    assert first.exception is None, first.exception
    log_before = H.read_bytes(hardlink_log)

    shutil.rmtree(source_dir / "Show A")

    with H.source_guard(str(target_dir)):
        second = _run(adapter, source_dir, target_dir, hardlink_log, orphan_cleanup=False)

    assert second.exception is None, second.exception
    assert second.orphans_removed == []
    assert "Deleted orphaned hard link folder" not in second.stdout
    assert (target_dir / "Show A" / "x.mkv").exists(), "关掉清理就绝不许删库"
    assert (target_dir / "Show A" / "Sub" / "y.mkv").exists()
    assert H.read_bytes(hardlink_log) == log_before, "临时摘出的日志行必须原样回写"


def test_orphan_cleanup_false_also_holds_on_the_ledger_path(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """``skip`` 是每库账本时，孤儿分支遍历的是**这份 dict**、不是日志行。

    不先把"源侧一级文件夹已不存在"的键摘掉，``orphan_cleanup=False`` 就会形同虚设 ——
    源作品被删掉后第二次运行仍会 ``rmtree`` 掉目标侧整个作品文件夹。本用例把两条路径
    （日志行 / 账本 dict）都钉死，并留一个反例对照证明默认行为没被削弱。
    """
    build_tree(source_dir, TREE)
    first = _run(adapter, source_dir, target_dir, hardlink_log)
    assert first.exception is None, first.exception
    ledger = {"Show A": 15, "Show B": 20}  # app/link_records.py 给出的形状

    shutil.rmtree(source_dir / "Show A")

    with H.source_guard(str(target_dir)):
        second = _run(
            adapter,
            source_dir,
            target_dir,
            hardlink_log,
            orphan_cleanup=False,
            skip=dict(ledger),
        )

    assert second.exception is None, second.exception
    assert second.orphans_removed == []
    assert "Deleted orphaned hard link folder" not in second.stdout
    assert (target_dir / "Show A" / "x.mkv").exists(), "关掉清理就绝不许删库（账本路径也一样）"
    assert (target_dir / "Show A" / "Sub" / "y.mkv").exists()
    assert sorted(second.skipped) == ["Show B"]
    assert ledger == {"Show A": 15, "Show B": 20}, "调用方的账本 dict 不该被就地改写"

    # 反例对照：同一份账本 + 默认 orphan_cleanup=True ⇒ 照旧删掉孤儿目标目录。
    third = _run(adapter, source_dir, target_dir, hardlink_log, skip=dict(ledger))
    assert len(third.orphans_removed) == 1, third.stdout
    assert H.same_path(third.orphans_removed[0], target_dir / "Show A")
    assert not (target_dir / "Show A").exists()


def test_orphan_without_a_target_folder_is_harmless(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    (source_dir / "Show B").mkdir(parents=True, exist_ok=True)
    build_tree(source_dir, {"Show B/z.mkv": 20})
    _write_lines(
        hardlink_log,
        [_line(source_dir, "Vanished", 7), _line(source_dir, "Show B", 20)],
    )
    outcome = _run(adapter, source_dir, target_dir, hardlink_log)
    assert outcome.exception is None, outcome.exception
    assert outcome.orphans_removed == [], "目标目录本来就不存在，没什么可删的"
    assert not (target_dir / "Vanished").exists()
    assert sorted(outcome.skipped) == ["Show B"]


def test_a_folder_that_still_exists_is_never_removed(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    build_tree(source_dir, TREE)
    _run(adapter, source_dir, target_dir, hardlink_log)
    with H.source_guard(str(target_dir)):
        outcome = _run(adapter, source_dir, target_dir, hardlink_log)
    assert outcome.orphans_removed == []
    assert sorted(H.tree_relpaths(target_dir)) == sorted(H.tree_relpaths(source_dir))


# --------------------------------------------------------------------------- 摘除-回写单元


def test_drop_and_restore_lines_preserve_the_file_byte_for_byte(
    adapter, build_tree, source_dir, hardlink_log, log_dir
):
    """摘出的行必须插回**原来的位置**，且只摘当前源目录里"源已不存在"的那些。"""
    build_tree(source_dir, {"Show B/z.mkv": 20})
    other_source = str(H.norm(source_dir).replace("/Bangumi", "/Movie"))
    original = [
        _line(other_source, "Keep Me", 1),
        _line(source_dir, "Vanished", 2),  # ← 唯一该被摘出的行
        _line(source_dir, "Show B", 20),
        "this is not a log record",
        "",
    ]
    _write_lines(hardlink_log, original)
    before = H.read_bytes(hardlink_log)

    removed = adapter._drop_missing_source_lines(hardlink_log, H.norm(source_dir))
    assert len(removed) == 1
    index, line = removed[0]
    assert index == 1 and "Folder: Vanished" in line

    on_disk = H.read_text(hardlink_log).splitlines()
    assert len(on_disk) == 4
    assert "Folder: Vanished" not in H.read_text(hardlink_log)

    adapter._restore_lines(hardlink_log, removed)
    assert H.read_bytes(hardlink_log) == before, "回写必须逐字节复原（含空行与畸形行）"


def test_restore_lines_never_duplicates_an_identical_line(
    adapter, source_dir, hardlink_log
):
    """摘出-回写不得把同一行写两份（``_restore_lines`` 按**整行内容**去重）。

    实测边界（已记入文档，不改代码）：去重是逐字节比较整行，而不是"按 folder 去重"，
    所以"同一 folder + 不同时间戳"的两行都会留下。这个差异无害——记录
    ``logged_folders[folder] = size`` 时后出现的行覆盖前一行；而且现实里不可达：
    被摘出的行之所以被摘出，正是因为源文件夹不存在，
    而实现只会遍历确实存在的文件夹，不可能在同一次运行里重新记录它。
    这里只钉住可达的那一半：完全相同的行绝不重复。
    """
    stale = _line(source_dir, "Show B", 20, ts="2026-01-01 00:00:00,000")
    _write_lines(hardlink_log, [stale])

    # 走真实路径取出待回写的行（行内容带换行符，与 _read_lines 一致）
    removed = adapter._drop_missing_source_lines(hardlink_log, H.norm(source_dir))
    assert len(removed) == 1 and "Folder: Show B" in removed[0][1]
    assert H.read_text(hardlink_log).strip() == "", "临时摘出后文件里不该还有这条记录"

    _write_lines(hardlink_log, [stale])  # 实现把同一行又写了一遍
    adapter._restore_lines(hardlink_log, removed)

    lines = [line for line in H.read_text(hardlink_log).splitlines() if line.strip()]
    assert lines == [stale], "同一行不许出现两次，否则日志会随运行次数膨胀"


# --------------------------------------------------------------------------- force / 定向删除


def test_remove_log_entries_removes_only_the_named_folder(
    adapter, build_tree, source_dir, hardlink_log
):
    other_source = str(H.norm(source_dir).replace("/Bangumi", "/Movie"))
    _write_lines(
        hardlink_log,
        [
            _line(source_dir, "Show A", 10),
            _line(source_dir, "Show B", 20),
            _line(other_source, "Show A", 10),
            "garbage",
        ],
    )
    removed = adapter.remove_log_entries(hardlink_log, H.norm(source_dir), "Show A")
    assert removed == 1
    text = H.read_text(hardlink_log)
    assert _line(source_dir, "Show A", 10) not in text
    assert _line(source_dir, "Show B", 20) in text
    assert _line(other_source, "Show A", 10) in text, "别的源目录不许被同一个 folder 名误伤"
    assert "garbage" in text


def test_remove_log_entries_with_a_wildcard_folder_drops_the_whole_source(
    adapter, source_dir, hardlink_log
):
    other_source = str(H.norm(source_dir).replace("/Bangumi", "/Movie"))
    _write_lines(
        hardlink_log,
        [
            _line(source_dir, "Show A", 10),
            _line(source_dir, "Show B", 20),
            _line(other_source, "Show A", 10),
            "garbage",
        ],
    )
    for folder in (None, "", "*"):
        _write_lines(
            hardlink_log,
            [
                _line(source_dir, "Show A", 10),
                _line(source_dir, "Show B", 20),
                _line(other_source, "Show A", 10),
                "garbage",
            ],
        )
        assert adapter.remove_log_entries(hardlink_log, H.norm(source_dir), folder) == 2
        text = H.read_text(hardlink_log)
        assert "Folder: Show B" not in text
        assert "garbage" in text, "畸形行不是记录，删不得"
        assert _line(other_source, "Show A", 10) in text, "别的源目录一律不碰"


def test_remove_log_entries_is_a_noop_when_nothing_matches(
    adapter, source_dir, hardlink_log, log_dir
):
    _write_lines(hardlink_log, [_line(source_dir, "Show A", 10)])
    assert adapter.remove_log_entries(hardlink_log, H.norm(source_dir), "Nope") == 0
    assert adapter.remove_log_entries(hardlink_log, "/unmapped/root", None) == 0
    assert len([line for line in H.read_text(hardlink_log).splitlines() if line.strip()]) == 1
    missing = str(log_dir / "never_created.log")
    assert adapter.remove_log_entries(missing, H.norm(source_dir), None) == 0
    assert not os.path.exists(missing), "日志不存在时不该顺手创建它"


def test_force_rebuilds_links_and_replaces_the_record(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """A8：``force=True`` 先删记录，就不会走"体积一致即跳过"分支。"""
    build_tree(source_dir, TREE)
    first = _run(adapter, source_dir, target_dir, hardlink_log)
    assert first.exception is None and len(first.created) == 3

    forced = _run(adapter, source_dir, target_dir, hardlink_log, force=True)
    assert forced.exception is None, forced.exception
    assert forced.skipped == [], "force 的意义就是不能命中跳过分支"
    # 目标已经在之前的工作里建好 → 再 os.link 必然 File exists；这些**同一 inode** 的
    # 已存在文件改判成 already_linked（幂等跳过），所以 failed 为空、任务不再被判失败。
    # 原生实现的 stdout 照旧打印失败行（等价性契约优先）。
    assert forced.failed == [], forced.stdout
    assert len(forced.already_linked) == 3, forced.already_linked
    assert "创建硬链接失败: " in forced.stdout
    assert forced.created == []
    for path in forced.already_linked:
        assert H.norm(path).startswith(H.norm(target_dir) + "/")

    lines = [line for line in H.read_text(hardlink_log).splitlines() if line.strip()]
    assert len(lines) == 2, "旧记录被删、新记录写上：总数仍是一级文件夹数"
    assert sorted(forced.logged) == [("Show A", 15), ("Show B", 20)]
