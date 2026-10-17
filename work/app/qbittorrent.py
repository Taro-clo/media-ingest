"""qBittorrent WebAPI 客户端（ARCHITECTURE §6.9）。

只用于**状态展示与连通性测试**（设置页的"测试连接"、仪表盘的 qB 状态灯）。
本应用**不依赖** qBittorrent 的 API 来触发任务：触发走 qB 的"运行外部程序"回调（§8），
这是刻意的解耦——回调是 qB 主动推，API 只是可选补偿。

HTTP 用 stdlib ``urllib.request``：ARCHITECTURE §1 把依赖冻结为
``fastapi`` + ``uvicorn[standard]``，不为一个 GET 请求再引 ``requests``/``httpx``。
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from .db import get_setting

logger = logging.getLogger("app.qbittorrent")


class QBError(RuntimeError):
    """qBittorrent 连接/认证/协议错误。"""


class QBClient:
    """一个 qBittorrent WebAPI 会话（cookie 由 ``OpenerDirector`` 自动管理）。"""

    def __init__(self, base_url: str, username: str = "", password: str = "", timeout: float = 10) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._logged_in = False

    # ------------------------------------------------------------------ 基础请求

    def _request(self, path: str, *, data: bytes | None = None, ok_codes=(200,)) -> bytes:
        """发一个请求；非预期 HTTP 码抛 ``QBError``（附中文说明）。"""
        if not self.base_url:
            raise QBError("尚未配置 qBittorrent 地址。")
        url = self.base_url + path
        request = urllib.request.Request(url, data=data)
        # qBittorrent 的 CSRF 防护要求 Referer 与 Host 同源，否则 POST 会被 403 掉。
        request.add_header("Referer", self.base_url)
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status not in ok_codes:
                    raise QBError(f"qBittorrent 返回 HTTP {response.status}。")
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise QBError("认证失败：用户名或密码错误（HTTP 403）。") from exc
            raise QBError(f"qBittorrent 返回 HTTP {exc.code}。") from exc
        except urllib.error.URLError as exc:
            raise QBError(f"无法连接 qBittorrent：{exc.reason}") from exc
        except TimeoutError as exc:
            raise QBError("连接 qBittorrent 超时。") from exc

    # ------------------------------------------------------------------ 会话

    def login(self) -> None:
        """登录并保存 cookie；失败抛 ``QBError``。"""
        form = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode("utf-8")
        body = self._request("/api/v2/auth/login", data=form)
        text = body.decode("utf-8", "replace").strip()
        # qB 在"IP 已被封禁"等情况下仍返回 200，必须看响应体。
        if text != "Ok.":
            raise QBError("认证失败：用户名或密码错误。")
        self._logged_in = True
        logger.debug("qBittorrent 登录成功：%s", self.base_url)

    def _ensure_login(self) -> None:
        if not self._logged_in or not self.username:
            self.login()

    # ------------------------------------------------------------------ 数据接口

    def torrents(self, **filters) -> list[dict]:
        """列出种子（``GET /api/v2/torrents/info``）。"""
        self._ensure_login()
        query = ("?" + urllib.parse.urlencode(filters)) if filters else ""
        body = self._request("/api/v2/torrents/info" + query)
        try:
            data = json.loads(body.decode("utf-8", "replace") or "[]")
        except ValueError as exc:
            raise QBError("qBittorrent 返回了无法解析的 JSON。") from exc
        return data if isinstance(data, list) else []

    def app_version(self) -> str:
        """qBittorrent 版本号（``GET /api/v2/app/version``）。"""
        self._ensure_login()
        body = self._request("/api/v2/app/version")
        return body.decode("utf-8", "replace").strip().strip('"')

    def app_webapi_version(self) -> str:
        """WebAPI 版本（``GET /api/v2/app/webapiVersion``）。"""
        self._ensure_login()
        body = self._request("/api/v2/app/webapiVersion")
        return body.decode("utf-8", "replace").strip().strip('"')


def get_client() -> QBClient | None:
    """按已保存的设置构造客户端；未配置地址时返回 ``None``。

    ``password`` 为 ``"***"`` 占位符的情况只在 HTTP 层出现，读到 DB 值的一定是明文。
    """
    url = (get_setting("qbittorrent_url", "") or "").strip()
    if not url:
        return None
    return QBClient(
        base_url=url,
        username=(get_setting("qbittorrent_username", "") or "").strip(),
        password=get_setting("qbittorrent_password", "") or "",
    )


def test_connection(base_url: str, username: str = "", password: str = "") -> tuple[bool, str]:
    """测试连通性；返回 ``(是否成功, 版本号或错误信息)``。

    契约（ARCHITECTURE §6.9）：连不上/认证失败都返回 ``(False, 中文原因)``，**不抛异常**——
    HTTP 层据此返回 ``200 + ok:false``（那是业务结果，不是 API 错误）。
    无用户名时跳过登录，直接请求版本接口（qB 若开了"免认证本地访问"就能成功）。
    """
    client = QBClient(base_url=base_url, username=username, password=password)
    try:
        if username:
            client.login()
        else:
            # 无凭据：先探版本接口，能通说明 qB 允许免认证访问。
            version = client.app_version()
            return True, version
        return True, client.app_version()
    except QBError as exc:
        return False, str(exc)
    except Exception as exc:  # 兜底：状态探测永不应把 500 抛给前端
        logger.debug("测试 qBittorrent 连接时出现意外异常：%s", exc)
        return False, f"测试失败：{exc}"


def test_saved() -> tuple[bool, str]:
    """用已保存的设置测试连通性（供 scanner 的周期刷新使用）。"""
    return test_connection(
        (get_setting("qbittorrent_url", "") or "").strip(),
        (get_setting("qbittorrent_username", "") or "").strip(),
        get_setting("qbittorrent_password", "") or "",
    )


def latency_of(base_url: str, username: str = "", password: str = "") -> tuple[bool, str, int]:
    """带耗时版本的连通性测试：``(ok, 版本或错误, 耗时毫秒)``。"""
    started = time.monotonic()
    ok, message = test_connection(base_url, username, password)
    return ok, message, int((time.monotonic() - started) * 1000)
