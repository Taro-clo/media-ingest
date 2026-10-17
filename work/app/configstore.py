"""配置文件（YAML）与数据库的双向同步：单一真源 + 可移植镜像。

契约真源：``docs/contracts/PROCESSORS.md`` §6。

* **运行时真源 = SQLite**；配置文件是它的镜像，供用户手工编辑与迁移。
* **写出方向**：任何一次成功的 API 写入都在同一请求内把受影响文件**原子**写出
  （``*.yaml.tmp`` → ``os.replace``），并把新 sha256 记进 ``config_files``。
* **读入方向**：后台线程每 ``config_sync_interval_seconds`` 秒比对一次
  mtime + sha256；发现"不是我们写出的那个哈希"⇒ 判定为外部编辑并导入。
  校验失败**不改 DB**，只写一条 ``config_events`` 错误并通过 ``GET /api/config`` 暴露。
* **删除语义**：文件里消失的**非内置**处理器 / 媒体库随导入删除；内置处理器永不删除。
* 导入成功后**回写**一次文件（规范化），避免哈希抖动。
* 全程只有一把 ``_config_lock``：API 写入与后台导入不会交错。

YAML 约定：一律 ``yaml.safe_load``；写出用
``yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)``。
``sort_keys=False`` 是硬要求 —— ``naming.expressions`` 是**有序**求值的。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import yaml

from .db import get_setting, now_iso, set_setting, tx
from .processors import defaults
from .processors.logs import slugify
from .processors.overrides import read_library_override, resolve_override
from .processors.registry import allocate_library_log_file, get_processor
from .processors.spec import normalize_spec, validate_spec

logger = logging.getLogger(__name__)

__all__ = [
    "LIBRARIES_FILE",
    "PROCESSORS_DIR",
    "config_dir",
    "libraries_path",
    "processor_path",
    "snapshot",
    "export_all",
    "export_libraries",
    "export_processor",
    "remove_processor_file",
    "write_libraries_file",
    "import_file",
    "reload",
    "start_watcher",
    "stop_watcher",
    "watcher_running",
    "import_libraries_payload",
    "import_processor_payload",
]

#: 媒体库清单文件名（§6.1）
LIBRARIES_FILE = "libraries.yaml"
#: 处理器目录名（§6.1）
PROCESSORS_DIR = "processors"

#: ``libraries.yaml`` 里每个库的字段顺序（手工编辑友好：按 §6.3 的顺序写）
LIBRARY_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "type",
    "processor_id",
    "enabled",
    "source_dir",
    "target_dir",
    "match_categories",
    "match_tags",
    "match_path_prefixes",
    "orphan_cleanup",
    "permission_mode",
    "log_file",
    # 库级规则覆盖（§3.3）。空对象 = 没有覆盖（完全跟随处理器）。
    "rule_override",
)

#: 配置里出现的旧字段 → 新字段（旧的 ``processor`` 键仍能被读进来）
_LEGACY_LIBRARY_KEYS = {"processor": "processor_id"}

_SCHEMA_VERSION = 2

#: 全局锁：API 写入与后台导入共用（§6.2 的"冲突窗口"）
_config_lock = threading.RLock()

_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()


# ── 路径 ────────────────────────────────────────────────────────────────


def config_dir() -> Path:
    """配置目录（``settings.config_dir``，默认 ``/data/config``）。"""
    from .config import settings

    return Path(settings.config_dir)


def config_path_text(path: Path | str) -> str:
    """把路径渲染成**展示用**的正斜杠文本（Windows 上 ``Path("/data")`` 会打印成 ``\\data``）。"""
    return str(path).replace("\\", "/")


_display_path = config_path_text


def libraries_path() -> Path:
    return config_dir() / LIBRARIES_FILE


def processor_path(processor_id: str) -> Path:
    return config_dir() / PROCESSORS_DIR / f"{processor_id}.yaml"


# ── YAML 读写 ───────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _write_yaml(path: Path, data: Any) -> str:
    """原子写出 YAML（``.tmp`` + ``os.replace``），返回 sha256。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    payload = text.encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(payload)
    os.replace(tmp, path)
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _record_file(path: Path, digest: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO config_files (path, sha256, at) VALUES (?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, at = excluded.at",
            (str(path), digest, now_iso()),
        )


def _recorded_hash(path: Path) -> str | None:
    with tx() as conn:
        row = conn.execute("SELECT sha256 FROM config_files WHERE path = ?", (str(path),)).fetchone()
    return str(row["sha256"]) if row is not None else None


def _forget_file(path: Path) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM config_files WHERE path = ?", (str(path),))


def _record_event(path: Path | str, direction: str, digest: str | None, note: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO config_events (path, direction, sha256, at, note) VALUES (?, ?, ?, ?, ?)",
            (str(path), direction, digest or "", now_iso(), note),
        )


def _touch(imported: bool) -> None:
    set_setting(
        "config_last_import_at" if imported else "config_last_export_at",
        now_iso(),
    )


# ── 由 DB 生成 payload ──────────────────────────────────────────────────


def _library_payload(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM libraries ORDER BY id ASC").fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {}
        for field_name in LIBRARY_FIELDS:
            value = row[field_name] if field_name in row.keys() else None
            if field_name in ("match_categories", "match_tags", "match_path_prefixes"):
                try:
                    value = json.loads(value or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    value = []
            elif field_name == "rule_override":
                # 写进 YAML 的是一个对象（空对象 = 没有覆盖），手工编辑友好。
                # 与 API 一样走 read_library_override：无论这一列当初是怎么写进去的，
                # 出来的形状都一致（模板原文 + 编译结果 + 补齐的 season_group）。
                try:
                    parsed = json.loads(value) if value else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                value = read_library_override(parsed) if isinstance(parsed, dict) else {}
            elif field_name in ("enabled", "orphan_cleanup"):
                value = bool(value)
            entry[field_name] = value
        items.append(entry)
    return items


def _processor_payload(processor_id: str) -> dict[str, Any] | None:
    spec = get_processor(processor_id)
    if spec is None:
        return None
    # 保留声明顺序：schema_version 永远第一（§6.1）
    ordered: dict[str, Any] = {"schema_version": _SCHEMA_VERSION}
    for key, value in spec.items():
        if key == "schema_version":
            continue
        ordered[key] = value
    return ordered


# ── 写出方向（DB → 文件）────────────────────────────────────────────────


def export_libraries() -> Path:
    """把 DB 里的媒体库清单写到 ``libraries.yaml``。"""
    with _config_lock:
        with tx() as conn:
            items = _library_payload(conn)
        target = libraries_path()
        digest = _write_yaml(target, {"schema_version": _SCHEMA_VERSION, "libraries": items})
        _record_file(target, digest)
        _touch(imported=False)
        return target


def export_processor(processor_id: str) -> Path | None:
    """把单个处理器写到 ``processors/<id>.yaml``；不存在则返回 None。"""
    with _config_lock:
        payload = _processor_payload(processor_id)
        if payload is None:
            return None
        target = processor_path(processor_id)
        digest = _write_yaml(target, payload)
        _record_file(target, digest)
        _touch(imported=False)
        return target


def remove_processor_file(processor_id: str) -> None:
    """删掉自定义处理器对应的 YAML 文件。"""
    with _config_lock:
        target = processor_path(processor_id)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("删除配置文件失败：%s", target)
        _forget_file(target)


def write_libraries_file() -> None:
    """API 写库之后的"镜像写出"薄封装（失败只记日志，不影响业务）。"""
    try:
        export_libraries()
    except Exception:  # noqa: BLE001
        logger.exception("写出媒体库配置文件失败。")


def export_all() -> list[str]:
    """把 DB 全量写出（首次启动 v2 与 ``POST /api/config/export`` 用）。"""
    written: list[str] = []
    with _config_lock:
        written.append(str(export_libraries()))
        with tx() as conn:
            rows = conn.execute("SELECT id FROM processors ORDER BY builtin DESC, id ASC").fetchall()
        for row in rows:
            target = export_processor(str(row["id"]))
            if target is not None:
                written.append(str(target))
        # 清掉 DB 里已经不存在的自定义处理器文件
        with tx() as conn:
            known = {str(row["id"]) for row in conn.execute("SELECT id FROM processors").fetchall()}
        directory = config_dir() / PROCESSORS_DIR
        if directory.is_dir():
            for path in directory.glob("*.yaml"):
                if path.stem not in known:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    _forget_file(path)
    return written


# ── 读入方向（文件 → DB）────────────────────────────────────────────────


def _legacy_processor_column(processor_id: str) -> str:
    """v1 的 ``processor`` 列（NOT NULL）——映射成它最接近的内置处理器 id。"""
    return processor_id if processor_id in ("bangumi", "movie") else "hardlink_only"


#: 公开别名：``routers/libraries.py`` 写 v1 兼容列时复用**同一个**映射，避免两处漂移。
legacy_processor_column = _legacy_processor_column


def _derive_type(processor_id: str, explicit: Any) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return {"bangumi": "Bangumi", "movie": "Movie"}.get(processor_id, "Custom")


def import_libraries_payload(payload: Any) -> dict[str, Any]:
    """导入 ``libraries.yaml`` 的内容（已解析的 dict）。返回统计信息。

    在一个事务里替换：逐条 upsert，最后删掉**本次未被触碰**的库（文件里消失 ⇒ 删除）。
    """
    if not isinstance(payload, dict):
        raise ValueError("libraries.yaml 的顶层必须是一个映射（对象）。")
    version = payload.get("schema_version")
    if version != _SCHEMA_VERSION:
        raise ValueError(f"schema_version 必须是 {_SCHEMA_VERSION}，当前是 {version!r}。")
    items = payload.get("libraries")
    if not isinstance(items, list):
        raise ValueError("libraries.yaml 必须包含 libraries 数组。")

    touched: set[int] = set()
    #: 本次 payload 内已经分配掉的日志名（同事务里还没落库，查不到，必须自己带上）
    payload_taken: set[str] = set()
    created = 0
    updated = 0
    with _config_lock, tx() as conn:
        for index, entry in enumerate(items):
            if not isinstance(entry, dict):
                raise ValueError(f"libraries[{index}] 必须是一个对象。")
            item = dict(entry)
            for old, new in _LEGACY_LIBRARY_KEYS.items():
                if old in item and new not in item:
                    item[new] = item.pop(old)
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"libraries[{index}].name 不能为空。")
            name = name.strip()
            processor_id = item.get("processor_id")
            if not isinstance(processor_id, str) or not processor_id:
                raise ValueError(f"libraries[{index}].processor_id 不能为空。")
            row = conn.execute("SELECT id FROM processors WHERE id = ?", (processor_id,)).fetchone()
            if row is None:
                raise ValueError(
                    f"libraries[{index}].processor_id={processor_id!r} 不存在："
                    f"请先在 processors/ 里定义该处理器。"
                )
            source_dir = item.get("source_dir")
            target_dir = item.get("target_dir")
            if not isinstance(source_dir, str) or not source_dir.strip():
                raise ValueError(f"libraries[{index}].source_dir 不能为空。")
            if not isinstance(target_dir, str) or not target_dir.strip():
                raise ValueError(f"libraries[{index}].target_dir 不能为空。")

            def _json_list(value: Any, field_name: str) -> str:
                if value is None:
                    return "[]"
                if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                    raise ValueError(f"libraries[{index}].{field_name} 必须是字符串数组。")
                return json.dumps([v.strip() for v in value if v.strip()], ensure_ascii=False)

            existing_id = item.get("id")
            existing = None
            if isinstance(existing_id, int):
                existing = conn.execute(
                    "SELECT id, log_file, rule_override FROM libraries WHERE id = ?", (existing_id,)
                ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT id, log_file, rule_override FROM libraries WHERE name = ?", (name,)
                ).fetchone()

            log_file = item.get("log_file")
            if not isinstance(log_file, str) or not log_file.strip():
                # 文件里没写 log_file：老库**沿用当前名字**（否则每导入一次就换一次名，
                # 历史记录跟不上，等于对真实媒体库全量重跑）；新库按处理器基名分配唯一名
                # （一个媒体库一份重命名日志，见 registry.allocate_library_log_file）。
                log_file = str(existing["log_file"] or "") if existing is not None else ""
            if not log_file.strip():
                log_file = allocate_library_log_file(
                    processor_id,
                    exclude_library_id=int(existing["id"]) if existing is not None else None,
                    extra_taken=payload_taken,
                )
            log_file = os.path.basename(log_file.strip())
            payload_taken.add(log_file)

            # 库级规则覆盖（§3.3）：文件里**没提**这个键 ⇒ 沿用现值（与 log_file 同一条理由：
            # 手写 YAML 常常只关心某个字段，不该顺手把别的字段清空）；显式写 {} 才是清空。
            # 校验走与 API 完全相同的入口（overrides.resolve_override），避免两套口径。
            override_raw = item.get("rule_override")
            if override_raw is None and "rule_override" not in item:
                kept = str(existing["rule_override"] or "") if existing is not None else ""
                rule_override: str | None = kept or None
            else:
                from .processors.overrides import resolve_override as _resolve_override

                if override_raw is not None and not isinstance(override_raw, dict):
                    raise ValueError(
                        f"libraries[{index}].rule_override 必须是对象（或留空表示不覆盖）。"
                    )
                stored_override, problems = _resolve_override(
                    override_raw, get_processor(processor_id) or {}
                )
                if problems:
                    detail = "；".join(
                        f"{problem['field']}: {problem['message']}" for problem in problems
                    )
                    raise ValueError(f"libraries[{index}].rule_override 不合法 —— {detail}")
                rule_override = stored_override

            values = (
                name,
                _derive_type(processor_id, item.get("type")),
                processor_id,
                _legacy_processor_column(processor_id),
                1 if item.get("enabled", True) else 0,
                source_dir.strip(),
                target_dir.strip(),
                _json_list(item.get("match_categories"), "match_categories"),
                _json_list(item.get("match_tags"), "match_tags"),
                _json_list(item.get("match_path_prefixes"), "match_path_prefixes"),
                1 if item.get("orphan_cleanup", True) else 0,
                item.get("permission_mode"),
                log_file,
                rule_override,
            )
            if existing is None:
                cursor = conn.execute(
                    "INSERT INTO libraries (name, type, processor_id, processor, enabled, source_dir,"
                    " target_dir, match_categories, match_tags, match_path_prefixes, orphan_cleanup,"
                    " permission_mode, log_file, rule_override, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values + (now_iso(), now_iso()),
                )
                touched.add(int(cursor.lastrowid))
                created += 1
            else:
                library_id = int(existing["id"])
                conn.execute(
                    "UPDATE libraries SET name = ?, type = ?, processor_id = ?, processor = ?, enabled = ?,"
                    " source_dir = ?, target_dir = ?, match_categories = ?, match_tags = ?,"
                    " match_path_prefixes = ?, orphan_cleanup = ?, permission_mode = ?, log_file = ?,"
                    " rule_override = ?, updated_at = ? WHERE id = ?",
                    values + (now_iso(), library_id),
                )
                touched.add(library_id)
                updated += 1

        removed = 0
        rows = conn.execute("SELECT id FROM libraries").fetchall()
        for row in rows:
            library_id = int(row["id"])
            if library_id not in touched:
                conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
                removed += 1

    return {"created": created, "updated": updated, "removed": removed}


def import_processor_payload(
    processor_id: str, payload: Any, *, filename_id: str | None = None
) -> dict[str, Any]:
    """导入单个处理器 YAML。``filename_id`` 是文件名推断出的 id（必须与内容一致）。"""
    if not isinstance(payload, dict):
        raise ValueError(f"processors/{processor_id}.yaml 的顶层必须是一个映射（对象）。")
    if filename_id is not None and filename_id != processor_id:
        raise ValueError(
            f"文件名与内容里的 id 不一致：文件名 {filename_id!r}，内容 {processor_id!r}。"
        )
    spec = normalize_spec(dict(payload))
    spec["id"] = processor_id
    errors = validate_spec(spec, require_id=True)
    if errors:
        message = "；".join(f"{item['field']}: {item['message']}" for item in errors)
        raise ValueError(f"处理器 {processor_id} 的规格不合法：{message}")

    existing = get_processor(processor_id)
    builtin = bool(existing.get("builtin")) if existing else processor_id in defaults.builtin_ids()
    spec["builtin"] = builtin
    spec.pop("reference_script", None)  # 兼容旧配置：带来的该键一律忽略，不进入 spec
    if builtin:
        # 内置处理器可以被改规则，但 builtin 标记不可被文件改掉
        if not spec.get("name"):
            spec["name"] = defaults.builtin_spec(processor_id)["name"]
    if not spec.get("log_file"):
        spec["log_file"] = f"{slugify(spec.get('name') or processor_id)}cache.log"
    if not builtin:
        # 手写 YAML 里如果沿用了内置处理器的日志名，同样要换掉（否则两个处理器
        # 共用一个日志文件会互相判定"已处理"，见 registry.avoid_builtin_log_file）。
        from .processors.registry import avoid_builtin_log_file

        avoid_builtin_log_file(spec)

    stamp = now_iso()
    action = "updated" if existing else "created"
    with _config_lock, tx() as conn:
        conn.execute(
            "INSERT INTO processors (id, name, kind, builtin, spec_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET name = excluded.name, kind = excluded.kind,"
            " builtin = excluded.builtin, spec_json = excluded.spec_json, updated_at = excluded.updated_at",
            (
                processor_id,
                spec["name"],
                spec["kind"],
                1 if builtin else 0,
                json.dumps(spec, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
    return {"id": processor_id, "action": action, "builtin": builtin}

def _purge_missing_processors(seen: Iterable[str]) -> int:
    """删掉文件里已经消失**非内置**处理器（内置永不删除）。"""
    keep = set(seen)
    removed = 0
    with _config_lock, tx() as conn:
        rows = conn.execute("SELECT id FROM processors WHERE builtin = 0").fetchall()
        for row in rows:
            processor_id = str(row["id"])
            if processor_id not in keep:
                conn.execute("DELETE FROM processors WHERE id = ?", (processor_id,))
                removed += 1
    return removed


def import_file(path: str | os.PathLike[str], *, force: bool = False) -> dict[str, Any]:
    """从单个文件导入。

    ``force=True`` 时忽略"哈希未变"的快速路径（手工点「重新从磁盘导入」）。
    返回 ``{"ok", "imported", "errors", "path"}``；失败时**不改 DB**，只写一条错误事件。
    """
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "path": str(target), "imported": None, "errors": [f"文件不存在：{target}"]}
    digest = _sha256_file(target)
    if not force and digest is not None and _recorded_hash(target) == digest:
        return {"ok": True, "path": str(target), "imported": None, "errors": [], "unchanged": True}
    try:
        payload = _read_yaml(target)
        if target.name == LIBRARIES_FILE:
            result = import_libraries_payload(payload)
            kind = "libraries"
        else:
            processor_id = target.stem
            content_id = None
            if isinstance(payload, dict):
                raw_id = payload.get("id")
                if isinstance(raw_id, str) and raw_id:
                    content_id = raw_id
            if content_id is not None and content_id != processor_id:
                raise ValueError(
                    f"文件名与内容里的 id 不一致：文件名 {processor_id!r}，内容 {content_id!r}。"
                )
            result = import_processor_payload(processor_id, payload, filename_id=processor_id)
            kind = "processor"
    except Exception as exc:  # noqa: BLE001 - 任何解析/校验失败都不改 DB
        message = str(exc) or exc.__class__.__name__
        logger.warning("导入配置文件失败：%s（%s）", target, message)
        _record_event(target, "import-error", digest, message)
        return {"ok": False, "path": str(target), "imported": None, "errors": [message]}

    _record_event(target, "import", digest, f"{kind} 已导入")
    if digest is not None:
        _record_file(target, digest)
    _touch(imported=True)
    # 导入成功后回写一次（规范化），哈希重新对齐，避免抖动（§6.2 第 3 步）
    try:
        if kind == "libraries":
            export_libraries()
            reexport = []
        else:
            reexport = [str(export_processor(target.stem))]
    except Exception:  # noqa: BLE001
        logger.exception("导入后回写配置失败：%s", target)
        reexport = []
    return {"ok": True, "path": str(target), "imported": {"kind": kind, **result}, "errors": [],
            "rewritten": reexport}


def reload(force: bool = False) -> dict[str, Any]:
    """扫描配置目录并导入所有内容有变化的文件（``force=True`` 时全部重导）。

    顺序固定：**先处理器、后媒体库** —— 媒体库的 ``processor_id`` 必须已经存在。
    """
    directory = config_dir()
    imported: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_processors: list[str] = []

    processor_dir = directory / PROCESSORS_DIR
    if processor_dir.is_dir():
        for path in sorted(processor_dir.glob("*.yaml")):
            seen_processors.append(path.stem)
            result = import_file(path, force=force)
            if result.get("imported") is not None:
                imported.append({"path": str(path), **result["imported"]})
            errors.extend(result.get("errors") or [])

    libraries = libraries_path()
    if libraries.is_file():
        result = import_file(libraries, force=force)
        if result.get("imported") is not None:
            imported.append({"path": str(libraries), **result["imported"]})
        errors.extend(result.get("errors") or [])

    removed = _purge_missing_processors(seen_processors)
    if removed:
        imported.append({"kind": "processor-purge", "removed": removed})
    return {"ok": not errors, "imported": imported, "errors": errors}


def _scan_and_import() -> None:
    """后台线程的一轮：比对每个文件的哈希，变了就导入。"""
    directory = config_dir()
    candidates: list[Path] = []
    libraries = libraries_path()
    if libraries.is_file():
        candidates.append(libraries)
    processor_dir = directory / PROCESSORS_DIR
    if processor_dir.is_dir():
        candidates.extend(sorted(processor_dir.glob("*.yaml")))
    for path in candidates:
        digest = _sha256_file(path)
        if digest is None:
            continue
        if _recorded_hash(path) == digest:
            continue
        result = import_file(path, force=True)
        if result.get("ok"):
            logger.info("配置文件已从磁盘重新导入：%s", path)
        else:
            logger.warning("配置文件导入失败（已记录事件）：%s", path)


def _watcher_loop() -> None:
    while not _watcher_stop.is_set():
        interval = _interval_seconds()
        if interval <= 0:
            # 关闭自动导入：只手工 reload
            _watcher_stop.wait(5.0)
            continue
        try:
            _scan_and_import()
        except Exception:  # noqa: BLE001
            logger.exception("配置文件同步线程出错（已继续运行）。")
        _watcher_stop.wait(interval)


def _interval_seconds() -> float:
    raw = get_setting("config_sync_interval_seconds", "5")
    try:
        value = float(raw if raw is not None else 5)
    except (TypeError, ValueError):
        value = 5.0
    return max(0.0, min(value, 3600.0))


def start_watcher() -> bool:
    """启动后台同步线程（幂等）。``config_sync_interval_seconds=0`` 时线程仍启动，但不导入。"""
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return False
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(target=_watcher_loop, name="mlm-config-watcher", daemon=True)
    _watcher_thread.start()
    return True


def stop_watcher(timeout: float = 5.0) -> bool:
    """停止后台同步线程（幂等）。"""
    global _watcher_thread
    if _watcher_thread is None:
        return False
    _watcher_stop.set()
    _watcher_thread.join(timeout=timeout)
    _watcher_thread = None
    return True


def watcher_running() -> bool:
    return _watcher_thread is not None and _watcher_thread.is_alive()


# ── GET /api/config ─────────────────────────────────────────────────────


def _iter_config_files() -> list[Path]:
    files: list[Path] = []
    libraries = libraries_path()
    if libraries.is_file():
        files.append(libraries)
    processor_dir = config_dir() / PROCESSORS_DIR
    if processor_dir.is_dir():
        files.extend(sorted(processor_dir.glob("*.yaml")))
    return files


def snapshot() -> dict[str, Any]:
    """``GET /api/config`` 的载荷（§7）。"""
    directory = config_dir()
    files: list[dict[str, Any]] = []
    for path in _iter_config_files():
        try:
            stat = path.stat()
            updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
        except OSError:
            continue
        digest = _sha256_file(path)
        files.append(
            {
                "path": _display_path(path),
                "relative": _display_path(path.relative_to(directory)) if directory in path.parents else path.name,
                "exists": True,
                "sha256": digest,
                "in_sync": digest is not None and _recorded_hash(path) == digest,
                "size": stat.st_size,
                "updated_at": updated,
            }
        )
    with tx() as conn:
        # ``config_events`` 是**只追加的历史表**，导入失败过的记录会永远留着。
        # 但 ``errors`` 必须表达"**当前**还没有问题"，否则用户把配置文件改对之后，
        # WebUI 顶部的红色横幅会一直挂着，只能靠人去 DB 删行。
        # 语义 = 最近一次**成功**导入之后发生的导入错误：
        #   * 锚在 ``id`` 而不是时间字符串上——``now_iso()`` 只到毫秒但同毫秒仍可能撞，
        #     而且排障时人看的是先后顺序，不是时间；
        #   * 从来没有成功导入过（``NULL``）⇒ 任何 import-error 都算"当前错误"；
        #   * ``LIMIT 20`` 保留，防爆（错误历史本身有价值，不删行、不忽略）。
        anchor = conn.execute(
            "SELECT MAX(id) AS last_ok FROM config_events WHERE direction = 'import'"
        ).fetchone()
        since = int(anchor["last_ok"]) if anchor and anchor["last_ok"] is not None else 0
        rows = conn.execute(
            "SELECT path, note, at FROM config_events WHERE direction = 'import-error' AND id > ?"
            " ORDER BY id DESC LIMIT 20",
            (since,),
        ).fetchall()
    return {
        "config_dir": _display_path(directory),
        "files": files,
        "last_import_at": get_setting("config_last_import_at"),
        "last_export_at": get_setting("config_last_export_at"),
        "errors": [
            {
                "path": _display_path(row["path"]),
                "message": str(row["note"] or ""),
                "at": str(row["at"] or ""),
            }
            for row in rows
        ],
        "watcher_running": watcher_running(),
        "interval_seconds": _interval_seconds(),
    }
