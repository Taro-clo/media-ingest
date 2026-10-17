"""``app/paths.py``（ARCHITECTURE §6.4）——路径工具与文件系统校验。

这一层是 **A9（绝不复制回退）** 的落点，所以断言分两类：

* 纯函数语义：``normalize`` / ``is_inside`` / ``to_host`` / ``to_container`` /
  ``existing_ancestor`` —— 全是字符串运算，任何平台都能跑；
* 文件系统语义：``check_same_filesystem`` / ``check_same_mount`` /
  ``check_hardlink_support`` / ``check_cross_mounts`` / ``assert_allowed`` /
  ``probe_cross_filesystem`` / ``snapshot_source_tree`` / ``verify_source_tree`` ——
  用 ``tmp_path`` 造真实目录，跨设备用例找不到第二块设备时**跳过**而不是假装通过。

``probe_cross_filesystem`` 是只读实证探针口径下的**只读安全**实证探测：它拿源树里**已存在**的普通
文件建一次真硬链接，再删掉那个链接（v1 在源侧 ``mkstemp``、遇 ``EROFS`` 就自我降级成
``ok=True`` —— 那正是"预检说可以、真跑全失败"的假绿来源）。所以这里逐条钉住：源树逐字节与
``(体积, inode, mtime_ns)`` 不变、``nlink`` 回落、目标目录时间戳还原、不留 ``.mlm_probe_*``
残留；**拿不到样本或判不出 errno 时结论是 ``None``（无法实证），不是 ``True``**。
"""

from __future__ import annotations

import errno
import os
import string
from pathlib import Path

import pytest

import mlm_helpers as H

# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

CONTAINER_TO_HOST = {
    "/downloads": "/vol1/1000/Download/qbittorrent",
    "/library": "/vol1/1000/Library",
    "/library/Bangumi": "/vol2/Bangumi",
}


def _config():
    import app.config as config  # noqa: PLC0415 - 只有被测模块存在时才会走到

    return config


def _set_map(monkeypatch, mapping):
    monkeypatch.setattr(_config().settings, "host_path_map", dict(mapping))


def _paths_under(root):
    """只比「有哪些条目」，不比目录 mtime——探针在目录里建/删临时文件必然会动 mtime。"""
    return sorted(H.tree_relpaths(root))


def _assert_no_probe_residue(before, root):
    after = _paths_under(root)
    assert after == before, "探针只许创建/删除自己的 .mlm_probe_* 临时文件，不得留下任何残留"
    assert not [p for p in after if ".mlm_probe_" in p]


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #
def test_normalize_is_absolute_and_forward_slashed(paths_mod, tmp_path):
    result = paths_mod.normalize(str(tmp_path / "a" / "b"))
    assert "\\" not in result, "数据库里存容器路径，反斜杠必须被统一"
    assert result.endswith("/a/b")
    assert os.path.isabs(result.replace("/", os.sep))


def test_normalize_strips_trailing_slashes_but_keeps_the_root(paths_mod, tmp_path):
    assert paths_mod.normalize(str(tmp_path) + "/") == paths_mod.normalize(str(tmp_path))
    assert paths_mod.normalize(str(tmp_path) + "///") == paths_mod.normalize(str(tmp_path))
    # 根目录的尾斜杠是它的一部分，不能 rstrip 成空串。
    # 注意 os.path.abspath("/") 在 Windows 上是当前盘根（C:/），所以这里用平台无关的写法。
    root = paths_mod.normalize(os.path.abspath(os.sep))
    assert root and root != ""
    assert root == paths_mod.normalize(root + "/")
    if os.name != "nt":
        assert root == "/" and paths_mod.normalize("/") == "/"


def test_normalize_handles_none_and_expanduser(paths_mod):
    assert paths_mod.normalize(None) is None
    expanded = paths_mod.normalize("~")
    assert expanded == paths_mod.normalize(os.path.expanduser("~"))
    assert "~" not in expanded


def test_normalize_collapses_dot_segments(paths_mod, tmp_path):
    messy = str(tmp_path) + "/x/./y/../y"
    assert paths_mod.normalize(messy) == paths_mod.normalize(str(tmp_path / "x" / "y"))


