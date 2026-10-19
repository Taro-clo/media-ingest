"""路径工具与文件系统校验（ARCHITECTURE §6.4）。

本模块是「绝不复制回退」的落点：
- 建链**之前**用**级联判据**拦截跨文件系统 / 跨挂载；
- 建链**之后**由 ``script_adapter`` 逐文件核对 ``(st_dev, st_ino)``。

级联判据（``check_hardlink_support``，顺序即优先级）：
1. **实证探针**（主判据）：在源树里有界递归找一个**已存在**的普通文件，把它
   ``os.link`` 到目标目录的临时名再删掉。**绝不往源目录写任何东西**：源目录只读时
   这个探针照样能给出真结论，不会因为写不进去就自我降级成 ``True``（那正是假绿的来源）。
2. 探针**跑不了**（源树里没有普通文件、或非 Linux 无 mountinfo）时看**挂载 ID**
   （``/proc/self/mountinfo``）：不同 ⇒ ``False``（可靠负判据）；相同 ⇒ ``True`` 但注明「未实证」。
   注意挂载 ID 相等是**必要不充分**：btrfs 同一挂载内跨子卷仍可能 EXDEV。
3. 挂载 ID 也不可用 ⇒ 回退 ``st_dev``：相同 ⇒ ``True`` + 注明「未实证，可能假绿」；不同 ⇒ ``False``。
4. 全都不可用 ⇒ ``None``（无法判定，调用方按**警告**处理，不硬拦）。

容器内路径与宿主路径的换算只用于**展示**（WebUI 里给人看），不参与任何文件操作。
"""

from __future__ import annotations

import errno
import os
import posixpath
import re
import uuid
from itertools import zip_longest
from typing import Iterator, Mapping

from .config import settings

#: 探针临时文件前缀：启动清扫与测试都依赖它，改名前先看 ``cleanup_probe_leftovers`` 的调用点。
PROBE_PREFIX = ".mlm_probe_"
PROBE_SUFFIX = ".link"

#: 本程序真正会创建的两类探针临时文件名（``cleanup_probe_leftovers`` 只清这两类，
#: 绝不按宽泛前缀误删用户文件 —— 见 2026-10-19 审计 F-01）：
#: - 写权限探针：``.mlm_probe_<32 位 uuid hex>.write``
#: - 硬链接探针：``.mlm_probe_<pid>_<8 位 uuid hex>.link``
_PROBE_WRITE_RE = re.compile(r"^\.mlm_probe_[0-9a-f]{32}\.write$")
_PROBE_LINK_RE = re.compile(r"^\.mlm_probe_[0-9]+_[0-9a-f]{8}\.link$")

#: 有界递归参数（冻结）：真实媒体的源树是「源根 / 标题文件夹 / 文件」，
#: 深度 3 足够；条目上限 512 保证在超大源根下也不会扫全盘。
PROBE_MAX_DEPTH = 3
PROBE_MAX_ENTRIES = 512


def _as_container(path: str) -> str:
    """把可能出现的 Windows 风格分隔符统一为 ``/``，**并保证结果以 ``/`` 开头**。

    补前导斜杠是有意为之：``host_path_map`` 的键保证是绝对容器路径（§6.1 不变量），
    而调用方可能传来 ``downloads/Bangumi`` 这种写法。``normalize()`` 走的是
    ``os.path.abspath``（结果本就绝对），因此不受这里影响；受影响的是
    ``to_host`` / ``to_container`` 的前缀比较——少了斜杠，``startswith("downloads/")``
    永远为假，映射会静默失效、原样返回，谁也不会发现。
    """
    text = (path or "").replace("\\", "/").strip()
    # Windows 盘符（``C:/...``）本身已是绝对路径，别画蛇添足加成 ``/C:/...``。
    # 项目里宿主路径都是 POSIX 路径，这一支只是为了本地开发运行在 Windows 时不出怪值。
    is_drive = len(text) >= 2 and text[1] == ":" and text[0].isalpha()
    if text and not text.startswith("/") and not is_drive:
        text = "/" + text
    return text


def to_host(container_path: str) -> str:
    """容器路径 -> 宿主路径（用于展示）。无匹配则原样返回。"""
    if not container_path:
        return container_path
    normalized = _as_container(container_path).rstrip("/") or "/"
    best_key = ""
    best_value = ""
    for container_root, host_root in settings.host_path_map.items():
        key = _as_container(container_root).rstrip("/") or "/"
        if normalized == key or normalized.startswith(key.rstrip("/") + "/"):
            # 取最长前缀，避免 /downloads 抢走 /downloads/Bangumi 的映射。
            if len(key) > len(best_key):
                best_key, best_value = key, host_root
    if not best_key:
        return container_path
    suffix = normalized[len(best_key):]
    return (best_value.rstrip("/") + suffix) or best_value


