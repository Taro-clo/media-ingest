#!/usr/bin/env python3
"""全量功能验收（56 项）：经 HTTP + ``docker exec`` 检查一个**正在运行**的实例。

跑在部署宿主上（宿主 python3，不用容器里的解释器），默认访问
``http://127.0.0.1:18087`` 上的容器 ``media-ingest-test``。全部参数可用环境变量覆盖：

===================  ==========================================  ============================
环境变量             含义                                        默认值
===================  ==========================================  ============================
``MLM_BASE``         被测实例基址                                ``http://127.0.0.1:18087``
``MLM_CONTAINER``    容器名（用于 docker exec 断言）             ``media-ingest-test``
``MLM_EXPECT_VERSION`` 期望 ``app.__version__``                   ``1.0.0``
``MLM_SRC``          测试源目录（qbittorrent 下载目录）          ``…/Download/qbittorrent/Bangumi``
``MLM_TGT``          测试目标库（会被建链/改名）                 ``…/Library/TestLibrary``
``MLM_LIB_NAME``     库名                                        ``全量验收库``
``MLM_LOG_FILE``     日志文件名                                  ``bangumi.log``
``MLM_ADMIN_PW``     实例管理员口令（**必须**提供，无默认值）    ``—``
``MLM_QBT_URL/USER/PASS``  qBittorrent WebAPI（``PASS`` **必须**提供）  ``http://192.168.1.100:18080``
``MLM_OUT``          结果目录                                    ``/tmp/mlm-acceptance``
===================  ==========================================  ============================

结果落盘 ``<MLM_OUT>/FULL-<版本>.json`` 与 ``.md``。
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_env = os.environ.get
BASE = _env("MLM_BASE", "http://127.0.0.1:18087")
QBT_URL = _env("MLM_QBT_URL", "http://192.168.1.100:18080")
QBT_USER = _env("MLM_QBT_USER", "admin")
QBT_PASS = _env("MLM_QBT_PASS", "")  # 真实口令不进仓库：必须由调用方用环境变量提供
SRC = _env("MLM_SRC", "/vol1/1000/Download/qbittorrent/Bangumi")
TGT = _env("MLM_TGT", "/vol1/1000/Library/TestLibrary")
LIB_NAME = _env("MLM_LIB_NAME", "全量验收库")
LOG_FILE = _env("MLM_LOG_FILE", "bangumi.log")
ADMIN_PW = _env("MLM_ADMIN_PW", "")  # 同上
CONTAINER = _env("MLM_CONTAINER", "media-ingest-test")
EXPECT_VERSION = _env("MLM_EXPECT_VERSION", "1.0.0")
SHORT = EXPECT_VERSION.replace(".", "")
HEALTH_KEYS = ["db", "data_dir", "downloads_readable", "library_writable", "processors", "config"]
OUT = Path(_env("MLM_OUT", "/tmp/mlm-acceptance"))
OUT.mkdir(parents=True, exist_ok=True)

_missing = [n for n, v in (("MLM_ADMIN_PW", ADMIN_PW), ("MLM_QBT_PASS", QBT_PASS)) if not v]
if _missing:
    print(
        "缺少必需的环境变量：" + "、".join(_missing)
        + " —— 真实口令**不进仓库**，请在调用时通过环境变量提供。",
        file=sys.stderr,
    )
    sys.exit(2)

RESULTS = []
report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "base": BASE, "target": TGT}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + (" | " + str(detail) if detail else ""), flush=True)


_cj = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


def req(method, path, body=None, form=None, timeout=180):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, {"error": repr(e)}


def docker_exec(args, timeout=180):
    p = subprocess.run(["docker", "exec", CONTAINER] + args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def db_set(key, value):
    return docker_exec(["python", "-c",
                        "import sys; from app.db import set_setting; set_setting(sys.argv[1], sys.argv[2])",
                        key, value])


def unwrap(body):
    if isinstance(body, dict) and isinstance(body.get("settings"), dict):
        return body["settings"]
    if isinstance(body, dict) and isinstance(body.get("item"), dict):
        return body["item"]
    return body if isinstance(body, dict) else {}


def tasks():
    st, body = req("GET", "/api/tasks?limit=200")
    items = body.get("items") if isinstance(body, dict) else body
    return items or []


def wait_idle(timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        busy = [t for t in tasks() if t.get("status") in ("queued", "running")]
        if not busy:
            return True, ""
        time.sleep(2)
    return False, "仍有任务未结束"


def run_task(lib_id, mode="full", folder=None, timeout=1800):
    before = max([t.get("id", 0) for t in tasks()] or [0])
    scope = {"mode": mode}
    if folder:
        scope["folder"] = folder
    st, body = req("POST", f"/api/libraries/{lib_id}/run", {"scope": scope, "force": False})
    if st not in (200, 202):
        return st, None
    deadline = time.time() + timeout
    task_id = None
    while time.time() < deadline:
        for t in tasks():
            if t.get("id", 0) > before and t.get("library_id") == lib_id:
                task_id = t["id"]
                if t.get("status") not in ("queued", "running"):
                    st2, full = req("GET", f"/api/tasks/{task_id}")
                    return st, (full if st2 == 200 else t)
        time.sleep(3)
    return st, {"id": task_id, "status": "timeout"}


def counters(task):
    deep = task.get("result") or {}
    samples = deep.get("samples") or {}

    def num(key):
        for src in (task, deep):
            v = src.get(key)
            if isinstance(v, (int, float)):
                return int(v)
        v = samples.get(key)
        if isinstance(v, list):
            return len(v)
        return None

    out = {}

    def sample_len(key):
        v = deep.get(key)
        if isinstance(v, list):
            return len(v)
        v = samples.get(key)
        if isinstance(v, list):
            return len(v)
        return None

    out["linked"] = num("linked")
    out["link_failed"] = num("link_failed")
    out["skipped"] = num("skipped")
    out["folders_scanned"] = num("folders_scanned")
    out["renamed"] = num("renamed")
    out["rename_failed"] = num("rename_failed")
    out["extras_classified"] = num("extras_classified")
    out["name_collisions"] = num("name_collisions")
    out["kept_unresolved_episode"] = num("kept_unresolved_episode")
    out["unscoped_works"] = sample_len("unscoped_works") if sample_len("unscoped_works") is not None else num("unscoped_works")
    if out["kept_unresolved_episode"] is None:
        out["kept_unresolved_episode"] = sample_len("unresolved_episode")
    if out["name_collisions"] is None:
        out["name_collisions"] = sample_len("name_collisions")
    if out["extras_classified"] is None:
        out["extras_classified"] = sample_len("extras_classified")
    return out


def manifest(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                out[os.path.relpath(p, root)] = os.stat(p).st_size
            except OSError:
                out[os.path.relpath(p, root)] = -1
    return out


def target_stats(root):
    works = files = hard = 0
    total = 0
    if not os.path.isdir(root):
        return {"works": 0, "files": 0, "hardlinked": 0, "bytes": 0, "exists": False}
    for name in os.listdir(root):
        if os.path.isdir(os.path.join(root, name)):
            works += 1
    for dirpath, _dirs, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files += 1
            total += st.st_size
            if st.st_nlink >= 2:
                hard += 1
    return {"works": works, "files": files, "hardlinked": hard, "bytes": total, "exists": True}


def board():
    st, body = req("GET", "/api/todos?status=open&limit=1000")
    items = body.get("items") if isinstance(body, dict) else body
    items = items or []
    by_kind = {}
    for it in items:
        by_kind[it.get("kind")] = by_kind.get(it.get("kind"), 0) + 1
    total = body.get("total") if isinstance(body, dict) else None
    return {"total": total if total is not None else len(items), "by_kind": by_kind,
            "items": [{"id": i.get("id"), "kind": i.get("kind"), "title": i.get("title")} for i in items]}


def qbt_version_direct():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        d = urllib.parse.urlencode({"username": QBT_USER, "password": QBT_PASS}).encode()
        r = urllib.request.Request(QBT_URL + "/api/v2/auth/login", data=d)
        with op.open(r, timeout=20) as resp:
            if b"Ok" not in resp.read():
                return None
        with op.open(QBT_URL + "/api/v2/app/version", timeout=20) as resp:
            return resp.read().decode().strip()
    except Exception:  # noqa: BLE001
        return None


def main():
    # ---------- A 系统基线 ----------
    st, body = req("POST", "/api/auth/setup", {"password": ADMIN_PW, "username": "admin"})
    check("A1 初始化/登录可用", st in (200, 201, 409), f"HTTP {st}")
    st, body = req("POST", "/api/auth/login", {"password": ADMIN_PW, "username": "admin"})
    check("A2 登录", st == 200, f"HTTP {st}")

    # ---------- A0 清理：保证从零开始（删遗留库 / 目标树 / 缓存日志） ----------
    st, libs0 = req("GET", "/api/libraries")
    l0 = libs0.get("items") if isinstance(libs0, dict) else libs0
    stale = [l for l in (l0 or []) if l.get("name") == LIB_NAME or l.get("target_dir") == TGT]
    for l in stale:
        st_del, _ = req("DELETE", f"/api/libraries/{int(l['id'])}?confirm=true")
        print(f"  清理遗留库 id={l.get('id')} HTTP {st_del}", flush=True)
    if stale:
        wait_idle(timeout=600)
    subprocess.run(["rm", "-rf", TGT], capture_output=True)
    wait_idle(timeout=300)
    st, libs0b = req("GET", "/api/libraries")
    l0b = libs0b.get("items") if isinstance(libs0b, dict) else libs0b
    left = [l.get("id") for l in (l0b or []) if l.get("name") == LIB_NAME or l.get("target_dir") == TGT]
    busy = [t for t in tasks() if t.get("status") in ("queued", "running")]
    check("A0 现场已清空（无遗留库 / 无目标树 / 无排队任务）",
          not left and not os.path.exists(TGT) and not busy,
          f"deleted={[l.get('id') for l in stale]} 剩余={left} 目标树存在={os.path.exists(TGT)} 忙={len(busy)}")

    st, health = req("GET", "/api/system/health")
    hc = unwrap(health)
    checks = hc.get("checks") or {}
    check("A3 health 六项（processors/config 全必需）", set(checks.keys()) == set(HEALTH_KEYS),
          f"keys={sorted(checks.keys())}")
    check("A4 health 六项全 true", all(checks.get(k) is True for k in HEALTH_KEYS), json.dumps(checks, ensure_ascii=False))
    check(f"A5 版本号 {EXPECT_VERSION}", hc.get("version") == EXPECT_VERSION, f"version={hc.get('version')}")
    report["health"] = hc

    # ---------- B 建库 ----------
    st, libs = req("GET", "/api/libraries")
    llist = libs.get("items") if isinstance(libs, dict) else libs
    existing = [l for l in (llist or []) if l.get("name") == LIB_NAME]
    check("B1 现场无同名遗留库", not existing, f"遗留={[l.get('id') for l in existing]}")
    st, created = req("POST", "/api/libraries", {
        "name": LIB_NAME, "processor": "bangumi", "type": "Bangumi",
        "source_dir": SRC, "target_dir": TGT, "log_file": LOG_FILE,
        "orphan_cleanup": False, "match_categories": [], "match_tags": [],
        "match_path_prefixes": [SRC],
    })
    lib = unwrap(created)
    lib_id = lib.get("id")
    check("B2 建库", st in (200, 201) and bool(lib_id), f"HTTP {st} id={lib_id} {str(created)[:160]}")
    report["library_id"] = lib_id
    if not lib_id:
        finish(1)
        return 1
    src_before = manifest(SRC)
    check("B3 源树清单 8534 文件", len(src_before) == 8534, f"n={len(src_before)}")
    report["src_files"] = len(src_before)
    st, ldet = req("GET", f"/api/libraries/{lib_id}")
    ldet = unwrap(ldet)
    check("B4 库详情可读且无脚本相关字段", st == 200 and "reference_script" not in ldet,
          f"HTTP {st} target_exists={ldet.get('target_exists')}")

    # ---------- C 全量跑（出厂判据） ----------
    st, task = run_task(lib_id, "full", timeout=2400)
    check("C1 全量任务结束", st in (200, 202) and task and task.get("status") == "success",
          f"HTTP {st} status={task.get('status') if task else None}")
    report["task_full"] = task
    c = counters(task or {})
    report["counters_full"] = c
    check("C2 folders_scanned 54", c["folders_scanned"] == 54, str(c["folders_scanned"]))
    check("C3 linked 8534", c["linked"] == 8534, str(c["linked"]))
    check("C4 link_failed 0", c["link_failed"] == 0, str(c["link_failed"]))
    check("C5 renamed 1251", c["renamed"] == 1251, str(c["renamed"]))
    check("C6 rename_failed 0", c["rename_failed"] == 0, str(c["rename_failed"]))
    # 出厂默认判据含 `CM集` ⇒ 5 个 `… CM集 (BD …).mkv` 从「无法解析集号」转为
    # 「特典排除」：kept_unresolved_episode 54→49、extras_classified 44→49，两者之和不变（98）。
    check("C7 kept_unresolved_episode 49", c["kept_unresolved_episode"] == 49, str(c["kept_unresolved_episode"]))
    check("C8 extras_classified 49", c["extras_classified"] == 49, str(c["extras_classified"]))
    check("C9 name_collisions 34", c["name_collisions"] == 34, str(c["name_collisions"]))
    check("C10 unscoped_works 7（新可见行为）", c["unscoped_works"] == 7, str(c["unscoped_works"]))

    # ---------- D 目标树 ----------
    ts = target_stats(TGT)
    report["target_stats"] = ts
    check("D1 目标树 54 个作品目录", ts["works"] == 54, str(ts["works"]))
    check("D2 目标树 8534 个文件", ts["files"] == 8534, str(ts["files"]))
    check("D3 8534 个文件全是硬链接", ts["hardlinked"] == 8534, f"hardlinked={ts['hardlinked']}")

    # ---------- E 幂等复跑 ----------
    st, task2 = run_task(lib_id, "full", timeout=1200)
    c2 = counters(task2 or {})
    report["counters_rerun"] = c2
    check("E1 复跑任务 success", task2 and task2.get("status") == "success", f"status={task2.get('status') if task2 else None}")
    check("E2 复跑 linked 0", c2["linked"] == 0, str(c2["linked"]))
    check("E3 复跑 skipped 54", c2["skipped"] == 54, str(c2["skipped"]))
    check("E4 复跑 rename_failed 0", c2["rename_failed"] == 0, str(c2["rename_failed"]))

    # ---------- F 源树零改动 ----------
    src_after = manifest(SRC)
    only_before = sorted(set(src_before) - set(src_after))
    only_after = sorted(set(src_after) - set(src_before))
    changed = [k for k in set(src_before) & set(src_after) if src_before[k] != src_after[k]]
    check("F1 源树零改动", not only_before and not only_after and not changed,
          f"仅前 {len(only_before)} / 仅后 {len(only_after)} / 变化 {len(changed)}")

    # ---------- G 看板 ----------
    b = board()
    report["board"] = {"total": b["total"], "by_kind": b["by_kind"]}
    check("G1 看板 total 90", b["total"] == 90, f"total={b['total']} by={b['by_kind']}")
    check("G2 看板构成 49+34+7", b["by_kind"] == {"unresolved_episode": 49, "rename_suffix": 34, "unscoped_works": 7},
          json.dumps(b["by_kind"], ensure_ascii=False))
    check("G3 看板总数自洽", sum(b["by_kind"].values()) == b["total"], f"{sum(b['by_kind'].values())} vs {b['total']}")
    st, summ = req("GET", "/api/todos/summary")
    sm = unwrap(summ)
    check("G4 /api/todos/summary 一致", st == 200 and sm.get("open") == 90, f"HTTP {st} open={sm.get('open')}")

    # ---------- H qBittorrent ----------
    direct = qbt_version_direct()
    report["qbt_direct"] = direct
    check("H1 宿主直连 qB 可用（对照）", bool(direct), f"version={direct}")
    st, put = req("PUT", "/api/settings", {
        "qbittorrent_url": QBT_URL, "qbittorrent_username": QBT_USER, "qbittorrent_password": QBT_PASS,
    })
    check("H2 保存 qB 接入设置", st == 200, f"HTTP {st}")
    st, got = req("GET", "/api/settings")
    g = unwrap(got)
    check("H3 设置回读（密码脱敏）", g.get("qbittorrent_url") == QBT_URL and g.get("qbittorrent_username") == QBT_USER
          and g.get("qbittorrent_password") == "***",
          f"url={g.get('qbittorrent_url')} user={g.get('qbittorrent_username')} pw={g.get('qbittorrent_password')}")
    st, t1 = req("POST", "/api/settings/test-qbittorrent", {"url": QBT_URL, "username": QBT_USER, "password": QBT_PASS})
    check("H4 测试连接成功", st == 200 and t1.get("ok") is True and bool(t1.get("version")),
          json.dumps(t1, ensure_ascii=False))
    st, t2 = req("POST", "/api/settings/test-qbittorrent", {"url": QBT_URL, "username": QBT_USER, "password": "wrong-pw"})
    check("H5 错误密码被识别", st == 200 and t2.get("ok") is False, json.dumps(t2, ensure_ascii=False))
    st, t3 = req("POST", "/api/settings/test-qbittorrent", {"url": "", "username": "", "password": ""})
    check("H6 空地址 400", st == 400, f"HTTP {st}")
    st, t4 = req("POST", "/api/settings/test-qbittorrent", {})
    check("H7 空 body 走已存设置也能成功", st == 200 and t4.get("ok") is True, json.dumps(t4, ensure_ascii=False))
    rc, so, se = db_set("scan_tick_seconds", "5")
    check("H8 扫描节奏调成 5s", rc == 0, f"rc={rc} {so} {se}"[:200])
    ok_scan = False
    detail = ""
    for _ in range(40):
        st, ov = req("GET", "/api/stats/overview")
        qb = (unwrap(ov)).get("qbittorrent") or {}
        if qb.get("status") == "ok" and qb.get("version"):
            ok_scan = True
            detail = json.dumps(qb, ensure_ascii=False)
            break
        detail = json.dumps(qb, ensure_ascii=False)
        time.sleep(3)
    check("H9 扫描器刷新 qB 状态", ok_scan, detail)

    # ---------- I 回调 hook ----------
    st, got = req("GET", "/api/settings")
    token = (unwrap(got)).get("hook_token") or ""
    check("I1 能读到 hook token", len(token) >= 20, f"len={len(token)}")
    st, bad = req("POST", f"/api/hooks/qbittorrent?token=deadbeef", form={"name": "x", "hash": "y"})
    check("I2 错误 token 401", st == 401, f"HTTP {st}")
    src_dirs = sorted([d for d in os.listdir(SRC) if os.path.isdir(os.path.join(SRC, d))])
    work = src_dirs[0] if src_dirs else ""
    st, h1 = req("POST", f"/api/hooks/qbittorrent?token={token}",
                 form={"name": work, "hash": "testhash2212", "category": "test", "tags": "",
                       "content_path": f"{SRC}/{work}", "save_path": SRC, "root_path": SRC})
    check("I3 正确 token 命中库", st == 200 and h1.get("matched") == 1,
          f"HTTP {st} matched={h1.get('matched')} tasks={h1.get('tasks')} warn={h1.get('warnings')}")
    report["hook_matched"] = h1 if isinstance(h1, dict) else {}
    if h1.get("tasks"):
        tid = h1["tasks"][0]
        for _ in range(60):
            st2, tk = req("GET", f"/api/tasks/{tid}")
            if unwrap(tk).get("status") not in ("queued", "running"):
                break
            time.sleep(3)
        tk = unwrap(tk)
        c3 = counters(tk)
        check("I4 回调任务成功且只扫 1 个作品目录", tk.get("status") == "success" and c3["folders_scanned"] == 1,
              f"status={tk.get('status')} folders_scanned={c3['folders_scanned']} trigger={tk.get('trigger')}")
    st, h2 = req("POST", f"/api/hooks/qbittorrent?token={token}",
                 form={"name": "outside", "hash": "testhash2212b", "content_path": "/vol1/1000/Library",
                       "save_path": "/vol1/1000/Library", "root_path": "/vol1/1000"})
    check("I5 库外路径不命中且给出理由", st == 200 and h2.get("matched") == 0 and bool(h2.get("ignored_details")),
          json.dumps(h2.get("ignored_details"), ensure_ascii=False)[:220])
    st, s1 = req("POST", f"/api/hooks/scan?token={token}")
    check("I6 /api/hooks/scan 正确 token", st == 200, f"HTTP {st} {str(s1)[:120]}")
    st, s2 = req("POST", "/api/hooks/scan?token=deadbeef")
    check("I7 /api/hooks/scan 错误 token 401", st == 401, f"HTTP {st}")
    st, h3 = req("POST", f"/api/hooks/qbittorrent?token={token}",
                 form={"name": work, "hash": "testhash2212c", "content_path": f"{SRC}/{work}"})
    check("I8 只传最少参数也不报错", st == 200 and "matched" in h3, f"HTTP {st} matched={h3.get('matched')}")

    wait_idle(timeout=600)

    # ---------- J 其它 API 抽检 ----------
    tlist = tasks()
    check("J1 任务列表信封", isinstance(tlist, list) and len(tlist) >= 2, f"n={len(tlist)}")
    report["tasks"] = [{"id": t.get("id"), "status": t.get("status"), "trigger": t.get("trigger"),
                        "scope": t.get("scope")} for t in tlist]
    st, works = req("GET", f"/api/libraries/{lib_id}/works?limit=200")
    wl = works.get("items") if isinstance(works, dict) else works
    check("J2 作品账本 54 条", st == 200 and len(wl or []) == 54 and (works.get("total") == 54),
          f"HTTP {st} n={len(wl or [])} total={works.get('total') if isinstance(works, dict) else None}")
    st, tun = req("GET", "/api/todos?kind=unscoped_works&status=open&limit=50")
    ti = tun.get("items") if isinstance(tun, dict) else tun
    check("J3 看板可按 kind 过滤（unscoped_works=7）", st == 200 and len(ti or []) == 7, f"HTTP {st} n={len(ti or [])}")
    st, ov2 = req("GET", "/api/stats/overview")
    ov2d = unwrap(ov2)
    check("J4 仪表盘仍返回 qB 状态块", st == 200 and isinstance(ov2d.get("qbittorrent"), dict),
          json.dumps(ov2d.get("qbittorrent"), ensure_ascii=False))
    rc, logs, _ = docker_exec(["sh", "-c", "ls /data/logs/ | head -20"])
    check("J5 日志仍在数据卷里", rc == 0 and LOG_FILE in logs, f"logs={logs.replace(chr(10), ' ')}")
    rc, hc2, _ = docker_exec(["sh", "/usr/local/bin/mlm-healthcheck"])
    check("J6 健康检查脚本判定通过", rc == 0, hc2[-300:])

    # ---------- K 结论 ----------
    rc, ps, _ = docker_exec(["sh", "-c", "echo alive"])
    check("K1 容器仍存活", rc == 0, ps)
    finish(0)
    return 0


def finish(rc):
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["summary"] = {"pass": passed, "fail": len(failed), "failed": failed}
    (OUT / f"FULL-{SHORT}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# v{EXPECT_VERSION} 全量功能验收", "",
             f"- 实例：`{CONTAINER}` @ {BASE}",
             f"- 起止：{report['started']} → {report['finished']}",
             f"- 结果：**PASS {passed} / FAIL {len(failed)}**", ""]
    for n, ok, d in RESULTS:
        lines.append(f"- [{'x' if ok else ' '}] {n}" + (f" — {d}" if d else ""))
    lines.append("")
    lines.append("## 关键计数")
    lines.append("```json")
    lines.append(json.dumps({k: report.get(k) for k in
                             ("health", "counters_full", "counters_rerun", "target_stats", "board",
                              "qbt_direct", "src_files", "library_id")}, ensure_ascii=False, indent=1))
    lines.append("```")
    (OUT / f"FULL-{SHORT}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"=== FULL{SHORT} RESULT PASS={passed} FAIL={len(failed)} ===", flush=True)
    for n, d in failed:
        print(f"  FAILED {n} | {d}", flush=True)
    sys.exit(rc)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("EXCEPTION", False, repr(e))
        finish(2)
