"""TTS：分段合成 → 拼接 → 可选响度标准化。

复用平台已有的 TTS 厂商（`factory.get_tts`）与 MP3 工具，不引入新依赖：
- 分段：按句末标点切，再按 `DAILY_TTS_SEGMENT_CHARS` 打包（过长会被厂商拒）；
- 拼接：`mp3_util.concat_mp3_files`，段间插 350ms 静音；
- 标准化：有 ffmpeg 时做 `loudnorm -16 LUFS`，没有就跳过（不阻塞出片）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ...ai.factory import get_tts, get_tts_sem
from ...core.config import settings
from ...core.mp3_util import concat_mp3_files, make_silent_mp3

logger = logging.getLogger(__name__)

#: 单期最多合成块数（护栏：防止脚本异常导致上百次 TTS 调用）
MAX_SEGMENTS = 120

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;…\n])")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def split_chunks(text: str, max_chars: int = 500) -> list[tuple[str, bool]]:
    """把口播稿切成大块（≤max_chars），返回 [(chunk, 是否段落结尾)]。

    关键点：
    - 先把所有空白归一化：连续换行视作段落分隔，单换行视作空格
      （LLM 常把每句话都写成独立段落，直接按空行切会得到几十个碎块）；
    - 按句号切句，再把句子打包成 ~max_chars 的大块，块越大语气越连贯；
    - 只有块长 ≥ 260 字时才在"真正的段落边界"处断开，避免一句话一块；
    - 块尾标出是否落在段落边界，供合成后决定"句间停"还是"段间停"。
    """
    max_chars = max(160, int(max_chars))
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # 用一个不常见标记保留"真正的段落边界"，其余空白压成单空格
    marked = re.sub(r"\n[ \t]*\n+", "\u0001", normalized)
    marked = re.sub(r"\s+", " ", marked)
    paragraphs = [p.strip() for p in marked.split("\u0001") if p.strip()]
    if not paragraphs:
        return []

    # 逐句展开：记录该句之后是否紧跟段落边界
    units: list[tuple[str, bool]] = []
    for pi, paragraph in enumerate(paragraphs):
        sentences = [x.strip() for x in _SENTENCE_SPLIT.split(paragraph) if x.strip()]
        for si, sentence in enumerate(sentences):
            # 最后一句结束于段落边界
            units.append((sentence, si == len(sentences) - 1))
    if not units:
        return []

    chunks: list[tuple[str, bool]] = []
    buf = ""
    buf_para_end = False
    for sentence, para_break in units:
        if len(sentence) > max_chars:
            if buf:
                chunks.append((buf, buf_para_end))
                buf = ""
            parts = [x.strip() for x in re.split(r"(?<=[，,])", sentence) if x.strip()]
            local = ""
            for part in parts:
                if len(local) + len(part) <= max_chars:
                    local += part
                else:
                    if local:
                        chunks.append((local, False))
                    local = part
            buf = local
            buf_para_end = para_break
            continue
        # 块接近上限，或已在段落边界且块足够长 → 断块
        if len(buf) + len(sentence) > max_chars or (buf and buf_para_end and para_break and len(buf) >= 260):
            chunks.append((buf, buf_para_end))
            buf = ""
            buf_para_end = False
        buf += sentence
        buf_para_end = para_break
    if buf:
        chunks.append((buf, buf_para_end))
        buf = ""
    return chunks[:MAX_SEGMENTS]


def split_segments(text: str, max_chars: int = 240) -> list[str]:
    """兼容旧测试：只返回文本块。"""
    return [chunk for chunk, _ in split_chunks(text, max_chars)]


def _gap_ms(base: int, seed: str, spread: int = 90) -> int:
    """给停顿加一点确定性抖动（同一文本可复现），避免节拍器般的机械感。"""
    jitter = (hash(seed) % (spread * 2 + 1)) - spread
    return max(120, int(base) + jitter)


def _pick_voice(voices: list[dict[str, Any]]) -> str:
    """默认音色：复用偏好层的"新闻/播报优先"策略，避免两处规则漂移。"""
    from .prefs import pick_default_voice

    return pick_default_voice(voices)


async def _normalize_loudness(mp3: bytes) -> bytes:
    """有 ffmpeg 时做 -16 LUFS 标准化；失败/缺失返回原音频。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return mp3

    def _run() -> bytes:
        with tempfile.TemporaryDirectory(prefix="daily-tts-") as tmp:
            src = Path(tmp) / "in.mp3"
            dst = Path(tmp) / "out.mp3"
            src.write_bytes(mp3)
            proc = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(src),
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "44100", "-b:a", "128k",
                    str(dst),
                ],
                capture_output=True,
                timeout=180,
            )
            if proc.returncode != 0 or not dst.is_file():
                raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
            return dst.read_bytes()

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("[daily] 响度标准化失败，使用原始音频: %s", e)
        return mp3


async def synthesize(text: str, *, voice_id: str = "") -> tuple[bytes, int, dict[str, Any]]:
    """合成整期音频，返回 (mp3 bytes, duration_ms, stats)。"""
    tts = get_tts()
    voices = await tts.list_voices()
    voice = voice_id.strip()
    if not voice:
        voice = _pick_voice(voices)
    from .prefs import voice_name_of

    voice_name = voice_name_of(voices, voice)

    sem = get_tts_sem()
    parts: list[bytes] = []
    duration_ms = 0
    chars = 0
    chunks = split_chunks(text, settings.DAILY_TTS_CHUNK_CHARS)
    if not chunks:
        raise RuntimeError("口播稿为空，无法合成")
    speed = float(settings.DAILY_TTS_SPEED or 1.0)
    emotion = (settings.DAILY_TTS_EMOTION or "calm").strip() or "calm"
    sentence_gap = int(settings.DAILY_TTS_SENTENCE_GAP_MS or 350)
    paragraph_gap = int(settings.DAILY_TTS_PARAGRAPH_GAP_MS or 900)
    #: 分块计时（供字幕 SRT 使用）：每块开始时间 + 真实时长
    timings: list[dict[str, Any]] = []

    for idx, (segment, para_end) in enumerate(chunks):
        async with sem:
            data, dur = await tts.synthesize_to_bytes(
                segment, voice, emotion=emotion, speed=speed,
            )
        if not data:
            raise RuntimeError(f"第 {idx + 1} 段合成返回空音频")
        parts.append(data)
        timings.append({
            "text": segment,
            "start_ms": duration_ms,
            "duration_ms": int(dur or 0),
        })
        duration_ms += int(dur or 0)
        chars += len(segment)
        if idx < len(chunks) - 1:
            gap = paragraph_gap if para_end else sentence_gap
            gap = _gap_ms(gap, segment)
            parts.append(make_silent_mp3(gap))
            duration_ms += gap

    audio = concat_mp3_files(*parts)
    audio = await _normalize_loudness(audio)
    stats = {
        "segments": len(chunks),
        "chars": chars,
        "voice_id": voice,
        "voice_name": voice_name,
        "duration_ms": duration_ms,
        "segment_timings": timings,
    }
    logger.info("[daily] TTS 完成 chunks=%d chars=%d dur=%.1fs", len(chunks), chars, duration_ms / 1000)
    return audio, duration_ms, stats
