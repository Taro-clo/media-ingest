"""库级规则覆盖与全局后缀白名单的公共层。

**三层解析，越高越优先**：

1. **库级覆盖** —— ``libraries.rule_override``（每库自己选命名字段/模板、
   自己的接受与拒绝目录正则、自己的后缀白名单）；只有用户显式填过的字段才覆盖，
   没填的字段一律沿用下一层；
2. **全局设置** —— ``settings.rename_extensions``（一个白名单，
   **应用于所有媒体库**）；空数组 = 未设置，继续往下走；
3. **处理器默认** —— ``processors/<id>.yaml`` 里的 ``extensions``（出厂清单，
   例如番剧/电影的 ``[".mkv", ".mp4", ".ass"]``）。

为什么要第 2 层：处理器清单是**按处理器**分家的，用户想加一个 ``.ts`` 就得逐个
去改处理器；而需求要的是"可修改、且应用于所有媒体库"。把全局值留空（默认）
就沿用处理器默认，所以这是一层**新增**能力，不是替换。

本模块只做"取值与合并"，**不做 HTTP 校验**：写入口（``PUT /api/settings``、
``PUT /api/libraries/{id}``）负责把非法值挡在库外并给出中文说明。
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from typing import Any, Iterable

from app import db
from app.processors import spec

#: 出厂后缀清单：三层全空时的兜底（与 ``spec.py`` 归一化期的默认值相同）。
#: 空白的白名单没有意义（会变成"哪个文件都不处理"），所以这里不给"空"这个选项。
DEFAULT_EXTENSIONS: tuple[str, ...] = (".mkv", ".mp4", ".ass")

#: 全局后缀白名单的设置键。
GLOBAL_EXTENSIONS_KEY = "rename_extensions"

#: 合法后缀的形状：点 + 字母/数字开头，后面只允许字母数字与 ``. _ + -``
#: （``.mkv`` / ``.mp4`` / ``.ass`` / ``.tar.gz`` / ``.7z`` / ``.x265-10bit`` 都合法）。
#: **空白绝不允许**：``".mkv .mp4"`` 是**一个**字符串而不是两个后缀，而
#: ``name.endswith((".mkv .mp4",))`` 永远为假 —— 用户会得到一份"看着生效、其实
#: 一个文件都匹配不上"的清单，且全程没有任何报错 —— 校验必须挡住这种输入。
EXTENSION_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._+-]*$")

#: 一项里塞进多个后缀是常见误操作（从文档/网页里整段复制）。
#: 空格、逗号、顿号、分号都当分隔符拆开，用户不必知道"数组元素"这件事。
_EXTENSION_SEPARATORS = re.compile(r"[\s,，、;；]+")


def split_extension_items(value: str) -> list[str]:
    """把**一项**切成候选后缀 token（容忍 ``".mkv .mp4 .ass"`` 这种把多项塞进一项的输入）。

    只负责按分隔符切分，不做合法性判断 —— 判断交给 :func:`is_valid_extension`。
    """
    return [token for token in _EXTENSION_SEPARATORS.split(value.strip()) if token]


def is_valid_extension(token: str) -> bool:
    """`token` 是不是合法后缀（点开头、无空白、只含字母数字与 ``. _ + -``）。"""
    return bool(EXTENSION_PATTERN.match(token))


def normalize_extensions(values: Any) -> list[str]:
    """把任意来源的后缀清单归一化成 ``[".mkv", ...]``。

    规则：必须是字符串、以 ``.`` 开头、**不含空白**、统一小写、去重且保持原顺序。
    一项里塞了多个后缀（``".mkv .mp4 .ass"``）时按 :func:`split_extension_items` 拆开，
    所以坏的存量值会被**修正**成三项，而不是整体留着害人。
    不合规的 **token** 直接丢弃（写入口已经拦过一遍；这里是读取路径，宁少不要错）。
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    items: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for token in split_extension_items(value):
            text = token.lower()
            if not is_valid_extension(text):
                continue
            if text not in items:
                items.append(text)
    return items


