"""配置文件（YAML）双向同步 —— ``docs/contracts/PROCESSORS.md`` §6.2 / §7。

真源是数据库；``/data/config`` 下的 YAML 是**可读可改的镜像**：

* ``GET /api/config``：列出配置文件、哈希、是否与上次记录一致、导入/导出时间与错误；
* ``POST /api/config/reload?force=true``：立刻从磁盘导入（``force`` 忽略"哈希未变"快速路径）；
* ``POST /api/config/export``：立刻把数据库写出到磁盘（外部编辑被覆盖，用于"以 DB 为准"）。

这两个写接口都不改业务逻辑，只是把后台线程每 5 秒做的事手动触发一次。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from .. import configstore, security
from .common import bad_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config(_: str = Depends(security.require_user)) -> dict:
    """配置目录快照（§7：``config_dir`` / ``files`` / ``last_import_at`` / ``errors``…）。"""
    return configstore.snapshot()


@router.post("/reload")
def reload_config(
    _: str = Depends(security.require_user),
    force: bool = Query(default=False),
) -> dict:
    """强制从文件导入（``force=true`` 时忽略哈希，全部重导）。

    顺序由 ``configstore.reload`` 保证：**先处理器、后媒体库**（库的 ``processor_id``
    必须已经存在）。导入失败**不改数据库**，错误进 ``config_events`` 并由
    ``GET /api/config`` 的 ``errors`` 暴露。
    """
    result = configstore.reload(force=force)
    return {**configstore.snapshot(), "result": result}


@router.post("/export")
def export_config(_: str = Depends(security.require_user)) -> dict:
    """把数据库里的处理器与媒体库全部写出到 ``/data/config``（覆盖手工编辑）。"""
    written = configstore.export_all()
    if not written:
        raise bad_request(
            f"没有可导出的配置（处理器与媒体库都是空的），或目录不可写："
            f"{configstore.config_dir()}"
        )
    return {**configstore.snapshot(), "written": written}
