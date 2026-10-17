"""重命名日志（``json_cache`` 形态）的读写。

契约真源：``docs/contracts/PROCESSORS.md`` §5。三类日志里，本模块只管**每个媒体库一条的重命名日志**：

- 位置：``settings.log_dir``（默认 ``/data/logs``）**下**，不允许按媒体库分子目录；
- 名字：``log_file`` 只是文件名（内置处理器是 ``bangumicache.log`` / ``moviecache.log`` /
  ``hardlink_only.log``；自定义处理器按自己的 id 生成 ``<slug>cache.log``）；
- **一个媒体库一份日志**：共用一个日志文件时，A 库处理过的标题文件夹会让 B 库走
  「体积一致 ⇒ 已处理」分支被静默跳过，所以 ``_resolve_log_file`` 缺省一律按库分配唯一名
  （``bangumicache.log`` → 第二个库 ``bangumicache-2.log``）；
- 形态：``{ "<标题文件夹>": {"size": N, "timestamp": "YYYY-MM-DD HH:MM:SS", "works_dir": ...} }``，
  ``ensure_ascii=False, indent=4`` —— 这是**冻结**的写出参数，不得更改。

写出走**原子写**：先写 ``<name>.tmp`` 再 ``os.replace``，避免进程被杀时留下半份
JSON。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "JSON_INDENT",
    "slugify",
    "log_path",
    "library_log_path",
    "read_json_cache",
    "write_json_cache",
    "ensure_json_cache",
    "remove_entry",
    "unique_log_file",
    "unique_log_file_name",
    "log_summary",
    "iter_entries",
]

#: ``json.dump(..., ensure_ascii=False, indent=4)`` 的缩进宽度
JSON_INDENT = 4


def slugify(name: str) -> str:
    """库名 → 日志文件名 slug（小写、非 ``[a-z0-9]`` 折叠为 ``-``）。PROCESSORS §5.2。"""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "library"


def log_path(log_dir: str | os.PathLike[str], file_name: str) -> Path:
    """``<log_dir>/<file_name>``（不做存在性检查）。"""
    return Path(log_dir) / file_name


def library_log_path(log_file: str) -> Path:
    """按当前设置解析某个媒体库的日志路径（只取文件名，忽略任何目录成分）。"""
    from ..config import settings

    return log_path(settings.log_dir, os.path.basename(log_file))


def unique_log_file(
    log_dir: str | os.PathLike[str],
    name: str,
    *,
    taken: Iterable[str] = (),
) -> str:
    """生成不冲突的日志文件名：``<slug>cache.log``（撞名加 ``-2``、``-3``…）。"""
    base = f"{slugify(name)}cache.log"
    used = {str(item) for item in taken}
    candidate = base
    index = 1
    while candidate in used or os.path.exists(os.path.join(str(log_dir), candidate)):
        index += 1
        candidate = f"{base[:-4]}-{index}.log"
    return candidate


def unique_log_file_name(
    log_dir: str | os.PathLike[str],
    base_name: str,
    *,
    taken: Iterable[str] = (),
) -> str:
    """把**完整文件名**去重：``bangumicache.log`` → ``bangumicache-2.log``（序号插在扩展名前）。

    与 :func:`unique_log_file` 的分工：那个收「名字」并用 ``slugify`` 自己拼 ``cache.log``
    （按处理器 id / 库名生成新名时用），这个收**已经是完整文件名**的基名，只做去重。

    占用判定（PROCESSORS §5.2）：

    1. ``base_name`` **不在** ``taken`` ⇒ **直接用**它，即使磁盘上已有同名文件（继续追加）。
       这是**连续性**要求：从旧版本升级上来时 ``/data/logs/bangumicache.log`` 本来就在，若按
       「文件存在就换名」处理，第一个 bangumi 库会被推成 ``bangumicache-2.log``，
       丢掉既有的历史记录，导致升级后第一次运行对真实媒体库**全量重跑**；
    2. ``base_name`` 在 ``taken`` ⇒ 从 ``-2`` 起找第一个「不在 ``taken`` 且磁盘上也不存在」的名字。
    """
    name = os.path.basename(str(base_name or "").strip()) or "librarycache.log"
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    used = {os.path.basename(str(item)) for item in taken if str(item).strip()}
    if name not in used:
        return name
    index = 1
    while True:
        index += 1
        candidate = f"{stem}-{index}.{ext}" if ext else f"{stem}-{index}"
        if candidate not in used and not os.path.exists(os.path.join(str(log_dir), candidate)):
            return candidate


def read_json_cache(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读 JSON 缓存；文件不存在或不是合法 JSON 时返回 ``{}``。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json_cache(path: str | os.PathLike[str], data: dict[str, Any]) -> None:
    """原子写出 JSON 缓存（临时文件 + ``os.replace``）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_name = handle.name
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=JSON_INDENT)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_json_cache(path: str | os.PathLike[str]) -> bool:
    """日志文件不存在时创建空 ``{}``；返回是否**新建**了文件。"""
    target = Path(path)
    if target.exists():
        return False
    write_json_cache(target, {})
    return True


def remove_entry(path: str | os.PathLike[str], folder: str) -> bool:
    """从日志里删掉某个标题文件夹的条目（``force`` 重跑用）；返回是否真的删了。"""
    data = read_json_cache(path)
    if folder not in data:
        return False
    data.pop(folder, None)
    write_json_cache(path, data)
    return True


