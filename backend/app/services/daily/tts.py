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

#: 段间静音（毫秒）：换段太硬会显得像机器拼接
SEGMENT_GAP_MS = 350
#: 单期最多合成段数（护栏：防止脚本异常导致上百次 TTS 调用）
MAX_SEGMENTS = 120

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;…\n])")


def split_segments(text: str, max_chars: int = 240) -> list[str]:
    """按句切分并打包成 ≤max_chars 的段（不切断句子）。"""
    max_chars = max(40, int(max_chars))
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s.strip()]
    segments: list[str] = []
    buf = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            # 单句过长：按逗号再切一次
            parts = [p.strip() for p in re.split(r"(?<=[，,])", sentence) if p.strip()]
            for part in parts:
                while len(part) > max_chars:
                    segments.append(part[:max_chars])
                    part = part[max_chars:]
                if part:
                    segments.append(part)
            buf = ""
            continue
        if len(buf) + len(sentence) <= max_chars:
            buf += sentence
        else:
            if buf:
                segments.append(buf)
            buf = sentence
    if buf:
        segments.append(buf)
    return segments[:MAX_SEGMENTS]


def _pick_voice(voices: list[dict[str, Any]]) -> str:
    """默认音色：优先"新闻/有声书"场景，其次第一个非童声。"""
    if not voices:
        raise RuntimeError("当前 TTS 厂商没有可用音色")
    for voice in voices:
        scenes = voice.get("scene") or []
        if isinstance(scenes, list) and any("新闻" in str(s) or "有声" in str(s) for s in scenes):
            return str(voice.get("id"))
    for voice in voices:
        if str(voice.get("age") or "") not in ("child", "teen"):
            return str(voice.get("id"))
    return str(voices[0].get("id"))


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
    segments = split_segments(text, settings.DAILY_TTS_SEGMENT_CHARS)
    if not segments:
        raise RuntimeError("口播稿为空，无法合成")

    tts = get_tts()
    voice = voice_id.strip()
    if not voice:
        voice = _pick_voice(await tts.list_voices())

    sem = get_tts_sem()
    parts: list[bytes] = []
    duration_ms = 0
    chars = 0
    silence = make_silent_mp3(SEGMENT_GAP_MS)

    for idx, segment in enumerate(segments):
        async with sem:
            data, dur = await tts.synthesize_to_bytes(segment, voice, emotion="calm", speed=1.0)
        if not data:
            raise RuntimeError(f"第 {idx + 1} 段合成返回空音频")
        parts.append(data)
        duration_ms += int(dur or 0)
        chars += len(segment)
        if idx < len(segments) - 1:
            parts.append(silence)
            duration_ms += SEGMENT_GAP_MS

    audio = concat_mp3_files(*parts)
    audio = await _normalize_loudness(audio)
    stats = {
        "segments": len(segments),
        "chars": chars,
        "voice_id": voice,
        "duration_ms": duration_ms,
    }
    logger.info("[daily] TTS 完成 segments=%d chars=%d dur=%.1fs", len(segments), chars, duration_ms / 1000)
    return audio, duration_ms, stats
