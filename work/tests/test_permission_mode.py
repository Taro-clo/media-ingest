"""``permission_mode`` 的行为测试（默认关闭、开启需确认）。

这个开关是一个**默认关闭的显式开关**（不是预留字段：预留字段会界面可设、永不生效）。
现在它的语义是：

* **默认关闭**（库里是 0）时，建链过程**一个 chmod 都不做** —— 默认路径逐字不变；
* 开启后，把**目标库根目录**与**本次处理到的每个一级文件夹的目标侧整棵子树**
  （含"体积一致 ⇒ 跳过"的旧作品，否则开关对已入库内容静默无效）改成 0777；
* 硬链接与源文件**共享同一个 inode** ⇒ 源文件的权限位一起变（内容、大小、inode 号、
  mtime 都不变）。这是已接受并写进确认框的已知副作用，因此这里也**正面钉住**它，
  而不是假装不存在。源侧的**目录**不共享 inode（目标目录是新建的），所以不受影响 ——
  这条边界同样有断言钉住，免得下次有人把"文件跟着变"错读成"整棵树跟着变"。

权限位断言因此只在 POSIX 上有意义（Windows 的 chmod 只有只读位）。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

_POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="权限位语义只在 POSIX 上有意义（Windows 的 chmod 只有只读位）"
)

MODE = 0o777


# --------------------------------------------------------------------------- 工具


def _mode(path) -> int:
    """实际权限位（不含文件类型位）。"""
    return stat.S_IMODE(os.lstat(str(path)).st_mode)


def _tight_work(source_dir, name: str = "Show A"):
    """建一个"下载器风格"的作品：目录 0700、文件 0600，并返回关键路径。"""
    folder = Path(str(source_dir)) / name
    season = folder / "Season 1"
    season.mkdir(parents=True)
    media = season / "ep01.mkv"
    media.write_bytes(b"x" * 10)
    os.chmod(folder, 0o700)
    os.chmod(season, 0o700)
    os.chmod(media, 0o600)
    return folder, season, media


def _native():
    import app.processors.hardlink as native

    return native


def _insert_library(db_mod, name: str, source: str, target: str, permission_mode: int = 0) -> int:
    """插一条 ``hardlink_only`` 库（``permission_mode`` 由调用方给）。"""
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO libraries(name,type,processor,processor_id,enabled,source_dir,target_dir,"
            "match_categories,match_tags,match_path_prefixes,orphan_cleanup,permission_mode,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "Bangumi", "hardlink_only", "hardlink_only", 1, source, target,
             "[]", "[]", "[]", 0, permission_mode, stamp, stamp),
        )
        return int(cursor.lastrowid)


def _insert_task(db_mod, library_id: int, name: str) -> int:
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,result,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (library_id, name, "hardlink", "manual", "queued",
             json.dumps({"mode": "full"}), "{}", "{}", stamp),
        )
        return int(cursor.lastrowid)


def _task_result(db_mod, task_id: int) -> dict:
    row = db_mod.get_conn().execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
    return json.loads((dict(row).get("result") if row else None) or "{}")


def _task_logs(db_mod, task_id: int) -> list[str]:
    rows = db_mod.get_conn().execute(
        "SELECT message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    return [dict(row)["message"] for row in rows]


# --------------------------------------------------------------------------- 原生实现


@_POSIX_ONLY
def test_default_off_is_byte_identical_to_the_old_behaviour(source_dir, target_dir, log_dir):
    """默认（不传 ``permission_mode``）时：不 chmod，源文件权限位一动不动。"""
    folder, season, media = _tight_work(source_dir)
    native = _native()
    native.LOG_FILE = str(Path(str(log_dir)) / "hard_links.log")

    native.create_hard_links(str(source_dir), str(target_dir))

    target_media = Path(str(target_dir)) / "Show A" / "Season 1" / "ep01.mkv"
    assert target_media.exists(), "默认路径必须照旧建出硬链接"
    assert _mode(media) == 0o600, "默认关闭时源文件权限位绝不能被改"
    assert _mode(target_media) == 0o600, "硬链接与源文件同 inode ⇒ 权限位必然一致"
    assert _mode(folder) == 0o700
    assert native.last_permission_stats() == {"dirs": 0, "files": 0, "errors": []}


@_POSIX_ONLY
def test_enabled_fixes_the_whole_target_tree(source_dir, target_dir, log_dir):
    """开启后：目标侧目录/文件都变成 0777 —— **源文件（同 inode）也是**（已知副作用）。"""
    folder, season, media = _tight_work(source_dir)
    native = _native()
    native.LOG_FILE = str(Path(str(log_dir)) / "hard_links.log")

    native.create_hard_links(str(source_dir), str(target_dir), permission_mode=True)

    target_folder = Path(str(target_dir)) / "Show A"
    target_season = target_folder / "Season 1"
    target_media = target_season / "ep01.mkv"
    for path in (target_dir, target_folder, target_season, target_media):
        assert _mode(path) == MODE, f"{path} 应被改成 0777"
    # 共享 inode ⇒ 源侧**文件**一起变。这是确认框里写明、用户接受的代价，必须被测试正面钉住。
    assert _mode(media) == MODE, "硬链接与源文件同 inode ⇒ 源文件权限位一起变（已知副作用）"
    # 源侧**目录**不共享 inode（目标侧目录是新建的）⇒ 权限位不动。边界也要钉住。
    assert _mode(folder) == 0o700, "源侧目录不是硬链接 ⇒ 权限位不应被改"
    assert _mode(season) == 0o700, "源侧 Season 1 同理"
    assert media.read_bytes() == b"x" * 10, "内容绝不能变"

    stats = native.last_permission_stats()
    assert stats["files"] == 1, stats
    assert stats["dirs"] >= 3, stats  # 库根 + Show A + Season 1
    assert stats["errors"] == []


@_POSIX_ONLY
def test_enabled_also_fixes_folders_that_are_skipped(source_dir, target_dir, log_dir):
    """**跳过分支**也要修权限：否则开关对"已经入库过"的作品静默无效。"""
    _tight_work(source_dir)
    native = _native()
    native.LOG_FILE = str(Path(str(log_dir)) / "hard_links.log")

    # 第一轮：开关关闭 ⇒ 只有 0600 的链接。
    native.create_hard_links(str(source_dir), str(target_dir))
    target_media = Path(str(target_dir)) / "Show A" / "Season 1" / "ep01.mkv"
    assert _mode(target_media) == 0o600

    # 第二轮：账本说体积一致（一条新链都不建），但开关打开了。
    size = sum(
        os.path.getsize(os.path.join(root, name))
        for root, _dirs, files in os.walk(str(Path(str(source_dir)) / "Show A"))
        for name in files
    )
    native.create_hard_links(str(source_dir), str(target_dir), skip={"Show A": size},
                             permission_mode=True)

    assert _mode(target_media) == MODE, "被跳过的旧作品也必须被修好权限"
    assert _mode(Path(str(target_dir)) / "Show A") == MODE
    assert native.last_permission_stats()["files"] == 1


@_POSIX_ONLY
def test_second_run_reports_zero_because_nothing_needs_fixing(source_dir, target_dir, log_dir):
    """幂等：第二次开启运行没有可改的条目（统计数字是"改了多少"，不是"扫了多少"）。"""
    _tight_work(source_dir)
    native = _native()
    native.LOG_FILE = str(Path(str(log_dir)) / "hard_links.log")

    native.create_hard_links(str(source_dir), str(target_dir), permission_mode=True)
    assert native.last_permission_stats()["files"] == 1

    native.create_hard_links(str(source_dir), str(target_dir), permission_mode=True)
    assert native.last_permission_stats() == {"dirs": 0, "files": 0, "errors": []}


@_POSIX_ONLY
def test_symlinks_are_never_followed(source_dir, target_dir, log_dir, tmp_path):
    """目标侧出现符号链接时绝不跟进：``os.chmod`` 默认会跟，那会改到库外面的文件。"""
    _tight_work(source_dir)
    native = _native()
    native.LOG_FILE = str(Path(str(log_dir)) / "hard_links.log")
    native.create_hard_links(str(source_dir), str(target_dir))

    outside = Path(str(tmp_path)) / "outside.conf"
    outside.write_text("secret", encoding="utf-8")
    os.chmod(outside, 0o600)
    link = Path(str(target_dir)) / "Show A" / "outside.conf"
    os.symlink(str(outside), str(link))

    native.create_hard_links(str(source_dir), str(target_dir), permission_mode=True)

    assert _mode(outside) == 0o600, "符号链接的目标（库外面）绝不能被 chmod"
    assert os.path.islink(str(link)), "符号链接本身也不能被替换成普通文件"


# --------------------------------------------------------------------------- 适配层


@_POSIX_ONLY
def test_adapter_threads_the_flag_and_reports_counts(adapter, build_tree, source_dir,
                                                     target_dir, log_dir):
    """适配层把开关原样传给原生实现，并把统计带回 ``LinkOutcome``。"""
    _tight_work(source_dir)

    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir),
        target_dir=str(target_dir),
        log_file=str(Path(str(log_dir)) / "hard_links.log"),
        permission_mode=True,
    )

    assert outcome.exception is None, outcome.exception
    assert outcome.permission_files == 1, outcome.permission_files
    assert outcome.permission_dirs >= 3, outcome.permission_dirs
    assert outcome.permission_errors == []
    assert _mode(Path(str(target_dir)) / "Show A" / "Season 1" / "ep01.mkv") == MODE


@_POSIX_ONLY
def test_adapter_default_leaves_everything_alone(adapter, source_dir, target_dir, log_dir):
    """不传开关时 ``LinkOutcome`` 的三个权限字段恒为 0/空，权限位一个都不动。"""
    _folder, _season, media = _tight_work(source_dir)

    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir),
        target_dir=str(target_dir),
        log_file=str(Path(str(log_dir)) / "hard_links.log"),
    )

    assert outcome.exception is None, outcome.exception
    assert (outcome.permission_dirs, outcome.permission_files) == (0, 0)
    assert outcome.permission_errors == []
    assert _mode(media) == 0o600


# --------------------------------------------------------------------------- 执行器


@_POSIX_ONLY
def test_executor_reads_the_library_flag_and_logs_the_side_effect(
    executor_mod, db_mod, build_tree, source_dir, target_dir
):
    """执行器：从库里读开关 → 传下去 → 把"改了源文件权限位"这件事写进任务日志。"""
    _tight_work(source_dir)
    library_id = _insert_library(
        db_mod, "权限模式库", str(source_dir), str(target_dir), permission_mode=1
    )
    task_id = _insert_task(db_mod, library_id, "权限模式库")

    executor_mod.execute_task(task_id)

    result = _task_result(db_mod, task_id)
    logs = _task_logs(db_mod, task_id)
    assert result.get("link_failed") == 0, result
    assert result.get("permission_mode_fixed_files") == 1, result
    assert any("权限模式（permission_mode）" in line for line in logs), logs
    assert any("共享同一 inode" in line for line in logs), logs
    assert _mode(Path(str(target_dir)) / "Show A" / "Season 1" / "ep01.mkv") == MODE


@_POSIX_ONLY
def test_executor_does_not_touch_permissions_when_the_library_keeps_it_off(
    executor_mod, db_mod, build_tree, source_dir, target_dir
):
    """默认库（``permission_mode=0``）：跑完权限位不动，日志里也不该出现权限模式那行。"""
    _folder, _season, media = _tight_work(source_dir)
    library_id = _insert_library(db_mod, "默认库", str(source_dir), str(target_dir))
    task_id = _insert_task(db_mod, library_id, "默认库")

    executor_mod.execute_task(task_id)

    result = _task_result(db_mod, task_id)
    logs = _task_logs(db_mod, task_id)
    assert result.get("permission_mode_fixed_files") is None, result
    assert not any("权限模式（permission_mode）" in line for line in logs), logs
    assert _mode(media) == 0o600
