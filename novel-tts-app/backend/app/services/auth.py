"""
用户鉴权服务：bcrypt 密码哈希 + JWT 签发/校验 + 启动 seed admin/admin。

依赖：
- bcrypt 4.x（密码哈希）
- PyJWT 2.x（token 签发）

公开 API：
- hash_password(plain) / verify_password(plain, hash)
- create_access_token(username) -> (token, expires_at)
- decode_token(token) -> username | None
- authenticate(username, password) -> User | None
- seed_admin_user() 启动时调用，确保 admin 存在
- get_user_by_username(username) -> User | None
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from pydantic import BaseModel
from sqlalchemy import select

from ..core.config import settings
from ..db.models import User
from ..db.session import get_session_factory

logger = logging.getLogger(__name__)


# =====================================================================
# 密码哈希
# =====================================================================

def hash_password(plain: str) -> str:
    """bcrypt 哈希，返回 utf-8 字符串（含 salt+version+cost）。"""
    if not plain:
        raise ValueError("密码不能为空")
    # bcrypt 限 72 字节，截断（admin/admin 不会触发）
    raw = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    """校验密码；空 hash 或异常都返回 False（不抛错）。"""
    if not plain or not password_hash:
        return False
    try:
        raw = plain.encode("utf-8")[:72]
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# =====================================================================
# JWT
# =====================================================================

def create_access_token(username: str) -> tuple[str, datetime]:
    """
    签发 JWT。返回 (token, expires_at)。
    payload 包含 sub(username)/iat/exp/jti（jti 用于后续黑名单扩展）。
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.JWT_EXP_DAYS)
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_token(token: str) -> Optional[str]:
    """解码并校验 JWT。返回 username，失败返回 None。"""
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        logger.info("[auth] token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.info(f"[auth] invalid token: {e}")
        return None


# =====================================================================
# DB 操作
# =====================================================================

async def get_user_by_username(username: str) -> Optional[User]:
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(User).where(User.username == username)
        return (await session.execute(stmt)).scalar_one_or_none()


async def authenticate(username: str, password: str) -> Optional[User]:
    """用户名 + 密码 → User（校验通过）或 None。"""
    user = await get_user_by_username(username)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def seed_admin_user() -> bool:
    """
    启动时调用：确保 admin 用户存在。
    - 不存在 → 创建（密码来自 SEED_ADMIN_PASS，默认 admin）
    - 已存在 → 跳过（不覆盖密码，避免重启覆盖用户改过的密码）

    返回 True 表示本次新建了 admin，False 表示已存在。
    """
    factory = get_session_factory()
    async with factory() as session:
        existing = await session.get(User, 1) if False else None  # 占位，下面按 username 查
        stmt = select(User).where(User.username == settings.SEED_ADMIN_USER)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            return False

        user = User(
            username=settings.SEED_ADMIN_USER,
            password_hash=hash_password(settings.SEED_ADMIN_PASS),
            is_active=True,
        )
        session.add(user)
        await session.commit()
        logger.warning(
            f"[auth_seed] 已创建默认管理员账号 "
            f"username={settings.SEED_ADMIN_USER!r} "
            f"password={settings.SEED_ADMIN_PASS!r}（请尽快登录修改密码）"
        )
        return True


# =====================================================================
# Response 模型
# =====================================================================

class LoginResp(BaseModel):
    """登录成功响应。"""
    token: str
    token_type: str = "Bearer"
    expires_at: str  # ISO 8601
    user: "UserInfo"


class UserInfo(BaseModel):
    """用户信息（不含密码）。"""
    id: int
    username: str
    is_active: bool
    created_at: str | None = None


# 前向引用修复（LoginResp.user 引用了 UserInfo，但定义顺序在后）
LoginResp.model_rebuild()
