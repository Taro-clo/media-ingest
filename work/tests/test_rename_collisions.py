"""L2「防丢」的独立回归：同名冲突**绝不覆盖**，一个文件都不丢。

同一次运行里的多个文件、以及目标盘上**原有**的同名文件，
**一律比体积** —— 体积最大的拿"干净名"；其余按体积从大到小依次加「 (2)」「 (3)」…；
新来的更大时，原有的那个**让位**（把干净名让出来，自己加后缀）。体积相同则由原有的保名。

本文件钉的是**不变量**（而不是某条分支的实现细节）：

1. 冲突定名的先后只由体积决定；
2. 让位只发生在"新来的更大"时，让位者内容一字不改、也不计入 ``renamed``；
3. 重跑幂等：已经在 `` (N)`` 位置上的文件照用 —— 不叠后缀、不重复记账；
4. 后缀用尽时**保留原文件名**并报错，绝不覆盖（``MAX_SUFFIX_INDEX`` 兜底）；
5. 改名前后"文件内容集合"完全相同 —— 任何一条失败都意味着丢了内容。

与 ``tests/test_processor_engine.py`` 里那几条用例的分工：那边钉**单条分支**的字段细节
（样本形状、告警文案），这边钉**跨分支的不变量**（尤其第 5 条）。
"""

from __future__ import annotations

import copy

import processor_helpers as P


# --------------------------------------------------------------------------- 小工具


def _spec_with(proc_defaults, **sections) -> dict:
    """以 ``bangumi/legacy`` 整套合法规格为底（对 L2 而言与 ``default`` 无异），再收窄。"""
    spec = copy.deepcopy(proc_defaults.preset_spec("bangumi", "legacy"))
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


def _content_multiset(root) -> list[str]:
    """改名不改内容 ⇒ 前后"内容集合"必须逐字一致（"一个都不丢"的直接证据）。"""
    return sorted(P.tree_digest(root).values())


def _errors_of(outcome) -> list[str]:
    return [str(item.get("error", "")) for item in (outcome.errors or [])]


# --------------------------------------------------------------------------- 用例


