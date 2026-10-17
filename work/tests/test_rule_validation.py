"""架构规则 B1–B13 的**可执行**版本（真源：``docs/contracts/BASELINE.md`` §4）。

本文件覆盖其中 **结构性、做错了会毁数据或让容器起不来**的那几条：

* B1 容器里不带宿主脚本挂载 —— 断言 `ORIGINAL_SCRIPT_DIR`
  这个环境变量不出现；
* B3 引擎在 ``preset=legacy`` 下保留最朴素的旧口径的字段值与语义；
* B6 日志格式冻结，且**全部落在数据卷的 ``logs/``**，日志名只允许是文件名；
* B8 绝不实现"复制回退"（调用前**挂载身份**预筛、结论三态，调用后逐文件核对 `(st_dev, st_ino)`）；
* B9 源目录不靠 ``:ro`` 挂载保护 —— 单挂载形态下内核让
  「源单独只读挂载」与「跨路径硬链接」**互斥**（`linkat(2)` 比较挂载身份 ⇒ `EXDEV`），改为
  「代码层零源写入 + 任务前后逐文件源完整性核对（``Task.result.source_integrity``）」：
  **结构性保证 → 可验证的保证**（见 ``docs/guides/TROUBLESHOOTING.md`` 场景 1/3、B9 行）；
  B10 容器 locale/编码锁定 UTF-8；B11 单进程单 worker 串行。

硬链接链路**完全由应用自带实现驱动**：``app/script_adapter.py`` 驱动
``app/processors/hardlink.py``（``adapter.runtime_module()``）。root logger 接管、print 捕获、
``ScopedOs`` 必定恢复、``force`` 先清记录、无复制回退 + inode 核对，作为该链路的行为约束继续保留。

静态检查读的是仓库里的 ``docker-compose.yml`` / ``docker/Dockerfile`` / 应用源码，
断言只看**生效行**（去掉注释），否则解释性注释里的反例会把断言带跑偏。
"""

from __future__ import annotations

import ast
import contextlib
import io
import shutil
import logging
import os
import re
from pathlib import Path, PurePosixPath

import pytest

import mlm_helpers as H

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"

ORIGINAL_SCRIPT_PATH = "/vol1/1000/Others/script"
HOST_DOWNLOAD_ROOT = "/vol1/1000/Download/qbittorrent"
HOST_LIBRARY_ROOT = "/vol1/1000/Library"
ASCTIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - ")


# --------------------------------------------------------------------------- 文本解析


def _effective_lines(text: str) -> list[str]:
    """去掉整行/行尾注释后的非空行（注释里常出现"反例"，不能参与断言）。"""
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            out.append(stripped)
    return out


def _mounts(text: str) -> dict[str, tuple[str, str]]:
    """``{容器路径: (宿主路径, 模式)}``；模式默认 ``rw``，识别 ``:ro``/``:rw`` 后缀。"""
    mounts: dict[str, tuple[str, str]] = {}
    for line in _effective_lines(text):
        if not line.startswith("- ") or ":" not in line:
            continue
        entry = line[2:].strip().strip('"').strip("'").replace(" ", "")
        parts = entry.split(":")
        if len(parts) < 2 or not parts[0].startswith("/"):
            continue
        mode = parts[-1] if parts[-1] in ("ro", "rw") else "rw"
        body = parts[:-1] if mode != "rw" else parts
        host = body[0]
        container = ":".join(body[1:])
        if container.startswith("/"):
            mounts[container] = (host, mode)
    return mounts


def _media_root_entry(text: str) -> str:
    """compose 里那条 ``${MEDIA_ROOT…}`` 共同父挂载的原文。

    采用单挂载形态（B9）后容器里**没有** ``/downloads`` 与 ``/library`` 两条独立 bind：唯一的媒体
    挂载是宿主上的共同父目录，且**容器路径 = 宿主路径**。该条目形如
    ``${MEDIA_ROOT:-/vol1/1000}:${MEDIA_ROOT:-/vol1/1000}``，因为表达式自身带 ``:``，
    ``_mounts()`` 解析不到它 —— 所以这里直接看生效行原文。
    """
    entries = [
        line[2:].strip().strip('"').strip("'")
        for line in _effective_lines(text)
        if line.startswith("- ") and "MEDIA_ROOT" in line
    ]
    assert len(entries) == 1, f"应当只有一条共同父挂载（单挂载形态），实际：{entries}"
    return entries[0]


