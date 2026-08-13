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

# 防止同一个 job 重复合成的内存锁（仅单 worker 场景可靠；
# 多 worker 下由 DB 的 book_status=synthesizing 做兜底检查）
_RUNNING_JOBS: set[str] = set()
_RUNNING_LOCK = asyncio.Lock()


async def _ensure_default_narrator(narrator_voice_id: str | None) -> str:
    """narrator 为空时，从 TTS 音色库取第一个兜底，避免合成时报 voice id not exist。"""
    if narrator_voice_id:
        return narrator_voice_id
    tts = get_tts()
    voices = await tts.list_voices()
    if voices:
        vid = (
            next((v["id"] for v in voices if v.get("id") == "male-qn-jingying"), None)
            or voices[0].get("id", "")
        )
        return vid
    raise RuntimeError("音色库为空，无法合成")


async def start_synthesize_book_background(
    job_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
) -> BookStatusResponse:
    """
    启动整本合成：先把状态设为 synthesizing（DB 层），再用 asyncio.create_task 后台跑。
    立刻返回当前 status，供前端轮询。

    去重策略（双重）：
    1. 内存 _RUNNING_JOBS：单 worker 内绝对不重复
    2. DB book_status=prepared：只有 prepared 才允许启动
    """
    from ..db.session import get_session_factory

    async with _RUNNING_LOCK:
        if job_id in _RUNNING_JOBS:
            # 已经在跑，直接返回当前状态
            logger.info(f"[book_synth_start] job_id={job_id[:8]}... already running")
            factory = get_session_factory()
            async with factory() as s:
                return await get_book_status(s, job_id)

    narrator_voice_id = await _ensure_default_narrator(narrator_voice_id)

    factory = get_session_factory()
    async with factory() as session:
        # DB 层去重 + 状态流转：只有 prepared 允许启动
        stmt = select(Job).where(Job.job_id == job_id).with_for_update()
        job = (await session.execute(stmt)).scalar_one_or_none()
        if not job:
            raise ValueError(f"job not found: {job_id}")
        if job.book_status == "synthesizing":
            logger.warning(f"[book_synth_start] job_id={job_id[:8]}... already synthesizing (DB)")
            return await get_book_status(session, job_id)
        if job.book_status == "done":
            logger.warning(f"[book_synth_start] job_id={job_id[:8]}... already done")
            return await get_book_status(session, job_id)
        if job.book_status != "prepared":
            raise RuntimeError(
                f"job 状态不允许合成: book_status={job.book_status!r}（要求: prepared）"
            )
        if not job.chapters_json:
            raise RuntimeError("job 没有 chapters_json，prepare 未完成")

        job.book_status = "synthesizing"
        job.completed_chapters = 0
        total = len(json.loads(job.chapters_json))
        job.progress_msg = f"准备合成 1/{total} 章…"
        await session.commit()
        cur_status = await get_book_status(session, job_id)

    # 写内存锁 + 后台启动
    async with _RUNNING_LOCK:
        _RUNNING_JOBS.add(job_id)

    async def _runner() -> None:
        """后台 worker：独立 session，完成后释放锁。"""
        try:
            await _synthesize_book_inner(
                job_id=job_id,
                voice_assignments=voice_assignments,
                narrator_voice_id=narrator_voice_id,
                speed=speed,
            )
        except Exception as e:
            logger.error(
                f"[book_synth_worker] FAIL job_id={job_id[:8]}... {type(e).__name__}: {e}",
                exc_info=True,
            )
            # 最后尝试把状态写回（失败不抛）
            try:
                f2 = get_session_factory()
                async with f2() as s:
                    j = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one()
                    j.book_status = "failed"
                    j.progress_msg = f"合成失败: {type(e).__name__}: {e}"[:200]
                    await s.commit()
            except Exception as e2:
                logger.error(f"[book_synth_worker] final status write fail: {e2}")
        finally:
            async with _RUNNING_LOCK:
                _RUNNING_JOBS.discard(job_id)

    asyncio.create_task(_runner(), name=f"book_synth_{job_id[:8]}")
    logger.info(f"[book_synth_start] launched worker job_id={job_id[:8]}...")
    return cur_status


