"""原生硬链接实现的行为守卫。

**背景**：硬链接链路驱动的是应用自己的 :mod:`app.processors.hardlink`，
不存在"第二份实现"。因此本文件只留两类断言：

* **黄金行为**：原生实现跑在固定剧本上的 stdout 与日志内容是稳定的 —— 断言值是历史差分
  里沉淀下来的黄金值（覆盖新建立链、已记录跳过、体积变化后删旧记录
  重跑、源文件夹消失后的孤儿清理、以及日志里有畸形/异源/错体积行）。
* **单一实现**：``app/`` 里不许再出现第二份实现或任何旁路调用点 —— 静态钉死，
  防止这些东西被加回来。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 归一化用：``%(asctime)s`` 默认格式 ``2026-10-13 11:48:33,123``。
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}")

#: 日志行格式（固定形状）—— 读**原始**日志时用。
_RAW_LOG_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - Source: .+ - Folder: .+ - Folder size: \d+$"
)

#: 同一格式，但时间戳已被 :func:`_normalize` 换成 ``<TS>`` —— 断言归一化结果时用。
_LOG_LINE = re.compile(r"^<TS> - Source: .+ - Folder: .+ - Folder size: \d+$")

#: 打印前缀（沿用 v1）。stdout 是适配层解析结果的
#: 契约，任何一个字变了都会让 ``_parse_hardlink_stdout``/人工排障失真。
_PRINT_PREFIXES = (
    "Deleted orphaned hard link folder: ",
    "文件夹已记录且体积一致，跳过: ",
    "已删除旧记录: ",
    "已创建硬链接: ",
    "创建硬链接失败: ",
    "已记录文件夹体积: ",
)


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _scenario_tree(source: Path) -> None:
    """两边完全相同的初始源树。

    刻意包含四种边界：同名一级目录、**嵌套子目录**、**空目录**、以及**根级文件**
    （基线只遍历 ``source_dir`` 的一级子目录 ⇒ 根级文件必须被忽略）。
    """
    _write(source / "Show A" / "ep01.mkv", b"a" * 10)
    _write(source / "Show A" / "Season 1" / "ep02.mkv", b"b" * 20)
    _write(source / "Show A" / "Season 1" / "notes.txt", b"c" * 5)
    _write(source / "loose.mkv", b"d" * 7)
    (source / "Empty").mkdir(parents=True, exist_ok=True)


def _run_direct(module, source: str, target: str, log_file: str) -> tuple[str, str]:
    """直接调用某个实现的 ``create_hard_links``，返回 ``(stdout, 日志文件内容)``。

    为了让**基线自己的** ``logging.basicConfig(filename=LOG_FILE, …)`` 真的生效（它在
    ``root.handlers`` 非空时是 no-op），这里先把 root logger 的 handler 全部摘掉并恢复
    ``NOTSET``，跑完再原样装回去。两个实现因此走的是各自完全相同的那条日志路径 ——
    否则"日志内容逐字相同"就会退化成"两边都靠适配层的 FileHandler 代写"。
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    root.setLevel(logging.NOTSET)

    original_log_file = module.LOG_FILE
    module.LOG_FILE = log_file
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            module.create_hard_links(source, target)
    finally:
        module.LOG_FILE = original_log_file
        for handler in list(root.handlers):
            with contextlib.suppress(Exception):
                handler.flush()
                handler.close()
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)

    path = Path(log_file)
    text = path.read_bytes().decode("utf-8", "replace") if path.is_file() else ""
    return buffer.getvalue(), text


def _path_pattern(path: Path) -> re.Pattern[str]:
    """把路径编译成"分隔符随便几个"的正则。

    为什么不能直接 ``text.replace(str(path), token)``：Windows 的 ``OSError`` 文案里
    路径有时带双反斜杠（``C:\\\\Users\\\\…``），直接替换匹配不到，两边的 tmp 路径就会漏进
    比较结果，让"逐字等价"变成假 FAIL。这里把每一段用 ``[\\\\/]+`` 连接，任何组合都能吃下。
    """
    pieces = [re.escape(piece) for piece in str(path).replace("\\", "/").split("/") if piece]
    # 前导分隔符必须一起吃掉：POSIX 的绝对路径以 ``/`` 开头，若把它留在替换结果外面，
    # 归一化会得到 ``/<TGT>/Show A``，与断言里的 ``<TGT>/Show A`` 差一个字符；Windows 的
    # ``C:\…`` 没有前导分隔符，所以这个坑只在 POSIX 上暴露。
    return re.compile(r"[\\/]?" + r"[\\/]+".join(pieces), re.IGNORECASE)


