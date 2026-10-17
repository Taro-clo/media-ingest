"""L3「特典排除」的引擎 / 执行器 / 设置接口测试。

判据：文件名命中特典名单的散装直接文件**不改名、不移动、不建目录**，保留原名留在原位置，
并先从待改名集合里剔除。名单全局生效（每个库、每个处理器），出厂默认 **7 条**（含 `CM集`），
可在设置页改成正则；``[]`` = 关闭。真实源里 `… CM集 (BD …).mkv` 这类 PV/CM 合集实测 5 个
文件命中、0 个能被集号规则解析。

精确判据：

* 只处理「作品名/Season N/」之下的**散装直接文件**；**不处理**「作品名/Season N/特典/」
  之下的文件（那是已经摆好的正规摆放，一个字节都不许动）。
* 名单全局生效（每一个库、每一个处理器），默认 7 条，可在设置页改成正则；``[]`` = 关闭。
* 命中者**不改名、不移动、不建目录**：保留原名留在原位置（一个字节都不碰）。
* 命中者先从待改名集合里剔除 ⇒ 不跟正片抢干净名、也不会进 ``episode_unresolved``。
* 本次排除清单进 ``RenameOutcome.extras_classified``：``{dir, name, size, pattern}``。

测试分三段：设置接口往返 / 引擎行为 / 执行器可见性。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os

import pytest

import processor_helpers as P

#: 出厂默认的 7 条判据（顺序即设置页里的顺序）。
DEFAULT_PATTERNS = ["NCOP", "NCED", "Preview", "Creditless", "Info", "予告編", "CM集"]

#: 修订后**不再**存在这个子目录；留着它专门用来断言"没有被创建"。
EXTRAS_DIR = "其他"


# --------------------------------------------------------------------------- 小工具


def _spec(proc_defaults, which: str = "bangumi"):
    """内置预设的 legacy 档（与现有引擎测试同形：判据、命名都最少）。"""
    return copy.deepcopy(proc_defaults.preset_spec(which, "legacy"))


def _run(proc_engine, spec, root, tmp_path, *, name="extras", **kwargs):
    """跑一轮引擎，返回 ``(outcome, stdout)``；log_file 固定复用（体积缓存要跨轮命中）。"""
    outcome, stdout = P.call_engine(
        proc_engine, spec, root, log_file=tmp_path / f"{name}.log", **kwargs
    )
    return outcome, stdout


def _set_patterns(db_mod, patterns) -> None:
    """直接写设置表（设置接口的校验另有专门用例）。"""
    db_mod.set_setting("extras_patterns", json.dumps(patterns, ensure_ascii=False))


def _classified(outcome) -> dict[str, tuple[str, int, str]]:
    """``{文件名: (作用域标签, 体积, 命中的判据)}``。"""
    return {
        item["name"]: (item["dir"], int(item["size"]), item["pattern"])
        for item in outcome.extras_classified
    }


def _kept_lines(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines() if line.strip().startswith("Extra")]


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _insert_library_with_processor(db_mod, name, source, target, processor_id):
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO libraries(name,type,processor,processor_id,log_file,enabled,source_dir,"
            "target_dir,match_categories,match_tags,match_path_prefixes,orphan_cleanup,"
            "permission_mode,rule_override,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "Bangumi", processor_id, processor_id, None, 1, source, target,
             "[]", "[]", "[]", 0, 0, None, stamp, stamp),
        )
        return int(cursor.lastrowid)


# --------------------------------------------------------------------------- 设置接口


def test_default_settings_seed_the_seven_patterns(proc_engine, db_mod):
    """出厂默认 = 7 条，且「设置」页读得到、引擎读得到（同一份）。"""
    assert json.loads(db_mod.get_setting("extras_patterns")) == DEFAULT_PATTERNS
    assert proc_engine.read_global_extras_patterns() == DEFAULT_PATTERNS
    # 兜底同样是默认值：文件系统里没有数据库时 L3 也是**开**的（与后缀白名单的兜底不同）。
    assert proc_engine.normalize_extras_patterns(None) == DEFAULT_PATTERNS
    assert proc_engine.normalize_extras_patterns("") == DEFAULT_PATTERNS


def test_normalize_extras_patterns_tolerates_shapes(proc_engine):
    assert proc_engine.normalize_extras_patterns([]) == []
    assert proc_engine.normalize_extras_patterns("NCOP") == ["NCOP"]
    assert proc_engine.normalize_extras_patterns('"NCOP"') == ["NCOP"]
    assert proc_engine.normalize_extras_patterns('["NCOP", "NCED"]') == ["NCOP", "NCED"]
    # 不拆分：正则里逗号/空格/点号都有意义 —— 一项就是一条正则。
    assert proc_engine.normalize_extras_patterns(["NCOP,NCED"]) == ["NCOP,NCED"]
    # 空项丢弃、去重保序、非字符串丢弃。
    assert proc_engine.normalize_extras_patterns(["NCOP", "  ", "NCOP", "NCED", 7]) == ["NCOP", "NCED"]
    # 坏 JSON ⇒ 整串当成**一条**判据（宁可留一条坏正则留痕，也不静默丢掉用户输入）。
    assert proc_engine.normalize_extras_patterns("坏 JSON [") == ["坏 JSON ["]


def test_settings_api_round_trips_and_rejects_bad_regex(api_client, api_login, db_mod):
    api_login(api_client)

    initial = api_client.get("/api/settings").json()
    assert initial["extras_patterns"] == DEFAULT_PATTERNS, initial.get("extras_patterns")

    saved = api_client.put(
        "/api/settings", json={"extras_patterns": ["^NCOP$", "Creditless", "^NCOP$"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["extras_patterns"] == ["^NCOP$", "Creditless"], saved.text
    assert json.loads(db_mod.get_setting("extras_patterns")) == ["^NCOP$", "Creditless"]

    # 坏正则必须整单拒绝，并且说清楚"是第几条、为什么"。
    bad = api_client.put("/api/settings", json={"extras_patterns": ["NCOP", "("]})
    assert bad.status_code == 400, bad.text
    detail = bad.json()["detail"]
    assert "第 2 条" in detail and "无法编译" in detail, detail
    assert "'('" in detail, detail
    # 400 不许破坏原值。
    assert api_client.get("/api/settings").json()["extras_patterns"] == ["^NCOP$", "Creditless"]

    empty_line = api_client.put("/api/settings", json={"extras_patterns": ["NCOP", "   "]})
    assert empty_line.status_code == 400, empty_line.text

    wrong_type = api_client.put("/api/settings", json={"extras_patterns": "NCOP"})
    assert wrong_type.status_code == 400, wrong_type.text
    wrong_item = api_client.put("/api/settings", json={"extras_patterns": ["NCOP", 7]})
    assert wrong_item.status_code == 400, wrong_item.text

    # 空数组 = 关闭 L3（合法，不是"未设置"）。
    cleared = api_client.put("/api/settings", json={"extras_patterns": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["extras_patterns"] == []
    assert api_client.get("/api/settings").json()["extras_patterns"] == []


# --------------------------------------------------------------------------- 引擎：命中


def test_every_default_pattern_hits_case_insensitively(proc_defaults, proc_engine, db_mod, tmp_path):
    """默认 7 条**逐条**都要命中，且大小写不敏感；命中者不改名、不移动。"""
    root = tmp_path / "engine"
    names = [
        "ncop.mkv",
        "NcEd.mkv",
        "PREVIEW.mkv",
        "creditless.mkv",
        "iNfO.mkv",
        "予告編.mkv",
        "cm集.mkv",
    ]
    tree = {"Show A/Season 1/Show.EP01.mkv": 10}
    for index, name in enumerate(names):
        tree[f"Show A/Season 1/{name}"] = 100 + index
    P.write_tree(root, tree)
    digests_before = P.tree_digest(root)

    outcome, stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)

    season = root / "Show A" / "Season 1"
    classified = _classified(outcome)
    assert sorted(classified) == sorted(names), classified
    for index, name in enumerate(names):
        label, size, pattern = classified[name]
        assert label == "Show A/Season 1", classified[name]
        assert size == 100 + index, classified[name]
        assert pattern.lower() in name.lower(), classified[name]
        # **留在原位置、原名不变**，内容一个字节都没变。
        assert (season / name).is_file(), f"{name} 被搬走了"
        assert _sha(season / name) == digests_before[f"Show A/Season 1/{name}"]
    # 修订后不再创建「其他/」。
    assert not (root / "Show A" / EXTRAS_DIR).exists()
    # 每个命中者一行 "Extra (kept): "，且**只**这一种前缀。
    kept = _kept_lines(stdout)
    assert len(kept) == 7, kept
    for line in kept:
        assert line.startswith("Extra (kept): Show A/Season 1/"), line
    assert "Extra: " not in stdout and "Extra in place" not in stdout, stdout
    # 正片照旧改名（L3 不碰它）。
    assert (season / "Show A S01E01.mkv").is_file()
    assert not (season / "Show.EP01.mkv").exists()
    # 命中的散装文件整体挪进"未改名"那一侧：不进 episode_unresolved。
    assert outcome.episode_unresolved == []


def test_nested_extras_folder_is_never_touched(proc_defaults, proc_engine, db_mod, tmp_path):
    """``Season N/特典/`` 之下的文件**不处理**：既不排除、也不改名。"""
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {
            "Show A/Season 1/NCOP.mkv": 10,
            "Show A/Season 1/特典/NCOP.mkv": 20,
            "Show A/Season 1/特典/Show.EP02.mkv": 30,
        },
    )

    outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)

    nested = root / "Show A" / "Season 1" / "特典"
    assert (nested / "NCOP.mkv").is_file(), "递归进特典目录 = 改错了用户的既有摆放"
    assert (nested / "Show.EP02.mkv").is_file(), "特典目录里的文件不该被改名"
    # 只有那个**直接**躺在 Season 1 里的散装 NCOP.mkv 被排除。
    assert (root / "Show A" / "Season 1" / "NCOP.mkv").is_file()
    assert list(_classified(outcome)) == ["NCOP.mkv"], _classified(outcome)
    assert not (root / "Show A" / EXTRAS_DIR).exists()


def test_excluded_extra_keeps_name_inode_and_content(proc_defaults, proc_engine, db_mod, tmp_path):
    """排除 = 只"不看它"：inode、文件名、内容、路径全都不变。"""
    root = tmp_path / "engine"
    path = root / "Show A" / "Season 1" / "NCOP.mkv"
    P.write_tree(root, {"Show A/Season 1/NCOP.mkv": 4321})
    before = os.stat(path)
    before_sha = _sha(path)

    _outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)

    after = os.stat(path)
    assert (after.st_ino, after.st_size) == (before.st_ino, 4321)
    assert _sha(path) == before_sha
    assert not (root / "Show A" / EXTRAS_DIR).exists()


def test_custom_patterns_and_off_switch(proc_defaults, proc_engine, db_mod, tmp_path):
    """自定义正则生效；``[]`` 关闭 L3（命中者照旧走正常改名）。"""
    _set_patterns(db_mod, [r"NC(OP|ED)", r"^CM\d+\.mkv$"])
    root = tmp_path / "custom"
    P.write_tree(
        root,
        {
            "Show A/Season 1/NCOP.mkv": 10,
            "Show A/Season 1/NCED.mkv": 20,
            "Show A/Season 1/CM01.mkv": 30,
            "Show A/Season 1/PV.mkv": 40,          # 不在自定义名单里 ⇒ 正常改名
            "Show A/Season 1/Show.EP01.mkv": 50,
        },
    )
    outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path, name="custom")
    assert sorted(_classified(outcome)) == ["CM01.mkv", "NCED.mkv", "NCOP.mkv"]
    assert sorted(_classified(outcome).values())[0][2] in ("NC(OP|ED)", "^CM\\d+\\.mkv$")
    season = root / "Show A" / "Season 1"
    # 命中的三个：一律原名原位置。
    assert (season / "NCOP.mkv").is_file()
    assert (season / "NCED.mkv").is_file()
    assert (season / "CM01.mkv").is_file()
    # 未命中的两个：照常改名（legacy 档兜底 "None"）。
    assert not (season / "PV.mkv").exists()
    assert (season / "Show A S01ENone.mkv").is_file()
    assert not (root / "Show A" / EXTRAS_DIR).exists()

    # ── [] = 关闭：同一个库、同一棵树，NCOP.mkv 不再被排除，而是走正常改名。
    _set_patterns(db_mod, [])
    off_root = tmp_path / "off"
    P.write_tree(off_root, {"Show A/Season 1/NCOP.mkv": 10, "Show A/Season 1/Show.EP01.mkv": 20})
    off_outcome, _stdout = _run(proc_engine, _spec(proc_defaults), off_root, tmp_path, name="off")
    assert off_outcome.extras_classified == []
    off_season = off_root / "Show A" / "Season 1"
    assert not (off_season / "NCOP.mkv").exists()
    assert (off_season / "Show A S01ENone.mkv").is_file()
    assert (off_season / "Show A S01E01.mkv").is_file()
    assert not (off_root / "Show A" / EXTRAS_DIR).exists()


def test_no_hit_means_no_behaviour_change(proc_defaults, proc_engine, db_mod, tmp_path):
    """没有命中 ⇒ 连目录都不建，而且整棵树的结果与 L3 关闭时**逐字节一致**。"""
    tree = {
        "Show A/Season 1/Show.EP01.mkv": 10,
        "Show A/Season 1/Show.EP02.mkv": 20,
        "Show A/Season 1/Show.EP01.chs.ass": 30,
    }
    _set_patterns(db_mod, [])
    off_root = tmp_path / "off"
    P.write_tree(off_root, tree)
    _off_outcome, off_stdout = _run(proc_engine, _spec(proc_defaults), off_root, tmp_path, name="off")

    _set_patterns(db_mod, DEFAULT_PATTERNS)
    on_root = tmp_path / "on"
    P.write_tree(on_root, tree)
    on_outcome, on_stdout = _run(proc_engine, _spec(proc_defaults), on_root, tmp_path, name="on")

    assert on_outcome.extras_classified == []
    assert not (on_root / "Show A" / EXTRAS_DIR).exists()
    assert P.tree_digest(on_root) == P.tree_digest(off_root)
    assert P.file_names(on_root) == P.file_names(off_root)
    assert P.renamed_lines(on_stdout) == P.renamed_lines(off_stdout)
    assert _kept_lines(on_stdout) == []


def test_uncompilable_setting_pattern_is_skipped_with_a_warning(
    proc_defaults, proc_engine, db_mod, tmp_path
):
    """设置表里躺着一条坏正则 ⇒ 跳过它、其余照常，并且留一条**纯文本**告警。"""
    _set_patterns(db_mod, ["NCOP", "("])
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/NCOP.mkv": 10, "Show A/Season 1/NCED.mkv": 20})

    outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)

    assert list(_classified(outcome)) == ["NCOP.mkv"]
    # 不在判据里的 NCED.mkv 照旧走正常改名（legacy 档兜底 "None"）。
    assert (root / "Show A" / "Season 1" / "Show A S01ENone.mkv").is_file()
    warning = [text for text in outcome.warnings if "无法编译" in text]
    assert warning, outcome.warnings
    assert "extras_patterns" in warning[0] and "(" in warning[0], warning[0]
    assert "**" not in warning[0] and "`" not in warning[0], "告警是纯文本，WebUI 按 esc() 渲染"


# --------------------------------------------------------------------------- 引擎：排除语义与幂等


def test_exclusion_keeps_the_hit_file_in_the_source_tree(
    proc_defaults, proc_engine, db_mod, tmp_path
):
    """排除**绝不能**是"先搬走再说"：源树里那份文件必须原地不动（内联回读校验）。"""
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {"Show A/Season 1/NCOP.mkv": 10, "Show A/Season 1/Show.EP01.mkv": 20},
    )
    before = P.tree_digest(root)

    outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)

    after = P.tree_digest(root)
    assert after["Show A/Season 1/NCOP.mkv"] == before["Show A/Season 1/NCOP.mkv"]
    # 只有正片换了名字，特典那一条**逐字节**一致。
    changed = {key for key in before if before[key] != after.get(key)}
    assert changed == {"Show A/Season 1/Show.EP01.mkv"}, (before, after)
    assert list(_classified(outcome)) == ["NCOP.mkv"]


def test_hits_do_not_pollute_the_unresolved_episode_list(
    proc_defaults, proc_engine, db_mod, tmp_path
):
    """生产档（``episode_fallback = None``）：命中者不算"没解析出集数"，未命中者才算。"""
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {
            "Show A/Season 1/NCOP.mkv": 10,
            "Show A/Season 1/Show [01] x.mkv": 20,
            "Show A/Season 1/无集数文件.mkv": 30,
        },
    )

    outcome, _stdout = _run(proc_engine, spec, root, tmp_path)

    assert list(_classified(outcome)) == ["NCOP.mkv"]
    # 命中的特典若算进 unresolved，待办看板就会被特典刷屏 —— 这是被明确断言的不变量。
    # （episode_unresolved 记的是**相对路径**。）
    assert [os.path.basename(item) for item in outcome.episode_unresolved] == ["无集数文件.mkv"], (
        outcome.episode_unresolved
    )
    assert (root / "Show A" / "Season 1" / "NCOP.mkv").is_file()
    assert (root / "Show A" / "Season 1" / "Show A S01E01.mkv").is_file()


def test_non_hits_still_take_the_normal_rename_path(proc_defaults, proc_engine, db_mod, tmp_path):
    """legacy 档（兜底 ``ENone``）：命中者原地不动，未命中的无集数文件照旧被改名。"""
    root = tmp_path / "engine"
    P.write_tree(
        root,
        {"Show A/Season 1/NCOP.mkv": 10, "Show A/Season 1/无集数文件.mkv": 20},
    )

    outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)

    season = root / "Show A" / "Season 1"
    assert list(_classified(outcome)) == ["NCOP.mkv"]
    assert (season / "NCOP.mkv").is_file()
    assert not (season / "无集数文件.mkv").exists()
    assert (season / "Show A S01ENone.mkv").is_file()


def test_two_extras_in_one_run_are_both_reported(proc_defaults, proc_engine, db_mod, tmp_path):
    """同一轮里两个 Season 的 NCOP：两条记录、两处原地保留、互不影响。"""
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/NCOP.mkv": 10, "Show A/Season 2/NCOP.mkv": 20})

    outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)

    assert (root / "Show A" / "Season 1" / "NCOP.mkv").is_file()
    assert (root / "Show A" / "Season 2" / "NCOP.mkv").is_file()
    assert len(outcome.extras_classified) == 2, outcome.extras_classified
    assert sorted(item["dir"] for item in outcome.extras_classified) == [
        "Show A/Season 1",
        "Show A/Season 2",
    ]
    assert not (root / "Show A" / EXTRAS_DIR).exists()


def test_second_run_is_idempotent(proc_defaults, proc_engine, db_mod, tmp_path):
    """第二遍（不带 force）：文件夹体积没变 ⇒ 整棵 Skipping folder，不再产生任何动作。"""
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/NCOP.mkv": 10, "Show A/Season 1/Show.EP01.mkv": 20})
    spec = _spec(proc_defaults)
    _run(proc_engine, spec, root, tmp_path, name="idem")
    digest = P.tree_digest(root)

    outcome, stdout = _run(proc_engine, spec, root, tmp_path, name="idem")

    assert outcome.extras_classified == []
    assert outcome.renamed == []
    assert "Skipping folder:" in stdout
    assert P.tree_digest(root) == digest
    assert (root / "Show A" / "Season 1" / "NCOP.mkv").is_file()


# --------------------------------------------------------------------------- 引擎：title 模式与 per=extension


def test_title_scope_excludes_extras_before_picking_the_biggest(
    proc_defaults, proc_engine, db_mod, tmp_path
):
    """``file_scope: title`` ⇒ 特典在作品目录里被排除；而且**先被剔除**再比"同后缀最大者"。"""
    root = tmp_path / "engine"
    spec = _spec(proc_defaults, "movie")
    # NCOP.mkv 体积最大：若不先剔除，它会当上 per=extension 的"胜者"再被排除，
    # 正片 b.mkv 就要等到下一次运行才被处理（那次体积没变、整棵跳过）—— 那是必须避免的行为。
    P.write_tree(root, {"Movie One/a.mkv": 5, "Movie One/b.mkv": 30, "Movie One/NCOP.mkv": 99})

    outcome, _stdout = _run(proc_engine, spec, root, tmp_path)

    title = root / "Movie One"
    assert (title / "NCOP.mkv").is_file()
    assert (title / "Movie One.mkv").stat().st_size == 30
    assert (title / "a.mkv").is_file()
    assert _classified(outcome) == {"NCOP.mkv": ("Movie One", 99, "NCOP")}
    assert not (title / EXTRAS_DIR).exists()


# --------------------------------------------------------------------------- 执行器可见性


def test_executor_reports_extras_classified(
    executor_mod, db_mod, proc_registry, proc_defaults, tmp_path
):
    """端到端：计数 + 样本（相对库根目录）+ 中文日志 + 收尾摘要，四处都要能看见。"""
    proc_registry.ensure_builtins()
    spec = proc_defaults.builtin_spec("bangumi")
    spec["id"] = "extras-bangumi"
    spec["name"] = "番剧（L3）"
    spec["log_file"] = "extrascache.log"
    proc_registry.create_processor(spec)

    source = tmp_path / "downloads"
    target = tmp_path / "library"
    tree = {"Show A/Season 1/Show [01] x.mkv": 10, "Show A/Season 1/NCOP.mkv": 20}
    P.write_tree(source, tree)
    P.write_tree(target, tree)

    library_id = _insert_library_with_processor(
        db_mod, "L3 库", str(source), str(target), "extras-bangumi"
    )
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        task_id = int(
            conn.execute(
                "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,"
                "result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (library_id, "L3 库", "rename", "manual", "queued",
                 json.dumps({"mode": "full"}), "{}", "{}", stamp),
            ).lastrowid
        )

    executor_mod.execute_task(task_id)

    row = db_mod.get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = json.loads(row["result"])
    assert row["status"] == "success", (dict(row), result)
    assert result["extras_classified"] == 1, result
    samples = result["samples"]["extras_classified"]
    assert len(samples) == 1, result
    assert samples[0]["name"] == "NCOP.mkv", samples
    assert samples[0]["size"] == 20, samples
    assert samples[0]["pattern"] == "NCOP", samples
    assert samples[0]["dir"] == "Show A/Season 1", samples
    assert result["samples_truncated"] is False, result

    # 目标侧：特典**原名原位置**（不被搬进 其他/、也不改名），正片照常改名。
    assert (target / "Show A" / "Season 1" / "NCOP.mkv").is_file()
    assert (target / "Show A" / "Season 1" / "Show A S01E01.mkv").is_file()
    assert not (target / "Show A" / EXTRAS_DIR).exists()

    logs = db_mod.get_conn().execute(
        "SELECT level, message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    kept = [(item["level"], str(item["message"])) for item in logs
            if "特典保留原名" in str(item["message"])]
    assert kept, [str(item["message"]) for item in logs]
    assert all(level == "INFO" for level, _text in kept), kept
    assert "NCOP.mkv" in kept[0][1]
    # 收尾摘要里的计数（"特典保留原名 1"）。
    summary = [str(item["message"]) for item in logs if "特典保留原名 1" in str(item["message"])]
    assert summary, [str(item["message"]) for item in logs][-5:]
    # 告警也要能看见（可计数 + 点了名）。
    warnings = result["warnings"]
    assert any("已排除改名" in text and "NCOP.mkv" in text for text in warnings), warnings


def test_executor_does_not_claim_extras_when_l3_is_off(
    executor_mod, db_mod, proc_registry, proc_defaults, tmp_path
):
    """``extras_patterns = []`` ⇒ 执行器侧计数为 0、没有样本、没有「其他/」目录。"""
    db_mod.set_setting("extras_patterns", "[]")
    proc_registry.ensure_builtins()
    spec = proc_defaults.builtin_spec("bangumi")
    spec["id"] = "extras-off"
    spec["name"] = "番剧（L3 关）"
    spec["log_file"] = "extrasoffcache.log"
    proc_registry.create_processor(spec)

    source = tmp_path / "downloads"
    target = tmp_path / "library"
    tree = {"Show A/Season 1/Show [01] x.mkv": 10, "Show A/Season 1/NCOP.mkv": 20}
    P.write_tree(source, tree)
    P.write_tree(target, tree)

    library_id = _insert_library_with_processor(
        db_mod, "L3 关库", str(source), str(target), "extras-off"
    )
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        task_id = int(
            conn.execute(
                "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,"
                "result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (library_id, "L3 关库", "rename", "manual", "queued",
                 json.dumps({"mode": "full"}), "{}", "{}", stamp),
            ).lastrowid
        )

    executor_mod.execute_task(task_id)

    row = db_mod.get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = json.loads(row["result"])
    assert row["status"] == "success", (dict(row), result)
    assert result["extras_classified"] == 0, result
    assert result["samples"]["extras_classified"] == [], result
    assert not (target / "Show A" / EXTRAS_DIR).exists()
    # L3 关掉之后 NCOP.mkv 不再被排除：它没解析出集数 ⇒ 走"保留原名"那条路（与 L3 无关），
    # 正片照常改名。
    assert (target / "Show A" / "Season 1" / "NCOP.mkv").is_file()
    assert (target / "Show A" / "Season 1" / "Show A S01E01.mkv").is_file()
    assert result["episode_unresolved"] == 1, result


# --------------------------------------------------------------------------- 契约护栏


def test_extras_patterns_are_not_silently_split(proc_engine):
    """正则里出现逗号/空格/点号时**不许**像后缀那样拆分（那会把一条正则拆成两条废的）。"""
    assert proc_engine.normalize_extras_patterns(["a b, c.d"]) == ["a b, c.d"]
    assert proc_engine.normalize_extras_patterns([r"NC(OP|ED)\s?"]) == [r"NC(OP|ED)\s?"]


def test_outcome_dict_exposes_extras_classified(proc_defaults, proc_engine, db_mod, tmp_path):
    root = tmp_path / "engine"
    P.write_tree(root, {"Show A/Season 1/NCOP.mkv": 10})
    outcome, _stdout = _run(proc_engine, _spec(proc_defaults), root, tmp_path)
    payload = P.outcome_to_dict(outcome)
    assert payload["extras_classified"] == [
        {"dir": "Show A/Season 1", "name": "NCOP.mkv", "size": 10, "pattern": "NCOP"}
    ], payload["extras_classified"]
