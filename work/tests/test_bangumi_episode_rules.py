"""L1：bangumi **默认**集数识别的 9 条有序规则 + 反例 + legacy 旧行为。

契约：`docs/contracts/PROCESSORS.md` §3.2「有意增量④」、`docs/contracts/COMPATIBILITY.md` 的增量清单。
本文件走**端到端**（真造一棵小树 + 真跑 `run_rename`），断言的是最终文件名：

- 命中 ⇒ `Show A S01E<两位集号>.<后缀>`；
- 9 条**全部不命中** ⇒ `episode_fallback=None` ⇒ **保留原文件名**（不生成 `S01ENone`）。

规则序号 = 任务书/文档里的书写顺序（序号小者优先），用例 id 标出该条命中的规则。
既有 fixture（`processor_helpers.BANGUMI_TREE`）上的集合差异见
`tests/test_processor_engine.py` 里同名规则的用例。
"""

from __future__ import annotations

import pytest

import processor_helpers as P

TITLE = "Show A"
SEASON_DIR = f"{TITLE}/Season 1"

#: 默认预设必须逐条声明这 9 个名字，且顺序不能变（顺序即优先级）。
DEFAULT_RULE_NAMES = (
    "chapter",
    "episode_word",
    "season_episode",
    "bracketed",
    "ep_marker",
    "hash_marker",
    "dash",
    "bare_e",
    "bare_number",
)

#: 正例：`(用例 id, 文件名, 期望集号)`。期望集号是命名段 `episode`（pad=2）渲染出来的两位数字。
HITS = (
    # --- rule1 chapter: 第 N 集 / 第 N 話 -------------------------------------------------
    ("rule1 第 N 話", "Show 第 12 話.mkv", "12"),
    ("rule1 第N集（紧贴）", "Show 第7集.mkv", "07"),
    ("rule1 全角空白", "Show 第　5　集.mkv", "05"),
    # --- rule2 episode_word: Episode N（大小写不敏感） ------------------------------------
    ("rule2 Episode", "Show Episode 3.mkv", "03"),
    ("rule2 episode 小写", "Show episode 4.mkv", "04"),
    ("rule2 EPISODE 大写", "Show EPISODE 5.mkv", "05"),
    ("rule2 紧贴数字", "Show Episode6.mkv", "06"),
    # --- rule3 season_episode: SxxExx（大小写不敏感、可带空白） ----------------------------
    ("rule3 S02E07", "Show S02E07.mkv", "07"),
    ("rule3 小写 s2e9", "Show s2e9.mkv", "09"),
    ("rule3 空白 S 02 E 08", "Show S 02 E 08.mkv", "08"),
    # --- rule4 bracketed: [N] ------------------------------------------------------------
    ("rule4 [05]", "Show [05].mkv", "05"),
    ("rule4 空白 [ 5 ]", "Show [ 5 ] x.mkv", "05"),
    # --- rule5 ep_marker: EP N（大小写不敏感、可带空白） -----------------------------------
    ("rule5 EP09", "Show EP09.mkv", "09"),
    ("rule5 小写 ep 10", "Show ep 10.mkv", "10"),
    ("rule5 紧贴点号", "Show.EP09.mkv", "09"),
    # --- rule6 hash_marker: #N -----------------------------------------------------------
    ("rule6 #04", "Show #04.mkv", "04"),
    ("rule6 空白 # 4", "Show # 4.mkv", "04"),
    # --- rule7 dash: -N（后面不能是 bit/Bit） ---------------------------------------------
    ("rule7 - 11", "Show - 11.mkv", "11"),
    ("rule7 紧贴 -6", "Show -6.mkv", "06"),
    # --- rule8 bare_e: 空白后的 E/e N -----------------------------------------------------
    ("rule8 E12", "Show E12.mkv", "12"),
    ("rule8 小写 e3", "Show e3.mkv", "03"),
    ("rule8 空白 e 3", "Show e 3.mkv", "03"),
    # --- rule9 bare_number: 空白后的裸数字（后接空白或句点） -------------------------------
    ("rule9 12.mkv", "Show 12.mkv", "12"),
    ("rule9 12 1080p", "Show 12 1080p.mkv", "12"),
    ("rule9 全角空白", "Show\u300012\u30001080p.mkv", "12"),
)

#: 优先级：同时能被多条规则命中的文件名，必须由**序号最小**的那条拿下。
ORDER_CASES = (
    ("rule1 优先于 rule9", "Show 第 05 話 12.mkv", "05"),
    ("rule2 优先于 rule8", "Show Episode 4 E9.mkv", "04"),
    ("rule3 优先于 rule9", "Show S02E07 12.mkv", "07"),
    ("rule4 优先于 rule9", "Show [07] 12.mkv", "07"),
    ("rule5 优先于 rule9", "Show EP 03 12.mkv", "03"),
    ("rule6 优先于 rule8", "Show #04 E9.mkv", "04"),
    ("rule7 优先于 rule9", "Show - 11 12.mkv", "11"),
    ("rule8 优先于 rule9", "Show E12 34.mkv", "12"),
    # rule1 要求 1~2 位数字（`\d{1,2}(?!\d)`），3 位数不成立 ⇒ 落到 rule9 的尾号
    ("rule1 拒 3 位数 ⇒ rule9", "Show 第123話 12.mkv", "12"),
)

