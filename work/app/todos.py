"""任务「待办看板」的数据层：种类注册表 + 去重语义 + 执行器入口（ARCHITECTURE §6.10）。

**为什么要有它**：有些情况值得用户知道，但不该打断任务（源目录被外部改动、某个文件建链失败、
改名时目标名被占用只好加后缀保留……）。这里把这类事件汇总成**全局一份**的台账，首页仪表盘上
展示，用户看完打钩即可——不弹窗、不阻断、不重试任何东西。

## 去重语义（冻结，改之前先读 `tests/test_todos.py`）

设 ``(kind, dedupe_key)`` 是"同一条问题"的身份：

1. 已有一条 ``open`` ⇒ **只累加**：``occurrences += 1``、``last_seen_at`` 与"最近一次"的上下文
   （任务号/任务类型/详情）刷新成这一次的；``first_seen_at`` **永不变**，也不新增条目。
2. 该 ``(kind, dedupe_key)`` 已被手动打钩（``done``）而现在**再次发生** ⇒ **新插一条 ``open``**；
   历史那条 ``done`` 一个字节都不动。于是"这个问题出现过几次、每次什么时候"天然留在表里。
3. 打钩只把 ``status`` 改成 ``done``（并记 ``resolved_at``），**绝不删数据**；``reopen`` 把
   ``status`` 改回 ``open`` 并清空 ``resolved_at``。
4. 看板是全局的：每条都带来源媒体库与任务，可按状态 / 种类 / 媒体库筛选。

实现上第 1、2 条是**同一个 SQL**：更新时只匹配 ``status='open'`` 的那一行，匹配不到就插新行，
所以"done 之后再发生 ⇒ 新开一条"不需要额外分支，也就不会有"忘了处理某个状态"的漏。

## 与前端的边界

``title`` 只放**标识符**（路径 / 目标名 / 媒体库），``detail`` 只放**原始数据**（路径、错误文本），
解释性文案（"硬链接创建失败"、"冲突文件已加后缀保留"）走 ``KNOWN_KINDS`` 的
``title``/``description`` 或前端 ``web/js/i18n.js`` 的 ``dash.todo.kind.*``，这样换语言不用改库。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Iterable, TypedDict

from .db import get_conn, get_setting, now_iso, tx
from .serializers import format_time

logger = logging.getLogger(__name__)

#: 种类注册表的值形状。``source_keys`` 是**从 ``result`` 里读哪些键**的文档化说明：
#: 依次是计数键、样本键；一种待办如果有**多条产生路径**（如 ``rename_suffix`` 的"新来的自己
#: 加后缀"与"目标盘上原有的文件让位加后缀"），就把每条路径的计数键、样本键都列上
#: （见 :func:`_events_rename_suffix`）。
class TodoKind(TypedDict):
    title: str
    severity: str
    description: str
    source_keys: tuple[str, ...]


#: 条目状态：``open`` 未完成 / ``done`` 已打钩。
TODO_STATUSES = ("open", "done")
#: 严重级别（前端用来挑颜色；顺序也是"越靠后越严重"）。
TODO_SEVERITIES = ("info", "warning", "error")

#: 单次 ``record_task_events`` 里每种 kind 的明细条数**不设任何上限**（``SAMPLE_LIMIT = None``）：
#: **一条明细一条待办**，看板完整反映
#: 本次任务的问题规模（8 534 个建链失败就是 8 534 条，不再只剩 100 条）。
#: 样本缺失时按来源库聚合成一条。真正的"看不到"只可能来自接口分页（``MAX_LIST_LIMIT``），
#: 分页由 WebUI 的翻页控件覆盖；任务详情页的逐条**渲染**另有一层显示上限（见
#: ``web/js/pages/task_detail.js``），那只影响一屏渲染多少行，不影响这里的落账条数。

#: 一条 ``detail`` 的最大长度（超出截断并注明）。防止把整个错误堆栈塞进列表接口。
MAX_DETAIL_CHARS = 4000

#: 拿不到样本时用的去重键（按来源库聚合，而不是把所有"没样本"的失败并成一条）。
NO_SAMPLE_KEY = "no-sample"

#: 六种事件。``severity`` 的取法：``error`` = 可能已经丢了东西/没建上链接；
#: ``warning`` = 行为偏离预期但没坏；``info`` = 只记录。
#:
#: ``name_collisions``（"目标名已被占用"）不单独进看板台账 —— 它和 ``rename_suffix`` 记的是
#: **同一次重命名**（两者重叠），一次重命名只留
#: **一条**「冲突文件已加后缀保留」。争名的事实清单仍照旧写进任务结果（``result.name_collisions``
#: 与 ``samples.name_collisions``，任务详情页照旧展示），只是**不再进待办台账**。
KNOWN_KINDS: dict[str, TodoKind] = {
    "link_failed": {
        "title": "硬链接创建失败",
        "severity": "error",
        "description": "源文件没能在媒体库里建出硬链接（跨文件系统、权限、目标已是别的 inode 等）。",
        "source_keys": ("link_failed", "samples.link_failed"),
    },
    "rename_suffix": {
        "title": "冲突文件已加后缀保留",
        "severity": "warning",
        "description": "目标名已被占用，体积更大的文件拿走干净名，另一个文件按「 (2)」「 (3)」序号模板加了后缀另存 —— 可能是**新来的文件**自己加后缀，也可能是**目标盘上原有的同名文件**让位。原名文件没有被覆盖，一个都没丢。",
        "source_keys": (
            "renamed_with_suffix",
            "samples.renamed_with_suffix",
            "displaced_targets",
            "samples.displaced_targets",
        ),
    },
    "unresolved_episode": {
        "title": "保留原名（未解析出集数）",
        "severity": "warning",
        "description": "命名引用了集数、但集数正则全部未命中且没有兜底值，该文件保留原文件名未改名。",
        "source_keys": ("kept_unresolved_episode", "samples.kept_unresolved_episode"),
    },
    "unscoped_works": {
        "title": "作品结构不可识别（未改名）",
        "severity": "warning",
        "description": "子目录模式（file_scope: subdir）下，作品目录里没有可接受的「季目录」，文件直接躺在作品目录里（扁平结构）—— 重命名阶段找不到作用域，整个作品被跳过：不改名、不报错。把文件放进「作品名/Season N/」，或把处理器的 file_scope 改成 title。",
        "source_keys": ("unscoped_works", "samples.unscoped_works"),
    },
    "source_integrity": {
        "title": "源目录完整性校验未通过",
        "severity": "error",
        "description": "任务开始前后逐文件核对源目录，发现相对路径 / 大小 / inode / mtime 有变动。",
        "source_keys": ("source_integrity.ok", "source_integrity.changed"),
    },
    "task_failed": {
        "title": "任务失败",
        "severity": "error",
        "description": "任务以 failed 结束（或结果里带非空错误），看板上留一条待办。",
        "source_keys": ("status", "error"),
    },
}

#: 每种 kind 的样本字段映射。``string_key`` 指"样本是裸字符串时按哪个字段解释"。
#: ``key_fields`` 按优先级取去重键与标题，``detail_fields`` 是详情里的 ``标签 -> 字段``；
#: ``key_all_fields``（可选）里的字段全都有值时优先用它拼键（见 :func:`_event_key`）。
_SAMPLE_SPECS: dict[str, dict[str, Any]] = {
    "link_failed": {
        "count_key": "link_failed",
        "samples_key": "samples.link_failed",
        "string_key": "dst",
        "key_fields": ("dst", "src", "error"),
        "detail_fields": (("源路径", "src"), ("目标路径", "dst"), ("错误", "error")),
    },
    "rename_suffix": {
        "count_key": "renamed_with_suffix",
        "samples_key": "samples.renamed_with_suffix",
        "string_key": "target",
        # 真实形状（app/processors/engine.py 的 _suffix_entry）：
        # {dir, old, target, clean, size, reason, displaced}
        "key_fields": ("target", "old"),
        "key_all_fields": ("dir", "target"),
        "detail_fields": (
            ("目录", "dir"),
            ("源文件名", "old"),
            ("最终文件名", "target"),
            ("本来想要的名字", "clean"),
            ("体积（字节）", "size"),
            ("原因", "reason"),
            ("让位者", "displaced"),
        ),
    },
    "unresolved_episode": {
        "count_key": "kept_unresolved_episode",
        "samples_key": "samples.kept_unresolved_episode",
        "string_key": "path",
        "key_fields": ("path", "target", "dst"),
        "detail_fields": (("路径", "path"), ("目标路径", "target")),
    },
    "unscoped_works": {
        # 结构不可识别（缺季目录）的作品 —— 一条作品一条待办。
        # 真实形状（app/processors/engine.py 的 unscoped_works 条目）：
        # {dir, folder, files, samples}
        "count_key": "unscoped_works",
        "samples_key": "samples.unscoped_works",
        "string_key": "dir",
        "key_fields": ("dir", "folder"),
        "key_all_fields": ("dir",),
        "detail_fields": (
            ("作品目录", "dir"),
            ("作品名", "folder"),
            ("直接文件数", "files"),
            ("文件名（前几个）", "samples"),
        ),
    },
}

#: 「占位者让位」的样本形状（``app/processors/engine.py`` 的 ``displaced_targets`` 调用点）：
#: ``{dir, old, target, clean, size}`` —— 没有 ``reason`` / ``displaced`` 两个字段
#: （``old`` 与 ``clean`` 都是占位者**原来的名字**，``size`` 记的是**赢家**的体积），所以单独
#: 给一份 spec：详情里把"原因"补成 ``displaced``，并把体积标注清楚是谁的。
#: 它与 ``rename_suffix`` 是**同一个问题**的两条产生路径，读取时合并成同一种待办
#: （见 :func:`_events_rename_suffix` —— 一次重命名只推一条）。
_DISPLACED_SPEC: dict[str, Any] = {
    "count_key": "displaced_targets",
    "samples_key": "samples.displaced_targets",
    "string_key": "target",
    "key_fields": ("target", "old"),
    "key_all_fields": ("dir", "target"),
    "detail_fields": (
        ("目录", "dir"),
        ("让位文件（原名）", "old"),
        ("让位后文件名", "target"),
        ("赢家体积（字节）", "size"),
        ("原因", "reason"),
    ),
}

#: 「占位者让位」样本缺 ``reason`` 字段时补的值（详情里要能一眼看出是哪条路径来的）。
_DISPLACED_REASON = "displaced（目标盘上原有的同名文件让位，未删除、未复制数据）"


# ----------------------------------------------------------------- 小工具


def _dotted(source: Any, path: str) -> Any:
    """按 ``a.b.c`` 从嵌套 dict 里取值；中间任何一层不是 dict 就返回 ``None``。"""
    current = source
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _text(value: Any) -> str:
    """把 ``result`` 里的任意值变成一行可存的文本；``None``/坏类型不抛异常。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return "、".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value).strip()


