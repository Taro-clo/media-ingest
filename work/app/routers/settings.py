"""``/api/settings/*``（ARCHITECTURE §5.3 与 §5.0 第 4 条）。

设置表是**纯文本 key-value**，但 HTTP 契约要求**真类型**（``bool`` / ``int`` / ``array``）。
两者之间只有这一层做转换，所以这里集中定义唯一一份「键 -> 类型」表：
写入时按表校验并序列化，读取时按表反序列化。表外的一律拒收，
避免前端写进来一个 `scan_enabeld` 这种拼错的键、然后永远读不出效果。

`hook_token` 与 `qbittorrent_password` 是两个特殊键：
- `hook_token` **返回明文**（设置页要一键复制回调命令，不给明文这个功能就没法用）；
- `qbittorrent_password` 已设置时返回 `"***"`，`PUT` 收到 `"***"` 表示「保持原值」，
  这样前端把整个对象原样回传也不会把密码冲掉。
"""

from __future__ import annotations

import json
import logging
import re
import secrets

from fastapi import APIRouter, Depends

from .. import security
from ..api_models import QbTestRequest, SettingsPatch
from ..config import settings
from ..db import all_settings, get_setting, set_setting
from ..qbittorrent import latency_of
from ..processors import engine, overrides
from .common import bad_request

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: 占位符：PUT 时表示「保持库里原值」。
KEEP_PLACEHOLDER = "***"

#: 布尔型设置键。
BOOL_KEYS = frozenset({"scan_enabled", "orphan_cleanup_enabled"})
#: 整数型设置键。
INT_KEYS = frozenset(
    {"scan_interval_seconds", "max_history", "max_task_log_lines", "config_sync_interval_seconds"}
)
#: 数组型设置键。
LIST_KEYS = frozenset({"allowed_source_roots", "allowed_target_roots"})
#: 后缀白名单型设置键：字符串数组，每项必须以 ``.`` 开头，**允许为空数组**。
#: 空 = 未设置 ⇒ 各库沿用"库覆盖 → 处理器默认"（见 ``app/processors/overrides.py``）。
SUFFIX_KEYS = frozenset({"rename_extensions"})
#: L3 特典文件分类：**正则数组**型设置键 —— 字符串数组，每项是一条 Python 正则，
#: **允许为空数组**（= 关闭 L3）。与 SUFFIX_KEYS 的区别：这里**不拆分、不做形状白名单**，
#: 只要求逐条能 ``re.compile`` —— 正则里逗号、空格、点号都有意义，任何"聪明的拆分"都会毁掉它。
REGEX_LIST_KEYS = frozenset({"extras_patterns"})
#: 纯字符串设置键。
STR_KEYS = frozenset(
    {
        "log_level",
        "qbittorrent_url",
        "qbittorrent_username",
        "qbittorrent_password",
        "hook_token",
        # 待办看板：逗号分隔的待办种类开关（例如 ``"rename_suffix,link_failed"``）。
        # 空串 = 全部种类都记（``app/todos.py::KNOWN_KINDS``）；未知名字会被 ``record_task_events``
        # 忽略，所以这里不需要枚举校验 —— 但**必须**登记，否则 ``PUT /api/settings`` 会把
        # 前端回传的它当成"未知的设置项"直接 400。
        "todo_kinds",
    }
)
KNOWN_KEYS = BOOL_KEYS | INT_KEYS | LIST_KEYS | SUFFIX_KEYS | REGEX_LIST_KEYS | STR_KEYS

#: 服务端自己记账用的键：**不出现在** ``GET /api/settings`` 里（它们不是用户设置，
#: 前端如果把它们渲染成表单字段，用户一保存就会被"未知的设置项"挡回来）。
INTERNAL_KEYS = frozenset(
    {
        "config_last_import_at",
        "config_last_export_at",
        "qbittorrent_last_status",
        "qbittorrent_version",
        "scan_last_run_at",
        # 遗留键：不再使用，但老数据卷里可能还在（读取不报错，只是不展示）。
        "seed_logs_on_first_run",
        "log_seeded_at",
    }
)

