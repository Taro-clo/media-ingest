"""tests/ 公共夹具（同时兼容 pytest 与 tests/_minipytest.py 兜底运行器）。

两条硬性纪律在这里用夹具强制执行：

1. **绝不在真实宿主路径上落盘**：``_no_real_vol1_writes`` 把所有 ``/vol1``
   （以及 Windows 上的 ``C:/vol1``）的 ``os.makedirs`` 重定向进 tmp_path。
2. **app 缺失不得变成"空跑通过"**：所有 app 依赖一律用 ``pytest.importorskip``
   在**夹具层**跳过，测试文件本身不 import app，保证收集阶段不炸。
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

for _path in (str(REPO_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import mlm_helpers as H  # noqa: E402  (必须在 sys.path 注入之后)

TEST_USERNAME = "admin"
TEST_PASSWORD = "media-ingest-test-password-123"


# --------------------------------------------------------------------------- 内部工具


def _patch_setting(monkeypatch, settings, name: str, value) -> None:
    """改写 settings 属性；对冻结 dataclass 退化为 object.__setattr__。"""
    try:
        monkeypatch.setattr(settings, name, value, raising=False)
    except Exception:  # noqa: BLE001 - frozen dataclass / __slots__
        object.__setattr__(settings, name, value)


def _try_patch_setting(monkeypatch, settings, name: str, value) -> bool:
    """尽力改写 settings 属性；属性不存在或只读时静默跳过并返回 False。

    v2 新增了 ``config_dir`` / ``config_sync_interval_seconds``，但它们可能是
    只读 property（由 data_dir 派生），所以这里必须先探测再改。
    """
    if not hasattr(settings, name):
        return False
    try:
        _patch_setting(monkeypatch, settings, name, value)
    except Exception:  # noqa: BLE001 - 只读派生属性
        return False
    return True


def _reset_db_connection() -> None:
    """清掉 app.db 的线程本地连接，避免上一个测试的 DB 文件被复用。"""
    module = sys.modules.get("app.db")
    if module is None:
        return
    local = getattr(module, "_local", None)
    conn = getattr(local, "conn", None) if local is not None else None
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()
        with contextlib.suppress(Exception):
            local.conn = None


def _reset_loaded_modules(adapter_module) -> None:
    """丢掉适配层可能持有的跨用例状态。

    这里只剩"万一适配层还有 dict 式缓存就清掉"这条防御；
    保留函数名是为了让 ``adapter`` 夹具不必跟着改。
    """
    loaded = getattr(adapter_module, "_loaded", None)
    if isinstance(loaded, dict):
        loaded.clear()


# --------------------------------------------------------------------------- autouse


@pytest.fixture(autouse=True)
def _no_bytecode(monkeypatch):
    """禁止写 __pycache__，让"源目录是否被改动"的断言保持干净。"""
    monkeypatch.setattr(sys, "dont_write_bytecode", True, raising=False)


@pytest.fixture(autouse=True)
def _isolate_root_logger():
    """把 root logger 的 handler 集合恢复成进入本用例之前的样子。

    ``script_adapter.ensure_file_logger`` 会**故意**把 FileHandler 挂到 root 上
    （沿用 basicConfig 的语义），且明确不移除别人的 handler。
    root logger 是进程级单例，于是每个用例都会多挂一个指向**那个用例 tmp 目录**的
    handler；用例结束后目录被删，句柄却还开着。这里统一摘除并 close，
    既不泄漏文件描述符，也保证"日志行数"断言不会看到别的用例的 handler。
    """
    root = logging.getLogger()
    before = list(root.handlers)
    level_before = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.setLevel(level_before)


@pytest.fixture(autouse=True)
def _no_real_vol1_writes(tmp_path, monkeypatch):
    """兜底：任何代码对 ``/vol1`` 的 ``os.makedirs`` 都重定向进 tmp_path。

    注意 Linux 容器里 ``/vol1`` 有真实挂载，Windows 开发机上会变成 ``C:\\vol1\\...``：
    测试绝不允许在真实宿主路径上落盘。
    """
    real_makedirs = os.makedirs
    redirect_root = tmp_path / "_host_paths"
    redirected: list[tuple[str, str]] = []

    def fake_makedirs(name, *args, **kwargs):
        text = str(name).replace("\\", "/")
        if text.startswith("/vol1") or text.startswith("C:/vol1") or text.startswith("c:/vol1"):
            target = redirect_root / text.lstrip("/").replace(":", "")
            redirected.append((text, str(target)))
            return real_makedirs(target, *args, **kwargs)
        return real_makedirs(name, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", fake_makedirs)
    yield redirected



# --------------------------------------------------------------------------- 路径类夹具


@pytest.fixture
def data_dir(tmp_path):
    directory = tmp_path / "data"
    (directory / "logs").mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def log_dir(data_dir):
    """应用数据卷里的日志目录（= settings.log_dir）。"""
    return data_dir / "logs"


@pytest.fixture
def downloads_root(tmp_path):
    directory = tmp_path / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def source_dir(downloads_root):
    directory = downloads_root / "Bangumi"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def library_root(tmp_path):
    directory = tmp_path / "library"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def target_dir(library_root):
    directory = library_root / "Bangumi"
    directory.mkdir(parents=True, exist_ok=True)
    return directory



# --------------------------------------------------------------------------- 辅助夹具


@pytest.fixture
def build_tree():
    return H.build_tree


@pytest.fixture
def source_guard():
    return H.source_guard


@pytest.fixture
def hardlink_assert():
    return H.assert_real_hardlink


# --------------------------------------------------------------------------- 被测模块夹具


@pytest.fixture
def settings_env(monkeypatch, data_dir):
    """把 app.config.settings 指向 tmp，重置 db 线程本地连接，并**建好表结构**。

    ``init_db()`` 和真实启动路径（``app.main`` 的 lifespan）一致；不建表就会在使用
    ``registry.ensure_builtins()`` / ``configstore`` / 设置类接口时踩到
    ``sqlite3.OperationalError: no such table: …``。
    """
    config = pytest.importorskip("app.config")
    settings = config.settings
    _patch_setting(monkeypatch, settings, "data_dir", str(data_dir))
    _patch_setting(monkeypatch, settings, "db_path", str(data_dir / "mlm.db"))
    _patch_setting(monkeypatch, settings, "host_path_map", {})
    # v2：配置目录与同步间隔（v1 的 Settings 没有这两个属性，存在才改）
    _try_patch_setting(monkeypatch, settings, "config_dir", str(data_dir / "config"))
    _try_patch_setting(monkeypatch, settings, "config_sync_interval_seconds", 1)
    _reset_db_connection()
    db = pytest.importorskip("app.db")
    db.init_db()
    yield settings
    _reset_db_connection()


@pytest.fixture
def adapter(settings_env):
    module = pytest.importorskip("app.script_adapter")
    _reset_loaded_modules(module)
    yield module
    _reset_loaded_modules(module)


@pytest.fixture
def paths_mod(settings_env):
    return pytest.importorskip("app.paths")


@pytest.fixture
def db_mod(settings_env):
    module = pytest.importorskip("app.db")
    module.init_db()
    return module


@pytest.fixture
def queue_mod(settings_env, db_mod):
    return pytest.importorskip("app.queue")


@pytest.fixture
def executor_mod(settings_env, db_mod):
    return pytest.importorskip("app.executor")


# ----------------------------------------------------------------- v2 处理器 / 配置夹具
#
# 纪律：**绝不在模块级 importorskip**。_minipytest 会把模块级 Skipped 记成
# collect-error 并让整轮返回 1；只有夹具里抛 Skipped 才是"用例 SKIP"。
# 所以 v2 模块一律经下面这些夹具解析，模块缺失 ⇒ 用例 SKIP（而不是收集失败）。


def _import_first(*names: str):
    """Import the first importable module; skip the case when none exists."""
    last_error: Exception | None = None
    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError as exc:  # 只在真的缺模块时跳过，模块内报错要继续暴露
            last_error = exc
    pytest.skip(f"v2 模块尚未落盘：{' / '.join(names)}（{last_error}）")


def _v2_module_fixture(fixture_name: str, *module_names: str, needs_settings: bool = True):
    """构造一个"解析 v2 模块"的夹具函数（显式注册，便于报错定位）。

    ``needs_settings=False`` 用于**纯规则模块**（spec / defaults / engine / logs）：
    它们不读 settings，夹具因此不依赖 ``settings_env``，也就不会被无关模块（例如某个时刻
    正在编辑中的 ``app.db``）拖成整片失败。
    """
    if needs_settings:

        def _fixture(settings_env):  # noqa: ARG001 - 依赖 settings_env 指向 tmp
            return _import_first(*module_names)

    else:

        def _fixture():
            return _import_first(*module_names)

    _fixture.__name__ = fixture_name
    _fixture.__doc__ = f"v2 模块夹具：{' / '.join(module_names)}"
    return _fixture


proc_spec = pytest.fixture(_v2_module_fixture("proc_spec", "app.processors.spec", needs_settings=False))
proc_defaults = pytest.fixture(_v2_module_fixture("proc_defaults", "app.processors.defaults", needs_settings=False))
proc_engine = pytest.fixture(_v2_module_fixture("proc_engine", "app.processors.engine", needs_settings=False))
proc_logs = pytest.fixture(_v2_module_fixture("proc_logs", "app.processors.logs", needs_settings=False))
proc_registry = pytest.fixture(_v2_module_fixture("proc_registry", "app.processors.registry"))
configstore = pytest.fixture(
    _v2_module_fixture("configstore", "app.configstore", "app.config_store")
)


@pytest.fixture
def config_dir(settings_env):
    """settings.config_dir 指向的目录（已建好），v2 配置镜像写在这里。"""
    directory = Path(str(getattr(settings_env, "config_dir", settings_env.data_dir + "/config")))
    directory.mkdir(parents=True, exist_ok=True)
    return directory



# --------------------------------------------------------------------------- API 夹具


@pytest.fixture
def api_client(settings_env, downloads_root, library_root):
    """FastAPI TestClient（进入 with 以触发 lifespan → 队列线程）。

    **宿主路径配置**：白名单就是 tmp 里的真实目录、``host_path_map`` 为空（``settings_env``
    的默认）。这样库目录指向开发机上真实存在的目录，API 级 e2e **真能跑**（真的建出硬链接、
    任务真的 success）。容器路径语义（``/downloads`` + ``HOST_PATH_MAP``）见
    ``api_client_mapped``。
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    db = pytest.importorskip("app.db")
    app_object, where = H.discover_asgi_app()
    if app_object is None:
        pytest.skip(
            "未找到 ASGI 应用（候选 app.main:app / app.api:app / app.server:app / app.web:app）"
        )
    db.init_db()
    db.set_setting("allowed_source_roots", json.dumps([str(downloads_root)]))
    db.set_setting("allowed_target_roots", json.dumps([str(library_root)]))
    with TestClient(app_object) as client:
        client.app_where = where  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def api_client_mapped(settings_env, downloads_root, library_root, monkeypatch):
    """**容器路径配置**的 TestClient：白名单 ``['/downloads']``/``['/library']``，
    ``host_path_map`` 把这两个容器前缀指到 tmp 目录。

    用来验证应用的规范形态：宿主路径入库会被 ``paths.to_container`` 归一成容器路径、
    白名单按容器根校验、越界报 400。**不在这里做真跑**——开发机上 ``/downloads`` 不是
    真实目录（容器里才是），executor 直接用库里存的容器路径访问文件系统。
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    db = pytest.importorskip("app.db")
    app_object, where = H.discover_asgi_app()
    if app_object is None:
        pytest.skip("未找到 ASGI 应用")
    settings = pytest.importorskip("app.config").settings
    _patch_setting(
        monkeypatch,
        settings,
        "host_path_map",
        {
            H.CONTAINER_SOURCE_ROOT: str(downloads_root),
            H.CONTAINER_LIBRARY_ROOT: str(library_root),
        },
    )
    db.init_db()
    db.set_setting("allowed_source_roots", json.dumps([H.CONTAINER_SOURCE_ROOT]))
    db.set_setting("allowed_target_roots", json.dumps([H.CONTAINER_LIBRARY_ROOT]))
    with TestClient(app_object) as client:
        client.app_where = where  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def api_login():
    """返回 ``api_setup_and_login`` 助手（测试文件不必 ``import conftest``）。"""
    return api_setup_and_login


@pytest.fixture
def api_credentials():
    """``(用户名, 口令)``：测试需要"错口令"时用它做反例。"""
    return TEST_USERNAME, TEST_PASSWORD


def api_setup_and_login(client, username: str = TEST_USERNAME,
                        password: str = TEST_PASSWORD):
    """幂等：/me → (可选 setup) → login。返回认证后的 client。"""
    me = client.get("/api/auth/me")
    if me.status_code == 200 and me.json().get("needs_setup"):
        response = client.post(
            "/api/auth/setup", json={"username": username, "password": password}
        )
        assert response.status_code in (201, 409), response.text
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, f"登录失败: {response.status_code} {response.text}"
    return client
