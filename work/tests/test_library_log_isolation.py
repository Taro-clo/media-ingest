"""一个媒体库一份重命名日志（PROCESSORS §5.2）的回归测试。

**为什么这条契约是硬要求**：重命名日志（JSON cache）不只是审计记录，它就是「这个标题文件夹
已经处理过、体积是多少」的**唯一账本** —— ``app/processors/engine.py:344`` 读它、``:408`` 写它。
所以同一个处理器下两个库**共用一份日志**时，A 库处理完的标题文件夹会让 B 库走「体积一致 ⇒
已处理」分支被**静默跳过**（用户看到"B 库跑了但什么都没做"）。这直接违背「每个媒体库一份日志」的要求：
「每个媒体库各自独立的重命名日志」。

三条要钉住的行为：
1. 建库不传 ``log_file`` ⇒ 由服务端按处理器基名分配**唯一**名（第一个 ``bangumicache.log``、
   第二个 ``bangumicache-2.log``）；
2. 第二库对**同名同体积**的标题文件夹执行时**不得**被第一库的日志判成「已处理」；
3. 历史库回填把每行都补成互不相同的名字（两个纯中文库名否则会折叠成同一个 ``librarycache.log``）。
"""

from __future__ import annotations

import copy
from pathlib import Path

import processor_helpers as P


