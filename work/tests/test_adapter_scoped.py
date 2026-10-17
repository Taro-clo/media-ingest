"""A7 定向重跑机制：``ScopedOs`` / ``_scoped_os`` 的行为测试。

为什么这个文件必须存在：适配层"定向重跑单个一级文件夹"的能力**不是**靠改实现源码，
而是把 ``mod.os`` 临时换成 ``ScopedOs``，让实现的 ``os.listdir(ROOT_DIR)`` 只看得到
被允许的那一个条目。这里有一个已经被实测撞到过的缺陷类型：

    ``_scoped_os(module, source_dir, [folder] if folder else [])``
    全量运行（``folder is None``）时 ``allowed == []`` 而 ``scoped_path`` 非空。
    如果实现按 ``scoped_path`` 收窄，``listdir(source_dir)`` 会返回 ``[]``：
    实现一个一级文件夹都遍历不到，任务**静默**变成空操作
    （``created=[]``、不写日志、不建目标树、``exception=None``，executor 还会判 success）。

所以本文件的每条断言都是防这类回归的。
"""

from __future__ import annotations

import os

import pytest

import mlm_helpers as H

TREE = {"Show A/Season 1/ep01.mkv": 10, "Show B/ep02.mkv": 20}


@pytest.fixture
def hardlink_module(adapter):
    return adapter.runtime_module()


# --------------------------------------------------------------------------- 单元


def test_scoped_os_passes_everything_through_without_a_scope(source_dir, build_tree, adapter):
    build_tree(source_dir, TREE)
    proxy = adapter.ScopedOs(os, None, [])
    assert sorted(proxy.listdir(str(source_dir))) == ["Show A", "Show B"]
    assert sorted(entry.name for entry in proxy.scandir(str(source_dir))) == ["Show A", "Show B"]
    # 非 listdir/scandir 的一切都透传
    assert proxy.path is os.path
    assert proxy.sep == os.sep
    assert proxy.link is os.link
    assert proxy.makedirs is os.makedirs
    assert proxy.walk is os.walk


def test_scoped_os_with_an_empty_allowed_list_is_a_full_run(source_dir, build_tree, adapter):
    """**核心防回归**：``allowed=[]`` + 非空 ``scoped_path`` 必须是全量，不能是全空。"""
    build_tree(source_dir, TREE)
    proxy = adapter.ScopedOs(os, str(source_dir), [])
    assert sorted(proxy.listdir(str(source_dir))) == ["Show A", "Show B"]
    assert len(list(proxy.scandir(str(source_dir)))) == len(os.listdir(str(source_dir)))


def test_scoped_os_filters_only_the_scoped_directory(
    source_dir, downloads_root, build_tree, adapter
):
    build_tree(source_dir, TREE)
    proxy = adapter.ScopedOs(os, str(source_dir), ["Show A"])
    assert proxy.listdir(str(source_dir)) == ["Show A"]
    # 别的目录一概不过滤
    assert sorted(proxy.listdir(str(downloads_root))) == ["Bangumi"]
    # 被允许的目录内部也必须完整可见，否则实现的 os.walk 会漏文件
    (source_dir / "Show A" / "nested").mkdir(parents=True, exist_ok=True)
    assert sorted(proxy.listdir(str(source_dir / "Show A"))) == ["Season 1", "nested"]


def test_scoped_path_comparison_is_normalized(source_dir, build_tree, adapter):
    """尾部分隔符 / 反斜杠写法都必须等价命中（调用方传进来的路径形态不可控）。"""
    build_tree(source_dir, TREE)
    for variant in (str(source_dir) + os.sep, str(source_dir) + "/", str(source_dir) + "\\"):
        proxy = adapter.ScopedOs(os, variant, ["Show A"])
        assert proxy.listdir(str(source_dir)) == ["Show A"], f"未命中：{variant!r}"


