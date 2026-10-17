"""``Scanner._refresh_qbittorrent()`` 的回归测试。

**背景（真实缺陷，不是假想）**：这段"顺带刷新 qB 状态"的代码曾写成
``client.test_connection()`` —— 而 ``QBClient`` 上**没有**这个方法（``test_connection``
只是 ``app.qbittorrent`` 的**模块级**函数）。``AttributeError`` 又被
``_refresh_qbittorrent`` 的宽 ``except`` 吞掉（只记 debug）⇒ **一旦填了 qB 地址，
仪表盘的 qB 状态灯与版本号就永远刷不出来**（一直是空）；而设置页的「测试连接」却是好的
（它走模块级 ``latency_of``），所以表面上看不出问题。``hasattr(QBClient,
"test_connection")`` 实测为 ``False``。

要点：刷新走**模块级** ``test_saved()``（它的 docstring 就是为这个周期刷新写的），
未配置地址时直接返回、不去打扰 qB。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def scanner(db_mod):
    """被测模块。模块缺失 ⇒ 用例 SKIP（绝不在模块级 importorskip）。"""
    return pytest.importorskip("app.scanner")


@pytest.fixture
def qb_mod(db_mod):
    return pytest.importorskip("app.qbittorrent")


def test_refresh_writes_status_and_version(scanner, qb_mod, db_mod, monkeypatch):
    db_mod.set_setting("qbittorrent_url", "http://192.168.1.100:18080")
    monkeypatch.setattr(qb_mod, "test_saved", lambda: (True, "5.0.4"))

    scanner.scanner._refresh_qbittorrent()

    assert db_mod.get_setting("qbittorrent_last_status") == "ok"
    assert db_mod.get_setting("qbittorrent_version") == "5.0.4"


def test_refresh_records_error_and_keeps_the_old_version(scanner, qb_mod, db_mod, monkeypatch):
    db_mod.set_setting("qbittorrent_url", "http://192.168.1.100:18080")
    db_mod.set_setting("qbittorrent_version", "5.0.4")

    monkeypatch.setattr(qb_mod, "test_saved", lambda: (False, "认证失败：用户名或密码错误。"))

    scanner.scanner._refresh_qbittorrent()

    assert db_mod.get_setting("qbittorrent_last_status") == "error"
    # 失败时不写版本（``version`` 是错误文案，不能被当成版本号存进去）
    assert db_mod.get_setting("qbittorrent_version") == "5.0.4"


def test_refresh_is_a_noop_without_a_configured_address(scanner, qb_mod, db_mod, monkeypatch):
    calls: list[int] = []

    def fake() -> tuple[bool, str]:
        calls.append(1)
        return True, "5.0.4"

    monkeypatch.setattr(qb_mod, "test_saved", fake)

    scanner.scanner._refresh_qbittorrent()

    assert calls == [], "未配置地址时不许去打扰 qB"
    assert db_mod.get_setting("qbittorrent_last_status") in (None, "")
