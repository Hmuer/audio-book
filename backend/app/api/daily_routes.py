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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ..core.config import settings
from ..services.daily import pipeline, prefs, store
from ..services.daily.constants import show_config
from ..services.daily.feed import build_feed, public_base_url
from ..services.daily.store import DailyError
from .routes import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daily", tags=["daily"])
public_router = APIRouter(prefix="/api/daily", tags=["daily-public"])

_CHUNK = 64 * 1024
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

_bearer = HTTPBearer(auto_error=False)


async def daily_operator(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(default=None),
    x_dsh_token: str | None = Header(default=None, alias="X-DSH-Token"),
) -> str:
    """两种身份都允许：

    - 登录用户（前端）：`Authorization: Bearer <JWT>`；
    - 本机服务（定时任务 / DSH 插件）：`X-DSH-Token` = `DSH_SERVICE_TOKEN`。

    返回操作者标识（写进审核记录 / 日志）。
    """
    expected = (settings.DSH_SERVICE_TOKEN or "").strip()
    if expected and x_dsh_token and secrets.compare_digest(x_dsh_token, expected):
        return "service"
    user = await get_current_user(creds=creds, token=token)
    return user.username


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

class DailySourceIn(BaseModel):
    name: str = Field(default="", max_length=40)
    url: str = Field(..., max_length=500)
    enabled: bool = True


class DailyConfigUpdate(BaseModel):
    """页面「生成设置」提交体；只传需要修改的字段。"""

    voice_id: str | None = Field(default=None, max_length=200)
    sources: list[DailySourceIn] | None = None


async def _config_payload() -> dict[str, Any]:
    prefs_doc = prefs.load()
    voices = await prefs.available_voices()
    voice_id = prefs.resolve_voice_id(voices)
    status = store.read_run_status()
    feed_key_set = bool((settings.DAILY_FEED_KEY or "").strip())
    return {
        "show": {**show_config(), "cover_url": None},
        "target_minutes": settings.DAILY_TARGET_MINUTES,
        "sources": prefs.effective_sources(prefs_doc),
        "llm_enabled": bool(settings.DAILY_LLM_ENABLED),
        # 配置默认值（可为空）与"最终生效"分开暴露：前端下拉直接绑 voice_id
        "tts_voice_id": settings.DAILY_TTS_VOICE_ID,
        "voice_id": voice_id,
        "voice_name": prefs.voice_name_of(voices, voice_id),
        "voices": voices,
        "output_dir": str(store.daily_root()),
        "feed_ready": feed_key_set,
        "feed_key_set": feed_key_set,
        "feed_url": "/api/daily/feed.xml",
        "circuit": {
            "open": bool(status.get("circuit_open")),
            "failures": int(status.get("consecutive_failures") or 0),
        },
    }


@router.get("/config")
async def daily_config(operator: str = Depends(daily_operator)) -> dict[str, Any]:
    return await _config_payload()


@router.put("/config")
async def daily_config_update(
    req: DailyConfigUpdate,
    operator: str = Depends(daily_operator),
) -> dict[str, Any]:
    """页面切换音色 / 启停信源 / 增删自定义源。"""
    updates: dict[str, Any] = {}
    fields = req.model_fields_set

    if "voice_id" in fields:
        voice_id = (req.voice_id or "").strip()
        if voice_id:
            voices = await prefs.available_voices()
            if voices and voice_id not in {v["id"] for v in voices}:
                raise HTTPException(400, f"音色不存在或不属于当前 TTS 厂商：{voice_id}")
        updates["voice_id"] = voice_id  # 空串 = 恢复自动挑选

    if req.sources is not None:
        try:
            updates["sources"] = prefs.normalize_sources(
                [s.model_dump() for s in req.sources]
            )
        except DailyError as e:
            raise HTTPException(400, str(e))

    if not updates:
        return await _config_payload()

    prefs.save(updates)
    logger.info(
        "[daily] %s 更新日报设置 keys=%s", operator, ",".join(sorted(updates))
    )
    return await _config_payload()


@router.get("/issues")
async def daily_issues(
    limit: int = Query(default=30, ge=1, le=100),
    operator: str = Depends(daily_operator),
) -> dict[str, Any]:
    items = store.list_issues(limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/issues/{date}")
async def daily_issue_detail(
    date: str,
    operator: str = Depends(daily_operator),
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
    operator: str = Depends(daily_operator),
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
    operator: str = Depends(daily_operator),
) -> dict[str, Any]:
    try:
        meta = store.update_review(
            date,
            decision=req.decision,
            note=req.note,
            reviewed_by=operator,
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
    operator: str = Depends(daily_operator),
):
    try:
        started = pipeline.start_run(date=req.date, max_items=req.max_items)
    except DailyError as e:
        raise HTTPException(409, str(e))
    logger.info(
        "[daily] %s 触发生成 date=%s run_id=%s",
        operator, started["date"], started["run_id"],
    )
    return JSONResponse(status_code=202, content=started)


@router.get("/run/status")
async def daily_run_status(operator: str = Depends(daily_operator)) -> dict[str, Any]:
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


@router.post("/reset-circuit")
async def daily_reset_circuit(operator: str = Depends(daily_operator)) -> dict[str, Any]:
    """人工重置熔断（连续失败自动停更后的恢复入口）。"""
    if pipeline.is_running():
        raise HTTPException(409, "生成进行中，请等本期结束后再重置")
    store.write_run_status(
        status="idle", stage=None, progress=0, error=None,
        circuit_open=False, consecutive_failures=0,
    )
    store.append_run_log(f"[reset] {operator} 重置熔断")
    status = store.read_run_status()
    status["is_running"] = pipeline.is_running()
    return {"ok": True, "run": status}


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