def test_scoped_os_refuses_magic_attributes(adapter):
    """``__getattr__`` 只透传普通属性，否则 copy/pickle 协议会被代理悄悄破坏。

    注意 ``__reduce__`` 之类 ``object`` 自带的魔术方法走正常属性查找，
    ``__getattr__`` 根本不参与——所以这里只挑 ``object`` 上确实没有的名字。
    """
    proxy = adapter.ScopedOs(os, None, [])
    for name in ("__deepcopy__", "__wrapped__", "__mlm_not_a_real_dunder__"):
        with pytest.raises(AttributeError):
            getattr(proxy, name)


# --------------------------------------------------------------------------- 上下文管理器


def test_context_manager_swaps_and_restores_the_module_os(
    hardlink_module, source_dir, build_tree, adapter
):
    build_tree(source_dir, TREE)
    before = hardlink_module.os
    with adapter._scoped_os(hardlink_module, str(source_dir), ["Show A"]):
        assert isinstance(hardlink_module.os, adapter.ScopedOs)
        assert hardlink_module.os is not before
        assert hardlink_module.os.listdir(str(source_dir)) == ["Show A"]
    assert hardlink_module.os is before, "退出 with 必须把 mod.os 恢复成真正的 os"


def test_context_manager_restores_os_even_when_the_body_raises(hardlink_module, source_dir, adapter):
    before = hardlink_module.os
    with pytest.raises(RuntimeError):
        with adapter._scoped_os(hardlink_module, str(source_dir), ["Show A"]):
            raise RuntimeError("实现炸了")
    assert hardlink_module.os is before


def test_empty_allowed_list_form_used_by_the_call_site_is_a_full_run(
    hardlink_module, source_dir, build_tree, adapter
):
    """逐字复刻 ``run_hardlinks`` 的 ``with`` 写法，钉住"实现能看到全部一级文件夹"。"""
    build_tree(source_dir, TREE)
    with adapter._scoped_os(hardlink_module, str(source_dir), []):
        seen = sorted(hardlink_module.os.listdir(str(source_dir)))
    assert seen == ["Show A", "Show B"]


def test_nested_walk_still_works_under_scoping(hardlink_module, source_dir, build_tree, adapter):
    """收窄只作用于 ``scoped_path`` 那一层，``os.walk`` 必须还能钻进子目录。

    实现事实（有意如此，不是缺陷）：``os.walk`` 是真实 ``os`` 模块的函数，内部用的是
    **模块自己的** ``scandir``，因此它绕过 ``ScopedOs``。这不影响正确性——实现只有在
    "一级文件夹已经从 ``os.listdir`` 收窄后"才会 ``os.walk`` 进去，而真正决定
    "处理哪些一级文件夹"的那次枚举走的是被代理的 ``listdir``。
    """
    build_tree(source_dir, TREE)
    with adapter._scoped_os(hardlink_module, str(source_dir), ["Show A"]):
        assert hardlink_module.os.listdir(str(source_dir)) == ["Show A"]
        nested = sorted(
            os.path.join(dirpath, name)
            for dirpath, _dirs, files in hardlink_module.os.walk(str(source_dir / "Show A"))
            for name in files
        )
    assert nested == [str(source_dir / "Show A" / "Season 1" / "ep01.mkv")]


# --------------------------------------------------------------------------- 集成


def test_full_run_still_sees_every_folder(adapter, build_tree, source_dir, target_dir, log_dir):
    """全量 ``run_hardlinks`` 不得静默空操作 —— 这是 ScopedOs 缺陷的端到端回归守卫。"""
    build_tree(source_dir, TREE)
    outcome = adapter.run_hardlinks(
        source_dir=str(source_dir),
        target_dir=str(target_dir),
        log_file=str(log_dir / "hard_links.log"),
    )
    assert outcome.exception is None, outcome.exception
    assert len(outcome.created) == 2, f"全量运行必须真的建链：{outcome.stdout!r}"
    assert sorted(outcome.logged) == [("Show A", 10), ("Show B", 20)]
    assert sorted(H.tree_relpaths(target_dir)) == sorted(H.tree_relpaths(source_dir))
