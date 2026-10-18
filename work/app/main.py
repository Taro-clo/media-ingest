"""应用入口（ARCHITECTURE §5 / §7 + PROCESSORS §6）。

启动顺序是**契约的一部分**，不能随意调整：

1. ``settings.ensure_dirs()`` + ``init_db()`` —— 建表、迁移、写入默认设置、生成
   ``hook_token``、按 env 播种首个管理员、确保三个内置处理器存在；
2. 起两个后台线程：任务队列（``mlm-task-worker``）、定时扫描（``mlm-scanner``），
   以及配置文件监听线程（``mlm-config``，PROCESSORS §6.2 的外部编辑自动导入）；
3. 挂路由**先于**静态目录：``/api/*`` 必须在 ``StaticFiles`` 之前注册，
   否则 ``/api/anything`` 会被静态目录抢走（§5.0 第 7 条）。

**没有**日志播种步骤：每个媒体库有自己的重命名日志（``libraries.log_file``），
不存在从外部目录拷贝/本地化日志的环节（PROCESSORS §5.3）。

**单进程单 worker**：原生硬链接实现在体积变化时会整份读-改-写日志文件，
多进程会互相覆盖。所以 ``uvicorn.run(..., workers=1)``，
队列也只有一个工作线程；部署时不要给 uvicorn/gunicorn 加 worker 数。
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, configstore, paths
from .config import settings
from .db import get_conn, get_setting, init_db
from .queue import queue
from .routers import (
    auth,
    config as config_router,
    hooks,
    libraries,
    processors as processors_router,
    settings as settings_router,
    stats,
    system,
    tasks,
    todos,
)
from .scanner import scanner

logger = logging.getLogger("app.main")

#: 静态前端目录的候选位置（按顺序找，第一个存在的就用）。
#: ``WEB_DIR`` 在最前，方便部署时覆盖；``/srv/mlm/web`` 是容器里的位置
#: （docs/meta/STRUCTURE.md：容器应用根 = ``/srv/mlm``，即 Dockerfile 的 ``WORKDIR``）。
WEB_DIR_ENV = "WEB_DIR"
WEB_DIR_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "web",  # 仓库根下的 web/
    Path("/srv/mlm/web"),  # 容器内（WORKDIR=/srv/mlm）
)


def _resolve_web_dir() -> Path | None:
    """找到静态前端目录；不存在返回 ``None``（不创建它——前端是另一个人的产物）。"""
    candidates: list[Path] = []
    override = os.environ.get(WEB_DIR_ENV)
    if override:
        candidates.append(Path(override))
    candidates.extend(WEB_DIR_CANDIDATES)
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _configure_logging() -> str:
    """按设置表里的 ``log_level`` 配置根 logger；返回最终级别名。"""
    level_name = (get_setting("log_level", "INFO") or "INFO").strip().upper()
    numeric = getattr(logging, level_name, None)
    if not isinstance(numeric, int):
        level_name, numeric = "INFO", logging.INFO
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(numeric)
    logging.getLogger("app").setLevel(numeric)
    # uvicorn 自己的 access log 走单独 logger，级别跟着设置一起降噪。
    logging.getLogger("uvicorn.access").setLevel(max(numeric, logging.INFO))
    return level_name


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动自检 + 后台线程生命周期（见模块 docstring 的启动顺序）。"""
    settings.ensure_dirs()  # 1) 数据卷子目录（logs/ 与 config/）
    init_db()  # 1) 建表 + 迁移 + 默认设置 + hook_token + 首个管理员 + 内置处理器
    level_name = _configure_logging()

    logger.info(
        "媒体入库整理启动：版本=%s 数据目录=%s 配置目录=%s 端口=%s 日志级别=%s",
        __version__,
        settings.data_dir,
        settings.config_dir,
        settings.port,
        level_name,
    )

    queue.start()
    scanner.start()
    configstore.start_watcher()  # PROCESSORS §6.2：外部编辑 YAML 后自动导入（间隔可设 0 关闭）

    # 清扫上次进程被强杀时可能残留的硬链接探针临时文件（``.mlm_probe_*.link``）。
    # 探针正常路径自己会删；这里的兜底避免它们被媒体扫描器/Emby 当成垃圾文件。
    # 目标目录逐库取（v2 已无「允许的目标根」白名单）。
    try:
        rows = get_conn().execute("SELECT DISTINCT target_dir FROM libraries").fetchall()
        for row in rows:
            root = str(row["target_dir"] or "").strip()
            if not root:
                continue
            removed = paths.cleanup_probe_leftovers(root)
            if removed:
                logger.warning("已清理 %s 下 %s 个残留的探针临时文件（%s*）", root, removed, paths.PROBE_PREFIX)
    except Exception:  # 清扫失败绝不该挡住启动
        logger.exception("清扫探针临时文件失败")
    try:
        yield
    finally:
        configstore.stop_watcher()
        scanner.stop()
        queue.stop()
        logger.info("媒体入库整理已停止")