async def _synthesize_book_inner(
    job_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
) -> None:
    """
    核心合成逻辑（在后台 worker 里运行）。
    - 用独立 session factory（不绑定 HTTP 请求 session）
    - 每章 commit 一次状态，前端轮询可读
    - 失败章写入占位静音 MP3，保证最终整本 MP3 的章节顺序不跳章
    """
    import time as _time
    from ..db.session import get_session_factory

    t0 = _time.perf_counter()
    logger.info(
        f"[book_synth_worker] START job_id={job_id[:8]}... "
        f"narrator={narrator_voice_id} voices={len(voice_assignments)} speed={speed}"
    )

    factory = get_session_factory()

    # 1. 加载 job + chapters + 对白
    async with factory() as s:
        job = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one()
        chapters_dicts = json.loads(job.chapters_json or "[]")
        chapters = [Chapter(idx=c["idx"], title=c["title"], text=c["text"]) for c in chapters_dicts]
        total = len(chapters)
        # 加载所有对白（跨章节一次性读，不持有 DB 对象循环）
        stmt_d = select(DbDialogue).where(DbDialogue.job_id == job_id)
        all_dialogues_rows = list((await s.execute(stmt_d)).scalars().all())

    # 对白按 chapter_idx 分桶（用 list，纯内存结构）
    dialogues_by_chapter: dict[int, list[DbDialogue]] = {}
    for d in all_dialogues_rows:
        dialogues_by_chapter.setdefault(d.chapter_idx, []).append(d)
    for lst in dialogues_by_chapter.values():
        lst.sort(key=lambda x: x.anchor_start)

    logger.info(f"[book_synth_worker] job_id={job_id[:8]}... total_chapters={total}")

    tts = get_tts()
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(4)

    # chapter_outputs[i] = (audio_path, duration_ms) — 长度固定 total
    # 失败章仍然写入占位静音，保证顺序与章号对齐
    chapter_outputs: list[tuple[str | None, int | None]] = [(None, None)] * total
    completed = 0
    failed_count = 0

    for ch_idx, ch in enumerate(chapters):
        ch_t0 = _time.perf_counter()
        # 写 ChapterResult.status=synthesizing + Job.progress_msg
        async with factory() as s:
            stmt_cr = select(ChapterResult).where(
                ChapterResult.job_id == job_id,
                ChapterResult.chapter_idx == ch_idx,
            )
            cr = (await s.execute(stmt_cr)).scalar_one_or_none()
            if cr:
                cr.status = "synthesizing"
                cr.error_msg = None
            j = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one()
            j.progress_msg = f"正在合成第 {ch_idx+1}/{total} 章：{ch.title}"
            await s.commit()

        try:
            ch_dialogues = dialogues_by_chapter.get(ch_idx, [])

            segs, _ = _build_segments_for_chapter(
                ch, ch_dialogues,
                narrator_voice_id=narrator_voice_id,
                voice_assignments=voice_assignments,
                segment_overrides=None,
                start_idx=0,
            )

            # 章内并发合成
            async def _synth_seg(s: _Segment) -> tuple[_Segment, bytes, int]:
                if s.kind == "silence":
                    return s, make_silent_mp3(max(s.silence_ms, 1)), s.silence_ms
                async with sem:
                    vid = s.voice_id or narrator_voice_id
                    # 注意：直接调 synthesize_to_bytes，避免磁盘 I/O 放大
                    data = await tts.synthesize_to_bytes(s.text, vid, speed=speed)
                    dur = _estimate_mp3_duration_ms(data)
                    return s, data, dur

            tasks = [_synth_seg(s) for s in segs]
            results = await asyncio.gather(*tasks)

            ch_bytes = concat_mp3_files(*[r[1] for r in results])
            ch_fname = f"book_{job_id}_ch{ch_idx:04d}.mp3"
            ch_fpath = str(audio_dir / ch_fname)
            with open(ch_fpath, "wb") as f:
                f.write(ch_bytes)
            ch_dur_ms = _estimate_mp3_duration_ms(ch_bytes)
            chapter_outputs[ch_idx] = (ch_fpath, ch_dur_ms)

            # 写 ChapterResult + completed_chapters
            async with factory() as s:
                stmt_cr = select(ChapterResult).where(
                    ChapterResult.job_id == job_id,
                    ChapterResult.chapter_idx == ch_idx,
                )
                cr = (await s.execute(stmt_cr)).scalar_one_or_none()
                if cr:
                    cr.status = "done"
                    cr.audio_filename = ch_fname
                    cr.audio_url = f"/media/{ch_fname}"
                    cr.duration_ms = ch_dur_ms
                    cr.error_msg = None
                j = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one()
                completed += 1
                j.completed_chapters = completed
                await s.commit()

            logger.info(
                f"[book_synth_worker] job_id={job_id[:8]}... ch {ch_idx+1}/{total} "
                f"done title={ch.title!r} dur_ms={ch_dur_ms} "
                f"ms={int((_time.perf_counter()-ch_t0)*1000)}"
            )

        except Exception as ch_err:
            logger.error(
                f"[book_synth_worker] job_id={job_id[:8]}... ch {ch_idx+1}/{total} "
                f"FAIL: {type(ch_err).__name__}: {ch_err}",
                exc_info=True,
            )
            failed_count += 1
            # 失败章写占位静音，保证最终 MP3 不跳章
            placeholder_bytes = make_silent_mp3(1000)  # 1s 占位
            ph_fname = f"book_{job_id}_ch{ch_idx:04d}_failed.mp3"
            ph_fpath = str(audio_dir / ph_fname)
            with open(ph_fpath, "wb") as f:
                f.write(placeholder_bytes)
            chapter_outputs[ch_idx] = (ph_fpath, 1000)

            async with factory() as s:
                stmt_cr = select(ChapterResult).where(
                    ChapterResult.job_id == job_id,
                    ChapterResult.chapter_idx == ch_idx,
                )
                cr = (await s.execute(stmt_cr)).scalar_one_or_none()
                if cr:
                    cr.status = "failed"
                    cr.audio_filename = ph_fname
                    cr.audio_url = f"/media/{ph_fname}"
                    cr.duration_ms = 1000
                    cr.error_msg = f"{type(ch_err).__name__}: {ch_err}"[:500]
                j = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one()
                j.completed_chapters = completed  # 不递增 completed（但仍保持进度可见）
                await s.commit()
            continue

    # 4. 合并所有章节 MP3（章节间插 1.5s 静音）
    logger.info(
        f"[book_synth_worker] job_id={job_id[:8]}... merging {total} chapters "
        f"(failed={failed_count})"
    )
    parts: list[bytes] = []
    merged_dur_ms = 0
    for i, (path, dur) in enumerate(chapter_outputs):
        if path is None:
            # 双重兜底：连占位都没写 → 补 0.5s 静音
            placeholder = make_silent_mp3(500)
            if i > 0:
                parts.append(make_silent_mp3(SILENCE_BETWEEN_CHAPTERS_MS))
                merged_dur_ms += SILENCE_BETWEEN_CHAPTERS_MS
            parts.append(placeholder)
            merged_dur_ms += 500
            continue
        if i > 0:
            parts.append(make_silent_mp3(SILENCE_BETWEEN_CHAPTERS_MS))
            merged_dur_ms += SILENCE_BETWEEN_CHAPTERS_MS
        with open(path, "rb") as f:
            data = f.read()
        parts.append(data)
        merged_dur_ms += dur or 0

    final_bytes = concat_mp3_files(*parts) if parts else make_silent_mp3(100)
    final_fname = f"book_{job_id}_final.mp3"
    final_path = str(audio_dir / final_fname)
    with open(final_path, "wb") as f:
        f.write(final_bytes)
    total_ms = _estimate_mp3_duration_ms(final_bytes)

    # 5. 写 Job 最终状态
    async with factory() as s:
        j = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one()
        if failed_count == 0:
            j.book_status = "done"
        else:
            # 部分失败仍产出最终 MP3，但标记为带警告的 done
            j.book_status = "done"
        j.final_audio_filename = final_fname
        j.final_duration_ms = total_ms
        j.progress_msg = (
            f"全部完成 {completed}/{total} 章"
            + (f"（{failed_count} 章失败已用静音占位）" if failed_count else "")
        )
        await s.commit()

    total_elapsed_ms = int((_time.perf_counter() - t0) * 1000)
    logger.info(
        f"[book_synth_worker] DONE job_id={job_id[:8]}... total_ms={total_elapsed_ms} "
        f"completed={completed}/{total} failed={failed_count} "
        f"final_size_kb={len(final_bytes)//1024} duration_s={round(total_ms/1000,1)}"
    )


