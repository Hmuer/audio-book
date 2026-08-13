"""
整本小说处理服务。

流程：
1. upload_book: 接收上传文件，保存到磁盘，返回 file_id
2. prepare_book: 读取文件 → 章节识别 → 全书角色识别 → 每章对白归属 → 音色推荐 → 落库
3. synthesize_book: 按章串行合成（复用单章逻辑），每章独立 MP3，最后合并整本 MP3
4. get_book_status: 返回进度（chapter_results 列表 + completed_chapters/total_chapters）

关键点：
- prepare 阶段：全书角色一次性识别，对白按章归属
- Dialogue 表的 anchor_start/end 是该章内的局部位置，chapter_idx 标识所属章节
- synthesize 阶段：每章独立合成，章节间插 1.5s 静音
"""
from __future__ import annotations

import asyncio
import json
import logging
import time as _time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..db.models import (
    Job,
    Character as DbCharacter,
    Dialogue as DbDialogue,
    ChapterResult,
)
from ..ai.factory import get_tts
from .chapter import (
    Chapter,
    _Segment,
    _build_segments_for_chapter,
)
from .character import (
    Character,
    extract_characters_with_llm,
    deduplicate_characters_with_llm,
    apply_dedup,
)
from .dialogue import attribute_dialogues_with_llm
from .voice_recommender import VoiceRecommendation, recommend_voices_with_llm
from .book_split import split_book_chapters
from ..ai.providers.minimax.tts import (
    make_silent_mp3,
    concat_mp3_files,
    _estimate_mp3_duration_ms,
)

logger = logging.getLogger(__name__)

SILENCE_BETWEEN_CHAPTERS_MS = 1500


# ---------- 文件上传 ----------

async def upload_book(file_content: bytes, filename: str) -> tuple[str, str]:
    """
    保存上传的 TXT 文件到磁盘，返回 (file_id, saved_path)。

    file_id 用作后续 prepare_book 的入参，便于后端从磁盘读全文（避免 HTTP body 过大）。
    """
    uploads_dir = Path(settings.DATA_DIR) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:16]
    # 保留原扩展名方便排查
    ext = Path(filename).suffix or ".txt"
    saved_name = f"book_{file_id}{ext}"
    saved_path = uploads_dir / saved_name
    saved_path.write_bytes(file_content)
    logger.info(
        f"[book_upload] saved file_id={file_id} name={filename} "
        f"size={len(file_content)} -> {saved_path}"
    )
    return file_id, str(saved_path)


# ---------- Pydantic schemas ----------

class BookPrepareResponse(BaseModel):
    job_id: str
    book_title: str | None
    total_chapters: int
    chapters: list[dict]  # [{idx, title, text_len}]
    characters: list[dict]
    voice_recommendations: list[dict]
    polish_warning: str | None


class BookChapterResult(BaseModel):
    chapter_idx: int
    title: str
    status: str  # pending / synthesizing / done / failed
    audio_url: str | None
    duration_ms: int | None
    error_msg: str | None


class BookStatusResponse(BaseModel):
    job_id: str
    book_status: str  # prepared / synthesizing / done / failed
    total_chapters: int
    completed_chapters: int
    progress_msg: str | None
    final_audio_url: str | None
    final_duration_sec: float | None
    chapters: list[BookChapterResult]


class BookSynthResponse(BaseModel):
    job_id: str
    final_audio_filename: str
    final_audio_url: str
    duration_sec: float
    chapters: list[BookChapterResult]


# ---------- Prepare ----------