def to_container(host_path: str) -> str:
    """宿主路径 -> 容器路径。

    三条规则，顺序不能颠倒：

    1. **已经是容器路径**（以 host_path_map 的某个键开头）-> 返回**容器空间归一化后**
       的形式（保证以 ``/`` 开头、分隔符是 ``/``）。qBittorrent 回调里的
       ``%F``/``%D``/``%R`` 走的就是这条：本应用的 ``/downloads``、``/library`` 与
       qBittorrent 容器**同名挂载**，所以回调参数零转换即可直接使用，
       绝不能再去套一次映射。注意这里**不能**把原始字符串原样返回：调用方
       （``routers/libraries.py``）紧接着会做 ``normalize(to_container(text))``，
       而 ``normalize`` 对不以 ``/`` 开头的输入走宿主语义（Windows 上会解析成
       ``C:/当前目录/downloads/...``）。用户手填一个 ``downloads/Bangumi``
       就会存进库里变成这种怪物——回落入库路径的"一律容器路径"不变量。
    2. 命中 host_path_map 的**宿主**前缀 -> 换成对应容器前缀（最长前缀优先）。
    3. 都不命中 -> 原样返回（多见于开发机/测试，路径本就是相对或临时目录）。
    """
    if not host_path:
        return host_path
    normalized = _as_container(host_path).rstrip("/") or "/"

    # 规则 1：已是容器路径。必须先判，否则宿主根与容器根同形（如挂载成 /srv -> /srv）时会来回映射。
    for container_root in settings.host_path_map:
        key = _as_container(container_root).rstrip("/") or "/"
        if normalized == key or normalized.startswith(key.rstrip("/") + "/"):
            return normalized

    # 规则 2：宿主前缀反查。
    best_key = ""
    best_value = ""
    for container_root, host_root in settings.host_path_map.items():
        key = _as_container(host_root).rstrip("/") or "/"
        if normalized == key or normalized.startswith(key.rstrip("/") + "/"):
            if len(key) > len(best_key):
                best_key, best_value = key, container_root
    if not best_key:
        return host_path
    suffix = normalized[len(best_key):]
    return (best_value.rstrip("/") + suffix) or best_value


def normalize(path: str) -> str:
    """绝对化 + 去掉尾斜杠（根目录保留 ``/``），并把分隔符统一为 ``/``。

    **按输入形态分派两套语义**（这一步不能省）：

    - 以 ``/`` 开头 ⇒ 这是**容器空间**的路径（``/downloads/Bangumi``），
      用 ``posixpath.normpath`` 归一化，**绝不经过 ``os.path.abspath``**。
      Windows 上 ``os.path.abspath("/e2e/download")`` 会得到 ``C:/e2e/download``；
      而库里存的一律是容器路径（§3 不变量）。一旦被加上盘符，
      日志侧用 ``source_dir_from_log == source_dir`` 做字符串比较
      就永远为假 ⇒「体积一致则跳过」彻底失效（首次仍然全量重扫、
      日志里满屏 ``创建硬链接失败: … File exists``）——这正是该跳过判据要消灭的症状。
      Linux 容器里 ``abspath`` 恰好是恒等变换，所以这个缺陷只在 Windows 上现形，
      而 Windows 又是本项目唯一能真跑端到端的宿主。
    - 其它（``C:/…``、``C:\\…``、相对路径）⇒ 宿主原生路径，仍用 ``os.path.abspath``，
      这样 ``~`` 展开、相对路径与盘符路径的行为与改动前完全一致。

    统一分隔符是必要的：数据库里存的是容器路径，而宿主路径在 Windows 上带反斜杠，
    两者必须能比较。
    """
    if path is None:
        return path
    text = str(path)
    if os.path.expanduser(text).replace("\\", "/").startswith("/"):
        absolute = posixpath.normpath(os.path.expanduser(text).replace("\\", "/"))
        if absolute != "/" and absolute.endswith("/"):
            absolute = absolute.rstrip("/") or "/"
        return absolute
    absolute = os.path.abspath(os.path.expanduser(text)).replace("\\", "/")
    if absolute != "/" and absolute.endswith("/"):
        absolute = absolute.rstrip("/") or "/"
    return absolute


def is_inside(child: str, parent: str) -> bool:
    """判断 ``child`` 是否等于或位于 ``parent`` 之内（按路径分量比较，不用字符串前缀）。

    直接用 ``startswith`` 会把 ``/library2`` 误判成在 ``/library`` 内，
    进而让不合法配置通过校验。因此逐段比较。"""
    child_parts = [p for p in normalize(child).split("/") if p]
    parent_parts = [p for p in normalize(parent).split("/") if p]
    if len(child_parts) < len(parent_parts):
        return False
    for left, right in zip_longest(child_parts[: len(parent_parts)], parent_parts):
        if left != right:
            return False
    return True