def read_global_extensions(stored: str | None = None) -> list[str]:
    """读全局后缀白名单；未设置/存坏了都返回 ``[]``（= 跟随下一层）。

    这里连"设置表还不存在"也要容错：引擎要能在**没有数据库**的环境里跑完一次重命名
    （回归测试里就有这种环境，它只关心处理器规格本身的语义）。
    生产路径上 ``init_db()`` 一定先于任何重命名，
    所以这个兜底只会退回"跟随处理器"，不会悄悄改变线上行为。
    """
    if stored is None:
        try:
            stored = db.get_setting(GLOBAL_EXTENSIONS_KEY)
        except (sqlite3.Error, RuntimeError):
            return []
    if not stored:
        return []
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        return []
    return normalize_extensions(parsed)


def effective_extensions(
    spec_values: Iterable[str] | None,
    override_values: Iterable[str] | None = None,
    global_values: Iterable[str] | None = None,
) -> list[str]:
    """按三层优先级算出**某一次重命名实际生效**的后缀白名单。

    ``global_values`` 显式传 ``None`` 时自己去读设置表（调用方要跑一批任务时可以
    读一次传进来，省掉每库一次查库）。
    """
    override_resolved = normalize_extensions(override_values)
    if override_resolved:
        return override_resolved
    if global_values is None:
        global_values = read_global_extensions()
    global_resolved = normalize_extensions(global_values)
    if global_resolved:
        return global_resolved
    spec_resolved = normalize_extensions(spec_values)
    if spec_resolved:
        return spec_resolved
    return list(DEFAULT_EXTENSIONS)


# ── 库级规则覆盖 ────────────────────────────────────────────
#
# 形状（``libraries.rule_override``，JSON 文本，可空）::
#
#     {
#       "extensions": [".mkv", ".ts"],                     # 全局后缀白名单的库级层（可选）
#       "traverse": {                                       # 库级目录规则
#         "accept_dirs": [{"pattern": "^Season \\d+$", "season_group": 1}],
#         "reject_dirs": [{"pattern": "特典|SP"}],
#         "file_scope": "subdir",
#         "skip_season_zero": true
#       },
#       "naming": {                                         # 库级命名覆盖
#         "template": "{title} S{season}E{episode}{suffix}",
#         "segments": [{"param": "title"}],                 # 与 template 二选一
#         "expressions": {"ep": "..."}                      # 可选，按名字增量覆盖
#       }
#     }
#
# **只有用户显式填过的键才覆盖**：数组整体替换（不做元素级合并），字典按子键合并，
# ``traverse.title_depth`` 不可覆盖（规格只支持 1，允许改只会造出永远跑不通的库）。

#: 允许出现在覆盖里的顶层键。
OVERRIDE_KEYS: tuple[str, ...] = ("extensions", "traverse", "naming")

#: 允许覆盖的 ``traverse`` 子键（``title_depth`` 刻意不在其中）。
OVERRIDE_TRAVERSE_KEYS: tuple[str, ...] = (
    "accept_dirs", "reject_dirs", "file_scope", "skip_season_zero",
)

#: 允许覆盖的 ``naming`` 子键。
OVERRIDE_NAMING_KEYS: tuple[str, ...] = ("template", "segments", "expressions")


def override_is_empty(override: Any) -> bool:
    """覆盖是不是"什么都没填"（空/None/没有生效键都算空）。"""
    if not isinstance(override, dict):
        return True
    for key in OVERRIDE_KEYS:
        value = override.get(key)
        if value in (None, "", [], {}):
            continue
        return False
    return True