async def prepare_book(file_id: str, original_filename: str = "") -> BookPrepareResponse:
    """
    整本 prepare：读取上传文件 → 章节识别 → 全书角色识别 → 每章对白归属 → 音色推荐 → 落库。

    注意：不在此函数里 polish（整本 polish 成本高且容易破坏原文，留到每章合成时可选）。
    """
    import time as _time
    t0 = _time.perf_counter()
    job_id = uuid.uuid4().hex
    logger.info(f"[book_prepare] START job_id={job_id[:8]}... file_id={file_id}")

    # 1. 读取文件
    uploads_dir = Path(settings.DATA_DIR) / "uploads"
    # 找对应文件
    candidates = list(uploads_dir.glob(f"book_{file_id}.*"))
    if not candidates:
        raise FileNotFoundError(f"上传文件未找到: file_id={file_id}")
    raw_bytes = candidates[0].read_bytes()
    # 尝试常见编码
    raw_text = ""
    for enc in ("utf-8", "gbk", "gb18030", "big5", "utf-16"):
        try:
            raw_text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not raw_text:
        raise RuntimeError(f"无法解码文件: {candidates[0].name}")
    raw_chars = len(raw_text)
    logger.info(f"[book_prepare] job_id={job_id[:8]}... file={candidates[0].name} chars={raw_chars}")

    # 2. 章节识别（正则 + LLM 兜底）
    pt = _time.perf_counter()
    chapters = await split_book_chapters(raw_text)
    logger.info(
        f"[book_prepare] job_id={job_id[:8]}... split_chapters={len(chapters)} "
        f"ms={int((_time.perf_counter()-pt)*1000)}"
    )

    # 3. 全书角色识别（拼接所有章节文本）
    pt = _time.perf_counter()
    full_text = "\n".join(c.text for c in chapters)
    characters = await _split_50k_and_run_chars(full_text, extract_characters_with_llm)
    if len(characters) >= 2:
        names = [c.name for c in characters]
        dedup_results = await deduplicate_characters_with_llm(names, full_text)
        characters, name_map = apply_dedup(characters, dedup_results)
    else:
        name_map = {c.name: c.name for c in characters}
    logger.info(
        f"[book_prepare] job_id={job_id[:8]}... characters={len(characters)} "
        f"ms={int((_time.perf_counter()-pt)*1000)}"
    )

    # 4. 每章对白归属（并行，每章独立）
    pt = _time.perf_counter()
    async def _attr_one(ch: Chapter, ch_idx: int) -> list:
        attrs = await attribute_dialogues_with_llm(ch.text, characters)
        # speaker 做去重 name → canonical 映射
        for a in attrs:
            a.speaker = name_map.get(a.speaker, a.speaker)
        return attrs
    all_attrs_per_chapter = await asyncio.gather(*[
        _attr_one(ch, i) for i, ch in enumerate(chapters)
    ])
    total_dialogues = sum(len(a) for a in all_attrs_per_chapter)
    logger.info(
        f"[book_prepare] job_id={job_id[:8]}... dialogue_attr done "
        f"total_dialogues={total_dialogues} ms={int((_time.perf_counter()-pt)*1000)}"
    )

    # 5. 音色推荐
    pt = _time.perf_counter()
    voice_recs: list[VoiceRecommendation] = []
    try:
        voice_recs = await recommend_voices_with_llm(characters)
    except Exception as e:
        logger.warning(f"[book_prepare] job_id={job_id[:8]}... voice_rec failed: {e}")
    logger.info(
        f"[book_prepare] job_id={job_id[:8]}... voice_recs={len(voice_recs)} "
        f"ms={int((_time.perf_counter()-pt)*1000)}"
    )

    # 6. 落库：Job + Characters + Dialogues + ChapterResults（pending）
    # 注意：Dialogue.anchor_start/end 是该章内的局部位置；chapter_idx 标识章节
    chapters_json = json.dumps(
        [{"idx": c.idx, "title": c.title, "text": c.text} for c in chapters],
        ensure_ascii=False,
    )

    # DB 操作需要 session，但本函数设计为无 session 参数；
    # 这里通过独立 session 写入（prepare_book 由 routes 层在请求 session 外调度）
    from ..db.session import get_session_factory
    factory = get_session_factory()
    async with factory() as session:
        job = Job(
            job_id=job_id,
            status="ready",
            raw_text=raw_text,
            polished_text=raw_text,  # 整本不 polish
            polish_warning=None,
            chapter_count=len(chapters),
            is_book=True,
            source_filename=original_filename or candidates[0].name,
            book_title=Path(original_filename).stem if original_filename else None,
            book_status="prepared",
            completed_chapters=0,
            progress_msg=f"prepare 完成，共 {len(chapters)} 章",
            chapters_json=chapters_json,
        )
        session.add(job)
        for c in characters:
            session.add(DbCharacter(
                job_id=job_id,
                name=c.name,
                gender=c.gender,
                age=c.age,
                personality=c.personality,
                canonical_name=c.name,
            ))
        for ch_idx, attrs in enumerate(all_attrs_per_chapter):
            for seg_idx, a in enumerate(attrs):
                session.add(DbDialogue(
                    job_id=job_id,
                    chapter_idx=ch_idx,
                    segment_index=seg_idx,
                    anchor_start=a.anchor.start,
                    anchor_end=a.anchor.end,
                    anchor_text=a.anchor.text,
                    speaker=a.speaker,
                    text=a.text,
                    confidence=a.confidence,
                ))
        # 预创建 ChapterResult（pending）
        for ch in chapters:
            session.add(ChapterResult(
                job_id=job_id,
                chapter_idx=ch.idx,
                title=ch.title,
                status="pending",
            ))
        await session.commit()

    total_ms = int((_time.perf_counter() - t0) * 1000)
    logger.info(
        f"[book_prepare] DONE job_id={job_id[:8]}... total_ms={total_ms} "
        f"chapters={len(chapters)} characters={len(characters)} "
        f"dialogues={total_dialogues}"
    )

    return BookPrepareResponse(
        job_id=job_id,
        book_title=job.book_title,
        total_chapters=len(chapters),
        chapters=[{"idx": c.idx, "title": c.title, "text_len": len(c.text)} for c in chapters],
        characters=[c.model_dump() for c in characters],
        voice_recommendations=[r.model_dump() for r in voice_recs],
        polish_warning=None,
    )


