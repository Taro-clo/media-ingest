"""全局重命名后缀白名单（``settings.rename_extensions``）。

三层解析（``app/processors/overrides.py``）：**库覆盖 → 全局设置 → 处理器默认**。
全局留空是默认值 ⇒ 行为与加入该能力之前逐字一致；这一层是“新增能力”，不是替换。

这里同时钉住两件容易回退的事：

* 白名单必须是**大小写归一 + 去重**后的 ``.xxx`` 形状 —— 否则用户会得到一个
  “看着生效、其实一个文件都匹配不上”的清单（比直接报错难查得多）；
* 白名单是**运行时**判定，不能只在保存时校验一遍就被别的层覆盖掉。
"""

from __future__ import annotations

import json

import pytest

import processor_helpers as P


def _spec(proc_defaults, extensions) -> dict:
    """以 bangumi legacy（= 最朴素的旧口径逐字语义）为底，只换后缀清单与命名拼装。"""
    spec = proc_defaults.preset_spec("bangumi", "legacy")
    spec["extensions"] = list(extensions)
    spec["naming"] = {
        # `ext` 自带前导点（= ".mkv"），所以这里不再补字面量点。
        "expressions": {},
        "segments": [{"param": "title"}, {"param": "ext"}],
    }
    return spec


def _tree(root) -> None:
    P.write_tree(root, {"Show A/Season 1/a.mkv": 5, "Show A/Season 1/b.ts": 7})


# --------------------------------------------------------------------------- 归一化


def test_normalize_extensions_is_lowercased_deduplicated_and_junk_tolerant():
    overrides = pytest.importorskip("app.processors.overrides")

    assert overrides.normalize_extensions([".MKV", "mkv", "", None, ".ts", ".mkv", 7]) == [".mkv", ".ts"]
    assert overrides.normalize_extensions(None) == []
    assert overrides.normalize_extensions(".avi") == [".avi"]
    # 把多项塞进**一项**（网页 textarea 里写 ".mkv .mp4 .ass"）必须被拆开 ——
    # 否则 `str.endswith((".mkv .mp4 .ass",))` 永远为假：白名单"看着生效、其实一个都不匹配"。
    assert overrides.normalize_extensions([".mkv .mp4 .ass"]) == [".mkv", ".mp4", ".ass"]
    assert overrides.normalize_extensions([".MKV,.mp4、.ass; .ts"]) == [".mkv", ".mp4", ".ass", ".ts"]
    assert overrides.normalize_extensions([".mk v"]) == [".mk"], "空白拆开后，合法碎片留下、碎片丢弃"
    assert overrides.normalize_extensions([".m/kv", "..", "mkv", ".tar.gz"]) == [".tar.gz"]
    # 存坏了的设置项不许把引擎拖下水：读出来就是“未设置”。
    assert overrides.read_global_extensions("这不是 JSON") == []
    assert overrides.read_global_extensions(json.dumps({"a": 1})) == []


# --------------------------------------------------------------------------- 设置接口


def test_settings_round_trip_normalises_and_validates_suffixes(api_client, api_login, db_mod):
    api_login(api_client)

    initial = api_client.get("/api/settings").json()
    assert initial["rename_extensions"] == [], "默认必须是空 = 跟随处理器（与 v2.0 行为一致）"

    saved = api_client.put("/api/settings", json={"rename_extensions": [".MKV", ".ts", ".mkv"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["rename_extensions"] == [".mkv", ".ts"], saved.text
    assert json.loads(db_mod.get_setting("rename_extensions")) == [".mkv", ".ts"]

    # 真实事故（一次真实建库被卡成"改名 0 个"）：网页 textarea 里写
    # ".mkv .mp4 .ass" —— 一项里塞了三个后缀。若原样存成**一项**，运行时
    # `name.endswith((".mkv .mp4 .ass",))` 永远为假，全程没有任何报错。
    spaced = api_client.put("/api/settings", json={"rename_extensions": [".mkv .mp4 .ass"]})
    assert spaced.status_code == 200, spaced.text
    assert spaced.json()["rename_extensions"] == [".mkv", ".mp4", ".ass"], spaced.text
    assert json.loads(db_mod.get_setting("rename_extensions")) == [".mkv", ".mp4", ".ass"]

    cleared = api_client.put("/api/settings", json={"rename_extensions": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["rename_extensions"] == [], "空数组合法：表示「未设置」"

    refused = api_client.put("/api/settings", json={"rename_extensions": ["mkv"]})
    assert refused.status_code == 400, refused.text
    assert "以点开头" in refused.json()["detail"], refused.text
    # 拆开之后**任何**一个 token 不合法都要整单拒绝，不能再留"静默失效"的输入。
    for bad in ("/vol1/1000/x.mkv", ".m/kv", ".mk v"):
        resp = api_client.put("/api/settings", json={"rename_extensions": [bad]})
        assert resp.status_code == 400, (bad, resp.text)
        assert "以点开头" in resp.json()["detail"], (bad, resp.text)
    wrong_type = api_client.put("/api/settings", json={"rename_extensions": "mkv"})
    assert wrong_type.status_code == 400, wrong_type.text
    # 400 不许破坏原值。
    assert api_client.get("/api/settings").json()["rename_extensions"] == []


# --------------------------------------------------------------------------- 引擎三层


def test_engine_falls_back_to_the_processor_list_when_the_global_one_is_unset(
    proc_defaults, proc_engine, tmp_path, db_mod
):
    db_mod.set_setting("rename_extensions", "[]")
    root = tmp_path / "engine"
    _tree(root)

    _outcome, stdout = P.call_engine(
        proc_engine, _spec(proc_defaults, [".mkv"]), root, log_file=tmp_path / "engine.log"
    )

    assert P.renamed_lines(stdout) == {"a.mkv -> Show A.mkv"}, stdout
    assert (root / "Show A/Season 1/b.ts").is_file(), "不在白名单里的后缀一个字节都不该动"


def test_engine_uses_the_global_whitelist_for_every_library(
    proc_defaults, proc_engine, tmp_path, db_mod
):
    """同一个处理器，全局白名单一改，**所有**媒体库的重命名范围跟着改。"""
    db_mod.set_setting("rename_extensions", json.dumps([".ts"]))
    root = tmp_path / "engine"
    _tree(root)

    _outcome, stdout = P.call_engine(
        proc_engine, _spec(proc_defaults, [".mkv"]), root, log_file=tmp_path / "engine.log"
    )

    assert P.renamed_lines(stdout) == {"b.ts -> Show A.ts"}, stdout
    assert (root / "Show A/Season 1/a.mkv").is_file()


def test_the_library_override_wins_over_the_global_whitelist(
    proc_defaults, proc_engine, tmp_path, db_mod
):
    """库级覆盖（库级命名覆盖 / 库级目录规则的落点）优先于全局 —— “总有一处不听话的库”仍可单独设置。"""
    db_mod.set_setting("rename_extensions", json.dumps([".ts"]))
    root = tmp_path / "engine"
    _tree(root)

    _outcome, stdout = P.call_engine(
        proc_engine,
        _spec(proc_defaults, [".mkv"]),
        root,
        log_file=tmp_path / "engine.log",
        override={"extensions": [".mkv"]},
    )

    assert P.renamed_lines(stdout) == {"a.mkv -> Show A.mkv"}, stdout
    assert (root / "Show A/Season 1/b.ts").is_file()
