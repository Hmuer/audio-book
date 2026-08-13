from __future__ import annotations
import logging
import time as _time
import urllib.parse
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session_factory
from ..db.models import Job, ChapterResult, Build, BuildArtifact, Project
from ..ai.factory import get_tts
from ..core.config import settings
from ..services.chapter import (
    prepare_chapter,
    synthesize_chapter,
    PrepareResponse,
    SynthesizeResponse,
)
from ..services.book import (
    upload_book,
    prepare_book,
    synthesize_book,
    start_synthesize_book_background,
    get_book_status,
    BookPrepareResponse,
    BookStatusResponse,
    BookSynthResponse,
)
from ..services.project import (
    create_project,
    import_file as project_import_file,
    prepare_project,
    get_project,
    list_projects,
    update_project,
    delete_project,
    get_project_chapters,
    get_project_characters,
    update_character_voice,
    ProjectResp,
    ProjectDetailResp,
    ProjectListItem,
    ProjectPrepareResp,
    ChapterSummary,
    CharacterWithVoice,
    CharacterResp,
)
from ..services.build import (
    start_build,
    get_build,
    list_builds,
    get_build_status,
    delete_build,
    BuildResp,
    BuildDetailResp,
    BuildListItem,
    BuildStatusResp,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["novel-tts"])


async def get_db() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as s:
        yield s


# ---------- Requests ----------

class PrepareRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=50000)
    enable_polish: bool = True


class SynthesizeRequest(BaseModel):
    job_id: str
    voice_assignments: dict[str, str] = Field(default_factory=dict)
    narrator_voice_id: str
    segment_overrides: dict[int, str] | None = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class TtsPreviewRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    voice_id: str
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


# ---------- Health & Voices ----------

@router.get("/health")
async def health():
    return {"status": "ok", "service": "novel-tts"}


@router.get("/voices")
async def list_voices():
    tts = get_tts()
    voices = await tts.list_voices()
    return {"voices": voices, "count": len(voices)}


# ---------- Chapter prepare / synthesize ----------