def normalize_override(override: Any) -> dict[str, Any]:
    """把覆盖归一化成规范形状（**不校验**；非法值留在原处交给 :func:`validate_override`）。

    归一化做的事：丢掉空值、``accept_dirs`` 补 ``season_group``、``naming.template``
    编译成 ``naming.segments``（见 :func:`app.processors.spec.compile_template`）。
    模板编译在这里发生，于是引擎与规格校验器都不需要认识 ``template``。

    **必须幂等**：路由把归一化结果存进 ``libraries.rule_override``，读回来时会再跑一次本
    函数（见 :func:`read_library_override`）。所以 ``template`` 只有"模板原文"一个来源、
    永远优先于 ``segments`` 并被重新编译 —— 否则模板文字会在第一次落库后就丢掉，WebUI
    再也看不到用户写的那串模板。
    """
    if not isinstance(override, dict):
        return {}
    result: dict[str, Any] = {}

    extensions = normalize_extensions(override.get("extensions"))
    if extensions:
        result["extensions"] = extensions

    traverse_in = override.get("traverse")
    if isinstance(traverse_in, dict):
        traverse: dict[str, Any] = {}
        for key in OVERRIDE_TRAVERSE_KEYS:
            if key not in traverse_in:
                continue
            value = traverse_in[key]
            if value in (None, "", []):
                continue
            if key == "accept_dirs":
                traverse[key] = spec._normalize_dir_rules(value, with_group=True)
            elif key == "reject_dirs":
                traverse[key] = spec._normalize_dir_rules(value, with_group=False)
            else:
                traverse[key] = value
        if traverse:
            result["traverse"] = traverse

    naming_in = override.get("naming")
    if isinstance(naming_in, dict):
        naming: dict[str, Any] = {}
        expressions = naming_in.get("expressions")
        if isinstance(expressions, dict) and expressions:
            naming["expressions"] = {
                str(key): value for key, value in expressions.items()
            }
        segments = naming_in.get("segments")
        template = naming_in.get("template")
        if isinstance(template, str) and template.strip():
            # 模板优先，且**总是**重编译：既支持用户直接给模板，也让"模板原文 + 编译结果"
            # 这份已归一化数据在再次进入本函数时保持不变（幂等）。
            naming["segments"] = spec.compile_template(template)
            naming["template"] = template
        elif isinstance(segments, list) and segments:
            naming["segments"] = [
                dict(item) if isinstance(item, dict) else item for item in segments
            ]
        if naming:
            result["naming"] = naming
    return result


def validate_override(
    override: Any, *, known_names: tuple[str, ...] | None = None
) -> list[dict[str, str]]:
    """逐条检查一份覆盖，返回字段级中文错误（空 = 合法）。

    只做"覆盖自己能不能读懂"的结构检查；**跨层冲突**（例如 ``file_scope=title`` 与
    ``accept_dirs`` 同时生效）由调用方把覆盖应用到真实 spec 后再跑一遍
    :func:`app.processors.spec.validate_spec` 来发现 —— 那才是唯一能判定冲突的地方。

    ``known_names``：这份覆盖的模板能读到哪些变量名。调用方
    （:func:`resolve_override`）会把**基本 spec 的表达式名 + 覆盖自己的表达式名**一起传进来，
    好让"模板里引用自定义表达式"不被误报成未知变量；不传则退回默认清单
    （``TEMPLATE_VARS`` + 引擎内部键）。
    """
    errors: list[dict[str, str]] = []

    def err(field_name: str, message: str) -> None:
        errors.append({"field": field_name, "message": message})

    if override in (None, "", {}, []):
        return errors
    if not isinstance(override, dict):
        return [{"field": "rule_override", "message": "本库规则覆盖必须是一个对象。"}]

    for key in override:
        if key not in OVERRIDE_KEYS:
            err(f"rule_override.{key}", f"未知字段：{key}（只允许 {'、'.join(OVERRIDE_KEYS)}）。")

    if "extensions" in override:
        raw = override.get("extensions")
        if raw not in (None, "", []):
            if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
                err(
                    "rule_override.extensions",
                    "extensions 必须是数组，例如 [\".mkv\", \".mp4\"]。",
                )
            else:
                for index, item in enumerate(raw):
                    if not isinstance(item, str) or not item.strip().startswith(".") \
                            or len(item.strip()) < 2:
                        err(
                            f"rule_override.extensions[{index}]",
                            f"后缀必须以点开头，当前是 {item!r}。",
                        )

    traverse_in = override.get("traverse")
    if traverse_in is not None:
        if not isinstance(traverse_in, dict):
            err("rule_override.traverse", "traverse 必须是一个对象。")
        else:
            for key in traverse_in:
                if key not in OVERRIDE_TRAVERSE_KEYS:
                    if key == "title_depth":
                        err(
                            "rule_override.traverse.title_depth",
                            "title_depth 不可按库覆盖（当前只支持 1：标题文件夹必须是库根下的直接子目录）。",
                        )
                    else:
                        err(
                            f"rule_override.traverse.{key}",
                            f"未知字段：{key}（只允许 {'、'.join(OVERRIDE_TRAVERSE_KEYS)}）。",
                        )
            if "file_scope" in traverse_in:
                scope = traverse_in.get("file_scope")
                if scope not in spec.FILE_SCOPES:
                    err(
                        "rule_override.traverse.file_scope",
                        f"file_scope 必须是 {' 或 '.join(spec.FILE_SCOPES)}，当前是 {scope!r}。",
                    )
            if "skip_season_zero" in traverse_in and not isinstance(
                traverse_in.get("skip_season_zero"), bool
            ):
                err(
                    "rule_override.traverse.skip_season_zero",
                    "skip_season_zero 必须是 true/false。",
                )
            for key in ("accept_dirs", "reject_dirs"):
                if key in traverse_in:
                    _check_override_dir_rules(
                        traverse_in.get(key),
                        f"rule_override.traverse.{key}",
                        err,
                        with_group=(key == "accept_dirs"),
                    )

    naming_in = override.get("naming")
    if naming_in is not None:
        if not isinstance(naming_in, dict):
            err("rule_override.naming", "naming 必须是一个对象。")
        else:
            for key in naming_in:
                if key not in OVERRIDE_NAMING_KEYS:
                    err(
                        f"rule_override.naming.{key}",
                        f"未知字段：{key}（只允许 {'、'.join(OVERRIDE_NAMING_KEYS)}）。",
                    )
            template = naming_in.get("template")
            if template is not None:
                if isinstance(template, str) and not template.strip():
                    err("rule_override.naming.template", "命名模板不能是空白。")
                for message in spec.template_errors(template, known_names=known_names):
                    err("rule_override.naming.template", message)
            segments = naming_in.get("segments")
            if segments is not None and (not isinstance(segments, list) or not segments):
                err(
                    "rule_override.naming.segments",
                    "naming.segments 必须是非空数组（或改用 naming.template）。",
                )
            expressions = naming_in.get("expressions")
            if expressions is not None:
                if not isinstance(expressions, dict):
                    err("rule_override.naming.expressions", "naming.expressions 必须是对象。")
                else:
                    for name, value in expressions.items():
                        if not isinstance(value, str):
                            err(
                                f"rule_override.naming.expressions.{name}",
                                "表达式必须是字符串。",
                            )
    return errors


