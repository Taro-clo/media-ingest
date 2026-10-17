"""处理器注册表（``processors`` 表 + 配置文件写出）的契约测试。

契约真源：``docs/contracts/PROCESSORS.md`` §3 / §6.2 / §7 / §10。覆盖对象：``app/processors/registry.py``。

DB 是运行时唯一真源；每次写操作都要在**同一请求内**把 ``<id>.yaml`` 原子写出；
删除保护（内置 / 仍被引用）必须给出中文 409 而不是静默删掉。
"""

from __future__ import annotations

import copy
import json

import pytest

import mlm_helpers as H


def _library(db, name: str, processor_id: str, *, log_file: str | None = None) -> int:
    stamp = db.now_iso()
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO libraries (name, type, processor_id, source_dir, target_dir, log_file,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                "Custom",
                processor_id,
                f"/downloads/{name}",
                f"/library/{name}",
                log_file,
                stamp,
                stamp,
            ),
        )
        return int(cursor.lastrowid)


def _custom(proc_registry, proc_defaults, processor_id="my-proc", name="我的处理器", **overrides):
    data = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    data.pop("builtin", None)
    data.pop("reference_script", None)
    data["id"] = processor_id
    data["name"] = name
    data.setdefault("log_file", f"{processor_id}cache.log")
    data.update(overrides)
    return proc_registry.create_processor(data)


def test_exception_types_map_to_http_status_codes(proc_registry):
    assert proc_registry.ProcessorNotFound.status_code == 404
    assert proc_registry.ProcessorProtected.status_code == 409
    assert proc_registry.ProcessorInUse.status_code == 409
    assert proc_registry.ProcessorInvalid.status_code == 400
    assert issubclass(proc_registry.ProcessorNotFound, proc_registry.RegistryError)
    exc = proc_registry.ProcessorInvalid("规格不合法：kind")
    assert exc.status_code == 400 and "kind" in str(exc)
    assert proc_registry.ProcessorInvalid("冲突", status_code=409).status_code == 409


def test_ensure_builtins_seeds_the_three_builtins_once(proc_registry, proc_defaults):
    proc_registry.ensure_builtins()

    listed = proc_registry.list_processors()
    ids = [item["id"] for item in listed]
    builtin_ids = [item["id"] for item in listed if item["builtin"]]
    # 排序契约：内置在前，且按 `defaults.BUILTIN_IDS` 的**展示顺序**（界面顺序，不是字母序）
    # —— `registry.list_processors` 在 SQL 之外还用 `defaults.BUILTIN_IDS` 的索引显式排序。
    assert builtin_ids == list(proc_defaults.BUILTIN_IDS)
    assert ids[: len(builtin_ids)] == builtin_ids
    assert proc_registry.ensure_builtins() == 0, "第二次必须什么都不补（幂等）"


def test_list_processors_filters_and_orders(proc_registry, proc_defaults):
    proc_registry.ensure_builtins()
    _custom(proc_registry, proc_defaults, "zz-custom", "Z")
    _custom(proc_registry, proc_defaults, "aa-custom", "A")

    everything = proc_registry.list_processors()
    ids = [item["id"] for item in everything]
    builtins = [item["id"] for item in everything if item["builtin"]]
    customs = [item["id"] for item in everything if not item["builtin"]]
    assert builtins == list(proc_defaults.BUILTIN_IDS), "内置按 BUILTIN_IDS 展示顺序（= hardlink_only, bangumi, movie）"
    assert ids[: len(builtins)] == builtins, "内置必须排在最前面"
    assert customs == sorted(customs) == ["aa-custom", "zz-custom"]

    assert [i["id"] for i in proc_registry.list_processors(builtin=True)] == list(proc_defaults.BUILTIN_IDS)
    assert set(i["id"] for i in proc_registry.list_processors(builtin=False)) == {"zz-custom", "aa-custom"}
    assert [i["id"] for i in proc_registry.list_processors(kind="hardlink_only")] == ["hardlink_only"]


def test_get_processor_returns_none_and_require_raises_404(proc_registry):
    assert proc_registry.get_processor("nope") is None
    assert proc_registry.spec_row("nope") is None
    with pytest.raises(proc_registry.ProcessorNotFound) as info:
        proc_registry.delete_processor("nope")
    assert info.value.status_code == 404
    assert "处理器不存在：nope" in str(info.value)