#: 整数型设置的合法区间（消息里要说清为什么，否则用户只会觉得"填了没反应"）。
INT_RANGES: dict[str, tuple[int, int]] = {
    # 小于 30 秒的扫描间隔没有意义：一次全量扫描本身就可能耗时更久。
    "scan_interval_seconds": (30, 86400),
    # 至少留 10 条历史，否则任务页会空得像坏了。
    "max_history": (10, 100000),
    # 单任务日志上限：太小会把正常的逐文件日志截掉。
    "max_task_log_lines": (100, 1000000),
    # 配置同步间隔：0 = 关闭自动导入；上限一天，避免"设成一周才发现改了文件没反应"。
    "config_sync_interval_seconds": (0, 86400),
}

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

#: 返回给前端的键顺序（固定顺序 = 设置页表单顺序，前端不用自己排）。
OUTPUT_ORDER = (
    "allowed_source_roots",
    "allowed_target_roots",
    "scan_enabled",
    "scan_interval_seconds",
    "log_level",
    "max_history",
    "max_task_log_lines",
    "orphan_cleanup_enabled",
    "rename_extensions",
    # L3 特典文件分类：紧挨着 rename_extensions（两者是同一类"文件级判据"设置）。
    "extras_patterns",
    "config_sync_interval_seconds",
    "qbittorrent_url",
    "qbittorrent_username",
    "qbittorrent_password",
    "hook_token",
)


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _parse_json_list(raw: str | None, default: list) -> list:
    if raw is None:
        return list(default)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return list(default)
    return value if isinstance(value, list) else list(default)


def _parse_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _mask_password(raw: str | None) -> str:
    """已设置 -> ``"***"``，未设置 -> ``""``（两者语义不同，别合并）。"""
    return KEEP_PLACEHOLDER if (raw or "") else ""


def settings_view() -> dict:
    """按契约形状读全量设置（值已按类型解析，§5.0 第 4 条）。"""
    stored = all_settings()
    view: dict = {
        "allowed_source_roots": _parse_json_list(
            stored.get("allowed_source_roots"), [settings.default_source_root]
        ),
        "allowed_target_roots": _parse_json_list(
            stored.get("allowed_target_roots"), [settings.default_target_root]
        ),
        "scan_enabled": _parse_bool(stored.get("scan_enabled"), False),
        "scan_interval_seconds": _parse_int(stored.get("scan_interval_seconds"), 900),
        "log_level": (stored.get("log_level") or "INFO").strip().upper() or "INFO",
        "max_history": _parse_int(stored.get("max_history"), 2000),
        "max_task_log_lines": _parse_int(stored.get("max_task_log_lines"), 5000),
        "orphan_cleanup_enabled": _parse_bool(stored.get("orphan_cleanup_enabled"), True),
        # 全局后缀白名单。空数组 = 未设置（各库沿用库覆盖/处理器默认）。
        "rename_extensions": overrides.read_global_extensions(stored.get("rename_extensions")),
        # L3 特典文件分类：全局特典判据（正则数组，一行一条）。空数组 = 关闭 L3；
        # 未设置/读不到 ⇒ 出厂默认 7 条（见 engine.read_global_extras_patterns）。
        "extras_patterns": engine.read_global_extras_patterns(stored.get("extras_patterns")),
        "config_sync_interval_seconds": _parse_int(stored.get("config_sync_interval_seconds"), 5),
        "qbittorrent_url": stored.get("qbittorrent_url") or "",
        "qbittorrent_username": stored.get("qbittorrent_username") or "",
        "qbittorrent_password": _mask_password(stored.get("qbittorrent_password")),
        # 契约要求返回明文：设置页要一键复制带 token 的 curl 回调命令。
        "hook_token": stored.get("hook_token") or "",
    }
    # 表外键（未来版本新增、或用户手工塞的）也带上，免得它们"消失"得莫名其妙；
    # 但服务端记账键（INTERNAL_KEYS）与遗留键排除在外。
    for key in sorted(set(stored) - KNOWN_KEYS - INTERNAL_KEYS):
        view[key] = stored[key]
    return view


@router.get("")
def get_settings(_: str = Depends(security.require_user)) -> dict:
    """全量设置（裸对象，不分页）。"""
    return settings_view()


