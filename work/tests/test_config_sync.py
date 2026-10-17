"""配置双向同步（YAML 镜像 ↔ SQLite）的契约测试。

契约真源：``docs/contracts/PROCESSORS.md`` §6.1 / §6.2。覆盖对象：``app/configstore.py``。

三条不可动摇的语义：
1. **运行时真源 = SQLite**，`/data/config/**` 只是它的可移植镜像；每次成功写入都必须**原子**落盘
   （`*.yaml.tmp` → `os.replace`，不留临时文件），格式为 YAML 1.1、`schema_version: 2` 首行、
   UTF-8 无 BOM、中文不转义、正则写成单引号标量、`sort_keys=False`（键序稳定）。
2. 外部编辑（文件哈希 ≠ 应用写出的哈希）由后台 watcher 每 ``config_sync_interval_seconds`` 秒
   （默认 5，**只从 DB 设置表读**）导入；导入失败 ⇒ **绝不改 DB**，只写一条
   ``config_events(import-error)``，并经 ``snapshot()["errors"]`` 暴露给 WebUI；修好再导入成功后
   该数组自动清空（`errors` 表达"当前还有没有问题"，不是只追加的历史日志）。
3. 文件里消失的**非内置**处理器随导入从 DB 删除；内置处理器**永不删除**，只允许重置。
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import yaml

import mlm_helpers as H


def _library(
    db,
    name: str,
    processor_id: str,
    *,
    log_file: str | None = None,
    match_tags=(),
    rule_override: str | None = None,
) -> int:
    """插一条媒体库（只填 v2 必需的列 + 要断言的列）。"""
    stamp = db.now_iso()
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO libraries (name, type, processor_id, source_dir, target_dir, log_file,"
            " match_tags, rule_override, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                "Custom",
                processor_id,
                f"/downloads/{name}",
                f"/library/{name}",
                log_file,
                json.dumps(list(match_tags)),
                rule_override,
                stamp,
                stamp,
            ),
        )
        return int(cursor.lastrowid)


def _custom(proc_registry, proc_defaults, processor_id: str = "my-proc", name: str = "我的处理器"):
    """建一个自定义处理器（从 bangumi 内置规格复制，去掉内置专属字段）。"""
    data = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    data.pop("builtin", None)
    data.pop("reference_script", None)
    data["id"] = processor_id
    data["name"] = name
    data["log_file"] = f"{processor_id}cache.log"
    return proc_registry.create_processor(data)


def _processor_file(config_dir: Path, processor_id: str) -> Path:
    return config_dir / "processors" / f"{processor_id}.yaml"


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"要替换的片段不存在：{old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


# ── 写出方向（DB → 文件）──────────────────────────────────────────────────


def test_export_all_writes_a_yaml_mirror_atomically_and_leaves_no_tmp_files(
    configstore, config_dir, proc_registry, proc_defaults, db_mod
):
    proc_registry.ensure_builtins()
    _library(db_mod, "番剧", "bangumi", log_file="bangumicache.log")

    written = configstore.export_all()

    assert len(written) == 4, "内置三个处理器 + libraries.yaml 各写一次"
    libraries = config_dir / "libraries.yaml"
    assert libraries.is_file()
    assert {p.name for p in (config_dir / "processors").glob("*.yaml")} == {
        "bangumi.yaml",
        "hardlink_only.yaml",
        "movie.yaml",
    }
    assert not list(config_dir.rglob("*.tmp")), "原子写出不得留下 *.tmp"

    lib_text = libraries.read_text(encoding="utf-8")
    proc_text = _processor_file(config_dir, "bangumi").read_text(encoding="utf-8")
    for text in (lib_text, proc_text):
        assert text.splitlines()[0] == "schema_version: 2", "schema_version 必须是首行"
        assert "\\u" not in text, "中文不得被转义成 \\uXXXX"
        assert not text.startswith("\ufeff")
    assert b"\xe7\x95\xaa\xe5\x89\xa7" in libraries.read_bytes(), "UTF-8 无 BOM，中文原样"
    assert r"^Season (\d+)$" in proc_text, "正则必须原样写出（不被 YAML 转义/加引号）"
    assert proc_text.index("id:") < proc_text.index("name:"), "sort_keys=False：键序按规格稳定"
    # YAML 往返必须逐值保真：正则、表达式顺序、拒绝目录都不能被改写
    parsed = yaml.safe_load(proc_text)
    assert [item["pattern"] for item in parsed["traverse"]["accept_dirs"]] == [
        item["pattern"] for item in proc_defaults.builtin_spec("bangumi")["traverse"]["accept_dirs"]
    ]
    assert list(parsed["naming"]["expressions"]) == list(
        proc_defaults.builtin_spec("bangumi")["naming"]["expressions"]
    ), "naming.expressions 是有序求值的，键序必须原样保留"


def test_snapshot_marks_every_exported_file_in_sync_with_no_errors(
    configstore, config_dir, proc_registry, db_mod
):
    proc_registry.ensure_builtins()
    _library(db_mod, "番剧", "bangumi")
    configstore.export_all()

    snapshot = configstore.snapshot()

    assert Path(snapshot["config_dir"]).name == config_dir.name
    relatives = {str(item["relative"]).replace("\\", "/") for item in snapshot["files"]}
    assert relatives == {
        "libraries.yaml",
        "processors/bangumi.yaml",
        "processors/hardlink_only.yaml",
        "processors/movie.yaml",
    }
    for item in snapshot["files"]:
        assert item["exists"] is True
        assert item["in_sync"] is True, f"{item['relative']} 应处于同步状态（哈希与记录一致）"
        assert item["sha256"] and item["size"] > 0 and item["updated_at"]
    assert snapshot["errors"] == []
    assert snapshot["watcher_running"] is False


def test_libraries_yaml_round_trip_keeps_processor_id_log_file_and_match_tags(
    configstore, config_dir, proc_registry, db_mod
):
    proc_registry.ensure_builtins()
    library_id = _library(db_mod, "番剧", "bangumi", log_file="bangumicache.log", match_tags=["简体"])

    configstore.export_libraries()
    text = (config_dir / "libraries.yaml").read_text(encoding="utf-8")
    assert "processor_id: bangumi" in text
    assert "log_file: bangumicache.log" in text
    assert "简体" in text

    _edit(config_dir / "libraries.yaml", "- 简体", "- 繁体")
    result = configstore.import_file(config_dir / "libraries.yaml", force=True)

    assert result["ok"] is True and result["imported"]["kind"] == "libraries"
    row = db_mod.get_conn().execute(
        "SELECT match_tags, log_file FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    assert json.loads(row["match_tags"]) == ["繁体"]
    assert row["log_file"] == "bangumicache.log"


# ── 读入方向（文件 → DB）──────────────────────────────────────────────────


def test_an_external_edit_is_imported_and_the_hash_realigns_so_it_never_thrashes(
    configstore, config_dir, proc_registry, proc_defaults
):
    proc_registry.ensure_builtins()
    _custom(proc_registry, proc_defaults, "my-proc", "旧名字")
    configstore.export_all()
    path = _processor_file(config_dir, "my-proc")

    _edit(path, "旧名字", "新名字")
    assert configstore.snapshot()["errors"] == []

    result = configstore.reload()

    assert result["ok"] is True and result["errors"] == []
    assert any(item.get("kind") == "processor" for item in result["imported"]), result
    assert proc_registry.get_processor("my-proc")["name"] == "新名字"
    # 导入成功后回写一次规范化（§6.2 第 3 步）⇒ 所有文件重新 in_sync，第二轮不再导入
    assert all(item["in_sync"] for item in configstore.snapshot()["files"])
    assert configstore.reload()["imported"] == [], "哈希已对齐 ⇒ 第二次必须什么都不导入"


def test_the_watcher_imports_an_external_edit_within_two_cycles(
    configstore, config_dir, proc_registry, proc_defaults, db_mod
):
    db_mod.set_setting("config_sync_interval_seconds", "1")
    assert configstore.snapshot()["interval_seconds"] == 1.0
    proc_registry.ensure_builtins()
    _custom(proc_registry, proc_defaults, "my-proc", "旧名字")
    configstore.export_all()
    _edit(_processor_file(config_dir, "my-proc"), "旧名字", "新名字")

    assert configstore.start_watcher() is True
    try:
        H.wait_until(
            lambda: proc_registry.get_processor("my-proc")["name"] == "新名字",
            timeout=8.0,
            interval=0.1,
            message="后台 watcher 没有在 2 个周期内导入外部编辑",
        )
    finally:
        assert configstore.stop_watcher() is True

    assert configstore.snapshot()["errors"] == []
    assert all(item["in_sync"] for item in configstore.snapshot()["files"])


def test_interval_zero_turns_auto_import_off_while_manual_reload_still_works(
    configstore, config_dir, proc_registry, proc_defaults, db_mod
):
    db_mod.set_setting("config_sync_interval_seconds", "0")
    assert configstore.snapshot()["interval_seconds"] == 0.0
    proc_registry.ensure_builtins()
    _custom(proc_registry, proc_defaults, "my-proc", "旧名字")
    configstore.export_all()
    _edit(_processor_file(config_dir, "my-proc"), "旧名字", "新名字")

    configstore.start_watcher()
    try:
        time.sleep(1.5)
        assert proc_registry.get_processor("my-proc")["name"] == "旧名字", "周期为 0 ⇒ 不自动导入"
    finally:
        configstore.stop_watcher()

    assert configstore.reload()["ok"] is True
    assert proc_registry.get_processor("my-proc")["name"] == "新名字"


def test_a_broken_external_edit_never_touches_the_db_and_the_error_clears_after_a_good_import(
    configstore, config_dir, proc_registry, proc_defaults
):
    proc_registry.ensure_builtins()
    _custom(proc_registry, proc_defaults, "my-proc", "好名字")
    configstore.export_all()
    path = _processor_file(config_dir, "my-proc")
    good = path.read_text(encoding="utf-8")

    _edit(path, "kind: rename", "kind: nonsense")
    result = configstore.import_file(path, force=True)

    assert result["ok"] is False and result["imported"] is None
    assert result["errors"], "必须给出中文原因"
    assert "kind" in result["errors"][0]
    assert proc_registry.get_processor("my-proc")["name"] == "好名字", "导入失败绝不允许改 DB"
    errors = configstore.snapshot()["errors"]
    assert len(errors) == 1 and "my-proc.yaml" in errors[0]["path"] and errors[0]["message"]

    path.write_text(good, encoding="utf-8")
    assert configstore.import_file(path, force=True)["ok"] is True

    assert configstore.snapshot()["errors"] == [], "改对之后红色横幅必须自己消失"


def test_a_vanished_custom_processor_is_purged_from_the_db_but_builtins_are_never_deleted(
    configstore, config_dir, proc_registry, proc_defaults
):
    proc_registry.ensure_builtins()
    _custom(proc_registry, proc_defaults, "my-proc")
    configstore.export_all()

    _processor_file(config_dir, "my-proc").unlink()
    _processor_file(config_dir, "bangumi").unlink()

    result = configstore.reload()

    assert result["ok"] is True
    assert any(item.get("kind") == "processor-purge" and item.get("removed") == 1 for item in result["imported"]), result
    assert proc_registry.get_processor("my-proc") is None, "文件消失的非内置处理器必须从 DB 删掉"
    assert proc_registry.get_processor("bangumi")["builtin"] is True, "内置永不因文件缺失被删"


def test_import_file_on_a_missing_path_gives_a_chinese_error(configstore, config_dir):
    result = configstore.import_file(config_dir / "processors" / "nope.yaml")

    assert result["ok"] is False and result["imported"] is None
    assert "文件不存在" in result["errors"][0]


def test_start_and_stop_watcher_are_idempotent(configstore):
    assert configstore.stop_watcher() is False, "没在跑时 stop 返回 False"
    assert configstore.start_watcher() is True
    assert configstore.start_watcher() is False, "已在跑时 start 返回 False"
    assert configstore.watcher_running() is True
    assert configstore.stop_watcher() is True


# ── 库级规则覆盖（rule_override）的双向同步 ──────────────────────────
#
# 覆盖本身就是"用户手写的规则"，所以它必须在 libraries.yaml 里**可读可写**：
# 导出要写出人能看懂的模板原文，导入要把它校验回同一种归一化形态；写坏了必须报错且不动 DB。


def _yaml_of(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "libraries.yaml").read_text(encoding="utf-8"))


def _write_yaml(config_dir: Path, payload: dict) -> None:
    (config_dir / "libraries.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _override_row(db, library_id: int) -> str | None:
    return db.get_conn().execute(
        "SELECT rule_override FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()["rule_override"]


def test_libraries_yaml_round_trip_keeps_the_override_template_and_regexes(
    configstore, config_dir, proc_registry, db_mod
):
    proc_registry.ensure_builtins()
    override = json.dumps(
        {
            "traverse": {"reject_dirs": [{"pattern": "^SP$"}]},
            "naming": {"template": "[{season}-{episode}] {title}{suffix}"},
        },
        ensure_ascii=False,
    )
    library_id = _library(db_mod, "番剧", "bangumi", rule_override=override)

    configstore.export_libraries()
    payload = _yaml_of(config_dir)
    item = payload["libraries"][0]

    assert item["rule_override"]["naming"]["template"] == "[{season}-{episode}] {title}{suffix}"
    assert item["rule_override"]["traverse"]["reject_dirs"] == [{"pattern": "^SP$"}]
    assert item["rule_override"]["naming"]["segments"], "模板必须同时给出编译结果，引擎才不用认识模板"

    # 用户改模板 ⇒ 导入后 DB 里的模板原文和编译结果都要跟着变
    item["rule_override"]["naming"]["template"] = "{title} S{season}E{episode}{suffix}"
    _write_yaml(config_dir, payload)
    result = configstore.import_file(config_dir / "libraries.yaml", force=True)

    assert result["ok"] is True, result
    stored = json.loads(_override_row(db_mod, library_id))
    assert stored["naming"]["template"] == "{title} S{season}E{episode}{suffix}"
    assert stored["naming"]["segments"] == [
        {"param": "title"},
        {"literal": " S"},
        {"param": "season"},
        {"literal": "E"},
        {"param": "episode"},
        {"param": "suffix"},
    ], "导入后必须按新模板重新编译"
    assert stored["traverse"]["reject_dirs"] == [{"pattern": "^SP$"}], "没碰的字段必须原样保留"


def test_import_keeps_the_current_override_when_the_yaml_omits_the_key(
    configstore, config_dir, proc_registry, db_mod
):
    proc_registry.ensure_builtins()
    override = json.dumps({"extensions": [".ts"]})
    library_id = _library(db_mod, "电影", "bangumi", rule_override=override)

    configstore.export_libraries()
    payload = _yaml_of(config_dir)
    del payload["libraries"][0]["rule_override"]
    _write_yaml(config_dir, payload)

    assert configstore.import_file(config_dir / "libraries.yaml", force=True)["ok"] is True
    assert json.loads(_override_row(db_mod, library_id)) == {"extensions": [".ts"]}, (
        "手写 YAML 只关心别的字段时，不该顺手把覆盖清空"
    )

    payload = _yaml_of(config_dir)
    payload["libraries"][0]["rule_override"] = {}
    _write_yaml(config_dir, payload)

    assert configstore.import_file(config_dir / "libraries.yaml", force=True)["ok"] is True
    assert _override_row(db_mod, library_id) is None, "显式写 {} 才是清空覆盖"


def test_import_rejects_a_broken_override_and_never_touches_the_db(
    configstore, config_dir, proc_registry, db_mod
):
    proc_registry.ensure_builtins()
    override = json.dumps({"naming": {"template": "{title}{suffix}"}})
    library_id = _library(db_mod, "番剧", "bangumi", rule_override=override)
    configstore.export_libraries()

    # 三种坏法各来一次：未知顶层键、正则编译不了、未知模板变量
    broken = [
        {"rule_override": {"namin": {"template": "{title}"}}},
        {"rule_override": {"traverse": {"reject_dirs": [{"pattern": "([unclosed"}]}}},
        {"rule_override": {"naming": {"template": "{title} {episode_num}"}}},
    ]
    for payload in broken:
        full = _yaml_of(config_dir)
        full["libraries"][0]["rule_override"] = payload["rule_override"]
        _write_yaml(config_dir, full)

        result = configstore.import_file(config_dir / "libraries.yaml", force=True)

        assert result["ok"] is False, f"{payload} 必须被拒绝"
        assert "rule_override" in result["errors"][0]
        assert json.loads(_override_row(db_mod, library_id)) == json.loads(override), (
            "导入失败绝不允许改 DB"
        )
    assert configstore.watcher_running() is False
