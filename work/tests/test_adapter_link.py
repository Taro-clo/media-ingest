"""``app.script_adapter.run_hardlinks`` 的行为测试。

对应 BASELINE §1.4/§1.5/§1.6、ARCHITECTURE §6.5 的要点 1-10，以及「输出等价 / 绝不复制回退 / 建链失败即报错」等规则：

* **不复制、不搬家**：目标树里的每个文件都必须是真硬链接（同 dev/ino、nlink==2），
  源目录必须逐字节、逐 mtime 未变；目标已存在同名文件时只报失败，绝不覆盖。
* **跨文件系统/跨挂载只警告、不硬拦**（级联预检的只读实证口径修订）：预筛结论（挂载身份 → `st_dev`，
  三态 `bool | None`）只进 ``outcome.warnings``，照跑 —— "建不出链"由逐文件的
  ``os.link`` 报进 ``outcome.failed``。预筛判据**必要不充分**（挂载 ID 相同也可能 EXDEV），
  硬拦会把"挂载配错"升级成"整个库一个文件都处理不了"；任何情况下都不回退成 ``shutil.copy``。
* **日志连续性**：``hard_links.log`` 是"已处理"判定的唯一来源，
  一行一个一级文件夹，体积一致即跳过（幂等）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mlm_helpers as H

# 故意包含：二级目录、非白名单扩展名、多个一级文件夹、空文件夹（单独构造）。
MEDIA_TREE = {
    "Show A/Season 1/ep01.mkv": 11,
    "Show A/Season 1/ep02.ass": 22,
    "Show A/Season 1/Subs/extra.srt": 33,
    "Show A/Movie.mkv": 44,
    "Show B/ep03.mp4": 55,
}
FLAT_TREE = {"Show A/a.mkv": 10, "Show A/b.mkv": 20, "Show B/c.mkv": 30}
SHOW_A_SIZE = 11 + 22 + 33 + 44


@pytest.fixture
def hardlink_log(log_dir):
    """应用数据卷里的 ``hard_links.log``（日志落在 /data，不写媒体库目录）。"""
    return str(log_dir / "hard_links.log")


def _failed_message_for(outcome, expected_path):
    matches = [msg for path, msg in outcome.failed if H.same_path(path, expected_path)]
    assert matches, f"没有找到 {expected_path} 的失败记录，实际 failed={outcome.failed}"
    return matches[0]


def _log_lines(log_file):
    return [line for line in H.read_text(log_file).splitlines() if line.strip()]


# --------------------------------------------------------------------------- 全量


def test_full_run_links_every_nested_file(
    adapter, build_tree, source_dir, target_dir, hardlink_log, hardlink_assert
):
    """全量运行：递归建链、stdout 被捕获、源目录不被写。"""
    build_tree(source_dir, MEDIA_TREE)
    with H.source_guard(str(source_dir)):
        outcome = adapter.run_hardlinks(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            log_file=hardlink_log,
        )

    assert outcome.exception is None, outcome.exception
    assert outcome.folder == "*", "folder 为空时记为通配 *"
    assert outcome.failed == [], outcome.failed
    assert len(outcome.created) == len(MEDIA_TREE)
    for path in outcome.created:
        assert os.path.isfile(path), f"created 里报告了不存在的文件: {path}"
        assert H.norm(path).startswith(H.norm(target_dir) + "/")

    # 目标树 = 源树的逐文件对应，且每个都是**真**硬链接（不是复制品）。
    pairs = H.relative_link_pairs(source_dir, target_dir)
    assert len(pairs) == len(MEDIA_TREE)
    for src, dst in pairs:
        hardlink_assert(src, dst)

    # 源内容逐字节未变（复制回退会改写源，这里顺便钉住 payload）。
    assert H.read_bytes(source_dir / "Show A" / "Season 1" / "Subs" / "extra.srt") == b"x" * 33

    assert outcome.stdout, "脚本 stdout 必须被 redirect_stdout 捕获，而不是漏到控制台"
    assert outcome.stdout.count("已创建硬链接: ") == len(MEDIA_TREE)


def test_documented_log_line_per_first_level_folder(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """日志格式冻结：``%(asctime)s - Source: … - Folder: … - Folder size: …``。"""
    build_tree(source_dir, FLAT_TREE)
    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=hardlink_log
    )
    assert outcome.exception is None, outcome.exception
    assert sorted(outcome.logged) == [("Show A", 30), ("Show B", 30)]

    lines = _log_lines(hardlink_log)
    assert len(lines) == 2, f"一级文件夹两个，日志就该正好两行: {lines}"
    for line in lines:
        assert " - Source: " in line
        assert " - Folder: " in line
        assert " - Folder size: " in line
        head, _, tail = line.partition(" - Source: ")
        assert head.strip(), "asctime 必须是行首（脚本按 ' - ' 切分日志）"
        assert tail.startswith(H.norm(source_dir) + " - Folder: ")


def test_second_run_with_same_sizes_is_skipped(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """幂等：体积一致 → “文件夹已记录且体积一致，跳过”，且不再追加日志行。"""
    build_tree(source_dir, FLAT_TREE)
    first = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=hardlink_log
    )
    assert first.exception is None, first.exception
    assert len(first.created) == 3

    with H.source_guard(str(source_dir)):
        second = adapter.run_hardlinks(
            source_dir=str(source_dir), target_dir=str(target_dir), log_file=hardlink_log
        )
    assert second.exception is None, second.exception
    assert sorted(second.skipped) == ["Show A", "Show B"]
    assert second.created == []
    assert second.failed == [], "跳过分支不该有任何建链失败"
    assert "文件夹已记录且体积一致，跳过: " in second.stdout
    assert len(_log_lines(hardlink_log)) == 2, "跳过分支不得重写日志"


def test_scoped_run_only_touches_the_named_folder(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """``folder=`` 定向：只处理这一个一级文件夹，其余目录连目标目录都不建。"""
    build_tree(source_dir, MEDIA_TREE)
    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir),
        target_dir=str(target_dir),
        log_file=hardlink_log,
        folder="Show A",
    )
    assert outcome.exception is None, outcome.exception
    assert outcome.folder == "Show A"
    assert outcome.logged == [("Show A", SHOW_A_SIZE)]
    assert len(outcome.created) == 4, outcome.stdout
    assert not (target_dir / "Show B").exists(), "定向运行不得越界处理 Show B"
    assert len(_log_lines(hardlink_log)) == 1


# --------------------------------------------------------------------------- 边界


def test_existing_target_file_fails_without_overwriting(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """目标已存在同名文件 → 报失败、保留原内容、绝不覆盖（BASELINE §1.5）。"""
    build_tree(source_dir, FLAT_TREE)
    pre_existing = target_dir / "Show A" / "a.mkv"
    pre_existing.parent.mkdir(parents=True, exist_ok=True)
    pre_existing.write_bytes(b"PRE-EXISTING-CONTENT")

    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=hardlink_log
    )
    assert outcome.exception is None, outcome.exception
    assert len(outcome.created) == 2, outcome.stdout
    assert len(outcome.failed) == 1, outcome.failed

    message = _failed_message_for(outcome, pre_existing)
    low = message.lower()
    assert ("exists" in low) or ("winerror 183" in low) or ("errno 17" in low) or ("已存在" in message)

    assert pre_existing.read_bytes() == b"PRE-EXISTING-CONTENT", "绝不覆盖已存在的目标文件"
    assert os.stat(pre_existing).st_ino != os.stat(source_dir / "Show A" / "a.mkv").st_ino, (
        "已存在的目标文件不是源的硬链接，更不该被改成源的硬链接"
    )


def test_existing_target_hardlink_is_idempotent_not_a_failure(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """目标已存在**且就是源文件的硬链接** ⇒ 幂等跳过，不算失败（修掉的真实事故）。

    与上一条的唯一区别是 inode：同一个 ``(st_dev, st_ino)`` ⇒ ``force`` 重跑不再把任务
    判成失败（否则"给一个有命名模板的库重跑"永远是红的）；不是同一个 inode ⇒ 照旧逐条
    报失败，既不覆盖也不复制（B8 契约不动摇）。原生实现依旧打印失败行，
    改的只是**账**，所以 stdout 等价性契约一个字都没动。
    """
    build_tree(source_dir, FLAT_TREE)
    first = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=hardlink_log
    )
    assert len(first.created) == 3, first.stdout

    forced = adapter.run_hardlinks(
        source_dir=str(source_dir),
        target_dir=str(target_dir),
        log_file=hardlink_log,
        force=True,
    )
    assert forced.created == []
    assert forced.failed == [], "同 inode 的已存在文件不该算失败"
    assert sorted(os.path.basename(p) for p in forced.already_linked) == [
        "a.mkv",
        "b.mkv",
        "c.mkv",
    ], forced.already_linked
    assert "创建硬链接失败: " in forced.stdout, "基线输出必须原样保留"
    assert "已创建硬链接: " not in forced.stdout


def test_cross_filesystem_warns_but_still_runs_the_script_without_copying(
    adapter, build_tree, source_dir, target_dir, log_dir
):
    """B8（级联预检的只读实证口径修订）：跨设备/跨挂载**警告但允许跑**，结论以实际 ``os.link`` 为准。

    旧口径"`st_dev` 不同就不跑"已退场：预筛（挂载身份 → `st_dev`）**必要不充分**，
    ``False`` 只说明"很可能失败"，不是"不许尝试" —— 硬拦会把"挂载配错"升级成"整个库建不出链"。
    现在照跑、逐文件报 ``EXDEV``，无论成败都**绝不**回退成复制（B8）。
    """
    other_device = H.find_other_device_dir(source_dir)
    if not other_device:
        pytest.skip("单文件系统环境，无法构造跨设备场景")

    build_tree(source_dir, FLAT_TREE)
    cross_target = str(Path(other_device) / "mlm_cross_target")  # 故意不创建
    log_file = str(log_dir / "cross.log")

    with H.source_guard(str(source_dir)):
        outcome = adapter.run_hardlinks(
            source_dir=str(source_dir), target_dir=cross_target, log_file=log_file
        )

    # 1) 预筛结论进 warnings 并写明"仍会尝试"，而不是变成异常/硬拦。
    joined = " ".join(outcome.warnings)
    assert ("跨挂载" in joined) or ("跨文件系统" in joined), outcome.warnings
    assert "仍会尝试建链" in joined, outcome.warnings
    assert outcome.exception is None, outcome.exception

    # 2) 结论以实际的 os.link 为准：一个都没建成，且逐条报失败（不静默）。
    assert outcome.created == [], outcome.created
    assert outcome.failed, "跨设备下必须逐条报失败，而不是静默成功"
    messages = " ".join(msg for _path, msg in outcome.failed).lower()
    assert ("cross-device" in messages) or ("exdev" in messages) or ("errno 18" in messages), (
        outcome.failed
    )

    # 3) 绝不回退成复制：目标侧不许出现任何**普通文件**（目录由 makedirs 建出，允许存在）。
    target_root = Path(cross_target)
    if target_root.exists():
        copied = [p for p in target_root.rglob("*") if p.is_file()]
        assert copied == [], f"绝不回退成复制（B8），目标侧出现了文件：{copied}"


def test_missing_source_dir_returns_an_exception_field(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """源目录不存在（建链阶段对源目录 listdir 会抛 FileNotFoundError）⇒ 适配层转成 ``exception`` 字段。"""
    build_tree(source_dir, FLAT_TREE)
    missing = str(Path(source_dir).parent / "NoSuchFolder")
    outcome = adapter.run_hardlinks(
        source_dir=missing, target_dir=str(target_dir), log_file=hardlink_log
    )
    assert outcome.exception is not None
    assert "源目录不存在或不可访问" in outcome.exception
    assert H.norm(missing) in outcome.exception
    assert outcome.created == [] and outcome.stdout == ""
    assert not (target_dir / "Show A").exists(), "源目录都不存在，不该建任何目标目录"


def test_empty_folder_is_logged_with_size_zero(
    adapter, build_tree, source_dir, target_dir, hardlink_log
):
    """空文件夹（体积 0）也要建目标目录并记录一行，否则每轮都会重扫。"""
    (source_dir / "Empty Show").mkdir(parents=True, exist_ok=True)
    build_tree(source_dir, {"Show B/c.mkv": 30})

    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=hardlink_log
    )
    assert outcome.exception is None, outcome.exception
    assert sorted(outcome.logged) == [("Empty Show", 0), ("Show B", 30)]
    assert (target_dir / "Empty Show").is_dir()
    assert any("Folder size: 0" in line for line in _log_lines(hardlink_log))


def test_every_target_file_is_a_real_hardlink_never_a_copy(
    adapter, build_tree, source_dir, target_dir, hardlink_log, hardlink_assert
):
    """无复制回退的正面证据：逐个文件校验 (dev, ino) 与 nlink。"""
    build_tree(source_dir, MEDIA_TREE)
    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=hardlink_log
    )
    assert outcome.exception is None and outcome.failed == []
    for src, dst in H.relative_link_pairs(source_dir, target_dir):
        hardlink_assert(src, dst)
    # 目标树里没有任何"多出来"的文件/目录
    assert H.tree_relpaths(target_dir) == H.tree_relpaths(source_dir)
