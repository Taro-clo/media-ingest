"""``/api/hooks/*``（ARCHITECTURE §5 与 §8）。

qBittorrent 下载完成后通过「运行外部程序」用 ``curl`` 回调这里（**唯一给定形式**，§8 那一行的
逐字副本：同样是 7 个参数、同样顺序，此处只为阅读折行）：

```
curl -s -X POST "http://192.168.1.100:18081/api/hooks/qbittorrent?token=<HOOK_TOKEN>" \\
  --data-urlencode "name=%N" --data-urlencode "hash=%I" --data-urlencode "category=%L" \\
  --data-urlencode "tags=%G" --data-urlencode "content_path=%F" --data-urlencode "save_path=%D" \\
  --data-urlencode "root_path=%R"
```

**标准命令一律是这 7 个参数，不要传 ``kind``**：qBittorrent 的 ``%K`` 是 **Torrent ID**，
把它填进 ``kind`` 只会得到一串数字。``kind`` 是本接口的**可选高级覆盖项**（见 ``EXPLICIT_KINDS``），
由人手写 ``--data-urlencode "kind=rename_hardlink"`` 才生效（向后兼容已经这么配的用户）；
``PARAM_NAMES`` 之外的占位符一律被静默忽略，所以多传不会报错、也不会影响匹配。

三个必须记住的点：

1. **参数在表单体里**（``--data-urlencode`` 发的是 ``application/x-www-form-urlencoded``），
   ``token`` 在查询串里。所以本接口是 ``async`` 的，要自己把请求体读出来解析。
2. ``%F``/``%D``/``%R`` **通常不需要路径转换**：本项目推荐把 ``/downloads``、``/library``
   挂成与 qBittorrent 容器**完全相同**的容器路径（ARCHITECTURE §2），qB 递过来的就是容器路径。
   仍然调用 ``paths.to_container()`` 兜底：它第一步就是"已是容器路径 -> 原样返回"，
   万一将来的挂载方式变了（或用户在库里手填了宿主路径）也不至于匹配不上。
   **两边容器路径不一致时**（qB 里 ``/downloads``、本应用里 ``/vol1/1000/Download/qbittorrent``），
   在设置页填「回调路径映射」（``downloader_path_map``）：本函数在匹配库之前先用
   ``paths.translate_downloader_path`` 把 ``content_path`` 换成应用容器路径，
   换算结果必须真实存在，否则不换算并给出 warning（详见 ``paths`` 里那段的说明）。
   换算后的路径已经属于「应用空间」，因此再调用 ``to_container`` 会做**双重换算** ——
   ``_match_library`` 的 ``already_app_space`` 参数就是用来跳过那一步的。
3. **全部匹配条件为空的库永远不匹配**（§5.3 末段）：一个三条条件都没填的库，
   如果不能"不匹配"，就会抓住每一个完成的种子——那是比漏抓危险得多的默认行为。

> ⚠️ 已知坑（Starlette 0.41.3 + fastapi 0.115.6，即镜像 pin 的版本）：
> ``await request.form()`` 对 ``application/x-www-form-urlencoded`` **也会**要求
> ``python-multipart``，缺库时抛
> ``AssertionError: The `python-multipart` library must be installed to use form parsing.``。
> 所以本模块**不用** ``request.form()`` 处理 urlencoded 体，改用
> ``urllib.parse.parse_qsl`` 直接解析 ``await request.body()``：
> 少一个运行时依赖，且失败时不会静默退化成"参数全空、永不命中"。
> 只有真正的 ``multipart/form-data`` 才回落到 ``request.form()``（本项目的回调用不到）。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request

from .. import paths, security
from ..db import get_conn, get_setting
from ..queue import TaskSpec, queue
from .common import unauthorized

logger = logging.getLogger("app.hooks")

router = APIRouter(prefix="/api/hooks", tags=["hooks"])

#: 回调参数名（§5.3 表格，顺序固定）。
PARAM_NAMES = (
    "name",
    "hash",
    "category",
    "tags",
    "content_path",
    "save_path",
    "kind",
    "root_path",
)

#: 允许 ``kind`` 参数显式覆盖的取值（其余一律按库的处理器推导）。
EXPLICIT_KINDS = {
    "hardlink": "hardlink",
    "link": "hardlink",
    "rename": "rename",
    "rename_hardlink": "rename_hardlink",
    "link+rename": "rename_hardlink",
}


def kind_for_processor(processor_id: str | None) -> str:
    """库的处理器 -> 默认任务类型（§5.0 第 1 条取值）。

    **不再靠"id 是不是 bangumi/movie"猜**：处理器可以是用户自定义的任意 id，
    是否重命名只有注册表里的 ``kind`` 说了算（``rename`` -> ``rename_hardlink``）。
    查不到（处理器被删/库还没修好）时退回"只建硬链接"——绝不猜成 rename，因为
    重命名会改库里的文件名，猜错的代价比少做一步大得多。
    """
    pid = (processor_id or "").strip()
    if not pid:
        return "hardlink"
    try:
        from ..processors import registry

        spec = registry.get_processor(pid)
    except Exception:  # noqa: BLE001 - 回调路径不能因为查库失败而 500
        logger.warning("回调时无法读取处理器 %s 的规格，按只建硬链接处理。", pid, exc_info=True)
        return "hardlink"
    if spec and str(spec.get("kind")) == "rename":
        return "rename_hardlink"
    return "hardlink"


def _extract_token(request: Request, form_token: str | None) -> str | None:
    """token 优先取查询串，其次表单，最后 ``Authorization: Bearer``。"""
    token = request.query_params.get("token")
    if not token:
        token = form_token
    if not token:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    return token


def _split_tags(raw: str | None) -> set[str]:
    """把 qB 的标签串拆成集合。

    ``%G`` 的分隔符不稳定（qB 自己用 ``, `` 空格``，手工填的参数可能带别的），
    所以逗号、分号、空白都当分隔符；比较时统一小写，避免 "Anime" / "anime" 漏匹配。
    """
    if not raw:
        return set()
    normalized = raw.replace("，", ",").replace("、", ",").replace(";", ",")
    for char in "\t\n\r":
        normalized = normalized.replace(char, ",")
    parts = [part.strip() for part in normalized.replace(" ", ",").split(",")]
    return {part.lower() for part in parts if part}


def _first_folder_under(source_dir: str, content_path: str) -> str | None:
    """``content_path`` 落在 ``source_dir`` 里时，返回它下面的**一级文件夹名**。

    这是把"某个种子完成了"翻译成硬链接阶段能接受的 ``folder`` 参数的唯一途径：
    硬链接只扫**一级文件夹**（原生实现的既有行为），
    所以范围只能切到一级。
    返回 ``None`` 表示切不出来（单文件直接躺在源目录里、或路径不在源目录下）。
    """
    if not source_dir or not content_path:
        return None
    source = paths.normalize(source_dir)
    content = paths.normalize(content_path)
    if not paths.is_inside(content, source) or content == source:
        return None
    relative = content[len(source):].lstrip("/")
    if not relative:
        return None
    first = relative.split("/", 1)[0]
    return first or None


def _match_library(
    row, category: str, tags: set[str], content_path: str, *, already_app_space: bool = False
) -> tuple[bool, str]:
    """判断一个库是否命中；返回 ``(是否命中, 未命中的中文原因)``。

    ``already_app_space=True`` 表示 ``content_path`` 已经过「回调路径映射」换算、
    属于本应用容器路径，**不能再套一次** ``paths.to_container``（否则命中
    ``host_path_map`` 宿主前缀时会被换算两次）。默认 ``False`` 保持原有兜底行为。
    """
    try:
        import json

        categories = json.loads(row["match_categories"] or "[]")
        match_tags = json.loads(row["match_tags"] or "[]")
        prefixes = json.loads(row["match_path_prefixes"] or "[]")
    except (ValueError, TypeError):
        return False, "库的匹配条件不是合法 JSON，请在媒体库页面重新保存一次。"
    categories = categories if isinstance(categories, list) else []
    match_tags = match_tags if isinstance(match_tags, list) else []
    prefixes = prefixes if isinstance(prefixes, list) else []

    if not categories and not match_tags and not prefixes:
        return False, "该库没有配置任何匹配条件（分类/标签/路径前缀全为空），回调不会命中它。"

    if categories:
        if not category:
            return False, f"该库要求分类 {'、'.join(map(str, categories))}，但回调没有带分类。"
        if category not in [str(item) for item in categories]:
            return False, f"分类 {category} 不在该库的分类列表里。"
    if match_tags:
        if not tags:
            return False, "该库配置了标签条件，但回调没有带标签。"
        wanted = {str(item).strip().lower() for item in match_tags}
        if not (wanted & tags):
            return False, "回调带的标签与该库的标签条件没有交集。"
    if prefixes:
        if not content_path:
            return False, "该库配置了路径前缀条件，但回调没有带 content_path。"
        # 前缀在库里可能是宿主路径（用户手填），统一换算到容器路径再比。
        # 但若 content_path 已经过回调路径映射（应用空间），再 to_container 就是双重换算。
        normalized = paths.normalize(
            content_path if already_app_space else paths.to_container(content_path)
        )
        if not any(
            paths.is_inside(normalized, paths.normalize(paths.to_container(str(prefix))))
            for prefix in prefixes
            if str(prefix).strip()
        ):
            return False, f"content_path {content_path} 不在该库的任何路径前缀之下。"
    return True, ""


async def _form_fields(request: Request) -> dict[str, str]:
    """把回调的请求体解析成一份字段表（解析失败返回空 dict，绝不抛）。

    - ``application/x-www-form-urlencoded``（qB 的 ``curl --data-urlencode``、
      以及任何 ``-d`` 请求）：自己用 ``parse_qsl`` 解析，**不依赖 python-multipart**。
    - 其它类型：先试 ``request.form()``（只有 multipart 才真的需要它），
      失败就记一条 warning 再当空表单——静默吞掉异常正是当初"回调永不命中"的成因。
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "回调的 multipart 表单解析失败（%s: %s），本次按空表单处理。"
                "若容器内缺 python-multipart，请改用 application/x-www-form-urlencoded 回调。",
                type(exc).__name__,
                exc,
            )
            return {}
        return {key: str(value) for key, value in form.items() if isinstance(value, str)}

    raw = await request.body()
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    return {key: value for key, value in parse_qsl(text, keep_blank_values=True)}