def _as_int(value: Any) -> int:
    """计数键：缺失 / ``None`` / 坏类型一律当 0（"缺键就跳过"就靠它）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return 0
    return 0


def _as_list(value: Any) -> list:
    """样本键：只认 list/tuple，其它一律当空（dict/str 不当成一条样本）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _clip(body: str) -> str:
    """``detail`` 的硬上限：超出截断并注明（列表接口会把 detail 整条发给前端）。"""
    if len(body) > MAX_DETAIL_CHARS:
        return body[:MAX_DETAIL_CHARS] + "\n…（详情过长已截断，完整明细见任务日志）"
    return body


def _lines(pairs: Iterable[tuple[str, str]]) -> str:
    """把 ``(标签, 值)`` 拼成多行详情，跳过空值；总长超限则截断并注明。"""
    body = "\n".join(f"{label}：{value}" for label, value in pairs if value)
    return _clip(body)


def _scope_key(ctx: dict) -> str:
    """拿不到具体对象时（样本缺失）按**来源库**聚合的去重键。"""
    library_id = ctx.get("library_id")
    if library_id is None:
        return NO_SAMPLE_KEY
    return f"library:{int(library_id)}"


def _context_title(ctx: dict) -> str:
    """拿不到具体对象时的标题：媒体库名 → 任务号 → 兜底。"""
    name = _text(ctx.get("library_name"))
    if name:
        return name
    task_id = ctx.get("task_id")
    if task_id is not None:
        return f"任务 #{int(task_id)}"
    return "—"


