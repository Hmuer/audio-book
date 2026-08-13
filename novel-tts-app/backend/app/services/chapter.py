from __future__ import annotations
import asyncio
import uuid
import logging
from pathlib import Path
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..core.config import settings
from ..db.models import Job, Character as DbCharacter, Dialogue as DbDialogue
from ..ai.factory import get_llm, get_tts
from ..ai.providers.minimax.tts import make_silent_mp3, concat_mp3_files
from .polish import polish_with_llm
from .character import (
    Character,
    extract_characters_with_llm,
    deduplicate_characters_with_llm,
    apply_dedup,
)
from .dialogue import DialogueAttribution, attribute_dialogues_with_llm
from .voice_recommender import VoiceRecommendation, recommend_voices_with_llm

logger = logging.getLogger(__name__)

MAX_CHAPTER_CHARS = 50000
SILENCE_AFTER_TITLE_MS = 1500
SILENCE_BETWEEN_SEGMENTS_MS = 250


# ---------- Pydantic schemas (service input/output) ----------

class Chapter(BaseModel):
    idx: int
    title: str
    text: str


class PrepareResponse(BaseModel):
    job_id: str
    polished_text: str
    diff: list[dict]
    polish_warning: str | None
    characters: list[dict]
    dialogue_attributions: list[dict]
    chapters: list[dict]
    voice_recommendations: list[dict]


class SynthesizeSegmentResponse(BaseModel):
    idx: int
    kind: str  # "title" | "narrator" | "dialogue" | "silence"
    speaker: str | None
    voice_id: str
    text: str
    audio_filename: str
    audio_url: str
    duration_ms: int
    confidence: float | None


class SynthesizeResponse(BaseModel):
    job_id: str
    audio_filename: str
    audio_url: str
    duration_sec: float
    segments: list[SynthesizeSegmentResponse]


# ---------- Semantic chapter split ----------

SPLIT_PROMPT_FEW_SHOT = r"""
你是一名小说章节编辑。请根据语义、场景切换、时间跳转、视角切换等信号，把长文切分为若干章节。
规则：
1. 每章尽量不超过 50000 字，但禁止在对白句中间硬切（整句保留）
2. 每章 title 用 4-12 字概括本章核心内容
3. 在语义自然分界处切：场景切换、时间跳转("第二天""三年后")、视角切换、完整小事/冲突结束
4. 输出格式要求：{ "data": [ {"idx":0,"title":"初遇街角","text":"切片原文"}, ... ] }
5. 所有 chapter.text 按 idx 顺序拼接后，字符级必须严格等于输入原文，不许漏字不许加字
6. 文本 ≤ 2000 字时就输出一章即可
"""


async def split_chapters_with_llm(long_text: str) -> list[Chapter]:
    """按 LLM 语义分章，不再硬字数切。"""
    if len(long_text) <= MAX_CHAPTER_CHARS:
        # 短文本也走 LLM 生成 title
        pass

    prompt = (
        SPLIT_PROMPT_FEW_SHOT
        + "\n【现在处理以下长文】\n---LONG TEXT START---\n"
        + long_text
        + "\n---LONG TEXT END---\n\n"
        + "⚠️输出格式必须是 {\"data\": [Chapter,...]}，顶层一定要有 data 字段!"
    )

    class _Wrapper(BaseModel):
        data: list[Chapter]

    llm = get_llm()
    try:
        wrapped = await llm.chat_structured(
            prompt=prompt,
            output_schema=_Wrapper,
            temperature=0.2,
            max_tokens=32000,
        )
        chapters = wrapped.data
        if not chapters:
            chapters = [Chapter(idx=0, title="正文", text=long_text)]
    except Exception as e:
        logger.warning(f"[chapter_split] LLM 失败，退化为单章: {e}")
        chapters = [Chapter(idx=0, title="正文", text=long_text)]

    # 校验拼接完整性（严格检查）
    restored = "".join(c.text for c in chapters)
    if restored != long_text:
        logger.warning(
            f"[chapter_split] 拼接与原文不等(len {len(restored)} vs {len(long_text)})，退回单章"
        )
        chapters = [Chapter(idx=0, title="正文", text=long_text)]

    # 若某章仍超 50000 字，递归再切一次（但不能对白中硬切——仍用 LLM）
    final_chapters: list[Chapter] = []
    idx_offset = 0
    for ch in chapters:
        if len(ch.text) > MAX_CHAPTER_CHARS * 1.2:
            sub = await split_chapters_with_llm(ch.text)
            for i, s in enumerate(sub):
                final_chapters.append(
                    Chapter(idx=idx_offset + i, title=s.title, text=s.text)
                )
            idx_offset += len(sub)
        else:
            final_chapters.append(Chapter(idx=idx_offset, title=ch.title, text=ch.text))
            idx_offset += 1

    # 重排 idx
    for i, ch in enumerate(final_chapters):
        ch.idx = i
    return final_chapters


