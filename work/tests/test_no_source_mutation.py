"""B9/B8：源目录只读、绝不被写入（"零改动"是可用性承诺的一部分）。

真实部署里源目录常是 ``/downloads:ro`` 挂载的 qBittorrent 下载目录。应用的整套价值主张是
"**只读取源、只在目标建硬链接**"，一旦某条代码路径顺手在源目录里写了什么（临时文件、缓存、
chmod、rename），用户的做种/下载目录就被破坏了 —— 而且这种破坏**不会报错**，只会静静地改变
源文件。所以本文件不满足于"跑完之后内容没变"，而是用 ``sys.addaudithook`` **审计每一次写
系统调用**，把"应用是否曾**尝试**写源目录"也钉死：

* ``test_hardlink_pipeline_never_touches_the_source_tree`` —— 建链 + 审计 + 逐字节快照 + nlink 恰好 +1
* ``test_rename_pipelines_only_run_inside_the_target_root`` —— 两条重命名流水线的 ROOT_DIR 是目标
* ``test_second_and_forced_runs_keep_the_source_pristine`` —— 幂等 + force 都不许碰源
* ``test_source_directory_may_be_mounted_read_only`` —— POSIX 下把源目录 chmod 成 r-x 仍能跑
* ``test_validate_probe_never_writes_into_the_source_even_when_it_cannot_link`` —— 只读实证探针
  **只读安全**：它拿源树里已存在的文件建链接，源目录不可写（甚至写入被拒）也能给出结论。
  v1 那条"探测需要写源目录、被拒就降级成 ``ok=True``"的假绿路径已被删除
  （``docstring`` 见 ``tests/test_paths.py`` 顶部）
* ``test_destructive_write_sites_are_pinned_and_rename_only_touches_the_target_library`` —— 静态钉住
  "后端只会动自己的文件"，且 v2 的 `os.rename` 唯一调用点在 `app/processors/engine.py`
"""

from __future__ import annotations

import ast
import errno
import json
import os
import sys
from pathlib import Path

import pytest

import mlm_helpers as H

REPO_ROOT = Path(__file__).resolve().parents[1]

MEDIA_TREE = {
    "Show A/Season 1/ep01.mkv": 11,
    "Show A/Season 1/Subs/zh.ass": 22,
    "Show A/Movie.mkv": 33,
    "Show B/Season 1/ep02.mp4": 44,
    "Show B/Season 1/Show - Dash/ep03.mkv": 55,
    "Show B/空文件夹": None,
}


# --------------------------------------------------------------------------- 审计钩子


class _WriteAudit:
    """记录"谁写了受保护目录"。钩子全局只能装一次，所以用注册表按用例开关。"""

    _installed = False
    _roots: list[str] = []

    @classmethod
    def install(cls) -> None:
        if cls._installed:
            return

        def hook(event: str, args: tuple) -> None:  # noqa: ANN001
            if not cls._roots:
                return
            for path in cls._touched_paths(event, args):
                if cls._is_protected(path):
                    cls.violations.append(
                        {"event": event, "path": str(path), "args": tuple(map(str, args))}
                    )

        sys.addaudithook(hook)
        cls._installed = True

    violations: list[dict] = []

    _WRITE_EVENTS = {
        "os.mkdir": (0,),
        "os.rmdir": (0,),
        "os.remove": (0,),
        "os.unlink": (0,),
        "os.chmod": (0,),
        "os.utime": (0,),
        "os.truncate": (0,),
        "os.symlink": (1,),
        "os.link": (1,),
        "os.rename": (0, 1),
        "os.replace": (0, 1),
        "shutil.copyfile": (1,),
        "shutil.copymode": (1,),
        "shutil.copystat": (1,),
        "shutil.move": (0, 1),
        "shutil.rmtree": (0,),
        "tempfile.mkstemp": (0,),
    }

    @classmethod
    def _touched_paths(cls, event: str, args: tuple) -> list:
        out = []
        indexes = cls._WRITE_EVENTS.get(event)
        if indexes is not None:
            for index in indexes:
                if index < len(args):
                    out.append(args[index])
        elif event == "open" and args:
            path = args[0]
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            writing = False
            if isinstance(mode, str) and mode:
                writing = any(flag in mode for flag in ("w", "a", "x", "+"))
            elif isinstance(flags, int):
                writing = bool(
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                )
            if writing:
                out.append(path)
        return [item for item in out if isinstance(item, (str, bytes, os.PathLike))]

    @classmethod
    def protect(cls, *roots) -> None:
        cls._roots.extend(str(root) for root in roots)

    @classmethod
    def _is_protected(cls, path) -> bool:
        try:
            candidate = os.path.normcase(os.path.abspath(os.fspath(path)))
        except (TypeError, ValueError):
            return False
        for root in cls._roots:
            root_norm = os.path.normcase(os.path.abspath(root))
            if candidate == root_norm or candidate.startswith(root_norm + os.sep):
                return True
        return False