def _check_override_dir_rules(value: Any, prefix: str, err, *, with_group: bool) -> None:
    """覆盖里的 ``accept_dirs`` / ``reject_dirs`` 体检（与 :mod:`app.processors.spec` 同源语义）。"""
    if not isinstance(value, list):
        err(prefix, "必须是数组，例如 [{\"pattern\": \"^Season \\\\d+$\", \"season_group\": 1}]。")
        return
    for index, item in enumerate(value):
        where = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            err(where, "每项必须是对象：{pattern, season_group?}。")
            continue
        for key in item:
            if key not in ("pattern", "season_group"):
                err(f"{where}.{key}", f"只允许 pattern / season_group，出现未知键 {key!r}。")
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            err(f"{where}.pattern", "缺少 pattern 正则。")
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            err(f"{where}.pattern", f"正则无法编译：{exc}。")
            continue
        if with_group:
            group = item.get("season_group", 1)
            if not isinstance(group, int) or isinstance(group, bool) or group < 0:
                err(f"{where}.season_group", "season_group 必须是 >=0 的整数。")
            elif group > compiled.groups:
                err(
                    f"{where}.season_group",
                    f"season_group={group} 超出正则的捕获组数量（{compiled.groups}）。",
                )


def apply_override(data: dict[str, Any], override: Any) -> dict[str, Any]:
    """把库级覆盖应用到一份（已归一化的）spec 上，返回新的 dict。

    * ``traverse``：数组整体替换，标量覆盖；
    * ``naming.expressions``：按名字增量合并（只覆盖同名表达式，其余保留）；
    * ``naming.template`` / ``naming.segments``：整体替换处理器原有的段序列
      （用户既然写了本库的命名方式，就该完全按他写的来，而不是"在处理器后面再拼一段"）；
    * ``extensions`` 不在这里处理 —— 它有三层优先级（库 → 全局 → 处理器），由
      :func:`effective_extensions` 统一算，避免两条并行的取值路径。

    输入不做净化也不抛异常：传入的覆盖应当先过 :func:`validate_override`。
    """
    merged = copy.deepcopy(data) if isinstance(data, dict) else {}
    if not isinstance(override, dict) or not override:
        return merged

    traverse_in = override.get("traverse")
    if isinstance(traverse_in, dict) and traverse_in:
        traverse = dict(merged.get("traverse") or {})
        for key, value in traverse_in.items():
            if key in OVERRIDE_TRAVERSE_KEYS:
                traverse[key] = copy.deepcopy(value)
        merged["traverse"] = traverse

    naming_in = override.get("naming")
    if isinstance(naming_in, dict) and naming_in:
        naming = dict(merged.get("naming") or {})
        expressions = dict(naming.get("expressions") or {})
        extra = naming_in.get("expressions")
        if isinstance(extra, dict):
            for name, value in extra.items():
                expressions[str(name)] = value
        naming["expressions"] = expressions
        segments = naming_in.get("segments")
        if isinstance(segments, list) and segments:
            naming["segments"] = copy.deepcopy(segments)
        elif isinstance(naming_in.get("template"), str) and naming_in["template"].strip():
            naming["segments"] = spec.compile_template(naming_in["template"])
        merged["naming"] = naming

    return merged