def test_a_three_way_collision_is_ordered_by_size(proc_defaults, proc_engine, tmp_path):
    """三个文件抢同一个名字 ⇒ 体积最大的拿干净名，其余按体积从大到小拿 `` (2)`` / `` (3)``。"""
    spec = _spec_with(
        proc_defaults,
        extensions=[".mkv"],
        naming={"segments": [{"literal": "same.mkv"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {
            "Show A/Season 1/big.mkv": 30,
            "Show A/Season 1/mid.mkv": 20,
            "Show A/Season 1/small.mkv": 10,
        },
    )
    before = _content_multiset(root)

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert "big.mkv -> same.mkv" in P.renamed_lines(stdout)
    assert "Renamed (suffix): mid.mkv -> same (2).mkv" in stdout
    assert "Renamed (suffix): small.mkv -> same (3).mkv" in stdout
    assert len(outcome.renamed) == 3, outcome.renamed
    assert not outcome.errors, _errors_of(outcome)
    assert not outcome.displaced_targets, outcome.displaced_targets
    # 两份"加后缀"样本 + 两条"争名"事实（干净名只有一个赢家，所以是 2 条）。
    assert {item["target"] for item in outcome.renamed_with_suffix} == {
        "same (2).mkv",
        "same (3).mkv",
    }
    assert len(outcome.name_collisions) == 2, outcome.name_collisions
    # 内容集合必须一模一样：三个文件都还在，只是名字不同。
    assert P.file_names(root) == {
        "Show A/Season 1/same.mkv",
        "Show A/Season 1/same (2).mkv",
        "Show A/Season 1/same (3).mkv",
    }
    assert _content_multiset(root) == before


def test_a_bigger_newcomer_takes_the_clean_name_and_the_occupier_moves_aside(
    proc_defaults, proc_engine, tmp_path
):
    """新来的**更大** ⇒ 它拿干净名，目标盘上原有的那个让位到 `` (2)``（内容一字不改）。"""
    spec = _spec_with(
        proc_defaults,
        extensions=[".mkv"],
        naming={"segments": [{"literal": "target.txt"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/alpha.mkv": 50})
    alpha_content = (root / "Show A/Season 1/alpha.mkv").read_text(encoding="utf-8")
    occupier = root / "Show A/Season 1/target.txt"
    occupier.write_text("small-old", encoding="utf-8")
    before = _content_multiset(root)

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert "alpha.mkv -> target.txt" in P.renamed_lines(stdout)
    assert "Displaced: target.txt -> target (2).txt" in stdout
    assert len(outcome.displaced_targets) == 1, outcome.displaced_targets
    moved = outcome.displaced_targets[0]
    assert moved["old"] == "target.txt", moved
    assert moved["target"] == "target (2).txt", moved
    assert moved["dir"] == "Season 1", moved
    # 争名事实一条：目标名被"来源不明的原有文件"占着（existing_target）。
    assert len(outcome.name_collisions) == 1, outcome.name_collisions
    collision = outcome.name_collisions[0]
    assert collision["kind"] == "existing_target", collision
    assert collision["current"] == "alpha.mkv", collision
    assert not outcome.errors, _errors_of(outcome)
    # 让位**不计入** renamed（renamed 只数"源文件自己改了名"的那些）。
    assert len(outcome.renamed) == 1, outcome.renamed
    assert not outcome.renamed_with_suffix, outcome.renamed_with_suffix
    assert (root / "Show A/Season 1/target.txt").read_text(encoding="utf-8") == alpha_content
    assert (root / "Show A/Season 1/target (2).txt").read_text(encoding="utf-8") == "small-old"
    assert _content_multiset(root) == before


def test_a_rerun_with_the_same_winner_does_not_stack_another_suffix(
    proc_defaults, proc_engine, tmp_path
):
    """重跑：赢家还在干净名上、输家还在 `` (2)`` 上 ⇒ 谁都不动、不叠 `` (3)``、不重复记账。"""
    spec = _spec_with(
        proc_defaults,
        extensions=[".mkv"],
        naming={"segments": [{"literal": "same.mkv"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/big.mkv": 30, "Show A/Season 1/small.mkv": 10})

    first, _stdout, _log = _run(proc_engine, spec, root, tmp_path, name="run1")
    assert len(first.renamed_with_suffix) == 1, first.renamed_with_suffix
    after_first = _content_multiset(root)
    assert P.file_names(root) == {
        "Show A/Season 1/same.mkv",
        "Show A/Season 1/same (2).mkv",
    }

    second, stdout2, _log2 = _run(proc_engine, spec, root, tmp_path, name="run2")

    assert not second.renamed_with_suffix, second.renamed_with_suffix
    assert not second.name_collisions, second.name_collisions
    assert not second.displaced_targets, second.displaced_targets
    assert not second.errors, _errors_of(second)
    assert "Renamed (suffix): " not in stdout2, stdout2
    # 一个文件都没动：名字、内容都还是第一次跑完的样子。
    assert P.file_names(root) == {
        "Show A/Season 1/same.mkv",
        "Show A/Season 1/same (2).mkv",
    }
    assert _content_multiset(root) == after_first


def test_suffix_exhaustion_keeps_the_original_name_and_reports_an_error(
    proc_defaults, proc_engine, tmp_path, monkeypatch
):
    """干净名与所有 `` (N)`` 都被占满时：**保留原文件名** + 报错，绝不覆盖（兜底不变量）。"""
    monkeypatch.setattr(proc_engine, "MAX_SUFFIX_INDEX", 2)
    spec = _spec_with(
        proc_defaults,
        extensions=[".mkv"],
        naming={"segments": [{"literal": "same.mkv"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {
            "Show A/Season 1/a.mkv": 30,
            "Show A/Season 1/b.mkv": 20,
            "Show A/Season 1/c.mkv": 10,
        },
    )
    before = _content_multiset(root)

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert "a.mkv -> same.mkv" in P.renamed_lines(stdout)
    assert "Renamed (suffix): b.mkv -> same (2).mkv" in stdout
    errors = _errors_of(outcome)
    assert len(errors) == 1, errors
    assert "已保留原名" in errors[0], errors
    assert "Error renaming c.mkv:" in stdout, stdout
    # 第三个文件的**原文件名一字未动**，内容也还在。
    assert P.file_names(root) == {
        "Show A/Season 1/same.mkv",
        "Show A/Season 1/same (2).mkv",
        "Show A/Season 1/c.mkv",
    }
    assert _content_multiset(root) == before


def test_five_files_fighting_for_one_name_all_survive_with_their_content(
    proc_defaults, proc_engine, tmp_path
):
    """五个文件抢一个名字：一个都不丢、内容集合完全相同（真实事故的缩小版）。"""
    spec = _spec_with(
        proc_defaults,
        extensions=[".mkv"],
        naming={"segments": [{"literal": "same.mkv"}]},
    )
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {
            "Show A/Season 1/f01.mkv": 50,
            "Show A/Season 1/f02.mkv": 40,
            "Show A/Season 1/f03.mkv": 30,
            "Show A/Season 1/f04.mkv": 20,
            "Show A/Season 1/f05.mkv": 10,
        },
    )
    before = _content_multiset(root)

    outcome, stdout, _log = _run(proc_engine, spec, root, tmp_path)

    assert not outcome.errors, _errors_of(outcome)
    assert len(outcome.renamed) == 5, outcome.renamed
    assert sorted(item["target"] for item in outcome.renamed_with_suffix) == [
        "same (2).mkv",
        "same (3).mkv",
        "same (4).mkv",
        "same (5).mkv",
    ]
    assert P.file_names(root) == {
        "Show A/Season 1/same.mkv",
        "Show A/Season 1/same (2).mkv",
        "Show A/Season 1/same (3).mkv",
        "Show A/Season 1/same (4).mkv",
        "Show A/Season 1/same (5).mkv",
    }
    assert _content_multiset(root) == before
    # 体积最大的那个拿到了干净名（f01 的 50 字节）。
    biggest = max(root.glob("Show A/Season 1/same.mkv"), key=lambda p: p.stat().st_size)
    assert biggest.stat().st_size == 50