#: 反例：9 条**全部**不命中 ⇒ 保留原文件名。都是真实库里出现过的伪集号写法。
MISSES = (
    ("分辨率 1920x1080", "Show 1920x1080.mkv"),
    ("x264-10bit", "Show x264-10bit.mkv"),
    ("H265-8bit", "Show H265-8bit.mkv"),
    ("'- 10 bit'（词边界）", "Show - 10 bit.mkv"),
    ("'- 11 Bit'（词边界，大小写）", "Show - 11 Bit.mkv"),
    ("'10 Bit'（词边界）", "Show 10 Bit.mkv"),
    ("[1920x1080]", "Show [1920x1080].mkv"),
    ("[1AE35F3A] CRC32", "Show [1AE35F3A].mkv"),
    ("第123話（3 位数）", "Show 第123話.mkv"),
    ("S123E07（季号 3 位数）", "Show S123E07.mkv"),
    # 规格里给出的样例是 `Episode123`（无后缀、不进白名单）；这里补 `.mkv` 让反例非空跑。
    ("Episode123（3 位数）", "Show Episode123.mkv"),
    ("12v2（数字后不是空白/句点）", "Show 12v2.mkv"),
)


def _default_spec(defaults) -> dict:
    return defaults.preset_spec("bangumi", "default")


def _one_file_root(tmp_path, filename: str):
    root = tmp_path / "lib"
    root.mkdir(parents=True, exist_ok=True)
    P.write_tree(root, {f"{SEASON_DIR}/{filename}": 10})
    return root


def _run(engine, defaults, root, tmp_path, *, require_output: bool):
    outcome, stdout = P.call_engine(
        engine,
        _default_spec(defaults),
        root,
        log_file=tmp_path / "bangumi.log",
        require_output=require_output,
    )
    return outcome, stdout


def test_default_preset_declares_the_nine_named_rules_in_order(proc_defaults):
    spec = _default_spec(proc_defaults)
    assert [item["name"] for item in spec["episode_patterns"]] == list(DEFAULT_RULE_NAMES)
    assert spec["episode_fallback"] is None, "未命中集数 ⇒ 保留原文件名"


@pytest.mark.parametrize("case_id, filename, episode", (*HITS, *ORDER_CASES))
def test_default_rules_rename_with_the_expected_episode(
    proc_defaults, proc_engine, tmp_path, case_id, filename, episode
):
    root = _one_file_root(tmp_path, filename)
    outcome, _stdout = _run(proc_engine, proc_defaults, root, tmp_path, require_output=True)

    expected = f"{SEASON_DIR}/{TITLE} S01E{episode}.mkv"
    assert P.file_names(root) == {expected}, case_id
    assert P.outcome_renamed(outcome) == {f"{filename} -> {TITLE} S01E{episode}.mkv"}, case_id


@pytest.mark.parametrize("case_id, filename", MISSES)
def test_default_rules_keep_the_original_name_when_nothing_matches(
    proc_defaults, proc_engine, tmp_path, case_id, filename
):
    root = _one_file_root(tmp_path, filename)
    _outcome, _stdout = _run(proc_engine, proc_defaults, root, tmp_path, require_output=False)

    original = f"{SEASON_DIR}/{filename}"
    assert P.file_names(root) == {original}, case_id
    assert not any("ENone" in name for name in P.file_names(root)), (
        f"{case_id}: 未命中集数不得回落成字面量 ENone"
    )


def test_legacy_preset_keeps_the_old_loose_behaviour(proc_defaults, proc_engine, tmp_path):
    """旧行为（裸 `e`/`E` 分支 + 字符串 `"None"` 兜底）移到 `legacy` 下断言。

    default 下 `Show.e06.mkv` / `Show.E07.mkv` 都**不命中**（rule8 要求 `E` 前是空白），
    而 legacy 逐字复刻最朴素的旧口径的两条集数正则，仍然命中 06 / 07；`Show.01.ass` 两条
    旧正则都认不出集号，于是回落成字面量 `None` ⇒ `S01ENone.01.ass`。
    """
    tree = {
        f"{SEASON_DIR}/Show.e06.mkv": 10,
        f"{SEASON_DIR}/Show.E07.mkv": 20,
        f"{SEASON_DIR}/Show.01.ass": 30,
    }
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir(parents=True, exist_ok=True)
    P.write_tree(legacy_root, dict(tree))
    P.call_engine(
        proc_engine,
        proc_defaults.preset_spec("bangumi", "legacy"),
        legacy_root,
        log_file=tmp_path / "legacy.log",
    )
    assert P.file_names(legacy_root) == {
        f"{SEASON_DIR}/{TITLE} S01E06.mkv",
        f"{SEASON_DIR}/{TITLE} S01E07.mkv",
        f"{SEASON_DIR}/{TITLE} S01ENone.01.ass",
    }

    default_root = tmp_path / "default"
    default_root.mkdir(parents=True, exist_ok=True)
    P.write_tree(default_root, dict(tree))
    _run(proc_engine, proc_defaults, default_root, tmp_path, require_output=False)
    assert P.file_names(default_root) == {f"{SEASON_DIR}/{name}" for name in
                                          ("Show.e06.mkv", "Show.E07.mkv", "Show.01.ass")}
