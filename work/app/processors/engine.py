"""配置驱动的重命名规则引擎。

契约真源：``docs/contracts/PROCESSORS.md`` §4。执行体就是本模块：输入一份 spec（§3）+ 媒体库的
``target_dir``，按 spec 执行重命名，**规则全部来自配置**。

``preset=legacy`` 是**最朴素的旧口径**预设（§4.1）：每一项配置都等于旧口径的取值，
取值，其余预设不承担这项兼容义务。因此本模块：

* 打印文本固定为 ``Skipping folder`` / ``Renamed`` / ``Error renaming`` /
  ``Folder processed``，``Renamed:`` 行的两侧都是**文件名**（取自 ``os.listdir`` 的结果）；
* 遍历顺序用 ``os.listdir`` 的**原始顺序**（不排序）—— 这关系到两个源文件映射到
  同一个目标名时"谁最后落地"的结果；
* 日志文件读一次、逐文件夹"读-改-写"（``read_log`` + ``write_log``）；
* ``file_scope: title`` 时**不做 ``isfile`` 过滤**（电影语义就没有），
  ``file_scope: subdir`` 时**要**过滤（番剧语义有）；
* 唯一的有意增强：日志写出是原子的（``os.replace``），且条目多一个 ``works_dir`` 键
  （旧日志没有该键也能正常读，§4 第 10 条）。

行为说明（文档：``CHANGELOG.md`` / ``docs/contracts/API.md``）：没解析出集数时
（``episode_patterns`` 全部未命中 **且** ``episode_fallback`` 是 ``None``）不把**字符串** ``"None"``
渲染进文件名，而是**保留原文件名、不改名**，并记进 ``RenameOutcome.kept_unresolved_episode``
+ 打一行 ``Kept (no episode): <标题文件夹>/<子目录>/<原文件名>``。只有当命名真的引用了
``episode`` / ``episode_raw``（见 :func:`naming_uses_episode`）时才生效；只有**显式配置了非 None**
的 ``episode_fallback``（例如内置 ``legacy`` 预设的字符串 ``"None"``）才照旧把那**一个**字面量
渲染进文件名 —— 内置 ``bangumi`` 的**推荐默认**也是 ``None``（保留原名）。

L4「可见性」：引擎额外把清单暴露给执行器/WebUI ——
:attr:`RenameOutcome.kept_unresolved_episode`（保留原名）、
:attr:`RenameOutcome.name_collisions`（改名时目标文件名被占用：同一次运行内更早的文件 /
目标上本来就存在）与 :attr:`RenameOutcome.episode_unresolved`（**所有**没解析出集数的文件，
不论是否因此保留了原名）。

L2「防丢」：**不再有覆盖**。改名之前先由
:func:`_resolve_target_names` 给每个文件定下最终目标名 —— 争同一个名字时**体积最大的拿干净名**
（体积相同按目录列出的先后），其余按体积从大到小依次加 `` (2)``、`` (3)``… 后缀；目标盘上原有
的同名文件若比新来的小，则让它让位到 `` (2)``（同一目录里的 ``os.rename``，不复制数据、不删文件）。
**幂等**：候选后缀路径若已经是本文件自己的 inode（force 重跑），照用、不再叠后缀。每次争名都记进
:attr:`RenameOutcome.name_collisions`（事实）+:attr:`RenameOutcome.renamed_with_suffix`（加后缀）
+:attr:`RenameOutcome.displaced_targets`（原有文件让位），并在任务结果里计数、在 WebUI 里成清单。

L3「特典排除」（**取消"移入 其他/"**）：真实源里大量特典是
**与正片平级的散装文件**（不落在被拒绝的目录里），会参与改名并与正片撞名。判据来自设置项
``extras_patterns``（**正则**数组、逐库生效、可在设置页改；空数组 = 关闭）：对**当前作用域目录里
通过后缀白名单的直接文件**做 ``re.search(pattern, filename)``（大小写不敏感）。命中者**不套命名
模板、不改名、也不移动**：保持原名留在原位置（硬链接阶段已经建好的目标侧文件一个字节都不动）。
绝不递归子目录，所以 ``Season N/特典/`` 里的文件一个都不碰。命中者先从待改名集合里剔除，因此不会
跟正片抢干净名、也不会进 :attr:`RenameOutcome.episode_unresolved`；本次排除的清单记进
:attr:`RenameOutcome.extras_classified`（形状 ``{"dir", "name", "size", "pattern"}``）。

平台注记：L2 落地前，POSIX 的 ``os.rename`` 会**静默覆盖**已有的目标文件（Windows 上则抛
``FileExistsError``/WinError 183，那个文件保留原名并记进 :attr:`RenameOutcome.errors`）。
L2 之后两种平台都不再覆盖：目标名在改名**之前**就解析成互不相同的名字。
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app import db

from .logs import ensure_json_cache, library_log_path, read_json_cache, remove_entry, write_json_cache
from . import overrides
from .spec import (
    ProcessorSpec,
    SpecValidationError,
    normalize_episode_fallback,
    normalize_spec,
    validate_spec,
)

__all__ = [
    "RenameOutcome",
    "run_rename",
    "render_segments",
    "sample_context",
    "naming_uses_episode",
    "folder_size",
    "ass_suffix",
    "format_stamp",
    # L3 特典文件分类：读取/归一的公开入口（routers/settings.py 与执行器都要用）。
    "DEFAULT_EXTRAS_PATTERNS",
    "EXTRAS_PATTERNS_KEY",
    "normalize_extras_patterns",
    "read_global_extras_patterns",
]

#: 命名里引用"集数"的参数名：只有命名真的写了集数，才需要"没解析出集数就保留原名"。
EPISODE_PARAMS: tuple[str, ...] = ("episode", "episode_raw")

#: ``Folder processed`` / 日志 ``timestamp`` 的时间格式（``strftime("%Y-%m-%d %H:%M:%S")``）。
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class RenameOutcome:
    """一次重命名运行的结果（§4 的"输出结构" + 服务端需要的补充字段）。"""

    renamed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    #: 命名要写集数、但这个文件没解析出集数（episode_patterns 全不命中且
    #: ``episode_fallback`` 没配）⇒ 保留硬链接阶段用的原文件名、不执行 ``os.rename``。
    #: 条目是相对媒体库根目录的路径（``标题文件夹/子目录/原文件名``）。
    kept_unresolved_episode: list[str] = field(default_factory=list)
    #: L4「可见性」：改名时**目标文件名已被占用**的每一条记录。条目形状见
    #: :func:`_collision_entry`：``{"dir", "target", "previous", "current", "kind"}``，
    #: ``kind`` 取 ``"within_run"``（本次运行中更早的某个文件已经占用同一个目标名）
    #: 或 ``"existing_target"``（目标路径本来就存在、本次要占它的名字）。
    #: L2 落地后**不再有覆盖**：冲突由 :func:`_resolve_target_names` 加后缀化解，这一份清单
    #: 记录的是"发生过争名"这个事实。
    name_collisions: list[dict[str, Any]] = field(default_factory=list)
    #: L2 防丢：因为目标名被占用而**加了 `` (2)``/`` (3)``… 后缀**的每一条记录
    #: （策略：正片优先、其余加后缀保留、一个都不丢）。条目形状见
    #: :func:`_suffix_entry`：``{"dir", "old", "target", "clean", "size", "reason", "displaced"}``；
    #: ``reason`` 取 ``"within_run"``（本次运行里更小的那个文件）或 ``"existing_target"``
    #: （目标盘上已有的同名文件比新来的大 ⇒ 新来的自己加后缀）。
    renamed_with_suffix: list[dict[str, Any]] = field(default_factory=list)
    #: L2 防丢：**目标盘上原有的文件让位**给体积更大的新文件的每一条记录
    #: （``{"dir", "old", "target", "clean", "size"}``）—— 让位是 `os.rename` 一个硬链接，
    #: 不复制数据、不删文件。
    displaced_targets: list[dict[str, Any]] = field(default_factory=list)
    #: L4「可见性」：所有 ``episode_resolved is False`` 的文件（**不论是否因此保留了
    #: 原名**）。条目形式与 :attr:`kept_unresolved_episode` 相同（``标题文件夹/子目录/文件名``）。
    #: 为什么要单独记这一份：命名模板**不引用集数**时（例如 ``movie`` 的 ``标题.后缀``）这类文件
    #: 照旧改名，解析失败就会**静默消失**；有了这份清单，"没认出集数"在任何规格下都可见。
    episode_unresolved: list[str] = field(default_factory=list)
    #: L3 特典排除（命中者留在原位置，不移动）：命中特典判据、**被排除在改名
    #: 之外**的散装文件（``{"dir", "name", "size", "pattern"}``：``dir`` = ``作品名/Season N``
    #: 这样的作用域标签，``name`` = 原文件名（也是它留在原位置的名字），``size`` = 字节，
    #: ``pattern`` = 命中的那条判据）。这些文件**不改名、不移动**，所以不记进 :attr:`renamed`，
    #: 也不进 :attr:`episode_unresolved`。
    extras_classified: list[dict[str, Any]] = field(default_factory=list)
    #: 命名里用到了**上下文里不存在的变量名**的每一条记录
    #: （``{"label", "file", "params"}``：``label`` = ``标题/子目录/文件名`` 作用域标签，
    #: ``file`` = 原文件名，``params`` = 这文件名里被留空的那几个名字，去重后升序）。
    #: 这些段什么都不输出 ⇒ 文件名**少了一段**，但改名照样 success（这类文件会在此留痕）。
    unknown_params: list[dict[str, Any]] = field(default_factory=list)
    #: **结构不可识别**的作品 —— 子目录模式（``file_scope: subdir``）下该作品
    #: 目录里一个可接受的子目录都没有，却**直接躺着**本规格要处理的文件（扁平结构）。
    #: 引擎不会给它们改名（作用域是"作品/季目录"，扁平结构连作用域都不算），而且这一路
    #: **既不报错、也进不了** :attr:`kept_unresolved_episode` —— 用户只会觉得"什么都没发生"。
    #: 条目形状 ``{"dir", "folder", "files", "samples"}``：``dir`` = 作品目录（绝对路径），
    #: ``folder`` = 作品名，``files`` = 目录里直接文件数，``samples`` = 前几个文件名（便于一眼
    #: 认出来是哪部作品）。命中"特典判据"的文件不算在内（那些文件本来就不需要改名）。
    unscoped_works: list[dict[str, Any]] = field(default_factory=list)
    #: **运行期记账，不参与判状态**：绝对目标路径 → 该目标当前的标签（``标题/子目录/文件名``），
    #: 只在一次 :func:`run_rename` 内累积，用来判定 ``within_run`` 争名与给出 ``previous``。
    rendered_targets: dict[str, str] = field(default_factory=dict)
    folders_scanned: int = 0
    folders_processed: int = 0
    warnings: list[str] = field(default_factory=list)
    log_file: str = ""
    stdout: list[str] = field(default_factory=list)
    exception: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "renamed": list(self.renamed),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "kept_unresolved_episode": list(self.kept_unresolved_episode),
            "name_collisions": list(self.name_collisions),
            "renamed_with_suffix": list(self.renamed_with_suffix),
            "displaced_targets": list(self.displaced_targets),
            "episode_unresolved": list(self.episode_unresolved),
            "extras_classified": list(self.extras_classified),
            "unknown_params": list(self.unknown_params),
            "unscoped_works": list(self.unscoped_works),
            "folders_scanned": self.folders_scanned,
            "folders_processed": self.folders_processed,
            "warnings": list(self.warnings),
            "log_file": self.log_file,
            "exception": self.exception,
        }


def format_stamp(moment: datetime | None = None) -> str:
    """``YYYY-MM-DD HH:MM:SS``（本地时区），即 ``datetime.now().strftime`` 的默认格式。"""
    return (moment or datetime.now()).strftime(STAMP_FORMAT)


def folder_size(path: str) -> int:
    """``os.walk`` 累加所有文件大小（复刻 ``get_folder_size``）。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            total += os.path.getsize(os.path.join(dirpath, name))
    return total


