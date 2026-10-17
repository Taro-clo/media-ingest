"""处理器规格（``ProcessorSpec``）解析 / 补默认 / 校验的契约测试。

契约真源：``docs/contracts/PROCESSORS.md`` §3。覆盖对象：``app/processors/spec.py``。

`validate_spec` 的契约是 **不抛异常、永远返回字段级中文错误列表**（空列表 = 合法），
字段名必须是点分路径（如 ``traverse.accept_dirs[0].pattern``），供 HTTP 层包成 422。
"""

from __future__ import annotations

import copy
import json

import pytest

import mlm_helpers as H


def _bangumi(proc_defaults):
    return copy.deepcopy(proc_defaults.builtin_spec("bangumi"))


def _fields(errors):
    return [item["field"] for item in errors]


def test_module_constants_match_the_frozen_contract(proc_spec):
    assert proc_spec.SCHEMA_VERSION == 2
    assert proc_spec.PROCESSOR_KINDS == ("rename", "hardlink_only")
    assert proc_spec.FILE_SCOPES == ("subdir", "title")
    assert proc_spec.SELECTION_MODES == ("file", "extension")
    assert proc_spec.LOG_FORMATS == ("json_cache",)
    assert set(proc_spec.SPEC_TEMPLATES) >= {"bangumi", "movie", "hardlink_only", "blank"}
    assert proc_spec.ID_PATTERN.match("bangumi")
    assert proc_spec.ID_PATTERN.match("my-proc_2")
    assert not proc_spec.ID_PATTERN.match("Bangumi")
    assert not proc_spec.ID_PATTERN.match("a")


def test_builtin_specs_validate_without_errors(proc_spec, proc_defaults):
    for processor_id in proc_defaults.BUILTIN_IDS:
        spec = copy.deepcopy(proc_defaults.builtin_spec(processor_id))
        assert proc_spec.validate_spec(spec) == [], (processor_id, proc_spec.validate_spec(spec))


def test_normalize_fills_missing_keys_without_reordering_known_ones(proc_spec):
    given = {
        "id": "custom",
        "name": "自定义",
        "kind": "rename",
        "extensions": [".mkv"],
        "naming": {"expressions": {}, "segments": [{"param": "title"}]},
    }
    data = proc_spec.normalize_spec(copy.deepcopy(given))

    # `given` 里已有键的**相对次序**必须保持不变（缺失键在末尾/开头补上都不算破坏）
    order = [key for key in data if key in given]
    assert order == list(given), f"已有键的相对顺序被改动了：{order} != {list(given)}"
    assert data["schema_version"] == 2
    for key in ("traverse", "episode_patterns", "episode_fallback", "selection", "log_file", "log_format"):
        assert key in data, f"normalize_spec 必须补齐 {key}"


def test_normalize_is_idempotent(proc_spec, proc_defaults):
    once = proc_spec.normalize_spec(copy.deepcopy(proc_defaults.builtin_spec("movie")))
    twice = proc_spec.normalize_spec(copy.deepcopy(once))
    assert once == twice


def test_normalize_never_validates_and_keeps_unknown_keys(proc_spec):
    data = proc_spec.normalize_spec({"id": "x1", "kind": "nonsense", "surprise": 1})
    assert data["kind"] == "nonsense", "normalize 不校验，非法值必须原样留给 validate_spec"
    assert data["surprise"] == 1
    errors = proc_spec.validate_spec(data)
    assert "surprise" in _fields(errors)
    assert "kind" in _fields(errors)


def test_normalize_forces_schema_version_to_two(proc_spec):
    assert proc_spec.normalize_spec({"schema_version": 99})["schema_version"] == 2


def test_normalize_handles_none_and_non_mapping(proc_spec):
    data = proc_spec.normalize_spec(None)
    assert isinstance(data, dict) and data["schema_version"] == 2
    assert proc_spec.validate_spec(None) == [{"field": "", "message": "spec 必须是一个对象（YAML 映射）。"}]
    assert _fields(proc_spec.validate_spec(["not", "a", "mapping"])) == [""]


def test_processor_spec_round_trip_and_convenience_properties(proc_spec, proc_defaults):
    data = proc_spec.normalize_spec(copy.deepcopy(proc_defaults.builtin_spec("bangumi")))
    spec = proc_spec.ProcessorSpec.from_dict(copy.deepcopy(data))

    assert spec.to_dict() == data
    assert spec.is_rename is True
    assert spec.file_scope == "subdir"

    movie = proc_spec.ProcessorSpec.from_dict(copy.deepcopy(proc_defaults.builtin_spec("movie")))
    assert movie.file_scope == "title"

    hardlink = proc_spec.ProcessorSpec.from_dict(copy.deepcopy(proc_defaults.builtin_spec("hardlink_only")))
    assert hardlink.is_rename is False


