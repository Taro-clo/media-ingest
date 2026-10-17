"""v2 处理器测试的共享工具（fixture 树、断言辅助、引擎调用封装）。

这里提供的是**原生引擎/原生实现**侧的公共工具（PROCESSORS.md §4.1）。
"""

from __future__ import annotations

import inspect
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import mlm_helpers as H

#: 入口函数名沿用旧称（契约要求保留）
LEGACY_FUNCTION = "rename_bangumi_episodes"

#: 差分 fixture —— 覆盖 BASELINE §2.6 命名表、EPISODE_PATTERN 八分支 + ALEPISODE 尾号、
#: 多段 .ass 后缀、Season 0、无集数号、大小写敏感白名单、嵌套目录、空作用域目录。
#: 命名结果**两两不撞名**（同目录内改名后仍唯一），保证比较不受 os.rename 覆盖语义影响。
BANGUMI_TREE = {
    "Show A/Season 1/[Lilith-Raws] Show - 01.mkv": 10,   # 分支 8：' - NN'
    "Show A/Season 1/Show.S01E01.chs.ass": 20,          # 分支 5/6 + 多段 .ass 后缀
    "Show A/Season 1/Show.01.ass": 30,                  # 无集数号 → ENone.01.ass
    "Show A/Season 1/Show 12.mp4": 40,                  # ALEPISODE 尾号
    "Show A/Season 1/Show.EP03.mkv": 50,                # 分支 4：EP
    "Show A/Season 1/Show.Episode04.mkv": 60,           # 分支 3：Episode
    "Show A/Season 1/Show.第05集.mkv": 70,              # 分支 1：第 N 集
    "Show A/Season 1/Show [09] x.mkv": 80,              # 分支 2：[NN]
    "Show A/Season 1/Show.e06.mkv": 90,                 # 分支 5：小写 e
    "Show A/Season 1/Show.E07.mkv": 100,                # 分支 6
    "Show A/Season 1/Show.#08.mkv": 110,                # 分支 7：#
    "Show A/Season 1/Show - 13.mkv": 120,               # 分支 8（与 09 错开，避免撞名）
    "Show A/Season 1/Show 5.mp4": 130,                  # ALEPISODE
    "Show A/Season 1/UPPER.MKV": 140,                   # 白名单大小写敏感 ⇒ 不动
    "Show A/Season 1/notes.txt": 150,                   # 非白名单 ⇒ 不动
    "Show A/Season 1/Subs/extra.srt": 160,              # 嵌套目录 ⇒ 从不处理
    "Show A/Season 0/Show.01.mkv": 170,                 # season "00" ⇒ 跳过
    "Show A/Season 1 (2018)/Show.02.mkv": 180,          # fullmatch 不中 ⇒ 跳过
    "Show B/Season 2/Show.S02E10.mkv": 200,
    "Show C/Season 1/CD/nested.mkv": 210,               # 嵌套子目录里的文件照旧不动
    "Show C/Season 1/Show.10.mkv": 220,
    "Show D/Season 3": None,                            # 空作用域目录
}

#: Movie 语义：title 级作用域、同后缀只留体积最大者。
#: 这里不放任何"可能被默认 reject_dirs 命中"的标题名——那一条单独构造（见 test_processor_engine）。
MOVIE_TREE = {
    "Movie One/a.mkv": 10,
    "Movie One/b.mkv": 30,
    "Movie One/c.mp4": 5,
    "Movie One/d.srt": 99,
    "Movie One/e.MKV": 77,
    "Movie One/f.ass": 1,
    "Movie One/g.ass": 4,
    "Movie One/notes.txt": 3,
    "Movie One/Extras/huge.mkv": 999,          # 嵌套目录不参与（file_scope=title）
    "Movie Two/x.mkv": 42,
}

TREES = {"bangumi": BANGUMI_TREE, "movie": MOVIE_TREE}


# --------------------------------------------------------------------------- 树 / 指纹


def write_tree(root, tree) -> None:
    H.build_tree(root, dict(tree))


def tree_digest(root) -> dict[str, str]:
    """``{相对路径: sha256}``，只含文件（目录不进比较，改名不改内容）。"""
    root = Path(root)
    digest: dict[str, str] = {}
    for rel in sorted(H.tree_relpaths(root)):
        target = root / rel
        if target.is_file() and not target.is_symlink():
            digest[rel] = H.sha256_file(target)
    return digest


def file_names(root) -> set[str]:
    root = Path(root)
    return {rel for rel in H.tree_relpaths(root) if (root / rel).is_file()}


def renamed_lines(stdout: str) -> set[str]:
    return {
        line.strip()[len("Renamed: "):]
        for line in stdout.splitlines()
        if line.strip().startswith("Renamed: ")
    }


def error_lines(stdout: str) -> set[str]:
    return {line.strip() for line in stdout.splitlines() if line.strip().startswith("Error renaming ")}