def ass_suffix(filename: str) -> str:
    """``.ass`` 多段后缀规则（``.chs.ass``）。

    非 ``.ass`` 文件不走这里（调用点用 ``ext`` 代替）。
    """
    lowered = filename.lower()
    cut = lowered.rfind(".ass")
    before = filename[: cut if cut != -1 else len(filename)]
    last_dot = before.rfind(".")
    if last_dot != -1:
        return filename[last_dot:]
    return ".ass"


# ── 内部：正则规则 ──────────────────────────────────────────────────────


@dataclass
class _Rule:
    pattern: str
    group: int
    regex: re.Pattern[str]


def _compile_rules(rules: Any, *, with_group: bool) -> list[_Rule]:
    compiled: list[_Rule] = []
    for item in rules or []:
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            regex = re.compile(pattern)
        except re.error:
            continue
        group = item.get("season_group", 1) if with_group else 0
        compiled.append(_Rule(pattern=pattern, group=int(group or 0), regex=regex))
    return compiled


def _rule_hit(rules: list[_Rule], name: str) -> _Rule | None:
    """命中即返回（§4 用 ``search``，规则串里的 ``^`` / ``$`` 需要自己写）。"""
    for rule in rules:
        if rule.regex.search(name):
            return rule
    return None


# ── 内部：剧集号 ────────────────────────────────────────────────────────


def _episode_raw(
    filename: str,
    patterns: list[dict[str, Any]],
    fallback: Any,
) -> Any:
    """按 ``episode_patterns`` 的书写顺序取**首个命中的首个非空捕获组**（复刻 get_episode_numbers）。"""
    for item in patterns or []:
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            regex = re.compile(pattern)
        except re.error:
            continue
        match = regex.search(filename)
        if not match:
            continue
        for _name, index in sorted(match.re.groupindex.items(), key=lambda kv: kv[1]):
            value = match.group(index)
            if value:
                return value
    if fallback is None:
        # 行为：这里返回**字符串** "None" 时，没认出集数的文件名里会出现 `S01ENone`，
        # 同一作品的多个压制版本会撞成同一个目标名
        # 互相覆盖。因此返回 None = "没解析出集数"，由 _file_context 标成
        # ``episode_resolved=False``，再由 _process_scope 保留原文件名（不改名）。
        # 显式配置了 ``episode_fallback``（非 None）时照旧返回该值，属于"已解析"。
        return None
    return fallback


def naming_uses_episode(segments: list[dict[str, Any]] | None) -> bool:
    """命名段序列里是否引用了集数参数（``episode`` / ``episode_raw``）。

    只有命名**真的把集数写进文件名**时，"没解析出集数"才会影响改名 ——
    movie 处理器的「标题.后缀」不引用集数，于是它有没有解析出集数都不影响结果。
    """
    for segment in segments or []:
        if isinstance(segment, dict) and segment.get("param") in EPISODE_PARAMS:
            return True
    return False


def _scope_label(title: str, rel_dir: str, filename: str) -> str:
    """``<标题文件夹>/<子目录>/<文件名>``（相对媒体库根目录，与文档里的路径写法一致）。"""
    return "/".join(part for part in (title, rel_dir, filename) if part)


