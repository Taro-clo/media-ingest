"""库级命名覆盖 / 库级目录规则：**每个媒体库**自己的重命名方式与目录规则（``libraries.rule_override``）。

三条契约（用户原话 → 落地形状）：

* 库级命名覆盖：「不同媒体库的重命名方式应当允许分别设定……用户可在 webui 中自行选择修改采用
  哪些字段来重命名」⇒ 库级 ``naming.template`` / ``naming.segments`` + calibre 风格的
  ``{字段}`` 模板（``app/processors/spec.py`` 的 ``compile_template``）；
* 库级目录规则：「不同媒体库应当允许单独设定接受和拒绝重命名特定文件夹下的文件，采用正则识别」
  ⇒ 库级 ``traverse.accept_dirs`` / ``traverse.reject_dirs``；
* 共同点：**只覆盖用户填过的字段**，其余沿用处理器；覆盖在引擎里于**校验之前**合并，
  所以覆盖本身造成的跨层冲突（``file_scope=title`` 却留着 ``accept_dirs``）也会被拦住。

这里刻意把「模板编译」当纯函数测（不碰 DB），再用引擎与执行器各真跑一次，验证
"落库的规则真的会改变重命名结果"。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import processor_helpers as P

#: 引擎/执行器用例的目录树：两个标题文件夹，一个该处理、一个该被拒绝。
TREE = {
    "Show A/Season 1/ep01.mkv": 11,
    "Show A/Season 1/ep02.mkv": 13,
    "Show A/Extras/extra.mkv": 17,
    "Show B/Season 1/ep03.mkv": 19,
}

#: 处理器自带的命名（标题 + S + 季 + E + 集 + 后缀）——"没写覆盖"时的对照组。
PROCESSOR_EXPECTED = {
    "ep01.mkv -> Show A S01E01.mkv",
    "ep02.mkv -> Show A S01E02.mkv",
    "ep03.mkv -> Show B S01E03.mkv",
}


# --------------------------------------------------------------------------- 模板编译器


def test_compile_template_expands_fields_literals_and_padding(proc_spec):
    spec = proc_spec

    assert spec.compile_template("{title} S{season}E{episode}{suffix}") == [
        {"param": "title"},
        {"literal": " S"},
        {"param": "season"},
        {"literal": "E"},
        {"param": "episode"},
        {"param": "suffix"},
    ]
    # `{episode:02}` ⇒ pad=2（与段序列的 pad 语义完全一致，引擎不需要认识模板）
    assert spec.compile_template("E{episode:02}") == [
        {"literal": "E"},
        {"param": "episode", "pad": 2},
    ]
    # `{{` / `}}` 是字面量花括号；落单的 `{` 当字面量而不是整份失败。
    assert spec.compile_template("{{x}} {title}") == [
        {"literal": "{x} "},
        {"param": "title"},
    ]
    assert spec.compile_template("a{b") == [{"literal": "a{b"}]


def test_template_errors_flag_unknown_names_and_bad_padding(proc_spec):
    spec = proc_spec

    assert spec.template_errors("{title} S{season}E{episode}") == []
    assert spec.template_errors(None) == []

    unknown = spec.template_errors("{title} {nope}")
    assert len(unknown) == 1 and "未知变量" in unknown[0], unknown
    bad_pad = spec.template_errors("{episode:0>3s}")
    assert len(bad_pad) == 1 and "补零" in bad_pad[0], bad_pad
    assert spec.template_errors("{episode:00}"), "补零位数必须是 >=1 的整数"
    assert spec.template_errors("") == []


# 引擎上下文里有一个**内部键** ``episode_resolved``（见 engine._file_context）。
# 它不属于"给用户看的模板变量"（``TEMPLATE_VARS``），也不该出现在 WebUI 变量下拉里，
# 所以下面两条"变量表不漂移"的断言把它排除；其余所有键仍然必须逐字一致。
ENGINE_INTERNAL_CONTEXT_KEYS = ("episode_resolved",)


def test_template_variables_are_exactly_the_engine_context(proc_spec, proc_engine):
    """模板变量表 == 引擎真实提供的上下文键（扣除引擎内部键）。

    这条断言是"前后端变量表不会漂移"的机器保证：``TEMPLATE_VARS`` 是给用户看的文档，
    ``engine.sample_context()`` 才是运行期真值 —— 两者只要不一致，用户就会写出一个
    "校验通过、运行期静默为空"的模板。

    为什么：`sample_context()` 会多返回 ``episode_resolved``（bool，未解析出
    集数时为 False）。它是引擎内部标志（`_process_scope` 靠它决定"保留原文件名"），故意
    **不**进 ``TEMPLATE_VARS`` / WebUI 变量表 —— 否则 ``{episode_resolved}`` 就成了一个没人
    需要的模板变量，还会让 `app/processors/spec.py` 与 `web/js/processors_util.js` 两个
    前端文件必须跟着改。所以这里显式排除它，而不是把它加进变量表。
    """
    names = set(proc_engine.sample_context()) - set(ENGINE_INTERNAL_CONTEXT_KEYS)
    assert tuple(sorted(proc_spec.TEMPLATE_VARS)) == tuple(sorted(names))


def test_webui_variable_table_matches_the_engine(proc_engine):
    """WebUI 的变量下拉（``PROC_PARAMS``）同样必须与引擎上下文逐字一致（同样扣除内部键）。"""
    source = Path("web/js/processors_util.js").read_text(encoding="utf-8")
    match = re.search(r"export const PROC_PARAMS = \[(.*?)\];", source, re.S)
    assert match, "web/js/processors_util.js 里的 PROC_PARAMS 不见了"
    names = re.findall(r"'([^']+)'", match.group(1))
    context_names = set(proc_engine.sample_context()) - set(ENGINE_INTERNAL_CONTEXT_KEYS)
    assert tuple(sorted(names)) == tuple(sorted(context_names)), names


# --------------------------------------------------------------------------- 覆盖归一化


def test_normalize_override_keeps_only_what_was_filled():
    overrides = pytest.importorskip("app.processors.overrides")

    assert overrides.normalize_override(None) == {}
    assert overrides.normalize_override({"naming": {}, "traverse": {}}) == {}
    assert overrides.override_is_empty({}) and overrides.override_is_empty({"naming": {}})
    assert overrides.override_is_empty({"extensions": []})

    normalized = overrides.normalize_override(
        {
            "extensions": [".TS", ".mkv", ".ts"],
            "traverse": {"reject_dirs": [{"pattern": "SP"}], "file_scope": "subdir"},
            "naming": {"template": "{title}{suffix}", "expressions": {"ep": "int(episode)"}},
        }
    )
    assert normalized["extensions"] == [".ts", ".mkv"], "后缀归一化：小写 + 去重 + 保序"
    assert normalized["traverse"]["file_scope"] == "subdir"
    # accept_dirs 补 season_group（带组），reject_dirs 不带 —— 与处理器规格同源。
    accept = overrides.normalize_override(
        {"traverse": {"accept_dirs": [{"pattern": "^Season (\\d+)$"}]}}
    )
    assert accept["traverse"]["accept_dirs"] == [
        {"pattern": "^Season (\\d+)$", "season_group": 1}
    ]
    assert overrides.normalize_override(
        {"traverse": {"reject_dirs": [{"pattern": "^SP$"}]}}
    )["traverse"]["reject_dirs"] == [{"pattern": "^SP$"}]
    # 模板在归一化期就编译成段序列，引擎与规格校验器都不需要认识 template。
    assert normalized["naming"]["segments"] == [{"param": "title"}, {"param": "suffix"}]
    assert normalized["naming"]["template"] == "{title}{suffix}"
    # **幂等**：路由把归一化结果落库、读回时还要再跑一次 normalise；模板原文必须活下来，
    # 否则用户在 WebUI 写的模板在第一次保存后就丢了。
    assert overrides.normalize_override(normalized) == normalized
    assert overrides.normalize_override(
        overrides.read_library_override(json.dumps(normalized))
    ) == normalized

    # 存坏的 JSON 不许把库变成"跑不起来"：读出来就是"没有覆盖"。
    assert overrides.read_library_override("{不是 JSON") == {}
    assert overrides.read_library_override("") == {}
    assert overrides.read_library_override(json.dumps({"extensions": [".ts"]})) == {
        "extensions": [".ts"]
    }


def test_validate_override_reports_field_level_chinese_errors():
    overrides = pytest.importorskip("app.processors.overrides")

    assert overrides.validate_override(None) == []
    assert overrides.validate_override({"naming": {"template": "{title}"}}) == []

    fields = {
        item["field"]
        for item in overrides.validate_override(
            {
                "nonsense": 1,
                "extensions": ["mkv"],
                "traverse": {
                    "title_depth": 2,
                    "file_scope": "nope",
                    "reject_dirs": [{"pattern": "("}],
                },
                "naming": {"template": "{nope}", "expressions": {"x": 1}},
            }
        )
    }
    assert fields == {
        "rule_override.nonsense",
        "rule_override.extensions[0]",
        "rule_override.traverse.title_depth",
        "rule_override.traverse.file_scope",
        "rule_override.traverse.reject_dirs[0].pattern",
        "rule_override.naming.template",
        "rule_override.naming.expressions.x",
    }, fields

    # 空模板 / 空段序列也要有话说，而不是"保存成功、运行期什么都不改"。
    assert overrides.validate_override({"naming": {"template": "   "}}), "空白模板必须报错"
    assert overrides.validate_override({"naming": {"segments": []}}), "空段序列必须报错"


def test_override_template_may_use_expressions_and_engine_keys(proc_defaults):
    """库级覆盖的模板可以引用"处理器表达式定义的名字"与引擎内部键。

    早期实现里 `template_errors` 只认 12 个变量表名字，于是 `{custom}`（处理器里写了
    ``naming.expressions.custom``）会被**误判**成未知变量；段写法却能读到它 —— 两条写法
    口径不一致。现在两件事一起对齐：保存期拦住真·未知名字，表达式名与 ``episode_resolved``
    都算认识（运行期 `apply_override` 会把表达式按名字并进上下文）。
    """
    overrides = pytest.importorskip("app.processors.overrides")
    spec_data = proc_defaults.builtin_spec("bangumi")
    spec_data["naming"] = {
        **spec_data["naming"],
        "expressions": {"custom": "'x'"},
        "segments": [{"param": "custom"}, {"param": "ext"}],
    }

    stored, problems = overrides.resolve_override(
        {"naming": {"template": "{custom}{ext}"}}, spec_data
    )
    assert problems == [], problems
    assert json.loads(stored)["naming"]["segments"] == [{"param": "custom"}, {"param": "ext"}]

    _, problems = overrides.resolve_override(
        {"naming": {"template": "{episode_resolved}{ext}"}}, spec_data
    )
    assert problems == [], problems

    _, problems = overrides.resolve_override({"naming": {"template": "{no_such_name}"}}, spec_data)
    assert [item["field"] for item in problems] == ["rule_override.naming.template"], problems
    assert "未知变量" in problems[0]["message"], problems

    _, problems = overrides.resolve_override(
        {"naming": {"segments": [{"param": "no_such_name"}]}}, spec_data
    )
    assert [item["field"] for item in problems] == [
        "rule_override.naming.segments[0].param"
    ], problems


def test_apply_override_merges_only_the_given_fields(proc_defaults):
    overrides = pytest.importorskip("app.processors.overrides")
    spec = proc_defaults.preset_spec("bangumi", "legacy")

    merged = overrides.apply_override(
        spec,
        overrides.normalize_override(
            {
                "traverse": {"reject_dirs": [{"pattern": "SP"}]},
                "naming": {"template": "{title}{ext}"},
            }
        ),
    )

    # 拒绝目录整份替换；接受目录没填 ⇒ 沿用处理器（Season 正则还在）。
    assert merged["traverse"]["reject_dirs"] == [{"pattern": "SP"}]
    assert merged["traverse"]["accept_dirs"] == spec["traverse"]["accept_dirs"]
    assert merged["traverse"]["file_scope"] == spec["traverse"]["file_scope"]
    # 命名整份替换：用户写了模板就该按他写的来，而不是在处理器后面再拼一段。
    assert merged["naming"]["segments"] == [{"param": "title"}, {"param": "ext"}]
    # expressions 按名字增量合并：同名覆盖、其余保留。
    increment = overrides.apply_override(
        {"naming": {"expressions": {"a": "1", "b": "2"}}, "traverse": {}},
        {"naming": {"expressions": {"b": "9"}}},
    )
    assert increment["naming"]["expressions"] == {"a": "1", "b": "9"}
    # 没填覆盖时是**深拷贝**：改副本不许污染注册表里的那份 spec。
    assert overrides.apply_override(spec, None) == spec
    untouched = overrides.apply_override(spec, None)
    untouched["traverse"]["reject_dirs"].append({"pattern": "x"})
    assert spec["traverse"]["reject_dirs"] != untouched["traverse"]["reject_dirs"]


# --------------------------------------------------------------------------- 引擎真跑


def _spec(proc_defaults) -> dict:
    """bangumi 处理器：只认 ``Season N`` 子目录，命名 = 标题 S季E集 + 后缀。

    集数正则刻意收紧成 ``ep01`` 这种写法，让"取到哪一位数字"完全确定。
    """
    spec = proc_defaults.preset_spec("bangumi", "legacy")
    spec["traverse"]["reject_dirs"] = []
    spec["episode_patterns"] = [{"name": "ep", "pattern": r"ep(?P<g1>\d{1,2})"}]
    spec["naming"] = {
        "expressions": {},
        "segments": [
            {"param": "title"},
            {"literal": " S"},
            {"param": "season", "pad": 2},
            {"literal": "E"},
            {"param": "episode", "pad": 2},
            {"param": "suffix"},
        ],
    }
    return spec


def test_engine_keeps_the_processor_segments_without_an_override(proc_defaults, proc_engine, tmp_path):
    root = tmp_path / "engine0"
    P.write_tree(root, TREE)

    _outcome, stdout = P.call_engine(
        proc_engine, _spec(proc_defaults), root, log_file=tmp_path / "engine0.log"
    )

    assert P.renamed_lines(stdout) == PROCESSOR_EXPECTED, stdout


def test_engine_uses_the_library_template_instead_of_the_processor_segments(
    proc_defaults, proc_engine, tmp_path
):
    root = tmp_path / "engine1"
    P.write_tree(root, TREE)

    _outcome, stdout = P.call_engine(
        proc_engine,
        _spec(proc_defaults),
        root,
        log_file=tmp_path / "engine1.log",
        override={"naming": {"template": "{title} - {episode_raw}{suffix}"}},
    )

    assert P.renamed_lines(stdout) == {
        "ep01.mkv -> Show A - 01.mkv",
        "ep02.mkv -> Show A - 02.mkv",
        "ep03.mkv -> Show B - 03.mkv",
    }, stdout


def test_engine_applies_the_library_template_with_padding(proc_defaults, proc_engine, tmp_path):
    root = tmp_path / "engine2"
    P.write_tree(root, TREE)

    _outcome, stdout = P.call_engine(
        proc_engine,
        _spec(proc_defaults),
        root,
        log_file=tmp_path / "engine2.log",
        override={"naming": {"template": "[{title}] {season}.{episode:03}{suffix}"}},
    )

    assert P.renamed_lines(stdout) == {
        "ep01.mkv -> [Show A] 01.001.mkv",
        "ep02.mkv -> [Show A] 01.002.mkv",
        "ep03.mkv -> [Show B] 01.003.mkv",
    }, stdout


def test_engine_uses_the_library_directory_regexes(proc_defaults, proc_engine, tmp_path):
    """库级目录规则：库自己决定接受/拒绝哪些文件夹（正则；命中拒绝 = 整棵子树都不处理）。"""
    root = tmp_path / "engine3"
    P.write_tree(root, TREE)

    _outcome, stdout = P.call_engine(
        proc_engine,
        _spec(proc_defaults),
        root,
        log_file=tmp_path / "engine3.log",
        override={
            "traverse": {
                "accept_dirs": [{"pattern": r"^Season (\d+)$", "season_group": 1}],
                "reject_dirs": [{"pattern": r"^Extras$"}, {"pattern": r"^Show B$"}],
            }
        },
    )

    assert P.renamed_lines(stdout) == {
        "ep01.mkv -> Show A S01E01.mkv",
        "ep02.mkv -> Show A S01E02.mkv",
    }, stdout
    # 被拒绝的子树整棵不动。
    assert (root / "Show A/Extras/extra.mkv").is_file()
    assert (root / "Show B/Season 1/ep03.mkv").is_file()


def test_engine_reports_the_cross_layer_conflict_from_an_override(
    proc_defaults, proc_engine, tmp_path
):
    """覆盖把 file_scope 改成 title 却留着 accept_dirs ⇒ 规格校验拦住（不静默半生效）。"""
    root = tmp_path / "engine4"
    P.write_tree(root, TREE)

    outcome, stdout = P.call_engine(
        proc_engine,
        _spec(proc_defaults),
        root,
        log_file=tmp_path / "engine4.log",
        override={"traverse": {"file_scope": "title"}},
        require_output=False,
    )

    assert outcome.exception, outcome
    assert "accept_dirs" in str(outcome.exception), outcome.exception
    assert not P.renamed_lines(stdout), stdout


# --------------------------------------------------------------------------- 接口 + 落库


def _body(source_dir, target_dir, **extra) -> dict:
    body = {
        "name": "覆盖库",
        "type": "Bangumi",
        "processor_id": "bangumi",
        "source_dir": str(source_dir),
        "target_dir": str(target_dir),
    }
    body.update(extra)
    return body


def test_library_api_round_trips_the_rule_override(
    api_client, api_login, source_dir, target_dir, db_mod
):
    api_login(api_client)

    created = api_client.post(
        "/api/libraries",
        json=_body(
            source_dir,
            target_dir,
            rule_override={
                "naming": {"template": "{title} S{season}E{episode:02}"},
                "traverse": {"reject_dirs": [{"pattern": "^Extras$"}]},
            },
        ),
    )
    assert created.status_code == 201, created.text
    view = created.json()
    # 回填给前端的形状：template 原文 + 编译好的 segments（前端据此画预览）。
    assert view["rule_override"]["naming"]["template"] == "{title} S{season}E{episode:02}"
    assert view["rule_override"]["naming"]["segments"] == [
        {"param": "title"},
        {"literal": " S"},
        {"param": "season"},
        {"literal": "E"},
        {"param": "episode", "pad": 2},
    ]
    assert view["rule_override"]["traverse"]["reject_dirs"] == [{"pattern": "^Extras$"}]

    library_id = view["id"]
    stored = db_mod.get_conn().execute(
        "SELECT rule_override FROM libraries WHERE id=?", (library_id,)
    ).fetchone()["rule_override"]
    assert json.loads(stored)["naming"]["template"] == "{title} S{season}E{episode:02}"

    # PUT 不带 rule_override ⇒ 保留现值（前端表单不总是带着它，悄悄清空是最坏的结果）。
    kept = api_client.put(
        f"/api/libraries/{library_id}", json=_body(source_dir, target_dir, name="改名")
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["rule_override"]["naming"]["template"] == "{title} S{season}E{episode:02}"

    # PUT 显式给空对象 ⇒ 清掉覆盖（回到完全跟随处理器）。
    cleared = api_client.put(
        f"/api/libraries/{library_id}",
        json=_body(source_dir, target_dir, name="改名", rule_override={}),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["rule_override"] == {}


def test_library_api_refuses_a_broken_override(api_client, api_login, source_dir, target_dir):
    api_login(api_client)

    cases = [
        ({"naming": {"template": "{nope}"}}, "未知变量"),
        ({"naming": {"template": "{episode:0>3s}"}}, "补零"),
        ({"traverse": {"file_scope": "title"}}, "accept_dirs"),
        ({"traverse": {"reject_dirs": [{"pattern": "("}]}}, "正则无法编译"),
        ({"traverse": {"title_depth": 2}}, "title_depth"),
        ({"whatever": 1}, "未知字段"),
    ]
    for index, (override, expected) in enumerate(cases):
        response = api_client.post(
            "/api/libraries",
            json=_body(source_dir, target_dir, name=f"坏库{index}", rule_override=override),
        )
        assert response.status_code == 422, (override, response.status_code, response.text)
        detail = response.json()["detail"]
        assert any(expected in item["msg"] for item in detail), (override, detail)


def test_migration_adds_the_rule_override_column_without_touching_old_rows(db_mod, settings_env):
    """v2.0 建的库表没有这一列：迁移只 ADD COLUMN，老行一个字节都不动。"""
    conn = db_mod.get_conn()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(libraries)").fetchall()}
    assert "rule_override" in columns

    stamp = db_mod.now_iso()
    with db_mod.tx() as tx_conn:
        tx_conn.execute(
            "INSERT INTO libraries(name,type,processor,processor_id,enabled,source_dir,target_dir,"
            "match_categories,match_tags,match_path_prefixes,orphan_cleanup,permission_mode,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("老库", "Bangumi", "bangumi", "bangumi", 1, "/a", "/b",
             "[]", "[]", "[]", 1, 0, stamp, stamp),
        )
    row = conn.execute("SELECT * FROM libraries WHERE name='老库'").fetchone()
    assert row["rule_override"] is None, "没写覆盖的老库必须保持 NULL = 完全跟随处理器"

    applied = db_mod.migrate()
    assert "libraries.rule_override" not in applied, applied
    again = conn.execute("SELECT rule_override FROM libraries WHERE name='老库'").fetchone()
    assert again["rule_override"] is None


# --------------------------------------------------------------------------- 执行器真跑


def _insert_rename_library(db_mod, name: str, source: str, target: str, override: dict | None) -> int:
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO libraries(name,type,processor,processor_id,log_file,enabled,source_dir,"
            "target_dir,match_categories,match_tags,match_path_prefixes,orphan_cleanup,"
            "permission_mode,rule_override,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "Bangumi", "bangumi", "bangumi", None, 1, source, target,
             "[]", "[]", "[]", 0, 0,
             json.dumps(override, ensure_ascii=False) if override else None, stamp, stamp),
        )
        return int(cursor.lastrowid)


def test_executor_reads_the_override_from_the_library_row(
    executor_mod, db_mod, proc_registry, tmp_path
):
    """端到端：库级模板落库 → 跑任务 → 目标目录里的文件名按**它**改，而不是处理器的规则。"""
    proc_registry.ensure_builtins()
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    P.write_tree(source, TREE)
    P.write_tree(target, {"Show A/Season 1/ep01.mkv": 11})

    library_id = _insert_rename_library(
        db_mod,
        "覆盖真跑库",
        str(source),
        str(target),
        {
            "naming": {"template": "{title}__E{episode:02}{suffix}"},
            "traverse": {
                "reject_dirs": [],
                "accept_dirs": [{"pattern": r"^Season (1)$", "season_group": 1}],
            },
        },
    )
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        task_id = int(
            conn.execute(
                "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,"
                "result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (library_id, "覆盖真跑库", "rename", "manual", "queued",
                 json.dumps({"mode": "full"}), "{}", "{}", stamp),
            ).lastrowid
        )

    executor_mod.execute_task(task_id)

    row = db_mod.get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = json.loads(row["result"])
    assert row["status"] == "success", (dict(row), result)
    assert result["renamed"] == 1, result
    assert (target / "Show A/Season 1/Show A__E01.mkv").is_file()
    # 任务日志里明写"用的是本库规则"，用户不必猜。
    logs = [
        item["message"]
        for item in db_mod.get_conn().execute(
            "SELECT message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
    ]
    assert any("本库规则覆盖已生效" in text for text in logs), logs


# --------------------------------------------------- 执行器侧的可见性

#: 自定义处理器 id：内置 `bangumi` 的规格照抄，但**不配** `episode_fallback`。
#: 这就是"没解析出集数 ⇒ 保留原文件名"那条路径。
NO_FALLBACK_ID = "bangumi-nofallback"

#: L4「可见性」用例的处理器 id：与上面同形，但集数正则被抓窄成"首个数字"，
#: 好让两个**不同文件名**的源文件解析出**同一个**集数 ⇒ 渲染出同一个目标名。
COLLISION_ID = "bangumi-collision"

#: 未知变量名用例的处理器 id：保存期合法，之后再往 DB 里塞一个**未知变量名**
#: （模拟"库里存下的既有配置 / 手改过的 YAML"）。
UNKNOWN_PARAM_ID = "bangumi-unknownparam"


def _insert_library_with_processor(db_mod, name, source, target, processor_id):
    """与 ``_insert_rename_library`` 同形，但处理器 id 可指定（不写死 bangumi）。"""
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


def test_executor_makes_kept_unresolved_episode_visible(
    executor_mod, db_mod, proc_registry, proc_defaults, tmp_path
):
    """端到端：没解析出集数的文件**保留原名**，而且这件事在执行器侧处处可见。

    硬要求是"跳过不能静默"。执行器一共留了四处痕迹（本用例逐个钉住）：

    1. ``result["kept_unresolved_episode"]`` —— 计数（与 `renamed` / `rename_failed` 并列）；
    2. ``result["samples"]["kept_unresolved_episode"]`` —— 明细，路径**相对媒体库根目录**；
    3. ``result["warnings"]`` —— 一条中文汇总告警（"N 个文件没解析出集数…"）；
    4. 任务日志 —— 每个文件一行 ``Kept (no episode): …``（**WARNING**，不是 DEBUG），
       外加收尾摘要里的"保留原名（没解析出集数）N"。

    同时锚定"没有副作用"：目标侧那两条**都**还在（原有名保留 + 能解析的照旧改名）。
    """
    proc_registry.ensure_builtins()
    spec = proc_defaults.builtin_spec("bangumi")
    spec["id"] = NO_FALLBACK_ID
    spec["name"] = "番剧（不兜底）"
    spec["episode_fallback"] = None          # ← 唯一变化：不再把字符串 "None" 写进文件名
    spec["log_file"] = "banguminofallbackcache.log"
    proc_registry.create_processor(spec)

    source = tmp_path / "downloads"
    target = tmp_path / "library"
    tree = {"Show A/Season 1/Show.10.mkv": 220, "Show A/Season 1/ep05.mkv": 11}
    P.write_tree(source, tree)
    P.write_tree(target, tree)

    library_id = _insert_library_with_processor(
        db_mod, "不兜底库", str(source), str(target), NO_FALLBACK_ID
    )
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        task_id = int(
            conn.execute(
                "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,"
                "result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (library_id, "不兜底库", "rename", "manual", "queued",
                 json.dumps({"mode": "full"}), "{}", "{}", stamp),
            ).lastrowid
        )

    executor_mod.execute_task(task_id)

    row = db_mod.get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = json.loads(row["result"])
    assert row["status"] == "success", (dict(row), result)
    # 1 + 2：计数与明细（路径相对媒体库根目录）
    assert result["kept_unresolved_episode"] == 1, result
    assert result["samples"]["kept_unresolved_episode"] == ["Show A/Season 1/Show.10.mkv"], result
    # 3：汇总告警
    assert any("1 个文件没解析出集数" in text for text in result["warnings"]), result["warnings"]
    # 副作用：保留原名的那条还在原处；能解析出集数的那条照旧改名
    assert (target / "Show A/Season 1/Show.10.mkv").is_file()
    assert (target / "Show A/Season 1/Show A S01E05.mkv").is_file()
    assert result["renamed"] == 1, result
    # 4：任务日志（WARNING 的逐条行 + 收尾摘要）
    logs = db_mod.get_conn().execute(
        "SELECT level, message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    kept = [(row_["level"], row_["message"]) for row_ in logs
            if "Kept (no episode): " in str(row_["message"])]
    assert kept == [(
        "WARNING",
        "Kept (no episode): Show A/Season 1/Show.10.mkv",
    )], logs
    assert any("保留原名（没解析出集数）1" in str(row_["message"]) for row_ in logs), logs


def test_executor_makes_unknown_naming_variables_visible(
    executor_mod, db_mod, proc_registry, proc_defaults, tmp_path
):
    """端到端：命名里用了不存在的变量 ⇒ 改名照样成功，但**处处可见**。

    这条路径只能由"库里存下的既有配置 / 直接改数据库"造出来 —— 保存期
    （``registry._validate_or_raise``、库级覆盖的 ``resolve_override``）现在会拦住新写的错名字。
    执行器留了四处痕迹（与 ``kept_unresolved_episode`` 同形，本用例逐个钉住）：
    ``result["unknown_params"]`` 计数、``samples.unknown_params`` 明细、``warnings`` 汇总告警、
    任务日志每个文件一行 WARNING + 收尾摘要。
    """
    proc_registry.ensure_builtins()
    spec = proc_defaults.builtin_spec("bangumi")
    spec["id"] = UNKNOWN_PARAM_ID
    spec["name"] = "番剧（旧配置）"
    spec["log_file"] = "bangumiunknowncache.log"
    proc_registry.create_processor(spec)

    # 模拟"早就存进库里的旧配置"：绕过保存期校验，直接把坏名字写进 DB（运行时唯一真源）。
    legacy = dict(spec)
    legacy["naming"] = {
        **spec["naming"],
        "segments": [
            {"param": "no_such_name"},
            {"literal": "-"},
            {"param": "title"},
            {"param": "ext"},
        ],
    }
    with db_mod.tx() as conn:
        conn.execute(
            "UPDATE processors SET spec_json=? WHERE id=?",
            (json.dumps(legacy, ensure_ascii=False), UNKNOWN_PARAM_ID),
        )

    source = tmp_path / "downloads"
    target = tmp_path / "library"
    tree = {"Show A/Season 1/ep05.mkv": 11}
    P.write_tree(source, tree)
    P.write_tree(target, tree)

    library_id = _insert_library_with_processor(
        db_mod, "旧配置库", str(source), str(target), UNKNOWN_PARAM_ID
    )
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        task_id = int(
            conn.execute(
                "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,"
                "result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (library_id, "旧配置库", "rename", "manual", "queued",
                 json.dumps({"mode": "full"}), "{}", "{}", stamp),
            ).lastrowid
        )

    executor_mod.execute_task(task_id)

    row = db_mod.get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = json.loads(row["result"])
    assert row["status"] == "success", (dict(row), result)
    assert result["unknown_params"] == 1, result
    assert result["samples"]["unknown_params"] == [
        {"label": "Show A/Season 1/ep05.mkv", "file": "ep05.mkv", "params": ["no_such_name"]}
    ], result["samples"]["unknown_params"]
    assert any(
        "不存在的变量" in text and "no_such_name" in text for text in result["warnings"]
    ), result["warnings"]
    # 少了一段、但其余照拼 ⇒ 文件**改名成功**（老数据不许因为一个坏名字整轮失败）
    assert result["renamed"] == 1, result
    assert (target / "Show A/Season 1/-Show A.mkv").is_file()
    logs = db_mod.get_conn().execute(
        "SELECT level, message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    assert [
        row_["level"] for row_ in logs if "未知变量被留空" in str(row_["message"])
    ] == ["WARNING"], logs
    assert any("命名未知变量 1" in str(row_["message"]) for row_ in logs), logs


# --------------------------------------------------- L4：冲突与解析失败的可见性

def test_executor_makes_collisions_visible_and_keeps_every_file(
    executor_mod, db_mod, proc_registry, proc_defaults, tmp_path
):
    """端到端：两个文件撞同一个目标名 ⇒ 冲突**可计数、有明细、有告警**，且**一个文件都不丢**。

    这条用例针对的是"可见但行为不变（照旧覆盖）"这一阶段；L2 规则定型后行为**有意改变**，
    所以这里改成钉 L2 的完整账目（用户在任务详情页能看见的全部事实）：

    1. ``result["name_collisions"] == 1``（计数）；
    2. ``result["samples"]["name_collisions"]`` 非空，且样本形状是
       ``{dir,target,previous,current,kind}``、``kind == "within_run"``；
    3. 收尾摘要含「同名冲突 1」与「冲突加后缀 1」；
    4. ``result["renamed"] == 2``、``rename_failed == 0``、``renamed_with_suffix == 1``
       —— 两个文件都改名成功，小的那个加 `` (2)`` 保留（**不再分平台**：Windows 上
       ``os.rename`` 会因目标已存在而报错，但 L2 在执行前就把目标名定成了不同的两个）；
    5. 日志里有一条 **WARNING** 的「同名冲突：」行 + 一条 **WARNING** 的「冲突加后缀保留：」行。

    为什么用 ``episode_patterns: []`` + 显式模板：两个不同文件名的源文件都要把集数解析成
    同一个数字（`Show.05.a.mkv` 与 `Show.05.b.mkv` 的 `\\d+` 首个命中都是 `05`），
    这样渲染出的目标名逐字相同。
    """
    proc_registry.ensure_builtins()
    spec = proc_defaults.builtin_spec("bangumi")
    spec["id"] = COLLISION_ID
    spec["name"] = "番剧（同名冲突）"
    spec["episode_patterns"] = [{"name": "ep", "pattern": r"(?P<g1>\d+)"}]
    spec["episode_fallback"] = None
    spec["log_file"] = "bangumicollisioncache.log"
    spec["naming"] = {"segments": [{"param": "title"}, {"literal": " S01E"},
                                   {"param": "episode", "pad": 2}, {"param": "ext"}]}
    proc_registry.create_processor(spec)

    source = tmp_path / "downloads"
    target = tmp_path / "library"
    tree = {"Show A/Season 1/Show.05.a.mkv": 10, "Show A/Season 1/Show.05.b.mkv": 20}
    P.write_tree(source, tree)
    P.write_tree(target, tree)

    library_id = _insert_library_with_processor(
        db_mod, "冲突库", str(source), str(target), COLLISION_ID
    )
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        task_id = int(
            conn.execute(
                "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,"
                "result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (library_id, "冲突库", "rename", "manual", "queued",
                 json.dumps({"mode": "full"}), "{}", "{}", stamp),
            ).lastrowid
        )

    executor_mod.execute_task(task_id)

    row = db_mod.get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = json.loads(row["result"])
    assert row["status"] == "success", (dict(row), result)
    # 1：计数
    assert result["name_collisions"] == 1, result
    # 2：明细 + 样本形状
    collisions = result["samples"]["name_collisions"]
    assert len(collisions) == 1, result["samples"]
    collision = collisions[0]
    assert set(collision) == {"dir", "target", "previous", "current", "kind"}, collision
    assert collision["kind"] == "within_run", collision
    assert collision["target"] == "Show A S01E05.mkv", collision
    # previous = 抢先占住干净名的那个（L2 按体积择优 ⇒ `Show.05.b.mkv` 20 字节胜出），
    # current = 后来撞上它、只能加后缀的那个。
    assert collision["previous"] == "Show.05.b.mkv", collision
    assert collision["current"] == "Show.05.a.mkv", collision
    # 3（L2）：行为**有意改变** —— 两个文件都改名成功，小的那个加 ` (2)` 保留。
    #    与 L4 版本的关键区别：不再分平台。Windows 上 `os.rename` 的重复目标会报 WinError 183，
    #    但 L2 在**执行之前**就给两个文件定下了不同的最终名，那条报错路径根本不会走到。
    assert result["renamed"] == 2, result
    assert result["rename_failed"] == 0, result
    assert result["renamed_with_suffix"] == 1, result
    assert result["displaced_targets"] == 0, result
    suffix_samples = result["samples"]["renamed_with_suffix"]
    assert len(suffix_samples) == 1, result["samples"]
    assert suffix_samples[0]["reason"] == "within_run", suffix_samples
    assert suffix_samples[0]["old"] == "Show.05.a.mkv", suffix_samples
    assert suffix_samples[0]["target"] == "Show A S01E05 (2).mkv", suffix_samples
    # 两个文件都在盘上（一个都不丢），且名字各自唯一。
    assert (target / "Show A/Season 1/Show A S01E05.mkv").is_file()
    assert (target / "Show A/Season 1/Show A S01E05 (2).mkv").is_file()
    # 4：收尾摘要（同名冲突 + 冲突加后缀 两项账）
    logs = db_mod.get_conn().execute(
        "SELECT level, message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    assert any("同名冲突 1" in str(row_["message"]) for row_ in logs), logs
    assert any("冲突加后缀 1" in str(row_["message"]) for row_ in logs), logs
    # 5：一条 WARNING 的同名冲突行 + 一条 WARNING 的加后缀行（L2 后不再有"覆盖"）
    collision_logs = [(row_["level"], row_["message"]) for row_ in logs
                      if "同名冲突：" in str(row_["message"])]
    assert len(collision_logs) == 1, logs
    assert collision_logs[0][0] == "WARNING", collision_logs
    suffix_logs = [(row_["level"], row_["message"]) for row_ in logs
                   if "冲突加后缀保留：" in str(row_["message"])]
    assert len(suffix_logs) == 1, logs
    assert suffix_logs[0][0] == "WARNING", suffix_logs
