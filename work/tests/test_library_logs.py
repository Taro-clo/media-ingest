"""媒体库重命名日志（``json_cache``）的契约测试。

契约真源：``docs/contracts/PROCESSORS.md`` §5 / §5.2。覆盖对象：``app/processors/logs.py``。

要点：日志位置必须在 ``settings.log_dir`` 下、名字只能是文件名、形态必须是
``ensure_ascii=False, indent=4`` 的 ``json.dump`` 形态、写入必须是原子的
（不留半份 JSON、不留 ``*.tmp`` 残骸）。
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_module
from pathlib import Path

import pytest

import mlm_helpers as H

LOCAL_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _sample() -> dict:
    return {
        "番剧 A": {"size": 1710, "timestamp": "2026-10-13 00:33:58", "works_dir": "/downloads/Bangumi"},
        "Show B": {"size": 200, "timestamp": "2026-10-13 00:31:02", "works_dir": "/downloads/Bangumi"},
    }


def test_json_indent_matches_the_original_script(proc_logs):
    assert proc_logs.JSON_INDENT == 4


def test_written_bytes_are_byte_identical_to_the_original_dump_parameters(proc_logs, tmp_path):
    """沿用 `json.dump(..., ensure_ascii=False, indent=4)` 的形态，逐字节一致。"""
    target = tmp_path / "bangumicache.log"
    payload = _sample()

    proc_logs.write_json_cache(target, payload)

    expected = json.dumps(payload, ensure_ascii=False, indent=4)
    assert H.read_text(target) == expected
    assert "\\u" not in H.read_text(target), "中文必须原样出现，不能是 \\uXXXX 转义"
    assert H.read_text(target) == json.dumps(payload, ensure_ascii=False, indent=4)


def test_write_is_atomic_and_leaves_no_tmp_debris(proc_logs, tmp_path, monkeypatch):
    target = tmp_path / "moviecache.log"
    proc_logs.write_json_cache(target, {"Movie One": {"size": 1, "timestamp": "2026-01-01 00:00:00"}})
    before = H.read_bytes(target)

    def boom(src, dst, *args, **kwargs):  # pragma: no cover - 只在被替换时执行
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        proc_logs.write_json_cache(target, {"Movie Two": {"size": 2, "timestamp": "2026-01-01 00:00:01"}})

    assert H.read_bytes(target) == before, "失败的写入不得改动原文件（原子写）"
    leftovers = [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]
    assert leftovers == [], f"失败后必须清掉临时文件，实际残留 {leftovers}"


def test_read_json_cache_degrades_to_empty_mapping(proc_logs, tmp_path):
    missing = tmp_path / "missing.log"
    assert proc_logs.read_json_cache(missing) == {}

    broken = tmp_path / "broken.log"
    H.write_json(broken, {})  # 先确保父目录存在
    broken.write_text("{ not json", encoding="utf-8")
    assert proc_logs.read_json_cache(broken) == {}

    not_a_dict = tmp_path / "list.log"
    not_a_dict.write_text("[1, 2, 3]", encoding="utf-8")
    assert proc_logs.read_json_cache(not_a_dict) == {}

    invalid_utf8 = tmp_path / "latin.log"
    invalid_utf8.write_bytes(b"\xff\xfe\x00bad")
    assert proc_logs.read_json_cache(invalid_utf8) == {}


def test_read_json_cache_returns_entries_with_unknown_keys_intact(proc_logs, tmp_path):
    target = tmp_path / "legacy.log"
    # 旧日志没有 works_dir，读的时候要原样保留、不能补键
    target.write_text(json.dumps({"Show A": {"size": 3, "timestamp": "2026-01-01 00:00:00"}}), encoding="utf-8")
    data = proc_logs.read_json_cache(target)
    assert data == {"Show A": {"size": 3, "timestamp": "2026-01-01 00:00:00"}}


def test_ensure_json_cache_creates_only_when_missing(proc_logs, tmp_path):
    target = tmp_path / "data" / "logs" / "bangumicache.log"

    assert proc_logs.ensure_json_cache(target) is True
    assert target.is_file()
    assert json.loads(H.read_text(target)) == {}
    assert proc_logs.ensure_json_cache(target) is False


def test_remove_entry_only_reports_true_when_it_removed_something(proc_logs, tmp_path):
    target = tmp_path / "bangumicache.log"
    proc_logs.write_json_cache(target, _sample())
    before = H.read_bytes(target)

    assert proc_logs.remove_entry(target, "nope") is False
    assert H.read_bytes(target) == before

    assert proc_logs.remove_entry(target, "番剧 A") is True
    assert list(proc_logs.read_json_cache(target)) == ["Show B"]


def test_slugify_folds_non_ascii_and_falls_back(proc_logs):
    assert proc_logs.slugify("Bangumi") == "bangumi"
    assert proc_logs.slugify("Movie  Library") == "movie-library"
    assert proc_logs.slugify("  Bangumi!! ") == "bangumi"
    assert proc_logs.slugify("") == "library"
    assert proc_logs.slugify("番剧") == "library"
    assert proc_logs.slugify("番剧 A") == "a"


def test_unique_log_file_avoids_existing_and_taken_names(proc_logs, tmp_path):
    assert proc_logs.unique_log_file(tmp_path, "Bangumi") == "bangumicache.log"

    (tmp_path / "bangumicache.log").write_text("{}", encoding="utf-8")
    assert proc_logs.unique_log_file(tmp_path, "Bangumi") == "bangumicache-2.log"

    (tmp_path / "bangumicache-2.log").write_text("{}", encoding="utf-8")
    assert proc_logs.unique_log_file(tmp_path, "Bangumi") == "bangumicache-3.log"

    assert proc_logs.unique_log_file(tmp_path, "Movie", taken=["moviecache.log"]) == "moviecache-2.log"


def test_log_path_joins_without_creating_anything(proc_logs, tmp_path):
    target = proc_logs.log_path(tmp_path, "bangumicache.log")
    assert Path(target) == tmp_path / "bangumicache.log"
    assert not Path(target).exists(), "log_path 只做拼接，不得创建文件"


def test_library_log_path_ignores_any_directory_component(proc_logs, settings_env):
    """``log_file`` 只是文件名：即便塞进绝对路径，也只能落在 ``settings.log_dir`` 下。"""
    import app.config as config

    resolved = Path(proc_logs.library_log_path("/etc/passwd"))
    assert resolved == Path(config.settings.log_dir) / "passwd"
    assert resolved.parent == Path(config.settings.log_dir)

    nested = Path(proc_logs.library_log_path("sub/dir/bangumicache.log"))
    assert nested == Path(config.settings.log_dir) / "bangumicache.log"


def test_iter_entries_ignores_dirty_values(proc_logs):
    data = {"ok": {"size": 1}, "bad": "not a dict", "also_bad": [1, 2], "empty": {}}
    assert proc_logs.iter_entries(data) == [("ok", {"size": 1}), ("empty", {})]


def test_log_summary_reports_shape_for_missing_file(proc_logs, tmp_path):
    assert proc_logs.log_summary(tmp_path / "missing.log") == {
        "exists": False,
        "entries": 0,
        "size": 0,
        "last_write": None,
    }


def test_log_summary_counts_entries_and_picks_the_newest_stamp(proc_logs, tmp_path):
    target = tmp_path / "bangumicache.log"
    proc_logs.write_json_cache(target, _sample())

    summary = proc_logs.log_summary(target)

    assert summary["exists"] is True
    assert summary["entries"] == 2
    assert summary["size"] == target.stat().st_size
    assert summary["last_write"] == "2026-10-13 00:33:58"
    assert LOCAL_STAMP_RE.match(summary["last_write"])


def test_log_summary_falls_back_to_mtime_in_local_format(proc_logs, tmp_path):
    target = tmp_path / "bangumicache.log"
    # 条目没有 timestamp ⇒ 退回文件 mtime，但格式仍必须是本地时间戳
    target.write_text(json.dumps({"Show A": {"size": 1}}), encoding="utf-8")
    stamp = 1_700_000_000
    os.utime(target, (stamp, stamp))

    summary = proc_logs.log_summary(target)

    assert summary["entries"] == 1
    assert LOCAL_STAMP_RE.match(summary["last_write"]), summary["last_write"]
    from datetime import datetime

    assert summary["last_write"] == datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")


def test_log_summary_tolerates_a_directory_in_place_of_the_file(proc_logs, tmp_path):
    target = tmp_path / "logs_dir"
    target.mkdir()
    assert proc_logs.log_summary(target) == {"exists": False, "entries": 0, "size": 0, "last_write": None}


def test_written_file_permissions_are_not_odd(proc_logs, tmp_path):
    """原子写的临时文件在 Windows 上可能带只读位；os.replace 之后必须还能继续覆写。"""
    target = tmp_path / "moviecache.log"
    proc_logs.write_json_cache(target, {"Movie One": {"size": 1, "timestamp": "2026-01-01 00:00:00"}})
    proc_logs.write_json_cache(target, {"Movie One": {"size": 2, "timestamp": "2026-01-01 00:00:01"}})
    assert proc_logs.read_json_cache(target)["Movie One"]["size"] == 2

    if os.name != "nt":
        mode = stat_module.S_IMODE(target.stat().st_mode)
        assert mode & stat_module.S_IWUSR, f"写出的日志必须可写，实际 mode={oct(mode)}"
