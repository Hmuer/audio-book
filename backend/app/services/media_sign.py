"""
媒体签名 URL（P1 #6）：避免在 URL 里塞完整登录 JWT。

设计：
- 客户端先调 GET /api/media/sign?build_id=...&kind=...&idx=...，服务端校验
  当前用户对资源有读权限后，签发一个**单用途 + 短时 + 资源绑定**的签名 token。
- 客户端再调 GET /api/media/stream?token=<signed>，服务端校验签名 token 后
  返回实际文件（先 302 → 内部 /media 路径或直接 FileResponse）。
- 签名 token 默认 5 分钟过期；jti 一次性使用；后台清理（启动时 + 定期）。

存储：
- SQLite 表 media_sign_tokens（jti PK + payload + expires_at + used_at NULL）
- 启动时删除 expires_at < now() 的；定期清理由 ensure_cleanup_started() 后台任务做。
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..db.models import MediaSignToken

logger = logging.getLogger(__name__)

# 媒体签名 token 默认寿命（5 分钟 —— 足够浏览器发起请求 + 拖动 <audio> 进度）
DEFAULT_TTL_SECONDS = 300

MediaKind = Literal["chapter_mp3", "all_zip"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sign_token(
    jti: str,
    build_id: str,
    kind: str,
    chapter_idx: int | None,
    user_id: int,
    ttl_seconds: int,
) -> tuple[str, datetime]:
    """签发资源绑定的 JWT。"""
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    payload = {
        "jti": jti,
        "sub_kind": "media",
        "build_id": build_id,
        "kind": kind,
        "idx": chapter_idx,
        "uid": user_id,
        "iat": int(_time.time()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


async def issue_media_token(
    session: AsyncSession,
    *,
    build_id: str,
    kind: MediaKind,
    chapter_idx: int | None,
    user_id: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """签发一次性媒体签名 token。返回 dict 含 token / expires_at / url。"""
    from ..db.session import get_session_factory

    jti = uuid.uuid4().hex
    token, expires_at = _sign_token(jti, build_id, kind, chapter_idx, user_id, ttl_seconds)
    factory = get_session_factory()
    async with factory() as s:
        s.add(MediaSignToken(
            jti=jti,
            build_id=build_id,
            kind=kind,
            chapter_idx=chapter_idx,
            user_id=user_id,
            expires_at=expires_at,
            created_at=_now(),
        ))
        await s.commit()
    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "url": f"/api/media/stream?token={token}",
    }


async def consume_media_token(token: str) -> dict[str, Any] | None:
    """校验并"消费"一次性 token：成功返回 payload；过期/已用/签名错都返回 None。

    一次性的实现：成功后立即把 used_at 写上（即使后续因网络问题没下完，也不能再用）。
    """
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if payload.get("sub_kind") != "media":
        return None
    jti = payload.get("jti")
    if not jti:
        return None

    from ..db.session import get_session_factory
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(select(MediaSignToken).where(MediaSignToken.jti == jti))
        ).scalar_one_or_none()
        if not row:
            return None
        if row.used_at is not None:
            return None  # 一次性：已被消费
        if row.expires_at < _now():
            return None
        # 标记已用
        row.used_at = _now()
        await s.commit()
        return {
            "build_id": row.build_id,
            "kind": row.kind,
            "idx": row.chapter_idx,
            "user_id": row.user_id,
            "expires_at": row.expires_at,
        }


async def purge_expired_tokens(session: AsyncSession) -> int:
    """清理过期 + 已用超过 1 小时的 token。返回删除条数。"""
    cutoff = _now() - timedelta(hours=1)
    stmt = delete(MediaSignToken).where(
        (MediaSignToken.expires_at < _now()) | (MediaSignToken.used_at < cutoff)
    )
    res = await session.execute(stmt)
    await session.commit()
    return res.rowcount or 0


# ---- 启动后台清理任务 ----

_cleanup_started = False


async def _cleanup_loop(interval_seconds: int = 600) -> None:
    """每 10 分钟清理一次过期/已用 token。"""
    from ..db.session import get_session_factory
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            factory = get_session_factory()
            async with factory() as s:
                n = await purge_expired_tokens(s)
                if n:
                    logger.info(f"[media_sign] cleanup {n} expired/used tokens")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[media_sign] cleanup loop error: {type(e).__name__}: {e}")


def ensure_cleanup_started() -> None:
    """在 lifespan 启动一次；幂等。"""
    global _cleanup_started
    if _cleanup_started:
        return
    _cleanup_started = True
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_cleanup_loop())
    except RuntimeError:
        # 没运行中的 loop（迁移 / 测试），跳过
        _cleanup_started = False