# ------------------------------------------------------- 回调路径映射（downloader_path_map）
#
# 与 ``host_path_map``（容器↔宿主、只用于展示）是**两回事**：
# qBittorrent 容器与媒体库容器可以把同一个宿主目录挂到**不同**的容器路径
# （qB 里是 ``/downloads``，本应用里是 ``/vol1/1000/Download/qbittorrent``）。
# 回调里的 ``%F`` 是 qB 视角的路径，不换算就切不出一级文件夹 ⇒ 每次退化成整库全量。
# 这里做的是「下载器容器路径 -> 本应用容器路径」的换算，**只影响作用域选择**，
# 不参与任何文件操作之外的事情（换算结果仍然要过 ``is_inside`` / 硬链接前置校验）。

def _looks_absolute(path: str) -> bool:
    """映射两侧是否「看起来绝对」：``/xxx`` 或 Windows 盘符 ``C:/xxx``。

    Windows 盘符这一支只为**本机开发 + 测试**：真实部署在 Linux 容器里，两侧都是
    ``/`` 开头的容器路径。允许盘符不会放宽生产语义——右侧仍必须落在应用容器内。
    """
    text = str(path or "").strip().replace("\\", "/")
    if text.startswith("/"):
        return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def parse_downloader_map(text: str | None) -> list[tuple[str, str]]:
    """把设置项文本解析成 ``[(qB 路径, 应用路径), ...]``。

    文本格式（设置页按行编辑，一行一条；``#`` 开头为注释，空行忽略）::

        /downloads => /vol1/1000/Download/qbittorrent

    **抛 ``ValueError``** 时消息本身就是给用户看的中文（带行号），
    ``PUT /api/settings`` 会原样转成 400，回调侧则降级为「不换算」并记 warning。
    重复的左侧前缀直接拒绝：否则「最长前缀优先」会出现两条等长规则、结果取决于顺序。
    """
    mapping: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for lineno, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            raise ValueError(
                f"第 {lineno} 行：缺少「=>」分隔符，格式应为 "
                f"/downloads => /vol1/1000/Download/qbittorrent"
            )
        left, right = (part.strip() for part in line.split("=>", 1))
        if not left or not right:
            raise ValueError(f"第 {lineno} 行：「=>」两侧都不能为空。")
        if not _looks_absolute(left):
            raise ValueError(f"第 {lineno} 行：左侧必须是绝对路径（以 / 开头），收到 {left!r}。")
        if not _looks_absolute(right):
            raise ValueError(f"第 {lineno} 行：右侧必须是绝对路径（以 / 开头），收到 {right!r}。")
        left = normalize(left)
        right = normalize(right)
        if left == "/":
            raise ValueError(
                f"第 {lineno} 行：左侧不能是 /（那会把所有路径都换算到同一个根，"
                f"请写具体前缀，例如 /downloads）。"
            )
        if left in seen:
            raise ValueError(
                f"第 {lineno} 行：源前缀 {left} 与第 {seen[left]} 行重复，请合并成一条。"
            )
        seen[left] = lineno
        mapping.append((left, right))
    return mapping


def translate_downloader_path(
    raw: str,
    mapping: list[tuple[str, str]],
    *,
    exists=None,
) -> tuple[str, str | None, str | None]:
    """把 qB 回调里的路径换成应用容器路径。

    返回 ``(应用空间路径, 命中规则说明, 警告)``：

    - 未配置映射 / 没有前缀命中 ⇒ ``(原路径归一化, None, None)``，行为与改动前一致；
    - 命中且换算后的路径**真实存在** ⇒ ``(换算结果, "/downloads => /vol1/…", None)``；
    - 命中但换算后的路径不存在 ⇒ 不换算，返回 ``(原路径归一化, None, 警告)``。

    「存在性」这一关是有意的安全网：qB 里的 ``/downloads/Show`` 换算后如果指向一个
    碰巧同名的无关目录，按它切作用域会去处理错误的文件夹；宁可在本次退回全量并提示
    用户检查映射。``exists`` 参数供单元测试注入桩函数（开发机是 Windows，
    伪造的容器路径无法真实存在）。

    最长前缀优先：同时配了 ``/downloads`` 与 ``/downloads/Bangumi`` 时后者生效，
    且 ``is_inside`` 按路径段比较，``/downloads2`` 不会被 ``/downloads`` 误吃。
    """
    normalized = normalize(raw) if raw else ""
    if not normalized or not mapping:
        return normalized, None, None

    best: tuple[int, str, str] | None = None
    for source_root, app_root in mapping:
        if is_inside(normalized, source_root):
            if best is None or len(source_root) > best[0]:
                best = (len(source_root), source_root, app_root)
    if best is None:
        return normalized, None, None

    _, source_root, app_root = best
    relative = normalized[len(source_root):].lstrip("/")
    translated = normalize(app_root.rstrip("/") + ("/" + relative if relative else ""))
    rule = f"{source_root} => {app_root}"
    probe = exists or os.path.exists
    if not probe(translated):
        return normalized, None, (
            f"回调路径映射 {rule} 命中，但换算后的路径 {translated} 在应用容器里不存在，"
            f"本次不换算（请检查映射右侧是否是应用容器内可见的路径）。"
        )
    return translated, rule, None


