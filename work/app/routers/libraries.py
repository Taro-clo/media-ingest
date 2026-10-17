"""``/api/libraries/*``（ARCHITECTURE §5.1 + PROCESSORS §6.3/§7）。

这张表的形状由**处理器**驱动（不是写死的 bangumi/movie/custom 枚举）：

- ``processor_id`` 是外键，必须存在于 ``processors`` 表；缺失/不存在 → **422**（字段级中文错误）；
- ``log_file`` 只允许文件名，缺省由注册表生成 ``<slug>cache.log``（所有重命名日志都在
  ``settings.log_dir``，PROCESSORS §5 要求同一目录）；
- ``type`` 退化为自由标签（展示用），缺省按处理器 kind 派生；
- 每次写库成功后**顺手把配置导出成 YAML**（PROCESSORS §6.2：同一请求内原子写出并记录 sha256），
  保证"文件是数据库的镜像"；导出失败只记日志，不让 API 失败。

CRUD 之外的动作接口语义不同，别混淆：
- ``POST /{id}/validate``：**只读预检**，不落库、不建目录、不建硬链接
  （除了一次可选的 os.link 实证探测，它会自建自删临时文件）；
- ``POST /{id}/run``：把任务丢进队列，202 + Task 对象；
- ``GET /{id}/works`` / ``POST /{id}/works/delete`` / ``POST /{id}/works/rename``：
  **作品级**操作，作用于 ``link_records`` 账本 + 本库重命名缓存。删除是破坏性的
  （连带删除目标目录里的整个作品文件夹），因此必须 ``dry_run`` 预览 + ``confirm`` 确认。
"""

from __future__ import annotations

import json
import logging
import os
import shutil

from fastapi import APIRouter, Body, Depends, Query

from .. import configstore, paths, security
from ..api_models import LibraryInput, RunRequest, WorkDeleteRequest, WorkSelection
from ..config import settings
from ..db import get_conn, list_setting, now_iso
from ..link_records import (
    drop as drop_link_records,
    drop_by_library as drop_link_records_by_library,
    folder_stats,
    safe_target_path,
    search_records as search_link_records,
    target_folder,
)
from ..processors import logs as processor_logs, overrides, registry
from ..queue import TaskSpec, queue
from ..serializers import format_time, get_library_row, library_dict, task_dict
from .common import (
    bad_request,
    clamp_limit,
    clamp_offset,
    conflict,
    not_found,
    unprocessable,
    unprocessable_many,
)

logger = logging.getLogger("app.libraries")

router = APIRouter(prefix="/api/libraries", tags=["libraries"])

#: §3.2 的"类型标签 -> 说明"，只用于错误文案与缺省 type 派生（不再是硬校验表）。
TYPE_LABELS = {"Bangumi": "番剧库", "Movie": "电影库", "Custom": "自定义库"}

#: 内置处理器 -> 旧库的 ``type`` 取值（迁移后的老库要保持原来的显示标签）。
BUILTIN_TYPE_LABELS = {"bangumi": "Bangumi", "movie": "Movie", "hardlink_only": "Custom"}


def _allowed_roots() -> tuple[list[str], list[str]]:
    """读白名单根（settings 里的 JSON 数组），并做一次归一化。

    回退值取自 ``settings.default_source_root`` / ``default_target_root``（唯一真源：
    单挂载形态下容器路径 = 宿主路径，不要填 ``/downloads``、``/library`` 这类容器内路径）。
    """
    source_roots = [
        paths.normalize(r)
        for r in list_setting("allowed_source_roots", [settings.default_source_root])
    ]
    target_roots = [
        paths.normalize(r)
        for r in list_setting("allowed_target_roots", [settings.default_target_root])
    ]
    return (
        source_roots or [settings.default_source_root],
        target_roots or [settings.default_target_root],
    )


