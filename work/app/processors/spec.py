"""处理器规格（ProcessorSpec）的解析、补默认与校验。

唯一契约真源是 ``docs/contracts/PROCESSORS.md`` §3。本模块只做"数据"的事：把一份 dict 规范化成
完整 spec，并给出**字段级**中文错误列表（``{"field": "traverse.accept_dirs[0].pattern",
"message": "..."}``），供 HTTP 层包成 422 ``detail[]``。

设计要点：

* ``validate_spec`` **不抛异常**，永远返回错误列表（空列表 = 合法）。这样 WebUI 的
  ``POST /api/processors/validate`` 与真正的写入校验共用同一份判定。
* ``normalize_spec`` 负责补默认值并把 ``schema_version`` 写成 2；顺序（键的相对次序）尽量
  保持调用者给定的顺序，只有缺失的键才追加，方便"手工编辑友好"的 YAML 输出。
* 正则一律在**校验期**编译一次，避免运行期才发现写错。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "PROCESSOR_KINDS",
    "FILE_SCOPES",
    "SELECTION_MODES",
    "LOG_FORMATS",
    "ID_PATTERN",
    "SPEC_TEMPLATES",
    "TEMPLATE_VARS",
    "compile_template",
    "template_errors",
    "SpecValidationError",
    "ProcessorSpec",
    "normalize_spec",
    "validate_spec",
    "iter_errors",
    "slugify_id",
]

#: 当前 spec 版本；任何其它值都视为不兼容
SCHEMA_VERSION = 2

#: §3 表：kind 只有这两类（``hardlink_only`` 只建链接，``rename`` 走规则引擎）
PROCESSOR_KINDS: tuple[str, ...] = ("rename", "hardlink_only")

#: 文件作用域：``subdir`` = Bangumi 语义（文件在标题文件夹下匹配 accept_dirs 的子目录里）；
#: ``title`` = Movie 语义（文件直接躺在标题文件夹里）
FILE_SCOPES: tuple[str, ...] = ("subdir", "title")

#: 同后缀取哪个文件
SELECTION_MODES: tuple[str, ...] = ("file", "extension")

#: 日志形态；当前只有 json_cache 一种
LOG_FORMATS: tuple[str, ...] = ("json_cache",)

#: 处理器 id 规则（§3）
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

#: ``POST /api/processors`` 的 template 取值（§7）
SPEC_TEMPLATES: tuple[str, ...] = ("movie", "bangumi", "hardlink_only", "blank")

_SEGMENT_KEYS = ("param", "literal", "pad")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SpecValidationError(ValueError):
    """spec 不合法。``errors`` 是字段级列表，可直接塞进 422 的 detail。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        message = "；".join(f"{item['field']}: {item['message']}" for item in errors) or "spec 不合法"
        super().__init__(message)


