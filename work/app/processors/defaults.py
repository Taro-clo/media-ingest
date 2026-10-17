"""三个内置处理器的默认规格 + 预设（``default`` / ``legacy``）。

契约真源：``docs/contracts/PROCESSORS.md`` §3.2。``legacy`` 预设（清空 ``traverse.reject_dirs`` + 把
``episode_fallback`` 写回字符串 ``"None"`` + 把 ``episode_patterns`` 换成两条旧式正则）
= **最朴素的旧口径**。

四处**有意的行为增量**（相对旧口径，必须在 CHANGELOG 与 docs/contracts/COMPATIBILITY.md 显式标注）：

1. ``bangumi`` 的 ``reject_dirs``：一份拒绝目录名单
   （SP/CM/OP/ED/Scan/Scans/CD/CDs/映像特典/特典CD/Preview/Previews/Vol.x/EXTRA/Bonus/Menus/
   Music/Subtitle/ScreenShot/NCOP/NCED…）——旧口径没有这项排除；
2. ``movie`` 的 ``reject_dirs``：用户指定（"电影拒绝 特典/CD/扫图/scans 等文件夹"）+
   同名的常见特典目录，见 :data:`MOVIE_REJECT_PATTERNS` 的逐条来源注释；
3. ``bangumi`` 的 ``episode_fallback``：**推荐默认是 ``None``**（旧口径
   用的是**字符串** ``"None"``）——即"命名引用了集数、但 ``episode_patterns`` 一个都没命中"时
   **保留原文件名、不改名**；字符串 ``"None"`` 由 ``legacy`` 预设复刻（见
   :data:`LEGACY_OVERRIDES`）。旧口径会把同一作品的不同压制版本全部渲染成 ``… S01ENone.mkv``
   互相覆盖（真实事故）；
4. ``bangumi`` 的 ``episode_patterns``：**推荐默认是 9 条"有序、序号小者优先"的规则**
   （见 :data:`BANGUMI_EPISODE_PATTERNS`）——旧口径把 8 个分支塞进
   同一个 ``(?:…)`` 且没有任何"这里不可能有集号"的边界约束，于是 ``Show - 10 bit.mkv`` 会被
   ``-\\s*(?P<g8>\\d{1,2})`` 捞成第 10 集、文件名末尾的 CRC/校验方括号 ``[1AE35F3A]`` 里的 ``E35``
   会被 ``E(?P<g6>\\d{1,2})`` 当成第 35 集。那两条正则只由 ``legacy`` 预设使用
   （见 :data:`BANGUMI_LEGACY_EPISODE_PATTERNS`）。

除此之外，``legacy`` 沿用旧口径的字段值（等价性由本项目自己的回归测试守卫）；
``default`` 与它只差上面四条**有意的**增量。
"""

from __future__ import annotations

import copy
from typing import Any

__all__ = [
    "BUILTIN_IDS",
    "PRESETS",
    "LEGACY_OVERRIDES",
    "BANGUMI_EPISODE_PATTERNS",
    "BANGUMI_LEGACY_EPISODE_PATTERNS",
    "BANGUMI_REJECT_PATTERNS",
    "MOVIE_REJECT_PATTERNS",
    "builtin_ids",
    "builtin_spec",
    "builtin_specs",
    "preset_spec",
    "template_spec",
]

#: 内置处理器 id（顺序 = 界面展示顺序）
BUILTIN_IDS: tuple[str, ...] = ("hardlink_only", "bangumi", "movie")

#: 预设名
PRESETS: tuple[str, ...] = ("default", "legacy")