def test_normalize_is_idempotent(paths_mod, tmp_path):
    once = paths_mod.normalize(str(tmp_path / "a") + "/")
    assert paths_mod.normalize(once) == once


# --------------------------------------------------------------------------- #
# is_inside
# --------------------------------------------------------------------------- #
def test_is_inside_is_true_for_the_path_itself(paths_mod):
    # 有意如此：白名单校验靠它放行「根目录本身」，check_cross_mounts 另外单独排除相等。
    assert paths_mod.is_inside("/library", "/library") is True


@pytest.mark.parametrize(
    "child,parent,expected",
    [
        ("/library/Bangumi/Show A", "/library", True),
        ("/library/Bangumi", "/library/Bangumi", True),
        ("/library2/Bangumi", "/library", False),
        ("/library/BangumiX", "/library/Bangumi", False),
        ("/library", "/library/Bangumi", False),
        ("/other", "/library", False),
        ("/downloads/Bangumi/", "/downloads", True),
    ],
)
def test_is_inside_compares_path_components_not_string_prefixes(
    paths_mod, child, parent, expected
):
    assert paths_mod.is_inside(child, parent) is expected


def test_is_inside_survives_windows_separators(paths_mod, tmp_path):
    child = str(tmp_path / "downloads" / "Bangumi").replace("/", "\\")
    parent = str(tmp_path / "downloads").replace("/", "\\")
    assert paths_mod.is_inside(child, parent) is True
    assert paths_mod.is_inside(str(tmp_path / "downloads2"), parent) is False


# --------------------------------------------------------------------------- #
# existing_ancestor
# --------------------------------------------------------------------------- #
def test_existing_ancestor_returns_a_missing_paths_deepest_existing_parent(
    paths_mod, tmp_path
):
    deep = tmp_path / "not-yet" / "deeper" / "deepest"
    assert H.same_path(paths_mod.existing_ancestor(str(deep)), tmp_path)


def test_existing_ancestor_returns_the_path_itself_when_it_exists(paths_mod, tmp_path):
    assert H.same_path(paths_mod.existing_ancestor(str(tmp_path)), tmp_path)


def test_existing_ancestor_stops_at_an_existing_file(paths_mod, tmp_path):
    """文件也算「已存在」——``st_dev`` 比较只要有个能 stat 的对象就够了。"""
    a_file = tmp_path / "media.mkv"
    a_file.write_bytes(b"x")
    assert H.same_path(paths_mod.existing_ancestor(str(a_file / "child")), a_file)


# --------------------------------------------------------------------------- #
# host_path_map 换算（只用于展示）
# --------------------------------------------------------------------------- #
def test_to_host_maps_and_uses_the_longest_prefix(settings_env, monkeypatch):
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, CONTAINER_TO_HOST)
    assert paths_mod.to_host("/library/Bangumi/Show A") == "/vol2/Bangumi/Show A", (
        "更长的键必须赢过 /library"
    )
    assert paths_mod.to_host("/library/Movie") == "/vol1/1000/Library/Movie"
    assert paths_mod.to_host("/downloads/Bangumi") == (
        "/vol1/1000/Download/qbittorrent/Bangumi"
    )


def test_to_host_accepts_a_relative_container_path(settings_env, monkeypatch):
    """回归守卫（§6.1 不变量）：少了前导斜杠也必须能映射，否则静默原样返回。"""
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, CONTAINER_TO_HOST)
    assert paths_mod.to_host("downloads/Bangumi") == (
        "/vol1/1000/Download/qbittorrent/Bangumi"
    )


def test_to_host_returns_input_unchanged_when_unmapped(settings_env, monkeypatch):
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, CONTAINER_TO_HOST)
    assert paths_mod.to_host("/somewhere/else") == "/somewhere/else"
    assert paths_mod.to_host("") == ""


def test_to_host_does_not_let_downloads_steal_downloads2(settings_env, monkeypatch):
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, CONTAINER_TO_HOST)
    assert paths_mod.to_host("/downloads2/Bangumi") == "/downloads2/Bangumi"


