"""v2 规则引擎的分层契约测试（PROCESSORS.md §3.1 / §3.2 / §4）。

三层规则各自单独钉：
* `naming.expressions` —— **按书写顺序**求值、可引用前面算出的名字、沙箱里没有
  `__import__`/`open`/`eval`、出错必须给中文告警且不炸整轮；
* `naming.segments` —— `param` / `literal` / `pad` 三键、`pad` 只补零不截断、
  渲染出空名必须跳过并给中文原因；
* `traverse.reject_dirs` —— 在标题解析**之前**拦截（标题层级与子目录层级都生效）；
  `accept_dirs` 用 `fullmatch`（整名匹配语义），空数组 ⇒ 任何子目录都接受。

外加引擎的运行语义：日志条目形状（含 `works_dir`）、`Skipping folder:`、`force`、
`folder` 定向、`now` 注入、非法规格拒绝、`hardlink_only` 不做重命名、不碰 root logger。

**没解析出集数**（`episode_patterns` 全部不命中 **且** 没配 `episode_fallback`）
而命名又引用了集数时 ⇒ **保留原文件名、不改名** + 计入 `outcome.kept_unresolved_episode`
+ 打一行 `Kept (no episode):`（旧行为是渲染成**字符串** `None` ⇒ `… S01ENone.mkv`，同一作品的
多个压制版本会撞成同一个目标名互相覆盖）。三条边界用例在文件末尾。
"""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import re
from pathlib import Path

import pytest

import processor_helpers as P

STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


# --------------------------------------------------------------------------- 规格构造


def _base_spec(proc_defaults) -> dict:
    """以一整套合法的内置规格为底（bangumi + legacy = 最朴素的旧口径逐字语义），再收窄。"""
    return copy.deepcopy(proc_defaults.preset_spec("bangumi", "legacy"))


def _spec_with(proc_defaults, **sections) -> dict:
    spec = _base_spec(proc_defaults)
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(spec.get(key), dict):
            spec[key] = {**spec[key], **value}
        else:
            spec[key] = value
    return spec


def _run(proc_engine, spec, root, tmp_path, name="engine", **kwargs):
    log_file = tmp_path / f"{name}.log"
    outcome, stdout = P.call_engine(proc_engine, spec, root, log_file=log_file, **kwargs)
    return outcome, stdout, log_file


def _errors_of(outcome) -> list[str]:
    return [str(item.get("error", "")) for item in (getattr(outcome, "errors", None) or [])]


def _warnings_of(outcome) -> list[str]:
    return [str(item) for item in (getattr(outcome, "warnings", None) or [])]


# --------------------------------------------------------------------------- expressions


