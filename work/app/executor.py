"""任务执行器（ARCHITECTURE §6.7 + PROCESSORS §4/§5）——把一条 ``tasks`` 记录变成一次执行。

职责分成两半：

* **硬链接**：调用原生实现 ``app/processors/hardlink.create_hard_links``（经
  ``app/script_adapter.run_hardlinks`` 做作用域与日志接管），
  日志固定在 ``<data_dir>/logs/hard_links.log``；
* **重命名**：调用**配置驱动的引擎** ``app/processors/engine.run_rename``，日志按媒体库的
  ``libraries.log_file`` 落在同一个 ``<data_dir>/logs`` 目录（PROCESSORS §5：所有重命名日志
  必须同一文件夹）。

执行流程（与 §6.7 逐条对应）：
1. 读 task/library，置 ``status='running'`` / ``started_at``；
2. 预检：源可读、目标最近已存在祖先与源同 ``st_dev``；
3. ``kind`` 含 hardlink → ``run_hardlinks``；含 rename → 引擎 ``run_rename``；
4. 汇总 ``result``（计数 + 限量样本）；
5. 判状态：``exception`` 或“全失败” → ``failed``，部分失败 → ``success`` + warnings；
6. 写 ``finished_at``、落日志、``trim_history``。

**绝不写源目录里的媒体文件**：唯一对源侧的写入是建链自己 ``os.link`` 出来的目标链接
（目标在 /library 下），以及只读的 ``os.stat`` 核对。
"""

from __future__ import annotations

import json
import logging
import os
import time

from . import script_adapter
from .config import settings
from .db import get_conn, get_setting, now_iso, tx, trim_history
from .link_records import drop as _drop_link_records
from .link_records import folder_sizes as _link_folder_sizes
from .link_records import upsert_many as _upsert_link_records
from .logbuf import logbuf
from .paths import check_hardlink_support, snapshot_source_tree, verify_source_tree
from .processors import engine, overrides
from .processors.registry import get_processor
from .serializers import normalize_kind

logger = logging.getLogger("app.executor")

SEVERITY_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

#: 每个 sample 数组的上限；``None`` = **不设上限**（默认）。
#:
#: 待办条数**完全由**
#: ``result.samples.<kind>`` 的条数决定（``app/todos.py`` 逐条落账），所以样本默认全量保留 ——
#: 一次全量扫描里有多少个文件出问题，看板就记多少条。固定的 100 条上限会把大部分问题
#: 悄悄吞掉：一次全量运行里 8 534 个建链失败，旧版只留下 **100** 条待办。
#: 代价是 ``tasks.result`` 会随问题规模增长（逐条 `{src,dst,error}`）—— 这是"看得全"与
#: "单行小"的取舍，这里取前者；全量逐条运行明细另有 ``task_logs``。
#: 要退回限量只改这一个常量（例如 ``SAMPLE_LIMIT = 100``），``samples_truncated``
#: 与前端截断提示会照旧工作。
SAMPLE_LIMIT: int | None = None


class SampleCollector:
    """样本收集器：默认**全量**保留；显式传 ``limit`` 时只留前 ``limit`` 条并把 ``truncated`` 置真。

    默认不设上限 —— 上限等于"失败只看得见一部分"。
    全量逐条运行明细仍然另有去处 —— ``task_logs``（受 ``max_task_log_lines`` 约束）。
    """

    def __init__(self, limit: int | None = SAMPLE_LIMIT) -> None:
        self.limit = limit
        self.items: list = []
        self.truncated = False

    def add(self, item) -> None:
        if self.limit is None or len(self.items) < self.limit:
            self.items.append(item)
        else:
            self.truncated = True


def _relativize(target_path: str, target_dir: str, source_dir: str) -> str:
    """把目标侧的路径换算回源侧路径（``result.samples.link_failed[].src``）。

    失败输出只给了 ``target_file``（``os.link(source_file, target_file)`` 的目标），
    而目标目录与源目录是同一套一级文件夹结构，所以相对路径两端一致，可以直接拼。
    拼不出来时返回空串——**宁可留空，也不要编一个看起来对的路径**。
    """
    try:
        relative = os.path.relpath(target_path, target_dir)
    except (ValueError, OSError):
        return ""
    if relative.startswith(".."):
        return ""
    return os.path.join(source_dir, relative)


def _sample_renamed(outcome) -> list[dict]:
    """重命名样本 -> ``[{"from":…,"to":…}]``（只取文件名，避免暴露完整路径冗余）。

    条数跟随 :data:`SAMPLE_LIMIT`：默认无上限（``None``）时全量返回。
    """
    rows = list(outcome.renamed or [])
    if SAMPLE_LIMIT is not None:
        rows = rows[:SAMPLE_LIMIT]
    samples = []
    for old, new in rows:
        samples.append({"from": old, "to": new})
    return samples