def test_spec_validation_error_carries_field_level_errors(proc_spec):
    errors = [{"field": "kind", "message": "kind 必须是 rename 或 hardlink_only。"}]
    exc = proc_spec.SpecValidationError(errors)
    assert exc.errors == errors
    assert "kind" in str(exc) and "kind 必须是" in str(exc)


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda s: s.pop("name"), "name"),
        (lambda s: s.update(name=None), "name"),
        (lambda s: s.pop("kind"), "kind"),
        (lambda s: s.update(kind="movie"), "kind"),
        (lambda s: s.update(enabled="yes"), "enabled"),
        (lambda s: s.pop("traverse"), "traverse"),
        (lambda s: s.update(traverse=None), "traverse"),
        (lambda s: s.pop("extensions"), "extensions"),
        (lambda s: s.update(extensions=[]), "extensions"),
        (lambda s: s.pop("naming"), "naming"),
        (lambda s: s.update(naming={}), "naming.expressions"),
        (lambda s: s.pop("selection"), "selection"),
        (lambda s: s.update(log_format="text"), "log_format"),
    ],
)
def test_required_top_level_fields_are_enforced(proc_spec, proc_defaults, mutate, field):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    mutate(spec)
    assert field in _fields(proc_spec.validate_spec(spec))


def test_log_file_is_not_a_required_top_level_field(proc_spec, proc_defaults):
    """`log_file` 是可选的（缺省交给 `registry.next_log_file` 生成），别把它算进必填集合。"""
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec.pop("log_file")
    assert _fields(proc_spec.validate_spec(spec)) == []


def test_unknown_top_level_field_is_reported_by_name(proc_spec, proc_defaults):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["mystery"] = 1
    errors = proc_spec.validate_spec(spec)
    assert {"field": "mystery", "message": "未知字段：mystery"} in errors


@pytest.mark.parametrize("version", [1, 3, None, True, "x", 2.0])
def test_schema_version_must_be_exactly_two(proc_spec, proc_defaults, version):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["schema_version"] = version
    assert "schema_version" in _fields(proc_spec.validate_spec(spec))


def test_schema_version_two_is_accepted_verbatim(proc_spec, proc_defaults):
    """`2` 与 YAML 引号形式的 `'2'` 都接受（实现按 int() 比较，宽容引号标量）。"""
    for version in (2, "2"):
        spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
        spec["schema_version"] = version
        assert "schema_version" not in _fields(proc_spec.validate_spec(spec)), version


def test_reference_script_is_a_retired_field_and_is_silently_dropped(proc_spec, proc_defaults):
    """`reference_script` 已废除 —— 老配置带这个键不再算「未知字段」。

    老 DB 行 / 老 YAML / 老客户端都可能仍然带着它，因此校验必须放行、归一化必须丢掉，
    不能让一次升级因为一个已经删掉的字段而炸。
    """
    for value in ("v1-legacy.py", 123, None):
        spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
        spec["reference_script"] = value
        assert proc_spec.validate_spec(spec) == [], value
        assert "reference_script" not in proc_spec.normalize_spec(spec), value


def test_traverse_rules_are_checked_field_by_field(proc_spec, proc_defaults):
    def errors(**traverse):
        spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
        spec["traverse"].update(traverse)
        return _fields(proc_spec.validate_spec(spec))

    assert "traverse.title_depth" in errors(title_depth=2)
    assert "traverse.file_scope" in errors(file_scope="nested")
    assert "traverse.skip_season_zero" in errors(skip_season_zero="yes")
    assert errors(accept_dirs=[{"pattern": "^Season (\\d+)$"}]) == [], "subdir 语义下 accept_dirs 合法"


def test_title_scope_rejects_accept_dirs(proc_spec, proc_defaults):
    spec = copy.deepcopy(proc_defaults.builtin_spec("movie"))
    spec["traverse"]["accept_dirs"] = [{"pattern": "^Season (\\d+)$"}]
    assert "traverse.accept_dirs" in _fields(proc_spec.validate_spec(spec))