def test_expressions_are_evaluated_in_written_order(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        naming={
            "expressions": {"first": "'AB'", "second": "first + 'C'"},
            "segments": [{"param": "second"}, {"param": "ext"}],
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"ep01.mkv -> ABC.mkv"}, "后写的表达式必须能引用先写的"


def test_expression_forward_reference_reports_a_chinese_warning(proc_defaults, proc_engine, tmp_path):
    """书写顺序求值 ⇒ 前向引用必然失败，必须给中文告警且不中断整轮。"""
    spec = _spec_with(
        proc_defaults,
        naming={
            "expressions": {"a": "b", "b": "'ok'"},
            "segments": [{"param": "title"}, {"literal": "-"}, {"param": "a"}, {"literal": "-"}, {"param": "b"}, {"param": "ext"}],
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    warnings = _warnings_of(outcome)
    assert any("表达式 a 求值失败" in warning for warning in warnings), warnings
    assert any(re.search(r"[\u4e00-\u9fff]", warning) for warning in warnings)
    assert P.renamed_lines(stdout) == {"ep01.mkv -> Show A--ok.mkv"}


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').getcwd()",
        "open('pwned.txt', 'w')",
        "eval('1 + 1')",
        "getattr(os, 'system')('echo hi')",
    ],
)
def test_expressions_are_evaluated_in_a_restricted_sandbox(proc_defaults, proc_engine, tmp_path, expression):
    spec = _spec_with(
        proc_defaults,
        naming={"expressions": {"probe": expression}, "segments": [{"param": "stem"}, {"literal": "-x"}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert any("表达式 probe 求值失败" in warning for warning in _warnings_of(outcome)), _warnings_of(outcome)
    assert not (tmp_path / "pwned.txt").exists()
    assert P.renamed_lines(stdout) == {"ep01.mkv -> ep01-x.mkv"}


def test_expressions_can_use_the_documented_helpers_and_context_names(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        naming={
            "expressions": {
                "next_ep": "zfill(int(episode_raw) + 1, 2)",
                "stem_len": "str(len(stem))",
            },
            "segments": [
                {"param": "title"},
                {"literal": " E"},
                {"param": "next_ep"},
                {"literal": "-"},
                {"param": "extname"},
                {"literal": "-"},
                {"param": "stem_len"},
                {"param": "ext"},
            ],
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show - 03.mkv": 5})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"Show - 03.mkv -> Show A E04-mkv-9.mkv"}


def test_works_dir_and_rel_dir_are_available_to_the_naming_layer(proc_defaults, proc_engine, tmp_path):
    """`works_dir` / `rel_dir` / `season_raw` 必须出现在命名上下文里。

    `works_dir` 是绝对路径（含 `/`），直接当段会拼出非法文件名，所以经表达式取它的 basename。
    """
    spec = _spec_with(
        proc_defaults,
        naming={
            "expressions": {"works_tail": "basename(works_dir)"},
            "segments": [
                {"param": "title"},
                {"literal": "~"},
                {"param": "rel_dir"},
                {"literal": "~"},
                {"param": "season_raw"},
                {"literal": "~"},
                {"param": "works_tail"},
                {"param": "ext"},
            ],
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path, works_dir="/downloads/Bangumi")

    # 分隔符用 `~` 而不是 `|`：Windows 文件名不允许 `|`（WinError 123），那是平台约束不是引擎行为
    assert P.renamed_lines(stdout) == {"ep01.mkv -> Show A~Season 1~1~Bangumi.mkv"}


# --------------------------------------------------------------------------- segments


def test_segments_support_param_literal_and_pad(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        naming={
            "segments": [
                {"literal": "PRE-"},
                {"param": "title"},
                {"literal": "-S"},
                {"param": "season", "pad": 3},
                {"literal": "E"},
                {"param": "episode", "pad": 2},
                {"param": "ext"},
            ]
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show [03] x.mkv": 5})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"Show [03] x.mkv -> PRE-Show A-S001E03.mkv"}


def test_pad_only_adds_zeros_and_never_truncates(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        naming={"segments": [{"param": "title"}, {"literal": "-"}, {"param": "season", "pad": 4}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 12/ep01.mkv": 5})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"ep01.mkv -> Show A-0012.mkv"}


def test_segments_rendering_an_empty_name_skip_the_file_with_a_chinese_reason(proc_defaults, proc_engine, tmp_path):
    """段序列全空 ⇒ 绝不能把文件改成空名（PROCESSORS.md §3.1「留空」的唯一安全边界）。"""
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "totally_unknown_param"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == set()
    assert (root / "Show A/Season 1/ep01.mkv").is_file()
    assert any("空文件名" in warning for warning in _warnings_of(outcome)), _warnings_of(outcome)


def test_unknown_segment_param_is_still_dropped_but_now_recorded(proc_defaults, proc_engine, tmp_path):
    """未知 `param` 依旧被留空（**行为不变**），但必须留痕。

    运行期**不**因为这个名字拒绝整份规格 —— 库里存下的既有配置、手改过的 YAML 都要能照旧跑完；
    代价是引擎要把"少了一段"报成可计数的事实：stdout 一行 + `outcome.unknown_params` 明细 +
    收尾汇总告警。保存期由 ``spec.validate_spec`` 拦住新写的错名字（strict_names）。
    """
    spec = _spec_with(
        proc_defaults,
        naming={"segments": [{"literal": "OK-"}, {"param": "no_such_name"}, {"literal": "-TAIL"}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"ep01.mkv -> OK--TAIL.mkv"}, "少一段、其余照拼（行为不变）"

    assert len(outcome.unknown_params) == 1, outcome.unknown_params
    entry = outcome.unknown_params[0]
    assert entry["params"] == ["no_such_name"]
    assert entry["file"] == "ep01.mkv"
    assert entry["label"].endswith("Show A/Season 1/ep01.mkv"), entry["label"]

    assert any("Unknown param:" in line and "no_such_name" in line for line in outcome.stdout), outcome.stdout
    warnings = _warnings_of(outcome)
    assert any("不存在的变量" in item and "no_such_name" in item for item in warnings), warnings
    assert any("samples.unknown_params" in item for item in warnings), warnings


def test_blank_episode_fallback_behaves_like_unset_at_runtime(proc_defaults, proc_engine, tmp_path):
    """库里**已经存着**空串兜底（旧数据 / 手改 YAML）时也不许渲染出 `S01E00.mkv`。

    归一的第一道在 ``spec.normalize_spec``，运行期还有这一道保险（直接改数据库也拦得住）。
    """
    spec = _spec_with(proc_defaults, episode_patterns=[], episode_fallback="")
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == set(), "空串 = 没配兜底 ⇒ 保留原文件名、不改名"
    assert (root / "Show A/Season 1/ep01.mkv").is_file()
    assert outcome.kept_unresolved_episode, "必须记进「保留原名」名单"
    assert not (root / "Show A/Season 1/Show A S01E00.mkv").exists()


# --------------------------------------------------------------------------- traverse


def test_reject_dirs_block_the_whole_subtree_before_any_parsing(proc_defaults, proc_engine, tmp_path):
    """标题级 reject：命中即整棵子树不动（例：`Scan`/`Scans…` 这类扫图目录）。"""
    spec = _spec_with(proc_defaults, traverse={"reject_dirs": [{"pattern": r"(?i)^scans?( \(vol\.\d+\))?$"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {
        "Scans/Season 1/ep01.mkv": 5,
        "Scan (Vol.1)/Season 1/ep01.mkv": 6,
        "Show B/Season 1/ep01.mkv": 7,
    })

    outcome, stdout, log_file = _run(proc_engine, spec, root, tmp_path)

    assert (root / "Scans/Season 1/ep01.mkv").is_file()
    assert (root / "Scan (Vol.1)/Season 1/ep01.mkv").is_file()
    assert P.renamed_lines(stdout) == {"ep01.mkv -> Show B S01E01.mkv"}
    assert "Scans" not in P.log_keys(log_file)
    assert "Scan (Vol.1)" not in P.log_keys(log_file), "被拒绝的标题不得写进日志"
    assert any("命中拒绝目录规则" in warning for warning in _warnings_of(outcome)), _warnings_of(outcome)


def test_reject_dirs_also_apply_at_the_subdirectory_level(proc_defaults, proc_engine, tmp_path):
    """`file_scope=subdir` 下子目录层级的 reject 也生效（引擎 `_scopes_for`）。

    `accept_dirs` 为空 ⇒ 任何子目录都接受；此时子目录层级的 reject_dirs 才是唯一闸门。
    名字取自实际媒体库里常见的写法（CD/CDs）。
    """
    spec = _spec_with(
        proc_defaults,
        traverse={"accept_dirs": [], "reject_dirs": [{"pattern": r"(?i)^(cd|cds)$"}], "skip_season_zero": False},
        naming={"segments": [{"param": "title"}, {"literal": "-"}, {"param": "stem"}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {
        "Show A/CD/ep01.mkv": 5,
        "Show A/CDs/ep02.mkv": 6,
        "Show A/Season 1/ep01.mkv": 7,
    })

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert (root / "Show A/CD/ep01.mkv").is_file(), "被拒绝的子目录必须整棵不动"
    assert (root / "Show A/CDs/ep02.mkv").is_file(), "`cds` 大小写不敏感（成对出现）"
    assert P.renamed_lines(stdout) == {"ep01.mkv -> Show A-ep01.mkv"}
    assert any("跳过子目录 CD" in warning for warning in _warnings_of(outcome)), _warnings_of(outcome)
    assert any("跳过子目录 CDs" in warning for warning in _warnings_of(outcome)), _warnings_of(outcome)


def test_empty_accept_dirs_accepts_every_subdir_and_does_not_fake_season_zero(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        traverse={"accept_dirs": [], "reject_dirs": [], "skip_season_zero": True},
        naming={"segments": [{"param": "title"}, {"literal": "-"}, {"param": "stem"}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 0/ep01.mkv": 5, "Show A/Whatever/ep02.mkv": 7})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {
        "ep01.mkv -> Show A-ep01.mkv",
        "ep02.mkv -> Show A-ep02.mkv",
    }, "season_raw 为空时不得把它当成 \"00\" 而静默跳过（引擎 :502-506 的防呆）"


def test_accept_dirs_use_fullmatch_not_search(proc_defaults, proc_engine, tmp_path):
    """`accept_dirs` 用 `fullmatch`：`Season 1 (2018)` 不算季目录。"""
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "title"}, {"param": "season", "pad": 2}, {"param": "episode", "pad": 2}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1 (2018)/ep01.mkv": 5, "Show A/Season 1/ep02.mkv": 7})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"ep02.mkv -> Show A0102.mkv"}
    assert (root / "Show A/Season 1 (2018)/ep01.mkv").is_file()


def test_skip_season_zero_is_honoured_when_a_season_group_is_present(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "title"}, {"param": "season", "pad": 2}, {"param": "episode", "pad": 2}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 0/ep01.mkv": 5, "Show A/Season 2/ep02.mkv": 7})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"ep02.mkv -> Show A0202.mkv"}
    assert (root / "Show A/Season 0/ep01.mkv").is_file()
    assert any("季号是 00" in warning for warning in _warnings_of(outcome)), _warnings_of(outcome)


def test_season_group_zero_accepts_every_matching_subdir_and_leaves_season_raw_empty(proc_defaults, proc_engine, tmp_path):
    """用例 B：`season_group: 0` = 「只用这条正则判断接受、不取季号」。

    真实语义（`app/processors/engine.py:216`）：`season_raw` 是**空串**，但 `season` 仍走
    `"".zfill(2)` ⇒ 是字符串 **`"00"`** —— 这与 `accept_dirs: []`（不配接受规则）**逐字相同**，
    两条路径本来就该走同一条分支（本题末的对照断言就是在钉这一点）。
    也正是 `app/processors/engine.py:503` 的 `if matched.group:` 对 `0` 为假才让 `season_raw` 保持空。
    """
    segments = [
        {"param": "title"},
        {"literal": "~raw="},
        {"param": "sraw"},
        {"literal": "~s="},
        {"param": "season"},
        {"literal": "~e="},
        {"param": "episode"},
        {"param": "ext"},
    ]
    expressions = {"sraw": "season_raw"}

    def spec_for(accept_dirs, **traverse_extra):
        return _spec_with(
            proc_defaults,
            traverse={
                "file_scope": "subdir",
                "accept_dirs": accept_dirs,
                "reject_dirs": [],
                "skip_season_zero": True,
                **traverse_extra,
            },
            naming={"segments": segments, "expressions": expressions},
        )

    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/S1/e05.mkv": 5, "Show A/S2/e06.mkv": 7})

    outcome, stdout, _log = _run(proc_engine, spec_for([{"pattern": r"^S(\d+)$", "season_group": 0}]), root, tmp_path)

    # 两个子目录都被接受（同一条正则命中两次）；`~raw=` 后面是空的、`~s=` 后面是 `00`
    assert P.renamed_lines(stdout) == {
        "e05.mkv -> Show A~raw=~s=00~e=05.mkv",
        "e06.mkv -> Show A~raw=~s=00~e=06.mkv",
    }
    assert P.error_lines(stdout) == set()
    assert not _warnings_of(outcome), _warnings_of(outcome)
    assert (root / "Show A" / "S1" / "Show A~raw=~s=00~e=05.mkv").is_file()

    # 对照 1：与「不配接受规则」(`accept_dirs: []`) 逐字节相同 —— 两条路径是同一分支
    empty_root = tmp_path / "engine_empty"
    P.write_tree(empty_root, {"Show A/S1/e05.mkv": 5, "Show A/S2/e06.mkv": 7})
    _outcome, empty_stdout, _log = _run(proc_engine, spec_for([]), empty_root, tmp_path, name="engine_empty")

    assert P.renamed_lines(empty_stdout) == P.renamed_lines(stdout)
    assert P.tree_digest(empty_root) == P.tree_digest(root)

    # 对照 2：把 season_group 改成 1，季号就被真的取出来了（证明上面是"0 = 不取"）
    taken_root = tmp_path / "engine_taken"
    P.write_tree(taken_root, {"Show A/S1/e05.mkv": 5, "Show A/S2/e06.mkv": 7})
    _outcome, taken_stdout, _log = _run(
        proc_engine, spec_for([{"pattern": r"^S(\d+)$", "season_group": 1}]), taken_root, tmp_path, name="engine_taken"
    )

    assert P.renamed_lines(taken_stdout) == {
        "e05.mkv -> Show A~raw=1~s=01~e=05.mkv",
        "e06.mkv -> Show A~raw=2~s=02~e=06.mkv",
    }


def test_title_scope_reads_files_from_the_title_folder_itself(proc_defaults, proc_engine, tmp_path):
    """`file_scope: title` ⇒ 作用域就是标题文件夹、`season` 为空（Movie 语义）。"""
    spec = _spec_with(
        proc_defaults,
        traverse={"file_scope": "title", "accept_dirs": [], "reject_dirs": [], "skip_season_zero": False},
        naming={"segments": [{"param": "title"}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Movie One/b.mkv": 5, "Movie One/Deep/c.mkv": 7})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"b.mkv -> Movie One.mkv"}
    assert (root / "Movie One/Deep/c.mkv").is_file(), "title 作用域不递归"


def test_title_scope_renames_a_directory_that_looks_like_a_video_like_the_original_script(proc_defaults, proc_engine, tmp_path):
    """电影语义：`file_scope: title` 只看后缀、**不做 `isfile` 过滤**。

    所以形如 `Sub.mp4` 的**目录**也会被改名（`app/processors/engine.py:543`）。
    对照 `file_scope: subdir`（番剧语义）要求 `isfile`（`app/processors/engine.py:540`），
    同一个目录在 subdir 作用域下不会被碰。在此钉死，供 `docs/contracts/COMPATIBILITY.md` §3.2 引用。
    """
    naming = {"segments": [{"param": "title"}, {"param": "ext"}]}
    title_spec = _spec_with(
        proc_defaults,
        selection={"per": "file"},
        traverse={"file_scope": "title", "accept_dirs": [], "reject_dirs": [], "skip_season_zero": False},
        naming=naming,
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Movie One/b.mkv": 5})
    (root / "Movie One" / "Sub.mp4").mkdir()

    _outcome, stdout, _log = _run(proc_engine, title_spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"b.mkv -> Movie One.mkv", "Sub.mp4 -> Movie One.mp4"}
    assert (root / "Movie One" / "Movie One.mkv").is_file()
    assert (root / "Movie One" / "Movie One.mp4").is_dir(), "目录也被改名（既有行为）"

    subdir_spec = _spec_with(
        proc_defaults,
        selection={"per": "file"},
        traverse={"file_scope": "subdir", "reject_dirs": [], "skip_season_zero": False},
        naming=naming,
    )
    root2 = tmp_path / "engine2"
    P.write_tree(root2, {"Show A/Season 1/b.mkv": 5})
    (root2 / "Show A" / "Season 1" / "Sub.mp4").mkdir()

    _outcome2, stdout2, _log2 = _run(proc_engine, subdir_spec, root2, tmp_path, name="engine2")

    assert P.renamed_lines(stdout2) == {"b.mkv -> Show A.mkv"}
    assert (root2 / "Show A/Season 1/Sub.mp4").is_dir(), "subdir 作用域要求 isfile，目录被跳过"


def test_subdir_scope_requires_a_direct_child_and_a_whitelisted_lowercase_extension(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        naming={"segments": [{"param": "title"}, {"param": "season", "pad": 2}, {"param": "episode", "pad": 2}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {
            "Show A/Season 1/ep01.mkv": 5,
            "Show A/Season 1/EP02.MKV": 5,
            "Show A/Season 1/ep03.srt": 5,
            "Show A/Season 1/Deep/ep04.mkv": 5,
        },
    )

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"ep01.mkv -> Show A0101.mkv"}
    for untouched in ("EP02.MKV", "ep03.srt", "Deep/ep04.mkv"):
        assert (root / f"Show A/Season 1/{untouched}").is_file()


def test_extensions_are_matched_case_sensitively(proc_defaults, proc_engine, tmp_path):
    case_sensitive = _spec_with(proc_defaults, extensions=[".mkv"], naming={"segments": [{"param": "title"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/a.MKV": 5, "Show A/Season 1/b.mkv": 7})

    _outcome, stdout, _log = _run(proc_engine, case_sensitive, root, tmp_path)

    assert P.renamed_lines(stdout) == {"b.mkv -> Show A.mkv"}
    assert (root / "Show A/Season 1/a.MKV").is_file()


# --------------------------------------------------------------------------- selection


def test_selection_per_extension_keeps_only_the_largest_file_per_suffix(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        traverse={"file_scope": "title", "accept_dirs": [], "reject_dirs": [], "skip_season_zero": False},
        selection={"per": "extension"},
        naming={"segments": [{"param": "title"}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Movie One/small.mkv": 5, "Movie One/big.mkv": 40, "Movie One/mid.ass": 9})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"big.mkv -> Movie One.mkv", "mid.ass -> Movie One.ass"}
    assert (root / "Movie One/small.mkv").is_file()


def test_selection_per_file_processes_every_match(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(
        proc_defaults,
        selection={"per": "file"},
        naming={"segments": [{"param": "title"}, {"param": "episode", "pad": 2}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show [01] a.mkv": 5, "Show A/Season 1/Show [02] b.mkv": 7})

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {
        "Show [01] a.mkv -> Show A01.mkv",
        "Show [02] b.mkv -> Show A02.mkv",
    }


# --------------------------------------------------------------------------- 日志 / 幂等 / force / folder


def test_log_entry_has_size_timestamp_and_works_dir(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "stem"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    _outcome, _stdout, log_file = _run(proc_engine, spec, root, tmp_path, works_dir="/downloads/Bangumi")

    payload = json.loads(log_file.read_text(encoding="utf-8"))
    assert set(payload) == {"Show A"}
    entry = payload["Show A"]
    assert entry["size"] == 5
    assert STAMP_RE.match(entry["timestamp"]), entry["timestamp"]
    assert entry["works_dir"] == "/downloads/Bangumi"


def test_pre_existing_log_entries_with_unknown_keys_are_tolerated(proc_defaults, proc_engine, tmp_path):
    """读日志必须忽略未知键（PROCESSORS.md §4 第 10 步），旧日志没有 works_dir。"""
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "stem"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})
    log_file = tmp_path / "engine.log"
    log_file.write_text(
        json.dumps({"Show A": {"size": 5, "timestamp": "2020-01-01 00:00:00", "legacy_key": "x"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == set()
    assert "Skipping folder: Show A (already processed at 2020-01-01 00:00:00)" in stdout
    payload = json.loads(log_file.read_text(encoding="utf-8"))
    assert payload["Show A"]["legacy_key"] == "x", "跳过时不得改写已有条目"


def test_second_run_reports_the_skip_record(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "stem"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})
    _run(proc_engine, spec, root, tmp_path)

    outcome, _stdout, _log = _run(proc_engine, spec, root, tmp_path)

    skipped = getattr(outcome, "skipped", None) or []
    assert len(skipped) == 1
    assert skipped[0]["folder"] == "Show A"
    assert skipped[0]["reason"] == "already_processed"
    assert STAMP_RE.match(skipped[0]["at"])
    assert (getattr(outcome, "renamed", None) or []) == []


def test_force_only_prunes_the_named_folder(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "stem"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5, "Show B/Season 1/ep02.mkv": 7})
    _run(proc_engine, spec, root, tmp_path)
    log_file = tmp_path / "engine.log"
    before = json.loads(log_file.read_text(encoding="utf-8"))
    assert set(before) == {"Show A", "Show B"}

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path, folder="Show A", force=True)

    payload = json.loads(log_file.read_text(encoding="utf-8"))
    assert set(payload) == {"Show A", "Show B"}, "force 只删指定文件夹的旧条目并重跑"
    assert payload["Show B"]["size"] == before["Show B"]["size"]
    assert (getattr(outcome, "skipped", None) or []) == []
    assert P.renamed_lines(stdout) == {"ep01.mkv -> ep01.mkv"}


def test_folder_scope_touches_only_that_title_and_unknown_folder_is_a_noop(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "title"}, {"param": "episode", "pad": 2}, {"param": "ext"}]})
    root = tmp_path / "engine"
    tree = {"Show A/Season 1/Show [01] a.mkv": 5, "Show B/Season 1/Show [02] b.mkv": 7}
    P.write_tree(root, tree)
    digest_before = P.tree_digest(root)

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path, folder="Show A", name="scoped")

    assert P.renamed_lines(stdout) == {"Show [01] a.mkv -> Show A01.mkv"}
    assert P.file_names(root) == {
        "Show A/Season 1/Show A01.mkv",
        "Show B/Season 1/Show [02] b.mkv",
    }

    missing_outcome, missing_stdout, _log2 = _run(
        proc_engine, spec, root, tmp_path, folder="Nope", name="missing", require_output=False
    )
    assert P.renamed_lines(missing_stdout) == set()
    assert any("指定文件夹 Nope" in warning for warning in _warnings_of(missing_outcome))
    assert P.tree_digest(root) == {
        "Show A/Season 1/Show A01.mkv": digest_before["Show A/Season 1/Show [01] a.mkv"],
        "Show B/Season 1/Show [02] b.mkv": digest_before["Show B/Season 1/Show [02] b.mkv"],
    }


def test_now_is_injected_into_the_stamp_and_the_log(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "stem"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})
    moment = datetime.datetime(2021, 2, 3, 4, 5, 6)

    _outcome, stdout, log_file = _run(proc_engine, spec, root, tmp_path, now=moment)

    assert "Folder processed: Show A, Size: 5 bytes, Timestamp: 2021-02-03 04:05:06" in stdout
    assert json.loads(log_file.read_text(encoding="utf-8"))["Show A"]["timestamp"] == "2021-02-03 04:05:06"


# --------------------------------------------------------------------------- 规格拒绝与副作用


def test_an_invalid_spec_is_refused_without_touching_any_file(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, kind="not_a_kind")
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})
    before = P.tree_digest(root)

    outcome, _stdout, _log = _run(proc_engine, spec, root, tmp_path, require_output=False)

    assert getattr(outcome, "exception", None)
    assert "处理器规格不合法" in " ".join(_warnings_of(outcome))
    assert P.tree_digest(root) == before
    assert (getattr(outcome, "renamed", None) or []) == []


def test_hardlink_only_kind_refuses_to_rename(proc_defaults, proc_engine, tmp_path):
    spec = copy.deepcopy(proc_defaults.builtin_spec("hardlink_only"))
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})
    before = P.tree_digest(root)

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path, require_output=False)

    assert P.renamed_lines(stdout) == set()
    assert any("只建硬链接" in warning for warning in _warnings_of(outcome)), _warnings_of(outcome)
    assert P.tree_digest(root) == before


def test_outcome_to_dict_covers_the_documented_keys(proc_defaults, proc_engine, tmp_path):
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "stem"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})

    outcome, _stdout, _log = _run(proc_engine, spec, root, tmp_path)

    payload = P.outcome_to_dict(outcome)
    assert set(payload) >= {
        "renamed",
        "skipped",
        "errors",
        "folders_scanned",
        "folders_processed",
        "warnings",
        "log_file",
    }
    assert payload["folders_scanned"] == 1
    assert payload["folders_processed"] == 1
    assert payload["renamed"][0]["old"] == "ep01.mkv"
    assert Path(payload["renamed"][0]["new"]).name == "ep01.mkv"
    assert "dir" in payload["renamed"][0]


def test_the_engine_does_not_touch_the_root_logger(proc_defaults, proc_engine, tmp_path):
    """PROCESSORS.md：引擎把 stdout 交给执行器；日志采集不是它的职责（v1 的接管 root logger 已废）。"""
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "stem"}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 5})
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)
    level_before = root_logger.level

    _run(proc_engine, spec, root, tmp_path)

    assert list(root_logger.handlers) == handlers_before
    assert root_logger.level == level_before
    assert not list(tmp_path.glob("*.log.*")), "引擎不得自己造日志文件名"


def test_a_symlinked_file_is_renamed_from_its_own_name(proc_defaults, proc_engine, tmp_path):
    """作用域内的符号链接条目仍按名字处理（`os.path.isfile` 对链接为真）。"""
    spec = _spec_with(proc_defaults, naming={"segments": [{"param": "title"}, {"param": "episode", "pad": 2}, {"param": "ext"}]})
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show [05] real.mkv": 5})
    link = root / "Show A/Season 1/Show [06] link.mkv"
    if os.name == "nt":
        pytest.skip("Windows 上创建符号链接需要额外权限，本用例只在 POSIX 上有意义")
    os.symlink(root / "Show A/Season 1/Show [05] real.mkv", link)

    _outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {
        # 同级的**真实文件**也在作用域内（`file_scope: subdir` 按 `isfile` 判定，
        # 而 `isfile` 对符号链接为真）—— 两条都要在，链接那条是关键断言。
        "Show [05] real.mkv -> Show A05.mkv",
        "Show [06] link.mkv -> Show A06.mkv",
    }
    assert (root / "Show A/Season 1/Show A06.mkv").is_symlink()


# --------------------------------------------------- 未命中集数 ⇒ 保留原文件名


def test_an_unresolved_episode_keeps_the_original_name(proc_defaults, proc_engine, tmp_path):
    """命名引用了集数、但**没解析出集数** ⇒ 保留原文件名、不改名。

    旧行为（早期版本的兜底取值）：`_episode_raw` 在"全部模式都没命中
    **且** `episode_fallback is None`"时返回**字符串** `"None"`，`_file_context` 再无条件
    `str(raw).zfill(2)` ⇒ 文件名里出现 `S01ENone`；同一作品的多个压制版本还会渲染成**同一个
    目标名**，`os.rename` 互相覆盖（真实事故：4 个不同的压制版本全变成 `S01ENone.mp4`，丢了 3 个）。
    新行为：**跳过 `os.rename`**（硬链接阶段已经用原名把它链进目标了 ⇒ 目标侧就是原文件名）
    + 计入 `outcome.kept_unresolved_episode` + 打一行 `Kept (no episode):` + 一条可计数告警。

    为什么这里把 `episode_patterns` 清空、`episode_fallback` 置 None：**只有**"全部模式都没命中
    **且** 没配 fallback"才算未命中。内置 `bangumi` 预设显式配了 `episode_fallback: "None"`
    （那属于"已解析"，由下一条用例钉住），所以未命中路径必须显式构造。
    """
    spec = _spec_with(proc_defaults, episode_patterns=[], episode_fallback=None)
    root = tmp_path / "engine"
    # 两个文件在旧行为下会渲染成**同一个**目标名（`Show A S01ENone.mkv`）⇒ 必然互相覆盖。
    P.write_tree(root, {"Show A/Season 1/Show.01.mkv": 30, "Show A/Season 1/Show.02.mkv": 220})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == set(), "未解析出集数时不得改名"
    assert P.file_names(root) == {"Show A/Season 1/Show.01.mkv", "Show A/Season 1/Show.02.mkv"}
    assert stdout.count("Kept (no episode): ") == 2, "每个保留的文件都要有**可见**的一行"
    assert "Kept (no episode): Show A/Season 1/Show.01.mkv" in stdout
    assert sorted(outcome.kept_unresolved_episode) == [
        "Show A/Season 1/Show.01.mkv",
        "Show A/Season 1/Show.02.mkv",
    ]
    assert not outcome.errors
    warning = " ".join(outcome.warnings)
    assert "2 个文件没解析出集数" in warning and "已保留原文件名、未改名" in warning, warning
    assert "episode_fallback" in warning, "告警必须告诉用户怎么改（补模式或配 fallback）"


def test_the_builtin_bangumi_default_preset_keeps_the_original_name_when_unresolved(
    proc_defaults, proc_engine, tmp_path
):
    """内置 `bangumi` 的**推荐默认** `episode_fallback is None` ⇒ 未命中集数就保留原名。

    这条用例钉住事故现场：`Show.10.mkv` 一个 `episode_patterns` 分支都不命中，旧行为会把它改名成
    `Show A S01ENone.mkv` —— 同一作品的不同压制版本会撞成同一个目标名互相覆盖。
    新的推荐默认**不**执行 `os.rename`（硬链接阶段已经用原名把它链进目标 ⇒ 等于保留原名），
    并把这件事记进 `outcome.kept_unresolved_episode`（可计数、有告警，任务仍算成功）。
    """
    spec = proc_defaults.preset_spec("bangumi", "default")
    assert spec["episode_fallback"] is None, "推荐默认必须留空（保留原名），不许再写字符串 'None'"
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show.10.mkv": 220})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == set(), "默认预设不得再造出 `… S01ENone.mkv`"
    assert P.file_names(root) == {"Show A/Season 1/Show.10.mkv"}, "必须保留原文件名"
    assert outcome.kept_unresolved_episode == ["Show A/Season 1/Show.10.mkv"]
    assert "Kept (no episode): Show A/Season 1/Show.10.mkv" in stdout
    assert not outcome.errors


def test_the_builtin_bangumi_legacy_preset_still_renders_the_string_none(
    proc_defaults, proc_engine, tmp_path
):
    """`legacy` 预设复刻最朴素的旧口径的**字符串** `"None"` ⇒ 未命中集数时照旧渲染 `… S01ENone.mkv`。

    这条用例钉住边界："未命中"的第二个条件是 `episode_fallback is None`；
    `legacy` 显式配置了字符串 `"None"`（最朴素的旧口径的逐字行为，也是
    `preset=legacy` 必须保持旧口径语义的契约），所以它属于"已解析"，照旧把字面量写进文件名
    —— 项目自己的逐字节 golden（`Show A S01ENone.01.ass` /
    `Show C S01ENone.mkv`）钉的正是这一条。
    """
    spec = proc_defaults.preset_spec("bangumi", "legacy")
    assert spec["episode_fallback"] == "None"
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show.10.mkv": 220})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"Show.10.mkv -> Show A S01ENone.mkv"}
    assert outcome.kept_unresolved_episode == []
    assert "Kept (no episode): " not in stdout


def test_the_movie_preset_does_not_skip_when_there_is_no_episode(
    proc_defaults, proc_engine, tmp_path
):
    """movie 不受集数兜底变更影响：它的命名（`{title}.{extname}`）根本不引用集数。

    判定"要不要跳过"的唯一依据是**命名段序列是否引用了集数参数**（`naming_uses_episode`），
    而不是"有没有解析出集数"。movie 的 segments 是 title + "." + extname ⇒ 即使它的
    `episode_patterns` 是空数组、`episode_fallback is None`（永远"未命中"），也照旧改名。
    """
    spec = proc_defaults.preset_spec("movie", "default")
    assert proc_engine.naming_uses_episode(spec["naming"]["segments"]) is False
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {
            "Movie One/clip.mkv": 10,   # 同后缀只留体积最大者 ⇒ 它胜出
            "Movie One/other.mp4": 20,
            "Movie One/notes.txt": 5,   # 不在后缀白名单里 ⇒ 不动
        },
    )

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {
        "clip.mkv -> Movie One.mkv",
        "other.mp4 -> Movie One.mp4",
    }
    assert outcome.kept_unresolved_episode == []
    assert "Kept (no episode): " not in stdout


# --------------------------------------- L4「可见性」 + L2「防丢」
#
# 冲突清单（``name_collisions`` = "目标名被占用"的事实）、加后缀清单
# （``renamed_with_suffix``）与让位清单（``displaced_targets``）。
# 四个契约：① 判定在 ``os.rename`` **之前**做、只读；② **绝不覆盖** —— 体积最大的拿干净名，
# 其余加 `` (N)`` 保留、目标盘上原有的占位文件更小时让位（L2 策略）；
# ③ ``old_path == new_path``（改到自己）**不算冲突**；④ **幂等** —— 已经在 `` (N)`` 位置上的
# 文件重跑时照用，不再往上叠、也不重复记账（见文件末尾的重跑用例）。


def test_a_within_run_collision_keeps_both_files_with_a_suffix(
    proc_defaults, proc_engine, tmp_path
):
    """同一作用域里两个文件渲染出**同一个**目标名 ⇒ 体积大的拿干净名、另一个加 `` (2)``。

    这正是真实事故的那一类（日常 S01E19：48 个文件抢一个名字，最后只剩 1 个）。
    L2 防丢策略钉住：

    * ``name_collisions`` 恰好一条、``kind == "within_run"``、``previous`` / ``current`` 正确；
    * ``renamed_with_suffix`` 恰好一条（beta 拿到 `` (2)``），**两个文件都还在**、内容各归各的；
    * 告警可计数、且明确说明"不覆盖、加后缀保留"（不再出现"仍是覆盖"这种话）。

    构造：``alpha.mkv`` 写入 10 字节的填充内容（比 beta 的 ``second`` 大）⇒ alpha 是正片、赢。
    """
    spec = _spec_with(
        proc_defaults,
        naming={
            "expressions": {"ep": "'5'"},
            "segments": [{"param": "title"}, {"literal": " S01E"},
                         {"param": "ep", "pad": 2}, {"param": "ext"}],
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/alpha.mkv": 10})
    alpha_text = (root / "Show A/Season 1/alpha.mkv").read_text(encoding="utf-8")
    second = root / "Show A/Season 1/beta.mkv"
    second.write_text("second", encoding="utf-8")

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert "alpha.mkv -> Show A S01E05.mkv" in P.renamed_lines(stdout)
    assert "Renamed (suffix): beta.mkv -> Show A S01E05 (2).mkv" in stdout
    assert len(outcome.name_collisions) == 1, outcome.name_collisions
    collision = outcome.name_collisions[0]
    assert collision["kind"] == "within_run", collision
    assert collision["target"] == "Show A S01E05.mkv", collision
    # 目录标签是子目录名（不是完整绝对路径）。
    assert collision["dir"] == "Season 1", collision
    # previous 是**先前占用者的源文件名**（不是 `作品/季/文件` 形式的标签）。
    assert collision["previous"] == "alpha.mkv", collision
    assert collision["current"] == "beta.mkv", collision

    # L2：beta 加后缀保留 —— 一条样本、两个文件都在、内容各归各的，**没有任何覆盖**。
    assert len(outcome.renamed_with_suffix) == 1, outcome.renamed_with_suffix
    suffix = outcome.renamed_with_suffix[0]
    assert suffix["old"] == "beta.mkv", suffix
    assert suffix["clean"] == "Show A S01E05.mkv", suffix
    assert suffix["target"] == "Show A S01E05 (2).mkv", suffix
    assert suffix["reason"] == "within_run", suffix
    assert len(outcome.renamed) == 2, outcome.renamed
    assert not outcome.errors, outcome.errors
    assert not outcome.displaced_targets, outcome.displaced_targets
    assert P.file_names(root) == {
        "Show A/Season 1/Show A S01E05.mkv",
        "Show A/Season 1/Show A S01E05 (2).mkv",
    }
    assert (root / "Show A/Season 1/Show A S01E05.mkv").read_text(encoding="utf-8") == alpha_text
    assert (root / "Show A/Season 1/Show A S01E05 (2).mkv").read_text(encoding="utf-8") == "second"
    # 可计数告警（不是 error）：说明"加了后缀保留、不覆盖"。
    warning = " ".join(_warnings_of(outcome))
    assert "1 个文件改到了" in warning, warning
    assert "后缀保留，不覆盖、不丢文件" in warning, warning
    assert "1 个文件因为目标名被占用而加了后缀保留" in warning, warning


def test_an_existing_target_collision_makes_the_smaller_newcomer_take_a_suffix(
    proc_defaults, proc_engine, tmp_path
):
    """目标路径**本来就存在**且不比新来的小 ⇒ 新来的加 `` (2)``、原有文件原地不动（L2）。

    与上一条的区别：这次目标上那个文件**不是**本次运行改出来的（来源不明——可能是上次任务、
    也可能来自硬链接阶段），所以 ``kind`` 是 ``existing_target``。体积相等（10 字节 vs 10 字节）
    时**由原有文件保名**（"体积更大的算正片"，一样大不构成替换理由）—— 正是"一个都不丢"。

    构造要点：命名段用字面量 ``target.txt``，而 ``extensions`` 只认 ``.mkv`` —— 于是
    ``alpha.mkv`` 是**唯一**候选者，预先摆好的 ``target.txt`` 不会被当成候选去改（否则它的
    "改到自己"会随 ``os.listdir`` 顺序变化，还可能被升级成 ``within_run``，用例就不确定了）。
    """
    spec = _spec_with(
        proc_defaults,
        extensions=[".mkv"],
        naming={"segments": [{"literal": "target.txt"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/alpha.mkv": 10})
    alpha_content = (root / "Show A/Season 1/alpha.mkv").read_text(encoding="utf-8")
    # 目标名先被占着（内容与源不同 ⇒ 覆盖会丢内容，正是 L2 要防的那一类）。
    pre = root / "Show A/Season 1/target.txt"
    pre.write_text("old-target", encoding="utf-8")

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert len(outcome.name_collisions) == 1, outcome.name_collisions
    collision = outcome.name_collisions[0]
    assert collision["kind"] == "existing_target", collision
    assert collision["target"] == "target.txt", collision
    assert collision["current"] == "alpha.mkv", collision
    assert collision["previous"] == "target.txt", collision
    assert alpha_content != "old-target"
    # 新来的没能夺走干净名（体积没更大）⇒ 自己加后缀；原有文件**一个字节都没变**。
    assert P.renamed_lines(stdout) == set()
    assert "Renamed (suffix): alpha.mkv -> target (2).txt" in stdout
    assert len(outcome.renamed) == 1, outcome.renamed
    assert not outcome.errors, outcome.errors
    assert not outcome.displaced_targets, outcome.displaced_targets
    assert len(outcome.renamed_with_suffix) == 1, outcome.renamed_with_suffix
    assert outcome.renamed_with_suffix[0]["reason"] == "existing_target"
    assert pre.read_text(encoding="utf-8") == "old-target"
    assert (root / "Show A/Season 1/target (2).txt").read_text(encoding="utf-8") == alpha_content
    assert P.file_names(root) == {
        "Show A/Season 1/target.txt",
        "Show A/Season 1/target (2).txt",
    }
    assert "1 个文件改到了" in " ".join(_warnings_of(outcome))
    assert "1 个文件因为目标名被占用而加了后缀保留" in " ".join(_warnings_of(outcome))


def test_renaming_a_file_to_its_own_name_is_not_a_collision(
    proc_defaults, proc_engine, tmp_path
):
    """``old_path == new_path``（force 重跑时大量出现）**不算冲突** —— 它没有覆盖任何东西。"""
    spec = _spec_with(proc_defaults, episode_patterns=[])
    root = tmp_path / "engine"
    # 目标名与源文件名逐字相同 ⇒ 引擎渲染出的 new_path 就是 old_path。
    P.write_tree(root, {"Show A/Season 1/target.mkv": 10})

    outcome, _stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert outcome.name_collisions == [], outcome.name_collisions
    assert not any("改到了" in text for text in _warnings_of(outcome)), _warnings_of(outcome)
    # 照旧执行了那次改名（改名到自己）并计入 renamed。
    assert len(outcome.renamed) == 1, outcome.renamed


def test_an_unresolved_episode_is_listed_even_when_the_name_is_kept(
    proc_defaults, proc_engine, tmp_path
):
    """未解析出集数 + 命名**引用**集数 ⇒ 既有"保留原名"行为不变，且进了 ``episode_unresolved``。

    ``episode_unresolved`` 是**所有** ``episode_resolved is False`` 的文件，所以它在这里与
    ``kept_unresolved_episode`` 重合（子集关系），而**不是**替代它。
    """
    spec = _spec_with(proc_defaults, episode_patterns=[], episode_fallback=None)
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show.10.mkv": 220})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == set(), "保留原名：不得改名"
    assert P.file_names(root) == {"Show A/Season 1/Show.10.mkv"}
    assert outcome.kept_unresolved_episode == ["Show A/Season 1/Show.10.mkv"]
    assert outcome.episode_unresolved == ["Show A/Season 1/Show.10.mkv"]
    # 「没保留原名」的那条**追加**告警不该出现（保留原名那条已经解释过了）。
    assert not any("没有" in text and "保留原名" in text for text in _warnings_of(outcome)), (
        _warnings_of(outcome)
    )


def test_an_unresolved_episode_is_listed_even_when_the_name_is_renamed_anyway(
    proc_defaults, proc_engine, tmp_path
):
    """未解析出集数 + 命名**不**引用集数（movie 形状）⇒ 照旧改名，但解析失败**不再静默**。

    这是 L4 里"解析失败清单"的核心价值：``kept_unresolved_episode`` 在这种规格下**永远是空的**
    （压根不走进"保留原名"分支），只有 ``episode_unresolved`` 能把这次解析失败暴露出来。
    """
    spec = _spec_with(
        proc_defaults,
        naming={"segments": [{"param": "title"}, {"literal": "."}, {"param": "extname"}]},
        episode_patterns=[],
        episode_fallback=None,
    )
    assert proc_engine.naming_uses_episode(spec["naming"]["segments"]) is False
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show.10.mkv": 220})

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.renamed_lines(stdout) == {"Show.10.mkv -> Show A.mkv"}
    assert outcome.kept_unresolved_episode == [], "命名不引用集数 ⇒ 不会走保留原名分支"
    assert outcome.episode_unresolved == ["Show A/Season 1/Show.10.mkv"]
    warning = " ".join(_warnings_of(outcome))
    assert "1 个文件没解析出集数" in warning, warning
    assert "保留原名" in warning, warning


def test_a_collision_free_run_produces_no_collision_warning(proc_defaults, proc_engine, tmp_path):
    """没有冲突就**不**产生任何冲突告警，``episode_unresolved`` 也是空的。"""
    spec = _spec_with(
        proc_defaults,
        naming={"segments": [{"param": "stem"}, {"param": "ext"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {"Show A/Season 1/Show [05] a.mkv": 10, "Show A/Season 1/Show [06] b.mkv": 20},
    )

    outcome, _stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert outcome.name_collisions == []
    assert not any("改到了" in text for text in _warnings_of(outcome)), _warnings_of(outcome)
    assert outcome.episode_unresolved == []
    assert len(outcome.renamed) == 2


def test_a_rerun_keeps_the_suffixed_names_without_stacking_or_recounting(
    proc_defaults, proc_engine, tmp_path
):
    """L2 幂等：上一次跑出来的 `` (2)`` / `` (3)`` 重跑时**照用**，既不叠 `` (4)``，也不重复记账。

    构造的就是"第一次跑完之后"的盘面：干净名被体积最大的文件占着，另外两个文件分别坐在
    `` (2)`` 与 `` (3)`` 上（三个文件都是同一批候选 —— 命名表达式把集号固定成 5）。
    重跑时三个文件都已经在各自该坐的位置上 ⇒

    * 文件集合**一个不差、一个不涨**（没有 `` (4)``）；
    * ``renamed_with_suffix`` 与 ``name_collisions`` 都是**空的** —— 没有新冲突、没有新加后缀，
      否则每次重跑都会往任务清单和首页待办看板里灌假事件；
    * 日志里不出现 ``Renamed (suffix):`` 行，体积/内容原样不动。
    """
    spec = _spec_with(
        proc_defaults,
        naming={
            "expressions": {"ep": "'5'"},
            "segments": [{"param": "title"}, {"literal": " S01E"},
                         {"param": "ep", "pad": 2}, {"param": "ext"}],
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/Show A S01E05.mkv": 10})
    season = root / "Show A/Season 1"
    winner_text = (season / "Show A S01E05.mkv").read_text(encoding="utf-8")
    (season / "Show A S01E05 (2).mkv").write_text("second", encoding="utf-8")
    (season / "Show A S01E05 (3).mkv").write_text("x", encoding="utf-8")

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert P.file_names(root) == {
        "Show A/Season 1/Show A S01E05.mkv",
        "Show A/Season 1/Show A S01E05 (2).mkv",
        "Show A/Season 1/Show A S01E05 (3).mkv",
    }
    assert "(4)" not in stdout
    assert "Renamed (suffix):" not in stdout, stdout
    assert outcome.renamed_with_suffix == [], outcome.renamed_with_suffix
    assert outcome.name_collisions == [], outcome.name_collisions
    assert outcome.displaced_targets == [], outcome.displaced_targets
    assert not outcome.errors, outcome.errors
    assert len(outcome.renamed) == 3, outcome.renamed
    assert not any("后缀保留" in text for text in _warnings_of(outcome)), _warnings_of(outcome)
    # 内容与体积一个字节都没动。
    assert (season / "Show A S01E05.mkv").read_text(encoding="utf-8") == winner_text
    assert (season / "Show A S01E05 (2).mkv").read_text(encoding="utf-8") == "second"
    assert (season / "Show A S01E05 (3).mkv").read_text(encoding="utf-8") == "x"


# --------------------------------------------------------------------------- KI-1


def test_a_flat_work_folder_without_a_season_subdir_is_reported_as_unscoped(proc_defaults, proc_engine, tmp_path):
    """KI-1：作品目录里没有可接受的「季目录」⇒ 整部作品跳过改名，但必须留痕。

    子目录模式下 `_scopes_for()` 只看作品目录的**一层子目录**；文件直接躺在作品目录里
    （扁平结构）时它返回空列表 —— 若不留痕就**完全静默**（不改名、不报错、
    也进不了 `kept_unresolved_episode`），用户只会觉得"这一部什么都没发生"。
    现在 `RenameOutcome.unscoped_works` 记下事实，执行器据此落一条看板待办。
    """
    spec = _spec_with(
        proc_defaults,
        traverse={
            "accept_dirs": [{"pattern": r"^Season (\d+)$", "season_group": 1}],
            "reject_dirs": [],
            "skip_season_zero": False,
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {
        "Flat Show/ep01.mkv": 5,
        "Flat Show/ep02.mkv": 6,
        "Good Show/Season 1/ep01.mkv": 7,
    })

    outcome, stdout, log_file = _run(proc_engine, spec, root, tmp_path)

    # 扁平结构的文件一个都不许动（引擎的规矩：作用域算不出来就整部不动）。
    assert (root / "Flat Show/ep01.mkv").is_file()
    assert (root / "Flat Show/ep02.mkv").is_file()
    assert P.renamed_lines(stdout) == {"ep01.mkv -> Good Show S01E01.mkv"}
    assert not outcome.errors, outcome.errors
    # 扁平这部**不算**"未解析出集数"——它连作用域都没有，两者是不同的路径。
    assert outcome.kept_unresolved_episode == []
    assert outcome.episode_unresolved == []

    assert len(outcome.unscoped_works) == 1, outcome.unscoped_works
    entry = outcome.unscoped_works[0]
    assert entry["folder"] == "Flat Show"
    assert Path(entry["dir"]) == root / "Flat Show"
    assert entry["files"] == 2
    assert "ep01.mkv" in entry["samples"] and "ep02.mkv" in entry["samples"]
    assert P.outcome_to_dict(outcome)["unscoped_works"] == outcome.unscoped_works

    warnings = _warnings_of(outcome)
    assert any("没有「季目录」层级" in text for text in warnings), warnings
    assert any("Flat Show" in text for text in warnings), warnings
    # 日志照旧写它（"处理过"的事实不变），只是这次什么都没改名。
    assert "Flat Show" in P.log_keys(log_file)


def test_a_work_folder_whose_files_are_all_extras_is_not_reported_as_unscoped(
    proc_defaults, proc_engine, db_mod, tmp_path
):
    """扁平但"没活可干"的作品不进 KI-1 清单：作品目录为空、或直接文件全被特典判据排除。

    否则看板会被"本来就该原样躺着的特典目录"刷满 —— 判定标准是"**有该处理却没处理**"。
    """
    db_mod.set_setting("extras_patterns", json.dumps(["NCOP", r"cover\.jpg"]))
    spec = _spec_with(
        proc_defaults,
        traverse={
            "accept_dirs": [{"pattern": r"^Season (\d+)$", "season_group": 1}],
            "reject_dirs": [],
            "skip_season_zero": False,
        },
    )
    root = tmp_path / "engine"
    P.write_tree(root, {
        "Only Extras/NCOP.mkv": 5,
        "Only Extras/cover.jpg": 6,
        "Empty Show/.keep": 1,
        "Good Show/Season 1/ep01.mkv": 7,
        # 对照组：特典判据在**作用域内**照样生效（命中 ⇒ 计入 extras_classified、不改名）。
        "Good Show/Season 1/NCOP.mkv": 8,
    })

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert outcome.unscoped_works == [], outcome.unscoped_works
    assert outcome.extras_classified, outcome.extras_classified
    assert (root / "Only Extras/NCOP.mkv").is_file(), "扁平目录里的特典文件也不许动"
    assert P.renamed_lines(stdout) == {"ep01.mkv -> Good Show S01E01.mkv"}
    assert not any("没有「季目录」层级" in text for text in _warnings_of(outcome))