def _coerce(key: str, value) -> str:
    """把前端传来的已解析类型转成存储字符串；类型不符抛 400（含中文说明）。"""
    if key in BOOL_KEYS:
        if not isinstance(value, bool):
            raise bad_request(f"设置项 {key} 需要布尔值（true/false），收到 {type(value).__name__}。")
        return "true" if value else "false"
    if key in INT_KEYS:
        # bool 是 int 的子类，必须显式排除，否则 true 会被当成 1 存进去。
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad_request(f"设置项 {key} 需要整数，收到 {type(value).__name__}。")
        low, high = INT_RANGES.get(key, (None, None))
        if low is not None and not (low <= value <= high):
            raise bad_request(f"设置项 {key} 必须在 {low} 到 {high} 之间，收到 {value}。")
        return str(value)
    if key in LIST_KEYS:
        if not isinstance(value, list):
            raise bad_request(f"设置项 {key} 需要字符串数组，收到 {type(value).__name__}。")
        items: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise bad_request(f"设置项 {key} 的每一项都必须是非空字符串。")
            items.append(item.strip().rstrip("/") or "/")
        if not items:
            raise bad_request(f"设置项 {key} 不能是空数组：至少需要一个允许的根路径。")
        return json.dumps(items, ensure_ascii=False)
    if key in SUFFIX_KEYS:
        # 后缀白名单。空数组合法（= 未设置，各库沿用库覆盖/处理器默认）；
        # 非空时每一项都必须是 ".xxx" 形状，否则用户会得到一个"看起来生效、其实一个文件
        # 都匹配不上"的白名单 —— 那比直接报错难查得多。
        # 网页 textarea 里写 ".mkv .mp4 .ass" 会被当成**一项**存进去，之后所有任务都
        # "改名 0 个"且毫无报错。因此这里按分隔符拆开（split_extension_items），
        # 并把"空白的 token"直接判非法，避免这种"静默失效"的输入。
        if not isinstance(value, list):
            raise bad_request(f"设置项 {key} 需要字符串数组，收到 {type(value).__name__}。")
        suffixes: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise bad_request(f"设置项 {key} 的每一项都必须是非空字符串（如 \".mkv\"）。")
            for token in overrides.split_extension_items(item):
                if not overrides.is_valid_extension(token):
                    raise bad_request(
                        f"设置项 {key} 的后缀必须以点开头、且只含字母数字与 . _ + -"
                        f"（例如 \".mkv\"；多个后缀可以用空格或逗号分隔），收到 {token!r}。"
                    )
                text = token.lower()
                if text not in suffixes:
                    suffixes.append(text)
        return json.dumps(suffixes, ensure_ascii=False)
    if key in REGEX_LIST_KEYS:
        # L3 特典文件分类：特典判据（一条正则一项）。空数组合法（= 关闭 L3）；
        # 非空时每一条都必须能 ``re.compile`` —— 存一条坏正则进去，用户只会看到"这条判据
        # 永远不命中"，比 400 难查得多（和上面后缀白名单同一个理由）。
        # 这里**不做拆分**：正则里逗号、空格、点号都有意义（WebUI 是一行一条）。
        if not isinstance(value, list):
            raise bad_request(
                f"设置项 {key} 需要字符串数组（一条正则一项），收到 {type(value).__name__}。"
            )
        patterns: list[str] = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, str):
                raise bad_request(
                    f"设置项 {key} 的第 {index} 条必须是字符串（正则），收到 {type(item).__name__}。"
                )
            pattern = item.strip()
            if not pattern:
                raise bad_request(
                    f"设置项 {key} 的第 {index} 条是空行：请删掉这一行，"
                    "或用空数组 [] 关闭特典归类（L3）。"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise bad_request(
                    f"设置项 {key} 的第 {index} 条正则 {pattern!r} 无法编译：{exc}。"
                    "请检查括号、方括号与反斜杠转义是否配对（例如单独一个 \"(\" 就不是合法正则）。"
                ) from exc
            if pattern not in patterns:
                patterns.append(pattern)
        return json.dumps(patterns, ensure_ascii=False)
    # 字符串型
    if not isinstance(value, str):
        raise bad_request(f"设置项 {key} 需要字符串，收到 {type(value).__name__}。")
    return value


def _apply_side_effects(updates: dict) -> None:
    """把影响运行时的设置立刻生效（只处理日志级别；其余由各自模块每轮读取）。"""
    if "log_level" not in updates:
        return
    level = str(updates["log_level"]).strip().upper()
    numeric = getattr(logging, level, None)
    if isinstance(numeric, int):
        logging.getLogger().setLevel(numeric)
        logging.getLogger("app").setLevel(numeric)


@router.put("")
def update_settings(
    body: SettingsPatch,
    _: str = Depends(security.require_user),
) -> dict:
    """部分更新；返回更新后的全量对象（值同样是已解析类型）。"""
    allowed = set(KNOWN_KEYS)
    raw_all = body.model_dump(exclude_unset=True)
    raw_updates = body.known_updates(allowed)
    unknown = sorted(set(raw_all) - allowed)
    if unknown:
        raise bad_request(
            "未知的设置项：" + "、".join(unknown) + "。可用设置项：" + "、".join(sorted(allowed)) + "。"
        )

    to_store: dict[str, str] = {}
    if "qbittorrent_password" in raw_all and raw_all["qbittorrent_password"] is None:
        # 显式 JSON null = **清空**（§5.0 冻结）：给用户一条"把填错的密码去掉"的通路。
        # 三种写法的语义必须互不重叠：
        #   null    -> 清空（本分支）
        #   "***"   -> 保持原值（前端把 GET 回来的整个对象原样回传）
        #   缺省     -> 不变（只改了别的字段）
        # 少了这一支，用户填错密码后就永远只能改成另一个非空值。
        to_store["qbittorrent_password"] = ""
    for key, value in raw_updates.items():
        if key == "hook_token":
            if value == KEEP_PLACEHOLDER:
                continue
            if not value.strip():
                # 空串 = 重新生成一个：这是从 UI 重置回调 token 的唯一入口，
                # 明文 token 本来就展示给用户，这里不需要额外的确认流程。
                to_store[key] = secrets.token_urlsafe(32)
                continue
            to_store[key] = value.strip()
            continue
        if key == "qbittorrent_password" and value == KEEP_PLACEHOLDER:
            # 前端把整个设置对象原样回传时不冲掉密码。
            continue
        if key == "log_level":
            normalized = value.strip().upper()
            if normalized not in VALID_LOG_LEVELS:
                raise bad_request(
                    f"日志级别只能是 {'、'.join(VALID_LOG_LEVELS)} 之一，收到 {value}。"
                )
            to_store[key] = normalized
            continue
        to_store[key] = _coerce(key, value)

    for key, value in to_store.items():
        set_setting(key, value)
    _apply_side_effects(to_store)
    return settings_view()


@router.post("/test-qbittorrent")
def test_qbittorrent(
    body: QbTestRequest | None = None,
    _: str = Depends(security.require_user),
) -> dict:
    """测试 qBittorrent 连通性（§5.0：连不上是**业务结果**，返回 200 + ``ok:false``）。

    字段缺省时用已保存的设置；``password`` 传 ``"***"`` 时取库里真值。
    """
    body = body or QbTestRequest()
    url = (body.url if body.url is not None else (get_setting("qbittorrent_url", "") or "")).strip()
    username = (
        body.username if body.username is not None else (get_setting("qbittorrent_username", "") or "")
    ).strip()
    if body.password is None or body.password == KEEP_PLACEHOLDER:
        password = get_setting("qbittorrent_password", "") or ""
    else:
        password = body.password

    if not url:
        # 参数不全属于**调用错误**（而不是"连不上"），所以这里给 400。
        raise bad_request("尚未配置 qBittorrent 地址，请先填写地址再测试连接。")
    if not url.startswith(("http://", "https://")):
        raise bad_request("qBittorrent 地址必须以 http:// 或 https:// 开头。")

    ok, message, latency_ms = latency_of(url, username, password)
    return {
        "ok": bool(ok),
        "version": message if ok else None,
        "message": ("连接成功，qBittorrent 版本 " + message) if ok else message,
        "latency_ms": int(latency_ms),
    }
