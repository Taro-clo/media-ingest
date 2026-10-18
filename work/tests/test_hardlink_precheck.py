"""级联预检的只读实证口径（§2/§3）：**调用方语义** + 源目录完整性复核（执行器 / API 两层）。

分工：``tests/test_paths.py`` 钉住 ``app/paths.py`` 的**单元行为**（探针只读安全、
有界递归取样、三态级联）；本文件钉住**谁在用这个结论、怎么用**：

* 执行器（``app/executor.py``）：预检 ``False`` / ``None`` **只记警告、绝不硬拦**
  —— 挂载配错不该让整个库一个文件都建不出来，真正的结论由 ``os.link`` 给出；
* 执行器：``Task.result.source_integrity``（运行前快照 → 运行后复核）必须真的落到
  结果里，源被动过时报 ``ok=false`` + 列出路径 + 中文告警，但**不改 status**
  （源被外部程序改动不是本次任务失败）；
* API（``POST /api/libraries/{id}/validate?probe=true``）：三态映射成
  ``ok`` / ``errors`` / ``warnings`` —— ``false`` 才是错误，``null`` 只警告并放行。

三态语义（COMPATIBILITY B8）：``true`` = 可建（note 可能标注"未实证"）、``false`` =
明确建不出（跨挂载必 EXDEV）、``null`` = 判不出来。**``null`` 绝不等于 ``true``**：
把"不知道"当"可以"正是 v1 预检假绿的成因（见 TROUBLESHOOTING §21）。
"""

from __future__ import annotations

import json
import os

import pytest

import mlm_helpers as H

#: 源树：两个一级文件夹 → 按一级文件夹记账，`linked` 期望等于文件数。
MEDIA_TREE = {
    "Show A/Season 1/ep01.mkv": 11,
    "Show A/Season 1/ep02.ass": 22,
    "Show B/ep03.mp4": 33,
}
EXPECTED_LINKED = len(MEDIA_TREE)

#: 跨挂载时 `app/paths.py:check_same_mount` 给出的真实文案（挂载点/ID 按实际部署填入）。
CROSS_MOUNT_NOTE = (
    "跨挂载错误：源 /vol1/1000/Download/qbittorrent/Bangumi（挂载点 /downloads，mount ID 31）"
    "与目标根 /vol1/1000/Library/Bangumi（挂载点 /library，mount ID 47）不是同一个挂载，"
    "os.link 会返回 EXDEV。本应用不会回退为复制文件。请把两个目录放到同一个宿主挂载下。"
)

UNPROVABLE_NOTE = "无法读取 /proc/self/mountinfo，挂载 ID 判据不可用。"


# --------------------------------------------------------------------------- 工具


def _insert_library(db_mod, name: str, source: str, target: str) -> int:
    """插一条 ``hardlink_only`` 库（v2 起 ``processor_id`` 是 NOT NULL 外键）。"""
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO libraries(name,type,processor,processor_id,enabled,source_dir,target_dir,"
            "match_categories,match_tags,match_path_prefixes,orphan_cleanup,permission_mode,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "Bangumi", "hardlink_only", "hardlink_only", 1, source, target,
             "[]", "[]", "[]", 0, 0, stamp, stamp),
        )
        return int(cursor.lastrowid)


def _insert_task(db_mod, library_id: int, name: str) -> int:
    """插一条 ``kind='hardlink'`` 的任务（执行器只认这个值走建链分支）。"""
    stamp = db_mod.now_iso()
    with db_mod.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks(library_id,library_name,kind,trigger,status,scope,payload,result,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (library_id, name, "hardlink", "manual", "queued",
             json.dumps({"mode": "full"}), "{}", "{}", stamp),
        )
        return int(cursor.lastrowid)


def _task_row(db_mod, task_id: int) -> dict:
    row = db_mod.get_conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row is not None, f"任务 {task_id} 不存在"
    return dict(row)


def _task_result(db_mod, task_id: int) -> dict:
    raw = _task_row(db_mod, task_id)["result"] or "{}"
    return json.loads(raw)


