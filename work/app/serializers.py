"""响应序列化（把数据库行翻译成 docs/contracts/ARCHITECTURE.md §5 的 JSON 形状）。

**时间格式是这里唯一容易出错的地方**：数据库内部一律存 UTC ISO 串
（``db.now_iso()``，便于字符串比较与 24h 统计），而契约 §1.4 要求对外是
``YYYY-MM-DD HH:MM:SS`` 的**本地时间**（由容器 ``TZ`` 决定，默认 Asia/Shanghai）。
两者只在这一层互相转换，避免下游任何地方猜时区。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import settings
from .db import get_conn
from .processors import overrides

#: 任务列表/统计里 `recent` 的默认条数。
RECENT_TASKS_LIMIT = 8


def _local_tz():
    """返回配置时区对应的 ``tzinfo``；解析失败退回系统本地时区。"""
    name = (settings.timezone or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            # 容器里缺 tzdata 或用户填了非法值时，不要在响应里抛异常。
            pass
    return datetime.now().astimezone().tzinfo


def format_time(raw: str | None) -> str | None:
    """UTC ISO 串 -> 本地 ``YYYY-MM-DD HH:MM:SS``；``None``/空串/不可解析返回 ``None``。

    不可解析时原样返回，宁可让用户看到原始值，也不要显示一个编造的本地时间。
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_local_tz()).strftime("%Y-%m-%d %H:%M:%S")