async def _split_50k_and_run_chars(text: str, coro_fn) -> list[Character]:
    """长文本按 50k 切片并行跑角色识别，再合并去重前返回原始列表。"""
    MAX = 50000
    if len(text) <= MAX:
        return await coro_fn(text)
    slices = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    results = await asyncio.gather(*[coro_fn(s) for s in slices])
    merged: list[Character] = []
    for r in results:
        merged.extend(r)
    return merged


# ---------- Status ----------

async def get_book_status(session: AsyncSession, job_id: str) -> BookStatusResponse:
    stmt = select(Job).where(Job.job_id == job_id)
    job = (await session.execute(stmt)).scalar_one_or_none()
    if not job:
        raise ValueError(f"job not found: {job_id}")
    stmt_cr = select(ChapterResult).where(
        ChapterResult.job_id == job_id
    ).order_by(ChapterResult.chapter_idx)
    results = list((await session.execute(stmt_cr)).scalars().all())
    return BookStatusResponse(
        job_id=job_id,
        book_status=job.book_status or "unknown",
        total_chapters=job.chapter_count,
        completed_chapters=job.completed_chapters,
        progress_msg=job.progress_msg,
        final_audio_url=f"/media/{job.final_audio_filename}" if job.final_audio_filename else None,
        final_duration_sec=round((job.final_duration_ms or 0) / 1000.0, 2),
        chapters=[
            BookChapterResult(
                chapter_idx=r.chapter_idx,
                title=r.title,
                status=r.status,
                audio_url=r.audio_url,
                duration_ms=r.duration_ms,
                error_msg=r.error_msg,
            )
            for r in results
        ],
    )


# ---------- Synthesize ----------

