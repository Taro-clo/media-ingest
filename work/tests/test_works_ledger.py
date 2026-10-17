"""每库「已执行作品」账本（``link_records``）+ 作品级清单 / 删除 / 重命名。

四件事必须被钉死：

1. **账本身份 = (库, 作品)**，且 ``source_dir`` / ``target_dir`` 一起参与判断 ——
   同源多库、同库换目标都不再互相"跳过"（每库作品账本的本体）；
2. 建链阶段的判据可以被账本覆盖（``create_hard_links(..., skip=...)``），传 ``None``
   时**逐字**保持原行为（``test_hardlink_native.py`` 钉的就是那条默认路径）；
3. 重命名阶段支持**一批**作品（``folders=``），``force`` 只清这些作品的条目；
4. 作品级删除是"三处一起删"（账本 + 重命名缓存 + 目标文件夹），且非 dry-run 必须带
   ``confirm=true``（删除要带显著提示与确认；**界面侧只要
   一次弹窗确认** —— 勾选确认框已取消，后端这道 ``confirm`` 保留为防误调用）。
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import time

import pytest

import mlm_helpers as H

#: 作品名都带集数，才能走通"建硬链接 + 重命名"全流程（原因见 test_api_smoke 的 MEDIA_TREE）。
MEDIA_TREE = {
    "Show A/Season 1/ep01.mkv": 11,
    "Show A/Season 1/ep02.ass": 22,
    "Show B/Season 1/ep03.mp4": 33,
}
STAMP = "%Y-%m-%d %H:%M:%S"


def _wait_for_task(client, task_id: int, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail.get("status") in ("success", "failed", "cancelled"):
            return detail
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未结束，最后一次：{detail}")


def _create_library(client, source_dir, target_dir, **overrides) -> dict:
    body = {
        "name": overrides.pop("name", "作品账本测试库"),
        "type": "Bangumi",
        "processor_id": "bangumi",
        "source_dir": str(source_dir),
        "target_dir": str(target_dir),
    }
    body.update(overrides)
    response = client.post("/api/libraries", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _run_library(client, library_id: int, payload: dict | None = None) -> int:
    response = client.post(f"/api/libraries/{library_id}/run", json=payload or {})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _ledger(request):
    return pytest.importorskip("app.link_records")


# --------------------------------------------------------------------------- 账本本体


def test_ledger_is_scoped_by_library_source_and_target(db_mod, tmp_path):
    """每库作品账本的核心：换库、换源、换目标都不算"已处理过"。"""
    db_mod.init_db()
    ledger = _ledger(None)
    source = str(tmp_path / "downloads" / "Bangumi")
    target_a = str(tmp_path / "library" / "A")
    target_b = str(tmp_path / "library" / "B")

    ledger.upsert_many(1, source_dir=source, target_dir=target_a, items=[("Show A", 33)])
    ledger.upsert_many(2, source_dir=source, target_dir=target_b, items=[("Show A", 33)])

    assert ledger.folder_sizes(1, source_dir=source, target_dir=target_a) == {"Show A": 33}
    assert ledger.folder_sizes(2, source_dir=source, target_dir=target_b) == {"Show A": 33}
    # 同源不同目标（1 号库换了目标目录）⇒ 旧记录不算数。
    assert ledger.folder_sizes(1, source_dir=source, target_dir=target_b) == {}
    # 换了源目录同样不算数。
    assert ledger.folder_sizes(1, source_dir=source + "-2", target_dir=target_a) == {}
    # 别的库当然什么都看不到。
    assert ledger.folder_sizes(3, source_dir=source, target_dir=target_a) == {}


def test_ledger_rows_are_refreshed_instead_of_duplicated(db_mod, tmp_path):
    db_mod.init_db()
    ledger = _ledger(None)
    source, target = str(tmp_path / "src"), str(tmp_path / "tgt")

    assert ledger.upsert_many(1, source_dir=source, target_dir=target, items=[("Show A", 10)]) == 1
    ledger.upsert_many(1, source_dir=source, target_dir=target, items=[("Show A", 99)])
    ledger.upsert_many(1, source_dir=source, target_dir=target, items=[("Show B", 5)])

    records = ledger.list_records(1)
    assert [record["folder"] for record in records] == ["Show A", "Show B"]
    assert {record["folder"]: record["size"] for record in records} == {"Show A": 99, "Show B": 5}
    assert records[0]["target_path"] == os.path.join(target, "Show A")
    assert records[0]["source_dir"] == source


def test_ledger_drop_and_drop_by_library(db_mod, tmp_path):
    db_mod.init_db()
    ledger = _ledger(None)
    source, target = str(tmp_path / "src"), str(tmp_path / "tgt")
    ledger.upsert_many(1, source_dir=source, target_dir=target,
                       items=[("Show A", 1), ("Show B", 2)])
    ledger.upsert_many(2, source_dir=source, target_dir=target, items=[("Show C", 3)])

    removed = ledger.drop(1, ["Show A", "Not There"])
    assert [record["folder"] for record in removed] == ["Show A"]
    assert [record["folder"] for record in ledger.list_records(1)] == ["Show B"]
    assert ledger.drop(1, []) == []

    assert ledger.drop_by_library(1) == 1
    assert ledger.list_records(1) == []
    assert [record["folder"] for record in ledger.list_records(2)] == ["Show C"]


def test_search_records_filters_by_literal_substring_and_pages(db_mod, tmp_path):
    """独立页：搜索与翻页在 SQL 层做，``%`` / ``_`` 必须按字面量处理。"""
    db_mod.init_db()
    ledger = _ledger(None)
    source, target = str(tmp_path / "src"), str(tmp_path / "tgt")
    ledger.upsert_many(
        1,
        source_dir=source,
        target_dir=target,
        items=[("Show A", 10), ("Show B", 20), ("Show C", 30), ("Show D 100%_x", 40)],
    )
    ledger.upsert_many(2, source_dir=source, target_dir=target, items=[("Show A", 10)])

    rows, matched = ledger.search_records(1, limit=None)
    assert matched == 4
    assert [row["folder"] for row in rows] == ["Show A", "Show B", "Show C", "Show D 100%_x"]

    # 子串、大小写不敏感
    rows, matched = ledger.search_records(1, query="SHOW b")
    assert matched == 1 and [row["folder"] for row in rows] == ["Show B"]

    # % 和 _ 是字面量：LIKE 的通配符在这里不生效
    rows, matched = ledger.search_records(1, query="100%_x")
    assert matched == 1 and rows[0]["folder"] == "Show D 100%_x"
    rows, matched = ledger.search_records(1, query="100%Y")
    assert matched == 0 and rows == []

    # matched 是"命中总数"（翻页要用），不随 limit 变
    page1, matched1 = ledger.search_records(1, limit=2, offset=0)
    page2, matched2 = ledger.search_records(1, limit=2, offset=2)
    assert matched1 == matched2 == 4
    assert [row["folder"] for row in page1] == ["Show A", "Show B"]
    assert [row["folder"] for row in page2] == ["Show C", "Show D 100%_x"]
    assert ledger.search_records(1, limit=2, offset=99)[0] == []

    # 永远只在自己库的账本里搜
    rows, matched = ledger.search_records(2, query="show")
    assert matched == 1 and [row["folder"] for row in rows] == ["Show A"]


@pytest.mark.parametrize("bad", ["", "  ", ".", "..", "Show A/../B", "a/b", "a\\b"])
def test_safe_target_path_refuses_anything_but_a_first_level_name(tmp_path, bad):
    ledger = _ledger(None)
    assert ledger.safe_target_path(str(tmp_path), bad) is None


def test_safe_target_path_accepts_a_plain_first_level_name(tmp_path):
    ledger = _ledger(None)
    assert ledger.safe_target_path(str(tmp_path), "Show A") == str(tmp_path / "Show A")


def test_folder_stats_separates_hardlinks_from_loose_files(tmp_path):
    """``loose_files`` = 目标目录里"不是我们建的"文件（``st_nlink < 2``）⇒ 确认框要警告。"""
    ledger = _ledger(None)
    work = tmp_path / "Show A"
    (work / "Season 1").mkdir(parents=True)

    source = tmp_path / "src.mkv"
    source.write_bytes(b"x" * 10)
    os.link(source, work / "Season 1" / "linked.mkv")
    (work / "Season 1" / "loose.mkv").write_bytes(b"y" * 7)

    stats = ledger.folder_stats(str(work))
    assert stats == {"exists": True, "files": 2, "bytes": 17, "loose_files": 1}
    assert ledger.folder_stats(str(tmp_path / "Not There")) == {
        "exists": False, "files": 0, "bytes": 0, "loose_files": 0,
    }


# --------------------------------------------------------------------------- 建链判据


def _native_hardlink(tmp_path, monkeypatch):
    module = pytest.importorskip("app.processors.hardlink")
    monkeypatch.setattr(module, "LOG_FILE", str(tmp_path / "hard_links.log"))
    return module


def test_skip_dict_becomes_the_only_judgement(tmp_path, monkeypatch):
    """传了账本以后，全局 ``hard_links.log`` 里的旧记录**不再**决定跳过。"""
    module = _native_hardlink(tmp_path, monkeypatch)
    source = tmp_path / "src"
    source.mkdir()
    (source / "Show A" / "Season 1").mkdir(parents=True)
    (source / "Show A" / "Season 1" / "ep01.mkv").write_bytes(b"a" * 11)
    (source / "Show B" / "Season 1").mkdir(parents=True)
    (source / "Show B" / "Season 1" / "ep02.mkv").write_bytes(b"b" * 22)

    # 先写一条"全局日志说 Show A 已经处理过"的旧记录（升级前遗留的形态）。
    (tmp_path / "hard_links.log").write_text(
        f"2026-01-01 00:00:00 - Source: {source} - Folder: Show A - Folder size: 11\n",
        encoding="utf-8",
    )

    target_all = tmp_path / "tgt-all"
    target_all.mkdir()
    module.create_hard_links(str(source), str(target_all), skip={})
    assert (target_all / "Show A" / "Season 1" / "ep01.mkv").exists(), "账本为空 ⇒ 必须重建"
    assert (target_all / "Show B" / "Season 1" / "ep02.mkv").exists()

    target_scoped = tmp_path / "tgt-scoped"
    target_scoped.mkdir()
    module.create_hard_links(str(source), str(target_scoped), skip={"Show A": 11})
    assert not (target_scoped / "Show A").exists(), "账本说体积一致 ⇒ 跳过"
    assert (target_scoped / "Show B" / "Season 1" / "ep02.mkv").exists()

    # 账本给的体积与当前不符 ⇒ 删旧日志行后重建（而不是静默什么都不做）。
    target_stale = tmp_path / "tgt-stale"
    target_stale.mkdir()
    module.create_hard_links(str(source), str(target_stale), skip={"Show A": 999})
    assert (target_stale / "Show A" / "Season 1" / "ep01.mkv").exists()


def test_default_path_still_reads_the_global_log(tmp_path, monkeypatch, capsys):
    """不传 ``skip`` 时逐字保持既有行为（旧口径语义的默认路径）。"""
    module = _native_hardlink(tmp_path, monkeypatch)
    source = tmp_path / "src"
    (source / "Show A" / "Season 1").mkdir(parents=True)
    (source / "Show A" / "Season 1" / "ep01.mkv").write_bytes(b"a" * 11)
    (tmp_path / "hard_links.log").write_text(
        f"2026-01-01 00:00:00 - Source: {source} - Folder: Show A - Folder size: 11\n",
        encoding="utf-8",
    )
    target = tmp_path / "tgt"
    target.mkdir()

    module.create_hard_links(str(source), str(target))

    assert not (target / "Show A").exists(), "默认路径必须照旧读全局日志并跳过"
    assert f"{module.PRINT_SKIPPED}Show A" in capsys.readouterr().out


# --------------------------------------------------------------------------- 重命名阶段


def _base_spec(proc_defaults) -> dict:
    import copy

    return copy.deepcopy(proc_defaults.preset_spec("bangumi", "legacy"))


def test_engine_folders_limits_the_run_to_the_selected_titles(proc_defaults, proc_engine, tmp_path):
    import processor_helpers as P

    spec = _base_spec(proc_defaults)
    root = tmp_path / "root"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 11, "Show B/Season 1/ep02.mkv": 22})
    log_file = tmp_path / "rename.log"

    _outcome, stdout = P.call_engine(proc_engine, spec, root, log_file=log_file, folders=["Show B"])

    assert P.renamed_lines(stdout) == {"ep02.mkv -> Show B S01E02.mkv"}
    assert P.log_keys(log_file) == {"Show B"}
    assert (root / "Show A" / "Season 1" / "ep01.mkv").exists(), "没勾选的作品一个字节都不动"


def test_engine_force_clears_only_the_scoped_entries(proc_defaults, proc_engine, tmp_path):
    import processor_helpers as P

    spec = _base_spec(proc_defaults)
    root = tmp_path / "root"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 11, "Show B/Season 1/ep02.mkv": 22})
    log_file = tmp_path / "rename.log"
    first = datetime.datetime(2026, 1, 1, 10, 0, 0)
    second = datetime.datetime(2026, 1, 2, 11, 0, 0)

    P.call_engine(proc_engine, spec, root, log_file=log_file, now=first)
    before = P.log_sizes(log_file)
    assert set(before) == {"Show A", "Show B"}

    _outcome, stdout = P.call_engine(
        proc_engine, spec, root, log_file=log_file, folders=["Show A"], force=True, now=second,
    )

    payload = json.loads((log_file).read_text(encoding="utf-8"))
    assert payload["Show A"]["timestamp"] == second.strftime(STAMP), "被点名的作品必须重做"
    assert payload["Show B"]["timestamp"] == first.strftime(STAMP), "没点名的作品不许被动到"
    assert "Skipping folder: Show A" not in stdout


def test_engine_reports_a_scoped_folder_that_does_not_exist(proc_defaults, proc_engine, tmp_path):
    import processor_helpers as P

    spec = _base_spec(proc_defaults)
    root = tmp_path / "root"
    P.write_tree(root, {"Show A/Season 1/ep01.mkv": 11})

    outcome, _stdout = P.call_engine(
        proc_engine, spec, root, log_file=tmp_path / "rename.log",
        folders=["Not There"], require_output=False,
    )

    warnings = [str(item) for item in (getattr(outcome, "warnings", None) or [])]
    assert any("Not There" in warning for warning in warnings), warnings


# --------------------------------------------------------------------------- 作品级接口


def test_works_endpoints_round_trip(api_client, api_login, build_tree, source_dir, target_dir):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    task_id = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, task_id)["status"] == "success"

    works_url = f"/api/libraries/{library['id']}/works"
    listed = api_client.get(works_url)
    assert listed.status_code == 200, listed.text
    listing = listed.json()
    assert listing["total"] == 2
    assert {item["folder"] for item in listing["items"]} == {"Show A", "Show B"}
    # 每个作品目标的真实文件数（Show A 两个文件、Show B 一个文件）：
    expected_files = {"Show A": 2, "Show B": 1}
    for item in listing["items"]:
        assert item["target_exists"] is True
        assert item["rename_recorded"] is True, "重命名缓存里也该有这条（一套流程走完）"
        assert item["size"] > 0
        # 现算的目标目录实际情况（账本里的 linked 是历史记账，升级后的库是 0）：
        # 界面靠这两个数说"目标目录现在有多少文件"，所以必须真的数出来。
        assert item["target_files"] == expected_files[item["folder"]], "作品行要现算目标目录里的文件数"
        assert item["target_loose"] == 0, "链接出来的文件都该是硬链接"
    assert listing["rename_missing"] == [] and listing["rename_only"] == []

    delete_url = f"{works_url}/delete"
    refused = api_client.post(delete_url, json={"folders": ["Show A"]})
    assert refused.status_code == 400, refused.text
    assert "显式确认" in refused.json()["detail"]

    plan = api_client.post(delete_url, json={"folders": ["Show A"], "dry_run": True})
    assert plan.status_code == 200, plan.text
    planned = plan.json()["items"][0]
    assert planned["folder"] == "Show A"
    assert planned["in_ledger"] is True and planned["rename_recorded"] is True
    assert planned["exists"] is True and planned["files"] == 2 and planned["bytes"] == 33
    assert planned["loose_files"] == 0, "刚建出来的都是硬链接"
    assert (target_dir / "Show A").is_dir(), "dry-run 一个字节都不许动"

    renamed = api_client.post(
        f"{works_url}/rename", json={"folders": ["Show A"]}
    )
    assert renamed.status_code == 202, renamed.text
    assert renamed.json()["kind"] == "rename"
    assert _wait_for_task(api_client, renamed.json()["id"])["status"] == "success"

    bad = api_client.post(f"{works_url}/rename", json={"folders": ["Show A/Season 1"]})
    assert bad.status_code == 400, bad.text
    empty = api_client.post(delete_url, json={"folders": []})
    assert empty.status_code == 400, empty.text

    other_file = next((target_dir / "Show B").rglob("*.mp4"))
    other_inode = other_file.stat().st_ino

    deleted = api_client.post(delete_url, json={"folders": ["Show A"], "confirm": True})
    assert deleted.status_code == 200, deleted.text
    payload = deleted.json()
    assert payload["deleted"] is True
    assert payload["ledger_removed"] == ["Show A"]
    assert payload["rename_removed"] == ["Show A"]
    assert payload["items"][0]["target_removed"] is True
    assert not (target_dir / "Show A").exists()
    assert other_file.stat().st_ino == other_inode, "别的作品不许被动到"

    after = api_client.get(works_url).json()
    assert after["total"] == 1 and after["items"][0]["folder"] == "Show B"

    # 下次运行把被删掉的作品补回来：Show B 连 inode 都不该变。
    task_id = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, task_id)["status"] == "success"
    rebuilt = sorted(path.name for path in (target_dir / "Show A").rglob("*.mkv"))
    assert rebuilt == ["Show A S01E01.mkv"], rebuilt
    assert other_file.stat().st_ino == other_inode
    assert {item["folder"] for item in api_client.get(works_url).json()["items"]} == {
        "Show A", "Show B",
    }


def test_forced_folder_scope_really_relinks(api_client, api_login, build_tree, source_dir, target_dir):
    """force + scope.mode="folder" 必须真的重跑，而不是被账本继续判成"已处理"。

    回归点（首轮验收里发现并修掉的静默空操作）：整库 force 已被 API 拒掉，
    于是 ``force`` 只剩"单个作品"这一种来源；如果建链阶段不把被 force 的作品从账本
    判据里摘掉，用户点了"强制重跑这个作品"会得到一个 success 但什么都没做——
    比报错更难发现。这里先把目标产物删掉、**保留**账本行，再 force，必须补回来。
    """
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    first = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, first)["status"] == "success"

    works_url = f"/api/libraries/{library['id']}/works"
    assert {item["folder"] for item in api_client.get(works_url).json()["items"]} == {
        "Show A", "Show B",
    }
    show_b = next((target_dir / "Show B").rglob("*.mp4"))
    show_b_inode = show_b.stat().st_ino

    # 手工抹掉 Show A 的产物，账本行留着：这正是"不 force 就会静默跳过"的场景。
    shutil.rmtree(target_dir / "Show A")
    assert any(
        item["folder"] == "Show A" for item in api_client.get(works_url).json()["items"]
    ), "账本行必须还在，否则这个用例测不到回归点"

    forced = api_client.post(
        f"/api/libraries/{library['id']}/run",
        json={"force": True, "scope": {"mode": "folder", "folder": "Show A"}},
    )
    assert forced.status_code == 202, forced.text
    detail = _wait_for_task(api_client, forced.json()["id"])
    assert detail["status"] == "success", detail

    rebuilt = sorted(path.name for path in (target_dir / "Show A").rglob("*.mkv"))
    assert rebuilt == ["Show A S01E01.mkv"], (
        f"force 必须真的重建硬链接，而不是被账本判成已处理；实际 {rebuilt}"
    )
    assert show_b.stat().st_ino == show_b_inode, "只准动 Show A"


def test_library_wide_force_is_refused(api_client, api_login, build_tree, source_dir, target_dir):
    """任何涉及整个库的 force 都不允许；作品级（scope=folder）仍然可以。"""
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    run_url = f"/api/libraries/{library['id']}/run"

    for payload in ({"force": True}, {"force": True, "scope": {"mode": "full"}}):
        refused = api_client.post(run_url, json=payload)
        assert refused.status_code == 400, refused.text
        assert "整库强制重跑已停用" in refused.json()["detail"]

    allowed = api_client.post(
        run_url, json={"force": True, "scope": {"mode": "folder", "folder": "Show A"}}
    )
    assert allowed.status_code == 202, allowed.text
    assert allowed.json()["kind"] == "rename_hardlink"
    assert _wait_for_task(api_client, allowed.json()["id"])["status"] == "success"


def test_works_endpoint_supports_search_and_paging(
    api_client, api_login, build_tree, source_dir, target_dir
):
    """``GET works`` 的 ``q`` / ``limit`` / ``offset``（独立页靠它做搜索与翻页）。"""
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    assert _wait_for_task(api_client, _run_library(api_client, library["id"]))["status"] == "success"
    url = f"/api/libraries/{library['id']}/works"

    everything = api_client.get(url).json()
    assert everything["total"] == 2 and everything["ledger_total"] == 2
    assert everything["q"] == "" and everything["limit"] == 50 and everything["offset"] == 0
    assert [item["folder"] for item in everything["items"]] == ["Show A", "Show B"]

    # 搜索：命中数进 total，账本总数单独给（页脚要同时说清"命中几条 / 一共几条"）
    hit = api_client.get(url, params={"q": "show b"}).json()
    assert hit["total"] == 1 and hit["ledger_total"] == 2
    assert hit["q"] == "show b"
    assert [item["folder"] for item in hit["items"]] == ["Show B"]
    # 差集按整本账算，不跟着 q 变
    assert hit["rename_entries"] == 2

    miss = api_client.get(url, params={"q": "没有这个作品"}).json()
    assert miss["total"] == 0 and miss["items"] == [] and miss["ledger_total"] == 2

    # 翻页：一页一条
    first = api_client.get(url, params={"limit": 1, "offset": 0}).json()
    second = api_client.get(url, params={"limit": 1, "offset": 1}).json()
    assert first["limit"] == 1 and first["total"] == 2
    assert [item["folder"] for item in first["items"]] == ["Show A"]
    assert second["offset"] == 1 and [item["folder"] for item in second["items"]] == ["Show B"]
    # target_* 只对本页的行算（这里 Show A 有 2 个文件）
    assert first["items"][0]["target_files"] == 2
    assert api_client.get(url, params={"limit": 1, "offset": 99}).json()["items"] == []

    # 夹边界：limit 上限 200、非法 limit 落回默认 50、offset 不许为负
    clamped = api_client.get(url, params={"limit": 99999, "offset": -5}).json()
    assert clamped["limit"] == 200 and clamped["offset"] == 0
    assert api_client.get(url, params={"limit": 0}).json()["limit"] == 50


def test_pure_rename_is_refused_when_target_root_is_missing(
    api_client, api_login, build_tree, source_dir, target_dir
):
    """纯重命名任务指向不存在的目标根 ⇒ 入队前 400，而不是"202 收下、随即失败"。

    回归点：目标目录由建链阶段 ``os.makedirs`` 自建，所以
    "目标根不存在"只可能在**只改名、不建链**这条路上出现（作品记录页的「立即重命名」）。
    不拦的话它会入队成功，然后在任务里对每个作品报「无法读取媒体库目录 …：No such file or
    directory」；现在提交时就拦下并说明该怎么做。
    """
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    assert _wait_for_task(api_client, _run_library(api_client, library["id"]))["status"] == "success"

    rename_url = f"/api/libraries/{library['id']}/works/rename"
    # 目标根还在时：正常收下（202，kind=rename）
    accepted = api_client.post(rename_url, json={"folders": ["Show A"]})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["kind"] == "rename"
    assert _wait_for_task(api_client, accepted.json()["id"])["status"] == "success"

    # 目标根整个消失（现场：被手工删掉、或挂载换了）
    shutil.rmtree(target_dir)
    refused = api_client.post(rename_url, json={"folders": ["Show A"]})
    assert refused.status_code == 400, refused.text
    detail = refused.json()["detail"]
    assert "目标目录" in detail and "不存在" in detail, detail

    # 建链路线不受影响：目标根不存在照样 202（建链阶段自己 makedirs）
    assert (
        api_client.post(
            f"/api/libraries/{library['id']}/run", json={"scope": {"mode": "full"}}
        ).status_code
        == 202
    )


def test_deleting_a_library_removes_its_ledger(
    api_client, api_login, build_tree, source_dir, target_dir, db_mod
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    task_id = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, task_id)["status"] == "success"

    count = "SELECT COUNT(*) FROM link_records WHERE library_id=?"
    assert db_mod.get_conn().execute(count, (library["id"],)).fetchone()[0] == 2

    deleted = api_client.delete(
        f"/api/libraries/{library['id']}", params={"confirm": "true"}
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["link_records_removed"] == 2
    assert db_mod.get_conn().execute(count, (library["id"],)).fetchone()[0] == 0