def _collision_entry(
    *,
    rel_dir: str,
    new_name: str,
    previous: str,
    filename: str,
    kind: str,
) -> dict[str, Any]:
    """一条"目标文件名已被占用"的样本（L4「可见性」）。

    * ``dir``：**作用域标签**，子目录模式是 ``作品名/Season 1``，``file_scope: title`` 时是空串；
    * ``target``：新的文件名（本次渲染出来的目标**文件名**，不是完整路径）；
    * ``previous``：先前占用该目标的源文件名 —— ``kind="within_run"`` 时是"上一次改名的源文件名"，
      ``kind="existing_target"`` 时是"目标上原本的文件名"（认不出来就是空串）；
    * ``current``：本次被改名的源文件名；
    * ``kind``：``"within_run"`` | ``"existing_target"``（语义见 :attr:`RenameOutcome.name_collisions`）。
    """
    return {
        "dir": rel_dir,
        "target": new_name,
        "previous": previous,
        "current": filename,
        "kind": kind,
    }


# ── 内部：L2「争名不丢文件」 ────────────────────────────────────────────

#: L2：冲突文件加后缀的模板（模板形如 ``Show S01E01 (2).mkv``）。
SUFFIX_TEMPLATE = " ({index})"

#: L2：后缀序号上限（防呆；真到 999 说明目录里同名文件多到不正常，宁可报错也不覆盖）。
MAX_SUFFIX_INDEX = 999


def _norm(path: str) -> str:
    """路径比较用的规范形式（Windows 大小写不敏感，反斜杠统一）。"""
    return os.path.normcase(os.path.abspath(path))


def _split_name(name: str) -> tuple[str, str]:
    """把文件名拆成 ``(主干, 后缀)``；后缀取**最后一段点号后缀**（与 ``name.split(".")[-1]`` 同口径）。"""
    cut = name.rfind(".")
    if cut <= 0:
        return name, ""
    return name[:cut], name[cut:]


def _suffixed_name(clean_name: str, index: int) -> str:
    """``Show S01E01.mkv`` + 2 ⇒ ``Show S01E01 (2).mkv``。"""
    stem, ext = _split_name(clean_name)
    return f"{stem}{SUFFIX_TEMPLATE.format(index=index)}{ext}"


def _same_file(left: str, right: str) -> bool:
    """两个路径是不是**同一个 inode**（硬链接重复运行的判据）。任一路径不存在 ⇒ False。"""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _is_free_target(path: str, old_path: str) -> bool:
    """``path`` 能不能直接给 ``old_path`` 这个文件用。

    路径不存在 ⇒ 能；路径存在但**与本文件是同一个 inode** ⇒ 也能（force 重跑时"已经就位"，
    不能再叠一层后缀；这一步就是 L2 的幂等判据）。
    """
    if not os.path.lexists(path):
        return True
    return _same_file(path, old_path)


def _suffix_entry(
    *,
    rel_dir: str,
    filename: str,
    clean: str,
    target: str,
    size: int,
    reason: str,
    displaced: str = "",
) -> dict[str, Any]:
    """一条"因为争名而加了后缀"的样本（L2）。

    * ``dir``：作用域标签（子目录模式是 ``作品名/Season 1``）；
    * ``old``：源文件名（本次被改名的那个）；
    * ``target``：**最终**文件名（带 `` (N)``）；
    * ``clean``：它本来想要、但被占用的"干净名"；
    * ``size``：源文件体积（字节，"正片优先"的比较依据）；
    * ``reason``：``"within_run"``（同一次运行里比它更大的文件拿了干净名）|
      ``"existing_target"``（目标盘上原有的同名文件比它大）；
    * ``displaced``：预留字段，**实际恒为空串** —— "自己加后缀"与"逼占位者让位"是互斥的两条路
      （对方让位时赢家直接拿干净名、不会再有本条记录）；让位的事实见
      `RenameOutcome.displaced_targets` / ``Task.result.samples.displaced_targets``。
    """
    return {
        "dir": rel_dir,
        "old": filename,
        "target": target,
        "clean": clean,
        "size": size,
        "reason": reason,
        "displaced": displaced,
    }


def _pick_suffixed(
    *,
    clean: str,
    old_path: str,
    scope_dir: str,
    claimed: set[str],
) -> str | None:
    """按 `` (2)``、`` (3)``… 的顺序找一个可用的文件名；全被占满 ⇒ ``None``。

    可用 = 本次运行还没认领、且（不存在 **或** 已经是本文件自己的 inode）。
    """
    for index in range(2, MAX_SUFFIX_INDEX + 1):
        candidate = _suffixed_name(clean, index)
        path = os.path.join(scope_dir, candidate)
        if _norm(path) in claimed:
            continue
        if not _is_free_target(path, old_path):
            continue
        claimed.add(_norm(path))
        return candidate
    return None


def _displace_target(
    *,
    clean: str,
    scope_dir: str,
    claimed: set[str],
) -> str | None:
    """把**目标盘上原有的**占位文件让位到 `` (N)``（它体积更小、正片要它的名字）。

    只对同一个目录里的硬链接做 ``os.rename``（不复制数据、不删文件）。返回占位者的新文件名，
    找不到空位或改名失败 ⇒ ``None``（调用方会改为"新来的自己加后缀"，一个都不丢）。
    """
    occupier = os.path.join(scope_dir, clean)
    for index in range(2, MAX_SUFFIX_INDEX + 1):
        candidate = _suffixed_name(clean, index)
        path = os.path.join(scope_dir, candidate)
        if _norm(path) in claimed or os.path.lexists(path):
            continue
        try:
            os.rename(occupier, path)
        except OSError:
            return None
        claimed.add(_norm(path))
        return candidate
    return None