async def synthesize_book(
    session: AsyncSession,
    job_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
) -> BookSynthResponse:
    """
    按章串行合成整本小说。

    每章独立合成 MP3，落 ChapterResult 表；最后合并所有章节 MP3 + 章节间静音。
    """
    import time as _time
    t0 = _time.perf_counter()
    logger.info(
        f"[book_synth] START job_id={job_id[:8]}... "
        f"narrator={narrator_voice_id} voices={len(voice_assignments)} speed={speed}"
    )

    # 1. 加载 job + chapters_json
    stmt = select(Job).where(Job.job_id == job_id)
    job = (await session.execute(stmt)).scalar_one()
    if not job.chapters_json:
        raise RuntimeError("job 没有 chapters_json，可能不是整本任务或 prepare 未完成")
    chapters_dicts = json.loads(job.chapters_json)
    chapters = [Chapter(idx=c["idx"], title=c["title"], text=c["text"]) for c in chapters_dicts]
    total = len(chapters)
    logger.info(f"[book_synth] job_id={job_id[:8]}... total_chapters={total}")

    # 2. 更新状态：synthesizing
    job.book_status = "synthesizing"
    job.completed_chapters = 0
    job.progress_msg = f"正在合成第 1/{total} 章"
    await session.commit()

    # 3. 串行合成每章
    tts = get_tts()
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(4)  # 章内并发（与单章一致）

    # 加载该 job 所有对白
    stmt_d = select(DbDialogue).where(DbDialogue.job_id == job_id)
    all_dialogues: list[DbDialogue] = list((await session.execute(stmt_d)).scalars().all())

    chapter_audio_paths: list[str] = []  # 用于最后合并
    chapter_durations_ms: list[int] = []
    completed = 0

    try:
        for ch_idx, ch in enumerate(chapters):
            ch_t0 = _time.perf_counter()
            # 更新 ChapterResult 状态：synthesizing
            stmt_cr = select(ChapterResult).where(
                ChapterResult.job_id == job_id,
                ChapterResult.chapter_idx == ch_idx,
            )
            cr = (await session.execute(stmt_cr)).scalar_one_or_none()
            if cr:
                cr.status = "synthesizing"
                cr.error_msg = None
                await session.commit()

            job.progress_msg = f"正在合成第 {ch_idx+1}/{total} 章：{ch.title}"
            await session.commit()

            try:
                # 取该章对白
                ch_dialogues = [d for d in all_dialogues if d.chapter_idx == ch_idx]
                ch_dialogues.sort(key=lambda x: x.anchor_start)

                # build segments（与单章一致）
                segs, _ = _build_segments_for_chapter(
                    ch, ch_dialogues,
                    narrator_voice_id=narrator_voice_id,
                    voice_assignments=voice_assignments,
                    segment_overrides=None,
                    start_idx=0,
                )

                # 并发合成每个非 silence 段
                async def _synth_seg(s: _Segment) -> tuple[_Segment, bytes, int, str]:
                    if s.kind == "silence":
                        return s, make_silent_mp3(max(s.silence_ms, 1)), s.silence_ms, ""
                    async with sem:
                        fname = f"book_{job_id}_ch{ch_idx:04d}_seg{s.idx:04d}.mp3"
                        fpath = str(audio_dir / fname)
                        await tts.synthesize_to_file(
                            s.text, s.voice_id or narrator_voice_id, fpath, speed=speed
                        )
                        with open(fpath, "rb") as f:
                            data = f.read()
                        dur = _estimate_mp3_duration_ms(data)
                        return s, data, dur, fname

                tasks = [_synth_seg(s) for s in segs]
                results = await asyncio.gather(*tasks)

                # 拼接该章 MP3
                ch_bytes = concat_mp3_files(*[r[1] for r in results])
                ch_fname = f"book_{job_id}_ch{ch_idx:04d}.mp3"
                ch_fpath = str(audio_dir / ch_fname)
                with open(ch_fpath, "wb") as f:
                    f.write(ch_bytes)
                ch_dur_ms = _estimate_mp3_duration_ms(ch_bytes)
                chapter_audio_paths.append(ch_fpath)
                chapter_durations_ms.append(ch_dur_ms)

                # 更新 ChapterResult
                if cr:
                    cr.status = "done"
                    cr.audio_filename = ch_fname
                    cr.audio_url = f"/media/{ch_fname}"
                    cr.duration_ms = ch_dur_ms
                    cr.error_msg = None
                    await session.commit()

                completed += 1
                job.completed_chapters = completed
                await session.commit()
                logger.info(
                    f"[book_synth] job_id={job_id[:8]}... ch {ch_idx+1}/{total} "
                    f"done title={ch.title!r} dur_ms={ch_dur_ms} "
                    f"ms={int((_time.perf_counter()-ch_t0)*1000)}"
                )

            except Exception as ch_err:
                logger.error(
                    f"[book_synth] job_id={job_id[:8]}... ch {ch_idx+1}/{total} "
                    f"FAIL: {type(ch_err).__name__}: {ch_err}",
                    exc_info=True,
                )
                if cr:
                    cr.status = "failed"
                    cr.error_msg = f"{type(ch_err).__name__}: {ch_err}"[:500]
                    await session.commit()
                # 单章失败不阻断后续章节（可重试），继续下一章
                continue

        # 4. 合并所有章节 MP3（章节间插入 1.5s 静音）
        logger.info(f"[book_synth] job_id={job_id[:8]}... merging {len(chapter_audio_paths)} chapters")
        parts: list[bytes] = []
        for i, path in enumerate(chapter_audio_paths):
            if i > 0:
                parts.append(make_silent_mp3(SILENCE_BETWEEN_CHAPTERS_MS))
            with open(path, "rb") as f:
                parts.append(f.read())
        final_bytes = concat_mp3_files(*parts)
        final_fname = f"book_{job_id}_final.mp3"
        final_path = str(audio_dir / final_fname)
        with open(final_path, "wb") as f:
            f.write(final_bytes)
        total_ms = _estimate_mp3_duration_ms(final_bytes)

        job.book_status = "done"
        job.final_audio_filename = final_fname
        job.final_duration_ms = total_ms
        job.progress_msg = f"全部完成，共 {completed}/{total} 章"
        await session.commit()

        # 返回最终状态
        stmt_cr = select(ChapterResult).where(
            ChapterResult.job_id == job_id
        ).order_by(ChapterResult.chapter_idx)
        results = list((await session.execute(stmt_cr)).scalars().all())

        total_elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[book_synth] DONE job_id={job_id[:8]}... total_ms={total_elapsed_ms} "
            f"completed={completed}/{total} final_size_kb={len(final_bytes)//1024} "
            f"duration_s={round(total_ms/1000,1)}"
        )

        return BookSynthResponse(
            job_id=job_id,
            final_audio_filename=final_fname,
            final_audio_url=f"/media/{final_fname}",
            duration_sec=round(total_ms / 1000.0, 2),
            chapters=[
                BookChapterResult(
                    chapter_idx=r.chapter_idx,
                    title=r.title,
                    status=r.status,
                    audio_url=r.audio_url,
                    duration_ms=r.duration_ms,
                    error_msg=r.error_msg,
                )
                for r in results
            ],
        )

    except Exception as e:
        job.book_status = "failed"
        job.progress_msg = f"合成失败: {type(e).__name__}: {e}"[:200]
        await session.commit()
        logger.error(
            f"[book_synth] FAIL job_id={job_id[:8]}... {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise
