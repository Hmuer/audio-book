from __future__ import annotations
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session_factory
from ..ai.factory import get_tts
from ..core.config import settings
from ..services.chapter import (
    prepare_chapter,
    synthesize_chapter,
    PrepareResponse,
    SynthesizeResponse,
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


class TtsPreviewRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    voice_id: str


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
    db: AsyncSession = Depends(get_db),
):
    try:
        resp = await prepare_chapter(
            session=db,
            raw_text=req.text,
            enable_polish=req.enable_polish,
        )
        return resp
    except Exception as e:
        logger.exception("prepare 失败")
        raise HTTPException(500, f"prepare 失败: {type(e).__name__}: {e}")


@router.post("/chapter/synthesize", response_model=SynthesizeResponse)
async def api_synthesize(
    req: SynthesizeRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        resp = await synthesize_chapter(
            session=db,
            job_id=req.job_id,
            voice_assignments=req.voice_assignments,
            narrator_voice_id=req.narrator_voice_id,
            segment_overrides=req.segment_overrides,
        )
        return resp
    except Exception as e:
        logger.exception("synthesize 失败")
        raise HTTPException(500, f"synthesize 失败: {type(e).__name__}: {e}")


@router.post("/tts/preview")
async def api_tts_preview(req: TtsPreviewRequest):
    try:
        tts = get_tts()
        audio_dir = Path(settings.AUDIO_DIR)
        audio_dir.mkdir(parents=True, exist_ok=True)
        import uuid as _uuid
        fname = f"preview_{_uuid.uuid4().hex[:10]}.mp3"
        fpath = str(audio_dir / fname)
        path, dur_ms = await tts.synthesize_to_file(req.text, req.voice_id, fpath)
        return {
            "audio_filename": fname,
            "audio_url": f"/media/{fname}",
            "duration_ms": dur_ms,
        }
    except Exception as e:
        logger.exception("TTS preview 失败")
        raise HTTPException(500, f"TTS 失败: {type(e).__name__}: {e}")