def test_to_container_reverses_the_map(settings_env, monkeypatch):
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, CONTAINER_TO_HOST)
    assert paths_mod.to_container("/vol1/1000/Download/qbittorrent/Bangumi") == (
        "/downloads/Bangumi"
    )
    assert paths_mod.to_container("/vol2/Bangumi/Show A") == "/library/Bangumi/Show A"
    assert paths_mod.to_container("/nowhere/at/all") == "/nowhere/at/all"


def test_to_container_leaves_an_already_container_path_alone(settings_env, monkeypatch):
    """qBittorrent 回调（``%F``/``%D``/``%R``）走的就是这条：同名挂载，零转换。"""
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, CONTAINER_TO_HOST)
    assert paths_mod.to_container("/downloads/Bangumi") == "/downloads/Bangumi"
    assert paths_mod.to_container("/library/Bangumi") == "/library/Bangumi"


def test_host_and_container_round_trip(settings_env, monkeypatch):
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, CONTAINER_TO_HOST)
    for container in ("/downloads/Bangumi/Show A", "/library/Movie/Film"):
        assert paths_mod.to_container(paths_mod.to_host(container)) == container


def test_to_host_tolerates_a_map_key_without_leading_slash(settings_env, monkeypatch):
    """有人把 HOST_PATH_MAP 写成 ``{"downloads": "/vol1/..."}`` 也要能用。"""
    paths_mod = pytest.importorskip("app.paths")
    host_root = "/vol1/1000/Download/qbittorrent"
    _set_map(monkeypatch, {"downloads": host_root})
    assert paths_mod.to_host("/downloads/Bangumi") == host_root + "/Bangumi"
    # 反向：规则 2 原样回显 map 的键（生产路径上 app.config 已把键归一化成绝对路径，
    # 只有绕过 config 直接 patch settings 的测试才看得到这个少斜杠的形态）。
    assert paths_mod.to_container(host_root + "/Bangumi") == "downloads/Bangumi"


def test_to_host_keeps_windows_drive_paths_usable(settings_env, monkeypatch):
    """本地开发时容器路径可能就是个 Windows 盘符路径，别被补成 ``/C:/...``。"""
    paths_mod = pytest.importorskip("app.paths")
    _set_map(monkeypatch, {})
    assert paths_mod.to_host("C:/temp/downloads") == "C:/temp/downloads"
    assert paths_mod.to_container("C:/temp/downloads") == "C:/temp/downloads"


# --------------------------------------------------------------------------- #
# check_same_filesystem（A9 的前置拦截）
# --------------------------------------------------------------------------- #
def test_same_filesystem_for_two_dirs_on_one_device(paths_mod, tmp_path):
    source = tmp_path / "downloads"
    target = tmp_path / "library" / "Bangumi"
    source.mkdir(parents=True)
    ok, error = paths_mod.check_same_filesystem(str(source), str(target))
    assert ok is True and error is None, "目标目录还不存在也要能判断（上溯到已存在祖先）"


def test_same_filesystem_rejects_a_cross_device_pair(paths_mod, tmp_path):
    other = H.find_other_device_dir(str(tmp_path))
    if not other:
        pytest.skip("单文件系统环境，无法构造跨设备用例")
    ok, error = paths_mod.check_same_filesystem(str(tmp_path), other)
    assert ok is False
    assert error, "必须给出可读的中文原因"
    for fragment in (
        "跨文件系统错误",
        "st_dev=",
        "EXDEV",
        "不会回退为复制文件",
        "同一宿主文件系统",
    ):
        assert fragment in error, f"文案是 ARCHITECTURE §2 冻结的全文，缺少 {fragment!r}"
    assert str(paths_mod.existing_ancestor(str(tmp_path))) in error
    assert str(paths_mod.existing_ancestor(other)) in error


def _nonexistent_root():
    """找一个「整个根都不存在」的路径：Windows 上是没挂载的盘符，POSIX 上没有。"""
    if os.name != "nt":
        return None
    for letter in string.ascii_uppercase:
        root = f"{letter}:/"
        if not os.path.exists(root):
            return root + "definitely-not-here"
    return None


def test_same_filesystem_reports_an_unstatable_path(paths_mod, tmp_path):
    missing = _nonexistent_root()
    if not missing:
        pytest.skip("找不到「连根都不存在」的路径，无法触发不可访问分支")
    ok, error = paths_mod.check_same_filesystem(str(tmp_path), missing)
    assert ok is False
    assert "路径不存在或不可访问" in error and "无法比较文件系统" in error


