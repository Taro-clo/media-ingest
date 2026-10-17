"""每库「已执行作品」账本。

建链阶段的"已处理"判据如果只落在**全局** ``hard_links.log``（键 = 源目录 + 作品名 + 体积）里，
就有两个缺陷：

* **同源多库**：两个库指向同一个 ``source_dir`` 时，A 库跑完后 B 库全被
  "文件夹已记录且体积一致，跳过" —— 新库一个链接都不建；
* **同库换目标**：判据里根本没有目标目录，换了目标照样跳过。

本模块把判据搬进数据库，键是 ``(library_id, folder)``，并把 ``source_dir`` /
``target_dir`` 一起存下来参与判断：换库、换源、换目标都会各自独立地重新建链。

与 ``hard_links.log`` 的关系：**日志照写**（沿用既有的日志形态 + 兼容既有工具），
但它不再参与跳过判断。

设计取舍：账本是**作品级**的，**不保证文件级完整性** ——
目标目录里被手动增删的文件不会被发现、也不会被"修复"，除非用户显式删除该作品的记录
（见 ``app/routers/libraries.py`` 的 ``/works`` 接口）。用户对目标目录的修改应当被保留。
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from .db import get_conn, now_iso

__all__ = [
    "target_folder",
    "folder_sizes",
    "list_records",
    "search_records",
    "upsert_many",
    "drop",
    "drop_by_library",
    "folder_stats",
    "safe_target_path",
]


def target_folder(target_dir: str, folder: str) -> str:
    """作品在目标里的目录 —— 与建链阶段 ``os.makedirs(target_dir/folder)`` 的同一约定。"""
    return os.path.join(target_dir, folder)


def _record_dict(row) -> dict[str, Any]:
    return {
        "folder": str(row["folder"]),
        "size": int(row["size"] or 0),
        "linked": int(row["linked"] or 0),
        "source_dir": str(row["source_dir"] or ""),
        "target_dir": str(row["target_dir"] or ""),
        "target_path": str(row["target_path"] or ""),
        "first_at": str(row["first_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def folder_sizes(library_id: int, *, source_dir: str, target_dir: str) -> dict[str, int]:
    """本库「已建链且源/目标都没变」的作品 ⇒ 体积（建链阶段的跳过判据）。

    ``source_dir`` / ``target_dir`` 参与比较是刻意的：换了源或换了目标，旧记录一律不算数
    （这正是「每库作品账本」里"同库换目标仍然跳过"的修复点）。
    """
    rows = get_conn().execute(
        "SELECT folder, size, source_dir, target_dir FROM link_records WHERE library_id=?",
        (int(library_id),),
    ).fetchall()
    return {
        str(row["folder"]): int(row["size"] or 0)
        for row in rows
        if str(row["source_dir"] or "") == source_dir and str(row["target_dir"] or "") == target_dir
    }


def list_records(library_id: int) -> list[dict]:
    """本库全部作品记录（按作品名排序，大小写不敏感）。"""
    rows = get_conn().execute(
        "SELECT * FROM link_records WHERE library_id=? ORDER BY folder COLLATE NOCASE",
        (int(library_id),),
    ).fetchall()
    return [_record_dict(row) for row in rows]


def search_records(
    library_id: int,
    *,
    query: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """按作品名搜索 + 分页，返回 ``(本页记录, 命中总数)``。

    * ``query`` 为空/纯空白 = 不过滤；否则按**子串**匹配作品名，大小写不敏感
      （``LIKE`` 对 ASCII 不敏感，中文没有大小写，两边都对）；
    * ``%`` / ``_`` / ``\\`` 按**字面**处理（``ESCAPE``），所以作品名里的特殊字符
      不会被当成通配符；
    * ``limit=None`` = 不分页（内部用途，如算两本账的差集）；``offset`` 从 0 开始；
    * 排序与 :func:`list_records` 一致（``folder COLLATE NOCASE``），保证翻页稳定。
    """
    where = "library_id=?"
    params: list[Any] = [int(library_id)]
    text = str(query or "").strip()
    if text:
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND folder LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
    conn = get_conn()
    total = int(
        conn.execute(f"SELECT COUNT(*) FROM link_records WHERE {where}", params).fetchone()[0]
    )
    sql = f"SELECT * FROM link_records WHERE {where} ORDER BY folder COLLATE NOCASE"
    window = list(params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        window += [max(1, int(limit)), max(0, int(offset))]
    rows = conn.execute(sql, window).fetchall()
    return [_record_dict(row) for row in rows], total


def upsert_many(
    library_id: int,
    *,
    source_dir: str,
    target_dir: str,
    items: Iterable[tuple[str, int]],
) -> int:
    """把本次**实际建链并写了记录**的作品写进账本（``items`` = ``[(folder, size), ...]``）。

    幂等：同一 ``(library_id, folder)`` 重复写入只刷新体积/目标/时间，不产生第二行。
    """
    stamp = now_iso()
    written = 0
    conn = get_conn()
    with conn:
        for folder, size in items:
            name = str(folder or "").strip()
            if not name:
                continue
            conn.execute(
                """
                INSERT INTO link_records
                  (library_id, folder, source_dir, target_dir, target_path, size, first_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(library_id, folder) DO UPDATE SET
                  source_dir=excluded.source_dir,
                  target_dir=excluded.target_dir,
                  target_path=excluded.target_path,
                  size=excluded.size,
                  updated_at=excluded.updated_at
                """,
                (
                    int(library_id),
                    name,
                    source_dir,
                    target_dir,
                    target_folder(target_dir, name),
                    int(size),
                    stamp,
                    stamp,
                ),
            )
            written += 1
    return written


def drop(library_id: int, folders: Iterable[str]) -> list[dict]:
    """删掉这些作品的记录；返回被删掉的行（接口回执/前端提示用）。"""
    names = [str(name).strip() for name in folders if str(name or "").strip()]
    if not names:
        return []
    conn = get_conn()
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT * FROM link_records WHERE library_id=? AND folder IN ({placeholders})",
        (int(library_id), *names),
    ).fetchall()
    with conn:
        conn.execute(
            f"DELETE FROM link_records WHERE library_id=? AND folder IN ({placeholders})",
            (int(library_id), *names),
        )
    return [_record_dict(row) for row in rows]


def drop_by_library(library_id: int) -> int:
    """库被删除时清掉本库的全部作品记录（不依赖外键，删除动作要看得见）。"""
    conn = get_conn()
    with conn:
        cursor = conn.execute("DELETE FROM link_records WHERE library_id=?", (int(library_id),))
    return int(cursor.rowcount or 0)


def safe_target_path(library_target_dir: str, folder: str) -> str | None:
    """把「作品在目标里的目录」算成绝对路径；**不可信/越界的一律返回 ``None``**。

    这是删除动作唯一的安全闸：只允许删 ``target_dir`` 的**一级子目录**（与建链阶段的约定
    一致）。``folder`` 里出现路径分隔符、``.``/``..``，或结果等于 ``target_dir`` 本身时一律拒绝。
    """
    name = str(folder or "").strip()
    if not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name:
        return None
    base = os.path.normpath(os.path.abspath(str(library_target_dir or "")))
    if not base:
        return None
    candidate = os.path.normpath(os.path.abspath(os.path.join(base, name)))
    if candidate == base or os.path.dirname(candidate) != base:
        return None
    return candidate


def folder_stats(path: str) -> dict[str, Any]:
    """作品的产物统计（删除确认框用）：文件数 / 字节数 / **非硬链接文件数**。

    ``st_nlink < 2`` 的文件就是"不是我们建的硬链接"——通常是用户自己放进来的东西。
    确认框据此给显著警告。符号链接按自身 inode 计，同样会落入
    ``loose_files``：删除动作会连它一起删，提示里说清楚。
    """
    if not os.path.isdir(path):
        return {"exists": False, "files": 0, "bytes": 0, "loose_files": 0}
    files = 0
    total = 0
    loose = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            full = os.path.join(root, name)
            try:
                stat = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            files += 1
            total += int(stat.st_size)
            if int(stat.st_nlink) < 2:
                loose += 1
    return {"exists": True, "files": files, "bytes": total, "loose_files": loose}