def _log_parsed_line(task_id: int, line: str) -> str:
    """按 ARCHITECTURE §6.5 第 8 点的前缀表给一条 stdout 行定级。

    返回该行的级别；未匹配的行返回 ``DEBUG``（原样记录，不丢信息）。"""
    if line.startswith("创建硬链接失败: "):
        level = "ERROR"
    elif line.startswith("已创建硬链接: "):
        level = "INFO"
    elif line.startswith("文件夹已记录且体积一致，跳过: "):
        level = "INFO"
    elif line.startswith("已删除旧记录: "):
        level = "INFO"
    elif line.startswith("Deleted orphaned hard link folder: "):
        level = "WARNING"
    elif line.startswith("已记录文件夹体积: "):
        level = "INFO"
    elif line.startswith("已删除文件体积记录"):
        level = "INFO"
    elif line.startswith("Renamed: "):
        level = "INFO"
    elif line.startswith("Renamed (suffix): "):
        # L2 防丢：目标名被占用 ⇒ 加「 (2)」「 (3)」后缀保留（没有覆盖、没有丢文件）。
        level = "INFO"
    elif line.startswith("Displaced: "):
        # L2 防丢：新来的文件更大 ⇒ 目标盘上原有的同名文件被改名让位（只动目标库内的文件）。
        level = "INFO"
    elif line.startswith("Kept (no episode): "):
        # 没解析出集数 ⇒ 保留原文件名。这不是失败，但必须显眼（不能静默）。
        level = "WARNING"
    elif line.startswith("Error renaming "):
        level = "ERROR"
    elif line.startswith("Skipping folder: "):
        level = "INFO"
    elif line.startswith("Extra (kept): "):
        # L3 特典排除（不再移动位置）：命中特典判据的散装文件
        # **保留原名留在原位置**（不改名、不移动）—— 留一条中文 INFO，免得看起来"什么也没发生"。
        logbuf.add_log(
            task_id,
            f"特典保留原名（判据命中，未改名、未移动）：{line[len('Extra (kept): '):].strip()}",
            "INFO",
            stream="stdout",
        )
        return "INFO"
    else:
        level = "DEBUG"
    logbuf.add_log(task_id, line, level, stream="stdout")
    return level


def _task_row(task_id: int):
    return get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def _library_row(library_id):
    if library_id is None:
        return None
    return get_conn().execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()


def _parse_scope(raw) -> dict:
    try:
        scope = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        scope = {}
    return scope if isinstance(scope, dict) else {}


def _describe_override(override: dict) -> str:
    """把本库的规则覆盖压成一行中文，写进任务日志（用户要能一眼看出"这次用的是我改的规则"）。"""
    parts: list[str] = []
    naming = override.get("naming") if isinstance(override.get("naming"), dict) else {}
    template = naming.get("template")
    if isinstance(template, str) and template.strip():
        parts.append(f"命名模板 {template!r}")
    elif isinstance(naming.get("segments"), list) and naming["segments"]:
        parts.append(f"命名段 {len(naming['segments'])} 项")
    if isinstance(naming.get("expressions"), dict) and naming["expressions"]:
        parts.append("自定义表达式 " + "、".join(sorted(naming["expressions"])))
    traverse = override.get("traverse") if isinstance(override.get("traverse"), dict) else {}
    if isinstance(traverse.get("accept_dirs"), list):
        parts.append(f"接受目录 {len(traverse['accept_dirs'])} 条")
    if isinstance(traverse.get("reject_dirs"), list):
        parts.append(f"拒绝目录 {len(traverse['reject_dirs'])} 条")
    for key, label in (("file_scope", "文件作用域"), ("skip_season_zero", "跳过 Season 00")):
        if key in traverse:
            parts.append(f"{label}={traverse[key]}")
    if isinstance(override.get("extensions"), list) and override["extensions"]:
        parts.append("后缀白名单 " + "、".join(str(item) for item in override["extensions"]))
    return "；".join(parts) if parts else "（空）"


def _set_status(task_id: int, status: str, **columns) -> None:
    """更新任务状态并广播 SSE 事件。"""
    assignments = ["status=?"]
    values: list = [status]
    for key, value in columns.items():
        assignments.append(f"{key}=?")
        values.append(value)
    values.append(task_id)
    with tx() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?", tuple(values))
    logbuf.emit_task(task_id, status)


def _spawn_orphan_removal_task(task: dict, library, removed: list[str]) -> None:
    """孤儿目录被删除后，安排一次独立的 rename 任务（先链接后重命名，避免复活）。

    关键约束在这里体现为**顺序**：新硬链接必须先建好，重命名才有意义；
    若在删除后立刻 rename，孤儿清理分支会立刻把刚建好的目标目录再删一次。
    因此这里只 submit，本任务结束时队列自然串行执行它。"""
    from .queue import TaskSpec, queue

    payload = {"library_name": (library["name"] if library else task["library_name"])}
    queue.submit(
        TaskSpec(
            library_id=(int(library["id"]) if library else None),
            kind="rename_hardlink",
            trigger="retry",
            scope={"mode": "full"},
            payload=payload,
        )
    )