# ── 集数正则（default）────────────────────────────────────────────────────
# 9 条**有序**规则，序号即优先级（序号小者优先）。引擎对每条规则只取"首个命中的首个非空
# **命名组**"（`engine._episode_raw`：按组序号取第一个非空组），所以每条规则都必须把自己的
# 集号数字捕获成命名组 `ep`。
# 统一语义：全部 `re.IGNORECASE`；`\s` 一律可选；数字只允许 1~2 位（`(?!\d)` 排除 3 位以上）。
# 与 ``legacy`` 预设的那两条集数正则**故意不同**：
# 旧规则把 8 个分支塞进同一个 `(?:…)`、且没有任何"这里不可能有集号"的边界约束，实测在真实
# 媒体库里误判（`Show - 1920x1080.mkv` 被 `-\s*(?P<g8>\d{1,2})` 捞成第 19 集、末尾校验方括号
# `[1AE35F3A]` 里的 `E35` 被 `E(?P<g6>\d{1,2})` 当成第 35 集）。量化：真实源 8534 个文件名里，
# 新规则命中且数字落在 6 位以上十六进制串内部 = **0**（旧规则 21 个）。
#
# 逐条（`→` 是举例）：
#   1 chapter        第 12 話 / 第12集                  → 12
#   2 episode_word   Episode 3（前面不能紧贴英文字母）    → 3
#   3 season_episode S02E07 —— 捕获的是 **E 后**的数字    → 7（不是 2）
#   4 bracketed      [05]（整对方括号里只有 1~2 位数字）  → 5；[1920x1080]/[1AE35F3A] 不命中
#   5 ep_marker      EP09（前面不能紧贴英文字母）         → 9
#   6 hash_marker    #04                                → 4
#   7 dash           - 11；`- 10 bit` 被 (?!) 排除       → 11
#   8 bare_e         `… E12 …` / `… E12.`（**E 前必须是空白**）→ 12；`.E07` 这种紧贴点的**不命中**
#   9 bare_number    `Show 12.mkv` / `Show 12 1080p.mkv`（数字前空白、后空白或点）→ 12
BANGUMI_EPISODE_PATTERNS: list[dict[str, str]] = [
    {
        "name": "chapter",  # 1
        "pattern": r"(?i)第\s*(?P<ep>\d{1,2})(?!\d)\s*[集话話回]",
    },
    {
        "name": "episode_word",  # 2
        "pattern": r"(?i)(?<![A-Za-z])Episode\s*(?P<ep>\d{1,2})(?!\d)",
    },
    {
        "name": "season_episode",  # 3
        "pattern": r"(?i)(?<![A-Za-z])S\s*\d{1,2}(?!\d)\s*E\s*(?P<ep>\d{1,2})(?!\d)",
    },
    {
        "name": "bracketed",  # 4
        "pattern": r"(?i)\[\s*(?P<ep>\d{1,2})\s*\]",
    },
    {
        "name": "ep_marker",  # 5
        "pattern": r"(?i)(?<![A-Za-z])EP\s*(?P<ep>\d{1,2})(?!\d)",
    },
    {
        "name": "hash_marker",  # 6
        "pattern": r"(?i)#\s*(?P<ep>\d{1,2})(?!\d)",
    },
    {
        "name": "dash",  # 7
        "pattern": r"(?i)-\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)",
    },
    {
        "name": "bare_e",  # 8
        "pattern": r"(?i)(?<=\s)[Ee]\s*(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s|\.)",
    },
    {
        "name": "bare_number",  # 9
        "pattern": r"(?i)(?<=\s)(?P<ep>\d{1,2})(?!\d)(?!\s*[bB]it\b)(?=\s|\.)",
    },
]

#: **最朴素的旧口径**的两条集数规则（8 个候选组 + 尾部数字）—— `legacy` 预设专用，
#: **内容冻结**，不许改名/加注释/调顺序：
#: 项目自己的 legacy 行为回归测试全靠它。
#: 那两条用 `re.VERBOSE` 书写，这里展开成单行（语义完全一致）；捕获组按原顺序命名 `g1..g8`，
#: 引擎按**组序号**取首个非空组，所以名字不重要、**顺序**才重要。
BANGUMI_LEGACY_EPISODE_PATTERNS: list[dict[str, str]] = [
    {
        "name": "ep",
        "pattern": (
            r"(?i)(?:第\s*(?P<g1>\d{1,2})\s*[集话話回]"
            r"|\[\s*(?P<g2>\d{1,2})\s*\]"
            r"|Episode\s*(?P<g3>\d{1,2})"
            r"|EP\s*(?P<g4>\d{1,2})"
            r"|e(?P<g5>\d{1,2})"
            r"|E(?P<g6>\d{1,2})"
            r"|\#\s*(?P<g7>\d{1,2})"
            r"|-\s*(?P<g8>\d{1,2}))"
        ),
    },
    {
        "name": "trailing",
        "pattern": r"(?:\s+(?P<g1>\d{1,2}))",
    },
]

