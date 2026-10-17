"""硬链接执行适配层（PROCESSORS §2）。

**重命名由 :mod:`app.processors.engine` 的配置驱动引擎执行**，
见 ``docs/contracts/PROCESSORS.md`` §4；硬链接逻辑由 :mod:`app.processors.hardlink`
原生承担，本模块只剩"驱动 + 收窄范围 + 抓取结果"。

**红线**（PROCESSORS §1）：永不把 EXDEV 降级为复制。

本模块只做四件事：

1. 调用前覆盖原生模块的模块级常量 ``LOG_FILE``，让日志读写应用自己的数据卷。
2. 用非侵入手段（stdout 重定向、``os`` 代理、日志代理）收窄范围并抓取结果。
3. ``force`` / ``orphan_cleanup`` 的日志预处理。
4. 调用后逐文件核对 ``(st_dev, st_ino)``，任何不是真硬链接的目标都进 ``failed``。

跨文件系统的预筛：结论只进 ``warnings``，**绝不硬拦**；真正的结论由原生实现
逐文件的 ``os.link`` 给出。任何环节都不存在“复制文件冒充硬链接”的回退路径。

原生实现 ``app/processors/hardlink.py`` 在体积变化时**整份读-改-写**日志文件，两个任务并发
会互相覆盖，因此本模块只允许被**单工作线程**（``app/queue.py``）串行调用。
"""

from __future__ import annotations

import io
import logging
import os
import re
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from types import ModuleType
from typing import Iterator

from .paths import check_hardlink_support, normalize
from .processors import hardlink as hardlink_native
from .processors.engine import RenameOutcome  # noqa: F401  （兼容旧引用：引擎的返回类型）

_LOG_HANDLERS: dict[str, logging.FileHandler] = {}


@dataclass
class LinkOutcome:
    """``create_hard_links`` 一次调用的结构化结果。"""

    folder: str
    created: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    orphans_removed: list[str] = field(default_factory=list)
    logged: list[tuple[str, int]] = field(default_factory=list)
    stdout: str = ""
    exception: str | None = None
    #: 跨挂载等预筛结论（**警告**，不影响是否执行；失败仍由 ``failed`` 逐条体现）。
    warnings: list[str] = field(default_factory=list)
    #: 目标侧**已经是源文件硬链接**（同一 ``(st_dev, st_ino)``）的文件。
    #: 原生实现照基线对它们打印 ``创建硬链接失败: … File exists``，这里改判成"幂等跳过"，
    #: 好让 ``force`` 重跑不再被算成任务失败（幂等性 ≠ 失败）。
    already_linked: list[str] = field(default_factory=list)
    #: ``permission_mode``：本次**实际修正**权限位的目录/文件数量与失败项。
    #: 默认全 0/空 —— 开关关闭时原生实现一个 chmod 都不做，这里也就永远是 0。
    permission_dirs: int = 0
    permission_files: int = 0
    permission_errors: list[str] = field(default_factory=list)


def runtime_module() -> ModuleType:
    """返回**运行期真正被驱动**的那个模块（原生实现 ``app.processors.hardlink``）。

    ``run_hardlinks`` 驱动的是它；想在调用期间观察/替换 ``os`` 代理或模块级常量，
    必须拿这一个模块（测试广泛使用）。
    """
    return hardlink_native


# --------------------------------------------------------------------------- ScopedOs


class ScopedOs:
    """``os`` 代理：仅在 ``scoped_path`` 上过滤 ``listdir`` / ``scandir`` 结果，其余透传。

    实现方式——把 ``mod.os`` 换成它，就能让原生实现只看到一个指定的一级子文件夹，
    从而“定向重跑单个文件夹”。
    """

    def __init__(self, real_os, scoped_path: str | None, allowed: list[str]):
        self._os = real_os
        self._allowed = set(name for name in (allowed or []) if name)
        # ``scoped_path`` 为空、**或允许列表为空**时不做任何范围收窄（全量运行），
        # 此时 ``_is_scoped`` 恒为 False。
        #
        # 为什么 ``allowed`` 为空也要退化成全量：调用点写的是
        # ``_scoped_os(module, source_dir, [folder] if folder else [])``，全量运行
        # （``folder is None``）时 allowed==[]。若此时仍按 ``scoped_path`` 收窄，
        # ``listdir(source_dir)`` 会返回 ``[]``——原生实现遍历不到任何一级文件夹，
        # 整个任务静默变成空操作（created=[]、不写日志、exception=None ⇒ executor 判 success）。
        # 这是撞到过的缺陷，所以把这条不变式放在类里，而不是指望每个调用点都写对。
        self._scoped = normalize(scoped_path) if (scoped_path and self._allowed) else None

    def _is_scoped(self, path) -> bool:
        if self._scoped is None:
            return False
        try:
            return normalize(os.fspath(path)) == self._scoped
        except TypeError:
            return False

    def listdir(self, path="."):
        entries = list(self._os.listdir(path))
        if self._is_scoped(path):
            return [name for name in entries if name in self._allowed]
        return entries

    def scandir(self, path="."):
        entries = list(self._os.scandir(path))
        if self._is_scoped(path):
            return [entry for entry in entries if entry.name in self._allowed]
        return entries

    def __getattr__(self, name):
        # 只透传非魔术属性，避免 copy/pickle 之类的协议被代理破坏。
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._os, name)


@contextmanager
def _scoped_os(module: ModuleType, scoped_path: str | None, allowed: list[str]) -> Iterator[None]:
    """把 ``module.os`` 临时换成 ``ScopedOs``，``finally`` 恢复。

    ``scoped_path`` 为空、或 ``allowed`` 为空时仍然替换，但 ``ScopedOs`` 不过滤任何条目
    （见 ``ScopedOs.__init__`` 里的不变式说明：空允许列表必须是全量运行，不能是"什么都看不到"）——
    这样调用点可以永远写成同一个 ``with``，不必在两层缩进里复制调用代码。"""
    saved = module.os
    module.os = ScopedOs(saved, scoped_path, allowed)
    try:
        yield
    finally:
        module.os = saved


# --------------------------------------------------------------------------- 日志处理

_LOG_LINE_FORMAT = "%(asctime)s - %(message)s"

#: 允许写进「库日志文件」（``hard_links.log``）的**唯一**两类记账行：
#: "数据型完全不清理，且只用来判断是否跳过、是否改名，与做判断无关的信息都不应记录进来"）。
#:
#: 为什么需要白名单：``ensure_file_logger()`` 把 handler 挂在 **root logger** 上（见下），
#: 于是**整个应用**的日志记录（uvicorn 访问日志、任务日志、异常回溯……）都会被灌进
#: ``hard_links.log``。真实实例上这个文件因此无限膨胀，而它本来只该存"哪个文件夹处理过、
#: 体积多少"这类**判断依据**。本项目只写这两类行（``app/processors/hardlink.py``）。
_LOG_BOOKKEEPING_PREFIXES = (
    "Deleted orphaned hard link folder: ",
    "Source: ",
)