def test_extensions_must_start_with_a_dot(proc_spec, proc_defaults):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["extensions"] = [".mkv", "mp4"]
    errors = proc_spec.validate_spec(spec)
    assert "extensions[1]" in _fields(errors)


def test_expressions_are_compiled_at_validation_time(proc_spec, proc_defaults):
    def errors(expressions):
        spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
        spec["naming"]["expressions"] = expressions
        return proc_spec.validate_spec(spec)

    assert _fields(errors({"ok": "1 + 1"})) == []
    assert "naming.expressions.bad name" in _fields(errors({"bad name": "1"}))
    assert "naming.expressions.n" in _fields(errors({"n": 1}))
    assert "naming.expressions.broken" in _fields(errors({"broken": "1 +"}))
    # 校验期只编译**语法**：语法合法、运行期才炸的表达式必须放过（运行期由引擎记 warning）
    assert _fields(errors({"r": "re.compile('(')"})) == []


def test_segments_are_validated_including_index_paths(proc_spec, proc_defaults):
    def errors(segments):
        spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
        spec["naming"]["segments"] = segments
        return _fields(proc_spec.validate_spec(spec))

    assert "naming.segments" in errors([])
    assert "naming.segments[0]" in errors(["not an object"])
    assert "naming.segments[0].weird" in errors([{"param": "title", "weird": 1}])
    assert "naming.segments[0]" in errors([{"param": "title", "literal": "x"}])
    assert "naming.segments[0]" in errors([{}])
    assert "naming.segments[0].param" in errors([{"param": 1}])
    assert "naming.segments[0].literal" in errors([{"literal": 1}])
    assert "naming.segments[0].pad" in errors([{"param": "episode", "pad": 0}])


def test_episode_patterns_are_validated_including_regex_compilation(proc_spec, proc_defaults):
    def errors(patterns):
        spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
        spec["episode_patterns"] = patterns
        return _fields(proc_spec.validate_spec(spec))

    assert "episode_patterns" in errors("nope")
    assert "episode_patterns[0]" in errors(["nope"])
    assert "episode_patterns[0].extra" in errors([{"name": "a", "pattern": "x", "extra": 1}])
    assert "episode_patterns[0].name" in errors([{"pattern": "x"}])
    assert "episode_patterns[0].pattern" in errors([{"name": "a"}])
    assert "episode_patterns[0].pattern" in errors([{"name": "a", "pattern": "("}])
    assert errors([{"name": "a", "pattern": "x"}]) == []


def test_episode_fallback_must_be_a_string_or_null(proc_spec, proc_defaults):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["episode_fallback"] = 0
    assert "episode_fallback" in _fields(proc_spec.validate_spec(spec))
    spec["episode_fallback"] = None
    assert "episode_fallback" not in _fields(proc_spec.validate_spec(spec))
    spec["episode_fallback"] = "None"
    assert "episode_fallback" not in _fields(proc_spec.validate_spec(spec))


def test_blank_episode_fallback_is_normalized_to_null(proc_spec, proc_defaults):
    """清空「集数兜底」输入框会提交**空串** —— 空串与 null 等价。

    历史上空串被当成"已配置的兜底字符串"，于是 `{episode}` 渲染成 `00`（`S01E00.mkv`）。
    归一放在 ``normalize_spec``（真源）里，运行期另有一道保险（见 test_processor_engine.py）。
    """
    spec = _bangumi(proc_defaults)

    spec["episode_fallback"] = ""
    assert proc_spec.normalize_spec(spec)["episode_fallback"] is None
    spec["episode_fallback"] = "   "
    assert proc_spec.normalize_spec(spec)["episode_fallback"] is None
    spec["episode_fallback"] = "None"
    assert proc_spec.normalize_spec(spec)["episode_fallback"] == "None", "非空字符串原样保留"
    spec["episode_fallback"] = None
    assert proc_spec.normalize_spec(spec)["episode_fallback"] is None
    spec["episode_fallback"] = 0
    assert proc_spec.normalize_spec(spec)["episode_fallback"] == 0, "非字符串照旧留给校验去报错"


