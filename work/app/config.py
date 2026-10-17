"""配置（ARCHITECTURE §6.1）。

刻意用 stdlib ``os.environ`` 而不是 pydantic BaseSettings：
项目依赖被冻结为 ``fastapi`` + ``uvicorn[standard]``（ARCHITECTURE §1），
多引一个包就多一份部署面的构建风险，而这里的配置项少且全是字符串。
"""

from __future__ import annotations

import json
import os
import re
import secrets


def _env(name: str, default: str | None = None) -> str | None:
    """读取环境变量；空字符串按“未设置”处理，避免 compose 里写 ``VAR=`` 时把默认值顶掉。"""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


#: HOST_PATH_MAP 的默认值：**空映射（恒等）**。容器路径 -> 宿主路径，**仅用于 UI 展示与
#: 路径换算**。
#:
#: **为什么默认是空的**：规范形态是「共同父目录挂一条 bind、容器路径 = 宿主路径」，
#: 容器里**不再有** ``/downloads`` 与 ``/library``。而 ``paths.to_container`` 的规则 2 会拿这份
#: 映射的**值**做宿主前缀反查：用户在表单里填真实宿主路径
#: ``/vol1/1000/Download/qbittorrent/Bangumi`` 时，默认映射里的
#: ``/vol1/1000/Download/qbittorrent`` 把它**改写成不存在的** ``/downloads/Bangumi``，
#: 入库后源目录直接找不到。恒等（空映射）在这个形态下才是不变量：容器路径就是宿主路径。
#: 仍跑旧式布局（``/downloads``、``/library`` 两个 bind）的部署，用 ``HOST_PATH_MAP``
#: env 显式给这份映射即可 —— 它本来就是 env 驱动的。
#: ⚠️ **镜像层同样不得内置非空映射**：``docker/Dockerfile`` 里不得写死
#: ``HOST_PATH_MAP``（不显式设置的运行方式会继承它，然后上面这段注释描述的事故
#: 原样发生）。这一点有两处断言守着 ——
#: ``examples/validate-deployment-assets.sh`` §8 与
#: ``tests/test_rule_validation.py::test_b9_identity_path_map_cannot_be_overridden_by_the_image_or_compose``。
#:
#: **键必须写成带前导斜杠的绝对容器路径**（§6.1「host_path_map 的不变量」）：
#: 这个映射的键会被 ``paths.to_host()`` / ``paths.to_container()`` 直接当容器路径做前缀比较。
#: 少一个斜杠不会报错，但会让路径展示与 QL 回调的路径识别静默失准。
#: 它**只用于展示与路径换算**：没有任何代码把它的值写进日志文件、也没有代码从日志里
#: 反查它（PROCESSORS §5.3）。
DEFAULT_HOST_PATH_MAP: dict[str, str] = {}


#: 白名单根的**首次播种初值**（唯一真源）。
#:
#: 语义**只是**「新建数据卷时写进 ``allowed_source_roots``/``allowed_target_roots`` 的初值」：
#: - 已存在的 DB **一律不动**、迁移**不覆盖** —— 运行时真源永远是那两个 DB 设置项，仍可 PUT；
#: - 它们**不是**新建库表单的默认值，也不是运行时开关，别把它当 env 旋钮用。
#:
#: 为什么必须由 env 驱动（单挂载形态）：compose 用「共同父目录挂一条 bind、容器路径 =
#: 宿主路径」（MEDIA_ROOT）之后，容器里**不再有** ``/downloads`` 与 ``/library`` 这两个路径；
#: 而 ``/api/system/health`` 的 ``downloads_readable`` / ``library_writable`` 两项正是拿这两个
#: 设置项去探的（``app/routers/system.py``）——默认值若仍指向不存在的旧路径，容器一开箱就
#: unhealthy。devops 把这两个 env 名写进 ``.env.example``。
#: 缺省值取**整个下载树** ``/vol1/1000/Download``（而不是它下面的 ``qbittorrent`` 子目录）：
#: 单挂载形态下容器看到的就是宿主真实路径，把白名单放宽到整棵树，用户才能给
#: ``Download/<任意子目录>`` 建库（qBittorrent 换过保存目录、或手工放进去的剧集都不用再改配置）。
#: 想收紧就在 WebUI「设置」页把这一项改回具体子目录——运行时真源始终是 DB 设置项。
DEFAULT_SOURCE_ROOT: str = (
    _env("DEFAULT_SOURCE_ROOT", "/vol1/1000/Download")
    or "/vol1/1000/Download"
)
DEFAULT_TARGET_ROOT: str = (
    _env("DEFAULT_TARGET_ROOT", "/vol1/1000/Library") or "/vol1/1000/Library"
)


#: ``C:/Users/...``、``D:/media/...`` 这类带盘符的 Windows 路径。
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


def looks_like_windows_path(value: str) -> bool:
    """判断一个（已把反斜杠换成斜杠的）路径是否是 Windows 盘符路径。

    归一化的目的是把**容器**前缀统一成 ``/`` 开头的绝对 POSIX 路径；容器里所有路径
    都长这样，加斜杠是对的。但在 Windows 上开发时，集成测试的 ``HOST_PATH_MAP`` 里必然
    出现带盘符的真实路径（``C:/Users/…/downloads``）；给它们前面再补一根斜杠会得到
    ``/C:/Users/…`` 这种谁也匹配不上的怪物——依赖这份映射的路径展示与日志匹配会因此全部落空（表现为
    「日志连续性缺失、全站假告警、回调匹配不上」）。盘符路径在容器里永远不会出现，
    保留原样只是为了让本地端到端测试有意义。
    """
    return bool(_WINDOWS_DRIVE_RE.match(value))