@pytest.fixture
def write_audit():
    """审计受保护目录：用例内用 ``write_audit.protect(路径)`` 登记，收尾时校验零写操作。"""
    _WriteAudit.install()
    saved = list(_WriteAudit._roots)
    _WriteAudit._roots[:] = []
    _WriteAudit.violations = []
    yield _WriteAudit
    violations = list(_WriteAudit.violations)
    _WriteAudit.violations = []
    _WriteAudit._roots[:] = saved
    assert violations == [], f"检测到对只读目录的写操作：{violations}"


# --------------------------------------------------------------------------- 建链


def test_hardlink_pipeline_never_touches_the_source_tree(
    adapter, build_tree, source_dir, library_root, target_dir, log_dir, write_audit
):
    # 先建树、后登记受保护：审计只该覆盖**被测管线**的写操作，不能把夹具自己建树算成违规
    # （否则 POSIX 容器里会稳定 teardown ERROR）。
    build_tree(source_dir, MEDIA_TREE)
    write_audit.protect(source_dir)
    before = H.tree_snapshot(source_dir)
    contents = {
        str(path.relative_to(source_dir)): H.read_bytes(path)
        for path in source_dir.rglob("*")
        if path.is_file()
    }
    link_counts = {
        str(path.relative_to(source_dir)): path.stat().st_nlink
        for path in source_dir.rglob("*")
        if path.is_file()
    }

    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir),
        target_dir=str(target_dir),
        log_file=str(log_dir / "hard_links.log"),
    )
    assert outcome.exception is None, outcome.exception
    assert len(outcome.created) == 5, outcome.created
    assert outcome.failed == [], outcome.failed

    assert H.tree_snapshot(source_dir) == before, "源目录的 size/mtime/ino/mode 必须一个都不变"
    for rel, data in contents.items():
        assert H.read_bytes(source_dir / rel) == data, f"源文件内容被改动了：{rel}"
    for rel, count in link_counts.items():
        now = (source_dir / rel).stat().st_nlink
        assert now == count + 1, f"{rel} 的 nlink 应恰好 +1（建了一个硬链接），{count} -> {now}"



def test_second_and_forced_runs_keep_the_source_pristine(
    adapter, build_tree, source_dir, target_dir, log_dir
):
    build_tree(source_dir, MEDIA_TREE)
    before = H.tree_snapshot(source_dir)
    log_file = str(log_dir / "hard_links.log")

    first = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file
    )
    assert first.exception is None and len(first.created) == 5

    second = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file
    )
    assert second.exception is None
    assert second.created == [], "第二遍不该再建链"
    assert sorted(second.skipped) == ["Show A", "Show B"], second.skipped

    forced = adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file, force=True
    )
    assert forced.exception is None
    assert forced.created == [], "目标是硬链接，force 重跑只会撞 File exists"

    assert H.tree_snapshot(source_dir) == before
    nlinks = [
        path.stat().st_nlink
        for path in source_dir.rglob("*")
        if path.is_file()
    ]
    assert nlinks and all(count == 2 for count in nlinks), nlinks