# ----------------------------------------------------------------- 开关


def normalize_kinds(raw: Any) -> list[str]:
    """把设置值解析成**已知** kind 列表；空值或全非法 ⇒ 全部启用（缺省行为）。

    分隔符容忍逗号 / 中文逗号 / 顿号 / 分号 / 空白，大小写不敏感，未知值静默忽略、重复值去重，
    返回顺序固定为 ``KNOWN_KINDS`` 的注册顺序（前端下拉框的顺序才稳定）。
    """
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = re.split(r"[,，、;；\s]+", str(raw if raw is not None else ""))

    picked: set[str] = set()
    for part in parts:
        name = part.strip().lower()
        if name in KNOWN_KINDS:
            picked.add(name)
    if not picked:
        return list(KNOWN_KINDS)
    return [kind for kind in KNOWN_KINDS if kind in picked]


def enabled_kinds(raw: Any = None) -> list[str]:
    """读设置 ``todo_kinds``（缺省 ``""`` = 全部启用）。``raw`` 给测试直接注入用。"""
    if raw is None:
        raw = get_setting("todo_kinds", "") or ""
    return normalize_kinds(raw)


# ----------------------------------------------------------------- 事件读取


def _sample_record(item: Any, spec: dict) -> dict | None:
    """把一条样本规整成 ``{字段: 文本}``；形状完全不认识时返回 ``None``（跳过它）。"""
    if isinstance(item, str):
        text = item.strip()
        return {spec["string_key"]: text} if text else None
    if isinstance(item, dict):
        record = {str(key): _text(value) for key, value in item.items()}
        return record if any(record.values()) else None
    if isinstance(item, (list, tuple)) and item and all(isinstance(x, str) for x in item):
        # 极少数情况下样本是 ["src", "dst"] 这样的元组形状：按 detail_fields 的顺序硬套。
        fields = [field for _, field in spec["detail_fields"]]
        return {field: _text(item[index]) for index, field in enumerate(fields) if index < len(item)}
    return None


def _event_key(record: dict, spec: dict) -> str:
    """算一条样本的去重键。

    ``key_all_fields`` 有值、且这些字段**全部**有内容时，用 `` | `` 把它们拼起来当键
    （L2 的两个样本就是这种：``target`` 只是文件名，不同作品下会重名，必须带上作用域目录
    ``dir``，否则两条互不相干的问题会被并成一条、只看到其中一条）。
    否则退回"``key_fields`` 里第一个非空字段"（裸字符串样本与其余种类沿用旧口径）。
    """
    all_fields = tuple(spec.get("key_all_fields") or ())
    if all_fields and all(str(record.get(field, "")).strip() for field in all_fields):
        return " | ".join(str(record[field]).strip() for field in all_fields)
    return next(
        (str(record[field]) for field in spec.get("key_fields", ()) if record.get(field)), ""
    )


def _events_and_count(
    result: dict,
    spec: dict,
    *,
    defaults: dict[str, str] | None = None,
) -> tuple[list[dict], int]:
    """按 ``spec`` 把 ``result`` 里的计数键 + 样本键读成 ``(事件列表, 计数)``。

    ``defaults`` 给样本里**缺失**的字段补值（让位样本就没有 ``reason`` 字段）。
    """
    count = _as_int(result.get(spec["count_key"]))
    events: list[dict] = []
    seen: set[str] = set()
    for item in _as_list(_dotted(result, spec["samples_key"])):
        record = _sample_record(item, spec)
        if record is None:
            continue
        if defaults:
            for field, value in defaults.items():
                record.setdefault(field, value)
        key = _event_key(record, spec)
        if not key or key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "dedupe_key": key,
                "title": key,
                "detail": _lines(
                    (label, record.get(field, "")) for label, field in spec["detail_fields"]
                ),
            }
        )
    return events, count


def _missing_sample_event(ctx: dict, label: str, count: int) -> list[dict]:
    """计数 > 0 但样本缺失/形状不认识时的那一条（按来源库聚合）。

    事件本身是真实的（链接就是没建上、冲突就是发生过），不能因为"截图丢了"就当没发生。
    """
    return [
        {
            "dedupe_key": _scope_key(ctx),
            "title": _context_title(ctx),
            "detail": _lines(
                (
                    ("计数", f"{label} = {count}"),
                    ("说明", "样本缺失或形状无法识别，无法逐条展开。"),
                )
            ),
        }
    ]