def create_app() -> FastAPI:
    """构造 FastAPI 应用（工厂形式，便于测试里反复建实例）。"""
    app = FastAPI(
        title="媒体入库整理 / Media Ingest",
        description=(
            "为 qBittorrent 完成的下载创建硬链接并按既定规则重命名。"
            "硬链接的目录层级、命名规则与正则识别逻辑完全沿用既有语义，绝不改变。"
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # ---- 1) 访问日志（轻量，方便排查回调是否真的到达）
    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        path = request.url.path
        # 健康检查会被容器每秒打一次，静默掉；SSE 是长连接，只记开始不记耗时。
        if path != "/api/system/health":
            logger.debug(
                "%s %s -> %s (%.1fms)",
                request.method,
                path,
                response.status_code,
                elapsed_ms,
            )
        return response

    # ---- 2) 静态前端禁用浏览器启发式缓存
    # 没有 Cache-Control 时，浏览器会按 Last-Modified 推算"新鲜期"，期间刷新也
    # 不会重新校验 —— 部署后旧的 js/css 可能被继续直接使用（改密码反馈曾因此
    # 看起来"没生效"）。no-cache 只要求每次带 ETag 重新校验，未变化仍回 304，
    # 代价可以忽略；API 响应不受影响（/api/* 各自决定缓存策略）。
    @app.middleware("http")
    async def static_no_cache(request: Request, call_next):
        response = await call_next(request)
        if request.method in {"GET", "HEAD"} and not request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # ---- 3) 未捕获异常：给前端一个稳定的 JSON 形状，而不是 HTML 堆栈
    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理的异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务器内部错误：{type(exc).__name__}: {exc}"},
        )

    # ---- 4) API 路由：**必须**先于静态目录注册（ARCHITECTURE §5.0 ② 路由挂载）
    app.include_router(auth.router)
    app.include_router(libraries.router)
    app.include_router(tasks.router)
    app.include_router(processors_router.router)
    app.include_router(config_router.router)
    app.include_router(settings_router.router)
    app.include_router(stats.router)
    app.include_router(todos.router)
    app.include_router(system.router)
    app.include_router(hooks.router)

    # ---- 5) 静态前端：整棵挂在 /（html=True 让 GET / 命中 index.html）
    web_dir = _resolve_web_dir()
    if web_dir is not None:
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
        logger.info("静态前端目录：%s", web_dir)
    else:
        logger.warning(
            "没有找到静态前端目录（找了 %s 与 /srv/mlm/web）：只有 /api/* 可用。",
            WEB_DIR_CANDIDATES[0],
        )

        @app.get("/", include_in_schema=False)
        def index_missing() -> PlainTextResponse:
            return PlainTextResponse(
                "媒体入库整理后端已就绪，但没有找到静态前端目录。\n"
                "请确认 web/index.html 存在，或用环境变量 WEB_DIR 指定前端目录。\n"
                "API 文档：/api/docs\n",
                status_code=200,
            )

    return app


app = create_app()


def main() -> None:
    """``python -m app.main`` 时的入口（开发用；容器里由 uvicorn 直接起 ``app.main:app``）。"""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        # **必须**是 1。日志的读-改-写不是并发安全的，多 worker 会互相覆盖。
        workers=1,
        log_level=(get_setting("log_level", "INFO") or "INFO").strip().lower(),
    )


if __name__ == "__main__":
    main()