def test_create_processor_validates_id_and_writes_the_yaml(proc_registry, proc_defaults, config_dir):
    spec = _custom(proc_registry, proc_defaults, "new-proc", "新处理器")

    assert spec["builtin"] is False
    assert "reference_script" not in spec, "该字段已废除"
    stored = proc_registry.get_processor("new-proc")
    assert stored["name"] == "新处理器"
    assert stored["schema_version"] == 2
    assert stored["id"] == "new-proc" and stored["builtin"] is False

    target = config_dir / "processors" / "new-proc.yaml"
    assert target.is_file(), f"写操作必须同步写出 {target}"
    text = H.read_text(target)
    assert "新处理器" in text, "YAML 必须 allow_unicode（中文不能变成 \\uXXXX）"
    assert "\\u" not in text


def test_create_processor_rejects_bad_ids_and_duplicates(proc_registry, proc_defaults):
    _custom(proc_registry, proc_defaults, "dup-proc", "D")

    with pytest.raises(proc_registry.ProcessorInvalid) as info:
        _custom(proc_registry, proc_defaults, "dup-proc", "D2")
    assert info.value.status_code == 409 and "已存在" in str(info.value)

    with pytest.raises(proc_registry.ProcessorInvalid) as info:
        _custom(proc_registry, proc_defaults, "Bad-Id", "B")
    assert info.value.status_code == 400
    assert "id 不合法" in str(info.value)


def test_create_processor_rejects_an_invalid_spec(proc_registry, proc_defaults):
    data = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    data["id"] = "bad-spec"
    data["kind"] = "nonsense"
    with pytest.raises(proc_registry.ProcessorInvalid) as info:
        proc_registry.create_processor(data)
    assert "处理器规格不合法" in str(info.value)
    assert "kind" in str(info.value)


def test_create_processor_generates_its_own_log_file_and_never_borrows_a_builtin_one(
    proc_registry, proc_defaults
):
    """PROCESSORS §5.2：模板自带内置日志名 ⇒ 新建时**必须**换成自己的 ``<id>cache.log``。

    两个处理器共用一个日志会让「A 库处理过的文件夹」把 B 库静默跳过（体积一致分支），
    所以这里同时钉住两件事：不沿用内置名、不同处理器之间不撞名。用 **id** 而不是 name
    做 slug 也是刻意的 —— 中文名会被 ``slugify`` 折叠成通用的 ``library``。
    """
    builtin_names = {"bangumicache.log", "moviecache.log", "hard_links.log"}
    for template in ("bangumi", "movie"):
        data = copy.deepcopy(proc_defaults.builtin_spec(template))
        data.pop("builtin", None)
        data.pop("reference_script", None)
        data["id"] = f"own-{template}"
        data["name"] = "同名库"
        made = proc_registry.create_processor(data)
        assert made["log_file"] not in builtin_names, made["log_file"]
        assert made["log_file"] == f"own-{template}cache.log", made["log_file"]

    first = _custom(proc_registry, proc_defaults, "one-proc", "同名库", log_file=None)
    second = _custom(proc_registry, proc_defaults, "two-proc", "同名库", log_file=None)
    assert first["log_file"] == "one-proccache.log", first["log_file"]
    assert second["log_file"] == "two-proccache.log", second["log_file"]
    assert first["log_file"] != second["log_file"], "同名处理器必须拿到不同的日志文件名"


def test_update_processor_allows_rule_changes_even_for_builtins(proc_registry, proc_defaults):
    original = proc_registry.get_processor("bangumi")
    changed = copy.deepcopy(original)
    changed["naming"]["segments"] = [{"param": "title"}, {"literal": " X"}, {"param": "ext"}]

    updated = proc_registry.update_processor("bangumi", changed)

    assert updated["builtin"] is True, "内置身份不能因为改规则而丢失"
    assert updated["naming"]["segments"][-1] == {"param": "ext"}
    # `get_processor` 直接返回规格本身（id/name/kind/builtin 等被覆盖进同一层），没有 `["spec"]` 这层
    assert proc_registry.get_processor("bangumi")["naming"]["segments"] == changed["naming"]["segments"]

    proc_registry.reset_processor("bangumi", "default")
    assert proc_registry.get_processor("bangumi")["naming"]["segments"] == original["naming"]["segments"]


def test_update_processor_refuses_to_change_the_id(proc_registry, proc_defaults):
    _custom(proc_registry, proc_defaults, "keep-proc", "K")
    data = copy.deepcopy(proc_registry.get_processor("keep-proc"))
    data["id"] = "other-proc"

    with pytest.raises(proc_registry.ProcessorInvalid) as info:
        proc_registry.update_processor("keep-proc", data)
    assert "id 不可修改" in str(info.value)
    assert proc_registry.get_processor("other-proc") is None