@pytest.mark.skipif(os.name == "nt", reason="Windows 的目录只读位不阻止创建文件，只能在 POSIX 上验证")
def test_source_directory_may_be_mounted_read_only(
    adapter, build_tree, source_dir, library_root, target_dir, log_dir
):
    """真把源目录 chmod 成 r-x（模拟 ``/downloads:ro``）：建链必须照常成功。"""
    build_tree(source_dir, MEDIA_TREE)
    before = H.tree_snapshot(source_dir)
    os.chmod(source_dir, 0o555)
    try:
        outcome = adapter.run_hardlinks(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            log_file=str(log_dir / "hard_links.log"),
        )
    finally:
        os.chmod(source_dir, 0o755)

    assert outcome.exception is None, outcome.exception
    assert len(outcome.created) == 5, outcome.created
    assert outcome.failed == [], outcome.failed
    assert H.tree_snapshot(source_dir) == before


# --------------------------------------------------------------------------- 重命名


def test_rename_pipelines_only_run_inside_the_target_root(
    proc_engine, proc_defaults, build_tree, source_dir, library_root, log_dir, write_audit
):
    """引擎的 ``root`` 指向**目标库**；源目录连一次写调用都不许有。

    重命名由 ``app.processors.engine.run_rename(spec, root, log_file=…)`` 拿 YAML 规格跑；
    这里用 ``legacy`` 预设，它保留最朴素的旧口径的字段值与语义
    （等价性由本项目自己的回归测试守卫）。
    """
    # 先建树、后登记受保护：审计只该覆盖**被测管线**的写操作，不能把夹具自己建树算成违规
    # （否则 POSIX 容器里会稳定 teardown ERROR）。
    build_tree(source_dir, MEDIA_TREE)
    write_audit.protect(source_dir)
    source_before = H.tree_snapshot(source_dir)

    bangumi_root = library_root / "Bangumi"
    build_tree(
        bangumi_root,
        {"Show A/Season 1/ep01.mkv": 11, "Show A/Season 1/ep02.ass": 22},
    )
    bangumi_log = log_dir / "bangumicache.log"
    bangumi = proc_engine.run_rename(
        proc_defaults.preset_spec("bangumi", "legacy"),
        bangumi_root,
        log_file=str(bangumi_log),
    )
    assert bangumi.exception is None, bangumi.exception
    assert sorted((item["old"], item["new"]) for item in bangumi.renamed) == [
        ("ep01.mkv", "Show A S01E01.mkv"),
        ("ep02.ass", "Show A S01E02.ass"),
    ], bangumi.renamed

    movie_root = library_root / "Movie"
    build_tree(movie_root, {"Big Movie/a.mkv": 10, "Big Movie/b.mkv": 30})
    movie_log = log_dir / "moviecache.log"
    movie = proc_engine.run_rename(
        proc_defaults.preset_spec("movie", "legacy"),
        movie_root,
        log_file=str(movie_log),
    )
    assert movie.exception is None, movie.exception
    assert [(item["old"], item["new"]) for item in movie.renamed] == [("b.mkv", "Big Movie.mkv")], \
        movie.renamed
    assert (movie_root / "Big Movie" / "a.mkv").exists(), "落选文件不该被改名"

    assert H.tree_snapshot(source_dir) == source_before
    assert H.tree_relpaths(bangumi_root), "目标库必须真的有产物"
    assert json.loads(H.read_text(bangumi_log))["Show A"]["size"] == 33


# --------------------------------------------------------------------------- 探测 / 静态守卫