# --------------------------------------------------------------------------- #
# check_cross_mounts
# --------------------------------------------------------------------------- #
def test_cross_mounts_rejects_identical_dirs(paths_mod):
    ok, error = paths_mod.check_cross_mounts("/downloads/Bangumi", "/downloads/Bangumi/")
    assert ok is False and "不能相同" in error


def test_cross_mounts_rejects_either_containment(paths_mod):
    ok, error = paths_mod.check_cross_mounts("/library/Bangumi", "/library")
    assert ok is False and "位于目标目录" in error
    ok, error = paths_mod.check_cross_mounts("/library", "/library/Bangumi")
    assert ok is False and "位于源目录" in error


def test_cross_mounts_accepts_siblings_with_a_shared_prefix(paths_mod):
    ok, error = paths_mod.check_cross_mounts("/library/Bangumi", "/library/Bangumi2")
    assert ok is True, f"分量比较不得把 Bangumi2 当成 Bangumi 的子目录：{error}"
    ok, error = paths_mod.check_cross_mounts("/downloads/Bangumi", "/library/Bangumi")
    assert ok is True and error is None


# --------------------------------------------------------------------------- #
# assert_allowed
# --------------------------------------------------------------------------- #
def test_assert_allowed_accepts_a_path_under_any_root(paths_mod):
    roots = ["/downloads", "/library"]
    assert paths_mod.assert_allowed("/downloads/Bangumi/Show A", roots, "源目录") is None
    assert paths_mod.assert_allowed("/library", roots, "目标目录") is None


def test_assert_allowed_rejects_an_outside_path_with_a_helpful_message(paths_mod):
    with pytest.raises(ValueError) as info:
        paths_mod.assert_allowed("/etc/passwd", ["/downloads"], "源目录")
    message = str(info.value)
    assert "源目录" in message and "不在允许的根路径内" in message
    assert "/downloads" in message, "要把当前允许的根列出来，人才知道怎么改配置"


def test_assert_allowed_rejects_everything_when_no_root_is_configured(paths_mod):
    with pytest.raises(ValueError) as info:
        paths_mod.assert_allowed("/downloads/Bangumi", [], "目标目录")
    assert "（未配置）" in str(info.value)
    with pytest.raises(ValueError):
        paths_mod.assert_allowed("/downloads/Bangumi", None, "目标目录")


# --------------------------------------------------------------------------- #
# probe_cross_filesystem（实证探测，且绝不碰真实文件）
# --------------------------------------------------------------------------- #
def _source_fingerprint(root):
    """源树里每个普通文件的 ``(体积, inode, mtime_ns)`` —— 探针碰过就不该相等。"""
    return {
        rel: (
            os.stat(root / rel).st_size,
            os.stat(root / rel).st_ino,
            os.stat(root / rel).st_mtime_ns,
        )
        for rel in H.tree_relpaths(root)
    }


def test_probe_links_a_real_source_file_and_leaves_no_residue(paths_mod, tmp_path):
    """只读实证探针的主判据：拿**已存在**的源文件建一次真链接，再删掉它。"""
    source = tmp_path / "downloads" / "Bangumi"
    target = tmp_path / "library" / "Bangumi"
    H.build_tree(source, {"Show A/Season 1/ep01.mkv": 10, "Show B/Movie.mkv": 20})
    target.mkdir(parents=True)
    media = source / "Show A" / "Season 1" / "ep01.mkv"
    payload = H.read_bytes(media)
    nlink_before = os.stat(media).st_nlink
    before = _paths_under(tmp_path)
    target_mtime = os.stat(target).st_mtime_ns

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is True, f"同一设备上探针必须成功：{note}"
    assert "已实证" in note and "源目录未被写入" in note, note
    _assert_no_probe_residue(before, tmp_path)
    assert H.read_bytes(media) == payload, "探测不得改动任何真实媒体文件（B8/B9）"
    assert os.stat(media).st_nlink == nlink_before, "临时链接必须已删除，nlink 回落到原值"
    assert os.stat(target).st_mtime_ns == target_mtime, "目标目录的时间戳必须还原"