def test_segment_param_names_are_validated_against_the_context(proc_spec, proc_defaults):
    """保存期挡住「段里写了上下文里没有的变量名」。

    可用名单 = ``TEMPLATE_VARS`` + 引擎内部键（``episode_resolved``）+ 本 spec
    ``naming.expressions`` 里定义的名字；**运行路径**用 ``strict_names=False`` 放宽
    （库里存下的既有配置、手改的 YAML 必须能照旧跑完，由引擎留痕）。
    """

    def spec_with(naming):
        spec = _bangumi(proc_defaults)
        spec["naming"] = {**spec["naming"], **naming}
        return spec

    unknown = proc_spec.validate_spec(spec_with({"segments": [{"param": "no_such_name"}]}))
    assert "naming.segments[0].param" in _fields(unknown)
    assert "未知变量" in unknown[0]["message"] and "no_such_name" in unknown[0]["message"]

    empty = proc_spec.validate_spec(spec_with({"segments": [{"param": ""}]}))
    assert "naming.segments[0].param" in _fields(empty)
    assert "空字符串" in empty[0]["message"], empty[0]["message"]

    assert proc_spec.validate_spec(spec_with({"segments": [{"param": "episode_resolved"}]})) == [], (
        "episode_resolved 是引擎内部键，段序列能读到"
    )
    assert proc_spec.validate_spec(
        spec_with({"expressions": {"custom": "'x'"}, "segments": [{"param": "custom"}]})
    ) == [], "表达式定义的名字算认识"

    assert (
        proc_spec.validate_spec(
            spec_with({"segments": [{"param": "no_such_name"}]}), strict_names=False
        )
        == []
    ), "运行路径放宽"
    assert (
        proc_spec.validate_spec(spec_with({"segments": [{"param": ""}]}), strict_names=False) == []
    ), "运行路径放宽（空名字由引擎的『空文件名』兜底处理）"


def test_known_context_names_merges_the_three_sources(proc_spec, proc_defaults):
    names = proc_spec.known_context_names({"custom": "'x'", "title": "'y'"})
    assert names[: len(proc_spec.TEMPLATE_VARS)] == proc_spec.TEMPLATE_VARS, "变量表保持原顺序"
    assert "episode_resolved" in names
    assert "custom" in names
    assert names.count("title") == 1, "与变量表重名的表达式不重复"
    assert proc_spec.known_context_names()[-1] == "episode_resolved"


def test_the_builtin_bangumi_preset_pair_differs_only_in_the_registered_increments(proc_defaults):
    """规格层钉住预设契约（PROCESSORS.md §3 / COMPATIBILITY.md **B4**）。

    四件事：
    * **推荐默认**（`builtin_spec()` / `builtin_specs()` / `template_spec()` 播种/新建处理器用的那份）
      的 `episode_fallback` 是 `None` ⇒ 没解析出集数时**保留原文件名**（不再造 `… S01ENone.mkv`）；
    * **推荐默认**的 `episode_patterns` 是 9 条有序新规则（`BANGUMI_EPISODE_PATTERNS`），
      而 `legacy` 逐字复刻最朴素的旧口径的两条集数正则（`BANGUMI_LEGACY_EPISODE_PATTERNS`）；
    * `legacy` 复刻最朴素的旧口径的**字符串** `"None"` 兜底 ⇒ 逐字节 golden 不变；
    * `LEGACY_OVERRIDES` 的**每一项**都必须在 `preset_spec(..., "legacy")` 的结果里生效
      —— 这张表是"legacy 相对 default 有哪些差异"的唯一真源，以后再加差异不会被漏掉，
      也不会出现"表里加了、`preset_spec` 没应用"的静默漂移（B4 依赖它）。
    """
    # ① 播种/新建用的那份规格：留空
    assert proc_defaults.builtin_spec("bangumi")["episode_fallback"] is None
    assert proc_defaults.builtin_specs()["bangumi"]["episode_fallback"] is None
    assert proc_defaults.template_spec("bangumi")["episode_fallback"] is None

    # ①' 播种/新建用的那份规格：9 条有序新规则（名字与顺序都不能变 —— 顺序即优先级）
    expected_names = [
        "chapter", "episode_word", "season_episode", "bracketed",
        "ep_marker", "hash_marker", "dash", "bare_e", "bare_number",
    ]
    for seed in (
        proc_defaults.builtin_spec("bangumi"),
        proc_defaults.builtin_specs()["bangumi"],
        proc_defaults.template_spec("bangumi"),
    ):
        assert [item["name"] for item in seed["episode_patterns"]] == expected_names

    # ② 两套预设：default 留空 / legacy 是旧口径的字符串
    default = proc_defaults.preset_spec("bangumi", "default")
    legacy = proc_defaults.preset_spec("bangumi", "legacy")
    assert default["episode_fallback"] is None
    assert legacy["episode_fallback"] == "None"

    # ②' 预设层的集数规则：default 是新规则，legacy 是旧口径那两条（顺序也不能变）
    assert default["episode_patterns"] == proc_defaults.BANGUMI_EPISODE_PATTERNS
    assert legacy["episode_patterns"] == proc_defaults.BANGUMI_LEGACY_EPISODE_PATTERNS
    assert [item["name"] for item in legacy["episode_patterns"]] == ["ep", "trailing"]

    # ③ 逐项核对 LEGACY_OVERRIDES：legacy 里等于表里的值，且 default 与它**不同**
    #   （"不同"是 B4 的实质：表里登记一项就必须真的产生一项差异）
    assert set(proc_defaults.LEGACY_OVERRIDES) == {
        "traverse.reject_dirs",
        "episode_fallback",
        "episode_patterns",
    }
    for path, value in proc_defaults.LEGACY_OVERRIDES.items():
        node_default, node_legacy = default, legacy
        for key in path.split("."):
            node_default, node_legacy = node_default[key], node_legacy[key]
        assert node_legacy == value, f"{path} 没有在 legacy 预设里生效"
        assert node_default != value, f"{path} 在 default 与 legacy 里必须真的不同"

    # ④ movie / hardlink_only 只是被写上 "None" 与旧口径那两条正则
    #   （它们的 naming 不引用集数 ⇒ 行为不变；写回只是让"legacy = 旧口径那套字段值"处处成立）
    for processor_id in ("movie", "hardlink_only"):
        legacy_other = proc_defaults.preset_spec(processor_id, "legacy")
        default_other = proc_defaults.preset_spec(processor_id, "default")
        assert legacy_other["episode_fallback"] == "None"
        assert default_other["episode_fallback"] is None
        assert legacy_other["episode_patterns"] == proc_defaults.BANGUMI_LEGACY_EPISODE_PATTERNS
        assert default_other["episode_patterns"] == []