def existing_ancestor(path: str) -> str:
    """取 ``path`` 自身或最深的**已存在**祖先；用于 ``st_dev`` 比较与可写探测。

    预检阶段目标目录往往还不存在（``create_hard_links`` 自己会 ``makedirs``），
    所以必须先上溯到已存在的祖先再比较设备号。"""
    current = normalize(path)
    while True:
        if os.path.exists(current):
            return current
        parent = os.path.dirname(current) or "/"
        parent = normalize(parent) if parent != current else parent
        if parent == current:
            return current
        current = parent


def _st_dev(path: str) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def check_same_filesystem(source: str, target: str) -> tuple[bool, str | None]:
    """返回 ``(ok, error_message)``：源与目标是否在同一文件系统。

    两目录都取**已存在的最深祖先**再比 ``st_dev``。不同设备 → ``os.link`` 必然
    返回 ``EXDEV``，必须在这里前置拦截，绝不回退为复制。
    错误文案是 ARCHITECTURE §2 冻结的全文，WebUI/日志直接展示。
    """
    src_ancestor = existing_ancestor(source)
    dst_ancestor = existing_ancestor(target)
    src_dev = _st_dev(src_ancestor)
    dst_dev = _st_dev(dst_ancestor)
    if src_dev is None or dst_dev is None:
        missing = src_ancestor if src_dev is None else dst_ancestor
        return False, f"路径不存在或不可访问：{missing}，无法比较文件系统。"
    if src_dev == dst_dev:
        return True, None
    return False, (
        f"跨文件系统错误：源 {src_ancestor} (st_dev={src_dev}) 与目标根 {dst_ancestor} "
        f"(st_dev={dst_dev}) 不在同一文件系统，os.link 会返回 EXDEV。"
        f"本应用不会回退为复制文件。请把两个挂载点指向同一宿主文件系统。"
    )


def check_cross_mounts(source_dir: str, target_dir: str) -> tuple[bool, str | None]:
    """目录归属校验：两者不得相同、也不得互相包含。

    互相包含会让“源”与“目标”重叠，硬链接可能在源目录内生成新文件，
    违反“绝不动 qBittorrent 原文件”的前提。"""
    src = normalize(source_dir)
    dst = normalize(target_dir)
    if src == dst:
        return False, "源目录与目标目录不能相同。"
    if is_inside(src, dst):
        return False, f"源目录 {src} 位于目标目录 {dst} 之内，不允许互相包含。"
    if is_inside(dst, src):
        return False, f"目标目录 {dst} 位于源目录 {src} 之内，不允许互相包含。"
    return True, None