def test_probe_never_writes_into_the_source_tree(paths_mod, tmp_path):
    """源目录不写一个字节：v1 在源侧 ``mkstemp`` 的假绿路径已被删除（只读实证探针口径）。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    H.build_tree(source, {"Show A/Season 1/ep01.mkv": 10, "Show B/x.mkv": 20})
    target.mkdir()
    before_entries = sorted(H.tree_relpaths(source))
    before_files = _source_fingerprint(source)

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is True, note
    assert sorted(H.tree_relpaths(source)) == before_entries
    assert _source_fingerprint(source) == before_files
    assert not [p for p in before_entries if ".mlm_probe_" in p]


def test_probe_recurses_bounded_into_title_folders_to_find_a_sample(paths_mod, tmp_path):
    """源根第一层全是标题文件夹 —— 必须靠有界递归（深度 ≤3）找到样本。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    H.build_tree(source, {"Show A/Season 1/ep01.mkv": 10})
    target.mkdir()

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is True, f"第二层的普通文件足够取样：{note}"


def test_probe_refuses_to_claim_anything_when_no_sample_is_reachable(paths_mod, tmp_path):
    """空源目录（或样本深过上限）⇒ ``None``，**不是** ``True``。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    before = _paths_under(tmp_path)

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is None, f"没有样本就不能给结论：{note}"
    assert "无法实证" in note and "深度 3" in note and "条目 512" in note, note
    _assert_no_probe_residue(before, tmp_path)

    deep = source / "A" / "B" / "C" / "D"
    deep.mkdir(parents=True)
    (deep / "e01.mkv").write_bytes(b"x")
    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))
    assert ok is None, f"超出深度上限的文件不算样本：{note}"


def test_probe_skips_symlinks_and_its_own_leftovers(paths_mod, tmp_path):
    """不跟随符号链接（可能引到别的挂载），也不把自己的残留当样本。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    (source / ".mlm_probe_9_deadbeef.link").write_bytes(b"leftover")
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"real")
    try:
        (source / "link.mkv").symlink_to(outside)
    except (OSError, NotImplementedError):
        pass  # Windows 上建符号链接要额外权限，可能失败 —— 该分支不是本用例的重点

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is None, f"残留与符号链接都不是合法样本：{note}"

    (source / "real.mkv").write_bytes(b"sample")
    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))
    assert ok is True, note