@dataclass
class ProcessorSpec:
    """一份完整的处理器规格（字段与 §3 一一对应，缺一不可）。"""

    schema_version: int = SCHEMA_VERSION
    id: str = ""
    name: str = ""
    description: str = ""
    kind: str = "rename"
    enabled: bool = True
    builtin: bool = False
    traverse: dict[str, Any] = field(default_factory=dict)
    extensions: list[str] = field(default_factory=list)
    naming: dict[str, Any] = field(default_factory=dict)
    episode_patterns: list[dict[str, Any]] = field(default_factory=list)
    episode_fallback: Any = None
    selection: dict[str, Any] = field(default_factory=dict)
    log_file: str = ""
    log_format: str = "json_cache"

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProcessorSpec":
        normalized = normalize_spec(data)
        return cls(
            schema_version=normalized["schema_version"],
            id=normalized["id"],
            name=normalized["name"],
            description=normalized.get("description", ""),
            kind=normalized["kind"],
            enabled=normalized["enabled"],
            builtin=normalized["builtin"],
            traverse=normalized["traverse"],
            extensions=normalized["extensions"],
            naming=normalized["naming"],
            episode_patterns=normalized["episode_patterns"],
            episode_fallback=normalized["episode_fallback"],
            selection=normalized["selection"],
            log_file=normalized["log_file"],
            log_format=normalized["log_format"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "enabled": self.enabled,
            "builtin": self.builtin,
            "traverse": copy.deepcopy(self.traverse),
            "extensions": list(self.extensions),
            "naming": copy.deepcopy(self.naming),
            "episode_patterns": copy.deepcopy(self.episode_patterns),
            "episode_fallback": self.episode_fallback,
            "selection": copy.deepcopy(self.selection),
            "log_file": self.log_file,
            "log_format": self.log_format,
        }

    # ── 便捷属性 ────────────────────────────────────────────────────────
    @property
    def is_rename(self) -> bool:
        return self.kind == "rename"

    @property
    def file_scope(self) -> str:
        return str(self.traverse.get("file_scope", "subdir"))


def slugify_id(value: str) -> str:
    """把任意文本转成合法的处理器 id 片段（小写、非 ``[a-z0-9]`` 折叠为 ``-``）。"""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"p-{slug}" if slug else "processor"
    return slug[:32].strip("-") or "processor"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# ── 命名模板（calibre 风格的 {字段} 拼装）────────────────────────────────
#
# 契约：``docs/contracts/PROCESSORS.md`` §3.2。模板是 ``naming.segments`` 的**更好写的等价写法**：
# 编译期一次性展开成段序列，运行期（``app/processors/engine.py``）只认段序列，因此引擎
# 不需要知道模板的存在。
#
# 之所以只做这几件事：段序列的语义（param 查上下文、pad 补零、literal 直出）已经覆盖了
# 模板需要的全部能力；模板多出来的语法越少，"写错了"能被校验抓到的比例就越高。

#: 模板里可用的变量名 = 引擎 ``_file_context`` 提供的上下文键（顺序即 WebUI 变量表的顺序）。
#: ``tests/test_library_overrides.py`` 会断言这份清单与 ``engine.sample_context()`` 的键完全一致，
#: 并顺带比对 ``web/js/processors_util.js`` 的 ``PROC_PARAMS``，防止三边漂移。
TEMPLATE_VARS: tuple[str, ...] = (
    "title", "filename", "stem", "ext", "extname",
    "season_raw", "season", "episode_raw", "episode", "suffix",
    "works_dir", "rel_dir",
)

#: 引擎 ``_file_context`` 还会给出这些**内部键** —— 它们不在 WebUI 变量表
#: （``PROC_PARAMS``）里，但段序列与 ``naming.expressions`` 都读得到，所以校验"名字认不认识"
#: 时必须算进来。``episode_resolved`` = 这个文件有没有解析出集数（False 时 ``episode`` 为空串）。
ENGINE_ONLY_VARS: tuple[str, ...] = ("episode_resolved",)


def known_context_names(expressions: Any = None) -> tuple[str, ...]:
    """这次命名真正能读到的变量名 = ``TEMPLATE_VARS`` + ``ENGINE_ONLY_VARS`` + 表达式名。

    段序列的 ``param`` 名如果只校验形状（是字符串就放行），运行期查不到就会**静默
    少一段**；所以保存期用这份清单拦截，运行期再用它记一条 warning（``docs/meta/KNOWN_ISSUES.md``）。
    表达式名按书写顺序追加（表达式可以引用更早的表达式，所以它们都是"可用的变量名"）。
    """
    names: list[str] = list(TEMPLATE_VARS) + list(ENGINE_ONLY_VARS)
    if isinstance(expressions, dict):
        for key in expressions:
            name = str(key)
            if name not in names:
                names.append(name)
    return tuple(names)

#: 模板里变量的写法：``{name}`` 或 ``{name:0N}``（N = 补零位数，如 ``{episode:02}``）。
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]*)\}")
_TEMPLATE_BRACE = re.compile(r"\{\{|\}\}")


