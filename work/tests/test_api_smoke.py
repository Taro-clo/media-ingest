"""API 冒烟：**真实 ASGI 应用 + 真实 TestClient，零垫片**。

为什么零垫片是必须的：只用打真实 HTTP 才能发现的那类缺陷实际出现过三个 —— ``read_session(request)`` 传错对象导致 ``GET /api/auth/me`` 必
500、登录成功路径写不存在的 ``last_login_at`` 列导致口令正确时必 500、``require_user(request)``
缺 ``Request`` 标注导致所有 ``Depends(security.require_user)`` 路由 422。这三条都属于"路由根本没
跑起来"，任何用属性替换把中间层换掉的写法都会漏掉它们。

因此本文件的断言只打在**真实 HTTP 响应**上，并且用参数化把**所有** ``require_user`` 路由都点一遍
（401 → 登录 → 200 成对出现，才能排除"401 其实来自 404 路由不存在"）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

import mlm_helpers as H

#: 走 ``Depends(security.require_user)`` 的只读路由（每个都要求"匿名 401、登录 200"）。
REQUIRE_USER_ROUTES = [
    "/api/libraries",
    "/api/tasks",
    "/api/settings",
    "/api/system/info",
    "/api/stats/overview",
    "/api/processors",
    "/api/config",
]

#: 任务日志时间戳与任务 ``created_at`` 同一格式；**不许**再退回 UTC ISO8601。
LOCAL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

#: 冒烟用的媒体树：文件名都带集数，才能走通"建硬链接 + 重命名"全流程（``Show.01.ass``
#: 这类没有集数的名字会被 legacy 兜底命名成 ``S01ENone.*`` 并互相撞名，见 COMPATIBILITY 的 L 系列）。
MEDIA_TREE = {
    "Show A/Season 1/ep01.mkv": 11,
    "Show A/Season 1/ep02.ass": 22,
    "Show B/Season 1/ep03.mp4": 33,
}
EXPECTED_LINKED = 3
EXPECTED_FOLDERS = 2
EXPECTED_BYTES = 66


# --------------------------------------------------------------------------- 工具


def _wait_for_task(client, task_id: int, timeout: float = 60.0) -> dict:
    """轮询任务直到终态；超时给出最后一次快照，便于看卡在哪。"""
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


def _run_library(client, library_id: int, payload: dict | None = None) -> int:
    response = client.post(f"/api/libraries/{library_id}/run", json=payload or {})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued", body
    assert body["kind"] == "rename_hardlink", body
    assert body["trigger"] == "manual", body
    return body["id"]


def _create_library(client, source_dir, target_dir, **overrides) -> dict:
    body = {
        "name": overrides.pop("name", "冒烟番剧"),
        "type": "Bangumi",
        "processor_id": "bangumi",
        "source_dir": str(source_dir),
        "target_dir": str(target_dir),
    }
    body.update(overrides)
    response = client.post("/api/libraries", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _sse_events(client, url: str, limit: int = 2000) -> list[tuple[str, dict]]:
    """把命名事件流读成 ``[(event, data)]``，读到 ``done`` 就停。"""
    events: list[tuple[str, dict]] = []
    with client.stream("GET", url) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream"), response.headers
        current: str | None = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                current = line[len("event: "):].strip()
            elif line.startswith("data: "):
                events.append((current or "", json.loads(line[len("data: "):])))
                if current == "done":
                    break
            if len(events) >= limit:
                break
    return events


# --------------------------------------------------------------------------- 认证


def test_health_needs_no_auth_and_reports_every_named_check(api_client):
    """**四项**自检：`db / processors / config / data_dir`。

    自检项必须真实而不是恒为 true：``processors`` 查库里的处理器表，
    ``config`` 真写一遍 ``config_dir``（YAML 配置面）。
    """
    response = api_client.get("/api/system/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["checks"]) == {
        "db",
        "processors",
        "config",
        "data_dir",
    }, body
    assert all(body["checks"].values()), body
    assert set(body["details"]) == set(body["checks"]), body
    assert body["status"] == "ok", body


@pytest.mark.parametrize("path", REQUIRE_USER_ROUTES)
def test_anonymous_callers_are_rejected_with_401(api_client, path):
    response = api_client.get(path)
    assert response.status_code == 401, f"{path} 匿名访问返回 {response.status_code}"
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail, response.text


@pytest.mark.parametrize("path", REQUIRE_USER_ROUTES)
def test_require_user_routes_answer_200_after_login(api_client, api_login, path):
    """成对断言：401 不是"路由不存在"，登录后必须 200（这是"真实 HTTP 状态码"这一层要的证据）。"""
    api_login(api_client)
    response = api_client.get(path)
    assert response.status_code == 200, f"{path} 登录后返回 {response.status_code}: {response.text}"
    assert response.headers["content-type"].startswith("application/json")
    assert isinstance(response.json(), (dict, list))


def test_setup_login_logout_lifecycle(api_client, api_credentials):
    username, password = api_credentials

    anonymous = api_client.get("/api/auth/me")
    assert anonymous.status_code == 200, anonymous.text
    assert anonymous.json() == {
        "authenticated": False,
        "needs_setup": True,
        "username": None,
        "created_at": None,
    }, anonymous.text

    created = api_client.post(
        "/api/auth/setup", json={"username": username, "password": password}
    )
    assert created.status_code == 201, created.text
    assert created.json()["ok"] is True

    again = api_client.post(
        "/api/auth/setup", json={"username": username, "password": password}
    )
    assert again.status_code == 409, again.text

    wrong = api_client.post(
        "/api/auth/login", json={"username": username, "password": "definitely-wrong"}
    )
    assert wrong.status_code == 401, wrong.text
    assert "用户名或密码错误" in wrong.json()["detail"]

    ok = api_client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert ok.status_code == 200, ok.text
    cookie = ok.headers.get("set-cookie", "")
    assert "mlm_session=" in cookie
    assert "HttpOnly" in cookie

    me = api_client.get("/api/auth/me").json()
    assert me["authenticated"] is True and me["needs_setup"] is False
    assert me["username"] == username
    assert LOCAL_TIME_RE.match(me["created_at"]), me

    logout = api_client.post("/api/auth/logout")
    assert logout.status_code == 204, logout.text
    assert api_client.get("/api/libraries").status_code == 401


def test_password_change_invalidates_the_session(api_client, api_login, api_credentials):
    username, password = api_credentials
    api_login(api_client)

    changed = api_client.post(
        "/api/auth/password",
        json={"old_password": password, "new_password": "brand-new-password-123"},
    )
    assert changed.status_code == 204, changed.text
    assert api_client.get("/api/libraries").status_code == 401, "改口令后旧会话必须失效"

    wrong_old = api_client.post(
        "/api/auth/password",
        json={"old_password": password, "new_password": "whatever-123456"},
    )
    assert wrong_old.status_code == 401, wrong_old.text

    relogin = api_client.post(
        "/api/auth/login", json={"username": username, "password": "brand-new-password-123"}
    )
    assert relogin.status_code == 200, relogin.text

    too_short = api_client.post(
        "/api/auth/password",
        json={"old_password": "brand-new-password-123", "new_password": "short"},
    )
    assert too_short.status_code == 400, too_short.text


# --------------------------------------------------------------------------- 媒体库


def test_library_crud_and_validation_surface(
    api_client, api_login, build_tree, source_dir, library_root
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)

    # 故意用一个**还不存在**的目标目录：这样才能命中「目标目录…目前不存在」那条黄条
    # （`target_dir` 夹具会提前建好目录，那条警告就不会出现）。
    absent_target = library_root / "Brand"
    assert not absent_target.exists()
    library = _create_library(api_client, source_dir, absent_target)
    assert library["id"] > 0
    assert H.same_path(library["source_dir"], source_dir)
    assert H.same_path(library["target_dir"], absent_target)
    assert library["enabled"] is True and library["orphan_cleanup"] is True
    assert library["type"] == "Bangumi"

    # v2：处理器以 `processor_id` 为准，`processor` 只是值相同的 v1 兼容别名。
    assert library["processor_id"] == "bangumi" and library["processor"] == "bangumi"
    assert library["processor_name"] and library["processor_kind"] == "rename"
    assert library["processor_missing"] is False
    assert library["log_file"] == "bangumicache.log", library["log_file"]
    assert Path(library["log_path"]).name == "bangumicache.log"
    assert Path(library["log_path"]).parent.name == "logs", library["log_path"]

    warnings = library["warnings"]
    assert any("目前不存在" in text for text in warnings), warnings
    # A13 删除后不再有「日志连续性缺失」这类黄条。
    assert not any("日志连续性" in text or "HOST_PATH_MAP" in text for text in warnings), warnings

    listed = api_client.get("/api/libraries").json()
    assert listed["total"] == 1 and listed["limit"] == 50 and listed["offset"] == 0
    assert [item["id"] for item in listed["items"]] == [library["id"]]

    updated = api_client.put(
        f"/api/libraries/{library['id']}", json={**library, "name": "改名后的库", "enabled": False}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "改名后的库"
    assert updated.json()["enabled"] is False

    validation = api_client.post(f"/api/libraries/{library['id']}/validate")
    assert validation.status_code == 200, validation.text
    report = validation.json()
    assert report["ok"] is True, report
    assert report["errors"] == [], report
    assert report["source_exists"] is True and report["same_filesystem"] is True, report
    assert report["source_folders"] == EXPECTED_FOLDERS, report
    assert report["probe"]["ok"] is True, report
    assert report["library_id"] == library["id"]
    assert any("目前不存在" in text for text in report["warnings"]), report["warnings"]


def test_invalid_libraries_are_rejected(api_client, api_login, tmp_path, source_dir, target_dir):
    """v2 的媒体库校验：**处理器必须存在**（422），其余结构性错误仍是 400。

    `type` 在 v2 是**自由标签**（不再有枚举校验），所以 v1 那条「`type` 与 `processor`
    不匹配 → 400」已作废 —— 取而代之的是"`processor_id` 不存在 → 422"。
    """
    api_login(api_client)

    missing = api_client.post(
        "/api/libraries",
        json={
            "name": "处理器不存在",
            "type": "Bangumi",
            "processor_id": "no-such-processor",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        },
    )
    assert missing.status_code == 422, missing.text
    detail = missing.json()["detail"]
    assert detail[0]["loc"] == ["body", "processor_id"], detail
    assert "处理器 no-such-processor 不存在" in detail[0]["msg"], detail

    none = api_client.post(
        "/api/libraries",
        json={
            "name": "没给处理器",
            "type": "Bangumi",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        },
    )
    assert none.status_code == 422, none.text
    assert none.json()["detail"][0]["loc"] == ["body", "processor_id"], none.text

    same = api_client.post(
        "/api/libraries",
        json={
            "name": "同路径",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(source_dir),
            "target_dir": str(source_dir),
        },
    )
    assert same.status_code == 400, same.text
    assert "不能相同" in same.json()["detail"]

    # v2 起没有「允许的根路径」白名单：目标目录落在任何位置都合法。
    # 唯一的路径闸门是「不能相同 / 不能互相包含 / 必须在同一个文件系统」（见下一条用例）。
    outside = api_client.post(
        "/api/libraries",
        json={
            "name": "库外目录",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(source_dir),
            "target_dir": str(tmp_path / "outside"),
        },
    )
    assert outside.status_code == 201, outside.text
    assert H.same_path(outside.json()["target_dir"], tmp_path / "outside")

    custom = api_client.post(
        "/api/libraries",
        json={
            "name": "自定义库",
            "type": "Custom",
            "processor_id": "bangumi",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        },
    )
    assert custom.status_code == 201, custom.text
    # v2 的文案：`type=Custom` + rename 处理器时提醒"目录结构可能不满足"。
    assert any(
        "自定义库选择了重命名处理器" in text for text in custom.json()["warnings"]
    ), custom.json()["warnings"]


def test_library_create_and_update_reject_cross_mount(
    api_client, api_login, source_dir, target_dir, monkeypatch
):
    """v2 在**建库/改库时**校验源与目标是否同一个挂载。

    真实环境里把源与目标放到两个 bind 挂载（或两块盘）上，`os.link` 会直接返回
    EXDEV，所以写库前就用 `paths.check_same_mount()` 拦下（`False` 是可靠负判据）。
    这里伪造 `mount_id_for` 让两个目录“分属不同挂载”，断言 400 与冻结文案
    「跨挂载错误：…」，不依赖 CI 机器真的挂两个挂载点。
    """
    api_login(api_client)

    from app import paths as paths_module

    def fake_mount_id(path):
        text = str(path).replace("\\", "/")
        return ("1", "/downloads") if "/downloads" in text else ("2", "/library")

    monkeypatch.setattr(paths_module, "mount_id_for", fake_mount_id)

    rejected = api_client.post(
        "/api/libraries",
        json={
            "name": "跨挂载",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        },
    )
    assert rejected.status_code == 400, rejected.text
    detail = rejected.json()["detail"]
    assert "跨挂载错误" in detail and "EXDEV" in detail, detail

    monkeypatch.undo()
    library = _create_library(api_client, source_dir, target_dir)

    monkeypatch.setattr(paths_module, "mount_id_for", fake_mount_id)
    updated = api_client.put(
        f"/api/libraries/{library['id']}",
        json={
            "name": "冒烟番剧",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        },
    )
    assert updated.status_code == 400, updated.text
    assert "跨挂载错误" in updated.json()["detail"]


def test_library_delete_requires_confirm_and_keeps_hardlinks(
    api_client, api_login, build_tree, source_dir, target_dir, library_root
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)

    task_id = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, task_id)["status"] == "success"

    linked = [path for path in (library_root / "Bangumi").rglob("*") if path.is_file()]
    assert len(linked) == EXPECTED_LINKED

    refused = api_client.delete(f"/api/libraries/{library['id']}")
    assert refused.status_code == 400, refused.text
    detail = refused.json()["detail"]
    assert "confirm=true" in detail and "硬链接" in detail, detail

    deleted = api_client.delete(
        f"/api/libraries/{library['id']}", params={"confirm": "true"}
    )
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["deleted"] is True and body["hardlinks_kept"] is True
    assert body["library_id"] == library["id"]
    # 删库要顺手把本库的作品账本清干净（没有外键，删除动作必须看得见）。
    assert body["link_records_removed"] == EXPECTED_FOLDERS, body

    assert len([p for p in (library_root / "Bangumi").rglob("*") if p.is_file()]) == EXPECTED_LINKED
    for path in linked:
        assert path.exists(), f"删除媒体库不该动已建好的硬链接：{path}"
    assert api_client.get(f"/api/libraries/{library['id']}").status_code == 404
    assert api_client.delete("/api/libraries/999", params={"confirm": "true"}).status_code == 404


# --------------------------------------------------------------------------- 运行 / 任务


def test_run_creates_real_hardlinks_and_renames_end_to_end(
    api_client, api_login, build_tree, source_dir, target_dir, library_root
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)

    before_source = H.tree_snapshot(source_dir)
    task_id = _run_library(api_client, library["id"])
    detail = _wait_for_task(api_client, task_id)

    assert detail["status"] == "success", detail
    assert detail["error"] is None, detail
    result = detail["result"]
    assert result["linked"] == EXPECTED_LINKED, result
    assert result["renamed"] == EXPECTED_LINKED, result
    assert result["link_failed"] == 0 and result["rename_failed"] == 0, result
    assert result["folders_scanned"] == EXPECTED_FOLDERS, result
    assert result["bytes_linked"] == EXPECTED_BYTES, result
    assert result["orphans_removed"] == 0, result
    assert isinstance(result["duration_ms"], int) and result["duration_ms"] >= 0
    assert result["samples"]["renamed"], result
    assert {"from", "to"} <= set(result["samples"]["renamed"][0]), result

    # 目标树是**真硬链接**（不是复制）：同 (st_dev, st_ino) 且 nlink >= 2。
    target_files = sorted(p for p in (library_root / "Bangumi").rglob("*") if p.is_file())
    assert len(target_files) == EXPECTED_LINKED
    names = sorted(path.name for path in target_files)
    assert names == ["Show A S01E01.mkv", "Show A S01E02.ass", "Show B S01E03.mp4"], names
    for name, folder in (
        ("Show A S01E01.mkv", "Show A"),
        ("Show A S01E02.ass", "Show A"),
        ("Show B S01E03.mp4", "Show B"),
    ):
        original = {
            "Show A S01E01.mkv": "ep01.mkv",
            "Show A S01E02.ass": "ep02.ass",
            "Show B S01E03.mp4": "ep03.mp4",
        }[name]
        H.assert_real_hardlink(
            source_dir / folder / "Season 1" / original,
            library_root / "Bangumi" / folder / "Season 1" / name,
        )

    # 源目录一个字节都不许变（mtime/ino/mode 全等；nlink 不算，建链本来就会 +1）。
    assert H.tree_snapshot(source_dir) == before_source

    again = _run_library(api_client, library["id"])
    second = _wait_for_task(api_client, again)
    assert second["status"] == "success", second
    assert second["result"]["linked"] == 0, second["result"]
    assert second["result"]["skipped"] == EXPECTED_FOLDERS, second["result"]


def test_logs_and_sse_stream_share_the_same_frame_shape(
    api_client, api_login, build_tree, source_dir, target_dir
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    task_id = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, task_id)["status"] == "success"

    logs = api_client.get(f"/api/tasks/{task_id}/logs", params={"after_id": 0, "limit": 500})
    assert logs.status_code == 200, logs.text
    payload = logs.json()
    items = payload["items"]
    assert items, payload
    assert payload["last_id"] == items[-1]["id"]
    assert payload["task_id"] == task_id

    required = {"id", "ts", "level", "message", "stream"}
    for item in items:
        assert required <= set(item), item
        assert LOCAL_TIME_RE.match(item["ts"]), f"日志 ts 必须是容器本地时间：{item['ts']}"
        assert not ISO8601_RE.match(item["ts"]), "不再有 UTC ISO8601 的日志时间"
        assert item["level"] in ("DEBUG", "INFO", "WARNING", "ERROR"), item
        assert isinstance(item["message"], str) and item["message"]

    task = api_client.get(f"/api/tasks/{task_id}").json()
    assert LOCAL_TIME_RE.match(task["created_at"]), task
    assert any(item["stream"] == "stdout" for item in items), "脚本 stdout 必须被收进日志帧"

    tail = api_client.get(
        f"/api/tasks/{task_id}/logs", params={"after_id": payload["last_id"]}
    ).json()
    assert tail["items"] == [], tail

    events = _sse_events(api_client, f"/api/tasks/{task_id}/stream")
    names = [name for name, _ in events]
    assert names[0] == "task", f"首帧必须是任务快照，实际 {names[:3]}"
    assert "log" in names, names
    assert names[-1] == "done", names

    first_frame = events[0][1]
    assert first_frame["id"] == task_id
    assert first_frame["status"] == "success", first_frame
    assert "result" in first_frame

    log_frames = [data for name, data in events if name == "log"]
    assert log_frames, events
    assert set(log_frames[0]) == set(items[0]), (
        "SSE 的 log 帧必须与 /logs 的 items 逐字段同形：",
        sorted(log_frames[0]),
        sorted(items[0]),
    )
    for frame in log_frames:
        assert required <= set(frame), frame

    done = events[-1][1]
    assert done.get("task_id") == task_id, done
    assert done.get("status") == "success", done


def test_retry_and_cancel_follow_the_status_rules(
    api_client, api_login, build_tree, source_dir, target_dir, downloads_root
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    task_id = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, task_id)["status"] == "success"

    retry = api_client.post(f"/api/tasks/{task_id}/retry")
    assert retry.status_code == 409, retry.text
    assert "重试" in retry.json()["detail"]

    cancel = api_client.post(f"/api/tasks/{task_id}/cancel")
    assert cancel.status_code == 409, cancel.text
    assert "取消" in cancel.json()["detail"]

    # 造一个必失败的任务（源目录不存在），重试必须被接受并产生新任务。
    broken = _create_library(
        api_client, downloads_root / "NotThere", target_dir, name="坏库"
    )
    failed_id = _run_library(api_client, broken["id"])
    failed = _wait_for_task(api_client, failed_id)
    assert failed["status"] == "failed", failed
    assert "源目录不存在或不可访问" in failed["error"], failed

    retried = api_client.post(f"/api/tasks/{failed_id}/retry")
    assert retried.status_code in (200, 202), retried.text
    new_id = retried.json()["id"]
    assert new_id != failed_id
    assert retried.json()["trigger"] == "retry", retried.text
    assert _wait_for_task(api_client, new_id)["status"] == "failed"

    missing = api_client.post("/api/libraries/999/run", json={})
    assert missing.status_code == 404, missing.text


def test_stats_overview_counts_tasks_and_libraries(
    api_client, api_login, build_tree, source_dir, target_dir
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir)
    task_id = _run_library(api_client, library["id"])
    assert _wait_for_task(api_client, task_id)["status"] == "success"

    stats = api_client.get("/api/stats/overview")
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert set(body["tasks"]) == {"queued", "running", "success_24h", "failed_24h", "total"}, body
    assert body["tasks"]["total"] >= 1 and body["tasks"]["success_24h"] >= 1, body
    assert body["libraries"] == {"total": 1, "enabled": 1}, body
    assert [item["id"] for item in body["recent"]] == [task_id], body["recent"]
    assert set(body["queue"]) == {"running_task_id", "queued_count"}, body
    assert body["queue"]["queued_count"] == 0


# --------------------------------------------------------------------------- 钩子 / 设置 / 系统信息


def test_qbittorrent_hook_requires_a_token_and_matches_configured_libraries(
    api_client, api_login, build_tree, source_dir, target_dir, db_mod
):
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    library = _create_library(api_client, source_dir, target_dir, match_categories=["Bangumi"])
    token = db_mod.get_setting("hook_token")
    assert token, "初始化时必须生成 hook_token"

    params = {
        "name": "Show A",
        "hash": "deadbeef",
        "category": "Bangumi",
        "tags": "动漫",
        "content_path": str(source_dir / "Show A"),
        "save_path": str(source_dir),
        "root_path": str(source_dir),
    }

    assert api_client.post("/api/hooks/qbittorrent", params=params).status_code == 401
    assert api_client.post(
        "/api/hooks/qbittorrent", params={**params, "token": "wrong-token"}
    ).status_code == 401

    matched = api_client.post(
        "/api/hooks/qbittorrent", params={**params, "token": token}
    )
    assert matched.status_code == 200, matched.text
    body = matched.json()
    assert body["matched"] == 1, body
    assert len(body["tasks"]) == 1, body
    assert body["torrent"] == {"name": "Show A", "hash": "deadbeef"}, body
    assert body["ignored"] == [], body

    first = body["tasks"][0]
    task_id = first if isinstance(first, int) else first["id"]
    detail = _wait_for_task(api_client, task_id)
    assert detail["trigger"] == "hook", detail
    assert detail["library_id"] == library["id"], detail

    missed = api_client.post(
        "/api/hooks/qbittorrent",
        params={**params, "category": "电影", "token": token},
    ).json()
    assert missed["matched"] == 0, missed
    assert missed["tasks"] == [], missed
    assert library["name"] in missed["ignored"], missed
    assert missed["ignored_details"], missed
    assert missed["ignored_details"][0]["reason"], missed

    bearer = api_client.post(
        "/api/hooks/qbittorrent",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 200, bearer.text
    assert bearer.json()["matched"] == 1, bearer.text
    bearer_task = bearer.json()["tasks"][0]
    _wait_for_task(api_client, bearer_task if isinstance(bearer_task, int) else bearer_task["id"])

    # 扫描只给"当前没有排队/执行中任务"的库提交任务（`scanner._has_active_task`），
    # 所以必须先把上面那条 hook 任务等完，否则这里会 matched=0。
    scanned = api_client.post("/api/hooks/scan", params={"token": token})
    assert scanned.status_code == 200, scanned.text
    scan_body = scanned.json()
    assert scan_body["matched"] >= 1, scan_body
    assert len(scan_body["tasks"]) == scan_body["matched"], scan_body


def test_downloader_path_map_translates_the_content_path_before_scoping(
    api_client, api_login, build_tree, source_dir, target_dir
):
    """qB 容器路径 ``/downloads/...`` 经设置里的映射换算后才切得出一级文件夹。

    这正是「两边容器路径不一致」时唯一的正解：不换算的话 ``_first_folder_under`` 永远
    返回 None，每次回调都会退化成整库全量。
    """
    import app.paths as app_paths  # noqa: PLC0415 - 只在被测模块可导入时加载

    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    _create_library(api_client, source_dir, target_dir, match_categories=["Bangumi"])
    token = api_client.get("/api/settings").json()["hook_token"]

    saved = api_client.put(
        "/api/settings",
        json={"downloader_path_map": f"# qB 视角 => 应用视角\n/downloads => {source_dir}\n"},
    )
    assert saved.status_code == 200, saved.text
    assert "/downloads =>" in saved.json()["downloader_path_map"], saved.text

    matched = api_client.post(
        "/api/hooks/qbittorrent",
        params={
            "name": "Show A",
            "hash": "deadbeef",
            "category": "Bangumi",
            "content_path": "/downloads/Show A",
            "save_path": "/downloads",
            "token": token,
        },
    )
    assert matched.status_code == 200, matched.text
    body = matched.json()
    app_content = app_paths.normalize(str(source_dir / "Show A"))
    assert body["matched"] == 1, body
    assert body["translation"] == {
        "applied": True,
        "rule": f"/downloads => {app_paths.normalize(str(source_dir))}",
        "raw": "/downloads/Show A",
        "app": app_content,
        "warning": None,
    }, body
    assert any("已按映射" in warning for warning in body["warnings"]), body

    task_id = body["tasks"][0]
    detail = _wait_for_task(api_client, task_id if isinstance(task_id, int) else task_id["id"])
    assert detail["scope"] == {"mode": "folder", "folder": "Show A"}, detail
    # payload 里保留两个视角：排查「为什么跑了整库」时都要看。
    assert detail["payload"]["content_path"] == "/downloads/Show A", detail["payload"]
    assert detail["payload"]["content_path_app"] == app_content, detail["payload"]


def test_downloader_path_map_falls_back_to_full_when_the_translated_path_is_missing(
    api_client, api_login, build_tree, source_dir, target_dir
):
    """映射配错（右侧在应用容器里不可见）时：不换算、退回全量、给出警告，绝不 500。"""
    api_login(api_client)
    build_tree(source_dir, MEDIA_TREE)
    _create_library(api_client, source_dir, target_dir, match_categories=["Bangumi"])
    token = api_client.get("/api/settings").json()["hook_token"]
    wrong = api_client.put(
        "/api/settings", json={"downloader_path_map": "/downloads => /nope/definitely-missing\n"}
    )
    assert wrong.status_code == 200, wrong.text

    body = api_client.post(
        "/api/hooks/qbittorrent",
        params={
            "name": "Show A",
            "hash": "deadbeef",
            "category": "Bangumi",
            "content_path": "/downloads/Show A",
            "token": token,
        },
    ).json()
    assert body["matched"] == 1, body
    assert body["translation"]["applied"] is False, body
    assert body["translation"]["warning"], body
    assert any("不存在" in warning for warning in body["warnings"]), body

    task_id = body["tasks"][0]
    detail = _wait_for_task(api_client, task_id if isinstance(task_id, int) else task_id["id"])
    assert detail["scope"] == {"mode": "full"}, detail


def test_downloader_path_map_is_validated_on_save_and_round_trips(api_client, api_login, db_mod):
    api_login(api_client)
    assert api_client.get("/api/settings").json()["downloader_path_map"] == ""

    bad = api_client.put(
        "/api/settings", json={"downloader_path_map": "/downloads /vol1/1000/Download"}
    )
    assert bad.status_code == 400, bad.text
    assert "回调路径映射格式错误" in bad.json()["detail"], bad.text
    assert db_mod.get_setting("downloader_path_map") in ("", None), "400 不许落盘"

    text = (
        "# qB 视角 => 应用视角\n"
        "/downloads => /vol1/1000/Download/qbittorrent\n"
        "\n"
        "/downloads/Bangumi => /vol1/1000/Download/qbittorrent/Bangumi\n"
    )
    saved = api_client.put("/api/settings", json={"downloader_path_map": text})
    assert saved.status_code == 200, saved.text
    assert saved.json()["downloader_path_map"] == text, saved.text
    assert api_client.get("/api/settings").json()["downloader_path_map"] == text


def test_settings_password_semantics_are_exact(api_client, api_login, db_mod):
    api_login(api_client)

    initial = api_client.get("/api/settings").json()
    assert initial["qbittorrent_password"] == "", initial
    assert initial["hook_token"], initial

    cleared = api_client.put("/api/settings", json={"qbittorrent_password": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["qbittorrent_password"] == ""
    assert db_mod.get_setting("qbittorrent_password") in ("", None)

    stored = api_client.put("/api/settings", json={"qbittorrent_password": "s3cret-pass"})
    assert stored.status_code == 200, stored.text
    assert stored.json()["qbittorrent_password"] == "***", stored.text
    assert db_mod.get_setting("qbittorrent_password") == "s3cret-pass"

    kept = api_client.put("/api/settings", json={"qbittorrent_password": "***"})
    assert kept.status_code == 200, kept.text
    assert db_mod.get_setting("qbittorrent_password") == "s3cret-pass", "*** 必须表示保持原值"

    reread = api_client.get("/api/settings").json()
    assert reread["qbittorrent_password"] == "***", "已设置的密码只能回显 ***"
    assert "s3cret-pass" not in json.dumps(reread, ensure_ascii=False)

    rejected = api_client.put("/api/settings", json={"qbittorrent_password": 123})
    assert rejected.status_code == 400, rejected.text
    assert db_mod.get_setting("qbittorrent_password") == "s3cret-pass", "400 不许破坏原值"

    unknown = api_client.put("/api/settings", json={"not_a_key": "x"})
    assert unknown.status_code == 400, unknown.text
    assert "未知的设置项" in unknown.json()["detail"]

    bad_level = api_client.put("/api/settings", json={"log_level": "NOPE"})
    assert bad_level.status_code == 400, bad_level.text
    assert "日志级别" in bad_level.json()["detail"]


def test_system_info_exposes_paths_and_processors(api_client, api_login, source_dir, target_dir):
    """`/api/system/info` 的形状：`config_dir`/`processors`/`libraries`/`config`/`scan`；
    已废除的字段一律不出现。

    ``same_filesystem`` 判的是**第一个媒体库**的源/目标（``app/routers/system.py`` 的
    ``_same_filesystem_view``：没有库时按设计返回三态里的 ``null``），所以这里先建一个库，
    否则拿到的是 null 而不是本次要钉的 ``true``。
    """
    api_login(api_client)
    _create_library(api_client, source_dir, target_dir, name="系统信息库")

    info = api_client.get("/api/system/info")
    assert info.status_code == 200, info.text
    body = info.json()
    for key in (
        "version",
        "data_dir",
        "log_dir",
        "config_dir",
        "path_map",
        "ports",
        "mounts",
        "same_filesystem",
        "processors",
        "libraries",
        "config",
        "scan",
    ):
        assert key in body, (key, sorted(body))

    # 反向断言：下列已废除的字段一律不许再出现。
    assert "original_script_dir" not in body, sorted(body)
    assert "log_continuity" not in body, sorted(body)
    for retired in ("reference_script_dir", "scripts", "baseline_ok"):
        assert retired not in body, (retired, sorted(body))

    processors = body["processors"]
    assert processors["total"] == 3, processors
    assert processors["builtin_ids"] == ["hardlink_only", "bangumi", "movie"], processors
    assert [item["id"] for item in processors["items"]] == [
        "hardlink_only",
        "bangumi",
        "movie",
    ], processors["items"]

    # 本夹具用**显式空** HOST_PATH_MAP（开发机走宿主路径），所以两份映射都为空 —— 这是
    # 正确行为，不是缺陷。
    path_map = body["path_map"]
    assert set(path_map) == {"container_to_host", "host_to_container"}, path_map
    assert isinstance(path_map["container_to_host"], dict)
    assert isinstance(path_map["host_to_container"], dict)
    assert body["ports"]["container"] == 18081, body["ports"]
    assert body["same_filesystem"]["downloads_vs_library"] is True, body["same_filesystem"]

    config = body["config"]
    for key in ("config_dir", "files", "last_import_at", "last_export_at", "errors"):
        assert key in config, (key, sorted(config))
    assert Path(config["config_dir"]).name == "config", config["config_dir"]

    # `/api/system/scripts` 不存在 —— 老客户端必须拿到 404，
    # 而不是"200 + 空数组"（后者会让旧前端显示一张永远空白的卡片，看不出接口已经没了）。
    assert api_client.get("/api/system/scripts").status_code == 404


# --------------------------------------------------------------- v2 处理器 / 配置端点


def test_processor_endpoints_round_trip(api_client, api_login, source_dir, target_dir):
    """十条 v2 端点里的七条处理器端点，全部打**真实 HTTP**。

    覆盖的契约点（每一条都在 `docs/contracts/COMPATIBILITY.md` §3.3 有登记）：
    ``GET /api/processors`` 的**信封 + 全序稳定**（内置优先，其余按 id 升序，§3.3 第 9 条）、
    ``GET /api/processors/{id}`` 返回**规格本身**且没有 `spec` 包一层（第 11 条）、
    ``POST`` 建出来的自定义处理器**绝不借用内置日志名**（第 10 条）、
    `PUT` 的 id 不可改 + 规格非法给字段级 422、`POST .../validate` **永远 200**、
    `POST .../reset` 的 default/legacy 差异**只在** `traverse.reject_dirs` 与 `episode_fallback`、
    `DELETE` 内置 ⇒ 409、仍被库引用 ⇒ 409。
    """
    api_login(api_client)

    # ① 列表：信封形状 + 全序稳定（内置按 BUILTIN_IDS，其余按 id 升序）
    listing = api_client.get("/api/processors")
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert set(page) == {"items", "total", "limit", "offset"}, page
    assert page["total"] == 3, page
    assert [item["id"] for item in page["items"]] == ["hardlink_only", "bangumi", "movie"], page
    assert all(item["builtin"] is True for item in page["items"]), page

    # ② 单条 = 规格本身（traverse/naming 与 id 同层，没有 `spec` 包一层）
    detail = api_client.get("/api/processors/bangumi")
    assert detail.status_code == 200, detail.text
    bangumi = detail.json()
    assert "spec" not in bangumi, sorted(bangumi)
    assert bangumi["id"] == "bangumi" and bangumi["kind"] == "rename", bangumi
    assert bangumi["builtin"] is True and "reference_script" not in bangumi, bangumi
    assert bangumi["log_file"] == "bangumicache.log", bangumi
    assert isinstance(bangumi["traverse"]["reject_dirs"], list), bangumi["traverse"]
    assert bangumi["traverse"]["reject_dirs"], "default 预设必须带拒绝目录规则"
    assert api_client.get("/api/processors/no-such-processor").status_code == 404

    # ③ 新建：201 + builtin 被服务端钉死 + 不借用内置日志名
    created = api_client.post(
        "/api/processors",
        json={"id": "my_bangumi", "name": "我的番剧", "kind": "rename", "template": "bangumi"},
    )
    assert created.status_code == 201, created.text
    mine = created.json()
    assert mine["builtin"] is False and "reference_script" not in mine, mine
    assert mine["log_file"] not in {"bangumicache.log", "moviecache.log", "hardlink_only.log"}, mine
    assert mine["log_file"].endswith("cache.log"), mine
    assert mine["schema_version"] == 2, mine

    duplicated = api_client.post(
        "/api/processors",
        json={"id": "my_bangumi", "name": "重复", "kind": "rename", "template": "blank"},
    )
    assert duplicated.status_code == 409, duplicated.text
    bad_id = api_client.post(
        "/api/processors", json={"id": "Bad-ID", "name": "大小写非法", "kind": "rename"}
    )
    assert bad_id.status_code == 422, bad_id.text
    assert bad_id.json()["detail"][0]["loc"][0] == "body", bad_id.text

    # ④ PUT 全量替换：改 id ⇒ 422；规则非法 ⇒ 422 字段级（`loc` 逐段展开）；合法 ⇒ 200
    renamed = api_client.put("/api/processors/my_bangumi", json={**mine, "id": "other_id"})
    assert renamed.status_code == 422, renamed.text
    assert "不可修改" in renamed.json()["detail"][0]["msg"], renamed.text

    broken = {
        **mine,
        "traverse": {**mine["traverse"], "reject_dirs": [{"pattern": "("}]},
    }
    rejected = api_client.put("/api/processors/my_bangumi", json=broken)
    assert rejected.status_code == 422, rejected.text
    loc = rejected.json()["detail"][0]["loc"]
    assert loc[0] == "body" and "reject_dirs" in ".".join(map(str, loc)), rejected.text
    assert "pattern" in ".".join(map(str, loc)), rejected.text

    fixed = {**mine, "traverse": {**mine["traverse"], "reject_dirs": [{"pattern": "^Scans$"}]}}
    updated = api_client.put("/api/processors/my_bangumi", json=fixed)
    assert updated.status_code == 200, updated.text
    assert updated.json()["traverse"]["reject_dirs"] == [{"pattern": "^Scans$"}], updated.text
    assert api_client.get("/api/processors/my_bangumi").json()["traverse"]["reject_dirs"] == [
        {"pattern": "^Scans$"}
    ]

    # ⑤ `/validate` 永远 200（合法与非法都给 {ok, errors:[{field,message}]}）
    good = api_client.post("/api/processors/validate", json=fixed)
    assert good.status_code == 200, good.text
    assert good.json() == {"ok": True, "errors": []}, good.text
    bad = api_client.post("/api/processors/validate", json=broken)
    assert bad.status_code == 200, bad.text
    assert bad.json()["ok"] is False, bad.text
    assert bad.json()["errors"][0]["field"].endswith("pattern"), bad.text

    # ⑥ reset：default/legacy 的两处差异 = traverse.reject_dirs + episode_fallback；
    # 未知预设 ⇒ 422；自定义 ⇒ 400
    legacy = api_client.post("/api/processors/bangumi/reset", json={"preset": "legacy"})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["traverse"]["reject_dirs"] == [], legacy.text
    # legacy 复刻最朴素的旧口径的**字符串** "None" 兜底，未命中集数照旧渲染
    assert legacy.json()["episode_fallback"] == "None", legacy.text
    restored = api_client.post("/api/processors/bangumi/reset", json={"preset": "default"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["traverse"]["reject_dirs"], restored.text
    # 推荐默认留空 ⇒ 未命中集数时保留原文件名（不再造 `… S01ENone.mkv`）
    assert restored.json()["episode_fallback"] is None, restored.text
    unknown_preset = api_client.post("/api/processors/bangumi/reset", json={"preset": "nope"})
    assert unknown_preset.status_code == 422, unknown_preset.text
    custom_reset = api_client.post("/api/processors/my_bangumi/reset", json={"preset": "default"})
    assert custom_reset.status_code == 400, custom_reset.text
    assert "自定义处理器" in custom_reset.json()["detail"], custom_reset.text

    # ⑦ DELETE：内置 ⇒ 409；仍被媒体库引用 ⇒ 409（附引用清单）；无人引用 ⇒ 200
    builtin_delete = api_client.delete("/api/processors/bangumi")
    assert builtin_delete.status_code == 409, builtin_delete.text
    assert "内置处理器" in builtin_delete.json()["detail"], builtin_delete.text

    library = _create_library(
        api_client, source_dir, target_dir, name="引用自定义处理器的库", processor_id="my_bangumi"
    )
    referenced = api_client.delete("/api/processors/my_bangumi")
    assert referenced.status_code == 409, referenced.text
    assert "仍有媒体库在使用该处理器" in referenced.json()["detail"], referenced.text
    assert "引用自定义处理器的库" in referenced.json()["detail"], referenced.text

    disposable = api_client.post(
        "/api/processors",
        json={"id": "disposable", "name": "用完就删", "kind": "hardlink_only", "template": "hardlink_only"},
    )
    assert disposable.status_code == 201, disposable.text
    deleted = api_client.delete("/api/processors/disposable")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "id": "disposable"}, deleted.text
    assert api_client.get("/api/processors/disposable").status_code == 404
    assert library["processor_id"] == "my_bangumi", library


def test_config_endpoints_round_trip(api_client, api_login):
    """三条配置端点：`GET /api/config` / `POST /api/config/export` / `POST /api/config/reload`。

    `reload` 的 `force` 是**查询参数**（`app/routers/config.py:35`），不是 JSON body —— 这条在
    `docs/guides/TROUBLESHOOTING.md` §17 有专门的排错场景，所以这里也钉一遍。
    """
    api_login(api_client)

    snapshot = api_client.get("/api/config")
    assert snapshot.status_code == 200, snapshot.text
    body = snapshot.json()
    for key in (
        "config_dir",
        "files",
        "last_import_at",
        "last_export_at",
        "errors",
        "watcher_running",
        "interval_seconds",
    ):
        assert key in body, (key, sorted(body))
    config_dir = Path(body["config_dir"])
    assert config_dir.is_dir(), body["config_dir"]

    # ① export：四份 YAML（三个内置处理器 + libraries.yaml）全部原子写出
    exported = api_client.post("/api/config/export")
    assert exported.status_code == 200, exported.text
    written = exported.json()["written"]
    names = sorted(Path(item).name for item in written)
    assert {"bangumi.yaml", "hardlink_only.yaml", "movie.yaml", "libraries.yaml"} <= set(names), names
    for item in written:
        assert Path(item).is_file(), item
    assert [file for file in config_dir.rglob("*.tmp")] == [], "不得留下临时文件"
    assert all(entry["in_sync"] is True for entry in exported.json()["files"]), exported.json()["files"]

    # ② reload 走查询参数，`force=true` 时忽略哈希快速路径；成功导入后 errors 清空
    reloaded = api_client.post("/api/config/reload", params={"force": "true"})
    assert reloaded.status_code == 200, reloaded.text
    payload = reloaded.json()
    assert payload["result"]["ok"] is True, payload
    assert payload["result"]["errors"] == [], payload
    assert payload["errors"] == [], payload

    # ③ 外部改坏一个处理器文件 ⇒ 本次 reload 的 result.errors 非空且给出中文原因；
    #    注意 `files[].in_sync` 也会变成 False，而**顶层** `errors` 是"最近一次成功导入之后"的
    #    事件（同一遍里 `hardlink_only.yaml` 排在 `broken.yaml` 之后且导入成功，所以顶层是空的）。
    broken = config_dir / "processors" / "broken.yaml"
    broken.write_text("schema_version: 2\nkind: nonsense\n", encoding="utf-8")
    bad = api_client.post("/api/config/reload", params={"force": "true"})
    assert bad.status_code == 200, bad.text
    bad_body = bad.json()
    assert bad_body["result"]["ok"] is False, bad_body
    reasons = " ".join(bad_body["result"]["errors"])
    assert "nonsense" in reasons and "broken" in reasons, bad_body
    assert any(
        entry["relative"] == "processors/broken.yaml" and entry["in_sync"] is False
        for entry in bad_body["files"]
    ), bad_body["files"]

    broken.unlink()
    healed = api_client.post("/api/config/reload", params={"force": "true"})
    assert healed.status_code == 200, healed.text
    assert healed.json()["result"]["ok"] is True, healed.text
    assert healed.json()["result"]["errors"] == [], healed.text
    assert healed.json()["errors"] == [], healed.text
    assert all(entry["in_sync"] is True for entry in healed.json()["files"]), healed.text


# --------------------------------------------------------------------------- #
# 建库权限预检：保存时就实测（不依赖「预检」按钮）
# --------------------------------------------------------------------------- #


def test_library_create_still_allows_a_missing_source_but_validate_reports_it(
    api_client, api_login, tmp_path
):
    """源目录还不存在时**建库照旧允许**（「先建库、后有第一次下载」是正常流程），

    但「预检」必须报出来，且新增的 `permissions` 组要标明源目录不存在。
    权限类硬错（源不可读 / 目标不可写）才在保存时直接 400，见下一个用例。
    """
    api_login(api_client)
    missing_source = tmp_path / "downloads" / "no-such-dir"
    target = tmp_path / "library" / "Bangumi"
    target.mkdir(parents=True, exist_ok=True)
    response = api_client.post(
        "/api/libraries",
        json={
            "name": "源目录不存在",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(missing_source),
            "target_dir": str(target),
        },
    )
    assert response.status_code == 201, response.text
    library = response.json()

    report = api_client.post(f"/api/libraries/{library['id']}/validate?probe=false")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["ok"] is False, body
    assert any("源目录" in message and "不存在" in message for message in body["errors"]), body
    assert body["permissions"]["source_exists"] is False, body["permissions"]
    identity = body["permissions"]["identity"]
    assert "uid=" in identity and "gid=" in identity, body["permissions"]


def test_library_create_and_update_surface_permission_errors(
    api_client, api_login, source_dir, target_dir, monkeypatch
):
    """权限不可读/不可写时，建库和改库都立即 400；改库失败不能落库。"""
    api_login(api_client)
    from app import paths as paths_module

    fake_report = {
        "ok": False,
        "identity": "uid=1000, gid=999, 附加组=[999]",
        "errors": ["源目录 /downloads/Bangumi 对应用运行身份（uid=1000, gid=999, 附加组=[999]）不可读或不可进入"],
        "warnings": [],
        "source_exists": True,
        "target_exists": True,
        "probe": None,
    }
    monkeypatch.setattr(paths_module, "probe_media_access", lambda *args, **kwargs: dict(fake_report))

    rejected = api_client.post(
        "/api/libraries",
        json={
            "name": "权限不对",
            "type": "Bangumi",
            "processor_id": "bangumi",
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        },
    )
    assert rejected.status_code == 400, rejected.text
    detail = rejected.json()["detail"]
    assert "不可读" in detail and "uid=1000" in detail and "43" in detail, detail
    assert api_client.get("/api/libraries").json()["total"] == 0

    monkeypatch.undo()
    library = _create_library(api_client, source_dir, target_dir)
    monkeypatch.setattr(paths_module, "probe_media_access", lambda *args, **kwargs: dict(fake_report))
    updated = api_client.put(
        f"/api/libraries/{library['id']}",
        json={**library, "name": "改路径被拦"},
    )
    assert updated.status_code == 400, updated.text
    assert "不可读" in updated.json()["detail"]
    assert api_client.get(f"/api/libraries/{library['id']}").json()["name"] == library["name"]


def test_validate_reports_the_permission_probe_for_a_healthy_library(
    api_client, api_login, source_dir, target_dir
):
    """「预检」接口也带上权限体检结果，方便用户一次看到全部结论。"""
    api_login(api_client)
    library = _create_library(api_client, source_dir, target_dir)
    report = api_client.post(f"/api/libraries/{library['id']}/validate").json()
    assert report["permissions"]["ok"] is True, report["permissions"]
    assert report["permissions"]["identity"], report["permissions"]
    assert report["permissions"]["probe"]["ok"] is True, report["permissions"]


def test_static_frontend_is_served_without_browser_cache(api_client):
    """静态前端必须带 ``Cache-Control: no-store``。

    缺这个头时浏览器会按 ``Last-Modified`` 推算"启发式新鲜期"，期间普通刷新也
    不会重新校验 —— 部署后旧的 js/css 会被继续直接使用（改密码提示就曾因此
    看起来"没生效"，用户必须强制刷新才拿到新文件）。no-store 让每次加载都重新
    取；局域网几百 KB 的代价可忽略。
    """
    index = api_client.get("/")
    if index.status_code != 200 or "text/html" not in index.headers.get("content-type", ""):
        pytest.skip("测试环境没有挂载静态前端目录")
    assert index.headers["cache-control"] == "no-store"
    script = api_client.get("/js/app.js")
    assert script.status_code == 200, script.text
    assert script.headers["cache-control"] == "no-store"