#: ``legacy`` 相对 ``default`` 的**三处**差异：清空拒绝目录规则 + 恢复字符串 ``"None"``
#: + 换回那两条集数正则（前者 ⇒ 最朴素的旧口径；后两者分别是旧口径的兜底取值
#: 与两条正则，见模块 docstring 增量 ③/④）。
#: 键是**点号路径**（``preset_spec`` 逐项 apply，值一律深拷贝 ⇒ **list 值同样适用**）；
#: ``episode_fallback`` / ``episode_patterns`` 对 ``movie`` / ``hardlink_only`` 没有实际影响
#: （它们的 ``naming`` 不引用集数），写回只是让"legacy = 旧口径那套字段值"这条不变量在三处
#: 差异上都成立。
LEGACY_OVERRIDES: dict[str, Any] = {
    "traverse.reject_dirs": [],
    "episode_fallback": "None",
    "episode_patterns": BANGUMI_LEGACY_EPISODE_PATTERNS,
}

#: movie 默认拒绝目录（整棵子树跳过）。**每条都在注释里写明来源**；覆盖 特典/CD/扫图/scans 这类常见的
#: 特典 / 附加文件夹。全部**大小写不敏感 + 整名锚定**（``^…$``），
#: 避免误伤正常片名（例如 "Scans of Summer" 不会命中）。
#: YAML 的 safe_dump 不保留注释，所以这段说明同时写进 movie 规格的 description 字段。
MOVIE_REJECT_PATTERNS: list[str] = [
    r"(?i)^(特典|特典cd)$",      # 按需要补入
    r"(?i)^(cd|cds)$",           # 按需要补入
    r"(?i)^扫图$",                # 按需要补入
    r"(?i)^scans?$",             # 按需要补入（scans / scan）
    r"(?i)^(sp|sps)$",           # 常见特典目录
    r"(?i)^(menu|menus)$",       # 常见菜单目录
    r"(?i)^(previews?)$",        # 常见预告目录
    r"(?i)^(extra|bonus)$",      # 常见花絮目录
    r"(?i)^(ncop|nced)$",        # 无字幕 OP/ED
    r"(?i)^samples?$",           # 样片目录
]

#: bangumi 默认拒绝目录：**逐条列出**（``legacy`` 预设会把整份名单清空）。
#: 同样全部大小写不敏感 + 整名锚定。``legacy`` 预设会把整份名单清空。
BANGUMI_REJECT_PATTERNS: list[str] = [
    r"(?i)^(sp|cm|op|ed)s?$",           # SP CM OP ED SPs CMs OPs EDs
    r"(?i)^scans?$",                    # Scan Scans
    r"(?i)^cds?$",                      # CD CDs
    r"(?i)^(映像特典|特典cd)$",          # 映像特典 特典CD
    r"(?i)^previews?$",                 # Preview Previews
    r"(?i)^scan \(vol\.\d+\)$",         # Scan (Vol.任意数字)
    r"(?i)^vol\.\d+$",                  # Vol.任意数字
    r"(?i)^(extra|bonus|menus|music)$",  # EXTRA Bonus Menus Music
    r"(?i)^subtitles?$",                # Subtitle Subtitles
    r"(?i)^screenshots?$",              # ScreenShot ScreenShots
    r"(?i)^(ncop|nced)$",               # NCOP NCED ncop nced
]

_MOVIE_DESCRIPTION = (
    "按「标题文件夹/文件」结构重命名：同后缀只取体积最大的文件，重命名为「标题.后缀」。"
    "默认拒绝目录（命中即整棵子树跳过，大小写不敏感、整名匹配）："
    + "、".join(MOVIE_REJECT_PATTERNS)
    + "。其中 特典/CD/扫图/scans 是按需要补进来的，其余为同名的常见特典目录。"
)


def _hardlink_only_spec() -> dict[str, Any]:
    """``hardlink_only``：只建硬链接、绝不重命名（PROCESSORS §3 表）。"""
    return {
        "schema_version": 2,
        "id": "hardlink_only",
        "name": "只建硬链接",
        "description": (
            "只把下载完成的文件硬链接到媒体库，不做任何重命名；"
            "硬链接由应用自身的原生实现执行，不依赖任何外部程序。"
        ),
        "kind": "hardlink_only",
        "enabled": True,
        "builtin": True,
        "traverse": {
            "title_depth": 1,
            "file_scope": "subdir",
            "accept_dirs": [],
            "reject_dirs": [],
            "skip_season_zero": False,
        },
        "extensions": [".mkv", ".mp4", ".ass"],
        "naming": {
            "expressions": {},
            "segments": [{"param": "title"}],
        },
        "episode_patterns": [],
        "episode_fallback": None,
        "selection": {"per": "file"},
        "log_file": "hardlink_only.log",
        "log_format": "json_cache",
    }