def _task_logs(db_mod, task_id: int) -> list[str]:
    rows = db_mod.get_conn().execute(
        "SELECT message FROM task_logs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    return [row["message"] for row in rows]


class _FakeLinks:
    """替换 ``script_adapter.run_hardlinks``：只报告"建了 N 个"，不碰文件系统。"""

    def __init__(self, created: int = EXPECTED_LINKED) -> None:
        self.calls: list[dict] = []
        self._created = created

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        from app.script_adapter import LinkOutcome

        return LinkOutcome(
            folder=kwargs.get("folder") or "*",
            created=[f"/library/Bangumi/f{index}.mkv" for index in range(self._created)],
            logged=[("Show A", 33)],
        )


# --------------------------------------------------------------- 执行器：只警告不硬拦


def test_executor_only_warns_when_the_precheck_says_cross_mount(
    executor_mod, db_mod, source_dir, target_dir, monkeypatch, build_tree
):
    """预检 ``False``（明确跨挂载）**不硬拦**：任务照跑，结论进 warnings 和日志。

    这是级联预检的只读实证口径的核心让步：级联判据必要不充分（挂载 ID 可能误判、btrfs 子卷是已知
    例外），所以运行前只警告；"建不出链"只能由真实 ``os.link`` 判定（B8 也不允许
    用复制兜底，所以失败是逐文件 ``link_failed``）。
    """
    build_tree(source_dir, MEDIA_TREE)
    library_id = _insert_library(
        db_mod, "跨挂载库", str(source_dir), str(target_dir)
    )
    task_id = _insert_task(db_mod, library_id, "跨挂载库")

    monkeypatch.setattr(
        executor_mod, "check_hardlink_support", lambda *_a, **_k: (False, CROSS_MOUNT_NOTE)
    )
    fake = _FakeLinks()
    monkeypatch.setattr(executor_mod.script_adapter, "run_hardlinks", fake)

    executor_mod.execute_task(task_id)

    row = _task_row(db_mod, task_id)
    result = _task_result(db_mod, task_id)
    logs = _task_logs(db_mod, task_id)

    assert fake.calls, "预检失败也必须**真的执行**脚本（不许提前 return）"
    assert row["status"] == "success", row
    assert row["error"] in (None, ""), row
    assert result["linked"] == EXPECTED_LINKED, result
    assert CROSS_MOUNT_NOTE in result["warnings"], result["warnings"]
    assert any("（仍会尝试。）" in message for message in logs), logs
    # 预检失败与源完整性无关：源没被动过 ⇒ 复核通过。
    assert result["source_integrity"] == {"ok": True, "changed": []}, result["source_integrity"]


def test_executor_only_warns_when_the_precheck_cannot_tell(
    executor_mod, db_mod, source_dir, target_dir, monkeypatch, build_tree
):
    """预检 ``None``（判不出来）同样只警告 —— **不许**把 None 当成 False 拦掉。"""
    build_tree(source_dir, MEDIA_TREE)
    library_id = _insert_library(db_mod, "未知库", str(source_dir), str(target_dir))
    task_id = _insert_task(db_mod, library_id, "未知库")

    monkeypatch.setattr(
        executor_mod, "check_hardlink_support", lambda *_a, **_k: (None, UNPROVABLE_NOTE)
    )
    fake = _FakeLinks()
    monkeypatch.setattr(executor_mod.script_adapter, "run_hardlinks", fake)

    executor_mod.execute_task(task_id)

    result = _task_result(db_mod, task_id)
    logs = _task_logs(db_mod, task_id)
    assert fake.calls, "无法判定也必须真的执行"
    assert _task_row(db_mod, task_id)["status"] == "success"
    assert any("无法判定源/目标之间能否创建硬链接：" in text for text in result["warnings"]), (
        result["warnings"]
    )
    assert any("无法判定" in message for message in logs), logs


# --------------------------------------------------------------- 执行器：源完整性复核


def test_executor_reports_a_tampered_source_without_failing_the_task(
    executor_mod, db_mod, source_dir, target_dir, monkeypatch, build_tree
):
    """任务期间源被外部改动 ⇒ ``source_integrity.ok=false`` + 路径 + 中文告警，status 不变。

    包装真的 ``verify_source_tree``（先篡改再复核），这样断言的是**真实的快照比较**，
    而不是被替换掉的假实现。
    """
    build_tree(source_dir, MEDIA_TREE)
    library_id = _insert_library(db_mod, "被外部改动库", str(source_dir), str(target_dir))
    task_id = _insert_task(db_mod, library_id, "被外部改动库")

    real_verify = executor_mod.verify_source_tree
    tampered = source_dir / "Show A" / "Season 1" / "ep01.mkv"

    def tamper_then_verify(root, before):
        tampered.write_bytes(b"y" * 99)  # 模拟外部程序在任务运行期间改源
        return real_verify(root, before)

    monkeypatch.setattr(executor_mod, "verify_source_tree", tamper_then_verify)
    monkeypatch.setattr(
        executor_mod, "check_hardlink_support", lambda *_a, **_k: (True, "预检通过（测试替身）")
    )
    monkeypatch.setattr(executor_mod.script_adapter, "run_hardlinks", _FakeLinks())

    executor_mod.execute_task(task_id)

    row = _task_row(db_mod, task_id)
    result = _task_result(db_mod, task_id)
    integrity = result["source_integrity"]

    assert integrity["ok"] is False, integrity
    assert integrity["changed"] == ["Show A/Season 1/ep01.mkv"], integrity
    joined = " ".join(result["warnings"])
    assert "源目录完整性复核失败" in joined, result["warnings"]
    assert "本应用不应该改动源目录" in joined, result["warnings"]
    # 源被外部程序改动**不是本次任务失败**：status 只由 errors/all_failed 决定。
    assert row["status"] == "success", row


def test_executor_marks_a_clean_source_as_intact_after_a_real_run(
    executor_mod, db_mod, source_dir, target_dir, build_tree, hardlink_assert
):
    """真实跑一遍建链后：源完好、``source_integrity.ok=true``、真的建出了硬链接。

    这一条是"硬链接本身不得误报"的**端到端**钉法：建链会让源文件 ``nlink`` +1、``ctime``
    变，如果快照把这些算进去，每次任务都会假报"源被改动"。``snapshot_source_tree`` 刻意
    只比 ``(st_size, st_ino, st_mtime_ns)``，本用例守住这个选择。
    """
    build_tree(source_dir, MEDIA_TREE)
    library_id = _insert_library(db_mod, "真实跑库", str(source_dir), str(target_dir))
    task_id = _insert_task(db_mod, library_id, "真实跑库")

    before = {rel: H.stat_signature(source_dir / rel) for rel in MEDIA_TREE}

    executor_mod.execute_task(task_id)

    row = _task_row(db_mod, task_id)
    result = _task_result(db_mod, task_id)

    assert row["status"] == "success", (row, result)
    assert result["linked"] == EXPECTED_LINKED, result
    assert result["link_failed"] == 0, result
    assert result["source_integrity"] == {"ok": True, "changed": []}, result["source_integrity"]

    # 目标树里每个文件都是**真**硬链接（同 dev/ino），源文件本身逐字节未变。
    pairs = H.relative_link_pairs(source_dir, target_dir)
    assert len(pairs) == EXPECTED_LINKED, pairs
    for src, dst in pairs:
        hardlink_assert(src, dst)
    for rel, signature in before.items():
        assert H.stat_signature(source_dir / rel) == signature, f"源文件被改动了：{rel}"


# --------------------------------------------------------------- API：validate 三态


def _create_library(client, name: str, source_dir, target_dir) -> dict:
    response = client.post(
        "/api/libraries",
        json={
            "name": name,
            "type": "Bangumi",
            "processor_id": "hardlink_only",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _validate(client, library_id: int, *, probe: bool = True) -> dict:
    response = client.post(
        f"/api/libraries/{library_id}/validate", params={"probe": str(probe).lower()}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_validate_attributes_a_real_link_to_the_probe_without_overclaiming(
    api_client, api_login, source_dir, target_dir, build_tree
):
    """``probe=true`` 真做一次 ``os.link`` ⇒ ``probe.ok=true``，note 说"已实证"。

    有实证结论时 ``same_filesystem`` 直接采用探针结论（不再贴"未实证"的预筛 note）：
    探针**真的**建成了一个硬链接，这就是能拿到的最强证据。反过来，拿不到实证时
    （``probe=false`` 或探针返回 ``None``）note 必须诚实标注"未实证"，见下面两条。
    """
    build_tree(source_dir, MEDIA_TREE)
    api_login(api_client)
    library = _create_library(api_client, "预检真跑库", source_dir, target_dir)

    report = _validate(api_client, library["id"], probe=True)

    assert report["ok"] is True, report
    assert report["errors"] == [], report
    assert report["same_filesystem"] is True, report
    assert report["probe"] is not None, report
    assert report["probe"]["ok"] is True, report["probe"]
    assert "已实证" in report["probe"]["note"], report["probe"]
    # 落点冻结（定稿契约）：`true` 且判据文案非空时**也会**进 `warnings` —— 连"已实证
    # 成功"这句也不例外 ⇒ `warnings` 非空 ≠ 有问题，断言不许写成 `warnings == []`。
    assert any("已实证" in text for text in report["warnings"]), report["warnings"]
    # 探针不留残留：源目录里不许出现任何 `.mlm_probe_*`。
    assert not [name for name in os.listdir(source_dir) if name.startswith(".mlm_probe_")], (
        os.listdir(source_dir)
    )
    assert not [name for name in os.listdir(target_dir) if name.startswith(".mlm_probe_")], (
        os.listdir(target_dir)
    )


def test_validate_reports_exdev_as_a_conclusive_error_but_never_copies(
    api_client, api_login, source_dir, target_dir, paths_mod, monkeypatch, build_tree
):
    """探针撞上 EXDEV ⇒ ``same_filesystem=false`` 且 ``ok=false``（**结论性**错误）。

    跨挂载是唯一"确定不行"的结论，所以它必须进 ``errors``（不是 warning）—— 并明确写上
    "不会回退为复制文件"，避免用户以为程序会帮忙拷过去。
    """
    build_tree(source_dir, MEDIA_TREE)
    api_login(api_client)
    library = _create_library(api_client, "跨挂载库", source_dir, target_dir)

    monkeypatch.setattr(
        paths_mod, "probe_cross_filesystem", lambda *_a, **_k: (False, CROSS_MOUNT_NOTE)
    )

    report = _validate(api_client, library["id"], probe=True)

    assert report["probe"] == {"ok": False, "note": CROSS_MOUNT_NOTE}, report["probe"]
    assert report["same_filesystem"] is False, report
    assert report["ok"] is False, report
    assert CROSS_MOUNT_NOTE in report["errors"], report["errors"]
    assert "不会回退为复制文件" in " ".join(report["errors"]), report["errors"]


def test_validate_treats_unprovable_as_a_warning_not_an_error(
    api_client, api_login, source_dir, target_dir, paths_mod, monkeypatch
):
    """探针与预筛都判不出来 ⇒ ``same_filesystem=null`` + warning，``ok`` 仍为 true。

    "判不出来"不许变成"校验不通过"：校验接口的 ``ok`` 表示"用户配置没有明确错误"，
    真正的结论留给运行时（COMPATIBILITY B8 的三态语义）。
    """
    api_login(api_client)
    library = _create_library(api_client, "无法判定库", source_dir, target_dir)

    monkeypatch.setattr(
        paths_mod, "probe_cross_filesystem", lambda *_a, **_k: (None, UNPROVABLE_NOTE)
    )
    monkeypatch.setattr(
        paths_mod, "check_hardlink_support", lambda *_a, **_k: (None, UNPROVABLE_NOTE)
    )

    report = _validate(api_client, library["id"], probe=True)

    assert report["probe"] == {"ok": None, "note": UNPROVABLE_NOTE}, report["probe"]
    assert report["same_filesystem"] is None, report
    assert report["ok"] is True, report
    assert "无法判定源/目标之间能否创建硬链接：" in " ".join(report["warnings"]), report[
        "warnings"
    ]
    assert all("无法判定" not in text for text in report["errors"]), report["errors"]


def test_validate_without_probe_never_writes_anything_but_still_reports_three_states(
    api_client, api_login, source_dir, target_dir, build_tree
):
    """``probe=false``：不做写入型实证（``probe`` 为 null），预筛结论照常给出。

    只读接口（前端轮询/展示）不能偷偷写临时链接；但"能不能建链"的展示结论仍要给。
    """
    build_tree(source_dir, MEDIA_TREE)
    api_login(api_client)
    library = _create_library(api_client, "只读预检库", source_dir, target_dir)

    report = _validate(api_client, library["id"], probe=False)

    assert report["probe"] is None, report["probe"]
    assert report["same_filesystem"] is True, report
    assert report["ok"] is True, report
    # 预筛结论（挂载 ID 或 st_dev）必须带一句"未实证"的诚实标注。
    assert any("未实证" in text for text in report["warnings"]), report["warnings"]
    # 没有任何写入：源树逐文件签名不变、也没有 `.mlm_probe_*` 残留。
    assert not [name for name in os.listdir(source_dir) if name.startswith(".mlm_probe_")], (
        os.listdir(source_dir)
    )
    assert sorted(os.listdir(source_dir)) == ["Show A", "Show B"], os.listdir(source_dir)


def test_validate_reports_a_missing_source_dir_even_when_the_probe_is_unprovable(
    api_client, api_login, source_dir, target_dir, paths_mod, monkeypatch
):
    """源目录不存在 ⇒ 明确错误（不是三态里的 null）：建链阶段在这里会抛 FileNotFoundError。"""
    api_login(api_client)
    missing = source_dir.parent / "不存在的番剧"
    assert not missing.exists()
    library = _create_library(api_client, "缺源库", missing, target_dir)

    monkeypatch.setattr(
        paths_mod, "probe_cross_filesystem", lambda *_a, **_k: (None, UNPROVABLE_NOTE)
    )
    monkeypatch.setattr(
        paths_mod, "check_hardlink_support", lambda *_a, **_k: (None, UNPROVABLE_NOTE)
    )

    report = _validate(api_client, library["id"], probe=True)

    assert report["source_exists"] is False, report
    assert report["ok"] is False, report
    assert any("源目录" in text and "不存在" in text for text in report["errors"]), report["errors"]


def test_system_info_exposes_the_same_three_state_verdict_without_probing(
    api_client, api_login, source_dir, target_dir, paths_mod, monkeypatch
):
    """``GET /api/system/info`` 的 ``downloads_vs_library`` 也是三态，且**不做**实证探测。

    只读展示接口会被 UI 反复轮询：它绝不能每次往目标目录写一个临时链接再删掉
    （``app/routers/system.py`` 的 ``probe=False``）。这里把三个取值都点一遍。

    该字段取**第一个媒体库**的源/目标来判（没有库时按设计返回 ``null`` + 说明），
    所以先建一个库让接口走到被测分支。
    """
    api_login(api_client)
    _create_library(api_client, "三态库", source_dir, target_dir)

    for support, note in [
        (True, "未实证，仅据 st_dev（同为 st_dev=66306，可能假绿）。"),
        (False, CROSS_MOUNT_NOTE),
        (None, UNPROVABLE_NOTE),
    ]:
        monkeypatch.setattr(
            paths_mod, "check_hardlink_support", lambda *_a, _s=support, _n=note, **_k: (_s, _n)
        )
        response = api_client.get("/api/system/info")
        assert response.status_code == 200, response.text
        body = response.json()
        view = body["same_filesystem"]
        assert view["downloads_vs_library"] is support, (support, view)
        assert view["message"] == note, (note, view)

    # 探针函数在这个接口上**一次都不该被调用**：它是只读展示。
    def _forbidden(*_a, **_k):  # pragma: no cover - 只用来在越界时失败
        raise AssertionError("/api/system/info 不得调用写入型探针")

    monkeypatch.setattr(paths_mod, "probe_cross_filesystem", _forbidden)
    assert api_client.get("/api/system/info").status_code == 200


# ------------------------------------------- API：响应形状、落点冻结与 mounts 动态集合


def test_validate_response_shape_is_flat_and_never_waits_for_combination_ok(
    api_client, api_login, source_dir, target_dir
):
    """``validate`` 的响应是**扁平 21 字段**：没有 v1 的 ``checks[]``，也没有幽灵字段。

    ``combination_ok`` 在 v1 文档里出现过、**在 v2 从未实现**：处理器是否存在由
    ``processor_ok`` 表达（``app/routers/libraries.py:492``、``docs/contracts/API.md`` §3.8）。
    前端若还在等 ``combination_ok``，预检结果会永远停在"未校验"。
    """
    api_login(api_client)
    library = _create_library(api_client, "字段形状库", source_dir, target_dir)

    report = _validate(api_client, library["id"], probe=False)

    expected = {
        "ok",
        "source_dir",
        "target_dir",
        "source_exists",
        "target_exists",
        "same_filesystem",
        "probe",
        "errors",
        "warnings",
        "library_id",
        "library_name",
        "type",
        "processor_id",
        "processor_ok",
        "processor_name",
        "processor_kind",
        "log_file",
        "log_path",
        "source_folders",
        "target_dir_exists",
        "same_filesystem_paths",
    }
    assert len(expected) == 21
    assert set(report) == expected, sorted(set(report) ^ expected)
    assert "checks" not in report, report
    assert "combination_ok" not in report, "v1 幽灵字段：v2 由 processor_ok 表达"
    assert report["processor_ok"] is True, report
    assert report["processor_id"] == "hardlink_only", report
    assert report["library_id"] == library["id"], report
    assert report["library_name"] == "字段形状库", report
    assert report["type"] == "Bangumi", report
    assert report["target_dir_exists"] is True, report
    assert set(report["same_filesystem_paths"]) == {"source_path", "target_path"}, report


def test_validate_error_and_warning_landing_is_frozen(
    api_client, api_login, source_dir, target_dir, paths_mod, monkeypatch
):
    """落点冻结（定稿契约）：三态各进哪儿，别把 warning 当失败。

    * ``false``（已证实建不出硬链接）⇒ ``ok=false`` **且**同一文案进 ``errors[]``；
    * ``null``（判不了）⇒ **只**进 ``warnings[]``，``ok`` 仍为 true；
    * ``true`` 且判据文案非空 ⇒ 也进 ``warnings[]``（"已实证成功"那句也不例外）
      ⇒ **`warnings` 非空 ≠ 有问题**。

    "只警告不硬拦"只适用于**跑任务**（``app/executor.py:257-259``、
    ``app/script_adapter.py:509-511``）；``validate`` 的 ``false`` 是结论性错误。
    """
    api_login(api_client)
    library = _create_library(api_client, "落点库", source_dir, target_dir)
    proved_note = (
        "已实证：把源树中的 Show A/Season 1/ep01.mkv 硬链接到目标目录成功"
        "（临时链接已删除、目标目录时间戳已还原，源目录未被写入）。"
    )

    # ① false：结论性错误 ⇒ errors，且不许同时塞进 warnings 混淆视听。
    monkeypatch.setattr(
        paths_mod, "probe_cross_filesystem", lambda *_a, **_k: (False, CROSS_MOUNT_NOTE)
    )
    report = _validate(api_client, library["id"], probe=True)
    assert report["same_filesystem"] is False, report
    assert report["ok"] is False, report
    assert CROSS_MOUNT_NOTE in report["errors"], report["errors"]
    assert CROSS_MOUNT_NOTE not in report["warnings"], report["warnings"]

    # ② null：判不了 ⇒ 只 warning 并放行。
    monkeypatch.setattr(
        paths_mod, "probe_cross_filesystem", lambda *_a, **_k: (None, UNPROVABLE_NOTE)
    )
    monkeypatch.setattr(
        paths_mod, "check_hardlink_support", lambda *_a, **_k: (None, UNPROVABLE_NOTE)
    )
    report = _validate(api_client, library["id"], probe=True)
    assert report["same_filesystem"] is None, report
    assert report["ok"] is True, report
    assert report["errors"] == [], report
    assert any("无法判定源/目标之间能否创建硬链接" in text for text in report["warnings"]), report

    # ③ true + 非空文案：好消息也进 warnings，但 ok 仍是 true。
    monkeypatch.setattr(paths_mod, "probe_cross_filesystem", lambda *_a, **_k: (True, proved_note))
    report = _validate(api_client, library["id"], probe=True)
    assert report["same_filesystem"] is True, report
    assert report["ok"] is True, report
    assert report["errors"] == [], report
    assert proved_note in report["warnings"], report["warnings"]


def test_system_info_mounts_are_a_dynamic_set_derived_from_libraries(
    api_client, api_login, downloads_root, library_root
):
    """``mounts[]`` 是**动态集合**：固定两项 + 各媒体库源/目标目录的最近已存在祖先。

    v2 起不再有「允许的源/目标根目录」设置，挂载点改为按媒体库推导
    （``app/routers/system.py::mount_points``）⇒ 断言必须用"集合包含"，写成"恰好 N 条"
    会在正常部署下随机失败。还没有媒体库时只剩固定两项；采用单挂载形态后容器里
    **没有** ``/downloads`` 与 ``/library``，它们也不许作为幽灵挂载点出现。
    """
    api_login(api_client)

    eight_keys = {
        "container_path",
        "host_path",
        "device",
        "fs_type",
        "options",
        "st_dev",
        "exists",
        "read_only",
    }

    def normalized(path) -> str:
        return os.path.normcase(os.path.normpath(str(path)))

    # ① 还没有媒体库：只有固定两项（没有可推导的媒体挂载点）。
    response = api_client.get("/api/system/info")
    assert response.status_code == 200, response.text
    mounts = response.json()["mounts"]
    assert isinstance(mounts, list) and mounts, mounts
    for item in mounts:
        assert set(item) == eight_keys, item
        assert isinstance(item["read_only"], bool), item
    by_path = {normalized(item["container_path"]): item for item in mounts}
    assert len(by_path) == len(mounts), "同一个挂载点不许出现两次"
    assert set(by_path) == {normalized("/data"), normalized("/data/config")}, sorted(by_path)

    # ② 建一个媒体库后，它的源/目标目录（"最近已存在祖先"就是它们本身）出现在挂载表里。
    source = downloads_root / "Bangumi"
    target = library_root / "Bangumi"
    source.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    _create_library(api_client, "挂载表动态库", source, target)

    mounts = api_client.get("/api/system/info").json()["mounts"]
    by_path = {normalized(item["container_path"]): item for item in mounts}
    for path in (source, target):
        assert normalized(path) in by_path, (sorted(by_path), str(path))
    # v1 的两条业务路径在单挂载形态下不再是挂载点。
    assert normalized("/downloads") not in by_path, sorted(by_path)
    assert normalized("/library") not in by_path, sorted(by_path)


def test_the_precheck_module_never_copies_files():
    """静态守卫（与 test_paths 里那条同类）：预检链路里不许出现任何复制回退。

    B8 是硬约束：宁可不建链接、报 ``link_failed``，也不许"看起来成功"地复制一份。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    sources = [os.path.join(here, "..", "app", "paths.py"),
               os.path.join(here, "..", "app", "executor.py")]
    banned = ("shutil.copy", "shutil.copy2", "shutil.copyfile", "copyfileobj")
    for path in sources:
        text = H.read_text(path)
        for token in banned:
            assert token not in text, f"{path} 出现复制回退：{token}"