def test_update_processor_keeps_the_old_log_file_when_omitted(proc_registry, proc_defaults):
    created = _custom(proc_registry, proc_defaults, "log-proc", "L")
    data = copy.deepcopy(created)
    data.pop("log_file")

    updated = proc_registry.update_processor("log-proc", data)
    assert updated["log_file"] == created["log_file"]


def test_delete_processor_protects_builtins(proc_registry):
    proc_registry.ensure_builtins()
    with pytest.raises(proc_registry.ProcessorProtected) as info:
        proc_registry.delete_processor("bangumi")
    assert info.value.status_code == 409
    assert "内置处理器，不能删除" in str(info.value)
    assert proc_registry.get_processor("bangumi") is not None


def test_delete_processor_is_blocked_while_libraries_reference_it(proc_registry, proc_defaults, db_mod):
    _custom(proc_registry, proc_defaults, "used-proc", "被用")
    _library(db_mod, "番剧 A", "used-proc")

    assert proc_registry.referencing_libraries("used-proc") == [{"id": 1, "name": "番剧 A"}]
    with pytest.raises(proc_registry.ProcessorInUse) as info:
        proc_registry.delete_processor("used-proc")
    assert info.value.status_code == 409
    assert "番剧 A(#1)" in str(info.value)


def test_delete_processor_removes_an_unused_custom_one_and_its_yaml(proc_registry, proc_defaults, config_dir):
    _custom(proc_registry, proc_defaults, "gone-proc", "G")
    target = config_dir / "processors" / "gone-proc.yaml"
    assert target.is_file()

    result = proc_registry.delete_processor("gone-proc")

    assert result == {"deleted": True, "id": "gone-proc"}
    assert proc_registry.get_processor("gone-proc") is None
    assert not target.exists(), "删除处理器也要删掉它的 YAML 镜像"


def test_reset_processor_only_works_for_builtins(proc_registry, proc_defaults):
    proc_registry.ensure_builtins()

    legacy = proc_registry.reset_processor("bangumi", "legacy")
    assert legacy == proc_defaults.preset_spec("bangumi", "legacy")

    default = proc_registry.reset_processor("movie", "default")
    assert default == proc_defaults.preset_spec("movie", "default")

    with pytest.raises(proc_registry.ProcessorInvalid) as info:
        proc_registry.reset_processor("bangumi", "nonsense")
    assert "未知预设" in str(info.value)

    _custom(proc_registry, proc_defaults, "custom-proc", "C")
    with pytest.raises(proc_registry.ProcessorInvalid) as info:
        proc_registry.reset_processor("custom-proc")
    assert "自定义处理器" in str(info.value)


def test_next_log_file_avoids_library_and_processor_log_files(proc_registry, proc_defaults, db_mod, log_dir):
    proc_registry.ensure_builtins()
    _library(db_mod, "番剧 A", "bangumi", log_file="bangumicache.log")
    _custom(proc_registry, proc_defaults, "own-proc", "Own", log_file="owncache.log")

    assert "bangumicache.log" in proc_registry.all_log_files()
    assert proc_registry.next_log_file("Bangumi") != "bangumicache.log"
    assert proc_registry.next_log_file("Own") != "owncache.log"

    taken = {json.loads(row["spec_json"]).get("log_file") for row in _processor_rows(db_mod) if row["spec_json"]}
    assert "owncache.log" in taken, "处理器里登记的 log_file 也要算占用"


def _processor_rows(db):
    with db.tx() as conn:
        return conn.execute("SELECT spec_json FROM processors").fetchall()


def test_spec_from_template_produces_a_valid_spec(proc_registry, proc_spec, proc_defaults):
    for template in ("bangumi", "movie", "blank", "hardlink_only"):
        spec = proc_registry.spec_from_template(template, f"{template}-x", f"模板 {template}", None)
        assert spec["id"] == f"{template}-x"
        assert spec["name"] == f"模板 {template}"
        assert proc_spec.validate_spec(spec) == [], (template, proc_spec.validate_spec(spec))

    overridden = proc_registry.spec_from_template("bangumi", "kind-x", "K", "hardlink_only")
    assert overridden["kind"] == "hardlink_only"


def test_ensure_custom_log_file_falls_back_to_slug(proc_registry, proc_spec):
    spec = proc_spec.normalize_spec({"id": "x-proc", "name": "My Library"})
    spec.pop("log_file", None)
    assert proc_registry.ensure_custom_log_file(spec) == "my-librarycache.log"


def test_config_sync_interval_reads_the_setting(proc_registry, db_mod):
    db_mod.set_setting("config_sync_interval_seconds", "7")
    assert proc_registry.config_sync_interval() == 7.0

    db_mod.set_setting("config_sync_interval_seconds", "nonsense")
    assert proc_registry.config_sync_interval() == 5.0
