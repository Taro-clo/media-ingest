"""``/api/auth/*``（ARCHITECTURE §5.1）。

密码与用户名的规则集中放在这里，且**服务端必须再校验一遍**：WebUI 的 ``minlength``
只是即时反馈，不能替代后端判定（浏览器控制台可以绕过）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from .. import security
from ..api_models import Credentials, PasswordChange
from ..db import get_conn
from ..serializers import format_time
from .common import bad_request, conflict, unauthorized

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8
MIN_USERNAME_LENGTH = 2


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _validate_credentials(username: str, password: str) -> tuple[str, str]:
    """返回清洗后的 ``(username, password)``；不合法抛 400。"""
    name = _clean(username)
    if len(name) < MIN_USERNAME_LENGTH:
        raise bad_request(f"用户名至少需要 {MIN_USERNAME_LENGTH} 个字符。")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise bad_request(f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符。")
    return name, password


def _user_row(username: str):
    return get_conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def _set_session_cookie(response: Response, username: str) -> None:
    """写会话 Cookie。``httponly`` 防脚本读取；``samesite=lax`` 兼顾可用性与 CSRF。"""
    response.set_cookie(
        key=security.SESSION_COOKIE,
        value=security.make_session(username),
        max_age=security.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.get("/me")
def me(request: Request) -> dict:
    """当前登录状态。未登录时**不返回 401**，而是 ``{authenticated: false, needs_setup: …}``。

    理由：前端首屏就要决定“跳登录页还是跳初始化向导”，若此处 401，
    浏览器控制台会塞满红色错误，且前端还得把 401 当成正常分支处理。
    """
    # security.read_session() 要的是 **token 字符串**，不是 Request 对象，
    # 所以这里必须先把 Cookie 取出来（security.require_user 里是同样的写法）。
    username = security.read_session(request.cookies.get(security.SESSION_COOKIE))
    users_exist = security.users_exist()
    if username and users_exist:
        row = _user_row(username)
        return {
            "authenticated": True,
            "needs_setup": False,
            "username": username,
            "created_at": format_time(row["created_at"]) if row is not None else None,
        }
    return {
        "authenticated": False,
        "needs_setup": not users_exist,
        "username": None,
        "created_at": None,
    }


@router.post("/setup", status_code=201)
def setup(body: Credentials, response: Response) -> dict:
    """首次初始化：创建唯一管理员并直接登录（201）。

    必须是全新实例：只要已存在用户就 409 —— 否则任何人都能重新初始化并接管实例。
    """
    if security.users_exist():
        raise conflict("实例已初始化，无法重复执行初始化。如需重置密码请使用修改密码接口。")
    username, password = _validate_credentials(body.username, body.password)
    try:
        security.create_first_user(username, password)
    except ValueError:
        # create_first_user 内部已做一次存在性检查，这里是并发下的兜底。
        raise conflict("实例已初始化，无法重复执行初始化。")
    _set_session_cookie(response, username)
    return {"ok": True, "username": username, "message": "初始化完成。"}


@router.post("/login")
def login(body: Credentials, response: Response) -> dict:
    """登录成功 200 + ``Set-Cookie: mlm_session``；失败 401 中文提示。"""
    username, password = _clean(body.username), body.password or ""
    row = _user_row(username) if username else None
    if row is None or not security.verify_password(password, row["password_hash"]):
        # 用户不存在与密码错误返回同一句文案，避免枚举用户名。
        raise unauthorized("用户名或密码错误。")
    # 这里**不**记录"最后登录时间"：ARCHITECTURE §3 的 users 表只有
    # (id, username, password_hash, created_at) 四列，实证多写一列会让登录直接 500
    # （sqlite3.OperationalError: no such column），而且首次发布并不需要这个字段。
    _set_session_cookie(response, username)
    return {"ok": True, "username": username, "message": "登录成功。"}


@router.post("/logout", status_code=204)
def logout(response: Response) -> Response:
    """清除 Cookie。幂等：未登录时调用也返回 204。"""
    response.delete_cookie(key=security.SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.post("/password", status_code=204)
def change_password(
    body: PasswordChange,
    response: Response,
    username: str = Depends(security.require_user),
) -> Response:
    """修改自己的密码；成功后**当前会话失效**，前端需引导重新登录。"""
    row = _user_row(username)
    if row is None or not security.verify_password(body.old_password or "", row["password_hash"]):
        raise unauthorized("当前密码不正确。")
    if len(body.new_password or "") < MIN_PASSWORD_LENGTH:
        raise bad_request(f"新密码至少需要 {MIN_PASSWORD_LENGTH} 个字符。")
    if body.new_password == body.old_password:
        raise bad_request("新密码不能与当前密码相同。")
    security.set_password(username, body.new_password)
    # 会话签名里带的是用户名而非密码散列，改密码不会自动失效旧 Cookie —— 这里显式清掉，
    # 让「改完密码需要重新登录」成为真实行为，而不是前端的一厢情愿。
    response.delete_cookie(key=security.SESSION_COOKIE, path="/")
    response.status_code = 204
    return response