def compile_template(text: str, *, known_names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """把命名模板编译成段序列（``[{param|literal, pad?}]``）。

    * ``{title}`` ⇒ ``{"param": "title"}``
    * ``{episode:02}`` ⇒ ``{"param": "episode", "pad": 2}``
    * ``{{`` / ``}}`` ⇒ 字面量花括号
    * 其余文本原样成为 ``{"literal": ...}``

    未知变量**照旧编译成 param 段**（运行期由引擎给出"未知参数"警告），是否报错由
    :func:`template_errors` 决定 —— 这样"保存时拦住写错的名字"与"运行期绝不崩"两件事
    互不干扰。
    """
    source = text if isinstance(text, str) else ""
    segments: list[dict[str, Any]] = []
    buffer: list[str] = []
    index = 0
    length = len(source)

    def flush() -> None:
        if buffer:
            segments.append({"literal": "".join(buffer)})
            buffer.clear()

    while index < length:
        char = source[index]
        if char != "{":
            if char == "}" and source.startswith("}}", index):
                buffer.append("}")
                index += 2
                continue
            buffer.append(char)
            index += 1
            continue
        if source.startswith("{{", index):
            buffer.append("{")
            index += 2
            continue
        match = _TEMPLATE_TOKEN.match(source, index)
        if match is None:
            # 落单的 "{"：当字面量处理，避免"写了个 { 就整份模板失效"。
            buffer.append("{")
            index += 1
            continue
        body = match.group(1).strip()
        index = match.end()
        name, _, pad_text = body.partition(":")
        name = name.strip()
        pad_text = pad_text.strip()
        if not name:
            buffer.append(match.group(0))
            continue
        segment: dict[str, Any] = {"param": name}
        if pad_text:
            width = pad_text.lstrip("0") or "0"
            # 认不出来的宽度写法（例如 calibre 的 "0>3s"）**只丢掉补零**、照常填值：
            # 运行期永不因为"模板里多写了个冒号"而整份失败，写错由 :func:`template_errors`
            # 在保存时拦住。
            if width.isdigit() and int(width) >= 1:
                segment["pad"] = int(width)
        flush()
        segments.append(segment)
    flush()
    # 合并相邻的 literal 段没有意义（渲染结果相同），但保持稳定可预测更重要：
    # 这里只丢掉"空字面量"，避免 `{a}{b}` 之间塞一个空段。
    return [item for item in segments if item.get("literal") != ""] or [{"literal": ""}]


def template_errors(
    text: Any, *, known_names: tuple[str, ...] | None = None
) -> list[str]:
    """模板体检：返回中文错误列表（空 = 合法）。供"保存时拦住写错"用。"""
    if text is None:
        return []
    if not isinstance(text, str):
        return ["命名模板必须是字符串。"]
    errors: list[str] = []
    names = known_names if known_names is not None else known_context_names()
    known = set(names)
    for body in _TEMPLATE_TOKEN.findall(_TEMPLATE_BRACE.sub("", text)):
        body = body.strip()
        if not body:
            errors.append("模板里出现了空的 {}，请写变量名或对花括号转义为 {{ }}。")
            continue
        name, _, pad_text = body.partition(":")
        name = name.strip()
        if not _IDENTIFIER.match(name):
            errors.append(
                f"模板变量名不合法：{{{body}}}（变量名只能是字母、数字、下划线，且不以数字开头）。"
            )
            continue
        if name not in known:
            errors.append(
                f"模板里用了未知变量 {{{name}}}。可用变量：{'、'.join(names)}。"
            )
        pad_text = pad_text.strip()
        if pad_text:
            width = pad_text.lstrip("0") or "0"
            if not width.isdigit() or int(width) < 1:
                errors.append(
                    f"模板变量 {{{body}}} 的补零写法不合法：只支持 {{名称:0位数}}，例如 {{episode:02}}。"
                )
    return errors


def normalize_episode_fallback(value: Any) -> Any:
    """把"集数兜底"整流：**空串 / 纯空白串与 ``null`` 等价**。

    WebUI 的"集数兜底"输入框被清空后提交的是 ``""``，旧代码把它当成"配置了兜底"：
    ``_episode_raw`` 返回空串 ⇒ ``episode_resolved=True`` ⇒ ``str("").zfill(2)`` 渲染成
    ``00``，文件名长成 ``…S01E00.mkv``。清空输入框的语义显然是"没配兜底"（= 没解析出集数就
    保留原名），所以空白串一律归一到 ``None``；非字符串、非空白字符串原样返回，交给
    :func:`validate_spec` 报错。
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def normalize_spec(data: dict[str, Any] | None) -> dict[str, Any]:
    """补默认值并规范化一份 spec（**不校验**；非法值原样保留交给 ``validate_spec``）。

    返回的 dict 键顺序：结构与 §3 的书写顺序一致 —— 缺失的键在末尾补上，已有的键保持
    调用者给的相对顺序（YAML 输出要"手工编辑友好"，不能按字母排序）。
    """
    source: dict[str, Any] = dict(data or {})
    result: dict[str, Any] = {}

    # schema_version 永远由服务端写
    result["schema_version"] = SCHEMA_VERSION
    for key in ("id", "name", "description", "kind"):
        if key in source:
            result[key] = source[key]
    result.setdefault("id", "")
    result.setdefault("name", "")
    result.setdefault("description", "")
    result.setdefault("kind", "rename")
    result["enabled"] = _as_bool(source.get("enabled", True))
    result["builtin"] = _as_bool(source.get("builtin", False))
    # `reference_script` 不属于现行 spec：老数据库行 / 老 YAML / 老客户端仍可能带这个键，
    # 这里**静默丢弃**，既不报"未知字段"也不向上暴露。
    result.pop("reference_script", None)

    # ── traverse ────────────────────────────────────────────────────────
    traverse_in = source.get("traverse") if isinstance(source.get("traverse"), dict) else {}
    traverse: dict[str, Any] = {}
    traverse["title_depth"] = traverse_in.get("title_depth", 1)
    traverse["file_scope"] = traverse_in.get("file_scope", "subdir")
    traverse["accept_dirs"] = _normalize_dir_rules(traverse_in.get("accept_dirs"), with_group=True)
    traverse["reject_dirs"] = _normalize_dir_rules(traverse_in.get("reject_dirs"), with_group=False)
    if traverse["accept_dirs"] is None:
        # §3：这两个字段都可以省略 —— 省略 == “没有规则”，交给校验期报错的只有"写错形状"
        traverse["accept_dirs"] = []
    if traverse["reject_dirs"] is None:
        traverse["reject_dirs"] = []
    traverse["skip_season_zero"] = _as_bool(traverse_in.get("skip_season_zero", True))
    result["traverse"] = traverse

    # ── extensions ──────────────────────────────────────────────────────
    extensions = source.get("extensions")
    if extensions is None:
        extensions = [".mkv", ".mp4", ".ass"]
    if isinstance(extensions, (list, tuple)):
        result["extensions"] = [item for item in extensions]
    else:
        result["extensions"] = extensions

    # ── naming ──────────────────────────────────────────────────────────
    naming_in = source.get("naming") if isinstance(source.get("naming"), dict) else {}
    expressions = naming_in.get("expressions") or {}
    segments = naming_in.get("segments") or []
    if not segments:
        # 省略 naming.segments 时给一个"只写标题"的最小拼装（与 blank 模板一致），
        # 避免"少写一个键就整份 spec 校验不过"这种反直觉的失败。
        segments = [{"param": "title"}]
    result["naming"] = {
        "expressions": dict(expressions) if isinstance(expressions, dict) else expressions,
        "segments": [dict(item) if isinstance(item, dict) else item for item in segments]
        if isinstance(segments, (list, tuple))
        else segments,
    }

    # ── episode_patterns / fallback ─────────────────────────────────────
    patterns = source.get("episode_patterns") or []
    result["episode_patterns"] = [
        dict(item) if isinstance(item, dict) else item for item in patterns
    ] if isinstance(patterns, (list, tuple)) else patterns
    result["episode_fallback"] = normalize_episode_fallback(source.get("episode_fallback"))

    # ── selection ───────────────────────────────────────────────────────
    selection_in = source.get("selection") if isinstance(source.get("selection"), dict) else {}
    result["selection"] = {"per": selection_in.get("per", "file")}

    # ── log ─────────────────────────────────────────────────────────────
    result["log_file"] = source.get("log_file", "")
    result["log_format"] = source.get("log_format", "json_cache")

    # 保留调用者带来的未知键（校验期会报"未知键"，但不在这里丢）。
    # `reference_script` 不属于"未知键"——老配置带它时必须静默丢弃，
    # 不能在这里又被搬回来（否则 API 会把一个悬空指针重新暴露出去）。
    for key, value in source.items():
        if key not in result and key != "reference_script":
            result[key] = value
    return result


def _normalize_dir_rules(value: Any, *, with_group: bool) -> Any:
    if not isinstance(value, (list, tuple)):
        return value
    rules: list[Any] = []
    for item in value:
        if not isinstance(item, dict):
            rules.append(item)
            continue
        rule: dict[str, Any] = {"pattern": item.get("pattern", "")}
        if with_group:
            rule["season_group"] = item.get("season_group", 1)
        rules.append(rule)
    return rules


# ── 校验 ────────────────────────────────────────────────────────────────


def iter_errors(
    data: dict[str, Any] | None, *, require_id: bool = False, strict_names: bool = True
) -> list[dict[str, str]]:
    """逐条检查 spec，返回字段级中文错误列表（空 = 合法）。

    ``strict_names``：是否把"段序列里用了未知变量名"当成**错误**。保存路径
    （API / 配置文件导入 / 内置预设注册）一律用默认的 ``True``；**运行路径**
    （:func:`app.processors.engine.run_rename`）用 ``False`` —— 库里早就存着的旧配置、
    或人工改过的 YAML 必须能照旧跑完，那些未知名字由运行期 warning + ``result.unknown_params``
    留痕（见 ``docs/meta/KNOWN_ISSUES.md`` KI-11）。
    """
    errors: list[dict[str, str]] = []

    def err(field_name: str, message: str) -> None:
        errors.append({"field": field_name, "message": message})

    if not isinstance(data, dict):
        return [{"field": "", "message": "spec 必须是一个对象（YAML 映射）。"}]

    known = {
        "schema_version", "id", "name", "description", "kind", "enabled", "builtin",
        "traverse", "extensions", "naming", "episode_patterns",
        "episode_fallback", "selection", "log_file", "log_format",
    }
    for key in data:
        if key not in known and key != "reference_script":  # 兼容旧配置：该键容忍但忽略
            err(str(key), f"未知字段：{key}")

    version = data.get("schema_version")
    if version is None:
        err("schema_version", "缺少 schema_version，必须是 2。")
    elif int(version) != SCHEMA_VERSION if isinstance(version, (int, str)) and str(version).isdigit() else True:
        err("schema_version", f"schema_version 必须是 {SCHEMA_VERSION}，当前是 {version!r}。")

    raw_id = data.get("id")
    if raw_id is None or raw_id == "":
        if require_id:
            err("id", "缺少 id：必须匹配 ^[a-z][a-z0-9_-]{1,31}$。")
    elif not isinstance(raw_id, str) or not ID_PATTERN.match(raw_id):
        err("id", f"id 不合法：{raw_id!r}，必须匹配 ^[a-z][a-z0-9_-]{{1,31}}$（小写字母开头）。")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        err("name", "缺少 name：必须是非空字符串（用于界面展示）。")
    if "description" in data and not isinstance(data.get("description"), str):
        err("description", "description 必须是字符串。")

    kind = data.get("kind")
    if kind not in PROCESSOR_KINDS:
        err("kind", f"kind 必须是 {' 或 '.join(PROCESSOR_KINDS)}，当前是 {kind!r}。")

    for key in ("enabled", "builtin"):
        if key in data and not isinstance(data[key], bool):
            err(key, f"{key} 必须是 true/false。")

    # ── traverse ────────────────────────────────────────────────────────
    traverse = data.get("traverse")
    if not isinstance(traverse, dict):
        err("traverse", "缺少 traverse：必须是一个对象。")
    else:
        depth = traverse.get("title_depth", 1)
        if depth != 1:
            err("traverse.title_depth", "当前只支持 1（标题文件夹必须是媒体库根下的直接子目录）。")
        scope = traverse.get("file_scope")
        if scope not in FILE_SCOPES:
            err("traverse.file_scope", f"file_scope 必须是 {' 或 '.join(FILE_SCOPES)}，当前是 {scope!r}。")
        _check_dir_rules(traverse.get("accept_dirs"), "traverse.accept_dirs", err, with_group=True)
        _check_dir_rules(traverse.get("reject_dirs"), "traverse.reject_dirs", err, with_group=False)
        if "skip_season_zero" in traverse and not isinstance(traverse["skip_season_zero"], bool):
            err("traverse.skip_season_zero", "skip_season_zero 必须是 true/false。")
        if scope == "title" and traverse.get("accept_dirs"):
            err("traverse.accept_dirs", "file_scope=title 时文件直接取自标题文件夹，accept_dirs 无意义，请留空。")

    # ── extensions ──────────────────────────────────────────────────────
    extensions = data.get("extensions")
    if not isinstance(extensions, list) or not extensions:
        err("extensions", "extensions 必须是非空数组，例如 [.mkv, .mp4, .ass]。")
    else:
        for index, item in enumerate(extensions):
            if not isinstance(item, str) or not item.startswith(".") or len(item) < 2:
                err(f"extensions[{index}]", f"后缀必须以点开头，当前是 {item!r}。")

    # ── naming ──────────────────────────────────────────────────────────
    naming = data.get("naming")
    if not isinstance(naming, dict):
        err("naming", "缺少 naming：必须是一个对象（expressions + segments）。")
    else:
        expressions = naming.get("expressions")
        if expressions is None:
            err("naming.expressions", "缺少 naming.expressions（可以为空对象 {}）。")
        elif not isinstance(expressions, dict):
            err("naming.expressions", "naming.expressions 必须是对象（名字 -> 表达式）。")
        else:
            for key, value in expressions.items():
                if not isinstance(key, str) or not _IDENTIFIER.match(key):
                    err(f"naming.expressions.{key}", f"表达式名不合法：{key!r}（必须是合法标识符）。")
                if not isinstance(value, str):
                    err(f"naming.expressions.{key}", "表达式必须是字符串。")
                else:
                    try:
                        compile(value, "<expressions.%s>" % key, "eval")
                    except SyntaxError as exc:
                        err(f"naming.expressions.{key}", f"表达式无法编译：{exc.msg}。")

        segments = naming.get("segments")
        known_names = known_context_names(expressions if isinstance(expressions, dict) else None)
        if not isinstance(segments, list) or not segments:
            err("naming.segments", "naming.segments 必须是非空数组（决定文件名的组装顺序）。")
        else:
            for index, segment in enumerate(segments):
                prefix = f"naming.segments[{index}]"
                if not isinstance(segment, dict):
                    err(prefix, "段必须是对象，例如 {param: title} 或 {literal: ' S'}。")
                    continue
                for key in segment:
                    if key not in _SEGMENT_KEYS:
                        err(f"{prefix}.{key}", f"段只允许 param / literal / pad，出现未知键 {key!r}。")
                has_param = "param" in segment
                has_literal = "literal" in segment
                if has_param == has_literal:
                    err(prefix, "段必须且只能有 param 或 literal 之一。")
                if has_param and not isinstance(segment.get("param"), str):
                    err(f"{prefix}.param", "param 必须是字符串。")
                elif has_param and strict_names:
                    # 名字必须存在于这份清单里：运行期查不到就会静默少一段。
                    # 这里的清单 = 变量表 + 引擎内部键 + naming.expressions 里定义的名字。
                    name = segment.get("param") or ""
                    if not name:
                        err(f"{prefix}.param", "param 不能是空字符串（这个段会什么都不输出）。")
                    elif name not in known_names:
                        err(
                            f"{prefix}.param",
                            f"段里用了未知变量 {{{name}}}。可用变量：{'、'.join(known_names)}"
                            "；自定义名字要先在 naming.expressions 里定义。",
                        )
                if has_literal and not isinstance(segment.get("literal"), str):
                    err(f"{prefix}.literal", "literal 必须是字符串。")
                pad = segment.get("pad")
                if pad is not None and (not isinstance(pad, int) or isinstance(pad, bool) or pad < 1):
                    err(f"{prefix}.pad", "pad 必须是 >=1 的整数。")

    # ── episode_patterns ────────────────────────────────────────────────
    patterns = data.get("episode_patterns")
    if patterns is None:
        err("episode_patterns", "缺少 episode_patterns（命名里用到 episode 时必需；可以留空数组）。")
    elif not isinstance(patterns, list):
        err("episode_patterns", "episode_patterns 必须是数组。")
    else:
        for index, item in enumerate(patterns):
            prefix = f"episode_patterns[{index}]"
            if not isinstance(item, dict):
                err(prefix, "每项必须是对象：{name, pattern}。")
                continue
            for key in item:
                if key not in ("name", "pattern"):
                    err(f"{prefix}.{key}", f"只允许 name / pattern，出现未知键 {key!r}。")
            if not isinstance(item.get("name"), str) or not item.get("name"):
                err(f"{prefix}.name", "缺少 name（用于错误定位）。")
            pattern = item.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                err(f"{prefix}.pattern", "缺少 pattern 正则。")
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    err(f"{prefix}.pattern", f"正则无法编译：{exc}。")

    fallback = data.get("episode_fallback")
    if fallback is not None and not isinstance(fallback, str):
        err("episode_fallback", 'episode_fallback 必须是字符串或 null（写字符串 "None" 时按字面量处理）。')

    # ── selection ───────────────────────────────────────────────────────
    selection = data.get("selection")
    if not isinstance(selection, dict):
        err("selection", "缺少 selection：必须是一个对象。")
    else:
        per = selection.get("per")
        if per not in SELECTION_MODES:
            err("selection.per", f"per 必须是 {' 或 '.join(SELECTION_MODES)}，当前是 {per!r}。")

    # ── 日志 ────────────────────────────────────────────────────────────
    log_file = data.get("log_file")
    if log_file is None:
        # 允许省略：新建处理器/校验草稿时由服务端按"库名 slug + cache.log"生成
        # （``processors.registry.next_log_file``）。空串同样视为"请服务端生成"。
        log_file = ""
    if not isinstance(log_file, str):
        err("log_file", "log_file 必须是字符串（文件名，例如 bangumicache.log）。")
    elif "/" in log_file or "\\" in log_file or log_file in (".", "..") or ".." in log_file:
        err("log_file", "log_file 只允许文件名，不得包含路径分隔符或 ..。")
    log_format = data.get("log_format")
    if log_format not in LOG_FORMATS:
        err("log_format", f"log_format 只能是 {' 或 '.join(LOG_FORMATS)}。")

    return errors


def _check_dir_rules(value: Any, prefix: str, err, *, with_group: bool) -> None:
    if value is None:
        # 省略（或 YAML 里写了空键）= 没有规则；``normalize_spec`` 会补成 []。
        return
    if not isinstance(value, list):
        err(prefix, "必须是数组。")
        return
    for index, item in enumerate(value):
        item_prefix = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            err(item_prefix, "每项必须是对象：{pattern: '正则'}。")
            continue
        allowed = ("pattern", "season_group") if with_group else ("pattern",)
        for key in item:
            if key not in allowed:
                err(f"{item_prefix}.{key}", f"只允许 {' / '.join(allowed)}，出现未知键 {key!r}。")
        pattern = item.get("pattern")
        compiled = None
        if not isinstance(pattern, str) or not pattern:
            err(f"{item_prefix}.pattern", "缺少 pattern 正则。")
        else:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                err(f"{item_prefix}.pattern", f"正则无法编译：{exc}。")
        if with_group:
            group = item.get("season_group", 1)
            if not isinstance(group, int) or isinstance(group, bool) or group < 0:
                err(
                    f"{item_prefix}.season_group",
                    "season_group 必须是 0 或 >=1 的整数（0 表示只判断接受、不取季号）。",
                )
            elif group and compiled is not None and group > compiled.groups:
                # group == 0 是合法值（不取季号），无条件跳过越界比较；
                # 与 accept_dirs 为空时的季号语义逐字一致（engine.py:503 `if matched.group:`）。
                err(
                    f"{item_prefix}.season_group",
                    f"season_group={group} 超出正则的捕获组数量（该正则只有 {compiled.groups} 个组）；"
                    "若不打算从这个正则取季号，请显式写 \"season_group\": 0，或给正则加一个捕获组。",
                )


def validate_spec(
    data: dict[str, Any] | None, *, require_id: bool = False, strict_names: bool = True
) -> list[dict[str, str]]:
    """校验一份 spec，返回**字段级错误列表**（空列表 = 合法）。别名 ``iter_errors``。

    ``strict_names=False`` 供**运行路径**使用（见 :func:`iter_errors` 的说明）。
    """
    return iter_errors(data, require_id=require_id, strict_names=strict_names)