def test_validate_probe_never_writes_into_the_source_even_when_it_cannot_link(
    paths_mod, source_dir, target_dir, build_tree, monkeypatch
):
    """只读实证探针：**不需要**源目录可写；写被拒也照样给结论、且源树一个字节不变。

    v1 的实现在源侧 ``mkstemp``：``/downloads:ro`` 必然 ``EROFS``，于是它把 ``ok`` 置 ``True``
    自我降级 —— 那正是"预检说可以、真跑全失败"的假绿来源，级联预检的只读实证口径明确要求删掉。
    这里把"源目录里任何写调用都拒绝"精确注入一次，确认探针仍然走**只读**路径。
    """
    build_tree(source_dir, {"Show A/Season 1/ep01.mkv": 11, "Show B/ep02.mkv": 22})
    before_entries = sorted(H.tree_relpaths(source_dir))
    before_files = {
        rel: (
            os.stat(source_dir / rel).st_size,
            os.stat(source_dir / rel).st_mtime_ns,
            os.stat(source_dir / rel).st_ino,
        )
        for rel in before_entries
        if (source_dir / rel).is_file()
    }
    before_payload = {
        rel: H.read_bytes(source_dir / rel) for rel in before_files
    }
    real_open = os.open

    def deny_writes_inside_source(path, flags, *args, **kwargs):
        text = str(path)
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND))
        if text.startswith(str(source_dir)) and writing:
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(paths_mod.os, "open", deny_writes_inside_source)

    ok, note = paths_mod.probe_cross_filesystem(str(source_dir), str(target_dir))

    assert ok is True, note
    assert "已实证" in note and "源目录未被写入" in note, note
    assert sorted(H.tree_relpaths(source_dir)) == before_entries
    assert {rel: H.read_bytes(source_dir / rel) for rel in before_files} == before_payload
    assert {
        rel: (
            os.stat(source_dir / rel).st_size,
            os.stat(source_dir / rel).st_mtime_ns,
            os.stat(source_dir / rel).st_ino,
        )
        for rel in before_files
    } == before_files
    assert not [p for p in before_entries if ".mlm_probe_" in p]


def test_the_probe_reports_none_when_the_source_has_no_file_to_sample(
    paths_mod, source_dir, target_dir
):
    """空源目录 ⇒ ``None``（无法实证）。**绝不能**像 v1 那样降级成 ``True``。"""
    before = sorted(H.tree_relpaths(source_dir))

    ok, note = paths_mod.probe_cross_filesystem(str(source_dir), str(target_dir))

    assert ok is None, note
    assert "无法实证" in note, note
    assert sorted(H.tree_relpaths(source_dir)) == before


def test_the_probe_leaves_no_residue_in_the_source_or_the_target(
    paths_mod, source_dir, target_dir, build_tree
):
    build_tree(source_dir, {"Show A/Season 1/ep01.mkv": 11})
    before = sorted(H.tree_relpaths(source_dir))

    ok, note = paths_mod.probe_cross_filesystem(str(source_dir), str(target_dir))

    assert ok is True, note
    assert sorted(H.tree_relpaths(source_dir)) == before
    assert not [p for p in source_dir.rglob("*") if p.name.startswith(".mlm_probe_")]
    assert not [p for p in target_dir.rglob("*") if p.name.startswith(".mlm_probe_")]