def _loads(raw, fallback):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw) if raw else fallback
    except (ValueError, TypeError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


#: 内部 ``kind`` -> 契约取值（§5.0 第 1 条：``hardlink|rename|rename_hardlink``）。
KIND_VALUES = {"hardlink", "rename", "rename_hardlink"}

#: 历史/兼容：早期版本写入过 ``link`` / ``link+rename``。
_LEGACY_KIND = {"link": "hardlink", "link+rename": "rename_hardlink"}

#: 历史/兼容：早期版本写入过 ``qbittorrent`` / ``schedule``。
_LEGACY_TRIGGER = {"qbittorrent": "hook", "schedule": "scan"}


def normalize_kind(kind: str | None) -> str | None:
    """把库里的 ``kind`` 归一到契约取值 ``hardlink|rename|rename_hardlink``。"""
    if not kind:
        return kind
    return _LEGACY_KIND.get(kind, kind)


def normalize_trigger(trigger: str | None) -> str | None:
    """把库里的 ``trigger`` 归一到契约取值 ``hook|manual|retry|scan``。"""
    if not trigger:
        return trigger
    return _LEGACY_TRIGGER.get(trigger, trigger)


def library_stats(library_id: int) -> dict | None:
    """库的最近一次任务摘要（§5.0 第 1 条）。

    **无任务时返回 ``None``**（而不是字段全 null 的对象）：前端可以据此区分
    “这个库从未运行过”和“运行过但不是成功”，少一层猜测。
    """
    row = get_conn().execute(
        "SELECT id, status, kind, trigger, finished_at, error FROM tasks "
        "WHERE library_id=? ORDER BY id DESC LIMIT 1",
        (library_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "last_task_id": int(row["id"]),
        "last_kind": normalize_kind(row["kind"]),
        "last_status": row["status"],
        "last_trigger": normalize_trigger(row["trigger"]),
        "last_finished_at": format_time(row["finished_at"]),
        "last_error": row["error"],
    }


def _processor_view(processor_id: str) -> dict:
    """处理器摘要（名称/类型）；注册表不可用时返回空值，序列化**绝不抛异常**。"""
    try:
        from .processors import registry

        spec = registry.get_processor(processor_id)
    except Exception:  # pragma: no cover - 纯展示信息
        spec = None
    if not spec:
        return {"processor_name": None, "processor_kind": None, "processor_missing": True}
    return {
        "processor_name": spec.get("name"),
        "processor_kind": spec.get("kind"),
        "processor_missing": False,
    }


def library_warnings(row) -> list[str]:
    """§3.1 的 ``warnings``：结构前提不满足时的只读提示（纯静态判断，不改任何文件）。

    自定义库 + 重命名处理器是最容易踩的组合：处理器会按「番剧/电影」的规则去重命名，
    但目录结构（``Season NN`` 子文件夹 / 电影单文件）可能根本不满足，引擎会静默
    ``Skipping folder``。提前把这件事说出来，比让用户看一堆跳过日志好。

    **没有**日志连续性告警：每个媒体库有自己的重命名日志文件
    （``libraries.log_file``），不存在"日志里认不出本库路径"的问题。
    """
    import os

    warnings: list[str] = []
    library_type = (row["type"] or "").strip()
    processor_id = (row["processor_id"] or "").strip()
    processor_kind = _processor_view(processor_id).get("processor_kind")
    target_dir = row["target_dir"]

    if processor_id and processor_kind is None:
        warnings.append(
            f"该库引用的处理器「{processor_id}」不存在：任务会在重命名阶段报错并跳过。"
            f"请到「处理器」页面重新选择一个处理器。"
        )
    elif library_type == "Custom" and processor_kind == "rename":
        warnings.append(
            "自定义库选择了重命名处理器：处理器会按自己的目录规则（accept_dirs / "
            "file_scope / extensions）去识别作品，目录结构不满足时会整段跳过。"
        )
    if processor_kind == "hardlink_only":
        warnings.append("该库只创建硬链接，不执行任何重命名（处理器 kind = hardlink_only）。")
    if not (row["log_file"] or "").strip():
        warnings.append(
            "该库没有设置重命名日志文件名，将按处理器 id 自动生成一个；"
            "所有重命名日志都必须放在数据卷的 logs 目录里。"
        )
    if not bool(row["orphan_cleanup"]):
        warnings.append(
            "已关闭该库的「孤儿目录清理」：源目录里删掉的一级文件夹，其目标硬链接目录会被保留。"
        )
    if not os.path.isdir(target_dir):
        warnings.append(
            f"目标目录 {target_dir} 目前不存在：首次运行任务时会自动创建。"
        )
    return warnings


def library_dict(row) -> dict:
    """``libraries`` 行 -> §3.1 完整对象（以 ``processor_id`` 为准）。"""
    processor_id = (row["processor_id"] or "").strip()
    view = _processor_view(processor_id)
    log_file = (row["log_file"] or "").strip()
    try:
        log_path = settings.library_log_path(log_file) if log_file else None
    except Exception:  # pragma: no cover - 纯展示信息
        log_path = None
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "type": row["type"],
        # 真源是 processor_id（外键 -> processors.id）。
        "processor_id": processor_id,
        "processor_name": view.get("processor_name"),
        "processor_kind": view.get("processor_kind"),
        "processor_missing": bool(view.get("processor_missing")),
        # 兼容字段：= 处理器 id（``processor`` 曾是枚举语义，已由 processor_id 取代）。
        "processor": processor_id,
        "log_file": log_file,
        "log_path": log_path,
        "enabled": bool(row["enabled"]),
        "source_dir": row["source_dir"],
        "target_dir": row["target_dir"],
        "match_categories": _loads(row["match_categories"], []),
        "match_tags": _loads(row["match_tags"], []),
        "match_path_prefixes": _loads(row["match_path_prefixes"], []),
        "orphan_cleanup": bool(row["orphan_cleanup"]),
        "permission_mode": bool(row["permission_mode"]),
        # 本库的规则覆盖。归一化后的形状（template 已编译成 segments），
        # 前端据此回填表单；``{}`` = 完全跟随处理器。
        "rule_override": overrides.read_library_override(row["rule_override"]),
        "created_at": format_time(row["created_at"]),
        "updated_at": format_time(row["updated_at"]),
        "warnings": library_warnings(row),
        "stats": library_stats(int(row["id"])),
    }


def task_dict(row) -> dict:
    """``tasks`` 行 -> §4.1 Task 对象。"""
    return {
        "id": int(row["id"]),
        "library_id": int(row["library_id"]) if row["library_id"] is not None else None,
        "library_name": row["library_name"],
        "kind": normalize_kind(row["kind"]),
        "trigger": normalize_trigger(row["trigger"]),
        "status": row["status"],
        "scope": _loads(row["scope"], {}),
        "payload": _loads(row["payload"], {}),
        "result": _loads(row["result"], {}),
        "error": row["error"],
        "created_at": format_time(row["created_at"]),
        "started_at": format_time(row["started_at"]),
        "finished_at": format_time(row["finished_at"]),
    }


def log_dict(row) -> dict:
    """``task_logs`` 行 -> 日志项 ``{id, task_id, ts, level, message, stream}``。

    ``ts`` 走 :func:`format_time`，与任务对象的 ``created_at`` **同一时区同一格式**
    （``YYYY-MM-DD HH:MM:SS``，容器 ``TZ``）。这一条是 §5.0 冻结的用户可见要求：
    同一屏上"任务创建时间"是本地时间、日志却显示 UTC 原值，会差整整 8 小时，
    用户必然报 bug。``format_time`` 不可解析时原样返回——宁可显示原始值也不编造。

    全项目**没有**对外返回 UTC 原值的字段（``log_continuity`` 已整条删除，见
    PROCESSORS §5.3），配置同步的时间戳同样走 ``format_time``。

    ``task_id`` 照着 docs/contracts/API.md §4.4 的响应示例保留（ARCHITECTURE §5.0 ③-3 只列了必有的五项，
    多这一个字段不会破坏任何客户端，却能让"这份日志属于哪个任务"自明）。
    """
    return {
        "id": int(row["id"]),
        "task_id": int(row["task_id"]),
        "ts": format_time(row["ts"]),
        "level": row["level"],
        "message": row["message"],
        "stream": row["stream"],
    }


def get_library_row(library_id: int):
    """取库行；不存在返回 ``None``。"""
    return get_conn().execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()


def get_task_row(task_id: int):
    """取任务行；不存在返回 ``None``。"""
    return get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
