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
from ..db.models import Job, ChapterResult
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
