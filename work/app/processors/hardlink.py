"""硬链接的**容器内原生实现**。

实现说明（PROCESSORS §2）：建链逻辑全部写在本模块里 —— 读日志前的存在性判断、整份读-改-写、
只扫一级子目录等等都在这里，行为改动只改这一份代码。容器因此可以独立运行，也可以整体搬到
别的设备上（镜像里没有任何指向宿主的路径假设）。

**行为契约**（由 ``tests/test_hardlink_native.py`` 等项目自己的回归测试守卫）：

1. 打印的每一行**逐字冻结** —— stdout 是适配层解析结果的契约，不是给人看的日志；
2. 写进日志文件的行同样**逐字冻结**（``%(asctime)s - Source: … - Folder: … - Folder size: N``）；
3. 跳过分支、孤儿清理、读-改-写的**先后顺序**也冻结（含 ``已删除旧记录`` 的打印时机）；
4. 只遍历 ``source_dir`` 的**一级子目录**，递归建链，单个文件失败只打印、不中断整轮；
5. 任何分支都不写 ``source_dir``（源侧只 ``listdir``/``walk``/``stat``）——**唯一的例外**是
   ``permission_mode=True``：那时会对目标侧硬链接执行 ``os.chmod``，而硬链接与源文件共享
   同一个 inode，于是源文件的**权限位**会一起改变（大小、inode 号、mtime 都不变）。
   这条例外是刻意收窄的（默认关闭 + 开启前二次确认，见 BASELINE §1.6/B13）；
6. ``permission_mode`` 的默认值是 ``False``，此时本模块不 chmod、不 stat 权限位；开启后只把
   目标侧（含库根目录）改成 0777，不打印任何新行 —— stdout 契约与日志格式因此完全不受影响。

``os`` 是模块级名字并且**每次调用时现取**（``os.listdir(...)`` 而不是把函数提到模块级），
这样适配层的 ``ScopedOs`` 代理仍然能通过替换 ``module.os`` 做"定向重跑单个文件夹"。

整份读-改-写日志（体积变化时）不是并发安全的，两个任务并发会互相覆盖，因此本模块
只允许被**单工作线程**（``app/queue.py``）串行调用。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat

__all__ = [
    "LOG_FILE",
    "LOG_FORMAT",
    "create_hard_links",
    "last_failures",
    "reset_last_failures",
    "last_permission_stats",
    "read_logged_folders",
]

#: 模块级日志路径常量。``script_adapter.run_hardlinks`` 会在**调用前**把它覆盖成应用
#: 数据卷里的真实路径（``<data_dir>/logs/hard_links.log``，见 `app/config.py`）。
#: 默认值取数据卷内的位置，因此这个模块单独 import 进来也能工作，不依赖任何宿主路径、
#: 也不假设宿主的日志目录存在。
LOG_FILE = os.path.join("/data/logs", "hard_links.log")

#: 日志行格式是冻结的 —— 本模块写的行必须能被本模块解析回来。
LOG_FORMAT = "%(asctime)s - %(message)s"

_SOURCE_MARK = " - Source: "
_FOLDER_MARK = " - Folder: "
_SIZE_MARK = " - Folder size: "

#: 打印前缀（stdout 契约；``script_adapter._parse_hardlink_stdout`` 按这些前缀取值）。
PRINT_CREATED = "已创建硬链接: "
PRINT_CREATE_FAILED = "创建硬链接失败: "
PRINT_SKIPPED = "文件夹已记录且体积一致，跳过: "
PRINT_ORPHAN = "Deleted orphaned hard link folder: "
PRINT_OLD_RECORD = "已删除旧记录: "
PRINT_LOGGED = "已记录文件夹体积: "

#: 本次 ``create_hard_links()`` 里创建失败的 ``(目标路径, 异常文本)``。
#:
#: **结构化留痕**通道：调用方（``script_adapter.run_hardlinks``）优先读它，
#: 而不是从 stdout 的 ``创建硬链接失败: <目标> - <异常>`` 文本里切分路径。原因是那个文本格式
#: 用 ``" - "`` 当分隔符，一级文件夹名本身含 ``" - "`` 时会把目标路径切短，导致
#: ``already_linked`` 改判失效（``tests/test_idempotency.py`` 的 ``Show - Dash``）。
#: 文本通道保持逐字不变（stdout 形态冻结），只是不再被当成**唯一**来源。
_FAILURES: list[tuple[str, str]] = []


def last_failures() -> list[tuple[str, str]]:
    """返回最近一次 ``create_hard_links()`` 里失败项的 ``(目标路径, 异常文本)`` 副本。"""
    return list(_FAILURES)


def reset_last_failures() -> None:
    """清空结构化失败留痕。

    调用方（``script_adapter.run_hardlinks``）在**每次调用前**显式用它，
    让 ``last_failures()`` 只反映本次运行：驱动实现可能被替换（测试替身、
    ``runtime_module()`` 的其它注入方），不能指望被驱动的那个函数自己会清。
    """
    _FAILURES.clear()


# --------------------------------------------------------------------- permission_mode
#
# ``permission_mode`` 默认关闭，开启前必须二次确认。
# 语义（把"chmod 0777 整棵树"收窄到"本功能的产物"）：
#   目标库根目录 + 本次处理到的每个一级文件夹的**目标侧整棵子树**（目录与硬链接）改成 0777。
# 为什么必须包含"跳过分支"的文件夹：开关的价值就在于让**已经入库过**的作品对非 root 服务
# （实测场景：uid 0 之外的 Emby）可读；那些文件夹体积一致、一条新链都不建，只改新建产物
# 等于开关静默无效。它们的目标子树是本应用先前若干次运行建出来的（``app/link_records.py`` 有账）。
# 已知副作用（已写进确认框与 BASELINE B9 修订）：硬链接与源文件共享 inode ⇒ 源文件权限位
# 一起变。大小、inode 号、mtime 都不变，因此 ``app/paths.snapshot_source_tree`` 的完整性
# 复核维度不受影响（它比的是 size/ino/mtime_ns，不比权限位）。

#: ``permission_mode`` 下使用的目标权限位（0777；目录/文件分开常量便于日后收窄）。
PERMISSION_MODE_DIR = 0o777
PERMISSION_MODE_FILE = 0o777

#: 最近一次 ``create_hard_links()`` 在 ``permission_mode`` 下**实际修正**（权限位不同、真的调用了
#: ``chmod``）的目录/文件数与失败项。已经是目标值的不计数 ⇒ 幂等重跑第二次为 0，
#: 日志里的数字因此是"这次改了多少"，而不是"扫了多少"。
_PERMISSION_STATS: dict[str, object] = {"dirs": 0, "files": 0, "errors": []}


def last_permission_stats() -> dict[str, object]:
    """返回最近一次 ``create_hard_links()`` 的权限修正结果副本。

    形状：``{"dirs": int, "files": int, "errors": [str, ...]}``（``errors`` 是
    ``"<路径> - <异常文本>"``）。适配层（``app/script_adapter.run_hardlinks``）读取它，
    再经 ``LinkOutcome`` 交给执行器写任务日志。
    """
    errors = _PERMISSION_STATS["errors"]
    return {
        "dirs": int(_PERMISSION_STATS["dirs"]),  # type: ignore[call-overload]
        "files": int(_PERMISSION_STATS["files"]),  # type: ignore[call-overload]
        "errors": list(errors) if isinstance(errors, list) else [],
    }


def _chmod_if_needed(path: str, mode: int, counter: str) -> None:
    """把 ``path`` 的权限位改成 ``mode``；已经是目标值就跳过，符号链接一律不跟进。

    逐条目比对再改（而不是无脑 ``chmod``）有两个好处：幂等重跑不产生成千上万次多余的
    系统调用；``last_permission_stats()`` 的数字才有意义。单个条目失败只记录、不中断整轮
    （与建链的"单个文件失败只打印"同一取向）。
    """
    try:
        if os.path.islink(path):
            return
        if stat.S_IMODE(os.lstat(path).st_mode) == mode:
            return
        os.chmod(path, mode)
    except OSError as exc:
        errors = _PERMISSION_STATS["errors"]
        if isinstance(errors, list):
            errors.append(f"{path} - {exc}")
        return
    _PERMISSION_STATS[counter] = int(_PERMISSION_STATS[counter]) + 1  # type: ignore[call-overload]


def _apply_permission_mode(target_folder: str) -> None:
    """把 ``target_folder`` 整棵子树（含自身）改成 0777（``permission_mode`` 的唯一动作）。

    符号链接跳过：``os.chmod`` 默认跟进符号链接，会跑到目标库外面去改别的文件 ——
    本功能绝不允许发生这种事。
    """
    _chmod_if_needed(target_folder, PERMISSION_MODE_DIR, "dirs")
    for root, dirs, files in os.walk(target_folder):
        for name in dirs:
            _chmod_if_needed(os.path.join(root, name), PERMISSION_MODE_DIR, "dirs")
        for name in files:
            _chmod_if_needed(os.path.join(root, name), PERMISSION_MODE_FILE, "files")


def read_logged_folders(source_dir: str) -> dict[str, int]:
    """读日志里属于 ``source_dir`` 的 ``{folder: size}``。

    解析规则必须逐字保持：三段 ``" - "`` 分隔、键值再按 ``": "`` 取第二段；任何一段缺失或
    数字解析失败都**静默跳过该行**（``except (ValueError, IndexError): continue``）。
    只有 ``source_dir`` 与日志行里的源目录**逐字相等**的记录才算数（字符串比较）。
    """
    logged: dict[str, int] = {}
    if not os.path.exists(LOG_FILE):
        return logged
    # 用默认编码打开（容器 locale 由镜像决定）；不"顺手"改成 utf-8，
    # 否则同一份日志在两种实现下可能出现"一边能解析、一边跳过"的差异。
    with open(LOG_FILE, "r") as handle:
        for line in handle:
            if not (_SOURCE_MARK in line and _FOLDER_MARK in line and _SIZE_MARK in line):
                continue
            try:
                _timestamp, message = line.strip().split(" - ", 1)
                source_part, folder_part, size_part = message.split(" - ")
                source_dir_from_log = source_part.split(": ")[1]
                folder_name = folder_part.split(": ")[1]
                folder_size = int(size_part.split(": ")[1])
            except (ValueError, IndexError):
                continue
            if source_dir_from_log == source_dir:
                logged[folder_name] = folder_size
    return logged


def _folder_size(path: str) -> int:
    """递归体积。符号链接/硬链接按 ``getsize`` 原样累加，不去重。"""
    return sum(
        os.path.getsize(os.path.join(root, file))
        for root, dirs, files in os.walk(path)
        for file in files
    )


def _drop_record(source_dir: str, folder: str) -> None:
    """删掉该 folder 的日志行（整份读-改-写）。

    不做这一步，下一轮的"体积一致 ⇒ 跳过"分支会让 ``force`` 与体积变化都失去作用。
    写回用 ``"w"`` 截断，因此并发调用必须被上层串行化。
    """
    with open(LOG_FILE, "r") as handle:
        lines = handle.readlines()
    with open(LOG_FILE, "w") as handle:
        for line in lines:
            if not (f"Source: {source_dir} - Folder: {folder} - Folder size:" in line):
                handle.write(line)


def _cleanup_orphans(source_dir: str, target_dir: str, logged_folders: dict[str, int]) -> None:
    """日志里有记录、但源侧一级文件夹已不存在 ⇒ 递归删掉目标侧对应文件夹。

    这是**唯一**会删目标侧东西的分支，``orphan_cleanup=False`` 时由适配层把相关行临时摘出、
    让这里"看不到"这些条目。
    """
    for folder in list(logged_folders.keys()):
        source_folder = os.path.join(source_dir, folder)
        target_folder = os.path.join(target_dir, folder)
        if not os.path.exists(source_folder):
            if os.path.exists(target_folder):
                shutil.rmtree(target_folder)
                logging.info(f"Deleted orphaned hard link folder: {target_folder}")
                print(f"{PRINT_ORPHAN}{target_folder}")
            del logged_folders[folder]


def _link_folder(
    source_dir: str,
    target_dir: str,
    folder: str,
    logged_folders: dict[str, int],
    *,
    permission_mode: bool = False,
) -> None:
    """处理单个一级文件夹。

    ``os.listdir`` / ``os.walk`` / ``os.link`` 一律**现取模块级 ``os``**，好让适配层的
    ``ScopedOs`` 代理生效；``os.path.*`` 也走同一个 ``os`` 名字，保持一致性。
    """
    source_folder = os.path.join(source_dir, folder)
    target_folder = os.path.join(target_dir, folder)

    folder_size = _folder_size(source_folder)

    if folder in logged_folders:
        if logged_folders[folder] == folder_size:
            print(f"{PRINT_SKIPPED}{folder}")
            # 跳过分支也要修权限：开关必须对**已经入库过**的作品生效，否则"体积一致 ⇒ 跳过"
            # 会让它静默无效（约定的语义，见本模块顶部的 permission_mode 段）。
            if permission_mode:
                _apply_permission_mode(target_folder)
            return
        _drop_record(source_dir, folder)
        print(f"{PRINT_OLD_RECORD}{folder}")

    os.makedirs(target_folder, exist_ok=True)

    for root, dirs, files in os.walk(source_folder):
        relative_path = os.path.relpath(root, source_folder)
        target_subdir = os.path.join(target_folder, relative_path)
        os.makedirs(target_subdir, exist_ok=True)

        for file in files:
            source_file = os.path.join(root, file)
            target_file = os.path.join(target_subdir, file)
            try:
                os.link(source_file, target_file)
                print(f"{PRINT_CREATED}{target_file}")
            except Exception as exc:  # noqa: BLE001 - 捕获全部异常并逐条打印
                _FAILURES.append((target_file, str(exc)))
                print(f"{PRINT_CREATE_FAILED}{target_file} - {exc}")

    # 建完链再统一修权限：新建产物与"跳过分支的旧产物"走同一条代码路径（一个动作、一份计数）。
    if permission_mode:
        _apply_permission_mode(target_folder)

    logging.info(f"Source: {source_dir} - Folder: {folder} - Folder size: {folder_size}")
    print(f"{PRINT_LOGGED}{folder} - {folder_size}")


def create_hard_links(
    source_dir: str,
    target_dir: str,
    *,
    skip: dict[str, int] | None = None,
    permission_mode: bool = False,
) -> None:
    """把 ``source_dir`` 下每个一级文件夹镜像到 ``target_dir``，文件之间建硬链接。

    签名、副作用、打印顺序、日志格式都是冻结的契约（``docstring`` 与 ``tests/`` 一起守卫）。

    :param skip: 应用可以改传**每库账本**给出的 ``{folder: size}``
        （``app/link_records.py``）。传 ``None``（默认）时自己从
        ``LOG_FILE`` 读判据；``tests/test_hardlink_native.py`` 的字节级等价性依赖这条默认路径。
        传了 dict 时：键缺失 = 没处理过；值相等 = 跳过；值不等 = 删旧日志行后重建链。
        **两种情况都会照写 ``LOG_FILE``**：日志仍是兼容既有工具的台账，只是不再是判据。

    创建失败项除了逐条打印，还会通过 :func:`last_failures` 结构化暴露
    （调用方据此做 ``already_linked`` 改判，不再依赖 ``" - "`` 文本切分）。

    :param permission_mode: ``True`` 时把目标侧改成 0777 ——
        库根目录 ``target_dir`` 本身、以及本次处理到的每个一级文件夹的目标侧整棵子树
        （含"体积一致 ⇒ 跳过"的旧作品）。默认 ``False`` 时本函数不 chmod、不 stat 权限位。
        注意硬链接与源文件共享 inode ⇒ 源文件权限位一起变。
    """
    # ``logging.basicConfig``。root logger 上已经有 handler（适配层会挂一个指向数据卷的 FileHandler）时
    # 这是 no-op；单独 import 使用本模块时它才会真的接管，日志落到 LOG_FILE。
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format=LOG_FORMAT)

    # 每次运行开头清空结构化失败留痕（``last_failures()`` 只反映最近一次运行）。
    _FAILURES.clear()
    _PERMISSION_STATS["dirs"] = 0
    _PERMISSION_STATS["files"] = 0
    _errors = _PERMISSION_STATS["errors"]
    if isinstance(_errors, list):
        _errors.clear()

    # 库根目录本身也要可被穿越（其他服务以非 root 身份访问时缺 other-x 就一步都进不去）。
    # 只改这一个目录本身、不递归 —— 递归只发生在下面每个一级文件夹里。
    if permission_mode:
        _chmod_if_needed(target_dir, PERMISSION_MODE_DIR, "dirs")

    # 用副本：``_cleanup_orphans`` 会就地删键，调用方的账本 dict 不该被改写。
    logged_folders = dict(skip) if skip is not None else read_logged_folders(source_dir)
    _cleanup_orphans(source_dir, target_dir, logged_folders)

    first_level_folders = [
        name for name in os.listdir(source_dir) if os.path.isdir(os.path.join(source_dir, name))
    ]

    for folder in first_level_folders:
        _link_folder(
            source_dir, target_dir, folder, logged_folders, permission_mode=permission_mode
        )