def find_sample_file(
    root: str,
    *,
    max_depth: int = PROBE_MAX_DEPTH,
    max_entries: int = PROBE_MAX_ENTRIES,
) -> str | None:
    """在 ``root`` 下有界地找一个**普通文件**（实证探针的取样）。

    - **广度优先**，先看浅层：真实媒体的源根下第一层全是标题文件夹，第二层才是文件。
    - **不跟随符号链接**（``follow_symlinks=False``）：跟随可能把探针引到另一个挂载上，
      得到的结论就与真实硬链接行为无关了；也可能绕出源树。
    - 跳过 ``.mlm_probe_*``（自己或上次被杀留下的残留）。
    - 深度与条目都有上限，超限就当"找不到"，由调用方降级到挂载 ID 判据。
    """
    start = normalize(root)
    budget = max(0, int(max_entries))
    queue: list[tuple[str, int]] = [(start, 0)]
    while queue and budget > 0:
        current, depth = queue.pop(0)
        try:
            with os.scandir(current) as listing:
                entries = sorted(listing, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if budget <= 0:
                return None
            budget -= 1
            if entry.name.startswith(PROBE_PREFIX):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    return os.path.join(current, entry.name)
                if entry.is_dir(follow_symlinks=False) and depth + 1 <= max_depth:
                    queue.append((os.path.join(current, entry.name), depth + 1))
            except OSError:
                continue
    return None


# ------------------------------------------------- 建库权限预检（按运行身份实拍）
#
# 建库/改库保存时**以应用自己的运行身份**实测「读源、写目标」，而不是只看 mode 位：
# 有些 NAS 有自定义权限层（如 fnOS 的 trimacl），mode 位看起来不可读却实际可读，
# 也可能反过来；只有真正以当前进程身份访问一次才可靠。
# 这不是「预检」按钮的替代 —— 按钮仍然保留（它还会额外做 os.link 实证），
# 这里是在保存那一刻就把「必然失败」的配置拦下来，而不是等第一次跑任务才发现。


def describe_process_identity() -> str:
    """当前进程的运行身份（容器内即 PUID/PGID 生效后的 uid/gid），用于报错文案。"""
    try:
        uid = os.getuid()
        gid = os.getgid()
    except AttributeError:  # pragma: no cover - Windows 开发机
        return "当前进程（Windows 开发机没有 uid/gid）"
    try:
        groups = sorted({gid, *os.getgroups()})
    except OSError:  # pragma: no cover - 极端情况下取不到
        groups = [gid]
    return f"uid={uid}, gid={gid}, 附加组=[{', '.join(str(g) for g in groups)}]"


def _can_read_directory(path: str) -> bool:
    """目录能否被当前身份列出并进入（R + X）。"""
    try:
        return os.access(path, os.R_OK | os.X_OK)
    except OSError:  # pragma: no cover - os.access 不抛，仅防御
        return False


def _write_probe(directory: str) -> tuple[bool, str | None]:
    """在目录里真写一个临时文件再删掉。

    ``os.access`` 只看 mode 位与身份，识破不了**只读挂载**和某些 ACL；真写一次
    才能确认「建链阶段不会因为写不进目标目录而失败」。临时文件用 ``PROBE_PREFIX``
    前缀（与硬链接探针同一套残留清扫规则），正常路径会立即删除。
    """
    probe = os.path.join(normalize(directory), f"{PROBE_PREFIX}{uuid.uuid4().hex}.write")
    try:
        handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        return False, f"{exc.strerror or exc}（errno={exc.errno}）"
    try:
        os.write(handle, b"ok")
    except OSError as exc:  # pragma: no cover - 打开成功但写失败（磁盘满/配额）很少见
        return False, f"{exc.strerror or exc}（errno={exc.errno}）"
    finally:
        os.close(handle)
        try:
            os.unlink(probe)
        except OSError:  # pragma: no cover - 删不掉只留一个探针文件，不影响结论
            pass
    return True, None


def _permission_issue(kind: str, label: str, path: str, cause: str, fix: str) -> dict:
    """一条可读的权限问题：``code`` 给程序看，``label``/``cause``/``fix`` 给用户看。

    ``fix`` 的第一条必须是「调整 PUID/PGID」——那是绝大多数部署里真正的根因
    （容器身份与宿主目录属主不一致），其余 chmod/chown 只是补救。
    """
    return {
        "kind": kind,
        "label": label,
        "path": path,
        "cause": cause,
        "fix": fix,
    }


def probe_media_access(source_dir: str, target_dir: str) -> dict:
    """用**当前进程身份**实测：源目录可读可进入、目标目录可写。

    建库/改库保存时调用；返回::

        {"ok": bool, "identity": str, "errors": [...], "warnings": [...],
         "issues": [{kind, label, path, cause, fix}], ...}

    ``errors`` 是给 ``/validate`` 与日志用的**一行短文案**；``issues`` 是给 WebUI
    保存失败弹窗用的结构化明细（每条带原因与处理办法）。

    - 源目录不存在 / 不是目录 / 不可读 ⇒ ``errors``（建链阶段必失败，提前拦）
    - 目标目录已存在但写不进 ⇒ ``errors``（用真实临时文件探测，只读挂载也拦得住）
    - 目标目录还不存在 ⇒ 检查最近已存在的祖先可写（建链阶段会自己创建）
    - 源树抽样文件不可读 ⇒ ``warnings``（可能只是个别文件，其余文件照旧处理）
    """
    identity = describe_process_identity()
    report: dict = {
        "ok": True,
        "identity": identity,
        "errors": [],
        "warnings": [],
        "issues": [],
        "source_exists": False,
        "target_exists": False,
        "probe": None,
    }
    source = normalize(source_dir)
    target = normalize(target_dir)

    report["source_exists"] = os.path.isdir(source)
    if not report["source_exists"]:
        report["errors"].append(f"源目录不存在：{source}")
        report["issues"].append(
            _permission_issue(
                "source_missing",
                "源目录不存在",
                source,
                "建链阶段需要读取这个目录，目录不存在会直接报错（先完成一次下载，或修正挂载路径）",
                "在 qBittorrent 里完成一次下载后重试；或把源目录改成一个已存在的目录",
            )
        )
    elif not _can_read_directory(source):
        report["errors"].append(f"源目录不可读：{source}")
        report["issues"].append(
            _permission_issue(
                "source_unreadable",
                "源目录不可读",
                source,
                f"运行身份 {identity} 对该目录没有读/进入权限，建链时会报 Permission denied",
                f"先把容器 PUID/PGID 改成该目录属主；或 chmod 755 {source}；或 chown 到运行身份",
            )
        )

    report["target_exists"] = os.path.isdir(target)
    if report["target_exists"]:
        writable, note = _write_probe(target)
        report["probe"] = {"target": target, "ok": writable, "note": note}
        if not writable:
            report["errors"].append(f"目标目录不可写：{target}（{note}）")
            report["issues"].append(
                _permission_issue(
                    "target_unwritable",
                    "目标目录不可写",
                    target,
                    f"运行身份 {identity} 在该目录创建文件被拒绝（{note}），建链阶段会失败",
                    f"先把容器 PUID/PGID 改成该目录属主；或 chmod 775 {target}；或 chown 到运行身份",
                )
            )
    elif os.path.exists(target):
        report["errors"].append(f"目标路径不是目录：{target}")
        report["issues"].append(
            _permission_issue(
                "target_not_directory",
                "目标路径不是目录",
                target,
                "该路径已经存在，但不是目录，无法作为媒体库的目标目录",
                "把目标目录改成一个目录路径（或先删掉同名文件再试）",
            )
        )
    else:
        ancestor = existing_ancestor(target)
        writable, note = _write_probe(ancestor)
        report["probe"] = {"target_ancestor": ancestor, "ok": writable, "note": note}
        if not writable:
            report["errors"].append(f"目标目录无法创建：{target}（上级 {ancestor} 不可写，{note}）")
            report["issues"].append(
                _permission_issue(
                    "target_parent_unwritable",
                    "目标目录无法创建",
                    target,
                    f"目标目录尚不存在，运行身份 {identity} 也无法在最近的已存在目录 "
                    f"{ancestor} 中创建它（{note}）",
                    f"先把容器 PUID/PGID 改成 {ancestor} 的属主；或 chmod 775 {ancestor}；"
                    f"或 chown 到运行身份",
                )
            )

    if report["source_exists"] and _can_read_directory(source):
        sample = find_sample_file(source)
        if sample is not None and not os.access(sample, os.R_OK):
            report["warnings"].append(
                f"源树抽样文件不可读：{sample}（运行身份 {identity}）："
                f"该文件建链时会失败，其它文件照旧处理，排查见 FAQ 第 43 问。"
            )
    report["ok"] = not report["errors"]
    return report


def is_own_probe_artifact(name: str) -> bool:
    """名字是否是**本程序自己**创建的探针临时文件（严格格式匹配）。

    审计 F-01：旧实现只按 ``.mlm_probe_`` 前缀清理，会把用户碰巧同前缀的普通文件
    也删掉。这里改成只认两类真实产物格式；其它任何名字（哪怕前缀相同）一律不动。
    """
    return bool(_PROBE_WRITE_RE.match(name) or _PROBE_LINK_RE.match(name))


def cleanup_probe_leftovers(directory: str) -> int:
    """清扫 ``directory`` 里遗留的**本程序探针**临时文件（**不递归**）。

    探针正常路径会自己删掉临时链接；但进程被 SIGKILL / 容器被强杀时可能残留。
    启动时对目标根扫一遍，探针前后也各扫一遍（自己那一个）。返回删除条数。

    只删严格匹配 ``.mlm_probe_<32hex>.write`` 或 ``.mlm_probe_<pid>_<8hex>.link``
    的**普通文件**（不跟随符号链接）；同前缀的其它文件/目录一律保留（审计 F-01）。
    """
    removed = 0
    try:
        with os.scandir(normalize(directory)) as listing:
            victims = [e.path for e in listing if is_own_probe_artifact(e.name)]
    except OSError:
        return 0
    for victim in victims:
        try:
            # 只清普通文件；符号链接/目录即使名字撞上也别碰（避免顺手删掉用户数据）。
            if os.path.islink(victim) or not os.path.isfile(victim):
                continue
            os.unlink(victim)
            removed += 1
        except OSError:
            pass
    return removed


_MOUNTINFO_ESCAPES = ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\"))


def _unescape_mountinfo(text: str) -> str:
    for raw, real in _MOUNTINFO_ESCAPES:
        text = text.replace(raw, real)
    return text


def mount_id_for(path: str) -> tuple[str, str] | None:
    """返回 ``path`` 所属的 ``(mount ID, 挂载点)``；读不到 mountinfo（非 Linux）返回 ``None``。

    取**最长前缀**匹配的挂载点：``/library/sub`` 这种子目录挂载必须赢过 ``/library``。
    """
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    target = normalize(path)
    best: tuple[str, str] | None = None
    best_len = -1
    for line in lines:
        # 形如: 36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw,errors=continue
        head = line.split(" - ", 1)[0].split()
        if len(head) < 5:
            continue
        mount_id = head[0]
        mount_point = _unescape_mountinfo(head[4]).rstrip("/") or "/"
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > best_len:
                best, best_len = (mount_id, mount_point), len(mount_point)
    return best


def check_same_mount(source: str, target: str) -> tuple[bool | None, str]:
    """返回 ``(same, note)``：源与目标是否属于**同一个挂载**（预筛判据）。

    ``same=False`` 是**可靠负判据**：不同挂载之间 ``os.link`` 一定 EXDEV。
    ``same=True`` 只是**必要不充分**——btrfs 同一个挂载内跨子卷（``subvol=``）仍可能 EXDEV，
    所以调用方必须把它标成「未实证」。读不到 ``/proc/self/mountinfo`` 时返回 ``None``。
    """
    src_ancestor = existing_ancestor(source)
    dst_ancestor = existing_ancestor(target)
    src = mount_id_for(src_ancestor)
    dst = mount_id_for(dst_ancestor)
    if src is None or dst is None:
        return None, "无法读取 /proc/self/mountinfo，挂载 ID 判据不可用。"
    if src[0] == dst[0]:
        return True, (
            f"源 {src_ancestor} 与目标 {dst_ancestor} 同属挂载 {src[1]}（mount ID {src[0]}）。"
            "注意：挂载 ID 相等是**必要不充分**判据（btrfs 同一挂载内跨子卷仍可能 EXDEV）。"
        )
    return False, (
        f"跨挂载错误：源 {src_ancestor}（挂载点 {src[1]}，mount ID {src[0]}）与目标根 "
        f"{dst_ancestor}（挂载点 {dst[1]}，mount ID {dst[0]}）不是同一个挂载，os.link 会返回 EXDEV。"
        f"本应用不会回退为复制文件。请把两个目录放到同一个宿主挂载下。"
    )


def probe_cross_filesystem(source_dir: str, target_dir: str) -> tuple[bool | None, str]:
    """**实证** ``os.link`` 在源/目标之间是否可用（**主判据**）。

    做法：在源树里有界递归找一个**已存在**的普通文件（``find_sample_file``），把它
    ``os.link`` 到目标目录的 ``.mlm_probe_<pid>_<rand>.link``，然后删掉那个链接。

    **只读安全**：源目录**一个字节都不写**，因此源目录只读时也能得到真结论，
    不会因为写不进去就自我降级。用户拒绝写源目录也不再影响这个探针。

    返回 ``(ok, note)``：
    - ``ok=True``：**已实证**可以在源/目标间建硬链接；
    - ``ok=False``：**已实证**不行（例如 EXDEV 跨挂载），``note`` 是中文原因；
    - ``ok=None``：**无法实证**（源树里没有普通文件 / 目标不可写 / 挂了但拿到别的 errno），
      ``note`` 说明原因，调用方降级到挂载 ID 判据。
    """
    src_ancestor = existing_ancestor(source_dir)
    dst_ancestor = existing_ancestor(target_dir)
    if not os.path.isdir(dst_ancestor):
        return False, f"目标路径 {dst_ancestor} 不是目录，无法创建硬链接。"
    if not os.path.isdir(src_ancestor):
        return False, f"源路径 {src_ancestor} 不是目录，无法读取。"

    cleanup_probe_leftovers(dst_ancestor)
    sample = find_sample_file(src_ancestor)
    if sample is None:
        return None, (
            f"无法实证：源树 {src_ancestor} 在深度 {PROBE_MAX_DEPTH}、条目 {PROBE_MAX_ENTRIES} 之内"
            f"找不到任何普通文件（不跟随符号链接），无法取样建硬链接。"
        )
    if not os.access(dst_ancestor, os.W_OK):
        return None, f"无法实证：目标目录 {dst_ancestor} 不可写，无法在其下建临时链接。"

    # 探针必须**无痕**：在目录里建/删文件会把该目录的 mtime 改掉，而目标目录通常就是媒体库
    # 目录本身 —— "目标库逐字节不变" 守卫、以及 Emby/媒体扫描器都会看见这次改动。
    # 所以先记下它的 (atime, mtime)，探测完原样恢复。restore 失败不算故障（某些文件系统
    # 不允许改目录时间戳），只是留下一个"探针访问过"的时间戳。
    dst_atime_ns = dst_mtime_ns = None
    try:
        info = os.stat(dst_ancestor)
        dst_atime_ns, dst_mtime_ns = info.st_atime_ns, info.st_mtime_ns
    except OSError:
        pass

    tmp_link = os.path.join(
        dst_ancestor, f"{PROBE_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:8]}{PROBE_SUFFIX}"
    )
    try:
        os.link(sample, tmp_link)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return False, (
                f"实证失败：把 {sample} 硬链接到 {tmp_link} 时 os.link 返回 EXDEV"
                f"（{exc.strerror or exc}）：源与目标不在同一个文件系统/子卷。"
                f"本应用不会回退为复制文件；请把两个目录放到同一个宿主文件系统与子卷下。"
            )
        return None, (
            f"无法实证：把 {sample} 硬链接到 {tmp_link} 时 os.link 返回 "
            f"{exc.strerror or exc}（errno={exc.errno}）；该错误无法与「跨挂载」区分，"
            f"改用挂载 ID 预筛。"
        )
    finally:
        try:
            if os.path.lexists(tmp_link):
                os.unlink(tmp_link)
        except OSError:
            pass
        if dst_atime_ns is not None and dst_mtime_ns is not None:
            try:
                os.utime(dst_ancestor, ns=(dst_atime_ns, dst_mtime_ns))
            except OSError:
                pass
    return True, (
        f"已实证：把源树中的 {sample} 硬链接到 {dst_ancestor} 成功（临时链接已删除、"
        f"目标目录时间戳已还原，源目录未被写入）。"
    )


def check_hardlink_support(
    source: str, target: str, *, probe: bool = True
) -> tuple[bool | None, str]:
    """**级联判据**：``(ok, message)``，``ok`` 是 ``bool | None``。

    顺序（1 → 4，一旦有结论就返回）：
    1. ``probe=True`` 时先跑**实证探针**（主判据），拿到 ``bool`` 就是结论，``message`` 注明「已实证」；
    2. 探针**跑不了**（``None``）⇒ **挂载 ID**：不同 ⇒ ``False``；相同 ⇒ ``True`` + 注明「未实证」；
    3. 挂载 ID 也不可用 ⇒ 回退 ``st_dev``：相同 ⇒ ``True`` + 注明「未实证，可能假绿」；不同 ⇒ ``False``；
    4. 全都不可用 ⇒ ``None``。

    ``ok is None`` 表示**无法判定**：调用方（执行器 / 预检）应记为**警告**并放行，
    真正的结论由实际 ``os.link`` 给出 —— 绝不因为"判不了"就硬拦整个任务。
    """
    src_ancestor = existing_ancestor(source)
    dst_ancestor = existing_ancestor(target)
    if not os.path.isdir(dst_ancestor):
        return False, f"目标路径 {dst_ancestor} 不是目录，无法创建硬链接。"
    if not os.path.isdir(src_ancestor):
        return False, f"源路径 {src_ancestor} 不是目录，无法读取。"

    if probe:
        ok, note = probe_cross_filesystem(src_ancestor, dst_ancestor)
        if ok is not None:
            return ok, note

    same_mount, mount_note = check_same_mount(src_ancestor, dst_ancestor)
    if same_mount is False:
        return False, mount_note
    if same_mount is True:
        return True, f"{mount_note}（未实证，仅据挂载 ID；该判据必要不充分。）"

    src_dev = _st_dev(src_ancestor)
    dst_dev = _st_dev(dst_ancestor)
    if src_dev is None or dst_dev is None:
        missing = src_ancestor if src_dev is None else dst_ancestor
        return None, f"无法判定：{missing} 不存在或不可访问，且无法实证、也读不到挂载 ID。"
    if src_dev == dst_dev:
        return True, (
            f"未实证，仅据 st_dev（源 {src_ancestor} 与目标 {dst_ancestor} 同为 st_dev={src_dev}，"
            f"可能假绿：同一设备号但不同挂载子树时 os.link 仍可能 EXDEV）。{mount_note}"
        )
    return False, (
        f"跨文件系统错误：源 {src_ancestor} (st_dev={src_dev}) 与目标根 {dst_ancestor} "
        f"(st_dev={dst_dev}) 不在同一文件系统，os.link 会返回 EXDEV。"
        f"本应用不会回退为复制文件。请把两个挂载点指向同一宿主文件系统。"
    )


def _walk_source_files(root: str) -> Iterator[str]:
    """深度优先列出 ``root`` 下的普通文件（**不跟随符号链接**，逐项容错）。

    只用于源目录完整性快照；同一层内按名字排序以获得稳定的遍历顺序。
    """
    stack = [normalize(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as listing:
                entries = sorted(listing, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(os.path.join(current, entry.name))
                elif entry.is_file(follow_symlinks=False):
                    yield os.path.join(current, entry.name)
            except OSError:
                continue


def snapshot_source_tree(root: str) -> dict[str, tuple[int, int, int]]:
    """快照源树下的普通文件：``{相对路径: (st_size, st_ino, st_mtime_ns)}``。

    ``/downloads:ro`` 那层**结构性保证**退场之后，用这份快照在任务前后比对，
    把"绝不改动 qBittorrent 原文件"从结构性假设变成**可验证的保证**。

    **刻意不纳入 ``nlink`` 与 ``ctime``**：建硬链接本身就会让源文件的 ``nlink`` +1
    （Linux 上 ``ctime`` 也随之变），那是硬链接的固有语义、不是"源被改动"，
    纳入比较会造成每次任务都被误报。
    """
    start = normalize(root)
    snapshot: dict[str, tuple[int, int, int]] = {}
    for path in _walk_source_files(start):
        try:
            info = os.stat(path)
        except OSError:
            continue
        relative = os.path.relpath(path, start).replace(os.sep, "/")
        snapshot[relative] = (info.st_size, info.st_ino, info.st_mtime_ns)
    return snapshot


def verify_source_tree(
    root: str, before: Mapping[str, tuple[int, int, int]]
) -> tuple[bool, list[str]]:
    """复核源树：返回 ``(ok, changed)``，``changed`` 是相对路径列表（新增/消失/被改动）。

    只做比较，**不修任何东西**；``ok=False`` 时调用方应记**警告**并列出路径
    （不得因此改 ``status`` 的既有语义）。
    """
    after = snapshot_source_tree(root)
    changed = sorted(
        relative
        for relative in set(before) | set(after)
        if before.get(relative) != after.get(relative)
    )
    return (not changed), changed