def execute_task(task_id: int) -> None:
    """执行一条任务；**绝不向上抛异常**（队列工作线程依赖这一点保持存活）。"""
    task = _task_row(task_id)
    if task is None:
        logger.warning("任务 %s 不存在，跳过", task_id)
        return

    started = time.monotonic()
    library = _library_row(task["library_id"])
    scope = _parse_scope(task["scope"])
    folder = scope.get("folder") if scope.get("mode") == "folder" else None
    # ``scope.mode="works"`` —— WebUI 里勾选若干作品后的"立即重命名"（或重建链）。
    # 名字就是目标目录下的一级文件夹名（= 作品名），只作用于这些作品。
    work_folders = (
        [str(name) for name in (scope.get("folders") or []) if str(name or "").strip()]
        if scope.get("mode") == "works"
        else []
    )
    try:
        payload = json.loads(task["payload"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    force = bool(payload.get("force"))

    _set_status(task_id, "running", started_at=now_iso())
    logbuf.add_log(
        task_id,
        f"开始执行：库={task['library_name'] or '（已删除）'} 类型={task['kind']} "
        f"范围={folder or ('、'.join(work_folders) if work_folders else '全量')} 强制={force}",
        "INFO",
    )

    result = {
        "linked": 0,
        "skipped": 0,
        "link_failed": 0,
        # 目标侧已经是源文件硬链接（同一 inode）的文件数；只增不减地加进
        # ``skipped``，好让"幂等重跑"在结果里看得出来，且不会被算成失败。
        "already_linked": 0,
        "renamed": 0,
        "rename_failed": 0,
        # 命名引用了集数、但没解析出集数的文件数（保留原文件名、未改名）。
        # 明细在 samples.kept_unresolved_episode（相对媒体库根目录的路径）。
        "kept_unresolved_episode": 0,
        # L4「可见性」 + L2 防丢：改名时**目标文件名被占用**的文件数。不再有
        # 覆盖：冲突由引擎加 `` (2)``/`` (3)``… 后缀化解，这里记录"发生过争名"这个事实。
        # 明细形状见 samples.name_collisions（{dir,target,previous,current,kind}）。
        "name_collisions": 0,
        # L2 防丢：因为争名而**加了后缀**（正片优先、其余保留）的文件数 —— 与
        # ``name_collisions`` 大多重合，但它是"实际改名成了什么"的账，任务清单按它渲染。
        # 明细形状见 samples.renamed_with_suffix（{dir,old,target,clean,size,reason,displaced}）。
        "renamed_with_suffix": 0,
        # L2 防丢：**目标盘上原有的文件让位**给体积更大的新文件的条数（一个都不丢）。
        "displaced_targets": 0,
        # L4「可见性」：所有"没解析出集数"的文件数 —— **不论是否因此保留了原名**
        # （命名不引用集数时它们照旧改名）。明细在 samples.episode_unresolved。
        "episode_unresolved": 0,
        # L3 特典排除（不再移动位置）：命中特典判据、被**排除在
        # 改名之外**的散装特典文件数。这些文件保留原名留在原位置（不改名、不移动），明细形状见
        # samples.extras_classified（{dir, name, size, pattern}：dir = "作品名/Season N" 标签）。
        "extras_classified": 0,
        # 命名里用了"上下文里没有的变量名"的文件数 —— 那些段被留空，
        # 文件名因此少了一段（改名照样成功）。明细形状见 samples.unknown_params
        # （{label, file, params}：label = "作品名/子目录/文件名" 作用域标签）。
        "unknown_params": 0,
        # 子目录模式下"作品目录里没有可接受的季目录、文件直接躺在作品目录里" ⇒ 改名阶段整部
        # 跳过。这一路既要可计数、也要进看板待办。明细形状见 samples.unscoped_works
        # （{dir, folder, files, samples}）。
        # **必须在这里先落 0**：收尾摘要无条件读它，而只有跑改名阶段的任务才会覆盖它
        # （纯建链 / hardlink_only 的任务根本不进那段代码）。
        "unscoped_works": 0,
        "orphans_removed": 0,
        "folders_scanned": 0,
        "bytes_linked": 0,
        "duration_ms": 0,
        "samples": {
            "link_failed": [],
            "orphans_removed": [],
            "renamed": [],
            "kept_unresolved_episode": [],
            "name_collisions": [],
            "renamed_with_suffix": [],
            "displaced_targets": [],
            "episode_unresolved": [],
            "extras_classified": [],
            "unknown_params": [],
            "unscoped_works": [],
        },
        "samples_truncated": False,
        "warnings": [],
        # 源目录完整性（运行前快照 / 运行后复核）。形状已冻结：
        # {"ok": bool, "changed": [相对路径, ...]}；`ok=False` 只进 warnings，**不改 status**。
        "source_integrity": {"ok": True, "changed": []},
    }
    warnings: list[str] = result["warnings"]
    errors: list[str] = []
    link_failed_samples = SampleCollector()
    orphan_samples = SampleCollector()
    renamed_samples = SampleCollector()
    kept_episode_samples = SampleCollector()
    # L4「可见性」：两个样本收集器，与上面同形（都不设上限）。
    collision_samples = SampleCollector()
    unresolved_episode_samples = SampleCollector()
    # L2 防丢：争名加后缀 / 原有文件让位 两份清单。
    suffix_samples = SampleCollector()
    displaced_samples = SampleCollector()
    # L3 特典排除：命中特典判据、被排除改名的散装文件清单（原名留在原位置）。
    extras_samples = SampleCollector()
    # 命名里用了不存在变量名、那一段被留空的文件清单（{label, file, params}）。
    unknown_param_samples = SampleCollector()
    # 结构不可识别（没有季目录、文件直接躺在作品目录里）的作品清单。
    unscoped_work_samples = SampleCollector()

    if library is None:
        message = "媒体库规则已被删除，任务无法执行。"
        logbuf.add_log(task_id, message, "ERROR")
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        with tx() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', result=?, error=?, finished_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), message, now_iso(), task_id),
            )
        logbuf.emit_task(task_id, "failed")
        return

    source_dir = library["source_dir"]
    target_dir = library["target_dir"]
    kind = normalize_kind(task["kind"])
    do_link = kind in ("hardlink", "rename_hardlink")
    do_rename = kind in ("rename", "rename_hardlink")
    processor_id = (library["processor_id"] or "").strip()
    orphan_cleanup = bool(library["orphan_cleanup"]) and (
        (get_setting("orphan_cleanup_enabled", "true") or "true").lower()
        in ("1", "true", "yes", "on")
    )
    # 库设置里的 ``permission_mode``，默认关闭。
    # 开启时建链阶段把目标侧改成 0777（含"体积一致 ⇒ 跳过"的旧作品，否则开关对已入库内容无效）。
    # 注意其副作用：硬链接与源文件共享 inode ⇒ 源文件权限位一起变（大小/inode/mtime 不变，
    # 因此下面源目录完整性复核的维度不受影响）。
    permission_mode = (
        bool(library["permission_mode"])
        if library is not None and "permission_mode" in library.keys()
        else False
    )

    # ---------------------------------------------------------------- 预检
    if not os.path.isdir(source_dir):
        errors.append(f"源目录不存在或不可访问：{source_dir}")
        logbuf.add_log(task_id, errors[-1], "ERROR")
    elif not os.access(source_dir, os.R_OK | os.X_OK):
        errors.append(f"源目录不可读：{source_dir}")
        logbuf.add_log(task_id, errors[-1], "ERROR")

    # 级联判据（实证 → 挂载 ID → st_dev）。**任何情况下都不硬拦**——
    # 硬拦会让"挂载配错"变成整个库建不了，而预筛判据必要不充分；结论以实际 os.link 为准。
    support, support_note = check_hardlink_support(source_dir, target_dir)
    if support is False:
        warnings.append(support_note or "源与目标之间无法创建硬链接。")
        logbuf.add_log(task_id, f"警告：{warnings[-1]}（仍会尝试。）", "WARNING")
    elif support is None:
        warnings.append(f"无法判定源/目标之间能否创建硬链接：{support_note}")
        logbuf.add_log(task_id, f"警告：{warnings[-1]}", "WARNING")
    else:
        logbuf.add_log(task_id, f"预检通过：{support_note}", "INFO")

    # 源目录完整性快照（把"绝不改动原文件"从结构性保证变成可验证的保证）。
    source_snapshot = snapshot_source_tree(source_dir)
    logbuf.add_log(
        task_id,
        f"已快照源目录 {source_dir}（{len(source_snapshot)} 个文件），任务结束后复核。",
        "DEBUG",
    )

    if not errors:
        # ------------------------------------------------------------ 硬链接
        if do_link:
            # 每库账本：建链判据来自**本库**的作品记录（键 = 库 + 作品名，并比对
            # 源/目标目录），不再读全局 ``hard_links.log`` —— 于是"同源多库"和"同库换目标"
            # 都不再互相跳过。记录为空 = 该库什么都没做过 ⇒ 全量重建链。
            ledger_skip = (
                _link_folder_sizes(
                    int(library["id"]), source_dir=source_dir, target_dir=target_dir
                )
                if library is not None
                else None
            )
            if ledger_skip and force:
                # ``force`` 只可能来自"一个作品"或"一批作品"的范围（整库 force 已被
                # ``POST /api/libraries/{id}/run`` 直接 400 拒绝）。强制重跑必须真的重跑：
                # 把被强制的作品从账本判据里摘掉，否则旧账本行会让它继续被
                # "文件夹已记录且体积一致，跳过" —— force 变成静默的空操作。
                forced = set(work_folders) if work_folders else ({folder} if folder else set())
                ledger_skip = (
                    {
                        name: size
                        for name, size in ledger_skip.items()
                        if name not in forced
                    }
                    if forced
                    else {}
                )
            outcome = script_adapter.run_hardlinks(
                source_dir=source_dir,
                target_dir=target_dir,
                log_file=settings.hardlink_log_path,
                folder=folder,
                folders=work_folders or None,
                force=force,
                orphan_cleanup=orphan_cleanup,
                skip=ledger_skip,
                permission_mode=permission_mode,
            )
            if folder and not os.path.isdir(os.path.join(source_dir, folder)):
                warnings.append(
                    f"指定文件夹 {folder} 在源目录中不存在——既有行为是把它"
                    f"当作空文件夹处理并记录体积 0，本次未创建任何链接。"
                )
                logbuf.add_log(task_id, warnings[-1], "WARNING")
            for line in (outcome.stdout or "").splitlines():
                line = line.strip()
                if line:
                    _log_parsed_line(task_id, line)
            for target, error in outcome.failed:
                logbuf.add_log(task_id, f"硬链接失败：{target} - {error}", "ERROR")
                # src 由目标路径反推（两个目录的一级结构一致）；推不出来就留空。
                link_failed_samples.add(
                    {
                        "src": _relativize(target, target_dir, source_dir),
                        "dst": target,
                        "error": error,
                    }
                )
            for target in outcome.orphans_removed:
                orphan_samples.add(target)
                logbuf.add_log(
                    task_id,
                    f"警告：已删除孤儿硬链接目录 {target}（源目录中已无对应文件夹，"
                    f"这是 create_hard_links 的既有行为；如需保留请关闭该库的“孤儿目录清理”）。",
                    "WARNING",
                )
            for warning in outcome.warnings:
                warnings.append(warning)
                logbuf.add_log(task_id, f"警告：{warning}", "WARNING")
            if outcome.already_linked:
                logbuf.add_log(
                    task_id,
                    f"{len(outcome.already_linked)} 个文件在目标侧已经是同一 inode 的硬链接，"
                    f"按跳过处理（幂等重跑；不需要先删掉目标再跑一遍）。",
                    "INFO",
                )
            result["linked"] = len(outcome.created)
            # 已在目标侧建好的硬链接（同一 inode）计入"跳过"，不算失败。
            result["already_linked"] = len(outcome.already_linked)
            result["skipped"] = len(outcome.skipped) + len(outcome.already_linked)
            result["link_failed"] = len(outcome.failed)
            result["orphans_removed"] = len(outcome.orphans_removed)
            # folders_scanned：本次**实际处理过的一级文件夹**数量。
            # 新建链接的文件夹会打印“已记录文件夹体积: X - N”，跳过的会打印
            # “文件夹已记录且体积一致，跳过: X”，两者相加就是处理过的总数。
            result["folders_scanned"] = len(outcome.logged) + len(outcome.skipped)
            # bytes_linked：本次新建/重链并**写入体积记录**的文件夹字节数之和。
            # 它不是“磁盘空间的净增”（硬链接不占额外空间），而是“本次链接的媒体体积”。
            result["bytes_linked"] = int(sum(size for _folder, size in outcome.logged))
            # ``permission_mode`` 的结果（开关关闭时这三项恒为 0/空）。
            # 用 WARNING 级别而不只写 result：这个开关的副作用（改源文件权限位）必须留在
            # 任务日志里可追溯 —— 源目录完整性快照比的是 size/ino/mtime，看不见权限变化。
            if permission_mode:
                result["permission_mode_fixed_dirs"] = int(outcome.permission_dirs)
                result["permission_mode_fixed_files"] = int(outcome.permission_files)
                logbuf.add_log(
                    task_id,
                    f"权限模式（permission_mode）：已把 {outcome.permission_dirs} 个目录与 "
                    f"{outcome.permission_files} 个文件修正为 0777（目标库根目录与本次处理到的"
                    f"每个一级文件夹的目标子树，含账本里体积一致的旧作品）。硬链接与源文件共享"
                    f"同一 inode，因此源文件的权限位也随之改变（大小、inode 号、mtime 不变，"
                    f"源目录完整性复核不受影响）。",
                    "WARNING",
                )
                if outcome.permission_errors:
                    warnings.append(
                        f"权限模式：{len(outcome.permission_errors)} 个路径 chmod 失败（详见日志）。"
                    )
                    logbuf.add_log(task_id, f"警告：{warnings[-1]}", "WARNING")
                    for detail in outcome.permission_errors[:20]:
                        logbuf.add_log(task_id, f"权限模式 chmod 失败：{detail}", "WARNING")
            if outcome.exception:
                errors.append(outcome.exception)
                logbuf.add_log(task_id, f"硬链接阶段失败：{outcome.exception}", "ERROR")
            elif outcome.failed:
                warnings.append(f"{len(outcome.failed)} 个文件未能建立硬链接（详见日志）。")
            # 把本次**实际写了体积记录**的作品同步进每库账本。
            # 跳过的作品不会出现在 ``outcome.logged`` 里 ⇒ 旧行原样保留（时间/体积都不动）；
            # 体积变了或新建了链接的会刷新。孤儿清理删掉的目录，记录一并删除。
            if library is not None and not outcome.exception:
                try:
                    _upsert_link_records(
                        int(library["id"]),
                        source_dir=source_dir,
                        target_dir=target_dir,
                        items=list(outcome.logged),
                    )
                    removed_folders = {
                        os.path.basename(str(path).rstrip("/\\"))
                        for path in outcome.orphans_removed
                        if str(path).strip()
                    }
                    if removed_folders:
                        _drop_link_records(int(library["id"]), removed_folders)
                except Exception as exc:  # noqa: BLE001 - 账本写失败不能拖垮整条任务
                    warnings.append(f"作品账本写入失败：{exc}")
                    logbuf.add_log(
                        task_id,
                        f"警告：{warnings[-1]}（不影响本次结果；下次运行会按旧记录判断。）",
                        "WARNING",
                    )
            if outcome.orphans_removed:
                _spawn_orphan_removal_task(dict(task), library, outcome.orphans_removed)

        # ------------------------------------------------------------ 重命名（配置驱动引擎）
        if do_rename and not errors:
            spec = get_processor(processor_id) if processor_id else None
            if spec is None:
                errors.append(
                    f"媒体库引用的处理器 {processor_id or '（空）'} 不存在，已跳过重命名。"
                    "请在「处理器」页面重新选择一个处理器（配置文件的删除会被同步到数据库）。"
                )
                logbuf.add_log(task_id, errors[-1], "ERROR")
            elif str(spec.get("kind")) == "hardlink_only":
                # hardlink_only 没有任何重命名规则，明确告知而不擅自套用规则。
                warnings.append(
                    f"该库的处理器「{spec.get('name') or processor_id}」是 hardlink_only，"
                    f"按契约只创建硬链接、不执行任何重命名。"
                )
                logbuf.add_log(task_id, warnings[-1], "INFO")
            else:
                log_file = settings.library_log_path(
                    library["log_file"] or f"{processor_id}cache.log"
                )
                # 本库自己的规则覆盖（命名字段/模板、接受与拒绝目录正则、
                # 后缀白名单）。空 = 完全跟随处理器。
                library_override = overrides.read_library_override(
                    library["rule_override"] if "rule_override" in library.keys() else None
                )
                if library_override:
                    logbuf.add_log(
                        task_id,
                        f"本库规则覆盖已生效：{_describe_override(library_override)}",
                        "INFO",
                    )
                outcome_r = engine.run_rename(
                    spec,
                    target_dir,
                    log_file=log_file,
                    folder=folder,
                    folders=work_folders or None,
                    force=force,
                    override=library_override,
                )
                for line in (outcome_r.stdout or []):
                    line = str(line).strip()
                    if line:
                        _log_parsed_line(task_id, line)
                for item in outcome_r.errors:
                    where = str(item.get("dir") or "")
                    name = str(item.get("file") or "")
                    detail = str(item.get("error") or "")
                    logbuf.add_log(
                        task_id,
                        f"重命名失败：{os.path.join(where, name) if name else where} - {detail}",
                        "ERROR",
                    )
                for item in outcome_r.renamed:
                    renamed_samples.add(
                        {"from": str(item.get("old") or ""), "to": str(item.get("new") or "")}
                    )
                for path in outcome_r.kept_unresolved_episode:
                    kept_episode_samples.add(str(path))
                # L2 防丢：冲突清单 —— **WARNING**（不覆盖，由加后缀化解，
                # 但"发生过争名"仍然要显眼）。
                for item in outcome_r.name_collisions:
                    collision_samples.add(dict(item))
                    logbuf.add_log(
                        task_id,
                        "同名冲突："
                        f"{str(item.get('dir') or '')} · 目标名 {str(item.get('target') or '')} ← "
                        f"本次源名 {str(item.get('current') or '')}"
                        f"（先前占用者 {str(item.get('previous') or '')}；"
                        f"kind={str(item.get('kind') or '')}）"
                        "——L2 防丢策略已生效：不覆盖，冲突文件加「 (2)」「 (3)」… 后缀，"
                        "一个都不丢；明细见任务样本 samples.renamed_with_suffix。",
                        "WARNING",
                    )
                # L2 防丢：实际加后缀的文件清单 —— **WARNING**，任务清单按它渲染。
                for item in outcome_r.renamed_with_suffix:
                    suffix_samples.add(dict(item))
                    logbuf.add_log(
                        task_id,
                        "冲突加后缀保留："
                        f"{str(item.get('dir') or '')} · {str(item.get('old') or '')} → "
                        f"{str(item.get('target') or '')}"
                        f"（想要的干净名 {str(item.get('clean') or '')} 已归体积更大的文件，"
                        f"体积 {int(item.get('size') or 0)} 字节；"
                        f"reason={str(item.get('reason') or '')}）",
                        "WARNING",
                    )
                # L2 防丢：目标盘上原有的文件**让位**（它比新来的小）—— INFO。
                for item in outcome_r.displaced_targets:
                    displaced_samples.add(dict(item))
                    logbuf.add_log(
                        task_id,
                        "原有文件让位："
                        f"{str(item.get('dir') or '')} · {str(item.get('old') or '')} → "
                        f"{str(item.get('target') or '')}"
                        "（新来的文件体积更大，旧链接改名让位，未删除、未复制数据）",
                        "INFO",
                    )
                # L4「可见性」：解析失败清单 —— **INFO**（不一定是丢文件，只是"没认出
                # 集数"，命名不引用集数时它照旧改名，不该按 warning 报）。
                for label in outcome_r.episode_unresolved:
                    unresolved_episode_samples.add(str(label))
                    logbuf.add_log(
                        task_id,
                        f"没解析出集数（未因此保留原名）：{label}",
                        "INFO",
                    )
                # L3 特典排除：命中特典判据、被排除改名的散装
                # 文件 —— INFO。逐条的中文日志已由引擎 stdout 的 "Extra (kept): " 行映射
                # （见 _log_parsed_line）；这里收样本，任务详情页按它渲染"这次跳过了哪些文件"。
                for item in outcome_r.extras_classified:
                    extras_samples.add(dict(item))
                    logbuf.add_log(
                        task_id,
                        f"特典保留原名：{str(item.get('dir') or '')} · {str(item.get('name') or '')}"
                        f"（{int(item.get('size') or 0)} 字节；命中判据 "
                        f"{str(item.get('pattern') or '')}，未改名、未移动）",
                        "INFO",
                    )
                # 结构不可识别（没有季目录、文件直接躺在作品目录里）的作品 —— WARNING。这一路
                # 既进任务结果，也进看板待办（否则用户只会觉得"这一部什么都没发生"）。
                for item in outcome_r.unscoped_works:
                    unscoped_work_samples.add(dict(item))
                    logbuf.add_log(
                        task_id,
                        "结构不可识别（缺季目录，本次未改名）："
                        f"{str(item.get('dir') or '')} · {str(item.get('folder') or '')}"
                        f"（直接文件 {int(item.get('files') or 0)} 个："
                        f"{str(item.get('samples') or '')}）",
                        "WARNING",
                    )
                for warning in outcome_r.warnings:
                    warnings.append(warning)
                    logbuf.add_log(task_id, f"警告：{warning}", "WARNING")
                result["renamed"] = len(outcome_r.renamed)
                result["rename_failed"] = len(outcome_r.errors)
                result["kept_unresolved_episode"] = len(outcome_r.kept_unresolved_episode)
                result["name_collisions"] = len(outcome_r.name_collisions)
                result["renamed_with_suffix"] = len(outcome_r.renamed_with_suffix)
                result["displaced_targets"] = len(outcome_r.displaced_targets)
                result["episode_unresolved"] = len(outcome_r.episode_unresolved)
                result["extras_classified"] = len(outcome_r.extras_classified)
                # 命名里用了不存在的变量名 —— 那一段被留空、文件名少一段。
                result["unknown_params"] = len(outcome_r.unknown_params)
                # 结构不可识别（缺季目录）而被整目录跳过的作品数。
                result["unscoped_works"] = len(outcome_r.unscoped_works)
                for item in outcome_r.unknown_params:
                    unknown_param_samples.add(dict(item))
                    logbuf.add_log(
                        task_id,
                        f"命名里的未知变量被留空（文件名少了一段）："
                        f"{str(item.get('label') or '')} · "
                        f"{'、'.join(str(name) for name in item.get('params') or [])}",
                        "WARNING",
                    )
                result["rename_folders_scanned"] = int(outcome_r.folders_scanned)
                if outcome_r.exception:
                    errors.append(outcome_r.exception)
                    logbuf.add_log(task_id, f"重命名阶段失败：{outcome_r.exception}", "ERROR")
                elif outcome_r.errors:
                    warnings.append(f"{len(outcome_r.errors)} 个文件重命名失败（详见日志）。")

    # ---------------------------------------------------------------- 源目录复核
    # 任务结束后复核源树快照。差异 ⇒ 中文告警列出路径（**不改 status**）。
    integrity_ok, changed = verify_source_tree(source_dir, source_snapshot)
    result["source_integrity"] = {"ok": integrity_ok, "changed": changed}
    if not integrity_ok:
        listed = "、".join(changed[:10]) + ("…" if len(changed) > 10 else "")
        warnings.append(
            f"源目录完整性复核失败：任务开始后有 {len(changed)} 个文件与快照不一致"
            f"（{listed}）。本应用不应该改动源目录，请检查是否有外部程序同时改动了 "
            f"{source_dir}。"
        )
        logbuf.add_log(task_id, f"警告：{warnings[-1]}", "WARNING")

    # ---------------------------------------------------------------- 收尾
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["warnings"] = warnings
    result["samples"] = {
        "link_failed": link_failed_samples.items,
        "orphans_removed": orphan_samples.items,
        "renamed": renamed_samples.items,
        "kept_unresolved_episode": kept_episode_samples.items,
        "name_collisions": collision_samples.items,
        "renamed_with_suffix": suffix_samples.items,
        "displaced_targets": displaced_samples.items,
        "episode_unresolved": unresolved_episode_samples.items,
        "extras_classified": extras_samples.items,
        "unknown_params": unknown_param_samples.items,
        "unscoped_works": unscoped_work_samples.items,
    }
    result["samples_truncated"] = bool(
        link_failed_samples.truncated
        or orphan_samples.truncated
        or renamed_samples.truncated
        or kept_episode_samples.truncated
        or collision_samples.truncated
        or suffix_samples.truncated
        or displaced_samples.truncated
        or unresolved_episode_samples.truncated
        or extras_samples.truncated
        or unknown_param_samples.truncated
        or unscoped_work_samples.truncated
    )
    all_failed = result["link_failed"] > 0 and result["linked"] == 0
    if errors or all_failed:
        status = "failed"
    else:
        status = "success"
    error_text = "；".join(errors) if errors else None
    summary = (
        f"任务{('成功' if status == 'success' else '失败')}：新建硬链接 {result['linked']}，"
        f"已存在 {result['already_linked']}，"
        f"跳过文件夹 {result['skipped'] - result['already_linked']}，"
        f"硬链接失败 {result['link_failed']}，重命名 {result['renamed']}，"
        f"保留原名（没解析出集数）{result['kept_unresolved_episode']}，"
        # L4「可见性」：这两个计数也要出现在收尾摘要里（可计数、不必翻样本）。
        f"同名冲突 {result['name_collisions']}，未解析出集数 {result['episode_unresolved']}，"
        # L2 防丢：冲突加后缀 + 原有文件让位 —— 这两项是"没丢文件"的账。
        f"冲突加后缀 {result['renamed_with_suffix']}，原有文件让位 {result['displaced_targets']}，"
        # L3 特典排除：特典排除数也要出现在收尾摘要里（可计数、不必翻样本）。
        f"特典保留原名 {result['extras_classified']}，"
        # 命名里的未知变量（段被留空、文件名少一段）也要可计数。
        f"命名未知变量 {result['unknown_params']}，"
        # 结构不可识别（缺季目录）的作品数 —— 可计数、不必翻样本。
        f"结构不可识别 {result['unscoped_works']}，"
        f"重命名失败 {result['rename_failed']}，孤儿目录清理 {result['orphans_removed']}，"
        f"耗时 {result['duration_ms']}ms"
    )
    logbuf.add_log(task_id, summary, "INFO" if status == "success" else "ERROR")
    for warning in warnings:
        logbuf.add_log(task_id, f"警告：{warning}", "WARNING")

    with tx() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, result=?, error=?, finished_at=? WHERE id=?",
            (
                status,
                json.dumps(result, ensure_ascii=False),
                error_text,
                now_iso(),
                task_id,
            ),
        )
    logbuf.emit_task(task_id, status)
    # 把"值得注意"的情况写进全局待办看板（只读 result、只写 todos 表）。
    # 记不上绝不能影响任务结果 —— 整段吞异常。
    try:
        from .todos import record_task_events

        record_task_events(
            result=result,
            library_id=int(library["id"]),
            library_name=str(library["name"] or ""),
            task_id=int(task_id),
            task_kind=kind,
            status=status,
            error=error_text,
        )
    except Exception:  # pragma: no cover - 待办记录失败不影响任务
        logger.exception("记录待办事件失败")
    try:
        removed = trim_history()
        if removed:
            # 日志清理要看得见 —— 在**本次任务**的日志里留一行，
            # 说清楚删了什么、以及"待办看板的数据不受影响"（todos 表故意没有 tasks 外键）。
            logbuf.add_log(
                task_id,
                f"已按设置项 max_history 裁剪任务历史：删除最旧的 {removed} 条任务记录"
                f"（它们的日志同时删除；待办看板的数据独立保存，不受影响）。",
                "INFO",
            )
    except Exception:  # pragma: no cover - 历史裁剪失败不应影响任务结果
        logger.exception("裁剪任务历史失败")