def iter_entries(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """把日志里的条目规整成 ``[(folder, entry)]``，忽略非 dict 的脏数据。"""
    items: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            items.append((str(key), value))
    return items


#: ``log_summary`` 的进程缓存：``(路径, mtime_ns, size) -> 摘要``。
#: WebUI 会每几秒轮询一次 ``/api/system/info``，而一个库的重命名日志可能有上万条；
#: 没有缓存就等于每次轮询都把每个库的日志重读一遍。键里带 ``mtime_ns`` 与 ``size``，
#: 文件一改就换键，不会返回旧值。
_SUMMARY_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_SUMMARY_CACHE_LIMIT = 256


def _scan_cache_file(target: Path) -> tuple[int, str | None]:
    """流式数出 ``(顶层「值是对象」的键数量, 最新的 timestamp)``。

    为什么不用 ``json.load``：一个上万文件夹的库，日志本身就有上百 MB，
    而 ``/api/system/info`` 是健康检查级别的接口——为两个数建一棵同样大的对象树不划算。
    这里只做 JSON 的**词法**扫描（跟踪字符串/转义/嵌套深度），内存与文件大小无关。

    口径与 ``iter_entries`` 一致：只数"值是对象"的顶层键（``"bad": "x"`` 不算一条），
    ``timestamp`` 也只从这些条目里取；一个都没有时返回 ``None``，由调用方退回 mtime。
    非法 JSON 不抛异常（数到哪算哪），最坏情况是少报条目，不会让健康检查 500。
    """
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, None

    depth = 0
    in_string = False
    escaped = False
    buf: list[str] = []
    pending_string: str | None = None
    pending_top_value = False
    in_counted_entry = False
    want_stamp = False
    counted = 0
    newest: str | None = None

    for char in text:
        if in_string:
            if escaped:
                buf.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                token = "".join(buf)
                buf = []
                if want_stamp:
                    want_stamp = False
                    if token and (newest is None or token > newest):
                        newest = token
                else:
                    pending_string = token
            else:
                buf.append(char)
            continue

        if char.isspace():
            continue

        if pending_string is not None:
            was_key = char == ":"
            token, pending_string = pending_string, None
            if was_key:
                # 深度 1 上的键 = 媒体库条目；它的值是不是对象，由下一个非空白字符决定。
                if depth == 1:
                    pending_top_value = True
                elif depth == 2 and in_counted_entry and token == "timestamp":
                    want_stamp = True
            # 不是键 ⇒ 这个字符串是某个键的**值**，什么都不用做

        # `:` 与 `,` 是结构标点：它们本身不是"值的第一个字符"，
        # 绝不能走到下面的标量分支去清掉 pending_top_value / want_stamp。
        if char in ":,":
            continue

        if char == '"':
            in_string = True
            escaped = False
            continue

        if char == "{":
            if pending_top_value:
                pending_top_value = False
                counted += 1
                in_counted_entry = True
            depth += 1
            continue
        if char in "[":  # noqa: SIM114 - 数组值的顶层键按 iter_entries 口径不计
            if pending_top_value:
                pending_top_value = False
            depth += 1
            continue
        if char in "}]":
            depth = max(0, depth - 1)
            if depth == 1:
                in_counted_entry = False
            continue
        # 数字 / true / false / null 之类的标量值：顶层键到此确认"不是对象"
        if pending_top_value:
            pending_top_value = False
        if want_stamp:
            want_stamp = False

    return counted, newest


def log_summary(path: str | os.PathLike[str]) -> dict[str, Any]:
    """给 ``GET /api/system/info`` 的 ``rename_logs`` 用：``{exists, entries, size, last_write}``。

    * ``entries`` = 日志**顶层「值是对象」的键数量**（与 ``iter_entries`` 同一口径，
      一个媒体库条目一条记录）；非对象的顶层键（例如有人手工加一行
      ``"_comment": "..."``、或标量/数组值）**不计入**，因为它们不是媒体库条目，
      所以 ``entries`` 可能比文件里的字面键数少；
    * ``last_write`` = 最新条目的 ``timestamp``（本地 ``YYYY-MM-DD HH:MM:SS``，即
      ``datetime.now().strftime`` 的格式）；没有任何带时间戳的条目时退回文件 mtime；
    * 文件不存在（或路径是个目录、stat 失败）⇒ ``{exists: False, entries: 0, size: 0,
      last_write: None}``，**不抛异常**——它是诊断接口的一部分。

    统计走流式扫描（``_scan_cache_file``）并按 ``(路径, mtime_ns, size)`` 缓存，
    轮询不会重复读盘，也不会为上百 MB 的日志建对象树。
    """
    target = Path(path)
    if not target.is_file():
        return {"exists": False, "entries": 0, "size": 0, "last_write": None}
    try:
        stat = target.stat()
    except OSError:
        return {"exists": False, "entries": 0, "size": 0, "last_write": None}

    key = (str(target), stat.st_mtime_ns, stat.st_size)
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None:
        return dict(cached)

    entries, newest = _scan_cache_file(target)
    last_write = newest
    if not isinstance(last_write, str) or not last_write:
        # 没有任何带 timestamp 的条目时退回文件 mtime，仍按本地时间格式输出
        last_write = _format_local(stat.st_mtime)
    summary = {
        "exists": True,
        "entries": int(entries),
        "size": stat.st_size,
        "last_write": last_write,
    }
    if len(_SUMMARY_CACHE) >= _SUMMARY_CACHE_LIMIT:
        _SUMMARY_CACHE.clear()
    _SUMMARY_CACHE[key] = summary
    return dict(summary)


def _format_local(timestamp: float) -> str:
    """把 POSIX 时间戳格式化成 ``YYYY-MM-DD HH:MM:SS``（本地时区）。"""
    from datetime import datetime

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