def _env_block(text: str) -> dict[str, str]:
    """抓 ``KEY: value`` 形式的生效行（只认大写键，避开 YAML 的其它小写键）。"""
    out: dict[str, str] = {}
    for line in _effective_lines(text):
        match = re.match(r"^([A-Z][A-Z0-9_]*):\s*(.+)$", line)
        if match:
            out[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return out


def _dockerfile_env(text: str) -> dict[str, str]:
    """解析 Dockerfile 的 ``ENV K=V`` / 续行 ``K=V \\`` 块。"""
    out: dict[str, str] = {}
    joined = text.replace("\\\n", " ")
    for line in joined.splitlines():
        if not line.strip().upper().startswith("ENV "):
            continue
        for token in line.strip()[4:].split():
            if "=" in token:
                key, _, value = token.partition("=")
                out[key] = value.strip('"')
    return out


def _call_names(path: Path) -> list[tuple[str, str]]:
    """AST 级调用清点：``[(所在函数名, 被调名字)]``。

    用 AST 而不是文本搜索，是为了不被 docstring/注释里的 ``shutil.copy2`` 字面量骗到
    （`script_adapter.py` 的注释里正好在解释"为什么不用 mtime"，就写了这个函数名）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = f"{getattr(func.value, 'id', '')}.{func.attr}".lstrip(".")
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            name = ""
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        out.append((getattr(owner, "name", "<module>"), name))
    return out


def _all_call_names() -> list[tuple[str, str, str]]:
    """``[(文件, 所在函数, 被调名字)]``，覆盖 ``app/**/*.py``。"""
    out = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        for owner, name in _call_names(path):
            out.append((path.name, owner, name))
    return out


def _app_sources() -> str:
    """``app/**/*.py`` 的合并文本（保留：供将来需要整份文本的静态守卫用）。"""
    chunks = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


@pytest.fixture(scope="module")
def compose_text() -> str:
    if not COMPOSE.is_file():
        pytest.skip("docker-compose.yml 尚未交付（devops 的产物）")
    return COMPOSE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    if not DOCKERFILE.is_file():
        pytest.skip("docker/Dockerfile 尚未交付（devops 的产物）")
    return DOCKERFILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- B1 / B2

def test_b1_no_original_script_dir_is_mounted_and_no_copy_ships_in_the_image(compose_text):
    """B1/B2：引擎是唯一执行体。"""
    mounts = _mounts(compose_text)
    assert ORIGINAL_SCRIPT_PATH not in mounts, f"compose 里不该挂脚本目录，实际挂载：{mounts}"
    for retired in ("/downloads", "/library"):
        assert retired not in mounts, (
            f"{retired} 这类独立 bind 与共同父挂载互斥：单挂载形态下「源单独 :ro 挂载」与"
            f"「跨路径硬链接」互斥（两条独立 bind ⇒ link() 必然 EXDEV）。实际挂载：{mounts}"
        )
    assert set(mounts) == {"/data"}, mounts

    entry = _media_root_entry(compose_text)
    assert re.fullmatch(r"(?P<mount>\$\{MEDIA_ROOT[^}]*\}):(?P=mount)", entry), (
        f"共同父挂载必须「容器路径 = 宿主路径」（单挂载形态），实际：{entry}"
    )

    effective = "\n".join(_effective_lines(compose_text))
    assert "/vol1/1000/Others/script" not in effective, "宿主机脚本路径不该再出现在 compose 里"
    assert "/opt/original-scripts" not in effective

    assert not (REPO_ROOT / "app" / "processors" / "reference").exists(), (
        "仓库里不该出现处理器实现的副本目录"
    )
    assert not (REPO_ROOT / "app" / "processors" / "refcheck.py").exists(), (
        "不该出现动态载入器"
    )


def test_b1_no_original_script_dir_setting_is_needed(compose_text, dockerfile_text):
    """``ORIGINAL_SCRIPT_DIR`` 环境变量不该再出现。"""
    env = _env_block(compose_text)
    assert "ORIGINAL_SCRIPT_DIR" not in env, env
    docker_env = _dockerfile_env(dockerfile_text)
    assert "ORIGINAL_SCRIPT_DIR" not in docker_env, docker_env


def test_a10_is_superseded_by_b9_so_the_parent_mount_is_the_only_media_bind(compose_text):
    """（**已变更**）：「``/downloads`` 只读挂载」层保护**已由 B9 取代**。

    单挂载形态下容器里只有一条**共同父挂载**：内核的 ``linkat(2)`` 比较**挂载身份**，把源单独
    只读挂成第二条 bind 就等于让硬链接永远 ``EXDEV``。所以源目录一侧的「不许被写」不再是
    挂载权限，而是「代码层零源写入 + 任务前后逐文件源完整性核对」
    （``Task.result.source_integrity``，见 B9 行、``docs/guides/TROUBLESHOOTING.md`` 场景 1/3）。
    本用例因此**不再断言任何挂载级只读**（``read_only``/``:ro``），只钉共同父挂载的形状。
    """
    mounts = _mounts(compose_text)
    assert set(mounts) == {"/data"}, mounts
    assert mounts["/data"][1] == "rw", "数据卷必须可写（SQLite、日志、配置）"

    entry = _media_root_entry(compose_text)
    match = re.fullmatch(
        r"\$\{MEDIA_ROOT(?::-)?(?P<mount>[^}]*)\}:\$\{MEDIA_ROOT(?::-)?(?P=mount)\}", entry
    )
    assert match, (
        f"共同父挂载必须写成 ${{MEDIA_ROOT:-<宿主路径>}}:${{MEDIA_ROOT:-<宿主路径>}}"
        f"（容器路径 = 宿主路径），实际：{entry}"
    )
    default = match.group("mount")
    assert default.startswith("/"), f"默认媒体根必须是容器内绝对路径，实际：{default!r}"

    parent = PurePosixPath(default)
    for host_root in (HOST_DOWNLOAD_ROOT, HOST_LIBRARY_ROOT):
        assert PurePosixPath(host_root).is_relative_to(parent), (
            f"{host_root} 必须落在共同父挂载 {parent} 之内 —— 否则源与目标又是两条独立 bind，"
            f"hardlink 必然 EXDEV（见 B9）"
        )


def test_b9_identity_path_map_cannot_be_overridden_by_the_image_or_compose(
    compose_text, dockerfile_text
):
    """恒等映射（单挂载形态的不变量）不得被**镜像 ENV** 或 compose 默认值改写。

    真实事故：``docker/Dockerfile`` 里曾写死过

        ENV HOST_PATH_MAP="{\\"/downloads\\":\\"/vol1/1000/Download/qbittorrent\\", …}"

    而单挂载形态是「共同父目录单挂载」⇒ 容器路径 = 宿主路径、容器里没有 ``/downloads``。
    于是 ``app/paths.py::to_container()`` 的规则 2（拿映射的**值**做宿主前缀反查）会把用户在
    WebUI 里填的真实路径 ``/vol1/1000/Download/qbittorrent/Bangumi`` 改写成
    ``/downloads/Bangumi``（v1 还会被当年的 ``assert_allowed()`` 白名单拦下；
    该白名单机制已整体删除，这里只留事故记录）。

    这个坑**只有「落在映射值前缀之下」的路径才中招**：fixture 放在
    ``/vol1/1000/Docker/media-ingest/testmedia`` 时规则 2 不命中、路径原样通过，
    所以 44/44 的验收照样全绿 —— 必须在结构上钉死，不能靠 fixture 碰巧避开。
    """
    # 镜像层：允许不声明，或声明成空表；不许内置任何非空映射。
    for line in _effective_lines(dockerfile_text):
        if "HOST_PATH_MAP" not in line:
            continue
        value = line.split("HOST_PATH_MAP", 1)[1].lstrip("= ").strip().strip('"').strip("'")
        assert value in ("{}", ""), (
            "docker/Dockerfile 不得内置非空 HOST_PATH_MAP（默认必须是空表 = 恒等映射）：" + line
        )

    # compose 层：必须显式给出默认值，且不得指向 /downloads、/library（它们早已不存在）。
    declarations = [
        line for line in _effective_lines(compose_text) if line.startswith("HOST_PATH_MAP")
    ]
    assert declarations, "docker-compose.yml 必须显式声明 HOST_PATH_MAP 默认值"
    for line in declarations:
        assert "/downloads" not in line and "/library" not in line, (
            "compose 的 HOST_PATH_MAP 默认值不得再指向 v1 的 /downloads、/library：" + line
        )


def test_b9_default_host_path_map_is_the_empty_identity_mapping():
    """代码层真源：``DEFAULT_HOST_PATH_MAP`` 必须是空表（与上面两份部署资产一致）。"""
    text = (REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert re.search(
        r"^DEFAULT_HOST_PATH_MAP:\s*dict\[str,\s*str\]\s*=\s*\{\}\s*$", text, re.M
    ), "app/config.py 的 DEFAULT_HOST_PATH_MAP 必须是 {}（恒等映射；见其上方注释记录的路径映射事故）"


def test_a10_source_tree_is_never_opened_for_writing():
    """结构保证之外的应用侧保证：运行期的建链实现只 stat/walk/link，从不写源。

    这条断语直接钉住运行期实现 ``app/processors/hardlink.py``：只出现 ``os.link`` 与删**目标侧**废目录的
    ``shutil.rmtree``，绝不出现复制/搬移/删除源文件的调用。
    """
    text = (REPO_ROOT / "app" / "processors" / "hardlink.py").read_text(encoding="utf-8")
    assert "os.link(" in text and "shutil.rmtree(" in text
    for forbidden in ("shutil.copy", "shutil.move", "os.remove(", "os.unlink(", "os.rename("):
        assert forbidden not in text, f"原生建链实现不该出现 {forbidden}"


# --------------------------------------------------------------------------- A11 / A12


def test_a11_locale_and_encoding_are_locked_to_utf8(compose_text, dockerfile_text):
    env = _env_block(compose_text)
    assert env.get("LANG") == "C.UTF-8"
    assert env.get("PYTHONUTF8") == "1"
    assert env.get("PYTHONIOENCODING") == "utf-8"

    docker_env = _dockerfile_env(dockerfile_text)
    assert docker_env.get("LANG") == "C.UTF-8"
    assert docker_env.get("PYTHONUTF8") == "1"
    assert docker_env.get("PYTHONIOENCODING") == "utf-8"


def test_a12_single_process_single_worker(compose_text, dockerfile_text):
    effective = "\n".join(_effective_lines(compose_text))
    assert "replicas" not in effective and "scale" not in effective
    assert re.search(r"--workers\s+1\b", dockerfile_text), "uvicorn 必须 --workers 1"
    assert "--workers 2" not in dockerfile_text and "--workers 4" not in dockerfile_text


# --------------------------------------------------------------------------- B6

def test_b6_engine_logs_live_in_the_data_volume(
    proc_defaults, proc_logs, settings_env,
):
    """B6：日志/缓存全部落在数据卷的 `logs/`，日志名只允许是**文件名**。"""
    log_dir = Path(settings_env.log_dir)
    assert Path(settings_env.hardlink_log_path).parent == log_dir
    assert Path(settings_env.hardlink_log_path).name == "hard_links.log"

    for processor_id in ("bangumi", "movie"):
        spec = proc_defaults.preset_spec(processor_id)
        log_file = spec["log_file"]
        assert "/" not in log_file and "\\" not in log_file and ".." not in log_file, log_file
        assert spec["log_format"] == "json_cache"
        assert Path(proc_logs.library_log_path(log_file)).parent == log_dir
        assert Path(proc_logs.log_path(str(log_dir), log_file)).parent == log_dir

    # 静态钉住：这里不该再出现任何旧的旁路目录。
    assert not (REPO_ROOT / "app" / "processors" / "reference").exists()


def test_b3_three_pipelines_touch_only_the_data_volume(
    adapter, proc_engine, proc_defaults, build_tree, source_dir, library_root,
    settings_env,
):
    """硬链接 + 番剧重命名 + 电影重命名跑完，源目录一个字节都不许变。"""
    build_tree(
        source_dir,
        {"Show A/Season 1/ep01.mkv": 10, "Big Movie/b.mkv": 30},
    )
    source_before = H.tree_snapshot(source_dir)

    log_dir = Path(settings_env.log_dir)
    link = adapter.run_hardlinks(
        source_dir=str(source_dir / "Show A"),
        target_dir=str(library_root / "Show A"),
        log_file=str(log_dir / "hard_links.log"),
    )
    assert link.exception is None, link.exception
    assert len(link.created) == 1

    bangumi = proc_engine.run_rename(
        proc_defaults.preset_spec("bangumi", "legacy"),
        library_root,
        log_file=str(log_dir / "bangumicache.log"),
    )
    assert bangumi.exception is None, bangumi.exception
    movie = proc_engine.run_rename(
        proc_defaults.preset_spec("movie", "legacy"),
        library_root,
        log_file=str(log_dir / "moviecache.log"),
    )
    assert movie.exception is None, movie.exception

    assert H.tree_snapshot(source_dir) == source_before, "源目录（只读挂载）不许被碰"
    assert sorted(p.name for p in log_dir.iterdir()) == [
        "bangumicache.log", "hard_links.log", "moviecache.log",
    ], "三条流水线的日志都必须落在数据卷的 logs/"


# --------------------------------------------------------------------------- A13 已整体删除


def test_a13_seed_log_and_log_continuity_are_gone_from_the_backend():
    """A13（日志播种 + `Source:` 前缀本地化 + `log_continuity` 面板）在 v2 整体删除。

    用 AST 扫**代码标识符**（不是源码文本）：注释里保留"这一段已删除"的说明是允许的，
    但 `seed_log`/`seed_all_logs`/`relabel_log_sources`/`host_to_container_map`/`log_continuity`
    这些名字不许再作为函数、变量、属性或导入名出现在 `app/**` 里。
    """
    forbidden = {
        "seed_log",
        "seed_all_logs",
        "relabel_log_sources",
        "host_to_container_map",
        "log_continuity",
        "log_seeded_at",
        "_localize_source",
    }
    hits: list[tuple[str, str]] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.alias):
                names.append(node.name.split(".")[-1])
                if node.asname:
                    names.append(node.asname)
            for name in names:
                if name in forbidden:
                    hits.append((rel, name))
    assert not hits, f"A13 的入口必须彻底消失，仍存在：{sorted(set(hits))}"


# ----------------------------------------------------------------- 适配器接管与日志契约


def test_a5_backend_takes_over_the_root_logger_with_the_frozen_format(
    adapter, build_tree, source_dir, target_dir, log_dir
):
    log_file = str(log_dir / "hard_links.log")
    root = logging.getLogger()
    sentinel = logging.StreamHandler(io.StringIO())
    root.addHandler(sentinel)
    root.setLevel(logging.WARNING)
    try:
        build_tree(source_dir, {"Show A/ep01.mkv": 10})
        assert len(adapter.run_hardlinks(
            source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file
        ).created) == 1

        assert root.level == logging.INFO, "basicConfig 在 root 已有 handler 时是 no-op ⇒ 必须自己设 level"
        assert sentinel in root.handlers, "不得移除别人的 handler"

        root.info("sentinel-line")
        root.info(
            "Source: %s - Folder: %s - Folder size: %d", source_dir, "Show A", 10
        )
        text = Path(log_file).read_text(encoding="utf-8")
        # 日志隔离：库日志文件只收「记账行」（体积登记 / 孤儿目录清理），其它应用日志
        # 一律不进文件 —— 否则 uvicorn 访问日志、任务日志会把 hard_links.log 灌成无限增长的
        # 非数据文件。记账行一个字节都不删、不轮转（跳过/改名的判断依赖它）。
        assert "sentinel-line" not in text, "非记账行不得写进库日志文件（有白名单过滤器）"
        assert "Source: " in text and " - Folder: " in text and " - Folder size: " in text, (
            "记账行必须照旧落盘"
        )
        handler = next(
            (h for h in root.handlers if isinstance(h, logging.FileHandler)
             and Path(getattr(h, "baseFilename", "")).name == Path(log_file).name),
            None,
        )
        assert handler is not None, "应用的 FileHandler 必须挂在 root 上"
        assert any(isinstance(f, adapter._BookkeepingFilter) for f in handler.filters), (
            "库日志文件必须带记账行白名单过滤器"
        )
        for line in text.splitlines():
            assert ASCTIME_RE.match(line), f"日志格式必须是 %(asctime)s - %(message)s：{line!r}"
    finally:
        root.removeHandler(sentinel)
        sentinel.close()


def test_a6_script_prints_are_captured_not_leaked(adapter, build_tree, source_dir, target_dir, log_dir):
    """适配器必须把被调用方的 print 吞进 ``outcome.stdout``，不能漏到服务进程 stdout。"""
    build_tree(source_dir, {"Show A/ep01.mkv": 10})
    outer = io.StringIO()
    with contextlib.redirect_stdout(outer):
        outcome = adapter.run_hardlinks(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            log_file=str(log_dir / "hard_links.log"),
        )
    assert outcome.exception is None, outcome.exception
    assert "已创建硬链接: " in outcome.stdout, "脚本输出必须被收进 outcome.stdout"
    assert outer.getvalue() == "", f"服务进程 stdout 被污染：{outer.getvalue()!r}"


# ----------------------------------------------------------------- 定向重跑与孤儿清理


def test_a7_scoped_os_is_installed_during_the_call_and_restored_after(
    adapter, build_tree, source_dir, target_dir, log_dir
):
    # 运行期驱动的是应用自带的原生实现。
    # 想观察/替换 ``os`` 代理与 ``create_hard_links``，必须拿运行期那一个模块。
    module = adapter.runtime_module()
    real_os = module.os
    captured = {}

    # 适配层会多传一个可选关键字 ``skip=``（每库账本判据），替身必须能接。
    # 还会多传 ``permission_mode=``（库设置里的权限开关），同样必须能接。
    def spy(source_dir_, target_dir_, *, skip=None, permission_mode=False):
        captured["type"] = type(module.os).__name__
        captured["is_real_os"] = module.os is real_os

    build_tree(source_dir, {"Show A/ep01.mkv": 10})
    original = module.create_hard_links
    module.create_hard_links = spy
    try:
        outcome = adapter.run_hardlinks(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            log_file=str(log_dir / "hard_links.log"),
        )
    finally:
        module.create_hard_links = original

    assert outcome.exception is None, outcome.exception
    assert captured["type"] == "ScopedOs", "调用脚本期间必须换成 os 代理"
    assert captured["is_real_os"] is False
    assert module.os is real_os, "调用结束（含异常路径）必须恢复真实 os"


def test_a7_scoped_os_is_restored_even_when_the_script_explodes(
    adapter, build_tree, source_dir, target_dir, log_dir
):
    # 运行期驱动的是应用自带的原生实现。
    # 想观察/替换 ``os`` 代理与 ``create_hard_links``，必须拿运行期那一个模块。
    module = adapter.runtime_module()
    real_os = module.os
    seen = {}

    def boom(source_dir_, target_dir_, *, skip=None, permission_mode=False):
        seen["type"] = type(module.os).__name__
        raise RuntimeError("脚本炸了")

    build_tree(source_dir, {"Show A/ep01.mkv": 10})
    original = module.create_hard_links
    module.create_hard_links = boom
    try:
        outcome = adapter.run_hardlinks(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            log_file=str(log_dir / "hard_links.log"),
        )
    finally:
        module.create_hard_links = original

    assert seen["type"] == "ScopedOs"
    assert module.os is real_os, "异常路径也必须恢复，否则后续调用全部被代理污染"
    assert outcome.exception is not None and "脚本炸了" in outcome.exception
    assert outcome.stdout == ""


def test_a8_force_prunes_the_log_before_the_script_runs(
    adapter, build_tree, source_dir, target_dir, log_dir
):
    log_file = str(log_dir / "hard_links.log")
    build_tree(source_dir, {"Show A/ep01.mkv": 10})
    assert len(adapter.run_hardlinks(
        source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file
    ).created) == 1

    # 运行期驱动的是应用自带的原生实现。
    # 想观察/替换 ``os`` 代理与 ``create_hard_links``，必须拿运行期那一个模块。
    module = adapter.runtime_module()
    original = module.create_hard_links
    seen: dict[str, str] = {}

    # 适配层还会多传 ``permission_mode=``（库设置里的权限开关），替身要能接。
    def spy(source_dir_, target_dir_, *, skip=None, permission_mode=False):
        with open(log_file, "r", encoding="utf-8") as handle:
            seen["log"] = handle.read()
        return original(source_dir_, target_dir_, skip=skip, permission_mode=permission_mode)

    module.create_hard_links = spy
    try:
        adapter.run_hardlinks(
            source_dir=str(source_dir), target_dir=str(target_dir), log_file=log_file
        )
        assert "Folder: Show A" in seen["log"], "不 force 时记录应在，脚本才能判断跳过"

        adapter.run_hardlinks(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            log_file=log_file,
            force=True,
        )
        assert "Folder: Show A" not in seen["log"], "force 必须在调用脚本之前清记录"
    finally:
        module.create_hard_links = original


# ------------------------------------------------------------- 级联预检与不复制回退


def test_a9_no_copy_fallback_anywhere_in_the_backend():
    calls = _all_call_names()
    forbidden = [
        (module, owner, name)
        for module, owner, name in calls
        if name in ("shutil.copyfile", "shutil.copytree", "shutil.copy")
    ]
    assert forbidden == [], f"后端出现复制回退调用：{forbidden}"
    adapter_source = (REPO_ROOT / "app" / "script_adapter.py").read_text(encoding="utf-8")
    assert "_verify_created_links" in adapter_source
    assert "不提供复制回退" in adapter_source


def test_a9_copied_files_are_detected_and_reported_as_failures(
    adapter, build_tree, source_dir, target_dir, log_dir
):
    """模拟"脚本谎报成功、其实只复制了文件" ⇒ 适配器必须逐 inode 核对并记为失败。

    这里故意把运行期模块的 ``create_hard_links`` 换成"复制版"，它打印与真实现**完全相同**
    的成功前缀，所以只有调用后的 inode 核对能拦住它。
    """
    # 运行期驱动的是应用自带的原生实现。
    # 想观察/替换 ``os`` 代理与 ``create_hard_links``，必须拿运行期那一个模块。
    module = adapter.runtime_module()

    def fake_copy(source_dir_, target_dir_, *, skip=None, permission_mode=False):
        os.makedirs(target_dir_, exist_ok=True)
        for folder in sorted(os.listdir(source_dir_)):
            source_folder = os.path.join(source_dir_, folder)
            if not os.path.isdir(source_folder):
                continue
            for walk_root, _dirs, files in os.walk(source_folder):
                rel = os.path.relpath(walk_root, source_folder)
                dest_dir = os.path.join(target_dir_, folder) if rel == "." else os.path.join(
                    target_dir_, folder, rel
                )
                os.makedirs(dest_dir, exist_ok=True)
                for filename in sorted(files):
                    with open(os.path.join(walk_root, filename), "rb") as handle:
                        payload = handle.read()
                    dest = os.path.join(dest_dir, filename)
                    with open(dest, "wb") as handle:
                        handle.write(payload)
                    print(f"已创建硬链接: {dest}")

    build_tree(source_dir, {"Show A/ep01.mkv": 10})
    original = module.create_hard_links
    module.create_hard_links = fake_copy
    try:
        outcome = adapter.run_hardlinks(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            log_file=str(log_dir / "hard_links.log"),
        )
    finally:
        module.create_hard_links = original

    assert outcome.created == [], "复制品绝不能算成功"
    assert len(outcome.failed) == 1
    target, message = outcome.failed[0]
    assert os.path.basename(target) == "ep01.mkv"
    assert "非硬链接" in message and "不提供复制回退" in message
