"""DSH Agent 桥接 API（`/api/agent/*`）。

定位（详见 README「DSH 创作编排」一节）：

    DSH 插件（创作 + 决策 + 人在环）
        │  仅 HTTP，X-DSH-Token 鉴权
        ▼
    本模块 → 后端服务层（唯一写者：DB / COS / 构建 / 发布）

与 `/api/*` 的区别：

- `/api/*` 面向浏览器，使用用户 JWT（`get_current_user`）；
- `/api/agent/*` 面向本机的 DSH 插件，使用**独立服务 token**
  （`X-DSH-Token`，来自 `settings.DSH_SERVICE_TOKEN`）。
  插件不持有用户密码 / 长时 JWT，也拿不到 S3 / LLM 密钥。

安全约定：

1. `DSH_SERVICE_TOKEN` 留空 → 整个命名空间 503（fail closed，不默认放开）；
2. token 用 `secrets.compare_digest` 比较，避免时序侧信道；
3. 所有响应只含业务字段，绝不回显密钥；
4. 本模块**不**注册进 `router`（那个 router 挂了全局 JWT 依赖）。

P0 范围（本文件首版）：健康检查、书库检索/试读、项目与构建状态、
日志尾部、TTS 报价、创作任务（creation_jobs）与模型调用流水（agent_runs）。
真正的"建项目 / 导入 / 启动构建 / 发布"写接口属于 P1，另行加入。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..core.config import settings
from ..db.models import AgentRun, Build, CreationJob, Project
from ..db.session import get_session_factory
from ..services.library import (
    LibraryError,
    find_book,
    get_catalog,
    read_book_head,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

#: 日志尾部读取窗口（字节）。app.log 单日可达数十 MB，只回读尾部。
_LOG_TAIL_BYTES = 2 * 1024 * 1024

#: creation_jobs 的合法状态（写接口会校验；读接口只透传）
JOB_STATUSES = {
    "draft", "in_review", "approved", "building",
    "ready", "published", "failed", "cancelled", "blocked",
}

#: 预算熔断会改写状态的那些状态（已终态的不动）
_BUDGET_BLOCKABLE = {"draft", "in_review", "approved", "building"}


# =====================================================================
# 鉴权
# =====================================================================

def require_agent_token(
    x_dsh_token: str | None = Header(default=None, alias="X-DSH-Token"),
) -> None:
    """校验插件服务 token。

    - 未配置 `DSH_SERVICE_TOKEN`：503（明确"后端没开这个能力"，而不是 401
      让人误以为 token 错了）；
    - 配置了但请求头缺失/不匹配：401。
    """
    expected = (settings.DSH_SERVICE_TOKEN or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="后端未启用 Agent 桥接：请在配置中设置 DSH_SERVICE_TOKEN",
        )
    if not x_dsh_token or not secrets.compare_digest(x_dsh_token, expected):
        raise HTTPException(status_code=401, detail="X-DSH-Token 无效")


#: 本模块所有路由统一鉴权
router.dependencies = [Depends(require_agent_token)]


# =====================================================================
# 小工具
# =====================================================================

def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _job_public(job: CreationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "content_type": job.content_type,
        "workflow_version": job.workflow_version,
        "stage": job.stage,
        "status": job.status,
        "project_id": job.project_id,
        "title": job.title,
        "payload": _json_loads(job.payload_json, {}),
        "result": _json_loads(job.result_json, {}),
        "budget_cny": round(float(job.budget_cny or 0), 4),
        "spent_cny": round(float(job.spent_cny or 0), 4),
        "error": job.error,
        "created_at": job.created_at.isoformat(timespec="seconds"),
        "updated_at": job.updated_at.isoformat(timespec="seconds"),
    }


def _build_public(b: Build) -> dict[str, Any]:
    return {
        "build_id": b.build_id,
        "project_id": b.project_id,
        "status": b.status,
        "progress_msg": b.progress_msg,
        "completed_chapters": b.completed_chapters or 0,
        "total_chapters": b.total_chapters or 0,
        "failed_chapters": _json_loads(b.failed_chapters_json, []) or [],
        "progress_meta": _json_loads(b.progress_meta_json, {}),
        "tts_calls": b.tts_calls or 0,
        "tts_chars": b.tts_chars or 0,
        "total_duration_ms": b.total_duration_ms,
        "total_size_bytes": b.total_size_bytes,
        "created_at": b.created_at.isoformat(timespec="seconds") if b.created_at else None,
        "started_at": b.started_at.isoformat(timespec="seconds") if b.started_at else None,
        "completed_at": b.completed_at.isoformat(timespec="seconds") if b.completed_at else None,
    }


def _read_log_tail(path: Path, needles: list[str], tail: int) -> list[str]:
    """读 app.log 尾部并按关键字过滤（同步函数，调用方放线程池）。"""
    if not path.is_file():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    hits = [
        ln for ln in lines
        if any(n and n in ln for n in needles)
    ]
    return hits[-tail:]


# =====================================================================
# 健康检查
# =====================================================================

@router.get("/health")
async def agent_health() -> dict[str, Any]:
    """插件启动自检：确认 token 有效、后端可达、书库/存储配置状态。"""
    return {
        "ok": True,
        "service": "audio-book",
        "agent_api_version": 1,
        "server_time": _now_iso(),
        "storage_backend": (settings.STORAGE_BACKEND or "local").lower(),
        "library_prefix": settings.LIBRARY_PREFIX,
        "budget_cny_default": settings.AGENT_TASK_BUDGET_CNY,
        "job_statuses": sorted(JOB_STATUSES),
    }


# =====================================================================
# 书库（选题用）
# =====================================================================

@router.get("/library/search")
async def agent_library_search(
    q: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=20),
    offset: int = Query(default=0),
) -> dict[str, Any]:
    """按分类 + 标题/作者关键字检索书库（内存过滤，毫秒级）。

    返回压缩后的书目字段：Agent 选题只需要元信息，不需要全书正文。
    """
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    try:
        cat = await get_catalog()
    except LibraryError as e:
        raise HTTPException(503, str(e))
    total, books = cat.query(category=category, q=q, limit=limit, offset=offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "categories": [
            {"name": c.get("name"), "count": c.get("count")}
            for c in (cat.categories or [])
            if c.get("name")
        ],
        "items": [b.to_public() for b in books],
    }


@router.get("/library/books/{uid}")
async def agent_library_book(
    uid: str,
    excerpt_chars: int = Query(default=600, description="试读字符数；0 表示不试读"),
) -> dict[str, Any]:
    """单本书详情 + 开头试读（Range 读取，不下载整本）。"""
    try:
        book = await find_book(uid)
    except LibraryError as e:
        raise HTTPException(404, str(e))

    excerpt: dict[str, Any] = {"ok": False, "reason": "未请求试读"}
    if excerpt_chars and int(excerpt_chars) > 0:
        try:
            excerpt = await read_book_head(book, chars=int(excerpt_chars))
        except Exception as e:  # 对象存储抖动不应让元信息也拿不到
            logger.warning("[agent] 试读失败 uid=%s: %s: %s", uid, type(e).__name__, e)
            excerpt = {"ok": False, "reason": f"试读失败: {type(e).__name__}"}

    return {
        "book": book.to_public(),
        "excerpt": excerpt,
    }


# =====================================================================
# 项目 / 构建状态（长任务观查）
# =====================================================================

@router.get("/projects/{project_id}/status")
async def agent_project_status(project_id: str) -> dict[str, Any]:
    """项目当前状态 + 最近若干次构建（Agent 轮询构建进度用）。"""
    factory = get_session_factory()
    async with factory() as s:
        proj = await s.get(Project, project_id)
        if proj is None:
            raise HTTPException(404, f"项目不存在：{project_id}")
        builds = (
            await s.execute(
                select(Build)
                .where(Build.project_id == project_id)
                .order_by(Build.created_at.desc())
                .limit(3)
            )
        ).scalars().all()

    latest = _build_public(builds[0]) if builds else None
    return {
        "project_id": proj.project_id,
        "name": proj.name,
        "status": proj.status,
        "book_title": proj.book_title,
        "book_category": proj.book_category,
        "chapter_count": proj.chapter_count or 0,
        "selling_points": proj.selling_points,
        "cover_status": proj.cover_status,
        "latest_build": latest,
        "recent_builds": [_build_public(b) for b in builds],
        "terminal": proj.status in {"ready", "done", "failed"},
    }


@router.get("/builds/{build_id}")
async def agent_build_status(build_id: str) -> dict[str, Any]:
    """按 build_id 直接查构建状态（插件可能只保存了 build_id）。"""
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, build_id)
        if b is None:
            raise HTTPException(404, f"构建不存在：{build_id}")
        return _build_public(b)


@router.get("/projects/{project_id}/logs")
async def agent_project_logs(
    project_id: str,
    tail: int = Query(default=200),
) -> dict[str, Any]:
    """回读 app.log 尾部中与该 project 相关的日志行（排查构建失败）。

    key 匹配用 project_id 全串和它的前 8 位 —— 日志里常见的写法是
    `project_id=abcd1234...`（截断展示）。
    """
    tail = max(1, min(int(tail), 1000))
    path = Path(settings.LOG_FILE) if settings.LOG_FILE else None
    if path is None:
        return {"lines": [], "reason": "未启用文件日志（LOG_FILE 为空）"}
    needles = [project_id, project_id[:8]]
    lines = await asyncio.to_thread(_read_log_tail, path, needles, tail)
    return {"lines": lines, "count": len(lines), "log_file": str(path)}


# =====================================================================
# TTS 报价（成本预估，写文案时给 Agent 参考）
# =====================================================================

@router.get("/pricing/quote")
async def agent_pricing_quote(
    chars: int = Query(..., ge=0, le=100_000_000),
) -> dict[str, Any]:
    """按字符数估算 TTS 成本（元）。

    仅用于**预估展示**：真实用量由各厂商返回的 usage 决定，与
    `builds.tts_calls / tts_chars` 对账。价格来自
    `settings.DOUBAO_TTS_PRICE_PER_CHAR`（设置页常量，非计费口径）。
    """
    unit = float(settings.DOUBAO_TTS_PRICE_PER_CHAR or 0)
    return {
        "chars": chars,
        "price_per_char": unit,
        "tts_cost_cny": round(chars * unit, 4),
        "basis": "字符版语音合成预估，不含 LLM / 封面 / 存储",
        "disclaimer": "预估值，实际以厂商账单为准",
    }


# =====================================================================
# 创作任务（creation_jobs）+ 模型调用流水（agent_runs）
# =====================================================================

class AgentJobCreate(BaseModel):
    """创建一个创作任务；带 idempotency_key 时重复提交返回同一 job。"""

    content_type: str = Field(default="audiobook", max_length=32)
    title: str = Field(default="", max_length=256)
    project_id: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] | None = None
    budget_cny: float | None = Field(default=None, ge=0, le=1000)


class AgentRunCreate(BaseModel):
    """上报一次模型调用（DSH 编排器调用后写审计流水）。"""

    job_id: str | None = Field(default=None, max_length=64)
    role: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)
    prompt_version: str = Field(default="", max_length=64)
    input_hash: str = Field(default="", max_length=64)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_cny: float = Field(default=0.0, ge=0)
    status: str = Field(default="ok", max_length=16)
    output: Any | None = None


@router.post("/jobs")
async def agent_create_job(req: AgentJobCreate) -> dict[str, Any]:
    """创建创作任务（幂等）。"""
    factory = get_session_factory()
    key = (req.idempotency_key or "").strip() or None

    async with factory() as s:
        if key:
            existing = (
                await s.execute(
                    select(CreationJob).where(CreationJob.idempotency_key == key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"created": False, "job": _job_public(existing)}

        if req.project_id:
            proj = await s.get(Project, req.project_id)
            if proj is None:
                raise HTTPException(404, f"项目不存在：{req.project_id}")

        budget = (
            float(req.budget_cny)
            if req.budget_cny is not None
            else float(settings.AGENT_TASK_BUDGET_CNY or 0)
        )
        job = CreationJob(
            job_id=f"job_{secrets.token_hex(8)}",
            content_type=req.content_type or "audiobook",
            title=req.title or "",
            project_id=req.project_id,
            idempotency_key=key,
            payload_json=_json_dumps(req.payload or {}),
            budget_cny=budget,
            spent_cny=0.0,
        )
        s.add(job)
        try:
            await s.commit()
        except IntegrityError:
            # 并发重放：另一个请求刚插入了同一个 idempotency_key
            await s.rollback()
            if not key:
                raise HTTPException(500, "创建任务失败：数据库冲突")
            existing = (
                await s.execute(
                    select(CreationJob).where(CreationJob.idempotency_key == key)
                )
            ).scalar_one_or_none()
            if existing is None:
                raise HTTPException(500, "创建任务失败：幂等键冲突但查不到记录")
            return {"created": False, "job": _job_public(existing)}

    return {"created": True, "job": _job_public(job)}


@router.get("/jobs")
async def agent_list_jobs(
    status: str | None = Query(default=None),
    content_type: str | None = Query(default=None),
    limit: int = Query(default=20),
    offset: int = Query(default=0),
) -> dict[str, Any]:
    """列出创作任务（按创建时间倒序）。"""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    if status and status not in JOB_STATUSES:
        raise HTTPException(400, f"未知状态：{status}")

    factory = get_session_factory()
    async with factory() as s:
        stmt = select(CreationJob)
        if status:
            stmt = stmt.where(CreationJob.status == status)
        if content_type:
            stmt = stmt.where(CreationJob.content_type == content_type)
        total = int(
            (
                await s.execute(
                    select(func.count()).select_from(stmt.subquery())
                )
            ).scalar_one()
        )
        rows = (
            await s.execute(
                stmt.order_by(CreationJob.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()

    return {
        "items": [_job_public(j) for j in rows],
        "limit": limit,
        "offset": offset,
        "count": len(rows),
        "total": total,
    }


@router.get("/jobs/{job_id}")
async def agent_get_job(job_id: str) -> dict[str, Any]:
    """任务详情（含全部模型调用流水汇总）。"""
    factory = get_session_factory()
    async with factory() as s:
        job = await s.get(CreationJob, job_id)
        if job is None:
            raise HTTPException(404, f"任务不存在：{job_id}")
        runs = (
            await s.execute(
                select(AgentRun)
                .where(AgentRun.job_id == job_id)
                .order_by(AgentRun.created_at.asc())
            )
        ).scalars().all()

    return {
        "job": _job_public(job),
        "runs": [_run_public(r) for r in runs],
        "spent_cny": round(sum(float(r.cost_cny or 0) for r in runs), 4),
    }


def _run_public(r: AgentRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "job_id": r.job_id,
        "role": r.role,
        "model": r.model,
        "prompt_version": r.prompt_version,
        "input_hash": r.input_hash,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
        "cost_cny": round(float(r.cost_cny or 0), 6),
        "status": r.status,
        "output": _json_loads(r.output_json, None),
        "created_at": r.created_at.isoformat(timespec="seconds"),
    }


@router.post("/runs")
async def agent_create_run(req: AgentRunCreate) -> dict[str, Any]:
    """记录一次模型调用，并累加任务成本；超预算时把任务置 blocked。"""
    factory = get_session_factory()
    blocked = False

    async with factory() as s:
        job: CreationJob | None = None
        if req.job_id:
            job = await s.get(CreationJob, req.job_id)
            if job is None:
                raise HTTPException(404, f"任务不存在：{req.job_id}")

        run = AgentRun(
            job_id=req.job_id,
            role=req.role,
            model=req.model,
            prompt_version=req.prompt_version,
            input_hash=req.input_hash,
            tokens_in=req.tokens_in,
            tokens_out=req.tokens_out,
            cost_cny=float(req.cost_cny or 0),
            status=req.status or "ok",
            output_json=_json_dumps(req.output) if req.output is not None else None,
        )
        s.add(run)

        if job is not None:
            job.spent_cny = float(job.spent_cny or 0) + float(req.cost_cny or 0)
            budget = float(job.budget_cny or 0)
            if (
                budget > 0
                and job.spent_cny > budget
                and job.status in _BUDGET_BLOCKABLE
            ):
                job.status = "blocked"
                job.error = (
                    f"超出单任务预算：已花 ¥{job.spent_cny:.4f} > 上限 ¥{budget:.4f}"
                )
                blocked = True

        await s.commit()

    return {
        "recorded": True,
        "run_id": run.id,
        "job_blocked": blocked,
    }


@router.get("/jobs/{job_id}/runs")
async def agent_list_runs(
    job_id: str,
    limit: int = Query(default=200),
) -> dict[str, Any]:
    """任务的全部模型调用流水（审计 / 成本对账 / 复现）。"""
    limit = max(1, min(int(limit), 1000))
    factory = get_session_factory()
    async with factory() as s:
        job = await s.get(CreationJob, job_id)
        if job is None:
            raise HTTPException(404, f"任务不存在：{job_id}")
        rows = (
            await s.execute(
                select(AgentRun)
                .where(AgentRun.job_id == job_id)
                .order_by(AgentRun.created_at.asc())
                .limit(limit)
            )
        ).scalars().all()

    return {
        "job_id": job_id,
        "items": [_run_public(r) for r in rows],
        "count": len(rows),
        "spent_cny": round(sum(float(r.cost_cny or 0) for r in rows), 6),
    }