# ---------- Prepare 主流程 ----------

async def _split_50k_and_run(text: str, coro_fn) -> list:
    """
    长文本按 50000 字切片，并行跑 coro_fn(slice)，再拼接。
    coro_fn: async (str) -> list
    """
    if len(text) <= MAX_CHAPTER_CHARS:
        return await coro_fn(text)
    # 按 50k 切片（按句子边界尽量对齐：这里直接切，LLM 自己会处理）
    slices: list[str] = []
    i = 0
    while i < len(text):
        end = min(i + MAX_CHAPTER_CHARS, len(text))
        slices.append(text[i:end])
        i = end
    results = await asyncio.gather(*[coro_fn(s) for s in slices])
    merged: list = []
    for r in results:
        merged.extend(r)
    return merged


async def prepare_chapter(
    session: AsyncSession,
    raw_text: str,
    enable_polish: bool = True,
) -> PrepareResponse:
    import time as _time
    job_id = uuid.uuid4().hex
    t0 = _time.perf_counter()
    raw_chars = len(raw_text)
    logger.info(
        f"[prepare] START job_id={job_id[:8]}... raw_chars={raw_chars} enable_polish={enable_polish}"
    )
    phase_timings: dict[str, int] = {}

    # 1) Polish
    polished_text = raw_text
    diff: list[dict] = []
    polish_warning: str | None = None

    if enable_polish:
        pt = _time.perf_counter()
        try:
            polish_result = await polish_with_llm(raw_text)
            if polish_result.is_reasonable:
                polished_text = polish_result.polished_text
                diff = [d.model_dump() for d in polish_result.diff]
                logger.info(
                    f"[prepare] job_id={job_id[:8]}... polish ok: diff_count={len(diff)} "
                    f"ms={int((_time.perf_counter()-pt)*1000)}"
                )
            else:
                # 回退原文
                polished_text = raw_text
                polish_warning = (
                    "LLM 自我评估本次修改不合理（过度润色或改动文风），已回退到原文。"
                    f"原因：{polish_result.reason}"
                )
                logger.warning(
                    f"[prepare] job_id={job_id[:8]}... polish rejected: reason={polish_result.reason[:80]}"
                )
        except Exception as e:
            logger.warning(
                f"[prepare] job_id={job_id[:8]}... polish failed: {type(e).__name__}: {e}",
                exc_info=False,
            )
            polish_warning = f"错别字纠错调用失败，使用原文。原因：{type(e).__name__}"
            polished_text = raw_text
        phase_timings["polish_ms"] = int((_time.perf_counter() - pt) * 1000)

    # 2) 角色识别（短文本也调 LLM）
    pt = _time.perf_counter()
    characters = await _split_50k_and_run(
        polished_text, extract_characters_with_llm
    )
    # 去重
    if len(characters) >= 2:
        names = [c.name for c in characters]
        dedup_results = await deduplicate_characters_with_llm(names, polished_text)
        characters, name_map = apply_dedup(characters, dedup_results)
    else:
        name_map = {c.name: c.name for c in characters}
    phase_timings["char_extract_ms"] = int((_time.perf_counter() - pt) * 1000)
    logger.info(
        f"[prepare] job_id={job_id[:8]}... characters={len(characters)} "
        f"ms={phase_timings['char_extract_ms']}"
    )

    # 3) 对白归属（按章节）
    pt = _time.perf_counter()
    chapters = await split_chapters_with_llm(polished_text)
    phase_timings["split_chapter_ms"] = int((_time.perf_counter() - pt) * 1000)

    pt = _time.perf_counter()
    dialogue_attrs: list[DialogueAttribution] = []

    async def _attr_one(chapter_text: str) -> list[DialogueAttribution]:
        return await attribute_dialogues_with_llm(chapter_text, characters)

    all_attrs = await asyncio.gather(*[
        _attr_one(ch.text) for ch in chapters
    ])
    for ch_idx, attrs in enumerate(all_attrs):
        # 每章的 anchor.start/end 都是在该章内的偏移，要累加上前缀
        prefix = sum(len(c.text) for c in chapters[:ch_idx])
        for a in attrs:
            a.anchor.start += prefix
            a.anchor.end += prefix
            # speaker 做去重 name → canonical 映射
            a.speaker = name_map.get(a.speaker, a.speaker)
        dialogue_attrs.extend(attrs)
    phase_timings["dialogue_attr_ms"] = int((_time.perf_counter() - pt) * 1000)
    logger.info(
        f"[prepare] job_id={job_id[:8]}... chapters={len(chapters)} "
        f"dialogues={len(dialogue_attrs)} ms={phase_timings['dialogue_attr_ms']}"
    )

    # 4) 音色推荐
    pt = _time.perf_counter()
    voice_recs: list[VoiceRecommendation] = []
    try:
        voice_recs = await recommend_voices_with_llm(characters)
    except Exception as e:
        logger.warning(f"[prepare] job_id={job_id[:8]}... voice_rec failed: {e}")
        voice_recs = []
    phase_timings["voice_rec_ms"] = int((_time.perf_counter() - pt) * 1000)

    # 5) 落库
    pt = _time.perf_counter()
    job = Job(
        job_id=job_id,
        status="ready",
        raw_text=raw_text,
        polished_text=polished_text,
        polish_warning=polish_warning,
        chapter_count=len(chapters),
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
    for i, d in enumerate(dialogue_attrs):
        session.add(DbDialogue(
            job_id=job_id,
            chapter_idx=0,
            segment_index=i,
            anchor_start=d.anchor.start,
            anchor_end=d.anchor.end,
            anchor_text=d.anchor.text,
            speaker=d.speaker,
            text=d.text,
            confidence=d.confidence,
        ))
    await session.commit()
    phase_timings["db_ms"] = int((_time.perf_counter() - pt) * 1000)

    total_ms = int((_time.perf_counter() - t0) * 1000)
    timing_str = " ".join(f"{k}={v}" for k, v in phase_timings.items())
    logger.info(
        f"[prepare] DONE job_id={job_id[:8]}... total_ms={total_ms} {timing_str} "
        f"chars={len(polished_text)} chapters={len(chapters)} "
        f"characters={len(characters)} dialogues={len(dialogue_attrs)}"
    )

    return PrepareResponse(
        job_id=job_id,
        polished_text=polished_text,
        diff=diff,
        polish_warning=polish_warning,
        characters=[c.model_dump() for c in characters],
        dialogue_attributions=[d.model_dump() for d in dialogue_attrs],
        chapters=[c.model_dump() for c in chapters],
        voice_recommendations=[r.model_dump() for r in voice_recs],
    )


# ---------- Synthesize 主流程 ----------

class _Segment(BaseModel):
    """合成内部 segment：title/narrator/dialogue/silence"""
    kind: str
    chapter_idx: int
    idx: int  # 全局 segment_index
    speaker: str | None = None
    voice_id: str | None = None
    text: str = ""
    confidence: float | None = None
    silence_ms: int = 0  # kind=silence 时用


def _build_segments_for_chapter(
    ch: Chapter,
    dialogues: list[DbDialogue],  # 本章的对白（已按 anchor_start 排序）
    narrator_voice_id: str,
    voice_assignments: dict[str, str],
    segment_overrides: dict[int, str] | None,
    start_idx: int,
) -> tuple[list[_Segment], int]:
    """
    把一章切成：title(+1.5s静音) + 对白/旁白交替段 + 段间短静音
    返回 (segments, next_start_idx)
    """
    segs: list[_Segment] = []
    idx = start_idx

    # 1. 标题段
    segs.append(_Segment(
        kind="title", chapter_idx=ch.idx, idx=idx,
        voice_id=narrator_voice_id, text=f"第{ch.idx+1}章 {ch.title}",
    ))
    idx += 1
    segs.append(_Segment(
        kind="silence", chapter_idx=ch.idx, idx=idx, silence_ms=SILENCE_AFTER_TITLE_MS,
    ))
    idx += 1

    # 2. 扫描 text，按对白 anchor 间隙切片
    cursor = 0
    chapter_len = len(ch.text)
    chapter_offset_start = 0  # 这里会在外部传入时处理
    for dlg in dialogues:
        # anchor 是 polished_text 全局位置，转本章内位置
        #   （dialogue 的 anchor_start 在 prepare 时写的是全局位置，
        #     但这里传给 _build_segments_for_chapter 时已经先对章节切分做过转换）
        local_start = dlg.anchor_start
        local_end = dlg.anchor_end
        # 校验并修正 anchor 位置：LLM 返回的位置可能不准（尤其中文/字节位置错位），
        # 优先用 anchor_text 在 ch.text 中精确定位，避免 narrator 段误切片包含对白内容
        if dlg.anchor_text:
            found = ch.text.find(dlg.anchor_text)
            if found >= 0:
                local_start = found
                local_end = found + len(dlg.anchor_text)
        # narrator 段：[cursor, local_start)
        if cursor < local_start:
            narrator_text = ch.text[cursor:local_start].strip()
            if narrator_text:
                segs.append(_Segment(
                    kind="narrator", chapter_idx=ch.idx, idx=idx,
                    voice_id=narrator_voice_id, text=narrator_text,
                ))
                idx += 1
                segs.append(_Segment(
                    kind="silence", chapter_idx=ch.idx, idx=idx,
                    silence_ms=SILENCE_BETWEEN_SEGMENTS_MS,
                ))
                idx += 1

        # dialogue 段：对白文本（去掉引号的 text）
        seg_voice_id = voice_assignments.get(dlg.speaker, narrator_voice_id)
        dlg_seg = _Segment(
            kind="dialogue", chapter_idx=ch.idx, idx=idx,
            speaker=dlg.speaker,
            voice_id=seg_voice_id,
            text=dlg.text,
            confidence=dlg.confidence,
        )
        segs.append(dlg_seg)
        idx += 1

        segs.append(_Segment(
            kind="silence", chapter_idx=ch.idx, idx=idx,
            silence_ms=SILENCE_BETWEEN_SEGMENTS_MS,
        ))
        idx += 1

        # 推进 cursor 到对白结束位置（防止 anchor 错位时 cursor 倒退导致重复切片）
        if local_end > cursor:
            cursor = local_end

    # 收尾 narrator
    if cursor < chapter_len:
        tail = ch.text[cursor:].strip()
        if tail:
            segs.append(_Segment(
                kind="narrator", chapter_idx=ch.idx, idx=idx,
                voice_id=narrator_voice_id, text=tail,
            ))
            idx += 1

    return segs, idx


async def synthesize_chapter(
    session: AsyncSession,
    job_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    segment_overrides: dict[int, str] | None = None,
    speed: float = 1.0,
) -> SynthesizeResponse:
    import time as _time
    t0 = _time.perf_counter()
    logger.info(
        f"[synthesize] START job_id={job_id[:8]}... "
        f"narrator={narrator_voice_id} voices={len(voice_assignments)} "
        f"overrides={len(segment_overrides or {})} speed={speed}"
    )
    phase: dict[str, int] = {}
    # 1. 加载 job
    stmt = select(Job).where(Job.job_id == job_id)
    result = await session.execute(stmt)
    job = result.scalar_one()
    polished_text = job.polished_text or job.raw_text

    # 2. 使用单章处理（避免重新调 LLM 分章导致与 prepare 阶段不一致）
    chapters = [Chapter(idx=0, title="正文", text=polished_text)]

    stmt_d = select(DbDialogue).where(DbDialogue.job_id == job_id).order_by(DbDialogue.anchor_start)
    result_d = await session.execute(stmt_d)
    all_dialogues: list[DbDialogue] = list(result_d.scalars().all())

    # 将对白按章节内位置重新映射：预计算每章 start
    chapter_starts: list[int] = []
    acc = 0
    for ch in chapters:
        chapter_starts.append(acc)
        acc += len(ch.text)
    chapter_dialogues: list[list[DbDialogue]] = [[] for _ in chapters]
    for d in all_dialogues:
        # 找到 anchor_start 位于哪一章
        placed = False
        for i in range(len(chapters) - 1, -1, -1):
            if chapter_starts[i] <= d.anchor_start:
                ch_start = chapter_starts[i]
                ch_end = ch_start + len(chapters[i].text)
                if d.anchor_start < ch_end:
                    local_d = DbDialogue(
                        id=d.id, job_id=d.job_id, chapter_idx=i,
                        segment_index=d.segment_index,
                        anchor_start=d.anchor_start - ch_start,
                        anchor_end=d.anchor_end - ch_start,
                        anchor_text=d.anchor_text,
                        speaker=d.speaker, text=d.text, confidence=d.confidence,
                    )
                    chapter_dialogues[i].append(local_d)
                    placed = True
                    break
        if not placed:
            # 兜底：塞到第 0 章
            chapter_dialogues[0].append(d)

    # 3. build segments
    segments: list[_Segment] = []
    seg_idx = 0
    for i, ch in enumerate(chapters):
        dls = chapter_dialogues[i]
        dls.sort(key=lambda x: x.anchor_start)
        ch_segs, seg_idx = _build_segments_for_chapter(
            ch, dls,
            narrator_voice_id=narrator_voice_id,
            voice_assignments=voice_assignments,
            segment_overrides=None,  # 覆盖在构建后按对白索引应用
            start_idx=seg_idx,
        )
        segments.extend(ch_segs)

    # 3.5 按"对白索引"应用 segment_overrides（前端发送的是对白序号，非 segment 序号）
    if segment_overrides:
        dlg_idx = 0
        for seg in segments:
            if seg.kind == "dialogue":
                if dlg_idx in segment_overrides:
                    seg.voice_id = segment_overrides[dlg_idx]
                dlg_idx += 1

    # 4. 并发合成每个非 silence 段
    tts = get_tts()
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(4)  # TTS 并发限制 4

    async def _synth_seg(s: _Segment) -> tuple[_Segment, bytes, int, str]:
        if s.kind == "silence":
            data = make_silent_mp3(max(s.silence_ms, 1))
            return s, data, s.silence_ms, ""
        # 其他 kind 调 TTS，落盘
        async with sem:
            fname = f"job_{job_id}_seg{s.idx:05d}_{s.kind}.mp3"
            fpath = str(audio_dir / fname)
            await tts.synthesize_to_file(
                s.text, s.voice_id or narrator_voice_id, fpath, speed=speed
            )
            with open(fpath, "rb") as f:
                data = f.read()
            # 估算时长
            from ..ai.providers.minimax.tts import _estimate_mp3_duration_ms
            dur = _estimate_mp3_duration_ms(data)
            return s, data, dur, fname

    pt = _time.perf_counter()
    tasks = [_synth_seg(s) for s in segments]
    non_silence_count = sum(1 for s in segments if s.kind != "silence")
    logger.info(
        f"[synthesize] job_id={job_id[:8]}... segments_total={len(segments)} "
        f"to_synth={non_silence_count} concurrent=4"
    )
    results = await asyncio.gather(*tasks)
    phase["synth_ms"] = int((_time.perf_counter() - pt) * 1000)

    # 5. 拼接最终 MP3
    all_bytes = [r[1] for r in results]
    final_bytes = concat_mp3_files(*all_bytes)
    final_name = f"job_{job_id}_final.mp3"
    final_path = str(audio_dir / final_name)
    with open(final_path, "wb") as f:
        f.write(final_bytes)

    from ..ai.providers.minimax.tts import _estimate_mp3_duration_ms
    total_ms = _estimate_mp3_duration_ms(final_bytes)

    # 6. 更新 DB
    job.status = "done"
    job.final_audio_filename = final_name
    job.final_duration_ms = total_ms

    # 对白段回写 voice_id / audio_filename / duration_ms
    global_dialogues_sorted = sorted(all_dialogues, key=lambda d: d.anchor_start)
    dlg_ptr = 0
    responses: list[SynthesizeSegmentResponse] = []
    for s, data, dur_ms, fname in results:
        if s.kind == "silence":
            responses.append(SynthesizeSegmentResponse(
                idx=s.idx, kind=s.kind, speaker=None,
                voice_id="__silence__", text="",
                audio_filename="", audio_url="",
                duration_ms=dur_ms, confidence=None,
            ))
            continue
        url = f"/media/{fname}" if fname else ""
        responses.append(SynthesizeSegmentResponse(
            idx=s.idx, kind=s.kind, speaker=s.speaker,
            voice_id=s.voice_id or narrator_voice_id,
            text=s.text,
            audio_filename=fname, audio_url=url,
            duration_ms=dur_ms, confidence=s.confidence,
        ))
        if s.kind == "dialogue" and dlg_ptr < len(global_dialogues_sorted):
            dlg = global_dialogues_sorted[dlg_ptr]
            dlg.voice_id = s.voice_id
            dlg.audio_filename = fname
            dlg.duration_ms = dur_ms
            dlg_ptr += 1
    await session.commit()

    total_elapsed_ms = int((_time.perf_counter() - t0) * 1000)
    timing_str = " ".join(f"{k}={v}" for k, v in phase.items())
    logger.info(
        f"[synthesize] DONE job_id={job_id[:8]}... total_ms={total_elapsed_ms} {timing_str} "
        f"final_size_kb={len(final_bytes)//1024} duration_s={round(total_ms/1000,1)} "
        f"segments={len(results)}"
    )
    return SynthesizeResponse(
        job_id=job_id,
        audio_filename=final_name,
        audio_url=f"/media/{final_name}",
        duration_sec=round(total_ms / 1000.0, 2),
        segments=responses,
    )