def _normalize_keys(mapping: dict) -> dict[str, str]:
    """把映射的**键**统一成 ``/`` 开头的绝对容器路径（值原样保留）。

    这是 ``host_path_map`` 不变量的唯一入口：任何来源（env、默认值）都必须过这里，
    否则「默认值这一支带不带斜杠」就会变成一条只在没设环境变量时才炸的隐藏分支。
    空键直接丢弃——一个空的容器前缀没有任何意义，留着只会在前缀比较里全匹配。
    盘符路径见 :func:`looks_like_windows_path`，不加前导斜杠。
    """
    result: dict[str, str] = {}
    for key, value in mapping.items():
        normalized = (str(key) if key is not None else "").replace("\\", "/").strip()
        if not normalized:
            continue
        if looks_like_windows_path(normalized):
            result[normalized] = str(value)
        else:
            result["/" + normalized.lstrip("/")] = str(value)
    return result


#: 归一化后的默认映射（模块级算一次，避免在回退分支里递归调用自身）。
_DEFAULT_HOST_PATH_MAP_NORMALIZED: dict[str, str] = _normalize_keys(DEFAULT_HOST_PATH_MAP)


def _load_host_path_map(raw: str | None) -> dict[str, str]:
    """解析 ``HOST_PATH_MAP``（JSON：容器路径 -> 宿主路径）。解析失败退回默认映射。

    宽容处理键的写法（``downloads`` / ``/downloads`` 都接受），因为 compose 里手写 JSON
    很容易少写前导斜杠，而这里失败会让整个 UI 的路径展示变错但又不会报错。

    **四条回退分支（env 为空 / JSON 非法 / 非 dict / 解析结果为空）一律走
    ``_normalize_keys()``**：四条分支都必须补前导斜杠，不能只补齐其中一支。
    默认值是**空映射**（单挂载形态下容器路径 = 宿主路径，见 ``DEFAULT_HOST_PATH_MAP``
    的说明），所以「不设 env」= 恒等换算，这也正是这个形态的正解。
    """
    fallback = dict(_DEFAULT_HOST_PATH_MAP_NORMALIZED)
    if not raw:
        return fallback
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    return _normalize_keys(data) or fallback


class Settings:
    """全部运行时配置。属性即 ARCHITECTURE §6.1 列出的那批。"""

    def __init__(self) -> None:
        self.data_dir: str = _env("DATA_DIR", "/data") or "/data"
        self.db_path: str = os.path.join(self.data_dir, "mlm.db")
        # 配置文件（libraries.yaml + processors/*.yaml）的目录（PROCESSORS §6.1）。
        # 它必须落在数据卷里，否则容器重建就丢配置；默认 /data/config。
        self.config_dir: str = _env("CONFIG_DIR", "/data/config") or "/data/config"
        self.host_path_map: dict[str, str] = _load_host_path_map(_env("HOST_PATH_MAP"))
        self.port: int = int(_env("PORT", "18081") or "18081")
        self.secret_key: str = self._resolve_secret_key()
        self.admin_username: str | None = _env("ADMIN_USERNAME")
        self.admin_password: str | None = _env("ADMIN_PASSWORD")
        # 时区/编码相关的默认值，配合容器里的 LANG=C.UTF-8 / PYTHONUTF8=1。
        self.timezone: str = _env("TZ", "Asia/Shanghai") or "Asia/Shanghai"
        # 白名单根的首次播种初值（见模块顶部 DEFAULT_SOURCE_ROOT 的说明）。
        # 这里做成实例属性只是为了让调用方统一写 ``settings.default_source_root``，
        # **不改变**它的语义：它不会覆盖任何已存在的 DB 设置项。
        self.default_source_root: str = DEFAULT_SOURCE_ROOT
        self.default_target_root: str = DEFAULT_TARGET_ROOT

    # ------------------------------------------------------------------ 派生路径

    @property
    def log_dir(self) -> str:
        """应用与硬链接日志目录（``<data_dir>/logs``）。

        PROCESSORS §5：**所有**日志（硬链接日志 + 每个媒体库的重命名日志）都在这里，
        由 §5 的"所有重命名日志必须在同一文件夹"约束。
        """
        return os.path.join(self.data_dir, "logs")

    @property
    def hardlink_log_path(self) -> str:
        """硬链接日志 ``hard_links.log``（由 ``app/processors/hardlink.py`` 写出，
        行格式固定不变——既有日志仍然可读、可续写）。"""
        return os.path.join(self.log_dir, "hard_links.log")

    def library_log_path(self, log_file: str) -> str:
        """媒体库的重命名日志路径：``<log_dir>/<log_file>``（只允许文件名）。"""
        return os.path.join(self.log_dir, os.path.basename(log_file))

    # ------------------------------------------------------------------ 内部

    def _resolve_secret_key(self) -> str:
        """``SECRET_KEY`` 优先；否则用 ``<data_dir>/secret.key`` 落盘持久化。

        必须持久化：会话签名密钥每次重启变化会让所有人被登出（任务书要求配置持久化到卷）。
        """
        env_key = _env("SECRET_KEY")
        if env_key:
            return env_key
        key_path = os.path.join(self.data_dir, "secret.key")
        try:
            if os.path.exists(key_path):
                with open(key_path, "r", encoding="utf-8") as fh:
                    content = fh.read().strip()
                if content:
                    return content
            new_key = secrets.token_hex(32)
            os.makedirs(self.data_dir, exist_ok=True)
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write(new_key)
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
            return new_key
        except OSError:
            # 数据目录不可写（例如本地开发）时退化为进程内随机密钥，保证能起来。
            return secrets.token_hex(32)

    def ensure_dirs(self) -> None:
        """建好数据目录、日志目录与配置目录；幂等。"""
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(os.path.join(self.config_dir, "processors"), exist_ok=True)


settings = Settings()