def _sample_events(result: dict, kind: str, ctx: dict) -> list[dict]:
    """按 ``_SAMPLE_SPECS`` 把 ``result`` 里的计数键 + 样本键转成事件列表。

    **计数键是权威**：计数为 0 / 缺失 / 坏类型 ⇒ 一条都不记，哪怕样本数组里躺着东西
    （``result`` 是外部写进来的，宁可相信计数，也不要把脏样本变成一条待办）。
    """
    spec = _SAMPLE_SPECS[kind]
    events, count = _events_and_count(result, spec)
    if count <= 0:
        return []
    if events:
        return events
    return _missing_sample_event(ctx, f"result.{spec['count_key']}", count)


def _events_rename_suffix(result: dict, ctx: dict) -> list[dict]:
    """「冲突文件已加后缀保留」：**一次重命名只记一条**。

    同一个问题有两条产生路径，都归到这一个种类（``name_collisions`` 已不再进看板）：

    * ``renamed_with_suffix`` —— **新来的文件**自己加了后缀（``reason`` = ``within_run`` /
      ``existing_target``）；
    * ``displaced_targets`` —— **目标盘上原有的文件让位**、被加到 `` (2)``（详情里原因写作
      ``displaced``）。

    幂等重跑（文件已经坐在 `` (N)`` 上、同一个 inode）由引擎判为 ``in_place``、两边都不记录
    ⇒ 这里天然不会冒出假事件。
    """
    events, count = _events_and_count(result, _SAMPLE_SPECS["rename_suffix"])
    displaced, displaced_count = _events_and_count(
        result, _DISPLACED_SPEC, defaults={"reason": _DISPLACED_REASON}
    )
    # 计数键是权威（与 :func:`_sample_events` 同一口径）：计数 <= 0 的那条产生路径整条丢掉，
    # 免得一个脏样本凭空变出一条待办。
    if count <= 0:
        events = []
    if displaced_count <= 0:
        displaced = []
    merged: list[dict] = []
    seen: set[str] = set()
    for event in (*events, *displaced):
        if event["dedupe_key"] in seen:
            continue
        seen.add(event["dedupe_key"])
        merged.append(event)
    if merged:
        return merged
    total = count + displaced_count
    if total <= 0:
        return []
    return _missing_sample_event(
        ctx, "result.renamed_with_suffix + result.displaced_targets", total
    )


def _events_source_integrity(result: dict, ctx: dict) -> list[dict]:
    """源目录完整性：只在 ``ok is False`` 时记一条（``changed`` 列变动路径）。"""
    info = result.get("source_integrity")
    if info is False:
        changed: list[str] = []
    elif isinstance(info, dict) and info.get("ok") is False:
        changed = [text for text in (_text(item) for item in _as_list(info.get("changed"))) if text]
    else:
        # ``ok`` 为 True / 缺失 / 形状不认识 ⇒ 一律不记，避免误报。
        return []

    shown = changed[:20]
    body_lines = [f"- {path}" for path in shown]
    if len(changed) > len(shown):
        body_lines.append(f"…（共 {len(changed)} 条，已截断，完整明细见任务日志）")
    if not body_lines:
        body_lines = ["（未提供变动路径清单）"]
    return [
        {
            "dedupe_key": _scope_key(ctx),
            "title": _context_title(ctx),
            "detail": _clip("变动路径：\n" + "\n".join(body_lines)),
        }
    ]


def _first_line(text: str) -> str:
    """错误摘要：压掉空白、取第一行（去重键要稳，不能带上每行都变的堆栈偏移）。"""
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = " ".join(line.split())
        if stripped:
            return stripped
    return ""


def _events_task_failed(result: dict, ctx: dict) -> list[dict]:
    """任务失败：``status == 'failed'`` 或有非空错误文本时记一条。"""
    status = _text(ctx.get("status")) or _text(result.get("status"))
    error = (
        _text(ctx.get("error"))
        or _text(result.get("error"))
        or _text(result.get("error_summary"))
    )
    if status != "failed" and not error:
        return []

    summary = _first_line(error)
    if summary:
        dedupe_key = summary[:160]
        title = summary[:160]
    else:
        # 只有状态没有错误文本：按来源库聚合（否则每次失败都会被并成同一条）。
        dedupe_key = _scope_key(ctx)
        title = _context_title(ctx)
    detail = _lines(
        (
            ("状态", status),
            ("媒体库", _text(ctx.get("library_name"))),
            ("任务", f"#{int(ctx['task_id'])}" if ctx.get("task_id") is not None else ""),
            ("错误", error),
        )
    )
    return [{"dedupe_key": dedupe_key, "title": title, "detail": detail}]


#: kind -> 读取函数。加新种类就在这里加一行 + 在 ``KNOWN_KINDS`` 里登记。
_EVENT_READERS: dict[str, Callable[[dict, dict], list[dict]]] = {
    "link_failed": lambda result, ctx: _sample_events(result, "link_failed", ctx),
    # 「自己加后缀」与「占位者让位」是同一个问题的两条路径 ⇒ 合并读（一次重命名只推一条）。
    "rename_suffix": _events_rename_suffix,
    "unresolved_episode": lambda result, ctx: _sample_events(result, "unresolved_episode", ctx),
    # 结构不可识别（缺季目录）⇒ 整目录跳过改名，不静默略过。
    "unscoped_works": lambda result, ctx: _sample_events(result, "unscoped_works", ctx),
    "source_integrity": _events_source_integrity,
    "task_failed": _events_task_failed,
}


# ----------------------------------------------------------------- 写入


