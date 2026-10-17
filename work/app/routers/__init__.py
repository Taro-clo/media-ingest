"""HTTP 路由包（ARCHITECTURE §5）。

每个模块一个 ``router``，由 ``app.main.create_app()`` 按固定顺序 ``include_router``：

==============  ==========================  ==========================================
模块            前缀                        职责
==============  ==========================  ==========================================
``auth``        ``/api/auth``               初始化 / 登录 / 登出 / 改密 / ``me``
``libraries``   ``/api/libraries``          媒体库 CRUD、预检 ``validate``、触发 ``run``
``tasks``       ``/api/tasks``              历史列表、日志增量拉取、SSE、重试/取消/删除
``settings``    ``/api/settings``           设置读写、重置 token、测 qBittorrent
``stats``       ``/api/stats``              概览统计
``system``      ``/api/system``             健康检查（免认证）、诊断信息
``hooks``       ``/api/hooks``              qBittorrent 回调、手动扫描
``todos``       ``/api/todos``              全局待办看板：列表 / 汇总 / 打钩 / 恢复（无删除）
==============  ==========================  ==========================================

这里**不**主动 import 子模块：``__init__`` 保持零副作用，避免"导入包"就顺带把
FastAPI 依赖、数据库连接乃至后台线程的风格带进来（``create_app`` 才是唯一的组装点）。
"""

from __future__ import annotations

__all__ = [
    "auth",
    "hooks",
    "libraries",
    "settings",
    "stats",
    "system",
    "tasks",
    "todos",
]
