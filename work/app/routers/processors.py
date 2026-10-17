"""处理器（重命名 / 硬链接规则）管理 —— ``docs/contracts/PROCESSORS.md`` §7。

设计要点：

* **规格的真源是数据库**（``processors.spec_json``）；每次写成功都由注册表把该处理器
  写出成 ``/data/config/processors/<id>.yaml``（§6.2 的"写出方向"），失败只记日志、
  绝不回滚业务写入——配置文件是"可读的镜像"，不是业务的仲裁者。
* 校验失败一律 **422 + 中文字段级** ``detail[{loc,msg,type}]``：路由层先自己跑一遍
  ``validate_spec``（拿到逐字段错误），再交给注册表（它还会校验一次并落库）。
* 三个内置处理器**允许改规则**（用户可以按自己的口味微调），但**不能删除**，
  只能用 ``POST /api/processors/{id}/reset`` 恢复 ``default`` / ``legacy`` 预设。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from .. import security
from ..api_models import ProcessorCreate, ProcessorPreset
from ..processors import defaults, registry
from ..processors.registry import RegistryError
from ..processors.spec import ID_PATTERN, SPEC_TEMPLATES, normalize_spec, validate_spec
from .common import (
    clamp_limit,
    clamp_offset,
    not_found,
    unprocessable,
    unprocessable_many,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/processors", tags=["processors"])


def _raise(exc: RegistryError) -> None:
    """把注册表的领域异常翻译成 HTTP（它自带 status_code；缺省 400）。"""
    from fastapi import HTTPException

    raise HTTPException(status_code=exc.status_code or 400, detail=str(exc)) from exc


def _check_spec(data: Any) -> dict[str, Any]:
    """``normalize_spec`` + ``validate_spec``，把错误变成 422（返回规范化后的 spec）。"""
    if not isinstance(data, dict):
        raise unprocessable("", "请求体必须是一个对象（处理器规格 JSON/YAML 映射）。")
    try:
        spec = normalize_spec(data)
    except Exception as exc:  # noqa: BLE001 - normalize 对畸形输入可能抛
        raise unprocessable("", f"规格结构无法识别：{exc}") from exc
    errors = validate_spec(spec)
    if errors:
        raise unprocessable_many([(item["field"], item["message"]) for item in errors])
    return spec


def _get_or_404(processor_id: str) -> dict[str, Any]:
    spec = registry.get_processor(processor_id)
    if spec is None:
        raise not_found(f"处理器 {processor_id} 不存在。")
    return spec


@router.get("")
def list_processors(
    _: str = Depends(security.require_user),
    builtin: bool | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    offset: int | None = Query(default=None),
) -> dict:
    """列出处理器（信封形状，与其它列表接口一致）。"""
    items = registry.list_processors(builtin=builtin, kind=kind)
    total = len(items)
    take = clamp_limit(limit)
    skip = clamp_offset(offset)
    return {
        "items": items[skip : skip + take],
        "total": total,
        "limit": take,
        "offset": skip,
    }


@router.post("", status_code=201)
def create_processor(
    body: ProcessorCreate, _: str = Depends(security.require_user)
) -> dict:
    """新建自定义处理器（201）。``builtin`` 恒为 ``false``，``schema_version`` 由服务端写。"""
    processor_id = (body.id or "").strip()
    if not ID_PATTERN.match(processor_id):
        raise unprocessable(
            "id",
            f"处理器 id 不合法：{processor_id!r}；必须以小写字母开头，只含小写字母/数字/"
            f"下划线/连字符，长度 2–32（例如 my_bangumi）。",
        )
    template = (body.template or "blank").strip() or "blank"
    if template not in SPEC_TEMPLATES:
        raise unprocessable(
            "template",
            f"未知模板：{template}；可选：{'、'.join(SPEC_TEMPLATES)}。",
        )
    name = (body.name or "").strip() or processor_id
    if body.spec:
        data = dict(body.spec)
        data["id"] = processor_id
        data["name"] = name
        if body.kind:
            data["kind"] = body.kind
        data["builtin"] = False
        data.pop("reference_script", None)  # 兼容旧客户端：带来的该键一律忽略
        spec = _check_spec(data)
    else:
        spec = registry.spec_from_template(template, processor_id, name, body.kind)
        _check_spec(spec)
    try:
        created = registry.create_processor(spec)
    except RegistryError as exc:
        _raise(exc)
        raise  # pragma: no cover - _raise 一定抛异常
    return created


@router.post("/validate")
def validate_processor(
    body: dict[str, Any] = Body(...), _: str = Depends(security.require_user)
) -> dict:
    """**不落库**地校验一份 spec，返回 ``{ok, errors:[{field,message}]}``（§7）。"""
    if not isinstance(body, dict):
        return {"ok": False, "errors": [{"field": "", "message": "请求体必须是一个对象。"}]}
    try:
        spec = normalize_spec(body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "errors": [{"field": "", "message": f"规格结构无法识别：{exc}"}]}
    errors = validate_spec(spec)
    return {"ok": not errors, "errors": errors}


@router.get("/{processor_id}")
def get_processor(processor_id: str, _: str = Depends(security.require_user)) -> dict:
    """单条处理器规格。"""
    return _get_or_404(processor_id)


@router.put("/{processor_id}")
def update_processor(
    processor_id: str,
    body: dict[str, Any] = Body(...),
    _: str = Depends(security.require_user),
) -> dict:
    """全量替换 spec（``id`` 不可改；内置处理器允许改规则）。"""
    _get_or_404(processor_id)
    data = dict(body or {})
    incoming_id = data.get("id")
    if incoming_id is not None and str(incoming_id) != processor_id:
        raise unprocessable(
            "id",
            f"处理器 id 不可修改（当前是 {processor_id}，请求里是 {incoming_id!r}）。",
        )
    data["id"] = processor_id
    _check_spec(data)
    try:
        return registry.update_processor(processor_id, data)
    except RegistryError as exc:
        _raise(exc)
        raise  # pragma: no cover


@router.delete("/{processor_id}")
def delete_processor(processor_id: str, _: str = Depends(security.require_user)) -> dict:
    """删除自定义处理器；内置 ⇒ 409；仍被媒体库引用 ⇒ 409 并给出引用清单。"""
    _get_or_404(processor_id)
    try:
        return registry.delete_processor(processor_id)
    except RegistryError as exc:
        _raise(exc)
        raise  # pragma: no cover


@router.post("/{processor_id}/reset")
def reset_processor(
    processor_id: str,
    body: ProcessorPreset | None = Body(default=None),
    _: str = Depends(security.require_user),
) -> dict:
    """把内置处理器重置为 ``default``（推荐默认）或 ``legacy``（最朴素的旧口径）。"""
    _get_or_404(processor_id)
    preset = (body.preset if body else "default") or "default"
    if preset not in defaults.PRESETS:
        raise unprocessable(
            "preset", f"未知预设：{preset}；可选：{'、'.join(defaults.PRESETS)}。"
        )
    try:
        return registry.reset_processor(processor_id, preset)
    except RegistryError as exc:
        _raise(exc)
        raise  # pragma: no cover