def _bangumi_spec() -> dict[str, Any]:
    """``bangumi``：番剧语义（逐条字段说明见 §3.2）。"""
    return {
        "schema_version": 2,
        "id": "bangumi",
        "name": "番剧",
        "description": (
            "按「作品/Season N/文件」结构重命名为「作品 SxxExx 后缀」；"
            "接受目录正则按整名匹配（fullmatch）语义判定，集数按 9 条**有序**规则识别"
            "（第N集 / Episode / SxxExx / [NN] / EP / # / - N / E N / 裸数字，序号小者优先，"
            "均忽略大小写），"
            "ass 多段后缀按最后一段处理。默认拒绝目录覆盖常见的特典目录"
            "（SP/CM/OP/ED/Scan/CD/映像特典/Preview/Vol.x/EXTRA…）。"
            "未命中集数时保留原文件名（不写字符串 None，避免同作品不同压制版本互相覆盖）；"
            "如需旧口径，请把「集数兜底」episode_fallback 显式设为字符串 \"None\""
            "并在「集数正则」里换成对应的两条规则。"
        ),
        "kind": "rename",
        "enabled": True,
        "builtin": True,
        "traverse": {
            "title_depth": 1,
            # 旧口径番剧语义 —— 只取季文件夹**直接**包含的文件（不递归）
            "file_scope": "subdir",
            # 旧口径番剧语义 —— fullmatch('Season (\\d+)')
            "accept_dirs": [{"pattern": r"^Season (\d+)$", "season_group": 1}],
            # 拒绝目录名单（``legacy`` 预设没有这项排除）
            "reject_dirs": [{"pattern": p} for p in BANGUMI_REJECT_PATTERNS],
            # 旧口径番剧语义 —— season == "00" 时跳过
            "skip_season_zero": True,
        },
        # 旧口径番剧语义（只取直接子项）
        "extensions": [".mkv", ".mp4", ".ass"],
        "naming": {
            "expressions": {},
            # 旧口径番剧语义 —— f"{title} S{season}E{episode}{suffix|ext}"
            "segments": [
                {"param": "title"},
                {"literal": " S"},
                {"param": "season", "pad": 2},
                {"literal": "E"},
                {"param": "episode", "pad": 2},
                {"param": "suffix"},
            ],
        },
        # 番剧语义 —— **推荐默认不再逐字沿用旧口径**：改用 9 条有序规则
        # （见 :data:`BANGUMI_EPISODE_PATTERNS` 的逐条说明）；那两条由 ``legacy``
        # 预设沿用（见 :data:`BANGUMI_LEGACY_EPISODE_PATTERNS` / :data:`LEGACY_OVERRIDES`）。
        "episode_patterns": copy.deepcopy(BANGUMI_EPISODE_PATTERNS),
        # 推荐默认：未命中集数 ⇒ 保留原文件名；字符串 ``"None"``
        # 由 ``legacy`` 预设复刻（见 :data:`LEGACY_OVERRIDES`）。
        "episode_fallback": None,
        # 旧口径番剧语义 —— 每个匹配文件都处理
        "selection": {"per": "file"},
        # 旧口径番剧语义 —— 原名，延续历史记录（PROCESSORS §5.2）
        "log_file": "bangumicache.log",
        "log_format": "json_cache",
    }


def _movie_spec() -> dict[str, Any]:
    """``movie``：电影语义（逐条字段说明见 §3.2）。"""
    return {
        "schema_version": 2,
        "id": "movie",
        "name": "电影",
        "description": _MOVIE_DESCRIPTION,
        "kind": "rename",
        "enabled": True,
        "builtin": True,
        "traverse": {
            "title_depth": 1,
            # 旧口径电影语义 —— 只看标题文件夹的直接子项，不看子目录
            "file_scope": "title",
            # file_scope=title 时 accept_dirs 无意义（校验会拒绝非空值）
            "accept_dirs": [],
            "reject_dirs": [{"pattern": pattern} for pattern in MOVIE_REJECT_PATTERNS],
            # 电影没有季概念；保持 true 也不影响（file_scope=title 不做季判断）
            "skip_season_zero": True,
        },
        # 旧口径电影语义
        "extensions": [".mkv", ".mp4", ".ass"],
        "naming": {
            "expressions": {},
            # 旧口径电影语义 —— f"{title}.{extension}"，extension = filename.split('.')[-1]
            "segments": [
                {"param": "title"},
                {"literal": "."},
                {"param": "extname"},
            ],
        },
        # 电影不用剧集号
        "episode_patterns": [],
        "episode_fallback": None,
        # 旧口径电影语义 —— 同后缀只取体积最大者
        "selection": {"per": "extension"},
        # 旧口径电影语义 —— 原名，延续历史记录（PROCESSORS §5.2）
        "log_file": "moviecache.log",
        "log_format": "json_cache",
    }


_BUILDERS = {
    "hardlink_only": _hardlink_only_spec,
    "bangumi": _bangumi_spec,
    "movie": _movie_spec,
}


def builtin_ids() -> tuple[str, ...]:
    """内置处理器 id（顺序固定）。"""
    return BUILTIN_IDS


def builtin_spec(processor_id: str) -> dict[str, Any]:
    """内置处理器的**推荐默认**规格（深拷贝，调用方可随意改）。"""
    builder = _BUILDERS.get(processor_id)
    if builder is None:
        raise KeyError(processor_id)
    return builder()


def builtin_specs() -> dict[str, dict[str, Any]]:
    """三个内置规格（id -> spec 深拷贝）。"""
    return {processor_id: builtin_spec(processor_id) for processor_id in BUILTIN_IDS}


def _apply_dot_path(spec: dict[str, Any], path: str, value: Any) -> None:
    """把 ``LEGACY_OVERRIDES`` 的**点号路径**写进规格（``value`` 深拷贝，不动调用方）。

    ``value`` 可以是标量、list 或 dict —— 这里是"深拷贝后整体赋值"，所以
    ``episode_patterns`` 这类 list 值同样适用（不会把调用方那份改坏）。
    """
    keys = path.split(".")
    node: dict[str, Any] = spec
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = copy.deepcopy(value)


def preset_spec(processor_id: str, preset: str = "default") -> dict[str, Any]:
    """取某个内置处理器在某个预设下的规格。

    ``default`` = 推荐默认（含四处有意的行为增量；bangumi 的 ``episode_fallback`` 是 ``None``，
    即未命中集数时**保留原文件名**，集数用 9 条有序新规则识别）；
    ``legacy``  = **最朴素的旧口径**（逐项应用 :data:`LEGACY_OVERRIDES`：``traverse.reject_dirs = []``
    + ``episode_fallback = "None"`` + ``episode_patterns =`` 那两条旧正则，其余与 default 完全相同）。
    """
    if preset not in PRESETS:
        raise ValueError(f"未知预设：{preset}（可选：{'、'.join(PRESETS)}）")
    spec = builtin_spec(processor_id)
    if preset == "legacy":
        # 三处差异：清空拒绝目录规则 + 恢复字符串 "None" + 换回那两条集数正则。
        # 逐项遍历那张表 —— 以后再加差异只改 LEGACY_OVERRIDES，不必改这里。
        for path, value in LEGACY_OVERRIDES.items():
            _apply_dot_path(spec, path, value)
        if processor_id == "movie":
            spec["description"] = spec["description"].replace(
                "默认拒绝目录（命中即整棵子树跳过）：", "legacy 预设：不拒绝任何目录（最朴素的旧口径）。原默认会是："
            )
    return spec


def template_spec(template: str) -> dict[str, Any]:
    """``POST /api/processors`` 的模板（``movie``/``bangumi``/``hardlink_only``/``blank``）。

    返回的 spec 里 ``builtin`` 已置 False、``id``/``name`` 留空（由路由用请求体的 id/name 覆盖）。
    """
    if template not in ("movie", "bangumi", "hardlink_only", "blank"):
        raise ValueError(f"未知模板：{template}")
    if template == "blank":
        spec = _movie_spec()
        spec["id"] = ""
        spec["name"] = ""
        spec["description"] = "自定义处理器（空白模板）。"
        spec["traverse"]["reject_dirs"] = []
        spec["naming"]["segments"] = [{"param": "title"}]
        spec["log_file"] = ""
    else:
        spec = builtin_spec(template)
        spec["id"] = ""
        spec["name"] = ""
    spec["builtin"] = False
    return spec