def _upsert_event(
    conn,
    *,
    kind: str,
    event: dict,
    severity: str,
    ctx: dict,
    stamp: str,
) -> tuple[int, bool]:
    """把一条事件写进台账，返回 ``(todo_id, created)``；去重语义见模块 docstring。"""
    dedupe_key = event["dedupe_key"]
    title = event["title"]
    detail = event["detail"]
    library_id = ctx.get("library_id")
    library_name = _text(ctx.get("library_name")) or None
    task_id = ctx.get("task_id")
    task_kind = _text(ctx.get("task_kind")) or None

    existing = conn.execute(
        "SELECT id FROM todos WHERE kind=? AND dedupe_key=? AND status='open'"
        " ORDER BY id DESC LIMIT 1",
        (kind, dedupe_key),
    ).fetchone()
    if existing is not None:
        # 语义 1：已有一条 open ⇒ 只累加 + 把"最近一次"的上下文刷新（first_seen_at 永不变）。
        conn.execute(
            "UPDATE todos SET occurrences = occurrences + 1, last_seen_at=?, updated_at=?,"
            " title=?, detail=?, severity=?, library_id=?, library_name=?, task_id=?, task_kind=?"
            " WHERE id=?",
            (
                stamp,
                stamp,
                title,
                detail,
                severity,
                library_id,
                library_name,
                task_id,
                task_kind,
                int(existing["id"]),
            ),
        )
        return int(existing["id"]), False

    # 语义 2：没有 open 的那一条（要么第一次发生、要么上一次已经被打钩）⇒ 插一条新的 open。
    cursor = conn.execute(
        "INSERT INTO todos (kind, dedupe_key, status, severity, title, detail, library_id,"
        " library_name, task_id, task_kind, occurrences, first_seen_at, last_seen_at,"
        " resolved_at, created_at, updated_at)"
        " VALUES (?,?, 'open', ?,?,?,?,?,?,?, 1, ?,?, NULL, ?, ?)",
        (
            kind,
            dedupe_key,
            severity,
            title,
            detail,
            library_id,
            library_name,
            task_id,
            task_kind,
            stamp,
            stamp,
            stamp,
            stamp,
        ),
    )
    return int(cursor.lastrowid), True


def record_task_events(
    *,
    result: dict,
    library_id: int | None,
    library_name: str | None,
    task_id: int | None,
    task_kind: str = "run",
    status: str | None = None,
    error: str | None = None,
) -> list[int]:
    """把一次任务的 ``result`` 里值得注意的情况记进待办台账，返回**本次触及**的条目 id。

    * 前五个参数是与执行器约定的固定接口（关键字传参）；``status`` / ``error`` 是可选的补充：
      ``executor`` 自己知道 ``tasks.status`` 与 ``tasks.error``，而 ``result`` 里未必有这两个
      键**，所以调用方可以直接把它们传进来；不传时函数会退回 ``result["status"]`` /
      ``result["error"]``（对未来的 result 形状一样容错）。
    * 返回的是本次**新建的 + 累加的**条目 id（去重、按种类顺序），调用方可以拿去打日志。
    * 关掉的种类（``settings.todo_kinds``）直接跳过；``result`` 缺键、``None``、坏类型都不抛异常。
    * 整批写在一个事务里：出了意外不会留下半份台账。
    """
    if not isinstance(result, dict):
        result = {}

    wanted = set(enabled_kinds())
    ctx = {
        "library_id": library_id,
        "library_name": library_name,
        "task_id": task_id,
        "task_kind": task_kind,
        "status": status,
        "error": error,
    }
    stamp = now_iso()
    touched: list[int] = []
    seen_ids: set[int] = set()

    with tx() as conn:
        for kind, reader in _EVENT_READERS.items():
            if kind not in wanted:
                continue
            meta = KNOWN_KINDS[kind]
            try:
                events = reader(result, ctx)
            except Exception:  # 读取器再稳也不该把任务本身拖挂
                logger.exception("待办：读取 %s 事件失败，已跳过", kind)
                continue
            for event in events:
                todo_id, created = _upsert_event(
                    conn,
                    kind=kind,
                    event=event,
                    severity=meta["severity"],
                    ctx=ctx,
                    stamp=stamp,
                )
                if created:
                    logger.info("待办新增：%s #%s %s", kind, todo_id, event["title"])
                if todo_id not in seen_ids:
                    seen_ids.add(todo_id)
                    touched.append(todo_id)
    return touched


# ----------------------------------------------------------------- 读取


def _sort_clause() -> str:
    """未完成在前，其次按"最近一次出现"倒序（同一时刻再按 id 倒序，保证分页稳定）。"""
    return "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, last_seen_at DESC, id DESC"


def todo_dict(row) -> dict:
    """把一行 ``todos`` 序列化成前端用的形状（时间戳一律走 ``format_time``，与任务对象一致）。"""
    data = dict(row)
    meta = KNOWN_KINDS.get(str(data.get("kind")), {})
    data["kind_title"] = meta.get("title") or data.get("kind")
    data["kind_description"] = meta.get("description") or ""
    data["is_open"] = data.get("status") == "open"
    for key in ("first_seen_at", "last_seen_at", "resolved_at", "created_at", "updated_at"):
        data[key] = format_time(data.get(key))
    return data


def get_todo(todo_id: int) -> dict | None:
    """读单条待办；不存在返回 ``None``。"""
    row = get_conn().execute("SELECT * FROM todos WHERE id=?", (int(todo_id),)).fetchone()
    return todo_dict(row) if row is not None else None


