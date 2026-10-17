"""``/api/system/*``（ARCHITECTURE §5、PROCESSORS §2.1/§6.1/§9）。

三个接口的用途完全不同，别混：

- ``/health``：**无需认证**，给容器 ``HEALTHCHECK`` 用，所以文案与结构必须稳定、
  绝不能抛异常（健康探测只认 HTTP 状态码与 ``status`` 字段）。**固定报 6 项**：
  ``db`` / ``data_dir`` / ``downloads_readable`` / ``library_writable`` /
  ``processors``（三个内置处理器确实在注册表里）/ ``config``（配置目录可写）。
- ``/info``：给人看的诊断信息——路径映射、挂载、``st_dev``、处理器与配置状态。

``/health`` 里额外报出**文件系统编码降级**：硬链接日志是 ``open(LOG_FILE, 'r')``，
**没有指定 encoding**，跟随 locale。中文日志在非 UTF-8
locale 下会直接抛 ``UnicodeDecodeError``。容器里已设 ``LANG=C.UTF-8`` /
``PYTHONUTF8=1``，但一旦有人改了 env，这里要能立刻看出来。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from .. import paths, security
from ..config import settings
from ..db import get_conn, get_setting, list_setting
from ..processors.defaults import BUILTIN_IDS
from ..serializers import format_time

logger = logging.getLogger("app.system")

router = APIRouter(prefix="/api/system", tags=["system"])

#: 服务标识与镜像/容器名（§9 命名约定，全项目统一）。
SERVICE_NAME = "media-ingest"
CONTAINER_NAME = "media-ingest"

#: ``/proc/mounts`` 里我们要展示的**固定**容器挂载点：``/data`` 与配置目录 ``/data/config``
#: 来自 §2 冻结布局。
#:
#: **媒体根不写死**：容器里没有 ``/downloads`` 与 ``/library``，写死会让 ``/api/system/info``
#: 的 ``mounts`` 报两个 ``exists: false`` 的幽灵挂载点。真实挂载点由「两个白名单设置项的
#: 最近已存在祖先」决定，故按请求推导，见 :func:`mount_points`。
MOUNT_POINTS = ("/data", "/data/config")


def mount_points() -> tuple[str, ...]:
    """当前部署下要展示的容器挂载点：固定项 + 两个白名单根的最近已存在祖先。

    媒体根取「最近已存在祖先」而不是字符串本身：``/vol1/1000/Library`` 可能尚未创建
    （首次运行会创建），而挂载表里只有真实挂载点。去重且保序（固定项在前）。
    """
    points = list(MOUNT_POINTS)
    for root in (
        *list_setting("allowed_source_roots", [settings.default_source_root]),
        *list_setting("allowed_target_roots", [settings.default_target_root]),
    ):
        if not root:
            continue
        anchor = paths.existing_ancestor(root) or root
        if anchor not in points:
            points.append(anchor)
    return tuple(points)


def _writable(path: str) -> tuple[bool, str]:
    """实测目录可写：真的建一个临时文件再删掉。

    不用 ``os.access(W_OK)``：root 下它恒为真，而本应用明确以非 root 运行，
    这里要的是"现在真的能写"这个事实，不是权限位的推断。
    目录不存在时退到最近的已存在祖先上探测（「首次运行会创建」是正常状态，不该报红）。
    """
    probe_dir = path
    if not os.path.isdir(probe_dir):
        probe_dir = paths.existing_ancestor(probe_dir)
        if not probe_dir or not os.path.isdir(probe_dir):
            return False, f"{path} 及其祖先目录都不存在。"
        note = f"{path} 尚不存在（首次运行时会创建），其最近已存在的祖先 {probe_dir} "
    else:
        note = f"{path} "
    try:
        fd, tmp = tempfile.mkstemp(prefix=".mlm-health-", dir=probe_dir)
    except OSError as exc:
        return False, note + f"不可写：{exc.strerror or exc}。"
    try:
        os.close(fd)
        os.unlink(tmp)
    except OSError:  # pragma: no cover - 建得出来就删得掉
        pass
    return True, note + "可写。"


def _readable(path: str) -> tuple[bool, str]:
    """目录存在且能列出一级条目。"""
    if not os.path.isdir(path):
        return False, f"{path} 不存在或不是目录。"
    try:
        with os.scandir(path):
            pass
    except OSError as exc:
        return False, f"{path} 不可读：{exc.strerror or exc}。"
    return True, f"{path} 可读。"


def _filesystem_encoding_warning() -> str | None:
    """文件系统编码不是 UTF-8 时的降级说明（见模块 docstring）。"""
    encoding = (sys.getfilesystemencoding() or "").lower()
    if encoding.replace("-", "") in ("utf8", "utf8mb4"):
        return None
    return (
        f"当前文件系统编码是 {sys.getfilesystemencoding()}，不是 UTF-8："
        f"硬链接实现读取日志时未指定 encoding（跟随系统 locale），"
        f"中文文件夹名会抛 UnicodeDecodeError。请确认容器环境变量 "
        f"LANG=C.UTF-8、PYTHONUTF8=1、PYTHONIOENCODING=utf-8 都在。"
    )


def health_checks() -> tuple[dict, dict, list[str]]:
    """返回 ``(checks, details, warnings)``；checks 恒为布尔值，细节放 details。"""
    checks: dict[str, bool] = {}
    details: dict[str, str] = {}
    warnings: list[str] = []

    # db
    try:
        row = get_conn().execute("SELECT 1 AS ok").fetchone()
        checks["db"] = bool(row and row["ok"] == 1)
        details["db"] = (
            f"数据库可读：{settings.db_path}"
            if checks["db"]
            else f"数据库查询返回异常结果：{settings.db_path}"
        )
    except Exception as exc:  # 探测接口绝不抛异常
        checks["db"] = False
        details["db"] = f"数据库不可用：{exc}"
        logger.warning("健康检查：数据库不可用：%s", exc)

    # processors：三个内置处理器确实能从注册表加载出来（不能写死 True）
    try:
        from ..processors import registry

        rows = registry.list_processors()
        loaded = {str(row.get("id") or "") for row in rows}
        present = [pid for pid in BUILTIN_IDS if pid in loaded]
        absent = [pid for pid in BUILTIN_IDS if pid not in loaded]
        checks["processors"] = not absent
        if not absent:
            details["processors"] = (
                f"已加载 {len(loaded)} 个处理器，三个内置处理器齐备"
                f"（{'、'.join(BUILTIN_IDS)}）。"
            )
        else:
            details["processors"] = (
                f"内置处理器缺失：{'、'.join(absent)}（应有 {'、'.join(BUILTIN_IDS)}）。"
                f"请检查数据卷是否可写，或调用 POST /api/processors/{absent[0]}/reset 重置。"
            )
    except Exception as exc:  # 探测接口绝不抛异常
        checks["processors"] = False
        details["processors"] = f"处理器注册表不可用：{exc}"
        logger.warning("健康检查：处理器注册表不可用：%s", exc)

    # config：配置目录可写（外部编辑导入 + API 写出都要它）
    ok, message = _writable(settings.config_dir)
    checks["config"] = ok
    details["config"] = message
    if ok:
        details["config"] += f" 配置目录：{settings.config_dir}。"

    # data_dir
    ok, message = _writable(settings.data_dir)
    checks["data_dir"] = ok
    details["data_dir"] = message

    # downloads_readable：取 allowed_source_roots 的第一个（缺省值来自 settings.default_source_root）
    source_roots = list_setting("allowed_source_roots", [settings.default_source_root])
    source_probe = source_roots[0] if source_roots else settings.default_source_root
    ok, message = _readable(source_probe)
    checks["downloads_readable"] = ok
    details["downloads_readable"] = message
    if ok:
        # 源子树只读**不是结构性保证**（单挂载形态下 /downloads:ro 已退场），
        # 现在由「代码层不写源目录 + 运行前后逐文件核对」保证；挂载恰好是只读时只是加固。
        mount = _mount_for(source_probe)
        if mount and mount.get("read_only"):
            details["downloads_readable"] += " 该挂载点是只读（额外的加固，v2 不再依赖它保证源目录不被写）。"

    # library_writable
    target_roots = list_setting("allowed_target_roots", [settings.default_target_root])
    target_probe = target_roots[0] if target_roots else settings.default_target_root
    ok, message = _writable(target_probe)
    checks["library_writable"] = ok
    details["library_writable"] = message

    encoding_warning = _filesystem_encoding_warning()
    if encoding_warning:
        warnings.append(encoding_warning)

    return checks, details, warnings


def _mounts_table() -> dict[str, dict]:
    """解析 ``/proc/mounts``，返回 ``挂载点 -> {device,fs_type,options}``。

    非 Linux（本地开发）下没有这个文件，返回空 dict —— 调用方据此把
    ``device``/``fs_type``/``options`` 留空，但仍然给出 ``st_dev``。
    """
    table: dict[str, dict] = {}
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                device, mount_point, fs_type, options = parts[0], parts[1], parts[2], parts[3]
                table[mount_point] = {
                    "device": device,
                    "fs_type": fs_type,
                    "options": options,
                }
    except OSError:
        return {}
    return table


def _mount_for(path: str) -> dict | None:
    """取 ``path`` 自身的挂载信息；只认精确挂载点（不做最长前缀回溯，避免误导）。"""
    return _mounts_table().get(path.rstrip("/") or "/")


def _st_dev(path: str) -> int | None:
    """取 ``path``（或最近已存在祖先）的 ``st_dev``；取不到返回 ``None``。"""
    probe = path if os.path.exists(path) else paths.existing_ancestor(path)
    if not probe:
        return None
    try:
        return int(os.stat(probe).st_dev)
    except OSError:
        return None


def mounts_view() -> list[dict]:
    """按 §5.0 的形状给出每个受关注挂载点的信息（受关注集合见 :func:`mount_points`）。"""
    table = _mounts_table()
    result = []
    for mount_point in mount_points():
        info = table.get(mount_point, {})
        options = info.get("options", "") or ""
        # "ro" 必须是选项里的独立一项：/proc/mounts 的选项是逗号分隔，
        # 直接 `"ro" in options` 会被 "errors=remount-ro" 这种值误判。
        read_only = "ro" in [item for item in options.split(",") if item]
        result.append(
            {
                "container_path": mount_point,
                "host_path": paths.to_host(mount_point),
                "device": info.get("device", ""),
                "fs_type": info.get("fs_type", ""),
                "options": options,
                "st_dev": _st_dev(mount_point),
                "exists": os.path.exists(mount_point),
                "read_only": bool(read_only),
            }
        )
    return result


def _same_filesystem_view() -> dict:
    """源/目标之间能否创建硬链接（级联判据，附实际参与比较的路径）。

    键名不变（`docs/contracts/API.md` 与前端已在用），但 ``downloads_vs_library`` 现在是**三态**：
    ``true`` = 已实证（挂载 ID 或 st_dev 预筛通过时也返回 true，message 会注明「未实证」）、
    ``false`` = 明确不可用（跨挂载一定 EXDEV）、``null`` = 无法判定。

    **这里刻意不做写入型实证探测**（``probe=False``）：``/api/system/info`` 是只读展示接口、
    会被 UI 反复轮询，不适合每次都往目标目录写一个临时链接再删掉；预筛（挂载 ID → st_dev）
    足够给出展示结论，真正的实证由 ``POST /api/libraries/.../validate?probe=true`` 与运行前预检做。
    """
    source_roots = list_setting("allowed_source_roots", [settings.default_source_root])
    target_roots = list_setting("allowed_target_roots", [settings.default_target_root])
    source_path = source_roots[0] if source_roots else settings.default_source_root
    target_path = target_roots[0] if target_roots else settings.default_target_root
    source_dev = _st_dev(source_path)
    target_dev = _st_dev(target_path)
    ok, message = paths.check_hardlink_support(source_path, target_path, probe=False)
    return {
        "source_path": source_path,
        "target_path": target_path,
        "source_dev": source_dev,
        "target_dev": target_dev,
        "downloads_vs_library": ok,
        # 和 paths.check_hardlink_support 的判据必须一致：都取"已存在的最深祖先"，
        # 否则会出现"info 说同一个盘、运行却报跨挂载"这种最招人烦的矛盾。
        "message": message,
    }


def _libraries_for_info() -> list[dict]:
    """媒体库摘要（id/名称/源目录/处理器），供 ``/info`` 展示。"""
    rows = get_conn().execute(
        "SELECT id, name, source_dir, target_dir, processor_id, enabled"
        " FROM libraries ORDER BY id ASC"
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": row["name"],
            "source_dir": row["source_dir"],
            "target_dir": row["target_dir"],
            "processor_id": row["processor_id"],
            "enabled": bool(row["enabled"]),
        }
        for row in rows
    ]


def _rename_logs_for_info() -> list[dict]:
    """逐库一条的重命名日志摘要（`API.md` §5.6 / `PROCESSORS` §5）。

    契约形状（**六键，不多不少**——前端按它渲染「重命名日志」表格）::

        [{"library_id": int, "library_name": str, "log_file": str | None,
          "exists": bool, "entries": int, "last_write": str | None}]

    * 与 ``libraries`` **同一个顺序**（按 id 升序），前端可以按下标对齐两张表；
    * ``entries`` = 该库日志的**顶层键计数**，``last_write`` = 最新条目的时间
      （``YYYY-MM-DD HH:MM:SS`` 本地时间，与 ``GET /api/config`` 的 ``updated_at`` 同格式），
      两者都由 ``logs.log_summary()`` 给出（含"读盘统计带进程缓存"的实现，见该函数）；
    * 文件不存在 ⇒ ``exists=false`` / ``entries=0`` / ``last_write=null``；
    * ``log_file`` 为空（理论上只出现在遗留行还没回填的瞬间）时**不碰文件系统**，
      直接返回空摘要——否则会把空文件名拼成日志**目录**。
    """
    from ..processors import logs as processor_logs

    rows = get_conn().execute(
        "SELECT id, name, log_file FROM libraries ORDER BY id ASC"
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        log_file = str(row["log_file"] or "").strip() or None
        summary = (
            processor_logs.log_summary(processor_logs.library_log_path(log_file))
            if log_file
            else {"exists": False, "entries": 0, "last_write": None}
        )
        items.append(
            {
                "library_id": int(row["id"]),
                "library_name": row["name"],
                "log_file": log_file,
                "exists": bool(summary.get("exists")),
                "entries": int(summary.get("entries") or 0),
                "last_write": summary.get("last_write"),
            }
        )
    return items


@router.get("/health")
def health() -> dict:
    """容器健康检查（**无需认证**）。"""
    checks, details, warnings = health_checks()
    status = "ok" if all(checks.values()) and not warnings else "degraded"
    return {
        "status": status,
        "checks": checks,
        "details": details,
        "warnings": warnings,
        "version": _version(),
        "time": format_time(datetime.now(timezone.utc).isoformat()),
    }


def _version() -> str:
    from .. import __version__

    return __version__


@router.get("/info")
def info(_: str = Depends(security.require_user)) -> dict:
    """诊断信息（路径映射 / 挂载 / 处理器 / 配置）。"""
    from ..processors import registry
    from ..queue import queue
    from ..scanner import scanner

    path_map = dict(settings.host_path_map)
    host_to_container = {host: container for container, host in path_map.items()}
    processor_rows = registry.list_processors()
    return {
        "version": _version(),
        "service": SERVICE_NAME,
        "container_name": CONTAINER_NAME,
        "timezone": settings.timezone,
        "data_dir": settings.data_dir,
        "log_dir": settings.log_dir,
        "config_dir": settings.config_dir,
        # §5.0 第 6 条：path_map 是对象，两个方向都给，前端不用自己反转。
        "path_map": {"container_to_host": path_map, "host_to_container": host_to_container},
        "ports": {"container": settings.port, "host": settings.port, "protocol": "tcp"},
        "mounts": mounts_view(),
        "same_filesystem": _same_filesystem_view(),
        "processors": {
            "total": len(processor_rows),
            "builtin_ids": list(BUILTIN_IDS),
            "items": [
                {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "kind": row.get("kind"),
                    "builtin": bool(row.get("builtin")),
                }
                for row in processor_rows
            ],
        },
        "libraries": _libraries_for_info(),
        # §5.7：逐库一条的重命名日志摘要（WebUI「重命名日志」表格的唯一数据源）。
        "rename_logs": _rename_logs_for_info(),
        "log_level": (get_setting("log_level", "INFO") or "INFO"),
        "queue": {
            "running_task_id": queue.running_task_id,
            "queued_count": queue.queued_count(),
        },
        "scan": scanner.status(),
        "config": _config_view(),
        "encoding": {
            "filesystem": sys.getfilesystemencoding(),
            "stdout": (getattr(sys.stdout, "encoding", None) or ""),
            "utf8_mode": bool(getattr(sys.flags, "utf8_mode", 0)),
            "warning": _filesystem_encoding_warning(),
        },
        "disk": _disk_view(),
    }


def _config_view() -> dict:
    """配置文件同步状态（PROCESSORS §6.2/§7）；失败不影响 ``/info`` 其余字段。"""
    try:
        from .. import configstore

        return configstore.snapshot()
    except Exception as exc:  # 诊断接口不抛异常
        logger.warning("读取配置状态失败：%s", exc)
        return {
            "config_dir": settings.config_dir,
            "files": [],
            "last_import_at": None,
            "last_export_at": None,
            "errors": [{"path": "", "message": f"读取配置状态失败：{exc}", "at": None}],
        }


def _disk_view() -> dict:
    """数据卷/媒体库所在文件系统的剩余空间（``shutil.disk_usage``）。"""
    result: dict = {}
    for key, path in (
        ("data", settings.data_dir),
        ("library", (list_setting("allowed_target_roots", [settings.default_target_root]) or [settings.default_target_root])[0]),
    ):
        probe = path if os.path.exists(path) else paths.existing_ancestor(path)
        if not probe:
            result[key] = None
            continue
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            result[key] = None
            continue
        result[key] = {
            "path": probe,
            "total": int(usage.total),
            "used": int(usage.used),
            "free": int(usage.free),
        }
    return result


__all__ = ["router", "health_checks"]