def _create_library(client, source_dir, target_dir, **overrides) -> dict:
    body = {
        "name": overrides.pop("name", "番剧"),
        "type": "Bangumi",
        "processor_id": "bangumi",
        "source_dir": str(source_dir),
        "target_dir": str(target_dir),
    }
    body.update(overrides)
    response = client.post("/api/libraries", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _dirs(source_dir: Path, target_dir: Path, tag: str) -> tuple[Path, Path]:
    source = source_dir / tag
    target = target_dir / tag
    source.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    return source, target


def test_two_libraries_on_the_same_processor_get_distinct_log_files(
    api_client, api_login, source_dir, target_dir
):
    api_login(api_client)
    source_a, target_a = _dirs(source_dir, target_dir, "a")
    source_b, target_b = _dirs(source_dir, target_dir, "b")

    first = _create_library(api_client, source_a, target_a, name="番剧 A")
    second = _create_library(api_client, source_b, target_b, name="番剧 B")

    assert first["log_file"] == "bangumicache.log", "第一个库沿用处理器基名（保住 v1 历史日志）"
    assert second["log_file"] == "bangumicache-2.log", "第二个库必须自动改名，不能共用"
    assert first["log_file"] != second["log_file"]
    assert Path(first["log_path"]).parent == Path(second["log_path"]).parent
    assert Path(first["log_path"]).name == first["log_file"]


def test_the_second_library_is_not_fooled_by_the_first_librarys_processed_mark(
    api_client, api_login, source_dir, target_dir, proc_defaults, proc_engine
):
    api_login(api_client)
    source_a, target_a = _dirs(source_dir, target_dir, "a")
    source_b, target_b = _dirs(source_dir, target_dir, "b")
    first = _create_library(api_client, source_a, target_a, name="番剧 A")
    second = _create_library(api_client, source_b, target_b, name="番剧 B")

    # 两个库各自摆一棵**同名同体积**的标题文件夹树（Show A/Show B/… 完全一致）
    P.write_tree(source_a, P.BANGUMI_TREE)
    P.write_tree(source_b, P.BANGUMI_TREE)
    spec = proc_defaults.preset_spec("bangumi", "legacy")

    assert P.log_keys(second["log_path"]) == set(), "第二库的日志文件起步必须是空的"
    outcome_a, _ = P.call_engine(proc_engine, spec, source_a, log_file=first["log_path"])
    assert P.outcome_renamed(outcome_a), "第一库应当真的改名"
    assert P.log_keys(second["log_path"]) == set(), "第一库的记账绝不能写进第二库的日志"

    outcome_b, _ = P.call_engine(proc_engine, spec, source_b, log_file=second["log_path"])

    assert P.outcome_renamed(outcome_b), "第二库不得被第一库的日志判成「已处理」而静默跳过"
    assert not outcome_b.skipped, f"第二库不应该跳过任何文件夹：{outcome_b.skipped}"
    assert P.log_keys(first["log_path"]) == P.log_keys(second["log_path"]) != set(), (
        "两库各自记下了同一批标题文件夹 ⇒ 隔离生效且都真的跑过"
    )


def test_an_explicit_log_file_colliding_with_another_library_is_rejected(
    api_client, api_login, source_dir, target_dir
):
    api_login(api_client)
    source_a, target_a = _dirs(source_dir, target_dir, "a")
    source_b, target_b = _dirs(source_dir, target_dir, "b")
    _create_library(api_client, source_a, target_a, name="番剧 A")

    response = api_client.post(
        "/api/libraries",
        json={
            "name": "番剧 B",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(source_b),
            "target_dir": str(target_b),
            "log_file": "bangumicache.log",
        },
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail[0]["loc"][-1] == "log_file"
    assert "已被其它媒体库或处理器占用" in detail[0]["msg"]
    assert "bangumicache-2.log" in detail[0]["msg"], "报错要告诉用户下一个可用名"


def test_a_path_like_log_file_is_still_rejected(
    api_client, api_login, source_dir, target_dir
):
    api_login(api_client)
    source_a, target_a = _dirs(source_dir, target_dir, "a")

    response = api_client.post(
        "/api/libraries",
        json={
            "name": "番剧 C",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(source_a),
            "target_dir": str(target_a),
            "log_file": "logs/我的.log",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"][-1] == "log_file"


def test_the_v1_to_v2_backfill_gives_every_library_its_own_log_file(
    db_mod, proc_registry, proc_defaults
):
    proc_registry.ensure_builtins()
    custom = copy.deepcopy(proc_defaults.builtin_spec("bangumi"))
    custom.pop("builtin", None)
    custom.pop("reference_script", None)
    custom["id"] = "my-proc"
    custom["name"] = "自定义处理器"
    custom["log_file"] = "my-proccache.log"
    proc_registry.create_processor(custom)

    # v1 遗留形态：processor_id 为空、processor 列有名、log_file 为 NULL
    rows = [
        ("番剧一", "bangumi"),
        ("番剧二", "bangumi"),
        ("电影", "movie"),
        ("硬链接", "hardlink_only"),
        ("中文库一", "my-proc"),
        ("中文库二", "my-proc"),
    ]
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        for name, legacy in rows:
            conn.execute(
                "INSERT INTO libraries (name, type, processor_id, processor, log_file,"
                " source_dir, target_dir, created_at, updated_at) VALUES (?,?,?,?,NULL,?,?,?,?)",
                (name, "Custom", "", legacy, f"/downloads/{name}", f"/library/{name}", stamp, stamp),
            )

    fixed = db_mod._backfill_library_columns()

    assert fixed == len(rows)
    with db_mod.tx() as conn:
        got = conn.execute(
            "SELECT name, processor_id, log_file FROM libraries ORDER BY id ASC"
        ).fetchall()
    mapping = {row["name"]: (row["processor_id"], row["log_file"]) for row in got}
    assert mapping["番剧一"] == ("bangumi", "bangumicache.log")
    assert mapping["番剧二"] == ("bangumi", "bangumicache-2.log")
    assert mapping["电影"] == ("movie", "moviecache.log")
    assert mapping["硬链接"] == ("hardlink_only", "hardlink_only.log")
    # 两个纯中文库名 slugify 后都是 library ⇒ 第二个必须拿到 -2
    assert mapping["中文库一"] == ("my-proc", "librarycache.log")
    assert mapping["中文库二"] == ("my-proc", "librarycache-2.log")
    assert len({value[1] for value in mapping.values()}) == len(rows), "回填后必须两两不同"
