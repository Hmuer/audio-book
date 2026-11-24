"""AI 前沿日报 API（`/api/daily/*`）。

两类路由：
- `router`：需要登录（复用 `/api/*` 的 JWT 依赖），挂 `/api/daily/*`；
- `public_router`：`/api/daily/feed.xml`，用 `?key=` 鉴权 —— 播客客户端
  无法携带 JWT，RSS 必须能被外部阅读器直接抓取。

音频接口支持 Range（播客客户端会拖进度/分段拉取），Range 已手写实现而不是
依赖 FileResponse 的版本行为。
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..core.config import settings
from ..db.models import User
from ..services.daily import pipeline, store
from ..services.daily.constants import show_config, source_list
from ..services.daily.feed import build_feed, public_base_url
from ..services.daily.store import DailyError
from .routes import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daily", tags=["daily"])
public_router = APIRouter(prefix="/api/daily", tags=["daily-public"])

_CHUNK = 64 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


# =====================================================================
# 请求模型
# =====================================================================

class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=500)


class RunRequest(BaseModel):
    date: str | None = Field(default=None, max_length=10)
    max_items: int | None = Field(default=None, ge=10, le=200)


# =====================================================================
# 配置 / 列表 / 详情
# =====================================================================

@router.get("/config")
async def daily_config(current: User = Depends(get_current_user)) -> dict[str, Any]:
    show = show_config()
    feed_key_set = bool((settings.DAILY_FEED_KEY or "").strip())
    return {
        "show": {**show, "cover_url": None},
        "target_minutes": settings.DAILY_TARGET_MINUTES,
        "sources": source_list(),
        "llm_enabled": bool(settings.DAILY_LLM_ENABLED),
        "tts_voice_id": settings.DAILY_TTS_VOICE_ID,
        "output_dir": str(store.daily_root()),
        "feed_ready": feed_key_set,
        "feed_key_set": feed_key_set,
        "feed_url": "/api/daily/feed.xml",
    }


@router.get("/issues")
async def daily_issues(
    limit: int = Query(default=30, ge=1, le=100),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    items = store.list_issues(limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/issues/{date}")
async def daily_issue_detail(
    date: str,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        detail = store.read_issue(date)
    except DailyError as e:
        raise HTTPException(404, str(e))
    return {
        "issue": detail["issue"],
        "shownotes_md": detail["shownotes_md"],
        "script_md": detail["script_md"],
        "meta": detail["meta"],
        "audio_path": f"/api/daily/issues/{date}/audio",
        "feed_url": "/api/daily/feed.xml",
    }


# =====================================================================
# 音频（Range）
# =====================================================================

def _iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/issues/{date}/audio")
async def daily_issue_audio(
    date: str,
    request: Request,
    current: User = Depends(get_current_user),
):
    try:
        base = store.issue_dir(date)
    except DailyError as e:
        raise HTTPException(400, str(e))
    path = base / store.AUDIO_NAME
    if not path.is_file():
        raise HTTPException(404, "本期还没有音频（可能仍在生成或生成失败）")

    size = path.stat().st_size
    range_header = request.headers.get("range")
    if range_header:
        match = _RANGE_RE.match(range_header)
        if match:
            start = int(match.group(1)) if match.group(1) else 0
            end = int(match.group(2)) if match.group(2) else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                raise HTTPException(
                    status_code=416,
                    detail="Range 越界",
                    headers={"Content-Range": f"bytes */{size}"},
                )
            length = end - start + 1
            return StreamingResponse(
                _iter_file(path, start, length),
                status_code=206,
                media_type="audio/mpeg",
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                },
            )
    return StreamingResponse(
        _iter_file(path, 0, size),
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
    )


# =====================================================================
# 人工终审
# =====================================================================

@router.post("/issues/{date}/review")
async def daily_review(
    date: str,
    req: ReviewRequest,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        meta = store.update_review(
            date,
            decision=req.decision,
            note=req.note,
            reviewed_by=current.username,
        )
    except DailyError as e:
        raise HTTPException(404, str(e))
    return {"issue": store.issue_summary(meta)}


# =====================================================================
# 生成任务
# =====================================================================

@router.post("/run", status_code=202)
async def daily_run(
    req: RunRequest,
    current: User = Depends(get_current_user),
):
    try:
        started = pipeline.start_run(date=req.date, max_items=req.max_items)
    except DailyError as e:
        raise HTTPException(409, str(e))
    logger.info(
        "[daily] %s 触发生成 date=%s run_id=%s",
        current.username, started["date"], started["run_id"],
    )
    return JSONResponse(status_code=202, content=started)


@router.get("/run/status")
async def daily_run_status(current: User = Depends(get_current_user)) -> dict[str, Any]:
    status = store.read_run_status()
    # 进程内已经没有任务、状态却还写着 running（极端情况：后台任务被取消）：
    # 就地纠正，避免 UI 永久转圈。
    if status.get("status") == "running" and not pipeline.is_running():
        store.write_run_status(
            status="failed",
            stage="failed",
            error="生成进程已结束但状态未更新，请重新触发",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        store.append_run_log("[recover] 接口侧纠正残留 running 状态")
        status = store.read_run_status()
    status["is_running"] = pipeline.is_running()
    return {"run": status}


# =====================================================================
# RSS（公开，?key= 鉴权）
# =====================================================================

@public_router.get("/feed.xml")
async def daily_feed(
    request: Request,
    key: str | None = Query(default=None),
) -> Response:
    expected = (settings.DAILY_FEED_KEY or "").strip()
    if not expected:
        raise HTTPException(403, "未配置 DAILY_FEED_KEY，RSS 未开放")
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(401, "feed key 无效")
    issues = store.list_issues(limit=60)
    xml = build_feed(
        issues,
        base_url=public_base_url(request),
        feed_key=expected,
        issue_dir=store.daily_root(),
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")