async def _read_params(request: Request) -> tuple[dict, str | None]:
    """把表单体与查询串合成一份参数表（**查询串优先于表单**）。

    优先方向与直觉相反，特意写清楚：循环里是 ``params[key] = request.query_params.get(key)``，
    查询串的值会覆盖同名表单值。实际使用中没有冲突——qBittorrent 把 7 个参数放在**表单体**里，
    而 ``token`` 放在**查询串**里，两边键名不重叠；真出现同名时以 URL 上那一个为准。
    """
    params: dict[str, str] = await _form_fields(request)
    form_token = params.get("token")
    for key in PARAM_NAMES:
        value = request.query_params.get(key)
        if value is not None:
            params[key] = str(value)
    return params, form_token


@router.post("/qbittorrent")
async def qbittorrent_hook(request: Request) -> dict:
    """qBittorrent 下载完成回调：命中所有匹配的库并全部入队。"""
    params, form_token = await _read_params(request)
    if not security.check_hook_token(_extract_token(request, form_token)):
        raise unauthorized(
            "回调 token 不正确。请在「设置」页复制完整的回调命令（含 token=...），"
            "或重新生成 token 后更新 qBittorrent 的「运行外部程序」。"
        )

    category = (params.get("category") or "").strip()
    tags = _split_tags(params.get("tags"))
    raw_content_path = (params.get("content_path") or "").strip()
    explicit_kind = EXPLICIT_KINDS.get((params.get("kind") or "").strip().lower())

    # 回调路径映射（qB 容器路径 -> 本应用容器路径）。设置坏掉时只降级、
    # 绝不让 /api/hooks/* 500：回调链路上任何异常都会变成 qB 里一条静默失败。
    path_map: list[tuple[str, str]] = []
    try:
        path_map = paths.parse_downloader_map(get_setting("downloader_path_map", "") or "")
    except ValueError:
        logger.warning("设置项 downloader_path_map 不合法，本次回调不做路径换算。", exc_info=True)
    content_path, mapped_rule, mapped_warning = paths.translate_downloader_path(
        raw_content_path, path_map
    )
    # 换算成功的路径已是「应用空间」；未换算（含换算后不存在）时仍按原语义处理。
    content_path_is_app_space = mapped_rule is not None

    rows = get_conn().execute(
        "SELECT * FROM libraries WHERE enabled=1 ORDER BY id ASC"
    ).fetchall()
    task_ids: list[int] = []
    ignored: list[str] = []
    ignored_details: list[dict] = []
    warnings: list[str] = []
    if mapped_rule:
        warnings.append(
            f"回调路径已按映射 {mapped_rule} 换算：{raw_content_path} → {content_path}"
        )
        logger.info("qB 回调路径换算（%s）：%s → %s", mapped_rule, raw_content_path, content_path)
    elif mapped_warning:
        warnings.append(mapped_warning)
        logger.warning("qB 回调路径映射未生效：%s", mapped_warning)

    for row in rows:
        matched, reason = _match_library(
            row,
            category,
            tags,
            content_path,
            already_app_space=content_path_is_app_space,
        )
        if not matched:
            ignored.append(row["name"])
            ignored_details.append(
                {"library_id": int(row["id"]), "name": row["name"], "reason": reason}
            )
            continue

        source_dir = paths.normalize(row["source_dir"])
        folder = _first_folder_under(source_dir, content_path)
        scope = {"mode": "folder", "folder": folder} if folder else {"mode": "full"}
        if not folder and content_path:
            # 单文件种子：content_path 直接躺在源目录（或不在源目录下）。
            # 原生实现只遍历一级**文件夹**，所以这个文件本来就不会被链接；
            # 退回全量只是为了让同一批次里其它遗漏的文件夹也能被处理。
            warnings.append(
                f"{row['name']}：回调的 content_path={content_path} 不在源目录 {source_dir} 的"
                f"某个一级文件夹下，无法缩小范围，本次按全量执行"
                f"（注意：只处理源目录下的一级文件夹，直接躺在源目录下的单文件不会被创建硬链接）。"
            )
        kind = explicit_kind or kind_for_processor(row["processor_id"])
        task_ids.append(
            queue.submit(
                TaskSpec(
                    library_id=int(row["id"]),
                    kind=kind,
                    trigger="hook",
                    scope=scope,
                    payload={
                        "torrent_name": params.get("name") or "",
                        "torrent_hash": params.get("hash") or "",
                        "category": category,
                        "tags": sorted(tags),
                        # 原始值（qB 视角）与前缀换算后的应用路径都留着：
                        # 排查"为什么跑了整库/为什么没命中"时两个都要看。
                        "content_path": raw_content_path,
                        "content_path_app": content_path,
                        "save_path": params.get("save_path") or "",
                        "root_path": params.get("root_path") or "",
                    },
                )
            )
        )

    if task_ids:
        logger.info(
            "qB 回调命中 %d 个库（种子=%s）：任务 %s",
            len(task_ids),
            params.get("name") or params.get("hash") or "?",
            task_ids,
        )
    else:
        logger.info(
            "qB 回调没有命中任何媒体库（种子=%s，分类=%r，标签=%s，content_path=%r）",
            params.get("name") or params.get("hash") or "?",
            category,
            sorted(tags),
            content_path,
        )
        if not rows:
            warnings.append("当前没有任何**已启用**的媒体库，回调无处可去。")
    result = {
        "matched": len(task_ids),
        "tasks": task_ids,
        "ignored": ignored,
        "ignored_details": ignored_details,
        "warnings": warnings,
        "torrent": {
            "name": params.get("name") or "",
            "hash": params.get("hash") or "",
        },
    }
    # 只有配置了映射才带上这一块（没配 = 行为与旧版逐字节一致，前端/脚本无需改）。
    if path_map:
        result["translation"] = {
            "applied": bool(mapped_rule),
            "rule": mapped_rule,
            "raw": raw_content_path,
            "app": content_path,
            "warning": mapped_warning,
        }
    return result


@router.post("/scan")
async def scan_hook(request: Request) -> dict:
    """立即触发一次目录扫描（不管 ``scan_enabled`` 真假）。"""
    params, form_token = await _read_params(request)
    if not security.check_hook_token(_extract_token(request, form_token)):
        raise unauthorized("回调 token 不正确。请在「设置」页复制完整的回调命令（含 token=...）。")

    from ..scanner import scanner

    task_ids = scanner.scan_libraries()
    logger.info("手动扫描触发 %d 个任务：%s", len(task_ids), task_ids)
    return {
        "matched": len(task_ids),
        "tasks": task_ids,
        "ignored": [],
        "ignored_details": [],
        "warnings": [],
    }