def test_destructive_write_sites_are_pinned_and_rename_only_touches_the_target_library():
    """静态钉住后端**全部**删除/改名/覆盖调用点，并单独钉死 ``os.rename`` 的唯一归属。

    v2 的重命名只发生在**目标库**里：整个运行时只有一个 ``os.rename``，就是
    ``app/processors/engine.py::_process_scope``。日志与配置都走"临时文件 + ``os.replace``"
    的原子覆盖，落点固定在自己的数据卷（``configstore._write_yaml`` / ``logs.write_json_cache``）；
    另有两条健康检查/探测路径删掉**自己在源目录祖先里建的**临时文件。

    任何新增的 ``os.remove``/``os.unlink``/``os.rmdir``/``os.rename``/``os.replace``/
    ``shutil.rmtree``/``shutil.move`` 都会让这条失败 —— 这正是"顺手清理一下源目录"这类改动的入口。
    """
    allowed = {
        ("paths.py", "probe_cross_filesystem"),        # 删掉自己刚建的那个 .mlm_probe_* 临时链接
        ("paths.py", "cleanup_probe_leftovers"),       # 清扫被强杀留下的 .mlm_probe_* 残留（只扫目标目录）
        # 建库/改库的权限预检：在**目标目录或其已存在的祖先**里写一个探针文件再删（源目录只读）。
        ("paths.py", "_write_probe"),
        ("routers/system.py", "_writable"),            # 健康检查探测后删掉自己的探针文件
        ("configstore.py", "_write_yaml"),             # 临时文件 + os.replace 原子写配置（数据卷）
        ("processors/engine.py", "_process_scope"),    # 执行改名，只作用于目标库
        # L2 防丢：目标盘上原有的同名文件比新来的小时，让它让位到 ` (2)` ——
        # 同一个目标目录里的 `os.rename`（硬链接改名），不复制数据、不删文件、绝不碰源目录。
        ("processors/engine.py", "_displace_target"),
        ("processors/logs.py", "write_json_cache"),    # 临时文件 + os.replace 原子写日志（数据卷）
        # 原生硬链接实现：源侧一级文件夹已被删除时，孤儿清理会递归删掉**目标库**里
        # 对应的那一份镜像目录（受库级
        # orphan_cleanup 开关控制）。这是运行时代码里的唯一破坏性调用点，落点是目标库；
        # 源目录仍然只被读取（本文件其他用例单独断言）。
        ("processors/hardlink.py", "_cleanup_orphans"),
        # 作品记录的手动删除：用户在 WebUI 里删掉某个作品的账本记录时，要求
        # "连带删掉目标库里那个作品的文件夹"，好让下次运行真的重做。落点由
        # ``link_records.safe_target_path()`` 强制限定在"该库目标目录的直接子目录"
        # （拒绝 . / .. / 路径分隔符，并要求 dirname(candidate) == base），
        # 源目录一个字节都不碰；dry_run 预演 + confirm=true 执行见 docs/contracts/API.md §3.11
        # （界面侧只剩一级弹窗确认，后端那道 confirm 是防误调用）。
        ("routers/libraries.py", "delete_library_works"),
    }
    destructive = {"os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace",
                   "shutil.rmtree", "shutil.move"}
    renames = {"os.rename", "shutil.move"}

    found: set[tuple[str, str]] = set()
    rename_sites: set[tuple[str, str]] = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        rel = path.relative_to(REPO_ROOT / "app").as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = f"{getattr(node.func.value, 'id', '')}.{node.func.attr}".lstrip(".")
            if name not in destructive:
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            site = (rel, getattr(owner, "name", "<module>"))
            found.add(site)
            if name in renames:
                rename_sites.add(site)

    assert found <= allowed, f"出现了计划外的破坏性写入点：{sorted(found - allowed)}"
    assert found == allowed, f"允许清单过期了，实际只剩：{sorted(allowed - found)}"

    # 「重命名只碰目标库」的静态证明：运行时的**移动/改名**只剩引擎里那两处
    # （``os.replace`` 不算 —— 它只用来原子覆盖应用自己的日志/配置文件，见上面允许清单）。
    assert rename_sites == {
        ("processors/engine.py", "_process_scope"),
        ("processors/engine.py", "_displace_target"),
    }, f"运行时的改名调用点应当只有 engine._process_scope/_displace_target，实际：{sorted(rename_sites)}"

    # 绝不实现复制回退（B8）：运行时代码里不许出现 shutil.copy*/copytree/move 这类"搬文件"的调用。
    copyish: set[tuple[str, str]] = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO_ROOT / "app").as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = f"{getattr(func.value, 'id', '')}.{func.attr}".lstrip(".")
            if name in {"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
                        "shutil.move"}:
                copyish.add((rel, name))
    assert not copyish, f"运行时代码里出现了复制/搬迁文件的回退路径：{sorted(copyish)}"