def test_selection_mode_is_enumerated(proc_spec, proc_defaults):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["selection"]["per"] = "biggest"
    assert "selection.per" in _fields(proc_spec.validate_spec(spec))
    spec["selection"]["per"] = "extension"
    assert "selection.per" not in _fields(proc_spec.validate_spec(spec))


@pytest.mark.parametrize("log_file", ["sub/bangumicache.log", "sub\\bangumicache.log", "..", "../x.log"])
def test_log_file_must_be_a_bare_file_name(proc_spec, proc_defaults, log_file):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["log_file"] = log_file
    assert "log_file" in _fields(proc_spec.validate_spec(spec))


def test_log_file_is_optional_and_empty_means_let_the_server_generate(proc_spec, proc_defaults):
    """`log_file` 不是必填：缺省/空串/None 都合法 —— 由 `registry.next_log_file` 生成文件名。"""
    for value in ("", None):
        spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
        spec["log_file"] = value
        assert _fields(proc_spec.validate_spec(spec)) == []
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec.pop("log_file")
    assert _fields(proc_spec.validate_spec(spec)) == []
    # 非字符串仍要报错
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["log_file"] = 1024
    assert "log_file" in _fields(proc_spec.validate_spec(spec))


def test_dir_rules_are_validated_with_index_paths(proc_spec, proc_defaults):
    def errors(key, value, *, kind="bangumi"):
        spec = copy.deepcopy(proc_defaults.builtin_spec(kind))
        spec["traverse"][key] = value
        return _fields(proc_spec.validate_spec(spec))

    assert "traverse.reject_dirs" in errors("reject_dirs", "nope")
    assert "traverse.reject_dirs[0]" in errors("reject_dirs", ["nope"])
    assert "traverse.reject_dirs[0].pattern" in errors("reject_dirs", [{}])
    assert "traverse.reject_dirs[0].pattern" in errors("reject_dirs", [{"pattern": "("}])
    assert "traverse.accept_dirs[0].season_group" in errors(
        "accept_dirs", [{"pattern": "^Season (\\d+)$", "season_group": "x"}]
    )
    assert errors("reject_dirs", []) == []
    # 用实际媒体库里出现过的写法，别再让编造的 `[ANi]` 出现在仓库里
    assert errors("reject_dirs", [{"pattern": r"(?i)^scans?( \(vol\.\d+\))?$"}]) == []


def test_require_id_flag_is_honoured(proc_spec, proc_defaults):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec.pop("id")
    assert "id" in _fields(proc_spec.validate_spec(spec, require_id=True))
    assert "id" not in _fields(proc_spec.validate_spec(spec, require_id=False))

    spec["id"] = "Bad Id"
    errors = proc_spec.validate_spec(spec, require_id=True)
    assert "id" in _fields(errors)
    assert any("必须匹配" in item["message"] for item in errors if item["field"] == "id")