def _normalize(text: str, source: Path, target: Path) -> str:
    """抹掉"必然不同"的部分：时间戳与两边的 tmp 绝对路径。"""
    text = _TIMESTAMP.sub("<TS>", text)
    text = _path_pattern(source).sub("<SRC>", text)
    text = _path_pattern(target).sub("<TGT>", text)
    return text.replace("\\", "/")


def _native_rounds(tmp_path, adapter, *, runs: int = 1, before_run=None) -> list:
    """在独立目录里跑 ``runs`` 轮**原生实现**，返回归一化后的 ``(stdout, 日志)`` 列表。

    ``before_run(source, target, index)`` 在每轮开跑前调用，用来构造"第二轮状态变了"的剧本。
    """
    module = adapter.runtime_module()

    base = tmp_path / "native"
    source = base / "downloads" / "Bangumi"
    target = base / "library" / "Bangumi"
    log_file = base / "logs" / "hard_links.log"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    log_file.parent.mkdir(parents=True)
    _scenario_tree(source)

    rounds = []
    for index in range(runs):
        if before_run is not None:
            before_run(source, target, index)
        stdout, log_text = _run_direct(module, str(source), str(target), str(log_file))
        rounds.append((_normalize(stdout, source, target), _normalize(log_text, source, target)))
    return rounds


# --------------------------------------------------------------------------- 黄金行为


def test_the_native_implementation_on_a_fresh_tree(tmp_path, adapter):
    rounds = _native_rounds(tmp_path, adapter)
    stdout, log_text = rounds[0]

    # 只建一级目录下的文件；根级 loose.mkv 必须完全没被提到。
    assert "loose.mkv" not in stdout
    assert f"已记录文件夹体积: Show A - 35" in stdout
    assert "已记录文件夹体积: Empty - 0" in stdout
    assert stdout.count("已创建硬链接: ") == 3
    assert "创建硬链接失败: " not in stdout
    # 两个一级目录各写一行日志（Show A 有内容、Empty 体积为 0）。
    lines = [line for line in log_text.splitlines() if line.strip()]
    assert len(lines) == 2
    assert all(_LOG_LINE.match(line) for line in lines), lines


def test_the_native_implementation_when_already_logged(tmp_path, adapter):
    rounds = _native_rounds(tmp_path, adapter, runs=2)
    second_stdout, second_log = rounds[1]

    # 第二轮的 stdout 应该只剩两条"跳过"：一次都不许再建链。
    assert second_stdout.count("文件夹已记录且体积一致，跳过: ") == 2
    assert "已创建硬链接: " not in second_stdout
    assert "已记录文件夹体积: " not in second_stdout
    # 日志不许增长：跳过的文件夹不重写记录。
    assert second_log == rounds[0][1]


def test_the_native_implementation_when_a_folder_size_changes(tmp_path, adapter):
    def grow(source: Path, target: Path, index: int) -> None:
        if index == 1:
            with open(source / "Show A" / "Season 1" / "ep02.mkv", "ab") as handle:
                handle.write(b"z")

    rounds = _native_rounds(tmp_path, adapter, runs=2, before_run=grow)
    second_stdout, second_log = rounds[1]

    assert "已删除旧记录: Show A" in second_stdout
    assert "已记录文件夹体积: Show A - 36" in second_stdout
    # 体积变了就重跑：目标侧文件已存在 ⇒ 对每个文件都打印失败（**不删旧目标、不复制**）。
    assert second_stdout.count("创建硬链接失败: ") == 3
    assert "已创建硬链接: " not in second_stdout
    # 旧记录被删掉、新记录写回：Show A 仍然只有一行。
    show_a_lines = [line for line in second_log.splitlines() if "Folder: Show A" in line]
    assert len(show_a_lines) == 1 and show_a_lines[0].endswith("Folder size: 36")


def test_the_native_implementation_when_a_source_folder_disappears(tmp_path, adapter):
    def drop(source: Path, target: Path, index: int) -> None:
        if index == 1:
            shutil.rmtree(source / "Show A")

    rounds = _native_rounds(tmp_path, adapter, runs=2, before_run=drop)
    second_stdout, second_log = rounds[1]

    assert "Deleted orphaned hard link folder: <TGT>/Show A" in second_stdout
    assert "已创建硬链接: " not in second_stdout
    assert "文件夹已记录且体积一致，跳过: Empty" in second_stdout
    # 这条怪癖必须被保持：孤儿清理只删**目标目录**与内存里的字典项，**不删日志行** ——
    # 因此 Show A 的记录会一直留在日志里，之后每一轮都会重新走一次孤儿分支
    # （目标目录已被删掉，所以从第三轮起不再打印）。第 2 轮刚删掉目录，所以只留一条记录。
    show_a_lines = [line for line in second_log.splitlines() if "Folder: Show A" in line]
    assert len(show_a_lines) == 1 and show_a_lines[0].endswith("Folder size: 35")


