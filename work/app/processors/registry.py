"""处理器注册表：``processors`` 表的读写 + 与配置文件的双向同步入口。

契约真源：``docs/contracts/PROCESSORS.md`` §3、§6.2、§7、§10。

DB 是运行时**唯一真源**；``spec_json`` 存完整 spec，另外把 ``id/name/kind/builtin`` 镜像成列，
方便过滤与展示（两处永远一起写，读的时候以列为准）。

所有写操作都会在**同一请求内**把受影响的 YAML 原子写出（§6.2 的"写出方向"），失败只记
``config_events``，不阻塞 API（配置文件是镜像，丢一次可以手工 export 补回）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from ..db import get_setting, now_iso, tx
from . import defaults
from .logs import slugify, unique_log_file, unique_log_file_name
from .spec import ID_PATTERN, ProcessorSpec, normalize_spec, validate_spec

logger = logging.getLogger(__name__)

__all__ = [
    "RegistryError",
    "ProcessorNotFound",
    "ProcessorProtected",
    "ProcessorInUse",
    "ProcessorInvalid",
    "ensure_builtins",
    "list_processors",
    "get_processor",
    "spec_row",
    "create_processor",
    "update_processor",
    "delete_processor",
    "reset_processor",
    "referencing_libraries",
    "all_log_files",
    "allocated_log_files",
    "log_file_base",
    "allocate_library_log_file",
    "next_log_file",
]


class RegistryError(Exception):
    """注册表错误基类：``status_code`` + 中文 ``detail``，路由直接映射成 HTTP 响应。"""

    status_code = 400

    def __init__(self, detail: str, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class ProcessorNotFound(RegistryError):
    status_code = 404


class ProcessorProtected(RegistryError):
    """内置处理器不可删除。"""

    status_code = 409


class ProcessorInUse(RegistryError):
    """仍被媒体库引用。"""

    status_code = 409


class ProcessorInvalid(RegistryError):
    """spec 不合法或 id 冲突。"""

    status_code = 400


# ── 行 <-> spec ─────────────────────────────────────────────────────────


def _row_to_spec(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["spec_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data = normalize_spec(data)
    # 列是权威（镜像列在写的时候与 spec_json 保持一致）
    data["id"] = row["id"]
    data["name"] = row["name"]
    data["kind"] = row["kind"]
    data["builtin"] = bool(row["builtin"])
    data.pop("reference_script", None)  # 老库行里的遗留键一律不再向上暴露
    return data


def _select(conn: Any, processor_id: str) -> Any:
    return conn.execute("SELECT * FROM processors WHERE id = ?", (processor_id,)).fetchone()


def spec_row(processor_id: str) -> dict[str, Any] | None:
    """取一份 spec（不存在返回 None）。别名 :func:`get_processor`。"""
    return get_processor(processor_id)


# ── 启动：写入三个内置 ──────────────────────────────────────────────────


def ensure_builtins() -> int:
    """``processors`` 表里缺失的内置处理器补上（**不覆盖**已有记录）；返回新增条数。"""
    added = 0
    stamp = now_iso()
    with tx() as conn:
        for processor_id in defaults.builtin_ids():
            if _select(conn, processor_id) is not None:
                continue
            spec = defaults.builtin_spec(processor_id)
            conn.execute(
                "INSERT INTO processors (id, name, kind, builtin, spec_json, created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?, ?)",
                (
                    processor_id,
                    spec["name"],
                    spec["kind"],
                    json.dumps(spec, ensure_ascii=False),
                    stamp,
                    stamp,
                ),
            )
            added += 1
    if added:
        logger.info("已写入 %d 个内置处理器默认规格。", added)
    return added


# ── 读 ──────────────────────────────────────────────────────────────────


def list_processors(*, builtin: bool | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    """列出处理器（内置在前，且**按 :data:`defaults.BUILTIN_IDS` 的展示顺序**；自定义按 id 升序）。"""
    sql = "SELECT * FROM processors"
    clauses: list[str] = []
    params: list[Any] = []
    if builtin is not None:
        clauses.append("builtin = ?")
        params.append(1 if builtin else 0)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY builtin DESC, id ASC"
    with tx() as conn:
        rows = conn.execute(sql, params).fetchall()

    order = {pid: index for index, pid in enumerate(defaults.BUILTIN_IDS)}

    def sort_key(processor_id: str) -> tuple[int, int, str]:
        # 内置：按 BUILTIN_IDS 的固定顺序（界面展示顺序，别让字母序决定产品顺序）
        return (0, order.get(processor_id, len(order)), "") if processor_id in order else (1, 0, processor_id)

    specs = [_row_to_spec(row) for row in rows]
    specs.sort(key=lambda spec: sort_key(str(spec.get("id") or "")))
    return specs


def get_processor(processor_id: str) -> dict[str, Any] | None:
    with tx() as conn:
        row = _select(conn, processor_id)
    return _row_to_spec(row) if row is not None else None


def _require(processor_id: str) -> dict[str, Any]:
    spec = get_processor(processor_id)
    if spec is None:
        raise ProcessorNotFound(f"处理器不存在：{processor_id}")
    return spec


def referencing_libraries(processor_id: str) -> list[dict[str, Any]]:
    """引用了该处理器的媒体库（删除前的 409 提示要用）。"""
    with tx() as conn:
        rows = conn.execute(
            "SELECT id, name FROM libraries WHERE processor_id = ? ORDER BY id ASC",
            (processor_id,),
        ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]


def all_log_files() -> set[str]:
    """所有媒体库当前用到的日志文件名（生成新名时避免撞车）。"""
    with tx() as conn:
        rows = conn.execute("SELECT log_file FROM libraries WHERE log_file IS NOT NULL").fetchall()
    return {str(row["log_file"]) for row in rows if row["log_file"]}


def builtin_log_files() -> set[str]:
    """三个内置处理器登记的日志文件名（自定义处理器**不许**默默占用它们）。"""
    names: set[str] = set()
    for processor_id in defaults.builtin_ids():
        candidate = str(defaults.builtin_spec(processor_id).get("log_file") or "").strip()
        if candidate:
            names.add(candidate)
    return names


def avoid_builtin_log_file(spec: dict[str, Any]) -> dict[str, Any]:
    """自定义处理器不得沿用某个内置处理器的日志文件（PROCESSORS §5.2）。

    为什么必须挡：日志里记的是"这个标题文件夹已经处理过、体积是多少"。两个处理器
    共用一个日志文件时，A 库处理完的文件夹会让 B 库直接跳过（体积一致分支），
    用户看到的是"B 库跑了但什么都没做"——这是个静默的数据正确性问题。
    模板（movie/bangumi）天然带着内置的日志名，所以新建时必须换掉自己的。
    """
    if spec.get("builtin"):
        return spec
    if str(spec.get("log_file") or "").strip() in builtin_log_files():
        spec["log_file"] = next_log_file(str(spec.get("id") or spec.get("name") or "processor"))
    return spec


def next_log_file(name: str) -> str:
    """按 §5.2 生成 ``<slug>cache.log``（撞名追加 ``-2``、``-3``…）。

    用于**给处理器/新库生成一个新名字**（收的是"名字"，会 slugify）；若要给一份
    "已经是完整文件名"的基名去重，用 :func:`unique_log_file_name`。
    """
    from ..config import settings

    return unique_log_file(settings.log_dir, name, taken=allocated_log_files())


def allocated_log_files(
    *,
    exclude_library_id: int | None = None,
    exclude_processor_id: str | None = None,
) -> set[str]:
    """给「一个媒体库」分配日志文件名时算作**已占用**的全部名字。

    = 其它媒体库的 ``log_file`` ∪ 其它处理器登记的 ``log_file``。

    两个排除项是**必须**的：① 改库时不能和自己的旧名字撞（``exclude_library_id``）；
    ② 本处理器自己登记的基名不算占用，否则第一个 bangumi 库会被推成
    ``bangumicache-2.log``，丢掉既有日志（``exclude_processor_id``）。
    """
    taken = all_log_files()
    if exclude_library_id is not None:
        with tx() as conn:
            row = conn.execute(
                "SELECT log_file FROM libraries WHERE id = ?", (int(exclude_library_id),)
            ).fetchone()
        if row is not None and row["log_file"]:
            taken.discard(str(row["log_file"]))
    with tx() as conn:
        rows = conn.execute("SELECT id, spec_json FROM processors").fetchall()
    for row in rows:
        if exclude_processor_id and str(row["id"]) == exclude_processor_id:
            continue
        try:
            spec = json.loads(row["spec_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidate = spec.get("log_file")
        if isinstance(candidate, str) and candidate.strip():
            taken.add(candidate.strip())
    taken.discard("")
    return taken


def log_file_base(processor_id: str, spec: dict[str, Any] | None = None) -> str:
    """该处理器给媒体库用的**日志基名**（完整文件名）。

    ``spec.log_file`` → 内置处理器兜底 ``defaults.builtin_spec(id)["log_file"]``
    （自定义处理器**不能**调它，未知 id 会 ``KeyError``）→ 最后 ``{processor_id}cache.log``。
    """
    pid = (processor_id or "").strip()
    candidate = str((spec or {}).get("log_file") or "").strip()
    if not candidate and pid in defaults.BUILTIN_IDS:
        candidate = str(defaults.builtin_spec(pid).get("log_file") or "").strip()
    return candidate or f"{pid or 'library'}cache.log"


def allocate_library_log_file(
    processor_id: str,
    spec: dict[str, Any] | None = None,
    *,
    exclude_library_id: int | None = None,
    extra_taken: Iterable[str] = (),
) -> str:
    """给**一个媒体库**分配唯一的重命名日志文件名（缺省名 = 处理器基名，撞名 ``-2``）。

    ``extra_taken`` 给同一个事务里**还没落库**的名字用（配置文件批量导入时，前面几条库的
    ``log_file`` 在同事务内查不到，必须由调用方自己带上）。
    """
    from ..config import settings

    taken = allocated_log_files(
        exclude_library_id=exclude_library_id, exclude_processor_id=processor_id
    )
    taken.update(str(item) for item in extra_taken if str(item).strip())
    return unique_log_file_name(settings.log_dir, log_file_base(processor_id, spec), taken=taken)


# ── 写 ──────────────────────────────────────────────────────────────────


def _validate_or_raise(data: dict[str, Any]) -> dict[str, Any]:
    spec = normalize_spec(data)
    errors = validate_spec(spec)
    if errors:
        message = "；".join(f"{item['field']}: {item['message']}" for item in errors)
        raise ProcessorInvalid(f"处理器规格不合法：{message}")
    return spec


def _sync_file(processor_id: str) -> None:
    """把该处理器写出到 ``/data/config/processors/<id>.yaml``（失败不影响 DB 写入）。"""
    try:
        from ..configstore import export_processor

        export_processor(processor_id)
    except Exception:  # noqa: BLE001 - 配置文件是镜像，绝不能因为写文件失败而回滚业务
        logger.exception("写出处理器配置文件失败：%s", processor_id)


def create_processor(data: dict[str, Any]) -> dict[str, Any]:
    """新建自定义处理器（``builtin`` 永远为 False）。"""
    spec = _validate_or_raise(data)
    processor_id = str(spec.get("id") or "")
    if not ID_PATTERN.match(processor_id):
        raise ProcessorInvalid(
            f"处理器 id 不合法：{processor_id!r}，必须匹配 ^[a-z][a-z0-9_-]{{1,31}}$。"
        )
    spec["builtin"] = False
    if not spec.get("log_file") or str(spec["log_file"]).strip() in builtin_log_files():
        # 用 **id** 而不是 name 做 slug：id 由正则保证是 ASCII，而中文名会被
        # ``slugify`` 折叠成通用的 "library"（两个中文命名的处理器会撞在一起）。
        spec["log_file"] = next_log_file(processor_id)
    if get_processor(processor_id) is not None:
        raise ProcessorInvalid(f"处理器 id 已存在：{processor_id}", status_code=409)
    stamp = now_iso()
    with tx() as conn:
        conn.execute(
            "INSERT INTO processors (id, name, kind, builtin, spec_json, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, ?, ?, ?)",
            (
                processor_id,
                spec["name"],
                spec["kind"],
                json.dumps(spec, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
    _sync_file(processor_id)
    return spec


def update_processor(processor_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """全量替换一份 spec（``id`` 不可改；内置处理器允许改规则）。"""
    current = _require(processor_id)
    incoming = dict(data or {})
    incoming_id = incoming.get("id")
    if incoming_id is not None and str(incoming_id) != processor_id:
        raise ProcessorInvalid(f"处理器 id 不可修改（当前 {processor_id}，请求里是 {incoming_id!r}）。")
    incoming["id"] = processor_id
    incoming["builtin"] = bool(current.get("builtin"))
    incoming.pop("reference_script", None)  # 兼容旧客户端/旧配置：带来的该键一律忽略
    if not incoming.get("log_file"):
        incoming["log_file"] = current.get("log_file") or next_log_file(processor_id)
    spec = _validate_or_raise(incoming)
    with tx() as conn:
        conn.execute(
            "UPDATE processors SET name = ?, kind = ?, builtin = ?, spec_json = ?, updated_at = ?"
            " WHERE id = ?",
            (
                spec["name"],
                spec["kind"],
                1 if spec["builtin"] else 0,
                json.dumps(spec, ensure_ascii=False),
                now_iso(),
                processor_id,
            ),
        )
    _sync_file(processor_id)
    return spec


def delete_processor(processor_id: str) -> dict[str, Any]:
    """删除自定义处理器；内置 ⇒ 409；仍被媒体库引用 ⇒ 409（附引用清单）。"""
    current = _require(processor_id)
    if current.get("builtin"):
        raise ProcessorProtected(f"{processor_id} 是内置处理器，不能删除，只能「恢复默认」。")
    used_by = referencing_libraries(processor_id)
    if used_by:
        names = "、".join(f"{item['name']}(#{item['id']})" for item in used_by)
        raise ProcessorInUse(f"仍有媒体库在使用该处理器，无法删除：{names}。请先改掉这些媒体库的处理器。")
    with tx() as conn:
        conn.execute("DELETE FROM processors WHERE id = ?", (processor_id,))
    try:
        from ..configstore import remove_processor_file

        remove_processor_file(processor_id)
    except Exception:  # noqa: BLE001
        logger.exception("删除处理器配置文件失败：%s", processor_id)
    return {"deleted": True, "id": processor_id}


def reset_processor(processor_id: str, preset: str = "default") -> dict[str, Any]:
    """把**内置**处理器重置为 ``default`` 或 ``legacy`` 预设（自定义处理器不支持）。"""
    current = _require(processor_id)
    if not current.get("builtin"):
        raise ProcessorInvalid(
            f"{processor_id} 是自定义处理器，没有可恢复的默认值；如需重置请自己修改规则或删除重建。"
        )
    if preset not in defaults.PRESETS:
        raise ProcessorInvalid(f"未知预设：{preset}（可选：{'、'.join(defaults.PRESETS)}）。")
    spec = defaults.preset_spec(processor_id, preset)
    with tx() as conn:
        conn.execute(
            "UPDATE processors SET name = ?, kind = ?, builtin = 1, spec_json = ?, updated_at = ?"
            " WHERE id = ?",
            (spec["name"], spec["kind"], json.dumps(spec, ensure_ascii=False), now_iso(), processor_id),
        )
    _sync_file(processor_id)
    return spec


def spec_from_template(template: str, processor_id: str, name: str, kind: str | None) -> dict[str, Any]:
    """按 ``POST /api/processors`` 的 ``template`` 拼一份 spec（供路由使用）。"""
    spec = defaults.template_spec(template)
    spec["id"] = processor_id
    spec["name"] = name
    if kind:
        spec["kind"] = kind
    # 模板带着内置处理器的日志名（moviecache.log / bangumicache.log）：自定义处理器
    # 必须换成自己的，否则两个处理器的日志会互相判定"已处理"（见 avoid_builtin_log_file）。
    if not spec.get("log_file") or str(spec["log_file"]).strip() in builtin_log_files():
        spec["log_file"] = next_log_file(processor_id)
    return normalize_spec(spec)


def ensure_custom_log_file(spec: ProcessorSpec | dict[str, Any]) -> str:
    """给一份 spec 兜底生成 ``log_file``（缺省时按库名 slug）。"""
    data = spec.to_dict() if isinstance(spec, ProcessorSpec) else dict(spec)
    if data.get("log_file"):
        return str(data["log_file"])
    name = str(data.get("name") or data.get("id") or "processor")
    return f"{slugify(name)}cache.log"


def config_sync_interval() -> float:
    """配置同步间隔（秒）；0 = 关闭自动导入。"""
    try:
        return float(get_setting("config_sync_interval_seconds", "5") or 5)
    except (TypeError, ValueError):
        return 5.0
