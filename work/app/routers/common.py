"""HTTP 层的共享小工具：统一的中文错误响应与通用查询参数校验。"""

from __future__ import annotations

from fastapi import HTTPException

#: 列表接口的 limit 上限（§4.2：超过按 200 处理）。
MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50


def bad_request(detail: str | dict) -> HTTPException:
    """400：参数不合法（文案直接展示给用户，必须是中文）。

    ``detail`` 允许两种形态：

    - ``str``：一行中文，WebUI 直接 toast（大多数参数校验）；
    - ``dict``：结构化错误（如 ``{"code": "permission_precheck", ...}``），
      给需要弹窗展示明细的调用方用；``message`` 字段必须存在，作为兜底短文案。
    """
    return HTTPException(status_code=400, detail=detail)


def unauthorized(detail: str = "未登录或登录已过期，请重新登录。") -> HTTPException:
    """401。"""
    return HTTPException(status_code=401, detail=detail)


def not_found(detail: str) -> HTTPException:
    """404。"""
    return HTTPException(status_code=404, detail=detail)


def conflict(detail: str) -> HTTPException:
    """409：状态冲突。"""
    return HTTPException(status_code=409, detail=detail)


def unprocessable(field: str, message: str) -> HTTPException:
    """422：字段级校验失败（§7 冻结：``detail`` 是 ``[{loc,msg,type}]`` 数组）。

    ``loc`` 第一段固定 ``body``（与 Pydantic 的形状一致，前端只需一套解析逻辑），
    ``field`` 允许用点号表达嵌套，例如 ``naming.segments.0.param``。
    """
    return HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", *[part for part in str(field).split(".") if part]],
                "msg": message,
                "type": "value_error.mlm",
            }
        ],
    )


def unprocessable_many(items: list[tuple[str, str]]) -> HTTPException:
    """422：一次报多个字段错误（``items`` 为 ``(field, message)`` 列表）。"""
    detail = [
        {
            "loc": ["body", *[part for part in str(field).split(".") if part]],
            "msg": message,
            "type": "value_error.mlm",
        }
        for field, message in items
    ]
    return HTTPException(status_code=422, detail=detail)


def clamp_limit(limit: int | None) -> int:
    """把 ``limit`` 收敛到 ``[1, 200]``；非法输入按默认 50 处理。"""
    try:
        value = int(limit) if limit is not None else DEFAULT_LIST_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_LIST_LIMIT
    if value <= 0:
        return DEFAULT_LIST_LIMIT
    return min(value, MAX_LIST_LIMIT)


def clamp_offset(offset: int | None) -> int:
    """把 ``offset`` 收敛到 ``>= 0``。"""
    try:
        value = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        return 0
    return max(value, 0)