async def synthesize_book(
    session: AsyncSession,
    job_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
) -> BookSynthResponse:
    """
    【兼容旧调用】前台同步合成整本（仅用于单章等短任务）。
    真正的整本模式请走 start_synthesize_book_background（后台任务 + 状态轮询）。

    内部重定向：先 start，再循环等待完成。⚠️ 不建议对长任务用这个接口。
    """
    logger.warning(
        f"[book_synth] legacy sync path used for job_id={job_id[:8]}... "
        "长任务请使用 start_synthesize_book_background"
    )
    narrator_voice_id = await _ensure_default_narrator(narrator_voice_id)

    await start_synthesize_book_background(
        job_id=job_id,
        voice_assignments=voice_assignments,
        narrator_voice_id=narrator_voice_id,
        speed=speed,
    )
    # 同步等待
    from ..db.session import get_session_factory
    factory = get_session_factory()
    sleep_for = 0.5
    for _ in range(int(600 / sleep_for)):  # 最多 600s（默认 UVICORN_TIMEOUT）
        await asyncio.sleep(sleep_for)
        async with factory() as s:
            cur = await get_book_status(s, job_id)
        if cur.book_status == "done":
            return BookSynthResponse(
                job_id=job_id,
                final_audio_filename=cur.final_audio_url.split("/")[-1] if cur.final_audio_url else "",
                final_audio_url=cur.final_audio_url or "",
                duration_sec=cur.final_duration_sec or 0,
                chapters=cur.chapters,
            )
        if cur.book_status == "failed":
            raise RuntimeError(cur.progress_msg or "合成失败")
    raise TimeoutError("整本合成超时（> 600s），请使用后台任务接口")