def _library_path(raw: str) -> str:
    """把用户输入的路径归一化为**容器路径**（§5.1 冻结要求）。

    宿主路径（``/vol1/1000/Library/Bangumi``）会被 ``to_container`` 按 HOST_PATH_MAP
    反查成容器路径（``/library/Bangumi``）；已经是容器路径则原样保留。
    存进 DB 的必须是容器路径，否则硬链接输出解析、白名单校验、引擎的日志记录
    三者的路径都对不上。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    return paths.normalize(paths.to_container(text))


def _resolve_processor(processor_id: str) -> dict:
    """取处理器 spec；不存在或未提供 → 422 字段级错误（§7）。"""
    pid = (processor_id or "").strip()
    if not pid:
        raise unprocessable(
            "processor_id",
            "必须选择一个处理器（processor_id）。可先用 GET /api/processors 列出可用处理器。",
        )
    spec = registry.get_processor(pid)
    if spec is None:
        existing = [str(row.get("id")) for row in registry.list_processors()]
        raise unprocessable(
            "processor_id",
            f"处理器 {pid} 不存在。可用处理器：{'、'.join(existing) or '（无）'}。",
        )
    return spec


def _resolve_log_file(
    raw: str | None,
    *,
    processor_id: str,
    spec: dict | None = None,
    exclude_library_id: int | None = None,
) -> str:
    """确定重命名日志文件名（只允许文件名，落在 ``settings.log_dir``）。

    **一个媒体库一份日志**（PROCESSORS §5.2）：日志里记的是「这个标题文件夹已经
    处理过、体积是多少」，两个库共用一个文件时，A 库处理完的文件夹会让 B 库走「体积一致 ⇒
    已处理」分支被**静默跳过**（用户看到的是「B 库跑了但什么都没做」）。

    因此缺省（调用方没给 ``log_file``）时**一律**为这个库分配唯一名，基名 = 处理器 spec 的
    ``log_file``（空则 ``{processor_id}cache.log``），去重集合 = 其它库 ∪ 其它处理器登记名
    （**排除本处理器自己**，见 ``registry.allocated_log_files``）⇒ 第一个 bangumi 库仍然是
    ``bangumicache.log``，第二个得 ``bangumicache-2.log``。

    显式给的名字与别人撞车 → 422 字段级；路径分隔符 / 非 ``.log`` 结尾的校验保持不变。
    """
    text = (raw or "").strip()
    if text:
        if os.path.basename(text) != text or "/" in text or "\\" in text:
            raise unprocessable(
                "log_file",
                f"日志文件名只能是文件名，不能包含路径分隔符：{raw}。"
                f"所有日志都固定在数据卷的《{settings.log_dir}》目录下。",
            )
        if not text.lower().endswith(".log"):
            raise unprocessable(
                "log_file", f"日志文件名必须以 .log 结尾：{raw}（例如 {processor_id}cache.log）。"
            )
        if text in registry.allocated_log_files(
            exclude_library_id=exclude_library_id, exclude_processor_id=processor_id
        ):
            raise unprocessable(
                "log_file",
                f"日志文件名 {text} 已被其它媒体库或处理器占用；请换一个名字，"
                f"或留空由系统自动分配（下一个可用名："
                f"{registry.allocate_library_log_file(processor_id, spec, exclude_library_id=exclude_library_id)}）。",
            )
        return text
    return registry.allocate_library_log_file(
        processor_id, spec, exclude_library_id=exclude_library_id
    )


def _resolve_rule_override(raw, spec: dict) -> str | None:
    """校验并归一化本库的规则覆盖，返回写进 ``libraries.rule_override`` 的 JSON 文本。

    库可以只覆盖自己想改的那几项，其余沿用处理器。真正的校验逻辑
    在 :func:`app.processors.overrides.resolve_override`（与配置文件导入共用同一入口），
    这里只把 ``(field, message)`` 翻成 HTTP 422。

    返回 ``None`` 表示"没有覆盖"（空对象、什么都没填、或者本来就是 None）。
    """
    stored, problems = overrides.resolve_override(raw, spec)
    if problems:
        raise unprocessable_many(
            [
                (str(item.get("field") or "rule_override"), str(item.get("message") or ""))
                for item in problems
            ]
        )
    return stored


def _derive_type(raw: str | None, spec: dict) -> str:
    """缺省 type：内置处理器沿用旧的 Bangumi/Movie/Custom 取值，自定义处理器用它的 name。

    ``type`` 只是**纯展示标签**（PROCESSORS §6.3），因此这里绝不校验，只求"看起来对"。
    """
    text = (raw or "").strip()
    if text:
        return text
    pid = str(spec.get("id") or "")
    if pid in BUILTIN_TYPE_LABELS:
        return BUILTIN_TYPE_LABELS[pid]
    if str(spec.get("kind")) == "hardlink_only":
        return "Custom"
    return str(spec.get("name") or pid or "Custom")


def _export_config() -> None:
    """把当前配置导出成 YAML（PROCESSORS §6.2 的"写出方向"）；失败只记日志。"""
    try:
        configstore.export_all()
    except Exception as exc:  # 配置导出失败不该让写操作回滚
        logger.warning("配置导出失败（数据库已更新）：%s", exc)


def _validate_paths(source_dir: str, target_dir: str, *, probe: bool) -> dict:
    """路径四连检：白名单 -> 相同/包含 -> 硬链接可用性级联 ->（可选）os.link 实证。

    返回预检结果 dict；**不抛异常**，把失败原因作为 ``ok=false`` 返回，
    这样 WebUI 的"校验"按钮能一次把全部问题列出来，而不是修一个报一个。

    ``same_filesystem`` 是**三态**（``true``/``false``/``null``）：``false`` 才写
    ``errors``（明确建不出硬链接），``null``（判不出来）只写 ``warnings`` 并放行。
    """
    result: dict = {
        "ok": True,
        "source_dir": source_dir,
        "target_dir": target_dir,
        "source_exists": False,
        "target_exists": False,
        "same_filesystem": None,
        "probe": None,
        "errors": [],
        "warnings": [],
    }
    source_roots, target_roots = _allowed_roots()
    try:
        paths.assert_allowed(source_dir, source_roots, "源目录")
    except ValueError as exc:
        result["ok"] = False
        result["errors"].append(str(exc))
    try:
        paths.assert_allowed(target_dir, target_roots, "目标目录")
    except ValueError as exc:
        result["ok"] = False
        result["errors"].append(str(exc))

    ok_mounts, mount_error = paths.check_cross_mounts(source_dir, target_dir)
    if not ok_mounts:
        result["ok"] = False
        result["errors"].append(mount_error)

    result["source_exists"] = os.path.isdir(source_dir)
    result["target_exists"] = os.path.isdir(target_dir)
    if not result["source_exists"]:
        # 建链阶段对源目录调 os.listdir，源目录不存在会直接抛 FileNotFoundError，
        # 所以这不是"首次运行自动创建"能兜住的，必须提前拦。
        result["ok"] = False
        result["errors"].append(
            f"源目录 {source_dir} 不存在或不是目录：建链阶段会在此处抛 FileNotFoundError，"
            f"请先在 qBittorrent 里完成一次下载或修正挂载。"
        )
    if not result["target_exists"]:
        result["warnings"].append(
            f"目标目录 {target_dir} 目前不存在：运行任务时由建链阶段自动创建。"
        )

    # 级联判据：实证探针（主判据）→ 挂载 ID → st_dev。
    # `same_filesystem` 是三态：True（可用；可能只是"未实证"的预筛结论）/ False（明确不可用）/
    # None（无法判定 ⇒ 警告放行，真正的结论由运行时的 os.link 给出）。
    support: bool | None
    support_note: str
    if probe:
        probe_ok, probe_note = paths.probe_cross_filesystem(source_dir, target_dir)
        result["probe"] = {"ok": probe_ok, "note": probe_note}
        if probe_ok is not None:
            support, support_note = probe_ok, probe_note
        else:
            support, support_note = paths.check_hardlink_support(source_dir, target_dir, probe=False)
    else:
        support, support_note = paths.check_hardlink_support(source_dir, target_dir, probe=False)

    result["same_filesystem"] = support
    if support is False:
        result["ok"] = False
        result["errors"].append(support_note)
    elif support is None:
        result["warnings"].append(f"无法判定源/目标之间能否创建硬链接：{support_note}")
    elif support_note:
        result["warnings"].append(support_note)
    return result


def _source_child_count(source_dir: str) -> int | None:
    """源目录一级文件夹数量；不可读返回 ``None``（校验接口不因它失败）。"""
    try:
        with os.scandir(source_dir) as it:
            return sum(1 for entry in it if entry.is_dir())
    except OSError:
        return None


@router.get("")
def list_libraries(
    _: str = Depends(security.require_user),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
) -> dict:
    """库列表（信封格式，§5.0）。"""
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    conn = get_conn()
    total = int(conn.execute("SELECT COUNT(*) AS c FROM libraries").fetchone()["c"])
    rows = conn.execute(
        "SELECT * FROM libraries ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return {
        "items": [library_dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
def create_library(
    body: LibraryInput, _: str = Depends(security.require_user)
) -> dict:
    """新建库（201）。处理器必须存在（422），路径与重名规则非法 → 400/409。"""
    name = (body.name or "").strip()
    if not name:
        raise bad_request("媒体库名称不能为空。")
    spec = _resolve_processor(body.processor_id or "")
    processor_id = str(spec.get("id") or "")
    library_type = _derive_type(body.type, spec)
    log_file = _resolve_log_file(body.log_file, processor_id=processor_id, spec=spec)
    rule_override = _resolve_rule_override(body.rule_override, spec)
    source_dir = _library_path(body.source_dir)
    target_dir = _library_path(body.target_dir)
    if not source_dir or not target_dir:
        raise bad_request("源目录与目标目录都不能为空。")
    if source_dir == target_dir:
        raise bad_request("源目录与目标目录不能相同。")

    conn = get_conn()
    exists = conn.execute("SELECT id FROM libraries WHERE name=?", (name,)).fetchone()
    if exists is not None:
        raise conflict(f"已存在同名媒体库「{name}」，请换一个名称。")

    source_roots, target_roots = _allowed_roots()
    try:
        paths.assert_allowed(source_dir, source_roots, "源目录")
        paths.assert_allowed(target_dir, target_roots, "目标目录")
    except ValueError as exc:
        raise bad_request(str(exc))
    ok_mounts, mount_error = paths.check_cross_mounts(source_dir, target_dir)
    if not ok_mounts:
        raise bad_request(mount_error)

    moment = now_iso()
    with conn:
        cursor = conn.execute(
            "INSERT INTO libraries (name, type, processor_id, processor, log_file, enabled,"
            " source_dir, target_dir, match_categories, match_tags, match_path_prefixes,"
            " orphan_cleanup, permission_mode, rule_override, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                library_type,
                processor_id,
                configstore.legacy_processor_column(processor_id),
                log_file,
                1 if body.enabled else 0,
                source_dir,
                target_dir,
                _dump_list(body.match_categories),
                _dump_list(body.match_tags),
                _dump_list(body.match_path_prefixes),
                1 if body.orphan_cleanup else 0,
                1 if body.permission_mode else 0,
                rule_override,
                moment,
                moment,
            ),
        )
    row = get_library_row(int(cursor.lastrowid))
    _export_config()
    return library_dict(row)


@router.get("/{library_id}")
def get_library(library_id: int, _: str = Depends(security.require_user)) -> dict:
    """库详情。"""
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")
    return library_dict(row)


@router.put("/{library_id}")
def update_library(
    library_id: int, body: LibraryInput, _: str = Depends(security.require_user)
) -> dict:
    """整体更新（PUT 语义：未提供的字段回到模型默认值，前端应回填完整表单）。"""
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")

    name = (body.name or "").strip()
    if not name:
        raise bad_request("媒体库名称不能为空。")
    spec = _resolve_processor(body.processor_id or row["processor_id"] or "")
    processor_id = str(spec.get("id") or "")
    library_type = _derive_type(body.type, spec)
    # 未显式给 log_file ⇒ 沿用本库当前的名字（改库不该悄悄换日志，换名等于丢掉历史记录，
    # 下次运行会对真实媒体库全量重跑）；撞车判定把自己排除掉，所以原样保存不会自撞。
    log_file = _resolve_log_file(
        body.log_file if body.log_file is not None else row["log_file"],
        processor_id=processor_id,
        spec=spec,
        exclude_library_id=library_id,
    )
    source_dir = _library_path(body.source_dir)
    target_dir = _library_path(body.target_dir)
    if source_dir == target_dir:
        raise bad_request("源目录与目标目录不能相同。")
    # 覆盖：``None`` = 本次请求没提这件事 ⇒ 沿用库里的现值（PUT 是"整体更新"，
    # 但规则覆盖刻意例外：前端表单不总是带着它，悄悄清空用户写好的规则是最坏的结果）。
    if body.rule_override is None:
        rule_override = row["rule_override"]
    else:
        rule_override = _resolve_rule_override(body.rule_override, spec)

    conn = get_conn()
    clash = conn.execute(
        "SELECT id FROM libraries WHERE name=? AND id<>?", (name, library_id)
    ).fetchone()
    if clash is not None:
        raise conflict(f"已存在同名媒体库「{name}」，请换一个名称。")

    source_roots, target_roots = _allowed_roots()
    try:
        paths.assert_allowed(source_dir, source_roots, "源目录")
        paths.assert_allowed(target_dir, target_roots, "目标目录")
    except ValueError as exc:
        raise bad_request(str(exc))
    ok_mounts, mount_error = paths.check_cross_mounts(source_dir, target_dir)
    if not ok_mounts:
        raise bad_request(mount_error)

    with conn:
        conn.execute(
            "UPDATE libraries SET name=?, type=?, processor_id=?, processor=?, log_file=?,"
            " enabled=?, source_dir=?, target_dir=?, match_categories=?, match_tags=?,"
            " match_path_prefixes=?, orphan_cleanup=?, permission_mode=?, rule_override=?,"
            " updated_at=?"
            " WHERE id=?",
            (
                name,
                library_type,
                processor_id,
                configstore.legacy_processor_column(processor_id),
                log_file,
                1 if body.enabled else 0,
                source_dir,
                target_dir,
                _dump_list(body.match_categories),
                _dump_list(body.match_tags),
                _dump_list(body.match_path_prefixes),
                1 if body.orphan_cleanup else 0,
                1 if body.permission_mode else 0,
                rule_override,
                now_iso(),
                library_id,
            ),
        )
    update_row = get_library_row(library_id)
    _export_config()
    return library_dict(update_row)


@router.delete("/{library_id}")
def delete_library(
    library_id: int,
    _: str = Depends(security.require_user),
    confirm: bool = Query(default=False),
) -> dict:
    """删除库记录。

    **只删数据库记录，绝不删除目标目录里已经建好的硬链接**——那些链接与源文件
    同 inode，删掉会让用户以为"媒体文件丢了"。要清理请用户自己去文件管理器操作。
    ``?confirm=true`` 是防误触的显式确认（§5.1）。

    同时清掉本库的作品账本（``link_records``）；重命名缓存**不动**（缓存是历史
    记录，删库不等于重命名没发生过）；账本留着会让"重建同名库"继承旧记录而被静默跳过。
    """
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")
    if not confirm:
        raise bad_request(
            f"删除媒体库「{row['name']}」需要显式确认，请带上 ?confirm=true 重新提交。"
            f"注意：删除只移除本应用里的规则记录，不会删除 {row['target_dir']} 下已存在的硬链接。"
        )
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    # 作品账本按库清理（不依赖外键，删除动作必须看得见）。
    try:
        records_removed = drop_link_records_by_library(library_id)
    except Exception as exc:  # noqa: BLE001 - 账本清理失败不该让删库失败
        logger.warning("清理媒体库 %s 的作品账本失败：%s", library_id, exc)
        records_removed = 0
    # 历史任务里的 library_id 会被 ON DELETE SET NULL 置空，library_name 保留，
    # 因此任务历史仍可读，不会因为删库出现"未知任务"。
    # 必须立刻重写 libraries.yaml：否则后台同步线程会在下一轮把文件里的这条库
    # "导入"回来（文件是镜像，DB 才是真源）。
    _export_config()
    return {
        "deleted": True,
        "hardlinks_kept": True,
        "library_id": library_id,
        "link_records_removed": records_removed,
    }


@router.post("/{library_id}/validate")
def validate_library(
    library_id: int,
    _: str = Depends(security.require_user),
    probe: bool = Query(default=True),
) -> dict:
    """只读预检（§5.1）：不改任何配置、不建目录、不建硬链接。

    ``probe=true``（默认）先做一次**只读安全的实证探测**：从源树里有界
    递归取一个**已存在**的普通文件，把它 ``os.link`` 到目标目录的临时名再删掉 —— 源目录
    一个字节都不写，因此源目录只读时也能得到真结论，不会因为写不进去就自我降级。

    拿不到样本 / 目标不可写 / 拿到无法与「跨挂载」区分的 errno ⇒ 结论为 ``None``（无法
    判定），降级到挂载 ID / ``st_dev`` 预筛。``same_filesystem`` 因此是三态：
    ``true``（可用，可能只是未实证的预筛结论）／``false``（明确不可用 → 进 ``errors``）／
    ``null``（无法判定 → 进 ``warnings`` 并放行）。
    """
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")
    source_dir = row["source_dir"]
    target_dir = row["target_dir"]
    result = _validate_paths(source_dir, target_dir, probe=probe)
    result["library_id"] = library_id
    result["library_name"] = row["name"]
    result["type"] = row["type"]
    result["processor_id"] = row["processor_id"]
    spec = registry.get_processor(row["processor_id"])
    result["processor_ok"] = spec is not None
    result["processor_name"] = (spec or {}).get("name") or row["processor_id"]
    result["processor_kind"] = (spec or {}).get("kind")
    result["log_file"] = row["log_file"]
    result["log_path"] = str(settings.library_log_path(row["log_file"]))
    if spec is None:
        result["ok"] = False
        result["errors"].append(
            f"处理器 {row['processor_id']} 不存在（可能已被删除）。"
            f"请先到「处理器」页重建，或把本库改到其它处理器。"
        )
    result["source_folders"] = _source_child_count(source_dir)
    result["target_dir_exists"] = os.path.isdir(target_dir)
    result["same_filesystem_paths"] = {
        "source_path": paths.existing_ancestor(source_dir),
        "target_path": paths.existing_ancestor(target_dir),
    }
    return result


# --------------------------------------------------------------------------- #
# 作品级操作（每库账本）
# --------------------------------------------------------------------------- #


def _resolve_works(folders: list[str]) -> list[str]:
    """校验作品名（必须是**一级**文件夹名）并去重；空列表 → 400。"""
    cleaned: list[str] = []
    for raw in folders or []:
        name = str(raw or "").strip()
        if not name:
            continue
        if "/" in name or "\\" in name or name in (".", ".."):
            raise bad_request(
                f"作品名必须是目标目录下的一级文件夹名，不能包含路径分隔符：{name}"
            )
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise bad_request("必须至少指定一个作品（folders 不能为空）。")
    return cleaned


def _rename_cache_path(row) -> str:
    """本库重命名阶段的账本（每个媒体库一份 JSON 缓存）。"""
    return str(
        settings.library_log_path(row["log_file"] or f"{row['processor_id']}cache.log")
    )


@router.get("/{library_id}/works")
def list_library_works(
    library_id: int,
    _: str = Depends(security.require_user),
    q: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
) -> dict:
    """本库的「已执行作品」清单（账本 = ``link_records`` 表）。

    独立页需要的分页与搜索都在**服务端**做：

    * ``q``：作品名子串，大小写不敏感，``%``/``_`` 按字面处理；空/省略 = 不过滤；
    * ``limit`` / ``offset``：分页（``clamp_limit`` / ``clamp_offset`` 夹住上下界）；
    * ``total`` = **命中**条数（配合翻页），``ledger_total`` = 本库账本总条数。

    每行额外给两个"这条记录还算不算数"的直接证据：

    * ``target_exists``：目标目录里这个作品文件夹还在不在（被手动删过 = ``false``）；
    * ``rename_recorded``：本库**重命名阶段**的 JSON 缓存里还有没有这个作品。

    ``rename_missing`` / ``rename_only`` 是两本账对不上的差集（只做提示，不自动修复：
    账本刻意只看作品级、不保证文件级完整性）。差集与 ``target_*`` 一样按**整本账**算，
    不受 ``q``/翻页影响；``target_files``/``target_bytes``/``target_loose`` 只对**本页**
    的行做 ``os.walk``（几千个作品时"翻一页算一页"才不会把接口拖死）。
    """
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")
    target_dir = row["target_dir"]
    cache_path = _rename_cache_path(row)
    cache_data = processor_logs.read_json_cache(cache_path)
    limit = clamp_limit(limit)
    offset = clamp_offset(offset)
    query = str(q or "").strip()
    records, matched = search_link_records(
        library_id, query=query or None, limit=limit, offset=offset
    )
    ledger_names = {r["folder"] for r in search_link_records(library_id, limit=None)[0]}
    items: list[dict] = []
    for record in records:
        path = record["target_path"] or target_folder(target_dir, record["folder"])
        # 现算目标目录的实际情况：账本里的 ``linked`` 是历史记账（老库升级后为空），
        # 而用户真正想知道的是"这个作品现在在目标目录里有多少文件"。多走一次
        # ``os.walk`` 换一句不会骗人的话，值得。
        stats = folder_stats(path) if os.path.isdir(path) else None
        items.append(
            {
                **record,
                "target_path": path,
                "target_exists": os.path.isdir(path),
                "rename_recorded": record["folder"] in cache_data,
                "target_files": stats["files"] if stats else 0,
                "target_bytes": stats["bytes"] if stats else 0,
                "target_loose": stats["loose_files"] if stats else 0,
            }
        )
    return {
        "items": items,
        "total": matched,
        "ledger_total": len(ledger_names),
        "q": query,
        "limit": limit,
        "offset": offset,
        "library_id": library_id,
        "library_name": row["name"],
        "source_dir": row["source_dir"],
        "target_dir": target_dir,
        "rename_log_path": cache_path,
        "rename_entries": len(cache_data),
        "rename_missing": sorted(name for name in ledger_names if name not in cache_data),
        "rename_only": sorted(str(name) for name in cache_data if str(name) not in ledger_names),
    }


@router.post("/{library_id}/works/delete")
def delete_library_works(
    library_id: int,
    body: WorkDeleteRequest,
    _: str = Depends(security.require_user),
) -> dict:
    """删除选中作品的记录，并**连带删除目标目录里的整个作品文件夹**。

    确认口径：删除要附带删除对应媒体库内的产物，并且要有显著的提示与确认。
    界面侧是**一级确认**：点「删除记录」→ 弹窗里点「确定删除」即执行；
    下面这道 ``confirm`` 保留为**防误调用**：

    * ``dry_run=true``：只回执"会删掉什么"（目标路径、文件数、字节数、**非硬链接文件数**），
      一个字节都不动 —— WebUI 的弹窗用它填内容（**这只是读，不构成一次确认**）；
    * ``confirm=true``：真的执行；``confirm=false`` 且非 dry-run → 400。

    删除三处，缺一不可：

    1. ``link_records`` 里这些作品的行（建链阶段的判据）；
    2. 本库重命名 JSON 缓存里的同名条目 —— 只删第 1 处的话，下次运行重命名阶段会打印
       ``Skipping folder: … (already processed at …)``，用户会以为"删了记录也没重建"；
    3. 目标目录下的整个作品文件夹（``shutil.rmtree``；符号链接只删链接本身）。

    **源目录一个字节都不动**：硬链接是另一条目录项，删掉它不会减少源文件的数据。
    """
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")
    names = _resolve_works(body.folders)
    target_dir = row["target_dir"]
    cache_path = _rename_cache_path(row)
    cache_data = processor_logs.read_json_cache(cache_path)
    ledger = {
        record["folder"]: record
        for record in search_link_records(library_id, limit=None)[0]
    }

    plans: list[dict] = []
    for name in names:
        path = safe_target_path(target_dir, name)
        if path is None:
            raise bad_request(
                f"作品名不合法：{name}（必须是目标目录 {target_dir} 下的一级文件夹名，"
                f"且不能越出目标目录）。"
            )
        plans.append(
            {
                "folder": name,
                "target_path": path,
                "in_ledger": name in ledger,
                "rename_recorded": name in cache_data,
                **folder_stats(path),
            }
        )

    if body.dry_run:
        return {
            "dry_run": True,
            "library_id": library_id,
            "library_name": row["name"],
            "target_dir": target_dir,
            "rename_log_path": cache_path,
            "items": plans,
        }
    if not body.confirm:
        raise bad_request(
            "删除作品记录会连带删除目标目录里的整个作品文件夹（源目录不受影响），必须显式确认："
            "请先带 dry_run=true 查看将删除的内容，再用 confirm=true 提交。"
        )

    deleted_rows = drop_link_records(library_id, names)
    rename_removed = [name for name in names if processor_logs.remove_entry(cache_path, name)]
    items: list[dict] = []
    for plan in plans:
        path = plan["target_path"]
        removed = False
        error: str | None = None
        if os.path.islink(path):
            # 符号链接只删链接本身（rmtree 会拒绝符号链接，且删目标内容不是本操作的本意）。
            try:
                os.unlink(path)
                removed = True
            except OSError as exc:
                error = str(exc)
        elif os.path.isdir(path):
            try:
                shutil.rmtree(path)
                removed = True
            except OSError as exc:
                error = str(exc)
        items.append({**plan, "target_removed": removed, "error": error})
    logger.info(
        "媒体库 %s「%s」删除作品记录：账本 %d 条、重命名缓存 %d 条、目标文件夹 %d 个",
        library_id,
        row["name"],
        len(deleted_rows),
        len(rename_removed),
        sum(1 for item in items if item["target_removed"]),
    )
    return {
        "deleted": True,
        "library_id": library_id,
        "library_name": row["name"],
        "ledger_removed": [record["folder"] for record in deleted_rows],
        "rename_removed": rename_removed,
        "items": items,
        "next_run_note": (
            "这些作品已从账本移除：下次运行本库时会重新建链并按规则重命名，其余作品不受影响。"
        ),
    }


@router.post("/{library_id}/works/rename", status_code=202)
def rename_library_works(
    library_id: int,
    body: WorkSelection,
    _: str = Depends(security.require_user),
) -> dict:
    """对选中作品**立即**执行一次重命名（202 + Task；只涉及这些作品，不建链）。

    与 ``/{id}/run`` 的区别：``run`` 可能带整库范围，这里**永远**只作用于勾选的作品
    （``scope.mode="works"``），因此可以安全地强制重跑 —— 这是"整库 force 停用"之后
    用户重做单个作品的两条途径之一（另一条是删记录后等下次运行自动补做）。
    """
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")
    if not bool(row["enabled"]):
        raise conflict(f"媒体库「{row['name']}」已停用，请先启用后再运行。")
    target_root = str(row["target_dir"] or "")
    if not os.path.isdir(target_root):
        # 纯重命名任务在**目标目录**上工作（目标里的文件才是"已建链、待改名"的产物）：
        # 目标根整个不存在时，引擎对每个作品都会 `无法读取媒体库目录 …：No such file or directory`
        # 收场 —— 入队前就说清楚，别让它变成一个"202 收下、随即失败"的任务。
        # 目标目录由建链阶段 `os.makedirs` 自建，所以这条路只有"只改名、不建链"才会踩到。
        raise bad_request(
            f"目标目录 {target_root} 不存在：纯重命名任务只整理目标目录里已经存在的文件。"
            f"请先运行一次本库（会自动创建目标目录并建立硬链接），"
            f"或检查该库的目标目录是否被移动或删除。"
        )
    names = _resolve_works(body.folders)
    spec = _resolve_processor(row["processor_id"] or "")
    if str(spec.get("kind")) == "hardlink_only":
        raise bad_request(
            f"处理器「{spec.get('name') or spec.get('id')}」是 hardlink_only，没有任何重命名规则，"
            f"不能单独执行重命名。"
        )
    task_id = queue.submit(
        TaskSpec(
            library_id=library_id,
            kind="rename",
            trigger="manual",
            scope={"mode": "works", "folders": names},
            payload={
                "library_name": row["name"],
                "force": True,
                "processor_id": str(spec.get("id") or ""),
            },
        )
    )
    task_row = get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return task_dict(task_row)


@router.post("/{library_id}/run", status_code=202)
def run_library(
    library_id: int,
    _: str = Depends(security.require_user),
    body: RunRequest | None = Body(default=None),
) -> dict:
    """把库任务推进队列（202 + Task 对象，§5.1）。

    这里**只入队不执行**：真正的工作在队列的单工作线程里串行完成。
    """
    row = get_library_row(library_id)
    if row is None:
        raise not_found(f"媒体库 {library_id} 不存在。")
    if not bool(row["enabled"]):
        raise conflict(f"媒体库「{row['name']}」已停用，请先启用后再运行。")

    request = body or RunRequest()
    scope_mode = request.scope.mode
    folder = (request.scope.folder or "").strip() or None
    scope = {"mode": scope_mode}
    if scope_mode == "folder":
        if not folder:
            raise bad_request("按文件夹运行时必须提供 scope.folder。")
        if "/" in folder or "\\" in folder or folder in (".", ".."):
            # 必须是一级文件夹名：硬链接只遍历 source_dir 的一级目录，
            # 传嵌套路径进来只会让 ScopedOs 过滤掉所有条目、静默什么也不做。
            raise bad_request(
                f"scope.folder 必须是源目录下的**一级**文件夹名，不能包含路径分隔符：{folder}"
            )
        scope["folder"] = folder
    else:
        scope["mode"] = "full"

    if request.force and scope_mode != "folder":
        # **任何涉及整个库的 force 都不被允许**。整库 force 会让
        # 建链阶段把已经改过名的文件按"原名"再链一份（目标里于是同一 inode 出现两个目录项），
        # 重命名阶段接着多出一个 `xxx (2).mkv` 后缀 —— 越点越乱。
        # 重做整库的正道：在 WebUI 的作品清单里删掉要重做的作品记录，下次运行自动补做，
        # 其余作品不受影响（见 GET/POST /{id}/works*）。
        raise bad_request(
            "整库强制重跑已停用：force 只允许配合 scope.mode='folder'（单个作品）使用。"
            "要重做作品，请到「作品记录」里删除对应作品的记录，下次运行时这些作品会自动补做，"
            "其余作品不受影响。"
        )

    spec = _resolve_processor(row["processor_id"] or "")
    processor_id = str(spec.get("id") or "")
    kind = "rename_hardlink" if spec.get("kind") == "rename" else "hardlink"
    task_id = queue.submit(
        TaskSpec(
            library_id=library_id,
            kind=kind,
            trigger="manual",
            scope=scope,
            payload={
                "library_name": row["name"],
                "force": bool(request.force),
                "processor_id": processor_id,
            },
        )
    )
    task_row = get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return task_dict(task_row)


def _dump_list(values) -> str:
    """把字符串数组序列化成 JSON 文本（同时去空白、去重、保持顺序）。"""
    cleaned: list[str] = []
    for item in values or []:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return json.dumps(cleaned, ensure_ascii=False)
