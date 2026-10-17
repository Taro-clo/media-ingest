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
import uuid
from itertools import zip_longest
from typing import Iterator, Mapping

from .config import settings

#: 探针临时文件前缀：启动清扫与测试都依赖它，改名前先看 ``cleanup_probe_leftovers`` 的调用点。
PROBE_PREFIX = ".mlm_probe_"
PROBE_SUFFIX = ".link"

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


def assert_allowed(path: str, roots: list[str], kind: str) -> None:
    """断言 ``path`` 落在 ``roots``（白名单根，容器路径）之内，否则抛 ``ValueError``。

    ``kind`` 只用于中文错误文案（“源目录”/“目标目录”）。"""
    candidate = normalize(path)
    for root in roots or []:
        if is_inside(candidate, root):
            return
    allowed = "、".join(normalize(r) for r in (roots or [])) or "（未配置）"
    raise ValueError(f"{kind} {candidate} 不在允许的根路径内，当前允许：{allowed}。")


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


def cleanup_probe_leftovers(directory: str) -> int:
    """清扫 ``directory`` 里遗留的 ``.mlm_probe_*`` 临时文件（**不递归**）。

    探针正常路径会自己删掉临时链接；但进程被 SIGKILL / 容器被强杀时可能残留。
    启动时对目标根扫一遍，探针前后也各扫一遍（自己那一个）。返回删除条数。
    """
    removed = 0
    try:
        with os.scandir(normalize(directory)) as listing:
            victims = [e.path for e in listing if e.name.startswith(PROBE_PREFIX)]
    except OSError:
        return 0
    for victim in victims:
        try:
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