class _BookkeepingFilter(logging.Filter):
    """只放行记账行的 handler 级过滤器（不影响其它 handler / 控制台输出）。

    ``Source: <源> - Folder: <文件夹> - Folder size: <体积>`` 与
    ``Deleted orphaned hard link folder: <路径>`` 之外的一律丢弃（返回 ``False``）。

    注意：**只过滤，不清理**。日志文件里的历史行一个字节都不删、不做轮转 —— 那些行是
    "跳过 / 改名"的判断依据（``_parse_log_line`` 仍按原格式解析），按用户口径必须长期保留。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging 的协议名
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 坏格式参数（极罕见）一律丢弃
            return False
        if not isinstance(message, str):
            return False
        if message.startswith("Deleted orphaned hard link folder: "):
            return True
        if message.startswith("Source: "):
            return " - Folder: " in message and " - Folder size: " in message
        return False


def ensure_file_logger(log_file: str, level: int = logging.INFO) -> logging.FileHandler:
    """确保 root logger 上挂着指向 ``log_file`` 的 ``FileHandler`` 并返回它。

    为什么必须由我们接管：原生实现的 ``logging.basicConfig``（见
    ``app/processors/hardlink.py``）在
    ``root.handlers`` 非空时是 **no-op**，于是编码（容器 locale 可能是 ASCII）和格式
    （``%(asctime)s - %(message)s``）就由我们负责，才能保证日志行可被自己解析。

    **不移除任何其他 handler**——别人装的 handler 不归我们管。

    挂上 :class:`_BookkeepingFilter`，让这个文件**只**记两类记账行（见常量说明）。
    真实的污染路径不止一类（root logger 上的 handler 会收到全应用的记录），所以这层过滤
    是"日志不再无限膨胀"的关键；过滤**不会**影响任何其它 handler（控制台/任务日志照旧）。
    """
    root = logging.getLogger()
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and os.path.abspath(
            getattr(handler, "baseFilename", "")
        ) == os.path.abspath(log_file):
            return handler
    cached = _LOG_HANDLERS.get(log_file)
    if cached is not None and cached in root.handlers:
        return cached
    handler = logging.FileHandler(log_file, encoding="utf-8", delay=False)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_LINE_FORMAT))
    handler.addFilter(_BookkeepingFilter())
    root.addHandler(handler)
    _LOG_HANDLERS[log_file] = handler
    return handler


def ensure_log_file(log_file: str) -> None:
    """保证日志文件存在（原生实现会在 ``open(LOG_FILE, 'r')`` 前先判断存在性）。"""
    directory = os.path.dirname(os.path.abspath(log_file))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not os.path.exists(log_file):
        with open(log_file, "a", encoding="utf-8"):
            pass


def _parse_log_line(line: str) -> tuple[str, str] | None:
    """解析一行日志，返回 ``(source_dir, folder)``。

    解析规则必须逐字保持：3 段 ``' - '`` 分隔，否则视为无效行（静默忽略）。"""
    if " - Source: " not in line or " - Folder: " not in line or " - Folder size: " not in line:
        return None
    try:
        _timestamp, message = line.strip().split(" - ", 1)
        source_part, folder_part, _size_part = message.split(" - ")
        source_dir = source_part.split(": ")[1]
        folder = folder_part.split(": ")[1]
    except (ValueError, IndexError):
        return None
    return source_dir, folder


def _read_lines(log_file: str) -> list[str]:
    """读日志全文；文件不存在返回空列表。"""
    if not os.path.exists(log_file):
        return []
    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


def _write_lines(log_file: str, lines: list[str]) -> None:
    """整份回写日志（读-改-写；应用保证全局串行）。"""
    directory = os.path.dirname(os.path.abspath(log_file))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(log_file, "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _drop_missing_source_lines(log_file: str, source_dir: str) -> list[tuple[int, str]]:
    """``orphan_cleanup=False`` 的实现：把“源已不存在”的日志行临时移出文件。

    背景：原生实现会在“日志里有记录、``source_dir/<folder>`` 不存在”
    时 **递归删除** ``target_dir/<folder>``。要关掉这个破坏性分支又不改它的源码，
    做法就是让它“看不到”这些条目——于是把行摘到内存、执行完再合并回写。

    只处理当前 ``source_dir`` 的记录（按源目录隔离）。
    返回 ``[(原始行号, 行内容), ...]``。
    """
    lines = _read_lines(log_file)
    kept: list[str] = []
    removed: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        parsed = _parse_log_line(line)
        if parsed is None:
            kept.append(line)
            continue
        line_source, folder = parsed
        if line_source != source_dir:
            kept.append(line)
            continue
        if os.path.exists(os.path.join(source_dir, folder)):
            kept.append(line)
            continue
        removed.append((index, line))
    if removed:
        _write_lines(log_file, kept)
    return removed


def _restore_lines(log_file: str, removed: list[tuple[int, str]]) -> None:
    """把 ``_drop_missing_source_lines`` 摘出的行插回它们被摘出的位置。

    先按内容去重：原生实现可能在本次运行里重新记录了同一 folder，
    此时保留它新写的那条，丢弃我们的旧行（避免出现两条同 folder 记录让体积判定错乱）。
    """
    if not removed:
        return
    lines = _read_lines(log_file)
    current = set(lines)
    pending = [(index, line) for index, line in removed if line not in current]
    for index, line in pending:
        position = min(index, len(lines))
        lines.insert(position, line)
    _write_lines(log_file, lines)


def remove_log_entries(log_file: str, source_dir: str, folder: str) -> int:
    """从硬链接日志里删掉该 folder 的记录，返回删除行数。

    不做这一步，原生实现的“体积一致即跳过”分支会让
    ``force=True`` 完全失去作用。

    ``folder`` 为 ``None``/``"*"`` 时删除该源目录的全部记录（全量强制重跑）。
    """
    marker = f"Source: {source_dir} - Folder: "
    lines = _read_lines(log_file)
    kept: list[str] = []
    removed = 0
    for line in lines:
        if folder in (None, "", "*"):
            match = marker in line
        else:
            match = f"{marker}{folder} - Folder size:" in line
        if match:
            removed += 1
        else:
            kept.append(line)
    if removed:
        _write_lines(log_file, kept)
    return removed


# --------------------------------------------------------------------------- stdout 解析

#: ``创建硬链接失败: <目标> - <异常>`` 的**可靠**分隔符。
#: ``os.link`` 抛出的 OSError 一族 ``str(exc)`` 一律以 ``[Errno N]`` / ``[WinError N]`` 开头，
#: 因此"后随这两种方括号的 ``" - "``"就是路径与异常的分界；路径本身不会长这样
#: （``getattr(os, "supports_follow_symlinks")`` 之类的怪异名字不是路径的一部分）。
#: 只按**第一个** ``" - "`` 切分时，一级文件夹名自带 ``" - "`` 会把目标路径切短，
#: 使 ``_split_already_linked`` 的 stat 比对落空 ⇒ 幂等改判失效。
_CREATE_FAILED_SPLIT = re.compile(r"\s+-\s+(?=\[?(?:Errno|WinError)\b)")


def _structured_failures(module: ModuleType) -> list[tuple[str, str]] | None:
    """取原生实现的**结构化**失败项；接口缺失或钩子出错时返回 ``None``（退回文本通道）。"""
    hook = getattr(module, "last_failures", None)
    if not callable(hook):
        return None
    try:
        items = hook()
    except Exception:  # noqa: BLE001 - 留痕通道坏了不该让整次运行失败
        return None
    if not isinstance(items, list):
        return None
    return [(str(target), str(error)) for target, error in items]


def _permission_stats(module: ModuleType) -> dict | None:
    """取原生实现 ``permission_mode`` 的结构化结果；接口缺失或钩子出错时返回 ``None``。

    形状见 ``app.processors.hardlink.last_permission_stats``。与 ``_structured_failures``
    同一取向：留痕通道坏了不该让整次运行失败（最多是任务日志里少一行统计）。
    """
    hook = getattr(module, "last_permission_stats", None)
    if not callable(hook):
        return None
    try:
        stats = hook()
    except Exception:  # noqa: BLE001 - 统计通道坏了不该让整次运行失败
        return None
    return stats if isinstance(stats, dict) else None


def _parse_hardlink_stdout(text: str) -> tuple[list[str], list[tuple[str, str]], list[str], list[str], list[tuple[str, int]]]:
    """解析 ``create_hard_links`` 的输出（ARCHITECTURE §6.5 第 8 点的全部前缀）。

    文本格式是**冻结**的：``创建硬链接失败: <path> - <err>``。切分用的是
    "路径里不可能出现的那一段" —— ``str(exc)`` 对 OSError 一族一律以 ``[Errno N]`` /
    ``[WinError N]`` 开头（见 ``_CREATE_FAILED_SPLIT``），命中就按它切；非 OSError 的异常
    文本退回"第一个 ``" - "``"口径。原生实现另有结构化通道
    （``hardlink.last_failures()``），``run_hardlinks`` 优先用它。
    """
    created: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[str] = []
    orphans: list[str] = []
    logged: list[tuple[str, int]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("已创建硬链接: "):
            created.append(line[len("已创建硬链接: "):].strip())
        elif line.startswith("创建硬链接失败: "):
            body = line[len("创建硬链接失败: "):]
            parts = _CREATE_FAILED_SPLIT.split(body, maxsplit=1)
            if len(parts) == 2:
                target, error = parts
            else:
                # 非 OSError 的异常文本（没有 [Errno…] 锚点）：退回按第一个分隔符切分。
                target, _sep, error = body.partition(" - ")
            failed.append((target.strip(), error.strip()))
        elif line.startswith("文件夹已记录且体积一致，跳过: "):
            skipped.append(line[len("文件夹已记录且体积一致，跳过: "):].strip())
        elif line.startswith("Deleted orphaned hard link folder: "):
            orphans.append(line[len("Deleted orphaned hard link folder: "):].strip())
        elif line.startswith("已记录文件夹体积: "):
            body = line[len("已记录文件夹体积: "):]
            name, _sep, raw_size = body.rpartition(" - ")
            try:
                logged.append((name.strip(), int(raw_size.strip())))
            except ValueError:
                continue
        # “已删除旧记录: X” / “已删除文件体积记录” 无需结构化，由调用方按级别记 INFO
    return created, failed, skipped, orphans, logged


def _verify_created_links(
    source_dir: str,
    target_dir: str,
    created: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """逐文件核对 ``created`` 的目标确实是源文件的硬链接。

    比对 ``(st_dev, st_ino)``；任何不符（含目标已被替换为普通文件）都会移入 ``failed``
    并说明“非硬链接”。绝不接受“复制出来的同名文件”。"""
    verified: list[str] = []
    failed: list[tuple[str, str]] = []
    for target in created:
        try:
            relative = os.path.relpath(target, target_dir)
            source = os.path.join(source_dir, relative)
            source_stat = os.stat(source)
            target_stat = os.stat(target)
        except OSError as exc:
            failed.append((target, f"非硬链接：无法比对源/目标 inode（{exc}）"))
            continue
        if (source_stat.st_dev, source_stat.st_ino) == (
            target_stat.st_dev,
            target_stat.st_ino,
        ):
            verified.append(target)
        else:
            failed.append(
                (
                    target,
                    "非硬链接：目标 (st_dev=%s, st_ino=%s) 与源 (st_dev=%s, st_ino=%s) 不一致，"
                    "疑似被复制而非硬链接。本应用不提供复制回退。"
                    % (
                        target_stat.st_dev,
                        target_stat.st_ino,
                        source_stat.st_dev,
                        source_stat.st_ino,
                    ),
                )
            )
    return verified, failed


#: 目标已存在时原生实现打印的错误片段（POSIX ``[Errno 17] File exists`` / Windows
#: ``[WinError 183]``）。只有命中这些片段才值得去比 inode —— 其它错误（EXDEV、EACCES…）
#: 一律保持"失败"。
_EXISTS_MARKERS = ("File exists", "Errno 17", "WinError 183", "EEXIST", "已存在")


def _split_already_linked(
    source_dir: str,
    target_dir: str,
    failed: list[tuple[str, str]],
) -> tuple[list[str], list[tuple[str, str]]]:
    """把"目标已存在**且就是源文件的硬链接**"的失败项改判成**幂等跳过**。

    为什么要这一层：``force`` 重跑时原生实现对每个已存在的目标文件都会
    ``os.link`` 失败并打印 ``创建硬链接失败: … File exists``；执行器把
    ``link_failed > 0 且 linked == 0`` 判成任务失败 —— 于是"给一个有命名模板的库重跑"
    永远是红的，尽管目标里躺着的正是**同 inode 的硬链接**。

    判据严格只认 ``(st_dev, st_ino)`` 相同：目标同名但**不是**硬链接（不同 inode）时
    仍然留在 ``failed`` 里 —— 「绝不覆盖、绝不把复制品当成功」没有松动。
    原生实现的 stdout 一个字都不改，这里只改**账**。
    """
    already: list[str] = []
    remaining: list[tuple[str, str]] = []
    for target, error in failed:
        if not any(marker in error for marker in _EXISTS_MARKERS):
            remaining.append((target, error))
            continue
        try:
            relative = os.path.relpath(target, target_dir)
            source = os.path.join(source_dir, relative)
            source_stat = os.stat(source)
            target_stat = os.stat(target)
        except OSError:
            remaining.append((target, error))
            continue
        if (source_stat.st_dev, source_stat.st_ino) == (target_stat.st_dev, target_stat.st_ino):
            already.append(target)
        else:
            remaining.append((target, error))
    return already, remaining


# --------------------------------------------------------------------------- 对外调用


def run_hardlinks(
    *,
    source_dir: str,
    target_dir: str,
    log_file: str,
    folder: str | None = None,
    folders: list[str] | None = None,
    force: bool = False,
    orphan_cleanup: bool = True,
    skip: dict[str, int] | None = None,
    permission_mode: bool = False,
) -> LinkOutcome:
    """调用 ``create_hard_links.create_hard_links`` 并返回结构化结果。

    逐条对应 ARCHITECTURE §6.5 的 10 个要点；**任何**分支都不会去写 ``source_dir``
    以外的东西，也不会写 ``/downloads`` 里的媒体文件。

    :param folder: 只处理这一个一级文件夹（``scope.mode=folder``）
    :param folders: 只处理这一批一级文件夹（``scope.mode=works``：作品级重命名
        走的是 ``kind="rename"``、根本不建链，本参数是为"作品级重建链"预留的同构入口）；
        与 ``folder`` 同时给出时以 ``folders`` 为准。
    :param skip: 建链判据（``app/link_records.py`` 的每库账本）。``None`` = 读全局日志，
        由"只跑原生实现"的调用方使用。
    :param permission_mode: 库设置里的 ``permission_mode``。
        ``True`` 时原生实现会把目标侧（库根 + 本次处理到的每个一级文件夹的目标子树，含跳过的旧作品）
        改成 0777；默认 ``False`` 时**一个 chmod 都不做**。
    """
    effective = [name for name in (folders or []) if str(name or "").strip()]
    if not effective and folder:
        effective = [folder]
    outcome = LinkOutcome(folder=", ".join(effective) if effective else "*")
    source_dir = normalize(source_dir)
    target_dir = normalize(target_dir)

    # 跨文件系统 / 跨挂载**警告但允许跑，绝不硬拦**。
    # 硬拦会把"挂载配错"升级成"整个库建不了"，而预筛判据（挂载 ID 相同）**必要不充分**
    # （btrfs 同一挂载内跨子卷仍可能 EXDEV）；真正的结论由原生实现实际的 os.link 给出，
    # 失败会逐条进 ``failed``。判据顺序见 ``paths.check_hardlink_support``。
    #
    # ``probe=False``：这一层**不做写入型实证**。它是"仅供参考的警告"，而且执行器（以及
    # 校验接口）在调用它之前已经做过一次实证探测；每次任务在这里再往目标目录建/删一个
    # 临时链接纯属重复副作用，也会让"目标库逐字节不变"的守卫看见目录 mtime 变动。
    support, support_note = check_hardlink_support(source_dir, target_dir, probe=False)
    if support is False:
        outcome.warnings.append(f"{support_note}（仍会尝试建链，失败会逐条记录。）")
    elif support is None:
        outcome.warnings.append(f"无法判定源/目标之间能否创建硬链接：{support_note}")

    if not os.path.isdir(source_dir):
        outcome.exception = f"源目录不存在或不可访问：{source_dir}"
        return outcome

    ensure_log_file(log_file)  # 要点 2 的前置：原生实现也会 open(LOG_FILE)
    # 要点 1：直接用应用自带的原生实现，只覆盖它的模块级 LOG_FILE。
    module = hardlink_native
    module.LOG_FILE = log_file  # 要点 2
    ensure_file_logger(log_file)  # 要点 3

    removed_lines: list[tuple[int, str]] = []
    try:
        # 要点 6：强制重跑先删掉该 scope 的记录，否则会命中跳过分支。
        # scope 可以是一批作品 —— 逐个删；空列表 = 整库。
        if force:
            if effective:
                for name in effective:
                    remove_log_entries(log_file, source_dir, name)
            else:
                remove_log_entries(log_file, source_dir, None)
        # 要点 5：orphan_cleanup=False —— 把“源已不存在”的行临时摘出，跑完回写。
        if not orphan_cleanup:
            removed_lines = _drop_missing_source_lines(log_file, source_dir)

        # 要点 5 的第二半：``skip`` 是每库账本时，原生实现的孤儿分支遍历的是
        # **这份 dict**（``_cleanup_orphans`` 的输入是 ``dict(skip)``），不看日志行 ——
        # 不先把"源侧一级文件夹已不存在"的键摘掉，``orphan_cleanup=False`` 就会形同虚设：
        # 源作品被删掉后第二次运行仍会 rmtree 目标侧整个作品文件夹。
        effective_skip = skip
        if not orphan_cleanup and skip is not None:
            effective_skip = {
                name: size
                for name, size in skip.items()
                if os.path.exists(os.path.join(source_dir, name))
            }

        buffer = io.StringIO()
        with _scoped_os(module, source_dir, effective):
            # 要点 7：StringIO 同时规避容器 locale 造成的 UnicodeEncodeError。
            with redirect_stdout(buffer):
                module.create_hard_links(
                    source_dir,
                    target_dir,
                    skip=effective_skip,
                    permission_mode=permission_mode,
                )
        captured = buffer.getvalue()
    except Exception as exc:  # 源目录不存在时 os.listdir 会抛 FileNotFoundError
        outcome.stdout = ""
        outcome.exception = f"{type(exc).__name__}: {exc}"
        return outcome
    finally:
        # 要点 5 的后半：无条件把摘出的行合并回写。
        try:
            _restore_lines(log_file, removed_lines)
        except OSError:
            pass

    outcome.stdout = captured
    created, failed, skipped, orphans, logged = _parse_hardlink_stdout(captured)
    # 优先用原生实现暴露的**结构化**失败项。文本通道把 ``" - "`` 当分隔符，
    # 一级文件夹名自带 ``" - "`` 时目标路径会被切短，``_split_already_linked`` 的 stat 比对随之
    # 落空 ⇒ 明明已经建好的链被算成失败、每次重跑都刷一批无害的 ``File exists``。
    structured = _structured_failures(module)
    if structured is not None:
        failed = structured
    # 要点 10：逐文件核对 st_dev/st_ino。
    verified, bogus = _verify_created_links(source_dir, target_dir, created)
    # 目标已存在且就是源文件的硬链接 ⇒ 幂等跳过，不算失败（force 重跑不再报红）。
    already, failed = _split_already_linked(source_dir, target_dir, failed)
    outcome.created = verified
    outcome.failed = failed + bogus
    outcome.already_linked = already
    outcome.skipped = skipped
    outcome.orphans_removed = orphans
    outcome.logged = logged
    # ``permission_mode``：把原生实现实际修正的目录/文件数与失败项带上。
    # 只在开关打开时读 —— 关闭时原生实现一个 chmod 都不做，读它没有意义。
    if permission_mode:
        stats = _permission_stats(module)
        if stats is not None:
            outcome.permission_dirs = int(stats.get("dirs") or 0)
            outcome.permission_files = int(stats.get("files") or 0)
            raw_errors = stats.get("errors") or []
            if isinstance(raw_errors, list):
                outcome.permission_errors = [str(item) for item in raw_errors]
    return outcome