def error_lines_normalized(stdout: str, root) -> set[str]:
    """`Error renaming` 行里带 `os.rename` 抛出的**绝对路径**（tmp 目录不同必然不等），
    因此比较前把两侧各自的根目录替换成 `<ROOT>`；错误类型与文件名仍逐字比较。"""
    root_text = H.norm(root).rstrip("/")
    normalized = set()
    for line in error_lines(stdout):
        normalized.add(H.norm(line).replace(root_text, "<ROOT>"))
    return normalized


def log_sizes(log_file) -> dict[str, int]:
    """把 json_cache 归一成 ``{标题文件夹: size}``（timestamp/works_dir 不参与比较）。"""
    path = Path(log_file)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, int] = {}
    for folder, entry in payload.items():
        if isinstance(entry, dict) and "size" in entry:
            result[folder] = int(entry["size"])
    return result


def log_keys(log_file) -> set[str]:
    path = Path(log_file)
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).keys())



# --------------------------------------------------------------------------- 引擎侧


def engine_entry(engine_module):
    """取引擎入口（契约名 ``run_rename``；其余名字只为容错，取不到就报错）。"""
    for name in ("run_rename", "run", "rename_library"):
        candidate = getattr(engine_module, name, None)
        if callable(candidate):
            return candidate, name
    raise AssertionError(
        "规则引擎未暴露 run_rename(...)（PROCESSORS.md §4 约定的入口）"
    )


def call_engine(
    engine_module,
    spec,
    root,
    *,
    log_file=None,
    folder=None,
    force=False,
    now=None,
    works_dir=None,
    require_output=True,
    **extra,
):
    """按签名自适应地调用引擎入口，返回 (outcome, captured_stdout)。

    `require_output=False` 用于**预期什么都不打印**的调用（非法规格被拒、`folder` 不存在、
    `hardlink_only` 不做重命名）——那些路径只写 `outcome.warnings`/`exception`。
    """
    entry, _name = engine_entry(engine_module)
    params = inspect.signature(entry).parameters
    candidates = {
        "spec": spec if "spec" in params else None,
        "processor": spec,
        "root": str(root),
        "root_dir": str(root),
        "library_root": str(root),
        "log_file": None if log_file is None else str(log_file),
        "folder": folder,
        "force": force,
    }
    if now is not None:
        candidates["now"] = now
    if works_dir is not None:
        candidates["works_dir"] = str(works_dir)
    candidates.update(extra)
    kwargs = {}
    for key, value in candidates.items():
        if key in params and value is not None:
            kwargs[key] = value
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        outcome = entry(**kwargs)
    captured = buffer.getvalue()
    stdout = getattr(outcome, "stdout", None)
    if isinstance(stdout, list):
        stdout = "\n".join(str(line) for line in stdout)
    if not stdout:
        stdout = captured
    text = stdout if isinstance(stdout, str) else str(stdout)
    if require_output:
        assert text.strip(), "引擎既没有打印 Renamed: 也没有返回 outcome.stdout（PROCESSORS.md §4 第 9 步）"
    return outcome, text


def outcome_renamed(outcome) -> set[str]:
    """把 outcome.renamed 归一成 ``{"old -> new"}``（old/new 都是 basename）。"""
    renamed = getattr(outcome, "renamed", None)
    if renamed is None and isinstance(outcome, dict):
        renamed = outcome.get("renamed")
    result: set[str] = set()
    for item in renamed or []:
        if isinstance(item, dict):
            result.add(f"{item.get('old')} -> {item.get('new')}")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            result.add(f"{item[-2]} -> {item[-1]}")
    return result


def outcome_to_dict(outcome) -> dict:
    if hasattr(outcome, "to_dict"):
        return outcome.to_dict()
    if isinstance(outcome, dict):
        return outcome
    return dict(getattr(outcome, "__dict__", {}))


def fresh_copy(base: Path, name: str) -> Path:
    """为一次执行准备独立副本目录。"""
    target = base / name
    target.mkdir(parents=True, exist_ok=True)
    return target


# --------------------------------------------------------------------------- 额外标题


def add_rejected_title(tree: dict, title: str) -> dict:
    """在 fixture 里加一个额外的标题文件夹（含一个可改名文件）。

    文件名刻意用 `Show [01] a.mkv`（EPISODE_PATTERN 的 `[NN]` 分支命中 → E01），
    而不是 `Show.01.mkv`（那是**没有**任何分支命中的写法，会回落成字面量 None）。
    """
    extended = dict(tree)
    extended[f"{title}/Season 1/Show [01] a.mkv"] = 300
    return extended


def movie_title_file(title: str) -> str:
    return f"{title}/clip.mkv"


def add_movie_title(tree: dict, title: str) -> dict:
    extended = dict(tree)
    extended[movie_title_file(title)] = 300
    return extended


def is_running_on_windows() -> bool:
    return os.name == "nt"