def _resolve_target_names(
    ops: list[dict[str, Any]],
    *,
    scope_dir: str,
    rel_dir: str,
    outcome: RenameOutcome,
    emit: Callable[[str], None],
) -> list[dict[str, Any]]:
    """给本作用域里每个待改名文件定下**最终目标文件名**（L2 防丢策略）。

    规则：

    * 同一次运行里撞同一个目标名的多个文件 —— **体积最大的拿"干净名"**，其余按体积从大到小
      依次拿 `` (2)``、`` (3)``…（体积相同则按目录里列出的先后，保证可重复）。
    * 目标盘上**本来就存在**同名文件时也**比体积**：新来的更大 ⇒ 旧的那个让位到 `` (2)``、新来的
      拿干净名；新来的更小或一样大 ⇒ 新来的自己加后缀。**一个文件都不丢、不覆盖。**
    * 幂等：候选后缀路径若已经是**本文件自己**（同一 inode），就照用，不再往上叠；而且这种
      "已经在位"的情况**不记冲突、也不记"加后缀保留"**（否则每次重跑都会往任务清单和
      首页待办看板里灌假事件）。
    * 让位的原有文件如果**本身也在这批候选里**（它就在目标目录、扩展名也合格），本次改名
      **跳过它**：``_displace_target`` 已经替它改过一次名了，再走一遍只会拿到一条已经不在的
      原路径（假"加后缀"样本 + 假 ``rename_failed``）。

    返回按"执行顺序"排好的 ``ops``（每项多一个 ``"final"`` 键 = 最终文件名）。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for op in ops:
        groups.setdefault(op["clean"], []).append(op)

    claimed: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for clean, members in groups.items():
        members.sort(key=lambda item: (-item["size"], item["order"]))
        clean_path = os.path.join(scope_dir, clean)
        winner = members[0]
        take_clean = True
        displaced_name = ""
        if not _is_free_target(clean_path, winner["old"]):
            # 目标盘上有个"别的文件"占着这个名字 —— 比体积（L2 规则）
            try:
                occupier_size = os.path.getsize(clean_path)
            except OSError:
                occupier_size = None
            if occupier_size is not None and occupier_size < winner["size"]:
                displaced_name = _displace_target(
                    clean=clean, scope_dir=scope_dir, claimed=claimed
                ) or ""
                if displaced_name:
                    # 让位者本身可能也在这批候选里（它就躺在目标目录里、扩展名也合格）：
                    # 它刚被 ``_displace_target`` 改名到 `` (N)``，**不能再走一遍**下面
                    # "挑后缀 + os.rename" 那条路 —— 那条路会拿着已经不存在的原路径去改名，
                    # 只会多记一条假的"加后缀"样本 + 一条 rename_failed。
                    for member in members:
                        if member["old_name"] == clean:
                            member["skip"] = True
            if not displaced_name:
                take_clean = False
            outcome.name_collisions.append(
                _collision_entry(
                    rel_dir=rel_dir,
                    new_name=clean,
                    previous=clean,
                    filename=winner["old_name"],
                    kind="existing_target",
                )
            )
        if take_clean:
            winner["final"] = clean
            claimed.add(_norm(clean_path))
        else:
            picked = _pick_suffixed(
                clean=clean, old_path=winner["old"], scope_dir=scope_dir, claimed=claimed
            )
            if picked is None:
                winner["final"] = None
            elif picked == winner["old_name"]:
                # 幂等：这个文件已经坐在 `` (N)`` 上（同一 inode）⇒ 照用，不记账。
                winner["final"] = picked
                winner["in_place"] = True
            else:
                winner["final"] = picked
                outcome.renamed_with_suffix.append(
                    _suffix_entry(
                        rel_dir=rel_dir,
                        filename=winner["old_name"],
                        clean=clean,
                        target=picked,
                        size=winner["size"],
                        reason="existing_target",
                    )
                )
        if displaced_name:
            outcome.displaced_targets.append(
                {
                    "dir": rel_dir,
                    "old": clean,
                    "target": displaced_name,
                    "clean": clean,
                    "size": winner["size"],
                }
            )
            emit(f"Displaced: {clean} -> {displaced_name}")
        ordered.append(winner)
        for op in members[1:]:
            if op.get("skip"):
                # 上面已经让它位到 `` (N)`` 了（见 ``_displace_target``）：这次改名不再处理它，
                # 也不记任何账 —— 让位的事实已经记在 ``displaced_targets`` 里了。
                continue
            picked = _pick_suffixed(
                clean=clean, old_path=op["old"], scope_dir=scope_dir, claimed=claimed
            )
            if picked is None:
                op["final"] = None
            elif picked == op["old_name"]:
                # 幂等重跑：这个文件**已经**坐在它该坐的 `` (N)`` 位置上（同一 inode），
                # 既没被顶掉、也没新加后缀 ⇒ 不记冲突、不记"加后缀保留"。
                op["final"] = picked
                op["in_place"] = True
            else:
                op["final"] = picked
                outcome.renamed_with_suffix.append(
                    _suffix_entry(
                        rel_dir=rel_dir,
                        filename=op["old_name"],
                        clean=clean,
                        target=picked,
                        size=op["size"],
                        reason="within_run",
                    )
                )
                outcome.name_collisions.append(
                    _collision_entry(
                        rel_dir=rel_dir,
                        new_name=clean,
                        previous=winner["old_name"],
                        filename=op["old_name"],
                        kind="within_run",
                    )
                )
            ordered.append(op)
    # 执行顺序仍是"目录里列出的先后"（与旧行为一致），只有**争名归属**按体积决定。
    return sorted(ordered, key=lambda item: item["order"])


# ── L3「特典文件分类」 ─────────────────────────────────────────────────────

#: L3：特典判据的设置键（全局设置 ``settings.extras_patterns``）。
EXTRAS_PATTERNS_KEY = "extras_patterns"

#: L3：出厂默认判据（**OAD 不做** —— 字符太短、容易撞正片文件名）。
#: ``CM集`` 也会命中真实源里 ``… CM集 (BD …).mkv`` 这类 PV/CM 合集：
#: 这类 PV/CM 合集在真实媒体库里的表现：命中 5 个文件、其中 0 个能被集号规则解析 ⇒ 零误伤。
#: 这些是**正则**，大小写不敏感地 ``re.search`` 文件名；``[]`` = 关闭 L3。
DEFAULT_EXTRAS_PATTERNS: tuple[str, ...] = (
    "NCOP",
    "NCED",
    "Preview",
    "Creditless",
    "Info",
    "予告編",
    "CM集",
)


def normalize_extras_patterns(value: Any) -> list[str]:
    """把设置里的特典判据归一化成**去重保序**的正则字符串列表。

    容忍：``None``/空串 ⇒ 出厂默认（L3 **默认开启**，与"后缀白名单未设置"不同）；字符串先按
    JSON 解（设置表里存的就是 JSON 数组；解不出来 ⇒ 整串当成**一条**判据，别静默丢掉）；
    ``list`` ⇒ 逐项 ``strip``、丢空项、按字面去重；其它类型 ⇒ 出厂默认。**绝不做拆分**：
    正则里逗号、空格、点号都有意义，``"NCOP,NCED"`` 是一条正则而不是两条 —— 想要两条就传
    数组两项（WebUI 一行一条）。

    **不在这里编译**：坏正则由 :func:`_compile_extras_rules` 跳过并留一条可计数告警，
    这样读取/回显（``GET /api/settings``）永远与用户在设置页里看到的字面值一致。
    """
    if value is None:
        return list(DEFAULT_EXTRAS_PATTERNS)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return list(DEFAULT_EXTRAS_PATTERNS)
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return [text]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_EXTRAS_PATTERNS)
    items: list[Any] = list(value)
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        pattern = item.strip()
        if pattern and pattern not in result:
            result.append(pattern)
    return result


def read_global_extras_patterns(stored: str | None = None) -> list[str]:
    """读全局特典判据（设置键 :data:`EXTRAS_PATTERNS_KEY`）。

    通路与 :func:`app.processors.overrides.read_global_extensions` 完全一致（``stored`` 缺省时
    查 ``settings`` 表；表不存在、DB 不可用 ⇒ 兜底），**只有兜底值不同**：后缀白名单的兜底是
    "空 = 未设置"，而 L3 的兜底是出厂 6 条 —— 出厂默认行为就是**开启**，``[]`` 才是关闭。
    """
    if stored is None:
        try:
            stored = db.get_setting(EXTRAS_PATTERNS_KEY)
        except (sqlite3.Error, RuntimeError):
            return list(DEFAULT_EXTRAS_PATTERNS)
    return normalize_extras_patterns(stored)


def _compile_extras_rules(patterns: list[str], outcome: RenameOutcome) -> list[re.Pattern[str]]:
    """把特典判据编译成 ``re.IGNORECASE`` 正则；坏正则跳过并留一条可计数告警。"""
    rules: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            rules.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            outcome.warnings.append(
                f"设置项 {EXTRAS_PATTERNS_KEY} 里的正则 {pattern!r} 无法编译，本次已忽略它：{exc}"
            )
    return rules


def _extras_hit(filename: str, rules: list[re.Pattern[str]]) -> str:
    """文件名命中了哪一条特典判据（``re.search``、大小写不敏感）；没命中 ⇒ 空串。"""
    for rule in rules:
        if rule.search(filename):
            return rule.pattern
    return ""


# ── 内部：上下文与命名 ──────────────────────────────────────────────────


def _zfill(value: Any, width: int) -> str:
    return str(value).zfill(int(width))


def _eval_env(context: dict[str, Any]) -> dict[str, Any]:
    env: dict[str, Any] = {
        "__builtins__": {},
        "re": re,
        "os": os,
        "len": len,
        "str": str,
        "int": int,
        "sorted": sorted,
        "min": min,
        "max": max,
        "zfill": _zfill,
        "splitext": os.path.splitext,
        "basename": os.path.basename,
        "ass_suffix": ass_suffix,
    }
    env.update(context)
    return env


def _file_context(
    *,
    title: str,
    filename: str,
    season_raw: str,
    scope_mode: str,
    rel_dir: str,
    works_dir: str,
    patterns: list[dict[str, Any]],
    fallback: Any,
) -> dict[str, Any]:
    stem, ext = os.path.splitext(filename)
    extname = filename.split(".")[-1] if "." in filename else ""
    suffix = ass_suffix(filename) if filename.lower().endswith(".ass") else ext
    raw = _episode_raw(filename, patterns, fallback)
    # 没解析出集数时 episode_raw / episode 都留空 —— 字符串 "None" 不进入渲染路径。
    # ``episode_resolved`` 是引擎内部键（不进 TEMPLATE_VARS / WebUI 变量表），表达式与段
    # 也能读到它，用来区分"真的没解析出集数"。
    episode_resolved = raw is not None
    return {
        "title": title,
        "filename": filename,
        "stem": stem,
        "ext": ext,
        "extname": extname,
        "season_raw": season_raw,
        "season": season_raw.zfill(2) if scope_mode == "subdir" else "",
        "episode_raw": raw if episode_resolved else "",
        "episode": str(raw).zfill(2) if episode_resolved else "",
        "episode_resolved": episode_resolved,
        "suffix": suffix,
        "works_dir": works_dir or "",
        "rel_dir": rel_dir,
    }


def render_segments(
    segments: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    dropped: list[str] | None = None,
) -> str:
    """按段序列拼出文件名（``param`` 先查 expressions（已并入 context）再查上下文表）。

    ``dropped``：传一个列表进来，遇到"上下文里没有这个 param"时把名字追加进去，
    调用方据此记 warning / 任务结果。不传则只是照旧留空 —— 渲染本身永不抛异常。
    """
    parts: list[str] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        if "literal" in segment:
            parts.append(str(segment.get("literal") or ""))
            continue
        name = segment.get("param")
        value = context.get(name)
        if value is None:
            # 未知参数：留空并在调用方记 warning（不静默编一个看起来对的值）
            if dropped is not None:
                dropped.append(str(name))
            continue
        text = str(value)
        pad = segment.get("pad")
        if pad:
            text = _zfill(text, pad)
        parts.append(text)
    return "".join(parts)


def sample_context(
    *,
    title: str = "作品名",
    filename: str = "作品名 第01集.mkv",
    season_raw: str = "1",
    scope_mode: str = "subdir",
    rel_dir: str = "",
    works_dir: str = "",
) -> dict[str, Any]:
    """给 WebUI 的"实时预览"用的示例上下文（§8）。"""
    patterns = [
        {
            "name": "ep",
            "pattern": r"(?i)(?:第\s*(?P<g1>\d{1,2})\s*[集话話回]|\[\s*(?P<g2>\d{1,2})\s*\]"
            r"|Episode\s*(?P<g3>\d{1,2})|EP\s*(?P<g4>\d{1,2})|e(?P<g5>\d{1,2})"
            r"|E(?P<g6>\d{1,2})|\#\s*(?P<g7>\d{1,2})|-\s*(?P<g8>\d{1,2}))",
        },
    ]
    return _file_context(
        title=title,
        filename=filename,
        season_raw=season_raw,
        scope_mode=scope_mode,
        rel_dir=rel_dir,
        works_dir=works_dir,
        patterns=patterns,
        fallback="None",
    )


# ── 主流程 ──────────────────────────────────────────────────────────────


class _Printer:
    """同时写真实 stdout 与 ``outcome.stdout``（执行器只消费后者，避免重复记日志）。"""

    def __init__(self, outcome: RenameOutcome) -> None:
        self._outcome = outcome

    def __call__(self, line: str) -> None:
        self._outcome.stdout.append(line)
        print(line)


def _resolve_spec(spec: dict[str, Any] | ProcessorSpec) -> dict[str, Any]:
    if isinstance(spec, ProcessorSpec):
        return spec.to_dict()
    return normalize_spec(copy.deepcopy(spec))


def run_rename(
    spec: dict[str, Any] | ProcessorSpec,
    root: str,
    *,
    log_file: str | None = None,
    folder: str | None = None,
    folders: list[str] | None = None,
    force: bool = False,
    now: datetime | None = None,
    works_dir: str = "",
    override: dict[str, Any] | None = None,
) -> RenameOutcome:
    """对 ``root`` 下的标题文件夹执行一次重命名。

    :param spec: 处理器规格（dict 或 :class:`~app.processors.spec.ProcessorSpec`）
    :param root: 该媒体库的 ``target_dir``
    :param log_file: 日志文件**完整路径**；缺省时按 spec 的 ``log_file`` + ``settings.log_dir`` 解析
    :param folder: 只处理这一个标题文件夹（``None`` = 全部）
    :param folders: 只处理这一批标题文件夹（``scope.mode=works``：WebUI 里勾选若干作品
        后"立即重命名"）。与 ``folder`` 同时给出时以它为准；``force`` 会逐个清掉这些作品的
        "已处理"条目 —— 这正是"手动重命名只作用于选中作品"的实现方式。
    :param force: 忽略"已处理"判据并删掉该文件夹的旧日志条目后重跑
    :param now: 注入时间（测试用）
    :param works_dir: 写进日志条目的 ``works_dir``（= 媒体库的 ``source_dir``）
    :param override: 该媒体库自己的规则覆盖（``libraries.rule_override``）；
        只覆盖用户显式填过的字段，其余沿用处理器规格。
    """
    outcome = RenameOutcome()
    emit = _Printer(outcome)

    data = _resolve_spec(spec)
    # 库级规则覆盖（libraries.rule_override）在**校验之前**合并进来，
    # 于是"用户写的规则"与"处理器里的规则"共享同一份判定：覆盖造成的跨层冲突
    # （例如 file_scope=title 却留着 accept_dirs）会被下面的 validate_spec 抓住。
    data = overrides.apply_override(data, override)
    # **运行期不因为"命名里有未知变量名"而拒绝整份规格** —— 库里既有存下的
    # 配置、人工改过的 YAML 都要能照旧跑完；那些名字由 render_segments(dropped=…) 留痕
    # （result.unknown_params + warnings）。保存路径仍然严格（strict_names 默认 True）。
    errors = validate_spec(data, strict_names=False)
    if errors:
        outcome.exception = str(SpecValidationError(errors))
        outcome.warnings.append("处理器规格不合法，已放弃本次重命名。")
        return outcome
    if data.get("kind") != "rename":
        outcome.warnings.append(
            f"处理器 {data.get('id')!r} 的 kind 是 {data.get('kind')!r}，只建硬链接、不做重命名。"
        )
        return outcome

    traverse = data["traverse"]
    scope_mode = str(traverse.get("file_scope", "subdir"))
    accept_rules = _compile_rules(traverse.get("accept_dirs"), with_group=True)
    reject_rules = _compile_rules(traverse.get("reject_dirs"), with_group=False)
    skip_season_zero = bool(traverse.get("skip_season_zero", True))
    extensions = tuple(
        overrides.effective_extensions(
            data["extensions"], (override or {}).get("extensions")
        )
    )
    expressions: dict[str, Any] = data["naming"].get("expressions") or {}
    segments = data["naming"].get("segments") or []
    patterns = data["episode_patterns"]
    # 空串 / 纯空白串等同于"没配兜底"。库里可能存着由 WebUI
    # 清空输入框写入的 ""，这里再归一一次，免得这类数据继续渲染出 ``…S01E00.mkv``。
    fallback = normalize_episode_fallback(data["episode_fallback"])
    per = str(data["selection"].get("per", "file"))
    # 命名里没引用集数（例如 movie 的「标题.后缀」）时，"没解析出集数"不影响改名。
    uses_episode = naming_uses_episode(segments)
    # L3 特典文件分类：全局特典判据（设置项 extras_patterns，正则、逐库生效）。
    # 与 rename_extensions 走**同一条通路**：每轮运行读一次设置表（读不到就用出厂默认 7 条）。
    extras_rules = _compile_extras_rules(read_global_extras_patterns(), outcome)

    target_log = log_file or str(library_log_path(data["log_file"]))
    outcome.log_file = str(target_log)
    try:
        ensure_json_cache(target_log)
    except OSError as exc:
        outcome.exception = f"无法创建重命名日志 {target_log}：{exc}"
        return outcome

    # 整个运行**只读一次**日志（两类处理器都是如此）
    cache = read_json_cache(target_log)
    if not isinstance(cache, dict):
        cache = {}

    # 名字写成"作品清单"是因为 WebUI 里勾选的就是第一层文件夹名（= 作品名）。
    scoped = [str(name) for name in (folders or []) if str(name or "").strip()]
    if not scoped and folder:
        scoped = [folder]

    if force:
        if scoped:
            # 逐个清掉这些作品的"已处理"条目：手动重命名/重建链只作用于选中作品，
            # 其它作品一个字节都不动（与旧的"整库 force = 删掉范围内所有记录"等价，
            # 只是范围由 WebUI 显式给出）。
            for name in scoped:
                try:
                    remove_entry(target_log, name)
                except OSError:
                    pass
                cache.pop(name, None)
        else:
            outcome.warnings.append("force=true：忽略所有『已处理』判据，重新处理每个标题文件夹。")

    try:
        entries = os.listdir(root)
    except OSError as exc:
        outcome.exception = f"无法读取媒体库目录 {root}：{exc}"
        return outcome

    if scoped:
        present = [name for name in scoped if name in entries]
        missing = [name for name in scoped if name not in entries]
        if missing:
            if len(missing) == 1 and not present:
                outcome.warnings.append(
                    f"指定文件夹 {missing[0]} 不在 {root} 下：只遍历媒体库根目录的第一层标题文件夹，本次没有任何改动。"
                )
                return outcome
            outcome.warnings.append(
                f"指定文件夹 {', '.join(missing)} 不在 {root} 下：只遍历媒体库根目录的第一层标题文件夹，"
                "这些作品本次没有任何改动。"
            )
        if not present:
            return outcome
        entries = present

    for title in entries:
        full = os.path.join(root, title)
        if not os.path.isdir(full):
            continue
        outcome.folders_scanned += 1

        hit = _rule_hit(reject_rules, title)
        if hit is not None:
            outcome.warnings.append(
                f"跳过 {title}：命中拒绝目录规则 {hit.pattern}（整棵子树都不处理）。"
            )
            continue

        try:
            size_before = folder_size(full)
        except OSError as exc:
            outcome.errors.append({"dir": full, "file": "", "error": f"计算体积失败：{exc}"})
            continue

        record = cache.get(title)
        stamp_existing = record.get("timestamp") if isinstance(record, dict) else None
        # 判据：**只比标题 + 体积**（外加 timestamp 必须存在）。
        # 不要在这里加 works_dir 之类的比对——老条目没有这个键，加了会让升级后
        # 第一次运行对真实媒体库全量重跑。库与库之间的隔离靠「一个媒体库一份日志文件」
        # 本身（见 libraries._resolve_log_file / registry.allocate_library_log_file）。
        if (
            not force
            and isinstance(record, dict)
            and record.get("size") == size_before
            and isinstance(stamp_existing, str)
        ):
            emit(f"Skipping folder: {title} (already processed at {stamp_existing})")
            outcome.skipped.append(
                {
                    "dir": full,
                    "folder": title,
                    "reason": "already_processed",
                    "at": stamp_existing,
                }
            )
            continue

        try:
            scopes = _scopes_for(
                full,
                scope_mode=scope_mode,
                accept_rules=accept_rules,
                reject_rules=reject_rules,
                skip_season_zero=skip_season_zero,
                warnings=outcome.warnings,
            )
        except OSError as exc:
            outcome.errors.append({"dir": full, "file": "", "error": f"遍历子目录失败：{exc}"})
            continue

        if not scopes:
            # 子目录模式下**算不出任何作用域**，而作品目录里确实有本规格该
            # 处理的文件（扁平结构：文件直接躺在作品目录里）⇒ 记一条"结构不可识别"。
            # 只在这种"有活可干、却因为缺季目录干不了"的情况下记，避免把"作品目录本来就是空的"
            # 推上看板；命中特典判据的文件不算（它们本来就不需要改名）。
            flat = [
                name
                for name in _direct_scope_files(full, extensions)
                if not (extras_rules and _extras_hit(name, extras_rules))
            ]
            if flat:
                entry = {
                    "dir": full,
                    "folder": title,
                    "files": len(flat),
                    "samples": "、".join(flat[:5]) + ("…" if len(flat) > 5 else ""),
                }
                outcome.unscoped_works.append(entry)
                emit(
                    f"Flat structure (skipped): {title} - {len(flat)} file(s) directly in "
                    "the work folder, no season subdirectory to rename"
                )

        for scope_dir, season_raw, rel_dir in scopes:
            _process_scope(
                scope_dir=scope_dir,
                season_raw=season_raw,
                rel_dir=rel_dir,
                title=title,
                scope_mode=scope_mode,
                extensions=extensions,
                per=per,
                expressions=expressions,
                segments=segments,
                patterns=patterns,
                fallback=fallback,
                uses_episode=uses_episode,
                extras_rules=extras_rules,
                works_dir=works_dir,
                emit=emit,
                outcome=outcome,
            )

        try:
            size_after = folder_size(full)
            cache[title] = {
                "size": size_after,
                "timestamp": format_stamp(now),
                "works_dir": works_dir or "",
            }
            write_json_cache(target_log, cache)
        except OSError as exc:
            outcome.errors.append({"dir": full, "file": "", "error": f"写日志失败：{exc}"})
            continue
        outcome.folders_processed += 1
        emit(f"Folder processed: {title}, Size: {size_after} bytes, Timestamp: {format_stamp(now)}")

    if outcome.unscoped_works:
        # 结构不可识别的作品 —— 不记录就是**完全静默**（不改名、不报错、不进
        # 任何清单），用户只会觉得"这一部什么都没发生"。所以既进任务结果、也进看板待办。
        listed = "、".join(str(item.get("folder") or "") for item in outcome.unscoped_works[:10])
        more = "…" if len(outcome.unscoped_works) > 10 else ""
        outcome.warnings.append(
            f"{len(outcome.unscoped_works)} 个作品没有「季目录」层级（文件直接躺在作品目录里），"
            f"重命名阶段找不到可处理的作用域、已整目录跳过：{listed}{more}。"
            "要处理它们：把文件放进「作品名/Season N/」，或把该处理器的 file_scope 改成 title。"
            "明细见任务结果的 samples.unscoped_works。"
        )

    if outcome.kept_unresolved_episode:
        # 这次运行有文件"没解析出集数 ⇒ 保留原文件名"，必须留下**可计数的告警**，
        # 不能静默（每一条 Kept 行在 stdout 里，这里给汇总）。
        listed = "、".join(outcome.kept_unresolved_episode[:10])
        more = "…" if len(outcome.kept_unresolved_episode) > 10 else ""
        outcome.warnings.append(
            f"{len(outcome.kept_unresolved_episode)} 个文件没解析出集数（episode_patterns 全部"
            f"未命中且没有配置 episode_fallback），已保留原文件名、未改名：{listed}{more}。"
            "要给它们改名，请补一条能命中文件名的 episode_patterns，或显式配置 episode_fallback。"
        )

    if outcome.name_collisions:
        # L4「可见性」：本次改名里出现了"同一个目标文件名被两个源文件抢"的情况。
        # L2：策略不是覆盖 —— :func:`_resolve_target_names` 给冲突文件加
        # `` (2)``/`` (3)``… 后缀，**一个都不丢**；这条告警把争名变成**可计数**的事实。
        # 注意：警告文本在 WebUI 里按**纯文本**转义渲染（task_detail.js 用 esc()），所以这里
        # 不能写 Markdown 记号（`**` / 反引号），否则用户会看到字面量。
        within_run = sum(1 for item in outcome.name_collisions if item.get("kind") == "within_run")
        existing = len(outcome.name_collisions) - within_run
        outcome.warnings.append(
            f"本次运行有 {len(outcome.name_collisions)} 个文件改到了已被占用的目标文件名上"
            f"（同一次运行内更早的文件占用 {within_run} 个 + 目标上本来就存在的文件 {existing} 个）；"
            "已按 L2 防丢策略处理：体积被判定为正片的拿干净名，其余加「 (2)」「 (3)」… 后缀保留，"
            "不覆盖、不丢文件。原有文件比新来的小时，旧链接也会改名让位（不删除）。"
            "明细见任务结果的 samples.renamed_with_suffix 与 samples.name_collisions。"
        )
    if outcome.renamed_with_suffix:
        # L2：可计数的"加后缀保住了"清单 —— 任务结果里要给出冲突清单。
        listed = "、".join(
            f"{item.get('old')} → {item.get('target')}" for item in outcome.renamed_with_suffix[:10]
        )
        more = "…" if len(outcome.renamed_with_suffix) > 10 else ""
        outcome.warnings.append(
            f"{len(outcome.renamed_with_suffix)} 个文件因为目标名被占用而加了后缀保留"
            f"（正片优先、其余不丢）：{listed}{more}。"
            "若不想看到这些后缀，请调整命名模板或清理目标目录里的重复版本。"
        )
    unkept_unresolved = [
        label for label in outcome.episode_unresolved if label not in set(outcome.kept_unresolved_episode)
    ]
    if unkept_unresolved:
        # L4「可见性」：有文件没解析出集数、但**没有**走"保留原名"那条分支（命名不引用集数，
        # 或兜底把它算成了"已解析"）—— 它们**照旧改名**了，解析失败不能就这么静默。
        listed = "、".join(unkept_unresolved[:10])
        more = "…" if len(unkept_unresolved) > 10 else ""
        outcome.warnings.append(
            f"另有 {len(unkept_unresolved)} 个文件没解析出集数，但没有保留原名（命名模板没有"
            f"引用集数，或走了 episode_fallback 兜底），照旧按模板改名了：{listed}{more}。"
            "明细见任务结果的 samples.episode_unresolved。"
        )

    if outcome.unknown_params:
        # 命名里用到了不存在的变量名 —— 那些段被留空，文件名**少了一段**，
        # 但改名照样成功。这条告警把它变成可计数的事实（保存期已经会拦住新写的错名字，
        # 这条针对的是"早就存进库里"的老数据、以及直接改数据库/导入 YAML 的情况）。
        names = sorted({name for item in outcome.unknown_params for name in item.get("params", [])})
        listed = "、".join(item.get("label", "") for item in outcome.unknown_params[:10])
        more = "…" if len(outcome.unknown_params) > 10 else ""
        outcome.warnings.append(
            f"有 {len(outcome.unknown_params)} 个文件的命名里用了不存在的变量"
            f"（{'、'.join(names)}），这些段什么都没输出、文件名因此少了一段：{listed}{more}。"
            "请到处理器或媒体库的命名里改掉这些名字，或在 naming.expressions 里定义它们。"
            "明细见任务结果的 samples.unknown_params。"
        )

    if outcome.extras_classified:
        # L3 特典排除（命中者留在原位置，不移动）：可计数的告警 ——
        # 这次运行把命中特典判据的散装文件**排除在改名之外**（保留原名、留在原位置）。与其它
        # 告警一样：WebUI 按纯文本渲染（esc()），**不能**用 Markdown 记号。
        listed = "、".join(
            f"{str(item.get('name') or '')}（{str(item.get('pattern') or '')}）"
            for item in outcome.extras_classified[:10]
        )
        more = "…" if len(outcome.extras_classified) > 10 else ""
        outcome.warnings.append(
            f"{len(outcome.extras_classified)} 个文件命中特典判据、已排除改名（保留原名、留在原位置）："
            f"{listed}{more}。"
            f"判据来自设置项 {EXTRAS_PATTERNS_KEY}（正则数组，空数组 = 关闭）；"
            "若误判了正片，请到设置页调整这条判据。"
        )

    return outcome


def _direct_scope_files(title_dir: str, extensions: tuple[str, ...]) -> list[str]:
    """作品目录里**直接**躺着的、本规格会处理的文件名（升序；结构不可识别判定用）。

    只看一层，子目录里的文件不算（那属于"有季目录"的情形）。选法与
    :func:`_process_scope` 的 ``scope_mode == "subdir"`` 分支保持一致：必须是文件，
    且 ``name.endswith(extensions)``；``extensions`` 为空元组时按"不过滤"处理
    （``str.endswith(())`` 恒为假，照抄会把所有文件都判成"不处理"）。
    """
    try:
        names = os.listdir(title_dir)
    except OSError:
        return []
    found: list[str] = []
    for name in names:
        if not os.path.isfile(os.path.join(title_dir, name)):
            continue
        if extensions and not name.endswith(extensions):
            continue
        found.append(name)
    return sorted(found)


def _scopes_for(
    title_dir: str,
    *,
    scope_mode: str,
    accept_rules: list[_Rule],
    reject_rules: list[_Rule],
    skip_season_zero: bool,
    warnings: list[str],
) -> list[tuple[str, str, str]]:
    """算出该标题文件夹下的"作用域目录"列表：``[(目录, season_raw, rel_dir)]``。"""
    if scope_mode == "title":
        return [(title_dir, "", "")]
    scopes: list[tuple[str, str, str]] = []
    for name in os.listdir(title_dir):
        sub = os.path.join(title_dir, name)
        if not os.path.isdir(sub):
            continue
        hit = _rule_hit(reject_rules, name)
        if hit is not None:
            warnings.append(f"跳过子目录 {name}：命中拒绝目录规则 {hit.pattern}。")
            continue
        season_raw = ""
        if accept_rules:
            # 必须**整名匹配**（``fullmatch``）才算接受；
            # 取**第一条**整名匹配的规则，用它的 season_group 取季号。
            matched: _Rule | None = None
            for rule in accept_rules:
                if rule.regex.fullmatch(name):
                    matched = rule
                    break
            if matched is None:
                continue
            if matched.group:
                value = matched.regex.fullmatch(name).group(matched.group)  # type: ignore[union-attr]
                season_raw = value or ""
        # 只有真的取到了季号才做 "00" 判断：accept_dirs 为空时 season_raw 是空串，
        # 若照字面 "".zfill(2) == "00" 会把所有子目录静默跳过（配置陷阱）。
        if skip_season_zero and season_raw and season_raw.zfill(2) == "00":
            warnings.append(f"跳过子目录 {name}：季号是 00（skip_season_zero）。")
            continue
        scopes.append((sub, season_raw, name))
    return scopes


def _process_scope(
    *,
    scope_dir: str,
    season_raw: str,
    rel_dir: str,
    title: str,
    scope_mode: str,
    extensions: tuple[str, ...],
    per: str,
    expressions: dict[str, Any],
    segments: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
    fallback: Any,
    uses_episode: bool,
    extras_rules: list[re.Pattern[str]],
    works_dir: str,
    emit: Callable[[str], None],
    outcome: RenameOutcome,
) -> None:
    """处理一个作用域目录：选文件 → 命名 → ``os.rename``。

    当命名**引用了集数**（``uses_episode``）而某个文件没解析出集数时，不执行改名 ——
    目标侧的文件保持硬链接阶段用的原文件名，并记进
    :attr:`RenameOutcome.kept_unresolved_episode` + 打一行 ``Kept (no episode):``。

    L3「特典排除」：``extras_rules`` 是本次编译好的特典判据
    （正则、大小写不敏感）。命中的**直接**候选文件被排除在改名之外：保持原名留在原位置
    （不改名、不移动、不建目录），逐个打一行 ``Extra (kept):``；本次排除的清单记进
    :attr:`RenameOutcome.extras_classified`。
    """
    try:
        names = os.listdir(scope_dir)
    except OSError as exc:
        outcome.errors.append({"dir": scope_dir, "file": "", "error": f"读取目录失败：{exc}"})
        return

    if scope_mode == "subdir":
        # 旧口径番剧语义 —— 只取**直接**子项，且必须是文件
        candidates = [
            name
            for name in names
            if os.path.isfile(os.path.join(scope_dir, name)) and name.endswith(extensions)
        ]
    else:
        # 旧口径电影语义 —— 只看后缀，不做 isfile 过滤
        candidates = [name for name in names if name.endswith(extensions)]

    # ── L3「特典文件分类」──────────────────────────────────────────────────────
    # 判据只作用于**本作用域目录里的直接文件**（candidates），绝不递归子目录 ——
    # 所以 ``Season N/特典/`` 里的文件一个都不碰（那是"已经摆进特典目录"的正规摆放）。
    # 判定用 candidates 而不是 per=extension 收敛后的 selected，而且**必须在收敛之前**剔除：
    # 否则电影那种 ``per: extension`` 的库里，命中的特典会先当上"同后缀最大者"、再被剔除，
    # 正片于是一轮里白等（下一轮又会因为文件夹体积没变而整棵 Skipping folder）—— 正片就再也
    # 改不了名了。命中者不参与命名：特典本来就不需要名字，更不该跟正片抢干净名。
    extras_hits: dict[str, str] = {}
    if extras_rules:
        for name in candidates:
            if not os.path.isfile(os.path.join(scope_dir, name)):
                # title 模式的 candidates 不做 isfile 过滤（这是该模式的既定行为）；但"特典"显然是文件，
                # 不能让一个名字里带 ``.mkv`` 的**目录**被搬进「其他/」。
                continue
            pattern = _extras_hit(name, extras_rules)
            if pattern:
                extras_hits[name] = pattern
    if extras_hits:
        candidates = [name for name in candidates if name not in extras_hits]

    if per == "extension":
        # 旧口径电影语义 —— 按最后一段后缀分组，每组只留体积最大者（严格大于才替换）
        grouped: dict[str, dict[str, Any]] = {}
        for name in candidates:
            path = os.path.join(scope_dir, name)
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                outcome.errors.append({"dir": scope_dir, "file": name, "error": f"取体积失败：{exc}"})
                continue
            extension = name.split(".")[-1]
            current = grouped.get(extension)
            if current is None:
                grouped[extension] = {"name": name, "size": size}
            elif size > current["size"]:
                grouped[extension] = {"name": name, "size": size}
        selected = [item["name"] for item in grouped.values()]
    else:
        selected = list(candidates)

    # ── L3「特典文件分类」──────────────────────────────────────────────────────
    ops: list[dict[str, Any]] = []
    for order, filename in enumerate(selected):
        context = _file_context(
            title=title,
            filename=filename,
            season_raw=season_raw,
            scope_mode=scope_mode,
            rel_dir=rel_dir,
            works_dir=works_dir,
            patterns=patterns,
            fallback=fallback,
        )
        if not context["episode_resolved"]:
            # L4「可见性」：**所有**没解析出集数的文件都记进 episode_unresolved（不论下面是否
            # 因为它保留了原名）。命名不引用集数时（movie 形状）这个文件照旧改名，若只有
            # kept_unresolved_episode，这次解析失败就会静默消失。
            outcome.episode_unresolved.append(_scope_label(title, rel_dir, filename))
        if uses_episode and not context["episode_resolved"]:
            # 命名要写集数，但这个文件没解析出集数 —— 不改名，保留原文件名。
            # 若渲染成字符串 "None"（`… S01ENone.mkv`），同一作品的多个压制版本会
            # 撞成同一个目标名互相覆盖（真实事故：4 个文件只剩 1 个）。
            kept = _scope_label(title, rel_dir, filename)
            emit(f"Kept (no episode): {kept}")
            outcome.kept_unresolved_episode.append(kept)
            continue
        # ① 具名表达式：按**书写顺序**求值，结果并入上下文供后续表达式与段引用
        for key, expression in expressions.items():
            try:
                context[key] = eval(expression, _eval_env(context))  # noqa: S307 - 见 §3.1 的安全面说明
            except Exception as exc:  # noqa: BLE001 - 用户表达式，任何异常都只跳过该文件
                outcome.warnings.append(f"表达式 {key} 求值失败（{filename}）：{exc}")
                context[key] = ""
        dropped: list[str] = []
        new_name = render_segments(segments, context, dropped=dropped)
        if dropped:
            # 段序列里出现了上下文没有的名字 —— 这些段什么都不输出，文件名
            # 因此**少了一段**，但改名照样算成功。没有这行日志就只能靠肉眼比对名字。
            params = sorted(set(dropped))
            label = _scope_label(title, rel_dir, filename)
            emit(f"Unknown param: {label} ({'、'.join(params)})")
            outcome.unknown_params.append({"label": label, "file": filename, "params": params})
        if not new_name:
            outcome.warnings.append(f"段序列渲染出空文件名（{filename}），已跳过。")
            continue
        old_path = os.path.join(scope_dir, filename)
        try:
            size = os.path.getsize(old_path)
        except OSError as exc:
            outcome.errors.append({"dir": scope_dir, "file": filename, "error": f"取体积失败：{exc}"})
            continue
        ops.append(
            {
                "order": order,
                "old": old_path,
                "old_name": filename,
                "clean": new_name,
                "size": size,
                "final": new_name,
            }
        )

    # ② L2 防丢：先给所有待改名文件定下最终目标名（正片优先、冲突加后缀），再按原顺序执行。
    for op in _resolve_target_names(
        ops, scope_dir=scope_dir, rel_dir=rel_dir, outcome=outcome, emit=emit
    ):
        filename = op["old_name"]
        final = op["final"]
        if not final:
            # 极端情况：`` (2)``…`` (999)`` 全被占用。宁可保留原名 + 报错，也绝不覆盖。
            message = f"同名文件过多（{SUFFIX_TEMPLATE.format(index='2')}…已占满），已保留原名"
            emit(f"Error renaming {filename}: {message}")
            outcome.errors.append({"dir": scope_dir, "file": filename, "error": message})
            continue
        new_path = os.path.join(scope_dir, final)
        try:
            os.rename(op["old"], new_path)
        except Exception as exc:  # noqa: BLE001 - 刻意宽捕获：单个文件失败不能中断整轮
            emit(f"Error renaming {filename}: {exc}")
            outcome.errors.append({"dir": scope_dir, "file": filename, "error": str(exc)})
            continue
        outcome.rendered_targets[new_path] = _scope_label(title, rel_dir, filename)
        if final != op["clean"] and not op.get("in_place"):
            # L2：这个文件因为争名加了后缀 —— 单独一行，日志里一眼能看出"没丢"。
            emit(f"Renamed (suffix): {filename} -> {final}")
        else:
            emit(f"Renamed: {filename} -> {final}")
        outcome.renamed.append({"dir": scope_dir, "old": filename, "new": final})

    # ── L3：命中的散装特典**排除在改名之外**（不改名、不移动，保持原名留在原位置）──────────
    # 用户修订：取消「移入 其他/」。命中者在上面的判定块里已经从 candidates 剔除，
    # 所以这里**只记清单 + 打一行日志**，不做任何文件系统操作（连目录都不创建）。
    if not extras_hits:
        return
    extras_label = _scope_label(title, rel_dir, "")
    for filename in extras_hits:
        try:
            size = os.path.getsize(os.path.join(scope_dir, filename))
        except OSError as exc:
            outcome.errors.append({"dir": scope_dir, "file": filename, "error": f"取体积失败：{exc}"})
            continue
        emit(f"Extra (kept): {_scope_label(title, rel_dir, filename)}")
        outcome.extras_classified.append(
            {
                "dir": extras_label,
                "name": filename,
                "size": int(size),
                "pattern": extras_hits[filename],
            }
        )
