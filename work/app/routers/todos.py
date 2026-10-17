"""``/api/todos/*``（ARCHITECTURE §5「待办」 / §6.10）。

看板是**全局一份**台账：这几个端点只做"读列表 / 读计数 / 打钩 / 恢复 / 一键检测"。
（``GET /detect`` 只读地把"实况已说明不用再管"的条目挑出来；``POST /detect-resolve`` 把用户
在弹窗里确认的那些逐个**再核对一遍**后打钩。）

**刻意没有删除端点**：打钩是可逆的标记（``resolve`` ↔ ``reopen``），删掉一条就再也说不清
"这事发生过几次、上次是什么时候"。详见 ``app/todos.py`` 的去重语义。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query

from .. import security
from ..todos import (
    DETECT_MODES,
    KNOWN_KINDS,
    TODO_STATUSES,
    detect_resolved,
    list_todos,
    reopen_todo,
    resolve_detected,
    resolve_todo,
    todo_summary,
)
from .common import bad_request, clamp_limit, clamp_offset, not_found

router = APIRouter(prefix="/api/todos", tags=["todos"])

#: ``GET /api/todos`` 的 ``status`` 过滤值；``all`` = 不过滤（看板上"已完成"那一栏要用）。
VALID_STATUSES = (*TODO_STATUSES, "all")


@router.get("")
def list_todos_endpoint(
    _: str = Depends(security.require_user),
    status: str = Query(default="open"),
    kind: str | None = Query(default=None),
    library_id: int | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
) -> dict:
    """待办列表（信封格式；``total`` 是**过滤后**总数，与 ``/api/tasks`` 一致）。

    缺省只给未完成的（``status=open``）——看板首屏要的就是"还需要我处理的"。

    ``q``（看板的「作品」筛选框）：作品名子串，大小写不敏感，``%``/``_`` 按字面处理；
    空/省略 = 不过滤。它匹配的是条目的问题摘要 / 详情 / 去重键三列文本（详情里的目录与路径
    形如 ``Work 03/Season 1``），所以是**朴素子串**而不是按作品精确归属 —— 详见
    ``app.todos.list_todos`` 的说明与 ``docs/meta/KNOWN_ISSUES.md`` KI-10。
    """
    if status not in VALID_STATUSES:
        raise bad_request(
            f"未知的待办状态：{status}。可选值：" + "、".join(VALID_STATUSES) + "。"
        )
    if kind is not None and kind not in KNOWN_KINDS:
        raise bad_request(
            f"未知的待办种类：{kind}。可选值：" + "、".join(KNOWN_KINDS) + "。"
        )
    return list_todos(
        status=status,
        kind=kind,
        library_id=int(library_id) if library_id is not None else None,
        query=str(q or "").strip() or None,
        limit=clamp_limit(limit),
        offset=clamp_offset(offset),
    )


@router.get("/summary")
def get_summary(_: str = Depends(security.require_user)) -> dict:
    """按状态与种类计数（首页徽标与筛选下拉框的数据源）。"""
    return todo_summary()


@router.get("/detect")
def detect(
    _: str = Depends(security.require_user),
    kind: str | None = Query(default=None),
    library_id: int | None = Query(default=None),
    limit: int = Query(default=200),
    offset: int = Query(default=0),
) -> dict:
    """一键检测（**只读**，一个字节都不改）：挑出"文件系统实况已经说明不用再管"的未完成待办。

    判据是**两处证据的交叉**：待办自己记的 ``task_id`` + ``dedupe_key``，加上任务结果里的原始
    结构化样本；再拿它去和媒体库当前的目录实况比对（路径没了 / 缺失的东西补上了）。

    能自动判定的种类见 ``detectable_kinds``；``task_failed`` 与 ``source_integrity`` **不在其中**
    —— 任务级失败、源目录变动都没有单一可核对的文件路径，只能在核对后手工打钩。
    前端拿到 ``candidates`` 后用弹窗让用户勾选，再调 ``POST /api/todos/detect-resolve``。
    """
    if kind is not None and kind not in KNOWN_KINDS:
        raise bad_request(
            f"未知的待办种类：{kind}。可选值：" + "、".join(KNOWN_KINDS) + "。"
        )
    return detect_resolved(
        kind=kind,
        library_id=int(library_id) if library_id is not None else None,
        limit=clamp_limit(limit),
        offset=clamp_offset(offset),
    )


@router.post("/detect-resolve")
def detect_resolve(
    ids: list[int] = Body(default=[], embed=True),
    _: str = Depends(security.require_user),
) -> dict:
    """把用户在弹窗里确认的条目打钩。

    **逐个再核对一遍**（见 :func:`app.todos.resolve_detected`）：拿不出证据的原样不动，并在
    ``skipped`` 里说明原因 —— 弹窗可能是几分钟前拉的，期间文件可能被挪回来了。
    """
    return resolve_detected(ids)


@router.post("/{todo_id}/resolve")
def resolve(todo_id: int, _: str = Depends(security.require_user)) -> dict:
    """打钩：``open`` → ``done``。**只改状态、不删任何数据**，随时可 ``reopen`` 恢复。"""
    item = resolve_todo(todo_id)
    if item is None:
        raise not_found(f"待办 {todo_id} 不存在。")
    return item


@router.post("/{todo_id}/reopen")
def reopen(todo_id: int, _: str = Depends(security.require_user)) -> dict:
    """恢复未完成：``done`` → ``open``（历史条目照样留着）。"""
    item = reopen_todo(todo_id)
    if item is None:
        raise not_found(f"待办 {todo_id} 不存在。")
    return item