def test_validation_never_mutates_its_argument(proc_spec, proc_defaults):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["id"] = "BadId"
    before = json.dumps(spec, ensure_ascii=False, sort_keys=False)
    proc_spec.validate_spec(spec)
    assert json.dumps(spec, ensure_ascii=False, sort_keys=False) == before


def test_slugify_id_folds_to_the_id_pattern(proc_spec):
    assert proc_spec.slugify_id("My Processor!!") == "my-processor"
    assert proc_spec.slugify_id("番剧") in ("processor", "library", "")
    slug = proc_spec.slugify_id("A Very  Long   Name")
    assert slug == "a-very-long-name"


def _accept_dirs_errors(proc_spec, proc_defaults, accept_dirs):
    spec = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    spec["traverse"]["accept_dirs"] = accept_dirs
    errors = proc_spec.validate_spec(spec)
    return [item for item in errors if item["field"].startswith("traverse.accept_dirs")]


def test_accept_dirs_without_a_capture_group_must_say_season_group_zero(proc_spec, proc_defaults):
    """用例 A：无捕获组且不写 `season_group` ⇒ 仍 422，且消息必须给出两条出路（可操作）。"""
    errors = _accept_dirs_errors(proc_spec, proc_defaults, [{"pattern": r"^S\d+$"}])
    assert [item["field"] for item in errors] == ["traverse.accept_dirs[0].season_group"]
    message = errors[0]["message"]
    # 缺省的 season_group 仍是 1（accept_dirs 的捕获组就是季号），所以这里必须报错，
    # 但报错文本要直接告诉用户「不想取季号就显式写 0」或「给正则加一个捕获组」。
    assert "season_group=1" in message
    assert "0 个组" in message
    assert '"season_group": 0' in message
    assert "加一个捕获组" in message


@pytest.mark.parametrize(
    "accept_dirs",
    [
        [{"pattern": r"^S\d+$", "season_group": 0}],
        [{"pattern": r"^Season (\d+)$", "season_group": 0}],
        [{"pattern": r"^Season (\d+)$"}],
        [{"pattern": r"^Season (\d+)$", "season_group": 1}],
        [{"pattern": r"^Season (\d)$", "season_group": 1}],
        [],
    ],
)
def test_season_group_zero_and_in_range_values_validate(proc_spec, proc_defaults, accept_dirs):
    """用例 B 的前半：`season_group: 0` 合法（不取季号），1 在范围内也合法，空列表照样合法。"""
    assert _accept_dirs_errors(proc_spec, proc_defaults, accept_dirs) == []


@pytest.mark.parametrize(
    "accept_dirs",
    [
        [{"pattern": r"^S\d+$", "season_group": 1}],  # 用例 C：0 个组却要取第 1 组
        [{"pattern": r"^Season (\d)$", "season_group": 2}],  # 用例 D：1 个组却要取第 2 组
    ],
)
def test_season_group_out_of_range_is_still_rejected(proc_spec, proc_defaults, accept_dirs):
    """用例 C/D：放宽 `0` **不等于**放宽越界，防"放宽过头"。"""
    errors = _accept_dirs_errors(proc_spec, proc_defaults, accept_dirs)
    assert [item["field"] for item in errors] == ["traverse.accept_dirs[0].season_group"]
    assert "超出正则的捕获组数量" in errors[0]["message"]
    assert '"season_group": 0' in errors[0]["message"]


@pytest.mark.parametrize("season_group", [-1, -3])
def test_negative_season_group_is_rejected(proc_spec, proc_defaults, season_group):
    """负数不是"不取季号"（那是 0），必须报「必须是 0 或 >=1 的整数」。"""
    errors = _accept_dirs_errors(
        proc_spec, proc_defaults, [{"pattern": r"^S\d+$", "season_group": season_group}]
    )
    assert [item["field"] for item in errors] == ["traverse.accept_dirs[0].season_group"]
    assert "0 或 >=1" in errors[0]["message"]


def test_processor_specs_serialize_to_yaml_friendly_plain_data(proc_spec, proc_defaults):
    data = proc_spec.normalize_spec(copy.deepcopy(proc_defaults.builtin_spec("movie")))
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data
    assert isinstance(data["naming"]["expressions"], dict)
    assert isinstance(data["naming"]["segments"], list)