def list_todos(
    *,
    status: str = "open",
    kind: str | None = None,
    library_id: int | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """在看板上列条目（信封格式，与 ``/api/tasks`` 一致）。

    ``status`` 取 ``open`` / ``done`` / ``all``（``all`` = 不过滤）；``kind`` / ``library_id``
    为 ``None`` 时不过滤。``limit`` / ``offset`` 在这里也就地收敛一次，方便测试直接调用
    （路由还会用 ``clamp_limit`` / ``clamp_offset`` 走一遍同一套规则）。

    ``query``（路由上的 ``q``）是**按作品筛选**：空/纯空白 = 不过滤，否则按
    **子串**匹配 ``title`` / ``detail`` / ``dedupe_key`` 三列（大小写不敏感；``%`` / ``_`` / ``\\``
    按字面处理，同 :func:`app.link_records.search_records`）。

    为什么匹配这三列而不是"作品名列"：``todos`` 是**全局一份**台账，条目记的是"发生过什么"，
    表里刻意没有 ``folder`` 列（作品名只出现在问题摘要、详情里的目录/路径、以及去重键里 ——
    例如 ``Work 03/Season 1``）。所以这是**朴素子串**筛选，不是"按作品精确归属"：它同样会命中
    恰好包含同一段文字的其它条目（见 ``docs/meta/KNOWN_ISSUES.md`` 的 KI-10）。``total`` 是过滤后的
    条数，翻页与它一致。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    if limit <= 0:
        limit = 50
    limit = min(limit, 200)
    try:
        offset = max(int(offset), 0)
    except (TypeError, ValueError):
        offset = 0

    conditions: list[str] = []
    params: list = []
    if status and status != "all":
        conditions.append("status=?")
        params.append(str(status))
    if kind:
        conditions.append("kind=?")
        params.append(str(kind))
    if library_id is not None:
        conditions.append("library_id=?")
        params.append(int(library_id))
    text = str(query or "").strip()
    if text:
        # 与 ``link_records.search_records`` 同一套口径：子串 + ESCAPE 让 ``%``/``_``/``\``
        # 按字面匹配（否则用户搜 "50%" 会命中所有条目）。
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        conditions.append(
            "(title LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\'"
            " OR dedupe_key LIKE ? ESCAPE '\\')"
        )
        params += [like, like, like]
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = get_conn()
    total = int(conn.execute(f"SELECT COUNT(*) AS c FROM todos{where}", params).fetchone()["c"])
    rows = conn.execute(
        f"SELECT * FROM todos{where} {_sort_clause()} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "items": [todo_dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def todo_summary() -> dict:
    """按状态与种类计数（首页徽标 + 筛选下拉框的选项都用它，一次查询搞定）。"""
    conn = get_conn()
    by_status = {"open": 0, "done": 0}
    for row in conn.execute("SELECT status, COUNT(*) AS c FROM todos GROUP BY status").fetchall():
        by_status[str(row["status"])] = int(row["c"])

    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT kind, status, COUNT(*) AS c FROM todos GROUP BY kind, status"
    ).fetchall():
        entry = counts.setdefault(str(row["kind"]), {"open": 0, "done": 0})
        entry[str(row["status"])] = int(row["c"])

    by_kind = []
    for kind, meta in KNOWN_KINDS.items():
        entry = counts.get(kind, {"open": 0, "done": 0})
        by_kind.append(
            {
                "kind": kind,
                "title": meta["title"],
                "severity": meta["severity"],
                "open": int(entry.get("open", 0)),
                "done": int(entry.get("done", 0)),
                "total": int(entry.get("open", 0)) + int(entry.get("done", 0)),
            }
        )
    # 未在注册表里登记的 kind（手改库/旧版本遗留）也要能被筛出来，否则条目会"看不见"。
    for kind, entry in counts.items():
        if kind in KNOWN_KINDS:
            continue
        by_kind.append(
            {
                "kind": kind,
                "title": kind,
                "severity": "warning",
                "open": int(entry.get("open", 0)),
                "done": int(entry.get("done", 0)),
                "total": int(entry.get("open", 0)) + int(entry.get("done", 0)),
            }
        )

    libraries = [
        {
            "library_id": row["library_id"],
            "library_name": row["library_name"],
            "open": int(row["open_count"]),
            "done": int(row["done_count"]),
            "total": int(row["open_count"]) + int(row["done_count"]),
        }
        for row in conn.execute(
            "SELECT library_id, library_name,"
            " SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_count,"
            " SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_count"
            " FROM todos GROUP BY library_id, library_name"
            " ORDER BY open_count DESC, library_id ASC"
        ).fetchall()
    ]

    enabled = enabled_kinds()
    return {
        "open": int(by_status.get("open", 0)),
        "done": int(by_status.get("done", 0)),
        "total": int(by_status.get("open", 0)) + int(by_status.get("done", 0)),
        "by_status": by_status,
        "by_kind": by_kind,
        "libraries": libraries,
        # 设置 `todo_kinds` 关掉的种类：前端可以据此提示"有些种类没在看板上"。
        "enabled_kinds": enabled,
        "disabled_kinds": [kind for kind in KNOWN_KINDS if kind not in enabled],
        # 一键检测：哪些种类能自动核对、哪些只能手工打钩。
        "detectable_kinds": sorted(DETECT_MODES),
        "undetectable_kinds": [kind for kind in KNOWN_KINDS if kind not in DETECT_MODES],
    }


# ----------------------------------------------------------------- 状态变更


def _set_status(todo_id: int, *, new_status: str) -> dict | None:
    """把条目改成 ``new_status``，返回改后的条目；不存在返回 ``None``。

    幂等：已经是目标状态的条目只读一次、**不重复写时间戳**（避免反复点同一个按钮把
    ``resolved_at`` 越改越新，掩盖"什么时候真正打完钩"）。数据一行都不删。
    """
    with tx() as conn:
        row = conn.execute("SELECT * FROM todos WHERE id=?", (int(todo_id),)).fetchone()
        if row is None:
            return None
        if str(row["status"]) != new_status:
            stamp = now_iso()
            resolved_at = stamp if new_status == "done" else None
            conn.execute(
                "UPDATE todos SET status=?, resolved_at=?, updated_at=? WHERE id=?",
                (new_status, resolved_at, stamp, int(todo_id)),
            )
            logger.info("待办 #%s 状态：%s -> %s", todo_id, row["status"], new_status)
        updated = conn.execute("SELECT * FROM todos WHERE id=?", (int(todo_id),)).fetchone()
    return todo_dict(updated) if updated is not None else None


def resolve_todo(todo_id: int) -> dict | None:
    """打钩：``open`` → ``done``（只改状态、绝不删数据）。"""
    return _set_status(todo_id, new_status="done")


def reopen_todo(todo_id: int) -> dict | None:
    """恢复未完成：``done`` → ``open``（清空 ``resolved_at``，历史条目照样在）。"""
    return _set_status(todo_id, new_status="open")


# ----------------------------------------------------------------- 一键检测


#: 哪些 kind 能用"文件系统实况"核对，以及核对方向：
#:
#: * ``missing`` —— 记录指向的那个文件**已经不在了**（用户手工改名/搬走/删掉，或者样本记的是
#:   当时的临时状态）⇒ 这条待办可以打钩；
#: * ``present`` —— 记录里**缺失**的东西现在补上了（后来某次运行把硬链接建上了）⇒ 可以打钩。
#:
#: 不在表里的两种（``task_failed`` / ``source_integrity``）没有单一可核对的文件路径：前者是
#: 任务级失败、后者是"源目录在任务期间发生过变动"，**文件在不在根本说明不了是否已处理**，
#: 所以只支持手工打钩 —— 能自动核对哪些、只能手工核对哪些，取决于问题本身有没有可核对的锚点。
DETECT_MODES: dict[str, str] = {
    "link_failed": "present",
    "rename_suffix": "missing",
    "unresolved_episode": "missing",
}


def _detect_tail(kind: str, record: dict, target_dir: str) -> tuple[str, str] | None:
    """算出这条待办该去核对的路径：返回 ``(匹配方式, 路径)``，算不出来返回 ``None``。

    * ``exact`` —— 相对媒体库根目录的完整路径（``link_failed`` 的样本带绝对 ``dst``）；
    * ``abs`` —— 连媒体库根都推不出来时的绝对路径兜底；
    * ``suffix`` —— 只能按"尾巴"匹配：L2/L4 的样本只记了 ``dir``（如 ``Season 1``）与文件名，
      **不含作品目录**，所以拿 ``Season 1/xxx.mkv`` 去和整棵树里的相对路径做尾部比较。
    """
    if kind == "link_failed":
        dst = str(record.get("dst") or "")
        if not dst:
            return None
        if target_dir:
            try:
                rel = os.path.relpath(dst, target_dir)
            except ValueError:  # Windows 上跨盘符
                rel = ""
            if rel and not rel.startswith(".."):
                return ("exact", rel.replace(os.sep, "/"))
        return ("abs", dst)

    if kind == "unresolved_episode":
        tail = str(record.get("path") or record.get("target") or "")
    else:  # rename_suffix（自己加后缀 / 占位者让位，样本都带 dir + target）
        directory = str(record.get("dir") or "").strip("/")
        name = str(record.get("target") or "")
        tail = "/".join(part for part in (directory, name) if part)
    return ("suffix", tail) if tail else None


def _walk_rel_paths(root: str) -> dict[str, set[str]]:
    """扫一遍目录，返回 ``basename -> {相对路径}``（一次遍历，供所有 suffix 匹配复用）。"""
    index: dict[str, set[str]] = {}
    if not root or not os.path.isdir(root):
        return index
    for dirpath, _dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir in (".", "") else rel_dir.replace(os.sep, "/") + "/"
        for name in filenames:
            index.setdefault(name, set()).add(prefix + name)
    return index


def _tail_present(index: dict[str, set[str]], tail: str) -> str:
    """suffix 匹配：命中返回那条相对路径，没命中返回空串。"""
    base = tail.rsplit("/", 1)[-1]
    for rel in index.get(base) or ():
        if rel == tail or rel.endswith("/" + tail):
            return rel
    return ""


def _library_dirs(library_id: Any, cache: dict) -> dict | None:
    """``{source_dir, target_dir}``（带缓存；库不存在返回 ``None``）。"""
    if library_id is None:
        return None
    key = int(library_id)
    if key not in cache:
        row = get_conn().execute(
            "SELECT source_dir, target_dir FROM libraries WHERE id=?", (key,)
        ).fetchone()
        cache[key] = (
            {
                "source_dir": str(row["source_dir"] or ""),
                "target_dir": str(row["target_dir"] or ""),
            }
            if row is not None
            else None
        )
    return cache[key]


def _detect_rows(*, kind: str | None, library_id: int | None, only_ids: list[int] | None):
    """取参与检测的未完成条目（``only_ids`` 给定时只取这些 id）。"""
    conditions = ["status='open'"]
    params: list = []
    if kind:
        conditions.append("kind=?")
        params.append(str(kind))
    else:
        conditions.append(f"kind IN ({','.join('?' for _ in DETECT_MODES)})")
        params.extend(DETECT_MODES)
    if library_id is not None:
        conditions.append("library_id=?")
        params.append(int(library_id))
    if only_ids is not None:
        ids = [int(item) for item in only_ids]
        if not ids:
            return []
        conditions.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    return get_conn().execute(
        f"SELECT * FROM todos WHERE {' AND '.join(conditions)} {_sort_clause()}", params
    ).fetchall()


def detect_resolved(
    *,
    kind: str | None = None,
    library_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    only_ids: list[int] | None = None,
) -> dict:
    """一键检测：挑出"文件系统实况已经说明它不用再管"的未完成待办（**只读，不改状态**）。

    证据来自两处：**待办自己记的时间线**（``task_id`` + ``dedupe_key``）+ **任务结果里的原始
    样本**（工序 3 之前写下的结构化字段），再拿它去和**媒体库当前的目录实况**比对：
    路径没了（``missing``）或缺失的东西补上了（``present``）⇒ 列为候选。

    返回的 ``candidates`` 就是弹窗里默认全选的那些条目；用户取消勾选的部分由前端剔除，真正的
    状态变更走 :func:`resolve_detected`（它会**再核对一遍**，避免用一次过期点击打钩）。
    """
    try:
        limit = max(int(limit), 1)
    except (TypeError, ValueError):
        limit = 200
    limit = min(limit, 200)
    try:
        offset = max(int(offset), 0)
    except (TypeError, ValueError):
        offset = 0

    rows = _detect_rows(kind=kind, library_id=library_id, only_ids=only_ids)
    conn = get_conn()
    results: dict[int, dict] = {}
    libraries: dict = {}
    walks: dict[int, dict[str, set[str]]] = {}
    candidates: list[dict] = []
    undetectable = 0
    missing = 0
    errors: list[str] = []

    for row in rows:
        todo = dict(row)
        tkind = str(todo.get("kind"))
        mode = DETECT_MODES.get(tkind)
        spec = _SAMPLE_SPECS.get(tkind)
        if not mode or spec is None:
            undetectable += 1
            continue
        task_id = todo.get("task_id")
        if task_id is None:
            missing += 1
            continue

        task_id = int(task_id)
        if task_id not in results:
            found = conn.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
            raw = found["result"] if found is not None else None
            try:
                results[task_id] = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                results[task_id] = {}
        result = results[task_id]
        if not isinstance(result, dict):
            result = {}

        # 用去重键把这条待办对回它当年那条结构化样本（样本没了 ⇒ 判不了，如实计入 missing）。
        record = None
        for item in _as_list(_dotted(result, spec["samples_key"])):
            candidate = _sample_record(item, spec)
            if candidate is None:
                continue
            if _event_key(candidate, spec) == str(todo.get("dedupe_key") or ""):
                record = candidate
                break

        lib = _library_dirs(todo.get("library_id"), libraries)
        if record is None or lib is None:
            missing += 1
            continue
        target_dir = lib["target_dir"]
        tail = _detect_tail(tkind, record, target_dir)
        if tail is None:
            missing += 1
            continue
        how, path = tail

        if how == "abs":
            hit = path if os.path.exists(path) else ""
        elif how == "exact":
            absolute = os.path.join(target_dir, path)
            hit = path if os.path.exists(absolute) else ""
        else:
            key = int(todo.get("library_id") or 0)
            if key not in walks:
                walks[key] = _walk_rel_paths(target_dir)
            hit = _tail_present(walks[key], path)

        if mode == "missing":
            if hit:
                continue  # 文件还在 ⇒ 这条待办还没解决，不列为候选
            evidence = f"记录中的路径已不存在：{path}"
        else:  # present
            if not hit:
                continue
            evidence = f"记录里缺失的路径现在已经存在：{hit}"

        item = todo_dict(row)
        item["detect_mode"] = mode
        item["detect_path"] = path
        item["detect_evidence"] = evidence
        candidates.append(item)

    total_candidates = len(candidates)
    if only_ids is None:
        page = candidates[offset : offset + limit]
    else:
        page = candidates

    return {
        "checked": len(rows),
        "detectable": len(rows) - undetectable,
        "checked_kinds": sorted({str(row["kind"]) for row in rows}) if rows else [],
        "candidates": page,
        "candidate_total": total_candidates,
        "skipped_missing_sample": missing,
        "undetectable": undetectable,
        "detectable_kinds": sorted(DETECT_MODES),
        "undetectable_kinds": [name for name in KNOWN_KINDS if name not in DETECT_MODES],
        "limit": limit,
        "offset": offset,
        "truncated": total_candidates > len(page),
        "errors": errors,
    }


def resolve_detected(todo_ids: Iterable[int]) -> dict:
    """把用户确认的条目打钩：**逐个再核对一遍**，拿不出证据的原样不动并说明原因。

    这样即便弹窗是几分钟前拉的、期间文件被挪回来了，也不会被一次过期点击误标为"已完成"。
    """
    wanted: list[int] = []
    for item in todo_ids or ():
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value not in wanted:
            wanted.append(value)
    if not wanted:
        return {"resolved": [], "skipped": [], "requested": 0}

    report = detect_resolved(only_ids=wanted)
    resolved: list[dict] = []
    for item in report["candidates"]:
        updated = _set_status(int(item["id"]), new_status="done")
        if updated is not None:
            updated["detect_evidence"] = item.get("detect_evidence", "")
            resolved.append(updated)
    done_ids = {int(item["id"]) for item in resolved}
    skipped = [
        {
            "id": todo_id,
            "reason": "文件系统实况不再支持自动打钩（文件又出现了，或原始样本已随任务记录清理）",
        }
        for todo_id in wanted
        if todo_id not in done_ids
    ]
    logger.info("待办一键检测：请求 %s 条，打钩 %s 条，跳过 %s 条", len(wanted), len(resolved), len(skipped))
    return {"resolved": resolved, "skipped": skipped, "requested": len(wanted)}