@router.post("/chapter/prepare", response_model=PrepareResponse)
async def api_prepare(
    req: PrepareRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    text_len = len(req.text)
    logger.info(
        f"[HTTP] POST /api/chapter/prepare client={remote} "
        f"text_len={text_len} enable_polish={req.enable_polish}"
    )
    try:
        resp = await prepare_chapter(
            session=db,
            raw_text=req.text,
            enable_polish=req.enable_polish,
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/chapter/prepare job_id={resp.job_id[:8]}... "
            f"chapters={len(resp.chapters)} chars={len(resp.polished_text)} total_ms={elapsed_ms}"
        )
        return resp
    except Exception as e:
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.error(
            f"[HTTP] 500 /api/chapter/prepare client={remote} total_ms={elapsed_ms} "
            f"text_len={text_len} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"prepare 失败: {type(e).__name__}: {e}")


@router.post("/chapter/synthesize", response_model=SynthesizeResponse)
async def api_synthesize(
    req: SynthesizeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/chapter/synthesize client={remote} job_id={req.job_id[:8]}... "
        f"voices={len(req.voice_assignments)} narrator={req.narrator_voice_id} "
        f"overrides={len(req.segment_overrides or {})} speed={req.speed}"
    )
    try:
        resp = await synthesize_chapter(
            session=db,
            job_id=req.job_id,
            voice_assignments=req.voice_assignments,
            narrator_voice_id=req.narrator_voice_id,
            segment_overrides=req.segment_overrides,
            speed=req.speed,
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/chapter/synthesize job_id={req.job_id[:8]}... "
            f"duration_s={resp.duration_sec} segments={len(resp.segments)} total_ms={elapsed_ms}"
        )
        return resp
    except Exception as e:
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.error(
            f"[HTTP] 500 /api/chapter/synthesize client={remote} "
            f"job_id={req.job_id[:8]}... total_ms={elapsed_ms} -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"synthesize 失败: {type(e).__name__}: {e}")


@router.post("/tts/preview")
async def api_tts_preview(
    req: TtsPreviewRequest,
    request: Request,
):
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/tts/preview client={remote} "
        f"voice={req.voice_id} text_len={len(req.text)} speed={req.speed}"
    )
    try:
        tts = get_tts()
        audio_dir = Path(settings.AUDIO_DIR)
        audio_dir.mkdir(parents=True, exist_ok=True)
        import uuid as _uuid
        fname = f"preview_{_uuid.uuid4().hex[:10]}.mp3"
        fpath = str(audio_dir / fname)
        path, dur_ms = await tts.synthesize_to_file(
            req.text, req.voice_id, fpath, speed=req.speed
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/tts/preview voice={req.voice_id} "
            f"dur_ms={dur_ms} total_ms={elapsed_ms}"
        )
        return {
            "audio_filename": fname,
            "audio_url": f"/media/{fname}",
            "duration_ms": dur_ms,
        }
    except Exception as e:
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.error(
            f"[HTTP] 500 /api/tts/preview client={remote} "
            f"voice={req.voice_id} total_ms={elapsed_ms} -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"TTS 失败: {type(e).__name__}: {e}")


# ---------- Book (整本小说) ----------

class BookPrepareApiRequest(BaseModel):
    file_id: str
    filename: str = ""


class BookSynthesizeRequest(BaseModel):
    job_id: str
    voice_assignments: dict[str, str] = Field(default_factory=dict)
    narrator_voice_id: str
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@router.post("/book/upload")
async def api_book_upload(
    request: Request,
    file: UploadFile = File(...),
):
    """
    上传整本小说 TXT 文件，返回 file_id。
    后续用 /api/book/prepare 触发 prepare。
    """
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    # 限制 50MB（整本小说 txt 足够）
    MAX_SIZE = 50 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, f"文件过大: {len(content)} > {MAX_SIZE}")
    if not file.filename or not file.filename.lower().endswith((".txt", ".text", ".md")):
        # 不强制终止，但记录警告
        logger.warning(
            f"[HTTP] POST /api/book/upload client={remote} "
            f"unexpected filename={file.filename}"
        )
    logger.info(
        f"[HTTP] POST /api/book/upload client={remote} "
        f"filename={file.filename} size={len(content)}"
    )
    try:
        file_id, saved_path = await upload_book(content, file.filename or "book.txt")
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/book/upload file_id={file_id} total_ms={elapsed_ms}"
        )
        return {"file_id": file_id, "filename": file.filename, "size": len(content)}
    except Exception as e:
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.error(
            f"[HTTP] 500 /api/book/upload client={remote} total_ms={elapsed_ms} "
            f"-> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"上传失败: {type(e).__name__}: {e}")


@router.post("/book/prepare", response_model=BookPrepareResponse)
async def api_book_prepare(
    req: BookPrepareApiRequest,
    request: Request,
):
    """整本 prepare：识别章节 + 角色 + 对白归属 + 音色推荐。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/book/prepare client={remote} file_id={req.file_id} filename={req.filename}"
    )
    try:
        resp = await prepare_book(req.file_id, original_filename=req.filename)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/book/prepare job_id={resp.job_id[:8]}... "
            f"chapters={resp.total_chapters} chars={sum(c['text_len'] for c in resp.chapters)} "
            f"total_ms={elapsed_ms}"
        )
        return resp
    except Exception as e:
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.error(
            f"[HTTP] 500 /api/book/prepare client={remote} file_id={req.file_id} "
            f"total_ms={elapsed_ms} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"book prepare 失败: {type(e).__name__}: {e}")


@router.post("/book/synthesize", response_model=BookStatusResponse)
async def api_book_synthesize(
    req: BookSynthesizeRequest,
    request: Request,
):
    """
    启动整本合成（后台任务，立即返回）。
    调用后由前端轮询 GET /api/book/{job_id}/status 获取进度。
    """
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/book/synthesize client={remote} job_id={req.job_id[:8]}... "
        f"voices={len(req.voice_assignments)} narrator={req.narrator_voice_id} "
        f"speed={req.speed}"
    )
    try:
        resp = await start_synthesize_book_background(
            job_id=req.job_id,
            voice_assignments=req.voice_assignments,
            narrator_voice_id=req.narrator_voice_id,
            speed=req.speed,
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/book/synthesize job_id={req.job_id[:8]}... "
            f"status={resp.book_status} total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.error(
            f"[HTTP] 500 /api/book/synthesize client={remote} "
            f"job_id={req.job_id[:8]}... total_ms={elapsed_ms} -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"book synthesize 失败: {type(e).__name__}: {e}")


@router.get("/book/{job_id}/status", response_model=BookStatusResponse)
async def api_book_status(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """查询整本进度（前端轮询）。"""
    try:
        resp = await get_book_status(db, job_id)
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/book/{job_id}/status -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"status 查询失败: {type(e).__name__}: {e}")


def _safe_download_name(book_title: str | None, job_id: str, ext: str) -> str:
    """构造下载文件名（中文 + fallback 安全），ext 带点，例如 '.zip' / '.mp3'。"""
    base = (book_title or "").strip() or f"小说_{job_id[:8]}"
    # 去掉 Windows/Mac 都不允许的字符
    for ch in '\\/:*?"<>|\r\n\t':
        base = base.replace(ch, "_")
    base = base[:100].strip() or f"小说_{job_id[:8]}"
    return f"{base}{ext}"


@router.get("/book/{job_id}/download-all")
async def api_book_download_all(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """一键全部下载：返回打包好的 ZIP（中文文件名）。"""
    job = (await db.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "job not found")
    if not job.zip_filename:
        raise HTTPException(400, "整包 ZIP 尚未生成，请先等合成完成")
    audio_dir = Path(settings.AUDIO_DIR)
    zip_path = audio_dir / job.zip_filename
    if not zip_path.is_file():
        raise HTTPException(404, "ZIP 文件不存在")
    download_name = _safe_download_name(job.book_title, job_id, ".zip")
    # Content-Disposition 同时提供 ASCII fallback + UTF-8 编码，确保 Safari/Chrome 中文
    ascii_name = urllib.parse.quote(download_name.encode("utf-8"), safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{ascii_name}"
    }
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        headers=headers,
        filename=download_name,  # FastAPI 自身也会写 Content-Disposition
    )


@router.get("/book/{job_id}/chapters/{idx}/download")
async def api_book_chapter_download(
    job_id: str,
    idx: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """单章下载：返回 FileResponse，强制浏览器保存为中文文件名。"""
    job = (await db.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "job not found")
    cr = (
        await db.execute(
            select(ChapterResult).where(
                ChapterResult.job_id == job_id,
                ChapterResult.chapter_idx == idx,
            )
        )
    ).scalar_one_or_none()
    if not cr or not cr.audio_filename:
        raise HTTPException(404, f"章节 {idx} 尚未生成")
    audio_dir = Path(settings.AUDIO_DIR)
    fpath = audio_dir / cr.audio_filename
    if not fpath.is_file():
        raise HTTPException(404, f"章节 {idx} 音频文件不存在")
    fname = f"第{idx+1:03d}章 {cr.title or '章节'}.mp3"
    for ch in '\\/:*?"<>|\r\n\t':
        fname = fname.replace(ch, "_")
    ascii_name = urllib.parse.quote(fname.encode("utf-8"), safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{ascii_name}"
    }
    return FileResponse(
        path=str(fpath),
        media_type="audio/mpeg",
        headers=headers,
        filename=fname,
    )


# =====================================================================
# 项目制 API（新架构：Project → Build → BuildArtifact）
# 注意：旧 /api/book/* 和 /api/chapter/* 路由保留不动，仍兼容单章模式
# =====================================================================


# ---------- Requests ----------

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, max_length=256)
    description: str | None = None
    tags: str | None = Field(default=None, max_length=256)
    default_narrator_voice_id: str | None = None
    default_speed: float | None = Field(default=None, ge=0.5, le=2.0)
    cover_color: str | None = None


class UpdateCharacterVoiceRequest(BaseModel):
    voice_id: str | None = None  # None 表示清除


class StartBuildRequest(BaseModel):
    voice_assignments: dict[str, str] = Field(default_factory=dict)
    narrator_voice_id: str = ""
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


def _safe_download_name_build(book_title: str | None, build_id: str, ext: str) -> str:
    """构造 build 下载文件名（中文 + fallback 安全），ext 带点。"""
    base = (book_title or "").strip() or f"有声书_{build_id[:8]}"
    for ch in '\\/:*?"<>|\r\n\t':
        base = base.replace(ch, "_")
    base = base[:100].strip() or f"有声书_{build_id[:8]}"
    return f"{base}{ext}"


# ---------- 项目 CRUD ----------

@router.post("/projects", response_model=ProjectResp)
async def api_create_project(req: CreateProjectRequest, request: Request):
    """创建项目（status=draft）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(f"[HTTP] POST /api/projects client={remote} name={req.name!r}")
    try:
        resp = await create_project(req.name)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects project_id={resp.project_id[:8]}... total_ms={elapsed_ms}"
        )
        return resp
    except Exception as e:
        logger.error(f"[HTTP] 500 /api/projects -> {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(500, f"创建项目失败: {type(e).__name__}: {e}")


@router.get("/projects", response_model=list[ProjectListItem])
async def api_list_projects(request: Request):
    """项目列表。"""
    try:
        return await list_projects()
    except Exception as e:
        logger.error(f"[HTTP] 500 /api/projects -> {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(500, f"列表查询失败: {type(e).__name__}: {e}")


@router.get("/projects/{project_id}", response_model=ProjectDetailResp)
async def api_get_project(project_id: str, request: Request):
    """项目详情（含 chapters 摘要 + characters + 最近 build）。"""
    try:
        return await get_project(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"详情查询失败: {type(e).__name__}: {e}")


@router.patch("/projects/{project_id}", response_model=ProjectResp)
async def api_update_project(
    project_id: str,
    req: UpdateProjectRequest,
    request: Request,
):
    """更新项目名称/备注/标签/配置。"""
    try:
        return await update_project(
            project_id,
            name=req.name,
            description=req.description,
            tags=req.tags,
            default_narrator_voice_id=req.default_narrator_voice_id,
            default_speed=req.default_speed,
            cover_color=req.cover_color,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 PATCH /api/projects/{project_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"更新失败: {type(e).__name__}: {e}")


@router.delete("/projects/{project_id}")
async def api_delete_project(project_id: str, request: Request):
    """删除项目（级联删除 DB + 磁盘文件）。"""
    try:
        await delete_project(project_id)
        return {"ok": True, "project_id": project_id}
    except Exception as e:
        logger.error(
            f"[HTTP] 500 DELETE /api/projects/{project_id} -> {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"删除失败: {type(e).__name__}: {e}")


# ---------- 文件导入 + 识别 ----------

@router.post("/projects/{project_id}/import", response_model=ProjectResp)
async def api_project_import(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
):
    """上传/替换项目源文件（multipart/form-data）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    MAX_SIZE = 50 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, f"文件过大: {len(content)} > {MAX_SIZE}")
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../import "
        f"client={remote} filename={file.filename} size={len(content)}"
    )
    try:
        resp = await project_import_file(project_id, content, file.filename or "book.txt")
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects/{project_id[:8]}.../import total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../import -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"导入失败: {type(e).__name__}: {e}")


@router.post("/projects/{project_id}/prepare", response_model=ProjectPrepareResp)
async def api_project_prepare(project_id: str, request: Request):
    """触发识别：章节 → 角色 → 对白 → 音色 → 落库。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../prepare client={remote}"
    )
    try:
        resp = await prepare_project(project_id)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects/{project_id[:8]}.../prepare "
            f"chapters={resp.total_chapters} total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../prepare -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"prepare 失败: {type(e).__name__}: {e}")


@router.get("/projects/{project_id}/chapters", response_model=list[ChapterSummary])
async def api_project_chapters(project_id: str, request: Request):
    """章节列表。"""
    try:
        return await get_project_chapters(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../chapters -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"章节查询失败: {type(e).__name__}: {e}")


@router.get(
    "/projects/{project_id}/characters",
    response_model=list[CharacterWithVoice],
)
async def api_project_characters(project_id: str, request: Request):
    """角色 + 音色列表。"""
    try:
        return await get_project_characters(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../characters -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"角色查询失败: {type(e).__name__}: {e}")


@router.patch(
    "/projects/{project_id}/characters/{char_id}",
    response_model=CharacterResp,
)
async def api_update_character_voice(
    project_id: str,
    char_id: int,
    req: UpdateCharacterVoiceRequest,
    request: Request,
):
    """更新角色音色。"""
    try:
        return await update_character_voice(project_id, char_id, req.voice_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 PATCH /api/projects/{project_id[:8]}.../characters/{char_id} -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"更新音色失败: {type(e).__name__}: {e}")


# ---------- Build 任务 ----------

@router.post("/projects/{project_id}/builds", response_model=BuildResp)
async def api_start_build(
    project_id: str,
    req: StartBuildRequest,
    request: Request,
):
    """创建并启动 Build（后台任务，立即返回）。"""
    t0 = _time.perf_counter()
    remote = request.client.host if request.client else "?"
    logger.info(
        f"[HTTP] POST /api/projects/{project_id[:8]}.../builds client={remote} "
        f"voices={len(req.voice_assignments)} narrator={req.narrator_voice_id} "
        f"speed={req.speed}"
    )
    try:
        resp = await start_build(
            project_id=project_id,
            voice_assignments=req.voice_assignments,
            narrator_voice_id=req.narrator_voice_id,
            speed=req.speed,
        )
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[HTTP] 200 /api/projects/{project_id[:8]}.../builds "
            f"build_id={resp.build_id[:8]}... status={resp.status} total_ms={elapsed_ms}"
        )
        return resp
    except ValueError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"启动 build 失败: {type(e).__name__}: {e}")


@router.get("/projects/{project_id}/builds", response_model=list[BuildListItem])
async def api_list_builds(project_id: str, request: Request):
    """Build 历史列表。"""
    try:
        return await list_builds(project_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"build 列表查询失败: {type(e).__name__}: {e}")


@router.get("/projects/{project_id}/builds/{build_id}", response_model=BuildDetailResp)
async def api_get_build(project_id: str, build_id: str, request: Request):
    """Build 详情（含 artifacts，可用于轮询）。"""
    try:
        return await get_build(project_id, build_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds/{build_id[:8]}... -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"build 详情查询失败: {type(e).__name__}: {e}")


@router.get(
    "/projects/{project_id}/builds/{build_id}/status",
    response_model=BuildStatusResp,
)
async def api_build_status(project_id: str, build_id: str, request: Request):
    """轮询用：仅 progress + artifacts（精简版，比详情少配置快照字段）。"""
    try:
        # 校验 build 属于该 project
        factory = get_session_factory()
        async with factory() as s:
            b = await s.get(Build, build_id)
            if not b or b.project_id != project_id:
                raise ValueError("build 不存在")
        return await get_build_status(build_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 /api/projects/{project_id[:8]}.../builds/{build_id[:8]}.../status -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"build 状态查询失败: {type(e).__name__}: {e}")


@router.get(
    "/projects/{project_id}/builds/{build_id}/chapters/{idx}/download"
)
async def api_build_chapter_download(
    project_id: str,
    build_id: str,
    idx: int,
    request: Request,
):
    """单章下载：返回 FileResponse，强制浏览器保存为中文文件名。"""
    # 校验 build 属于该 project
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b or b.project_id != project_id:
            raise HTTPException(404, "build 不存在")
        art = (
            await s.execute(
                select(BuildArtifact).where(
                    BuildArtifact.build_id == build_id,
                    BuildArtifact.chapter_idx == idx,
                )
            )
        ).scalar_one_or_none()
        if not art or not art.audio_filename:
            raise HTTPException(404, f"章节 {idx} 尚未生成")
        audio_filename = art.audio_filename
        art_title = art.title

    audio_dir = Path(settings.AUDIO_DIR)
    fpath = audio_dir / audio_filename
    if not fpath.is_file():
        raise HTTPException(404, f"章节 {idx} 音频文件不存在")
    fname = f"第{idx+1:03d}章 {art_title or '章节'}.mp3"
    for ch in '\\/:*?"<>|\r\n\t':
        fname = fname.replace(ch, "_")
    ascii_name = urllib.parse.quote(fname.encode("utf-8"), safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{ascii_name}"
    }
    return FileResponse(
        path=str(fpath),
        media_type="audio/mpeg",
        headers=headers,
        filename=fname,
    )


@router.get("/projects/{project_id}/builds/{build_id}/download-all")
async def api_build_download_all(
    project_id: str,
    build_id: str,
    request: Request,
):
    """一键全部下载：返回打包好的 ZIP（中文文件名）。"""
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b or b.project_id != project_id:
            raise HTTPException(404, "build 不存在")
        if not b.zip_filename:
            raise HTTPException(400, "整包 ZIP 尚未生成，请先等合成完成")
        zip_filename = b.zip_filename
        book_title = None
        proj = await s.get(Project, project_id)
        if proj:
            book_title = proj.book_title

    audio_dir = Path(settings.AUDIO_DIR)
    zip_path = audio_dir / zip_filename
    if not zip_path.is_file():
        raise HTTPException(404, "ZIP 文件不存在")
    download_name = _safe_download_name_build(book_title, build_id, ".zip")
    ascii_name = urllib.parse.quote(download_name.encode("utf-8"), safe="")
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{ascii_name}"
    }
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        headers=headers,
        filename=download_name,
    )


@router.delete("/projects/{project_id}/builds/{build_id}")
async def api_delete_build(project_id: str, build_id: str, request: Request):
    """删除 build + 磁盘 MP3 文件。"""
    try:
        await delete_build(project_id, build_id)
        return {"ok": True, "build_id": build_id}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(
            f"[HTTP] 500 DELETE /api/projects/{project_id[:8]}.../builds/{build_id[:8]}... -> "
            f"{type(e).__name__}: {e}",
            exc_info=True,
        )
        raise HTTPException(500, f"删除 build 失败: {type(e).__name__}: {e}")