def test_the_native_implementation_on_a_damaged_log(tmp_path, adapter):
    """畸形行、别的源目录、错的体积 —— 三种"坏日志"必须被同样地对待。"""

    def seed(source: Path, target: Path, index: int) -> None:
        if index != 0:
            return
        log_file = source.parents[1] / "logs" / "hard_links.log"
        log_file.write_text(
            "2026-01-01 00:00:00,000 - 这一行完全不符合格式\n"
            "2026-01-01 00:00:00,000 - Source: /somewhere/else - Folder: Show A - Folder size: 35\n"
            f"2026-01-01 00:00:00,000 - Source: {source} - Folder: Show A - Folder size: 999\n",
            encoding="utf-8",
        )

    rounds = _native_rounds(tmp_path, adapter, before_run=seed)
    stdout, log_text = rounds[0]

    # 体积不一致 ⇒ 删旧记录 + 重跑；异源与畸形行都被忽略（不产生额外分支）。
    assert stdout.count("已删除旧记录: Show A") == 1
    assert "文件夹已记录且体积一致，跳过: Show A" not in stdout
    assert stdout.count("已创建硬链接: ") == 3
    # 读-改-写只精确删掉"本源目录 + 本 folder"那一行：错体积那把行消失，
    # 而异源行与畸形行**原样保留**（基线的行为，不许"顺手清理"）。
    assert "Folder size: 999" not in log_text
    assert "/somewhere/else" in log_text
    assert "这一行完全不符合格式" in log_text
    assert log_text.count("Folder: Show A") == 2


def test_the_native_implementation_keeps_the_frozen_print_prefixes(adapter):
    """打印前缀逐字冻结 —— 适配层是按这些前缀解析 stdout 的。"""
    module = adapter.runtime_module()
    source = (REPO_ROOT / "app" / "processors" / "hardlink.py").read_text(encoding="utf-8")
    for prefix in _PRINT_PREFIXES:
        assert prefix in source, f"原生实现丢掉了冻结的打印前缀：{prefix!r}"
    assert module.LOG_FORMAT == "%(asctime)s - %(message)s"


def test_the_native_implementation_keeps_a9_never_copy_and_never_overwrite():
    """「绝不复制回退」必须由**运行时那份实现**来满足。

    运行期实现只用 ``os.link``、绝无复制回退，因此同样的断言必须落在
    ``app/processors/hardlink.py`` 上 —— 否则"不许静默复制
    冒充硬链接"这条契约就没人守了。
    """
    source = (REPO_ROOT / "app" / "processors" / "hardlink.py").read_text(encoding="utf-8")
    assert "os.link(source_file, target_file)" in source, "运行时实现必须仍然用 os.link 建硬链接"
    for forbidden in ("shutil.copy", "shutil.copy2", "copyfile", "shutil.copytree"):
        assert forbidden not in source, f"运行时实现不应含复制回退: {forbidden}"
    assert "shutil.rmtree" in source, "孤儿清理仍依赖 shutil.rmtree"
    # 入口签名：多了**可选**的 ``skip=``（每库账本判据，见 app/link_records.py），
    # 还有**可选**的 ``permission_mode=``（默认 False ⇒ 逐字保持历史行为；
    # 只有库设置显式开启时才会 chmod，见 tests/test_permission_mode.py）。
    # 不传时逐字保持历史行为（上面的字节级断言与 test_works_ledger 的默认路径用例都在守这条）。
    assert (
        "def create_hard_links(\n"
        "    source_dir: str,\n"
        "    target_dir: str,\n"
        "    *,\n"
        "    skip: dict[str, int] | None = None,\n"
        "    permission_mode: bool = False,\n"
        ") -> None:" in source
    ), "原生实现的入口签名必须是冻结版签名 + 可选 skip= + 可选 permission_mode="


# --------------------------------------------------------------------------- 依赖已归零


def test_the_runtime_module_is_the_applications_own_implementation(adapter):
    """运行期唯一实现就是 ``app/processors/hardlink.py``。"""
    native = adapter.runtime_module()

    assert Path(native.__file__).resolve() == REPO_ROOT / "app" / "processors" / "hardlink.py"
    assert callable(native.create_hard_links)


