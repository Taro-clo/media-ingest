"""tests/ 共用的纯 stdlib 辅助函数（无 pytest / 无 app 依赖）。

这里的东西被 tests/** 与 docs/contracts/COMPATIBILITY.md 引用；刻意保持零依赖，
这样即使 ``app`` 包尚不可导入也能量化"源目录未被改动"这类不变量。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import time
from pathlib import Path

# --------------------------------------------------------------------------- 基础


#: 应用的**规范形态**是容器路径：``HOST_PATH_MAP`` 把容器前缀映射到宿主目录，
#: 库的 ``source_dir``/``target_dir`` 在入库前会被 ``paths.to_container`` 归一成容器形态
#: （见 ``app/routers/libraries.py``）。API 测试用这两个常量，不要用 tmp 宿主路径。
CONTAINER_SOURCE_ROOT = "/downloads"
CONTAINER_LIBRARY_ROOT = "/library"


def norm(path) -> str:
    """把路径归一成与 ``app.paths.normalize`` **逐字一致**的字符串。

    绝对化 + 统一分隔符，**必须保留大小写**：它的返回值会被拿去和"由应用写进日志文件
    的 Source 值"做字符串相等/子串比较，而 ``paths.normalize`` 不改变大小写。
    （这里**不能**用 ``os.path.normcase``：它会在 Windows 上把整个路径小写化，
    于是 ``H.norm(tmp/downloads)`` 得到 ``c:/users/...``、与日志里的 ``C:/Users/...``
    对不上，凭空产生"本地化没生效"的假失败。）
    """
    return os.path.abspath(str(path)).replace("\\", "/")


def same_path(left, right) -> bool:
    """路径等价判断（Windows 大小写无关），用于"这两个文件是同一个"这类断言。"""
    return os.path.normcase(norm(left)) == os.path.normcase(norm(right))


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_bytes(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def read_text(path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def read_json(path):
    return json.loads(read_text(path))


def write_json(path, payload) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=4)


# --------------------------------------------------------------------------- 目录树


def build_tree(root, spec: dict) -> None:
    """按 ``{相对路径: None|bytes|int}`` 造目录树。

    ``None`` → 建目录；``int`` → 写入该字节数的 ``b"x"``；``bytes/str`` → 原样写入。
    """
    root = Path(root)
    for rel, payload in spec.items():
        target = root / rel
        if payload is None:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, int):
            data = b"x" * payload
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        with open(target, "wb") as handle:
            handle.write(data)


def stat_signature(path) -> tuple:
    """(kind, size, mtime_ns, ino, mode) —— 目录/文件/符号链接通用。

    **刻意不含 st_nlink**：创建硬链接会让源文件的 nlink +1，这是硬链接的*预期*
    副作用，不是"源目录被改动"。nlink 的校验放在 ``assert_real_hardlink`` 里。
    """
    st = os.lstat(path)
    if os.path.islink(path):
        kind = "link"
    elif os.path.isdir(path):
        kind = "dir"
    else:
        kind = "file"
    return (kind, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode)


def tree_snapshot(root) -> dict:
    """完整快照：相对路径 -> (kind, size, mtime_ns, ino, mode)。

    含目录自身（``"."``）。用于证明"源目录一个字节、一个 mtime 都没变"。
    """
    root = str(root)
    snapshot: dict[str, tuple] = {}
    if not os.path.exists(root):
        return snapshot
    snapshot["."] = stat_signature(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            snapshot[rel] = stat_signature(full)
    return snapshot


def diff_snapshot(before: dict, after: dict) -> list[str]:
    problems: list[str] = []
    for rel in sorted(set(before) | set(after)):
        if rel not in before:
            problems.append(f"新增 {rel!r} -> {after[rel]!r}")
        elif rel not in after:
            problems.append(f"消失 {rel!r} <- {before[rel]!r}")
        elif before[rel] != after[rel]:
            problems.append(f"变化 {rel!r}: {before[rel]!r} -> {after[rel]!r}")
    return problems


def assert_tree_unchanged(root, before: dict, label: str = "源目录") -> None:
    after = tree_snapshot(root)
    problems = diff_snapshot(before, after)
    assert not problems, (
        f"{label} {root} 在操作后发生变化（size/mtime_ns/ino/mode 全部参与比对）：\n  "
        + "\n  ".join(problems)
    )


@contextlib.contextmanager
def source_guard(*roots):
    """在 with 块内执行操作，退出时断言这些目录树（含 mtime_ns/ino）逐项未变。"""
    snapshot_pairs = []
    for root in roots:
        if root is None:
            continue
        snapshot_pairs.append((str(root), tree_snapshot(root)))
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        if not failed:
            for root, before in snapshot_pairs:
                assert_tree_unchanged(root, before)


# --------------------------------------------------------------------------- 硬链接断言


def assert_real_hardlink(source, target) -> None:
    """核心断言：同 (st_dev, st_ino)、st_nlink == 2、目标不是符号链接、源仍在。"""
    source = str(source)
    target = str(target)
    assert os.path.exists(source), f"源文件消失: {source}"
    assert os.path.exists(target), f"目标文件不存在: {target}"
    assert not os.path.islink(target), f"目标是符号链接而非硬链接: {target}"
    src_stat = os.stat(source)
    dst_stat = os.stat(target)
    assert (src_stat.st_dev, src_stat.st_ino) == (dst_stat.st_dev, dst_stat.st_ino), (
        f"不是硬链接（inode 不同）: 源 {source} -> "
        f"(dev={src_stat.st_dev}, ino={src_stat.st_ino})，目标 {target} -> "
        f"(dev={dst_stat.st_dev}, ino={dst_stat.st_ino})"
    )
    assert dst_stat.st_nlink == 2, (
        f"st_nlink 应为 2（源 + 一个硬链接），实际 {dst_stat.st_nlink}: {target}"
    )


def relative_link_pairs(source_dir, target_dir) -> list[tuple[str, str]]:
    """目标树里每个文件对应的 (源路径, 目标路径)。"""
    pairs = []
    for dirpath, dirnames, filenames in os.walk(target_dir):
        dirnames.sort()
        for name in sorted(filenames):
            target = os.path.join(dirpath, name)
            rel = os.path.relpath(target, target_dir)
            pairs.append((os.path.join(source_dir, rel), target))
    return pairs


def tree_relpaths(root) -> set[str]:
    result = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            result.add(os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/"))
    return result


# --------------------------------------------------------------------------- 其他


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.05, message: str = ""):
    """轮询直到 predicate 为真；超时则返回最后一次结果（由调用方断言）。"""
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    if not result and message:
        raise AssertionError(message)
    return result


ASGI_CANDIDATES = (
    ("app.main", "app"),
    ("app.api", "app"),
    ("app.server", "app"),
    ("app.web", "app"),
    ("app.main", "application"),
)


def discover_asgi_app():
    """按候选清单找 FastAPI/ASGI 实例；找不到返回 ``(None, None)``。"""
    for modname, attr in ASGI_CANDIDATES:
        try:
            module = importlib.import_module(modname)
        except ImportError:
            continue
        except Exception:  # noqa: BLE001 - 模块存在但导入失败：交给测试报告
            continue
        candidate = getattr(module, attr, None)
        if candidate is not None:
            return candidate, f"{modname}:{attr}"
    return None, None


def find_other_device_dir(base_dir) -> str | None:
    """找一个与 ``base_dir`` 不同 ``st_dev`` 的已存在目录；找不到返回 ``None``。

    用于制造真实的 EXDEV 场景（跨文件系统）。Windows 上是另一个盘符，
    POSIX 上是 ``/dev/shm`` / ``/run`` 之类独立 tmpfs。
    """
    try:
        base_dev = os.stat(base_dir).st_dev
    except OSError:
        return None
    candidates: list[str] = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                candidates.append(root)
    else:
        candidates.extend(["/dev/shm", "/run", "/tmp", "/var/tmp"])
    for candidate in candidates:
        try:
            if os.path.isdir(candidate) and os.stat(candidate).st_dev != base_dev:
                return candidate
        except OSError:
            continue
    return None