def test_probe_returns_none_when_the_target_cannot_be_written(paths_mod, tmp_path, monkeypatch):
    """目标不可写 ⇒ ``None``（无法实证），不是失败、也不是通过。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    (source / "a.mkv").write_bytes(b"sample")
    real_access = os.access
    target_text = os.path.abspath(str(target))

    def deny_target(path, mode, **kwargs):
        if os.path.abspath(str(path)).startswith(target_text):
            return False
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(paths_mod.os, "access", deny_target)

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is None and "不可写" in note, note


def test_probe_returns_none_for_an_errno_it_cannot_interpret(paths_mod, tmp_path, monkeypatch):
    """拿到 EXDEV 之外的 errno 时无法与「跨挂载」区分 ⇒ 降级给挂载 ID 判据。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    (source / "a.mkv").write_bytes(b"sample")
    before = _paths_under(tmp_path)

    def boom(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(paths_mod.os, "link", boom)
    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is None, note
    assert "errno=" in note and "改用挂载 ID 预筛" in note, note
    _assert_no_probe_residue(before, tmp_path)


def test_probe_reports_exdev_as_a_reliable_false(paths_mod, tmp_path, monkeypatch):
    """``EXDEV`` 是可靠负判据：跨挂载就是建不出来，且绝不复制冒充（B8）。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    (source / "a.mkv").write_bytes(b"sample")
    before = _paths_under(tmp_path)

    def boom(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(paths_mod.os, "link", boom)
    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is False
    assert "EXDEV" in note and "不会回退为复制文件" in note, note
    _assert_no_probe_residue(before, tmp_path)


def test_probe_really_fails_across_devices(paths_mod, tmp_path):
    other = H.find_other_device_dir(str(tmp_path))
    if not other:
        pytest.skip("单文件系统环境，无法构造跨设备用例")
    source = tmp_path / "downloads"
    source.mkdir()
    # 探针必须在源树里**取样一个已存在的普通文件**才能实证：空源树只会得到 ``None``
    # （无法实证），不是 ``False``（跨设备实证失败）。本用例需要**第二块文件系统**：
    # 单文件系统环境会在上面 skip，而它一旦跳过，探针的跨设备失败路径就没人覆盖。
    (source / "a.mkv").write_bytes(b"sample")
    before = _paths_under(tmp_path)

    ok, note = paths_mod.probe_cross_filesystem(str(source), other)

    assert ok is False, f"跨设备真的建不出硬链接：{note}"
    assert "不会回退为复制文件" in note
    _assert_no_probe_residue(before, tmp_path)


def test_probe_rejects_a_target_that_is_not_a_directory(paths_mod, tmp_path):
    source = tmp_path / "downloads"
    source.mkdir()
    blocker = tmp_path / "library"
    blocker.write_bytes(b"i am a file, not a dir")

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(blocker))

    assert ok is False and "不是目录" in note


def test_probe_rejects_a_source_that_is_not_a_directory(paths_mod, tmp_path):
    blocker = tmp_path / "downloads"
    blocker.write_bytes(b"file")
    target = tmp_path / "library"
    target.mkdir()

    ok, note = paths_mod.probe_cross_filesystem(str(blocker), str(target))

    assert ok is False and "不是目录" in note


def test_cleanup_probe_leftovers_removes_only_its_own_files(paths_mod, tmp_path):
    """强杀留下的 ``.mlm_probe_*`` 要能清掉，别人的文件一个都不能碰。"""
    (tmp_path / ".mlm_probe_123_ab.link").write_bytes(b"x")
    (tmp_path / "keep.mkv").write_bytes(b"y")

    assert paths_mod.cleanup_probe_leftovers(str(tmp_path)) == 1
    assert not (tmp_path / ".mlm_probe_123_ab.link").exists()
    assert (tmp_path / "keep.mkv").exists()
    assert paths_mod.cleanup_probe_leftovers(str(tmp_path)) == 0
    assert paths_mod.cleanup_probe_leftovers(str(tmp_path / "missing")) == 0


def test_probe_sweeps_a_leftover_from_a_previous_kill(paths_mod, tmp_path):
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    (source / "a.mkv").write_bytes(b"sample")
    stale = target / ".mlm_probe_999_deadbeef.link"
    stale.write_bytes(b"stale")

    ok, note = paths_mod.probe_cross_filesystem(str(source), str(target))

    assert ok is True, note
    assert not stale.exists(), "探针前后都要清扫自己上一次被杀留下的残留"


# --------------------------------------------------------------------------- #
# check_same_mount / check_hardlink_support（只读实证口径的三态级联）
# --------------------------------------------------------------------------- #
_HAS_MOUNTINFO = os.path.exists("/proc/self/mountinfo")


def test_mount_id_for_returns_none_where_mountinfo_does_not_exist(paths_mod, tmp_path):
    found = paths_mod.mount_id_for(str(tmp_path))
    if _HAS_MOUNTINFO:
        assert found is not None and found[0].isdigit(), found
    else:
        assert found is None, "非 Linux 上必须老实返回 None，不能假装知道挂载身份"


def test_check_same_mount_admits_it_is_necessary_not_sufficient(paths_mod, tmp_path):
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()

    same, note = paths_mod.check_same_mount(str(source), str(target))

    if _HAS_MOUNTINFO:
        assert same is True, note
        assert "必要不充分" in note and "btrfs" in note, note
    else:
        assert same is None and "mountinfo" in note, note


def test_check_hardlink_support_lets_a_conclusive_probe_win(paths_mod, tmp_path, monkeypatch):
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()

    monkeypatch.setattr(
        paths_mod, "probe_cross_filesystem", lambda s, t: (False, "探针说不行")
    )
    ok, message = paths_mod.check_hardlink_support(str(source), str(target))
    assert ok is False and message == "探针说不行", message

    monkeypatch.setattr(
        paths_mod, "probe_cross_filesystem", lambda s, t: (True, "探针说行")
    )
    ok, message = paths_mod.check_hardlink_support(str(source), str(target))
    assert ok is True and message == "探针说行", message


def test_check_hardlink_support_marks_a_fallback_verdict_as_unproven(paths_mod, tmp_path):
    """探针跑不了、退到挂载 ID / st_dev 时，结论必须带「未实证」。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()

    ok, message = paths_mod.check_hardlink_support(str(source), str(target), probe=False)

    assert ok is True, message
    assert "未实证" in message, message
    if _HAS_MOUNTINFO:
        assert "必要不充分" in message, message
    else:
        assert "st_dev" in message and "可能假绿" in message, message


def test_check_hardlink_support_answers_none_when_nothing_is_provable(
    paths_mod, tmp_path, monkeypatch
):
    """全都判不了 ⇒ ``None``（调用方警告放行），绝不硬拦。"""
    source = tmp_path / "downloads"
    target = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    monkeypatch.setattr(paths_mod, "check_same_mount", lambda s, t: (None, "读不到 mountinfo"))
    monkeypatch.setattr(paths_mod, "_st_dev", lambda path: None)

    ok, message = paths_mod.check_hardlink_support(str(source), str(target), probe=False)

    assert ok is None and "无法判定" in message, message


def test_check_hardlink_support_rejects_a_source_that_is_not_a_directory(paths_mod, tmp_path):
    blocker = tmp_path / "downloads"
    blocker.write_bytes(b"file")
    target = tmp_path / "library"
    target.mkdir()

    ok, message = paths_mod.check_hardlink_support(str(blocker), str(target), probe=False)

    assert ok is False and "不是目录" in message, message


# --------------------------------------------------------------------------- #
# snapshot_source_tree / verify_source_tree（只读实证口径 §3 的可验证保证）
# --------------------------------------------------------------------------- #
def test_snapshot_and_verify_source_tree_report_added_removed_and_changed(paths_mod, tmp_path):
    root = tmp_path / "downloads"
    H.build_tree(root, {"a.mkv": 10, "sub/b.ass": 20})

    before = paths_mod.snapshot_source_tree(str(root))
    assert set(before) == {"a.mkv", "sub/b.ass"}
    assert paths_mod.verify_source_tree(str(root), before) == (True, [])

    (root / "c.mkv").write_bytes(b"x" * 5)
    (root / "a.mkv").write_bytes(b"y" * 11)
    (root / "sub" / "b.ass").unlink()

    ok, changed = paths_mod.verify_source_tree(str(root), before)

    assert ok is False and changed == ["a.mkv", "c.mkv", "sub/b.ass"], changed


def test_verify_source_tree_ignores_nlink_and_ctime_so_hardlinks_never_false_positive(
    paths_mod, tmp_path
):
    """建硬链接本身会让源文件 ``nlink`` +1、``ctime`` 变化 —— 那不算「源被改动」。"""
    root = tmp_path / "downloads"
    H.build_tree(root, {"a.mkv": 10})
    before = paths_mod.snapshot_source_tree(str(root))
    outside = tmp_path / "elsewhere.mkv"

    os.link(root / "a.mkv", outside)
    try:
        assert os.stat(root / "a.mkv").st_nlink >= 2
        assert paths_mod.verify_source_tree(str(root), before) == (True, [])
    finally:
        outside.unlink()


def test_verify_source_tree_never_modifies_anything(paths_mod, tmp_path):
    root = tmp_path / "downloads"
    H.build_tree(root, {"a.mkv": 10})
    outside = tmp_path / "elsewhere.mkv"
    os.link(root / "a.mkv", outside)
    before = _paths_under(tmp_path)
    try:
        paths_mod.verify_source_tree(str(root), {})
        assert _paths_under(tmp_path) == before
        assert (root / "a.mkv").exists() and outside.exists()
    finally:
        outside.unlink()


# --------------------------------------------------------------------------- #
# A9 的静态约束
# --------------------------------------------------------------------------- #
def test_paths_module_never_copies_files(paths_mod):
    text = Path(paths_mod.__file__).read_text(encoding="utf-8")
    assert "import shutil" not in text, "这一层要是能复制文件，A9 就无从谈起"
    for forbidden in ("shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree"):
        assert forbidden not in text