def read_library_override(stored: Any) -> dict[str, Any]:
    """把 ``libraries.rule_override`` 这一列读成 dict（空/坏值一律 ``{}`` = 不覆盖）。"""
    if not stored:
        return {}
    if isinstance(stored, dict):
        parsed: Any = stored
    else:
        try:
            parsed = json.loads(str(stored))
        except (TypeError, ValueError):
            return {}
    try:
        return normalize_override(parsed)
    except Exception:  # pragma: no cover - normalize_override 本身不抛，兜底防未来改动
        return {}


def _merged_known_names(raw: Any, spec_data: Any) -> tuple[str, ...]:
    """合并后可用的变量名：基本 spec 的表达式名 + 覆盖自己的表达式名 + 变量表。

    覆盖里的模板可以引用两边任意一个表达式（``apply_override`` 会把表达式按名字并起来），
    所以校验模板里的名字时也必须按"合并之后"算，否则会误报未知变量。
    """
    expressions: dict[str, Any] = {}
    for source in (spec_data, raw):
        naming = source.get("naming") if isinstance(source, dict) else None
        if isinstance(naming, dict) and isinstance(naming.get("expressions"), dict):
            expressions.update(naming["expressions"])
    return spec.known_context_names(expressions)


def resolve_override(
    raw: Any, spec_data: dict[str, Any]
) -> tuple[str | None, list[dict[str, str]]]:
    """校验一份覆盖并把它变成写进 ``libraries.rule_override`` 的 JSON 文本。

    返回 ``(落库文本或 None, 字段级错误)``；两者互斥 —— 有错误时第一项恒为 ``None``。
    校验分两步：先查覆盖自己读不读得懂（形状 / 正则 / 模板变量），再把它**应用到真实
    处理器 spec** 上跑一遍 :func:`app.processors.spec.validate_spec` —— 跨层冲突（典型
    是处理器 ``file_scope=title`` 而库级却写了 ``accept_dirs``）只有合并之后才看得见。

    这是 API（``POST/PUT /api/libraries``）与配置文件导入（``libraries.yaml``）**共用**
    的唯一入口，两边的口径必须一致；调用方只负责把 ``(field, message)`` 翻成自己的错误
    形态（HTTP 422 或 ``ValueError``）。
    """
    if raw in (None, {}, [], ""):
        return None, []
    problems = validate_override(raw, known_names=_merged_known_names(raw, spec_data))
    if problems:
        return None, problems
    normalized = normalize_override(raw)
    merged = apply_override(spec.normalize_spec(spec_data), normalized)
    problems = [
        {
            "field": f"rule_override.{item.get('field')}" if item.get("field") else "rule_override",
            "message": str(item.get("message") or ""),
        }
        for item in spec.validate_spec(merged)
    ]
    if problems:
        return None, problems
    if override_is_empty(normalized):
        return None, []
    return json.dumps(normalized, ensure_ascii=False), []
