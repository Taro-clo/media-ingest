"""口令哈希、会话 Cookie 与鉴权依赖（ARCHITECTURE §6.3）。

全部用 stdlib：``hashlib.scrypt`` + ``hmac`` + ``secrets``。理由见 ARCHITECTURE §1
——不引入 bcrypt 之类的原生扩展依赖，部署面的构建风险最小。

**FastAPI 是可选导入**：哈希/签名这几件事在不依赖 Web 框架的环境（例如适配层自测）里也要能用，
所以只有 ``require_user`` 依赖里才真正需要 fastapi。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from .config import settings

try:  # pragma: no cover - 本地无 fastapi 时走 except 分支
    from fastapi import HTTPException, Request
except Exception:  # pragma: no cover
    HTTPException = None  # type: ignore[assignment]
    Request = object  # type: ignore[assignment]

# scrypt 参数（ARCHITECTURE §1 冻结）：N=2^14, r=8, p=1
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

SESSION_COOKIE = "mlm_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 天


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(pw: str) -> str:
    """生成 ``scrypt$N$r$p$salt_b64$hash_b64`` 格式的口令哈希。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(pw: str, stored: str) -> bool:
    """校验口令。用 ``hmac.compare_digest`` 做恒时比较，避免时序侧信道。"""
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _b64d(parts[4])
        expected = _b64d(parts[5])
    except (ValueError, TypeError):
        return False
    try:
        digest = hashlib.scrypt(
            pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, expected)


def make_session(username: str) -> str:
    """生成 ``username.expiry.hmac`` 形式的签名会话串（用户名做 URL 安全编码）。

    username 里若含 ``.`` 会破坏三段解析，所以用 base64url 编码承载。
    """
    expiry = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{_b64e(username.encode('utf-8'))}.{expiry}"
    signature = hmac.new(
        settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def read_session(token: str) -> str | None:
    """校验会话串并返回用户名；签名不符/过期/格式错 → ``None``。"""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    encoded_user, raw_expiry, signature = parts
    payload = f"{encoded_user}.{raw_expiry}"
    expected = hmac.new(
        settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return None
    if expiry < int(time.time()):
        return None
    try:
        return _b64d(encoded_user).decode("utf-8")
    except (ValueError, TypeError):
        return None


def _unauthorized(detail: str = "未登录或会话已失效，请重新登录。"):
    if HTTPException is None:  # pragma: no cover
        raise RuntimeError(detail)
    return HTTPException(status_code=401, detail=detail)


def require_user(request: Request) -> str:
    """FastAPI 依赖：从 Cookie 里读会话，未认证抛 401。

    ``request: Request`` 这个标注**不能省**：FastAPI 是靠参数的类型标注（而不是参数名）
    决定要不要注入 ``Request`` 的。只写 ``request`` 会被当成一个**必需的查询参数**，
    于是所有挂了本依赖的端点都会返回 422 ``{"loc":["query","request"],"msg":"Field required"}``，
    而不是 401/200 —— 这是 FastAPI 的既定行为，别再改成不带标注的版本。

    另注：本模块顶部 ``except Exception: Request = object`` 是给"没装 fastapi"的环境兜底用的
    （那种环境根本不会挂路由，标注退化成 ``object`` 也无所谓）；装了 fastapi 时它是真正的
    ``starlette.requests.Request``。
    """
    token = request.cookies.get(SESSION_COOKIE) if hasattr(request, "cookies") else None
    username = read_session(token) if token else None
    if not username:
        raise _unauthorized()
    return username


def check_hook_token(token: str | None) -> bool:
    """校验 ``/api/hooks/*`` 的 Bearer/query token（恒时比较）。"""
    if not token:
        return False
    from .db import get_setting

    expected = get_setting("hook_token", "") or ""
    if not expected:
        return False
    return hmac.compare_digest(str(token), expected)


def users_exist() -> bool:
    """是否已存在任何用户（决定 ``needs_setup``）。"""
    from .db import get_conn

    row = get_conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return bool(row and row["n"] > 0)


def create_first_user(username: str, password: str) -> None:
    """创建首个管理员；已存在用户时抛 ``ValueError``（路由层转 409）。"""
    from .db import now_iso, tx

    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空。")
    if len(password or "") < 8:
        raise ValueError("密码长度至少 8 位。")
    with tx() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row["n"] > 0:
            raise ValueError("管理员已存在，无法重复初始化。")
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES(?,?,?)",
            (username, hash_password(password), now_iso()),
        )


def set_password(username: str, new_password: str) -> None:
    """改密（``ADMIN_USERNAME`` 播种的用户名与登录名一致）。"""
    from .db import now_iso, tx

    if len(new_password or "") < 8:
        raise ValueError("新密码长度至少 8 位。")
    with tx() as conn:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE username=?",
            (hash_password(new_password), username),
        )


def get_user(username: str):
    """按用户名取一行用户记录（``sqlite3.Row``）。"""
    from .db import get_conn

    return get_conn().execute(
        "SELECT * FROM users WHERE username=?", (username,)
    ).fetchone()


def parse_json_body(raw: bytes) -> dict:
    """容错解析 JSON 请求体：空体/非对象 → 空 dict（用于回调这类宽松入口）。"""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