def test_runtime_creates_real_hard_links_without_any_script(
    adapter, source_dir, target_dir, log_dir, build_tree
):
    """端到端：``run_hardlinks`` 必须建出真硬链接。

    真硬链接 = 同 ``(st_dev, st_ino)`` 且 ``nlink >= 2``；日志行仍必须是固定格式。
    """
    build_tree(source_dir, {"Show A/ep01.mkv": 10, "Show A/Season 1/ep02.mkv": 20})
    log_file = str(log_dir / "hard_links.log")

    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir),
        target_dir=str(target_dir),
        log_file=log_file,
    )

    assert outcome.exception is None, outcome.exception
    assert outcome.failed == []
    assert len(outcome.created) == 2

    for target in outcome.created:
        relative = os.path.relpath(target, str(target_dir))
        source_stat = os.stat(os.path.join(str(source_dir), relative))
        target_stat = os.stat(target)
        assert (source_stat.st_dev, source_stat.st_ino) == (
            target_stat.st_dev,
            target_stat.st_ino,
        )
        assert target_stat.st_nlink >= 2

    lines = [line for line in Path(log_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1 and _RAW_LOG_LINE.match(lines[0]), lines


def test_no_reference_copy_or_loader_survives_in_the_app():
    """静态钉死：单一实现 —— 第二份实现或任何旁路调用点都不许复活。

    "行为不依赖任何外部实现"只有在这条通过时才是结构上成立的 —— 只要有人把
    旁路实现放回来，它就会失败。
    """
    import io
    import tokenize

    app_root = REPO_ROOT / "app"
    assert not (app_root / "processors" / "reference").exists(), "不该存在处理器实现的副本目录"
    assert not (app_root / "processors" / "refcheck.py").exists(), "不该存在动态载入器"

    # 只看**代码 token**：注释与字符串里提到历史实现的名字
    # 是文档，不是依赖 —— 真正要禁的是这些名字出现在可执行的标识符里。
    skipped = {
        tokenize.COMMENT,
        tokenize.STRING,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
    }
    offenders: dict[str, set[str]] = {}
    for path in sorted(app_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tokens = {
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in skipped
        }
        hits = {name for name in ("refcheck", "load_reference", "load_module") if name in tokens}
        if hits:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = hits
    assert offenders == {}, f"仍有代码引用参考副本载入器：{offenders}"

def test_last_failures_reports_the_full_target_path_for_dashed_folders(
    build_tree, source_dir, target_dir, log_dir, tmp_path
):
    """失败项除了照基线打印，还按 ``(目标路径, 异常文本)`` 结构化暴露。

    stdout 文本通道用 ``" - "`` 当分隔符 —— 一级文件夹名自带 ``" - "`` 时目标路径会被切短
    （下面第 2 段断言把这个旧口径钉死，说明"为什么必须有结构化通道"）。适配层据此做
    ``already_linked`` 改判，所以这条通道的**路径必须完整**。
    """
    from app.processors import hardlink as native

    log_file = str(log_dir / "hard_links.log")
    build_tree(source_dir, {"Show - Dash/ep01.mkv": 10})

    _stdout, _text = _run_direct(native, str(source_dir), str(target_dir), log_file)
    assert native.last_failures() == [], "第一次运行目标还不存在，不该有失败"

    expected = str(target_dir / "Show - Dash" / "ep01.mkv")
    stdout, _text = _run_direct(native, str(source_dir), str(target_dir), log_file)
    failures = native.last_failures()
    # 实现用 os.path.relpath 取相对路径，一级目录得到 "." ⇒ 目标路径里带 "/./"
    # （stdout 的那行也是这个形状）；结构化通道给的是**完整**路径，规范化后即这个文件。
    assert [os.path.normpath(target) for target, _error in failures] == [expected], failures
    assert "File exists" in failures[0][1] or "Errno 17" in failures[0][1], failures[0][1]

    failed_line = next(line for line in stdout.splitlines() if line.startswith("创建硬链接失败: "))
    body = failed_line[len("创建硬链接失败: "):]
    assert body.partition(" - ")[0].endswith("Show"), (
        "旧口径（第一个 \" - \"）会把含 ' - ' 的文件夹名截短 —— 结构化通道存在的理由"
    )
    assert not body.partition(" - ")[0].endswith("ep01.mkv")

    # 留痕只反映**最近一次**运行：换一个干净目标目录重跑 ⇒ 全部成功、列表清空。
    fresh_target = str(tmp_path / "t2")
    _run_direct(native, str(source_dir), fresh_target, log_file)
    assert native.last_failures() == [], "每次 create_hard_links 开头都要清空留痕"
