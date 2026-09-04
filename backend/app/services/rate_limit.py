"""
P1 #9 速率限制（内存版 token-bucket）。

目标：
- /api/auth/login：每个 IP 5/min，防止口令爆破；
- /api/projects/{id}/prepare：每个 (user_id, project_id) 3/min，防止反复触发；
- /api/projects/{id}/builds 与 /retry-failed：每个 (user_id, project_id) 3/min，防止反复启动后台任务。

为什么不引入 Redis：单进程内存即可；多 worker 进程下每个进程有自己计数（粗略但仍能挡住单 IP 高频）。
生产环境多副本时建议迁移到 Redis（保留接口不变）。

实现：固定窗口 + 滑动计数（用 deque）。
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import defaultdict, deque
from typing import Deque

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# 计数器存储
# ---------------------------------------------------------------------

def _recent_events_factory() -> Deque[float]:
    """测试用：defaultdict 的 default_factory，返回空 deque。"""
    return deque()


# 每条 key 对应一个时间戳 deque（最近 N 次事件）
_recent_events: dict[str, Deque[float]] = defaultdict(_recent_events_factory)
_events_lock = asyncio.Lock()


async def _check_and_record(
    key: str,
    *,
    window_seconds: float,
    max_count: int,
) -> tuple[bool, int]:
    """
    原子检查 + 记录：
      - 裁剪掉 window_seconds 之前的事件
      - 当前计数 >= max_count → 拒绝
      - 否则记录一个新事件，返回 (允许, 当前计数+1)
    """
    now = _time.monotonic()
    cutoff = now - window_seconds
    async with _events_lock:
        dq = _recent_events[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_count:
            return False, len(dq)
        dq.append(now)
        return True, len(dq)


# ---------------------------------------------------------------------
# 业务级 helper
# ---------------------------------------------------------------------


def _client_ip(request) -> str:
    """优先 X-Forwarded-For（反向代理），否则 request.client.host。"""
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if fwd:
        # 多层代理只取第一个
        return fwd.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def enforce_login_rate_limit(request) -> None:
    """登录限流：每个 IP 5 次 / 分钟。"""
    ip = _client_ip(request)
    key = f"login:{ip}"
    allowed, count = await _check_and_record(
        key, window_seconds=60.0, max_count=5,
    )
    if not allowed:
        logger.warning(f"[rate_limit] login IP={ip} count={count} → 429")
        from fastapi import HTTPException
        raise HTTPException(
            status_code=429,
            detail="登录尝试过于频繁，请稍后再试（每分钟每 IP 最多 5 次）。",
            headers={"Retry-After": "60"},
        )


async def enforce_prepare_rate_limit(request, *, user_id: int, project_id: str) -> None:
    """prepare 限流：每 (user, project) 3 次 / 分钟。"""
    key = f"prepare:{user_id}:{project_id}"
    allowed, count = await _check_and_record(
        key, window_seconds=60.0, max_count=3,
    )
    if not allowed:
        logger.warning(
            f"[rate_limit] prepare user={user_id} project={project_id[:8]}... "
            f"count={count} → 429"
        )
        from fastapi import HTTPException
        raise HTTPException(
            status_code=429,
            detail="识别触发过于频繁，请稍后再试（每分钟每个项目最多 3 次）。",
            headers={"Retry-After": "60"},
        )


async def enforce_build_rate_limit(request, *, user_id: int, project_id: str) -> None:
    """build 限流：每 (user, project) 3 次 / 分钟（start_build + retry 共用）。"""
    key = f"build:{user_id}:{project_id}"
    allowed, count = await _check_and_record(
        key, window_seconds=60.0, max_count=3,
    )
    if not allowed:
        logger.warning(
            f"[rate_limit] build user={user_id} project={project_id[:8]}... "
            f"count={count} → 429"
        )
        from fastapi import HTTPException
        raise HTTPException(
            status_code=429,
            detail="合成启动过于频繁，请稍后再试（每分钟每个项目最多 3 次）。",
            headers={"Retry-After": "60"},
        )


# 测试 / 调试 hook：清空所有计数
def reset_all() -> None:
    _recent_events.clear()