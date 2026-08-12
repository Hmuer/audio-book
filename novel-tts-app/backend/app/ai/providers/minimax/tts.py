import json
import io
import struct
import asyncio
import logging
import wave
import os
from pathlib import Path
from typing import Any
import httpx

from ....core.config import settings
from ...base import BaseTTSProvider

logger = logging.getLogger(__name__)

VOICES_FILE = Path(__file__).parent / "voices.json"


def _estimate_mp3_duration_ms(mp3_bytes: bytes) -> int:
    """
    简化版：按 128kbps 比特率估算 MP3 时长。
    MiniMax TTS 返回的 MP3 多为 24kHz/单声道/约 32-128kbps；
    用字节数 * 8 / 96000 * 1000 估算（偏保守中位 96kbps）。
    """
    if len(mp3_bytes) < 128:
        return 0
    return int(len(mp3_bytes) * 8 / 96000 * 1000)


def make_silent_mp3(duration_ms: int) -> bytes:
    """
    生成指定毫秒数的静音 MP3。
    策略：用 8kHz/16bit/单声道 生成静音 PCM，写入 WAV 头后读取，
    由于本项目不引入 lame/ffmpeg 依赖，这里生成一个合法的"静音帧堆"：
    简化方案 —— 用 MPEG1 Layer3 128kbps 44.1kHz 静音帧模板拼接。

    标准 MPEG1 L3 128kbps 44.1kHz 单帧 = 26ms，417 bytes。
    """
    # 单个静音帧（MPEG1 L3 44.1kHz 128kbps mono silent），共 417 bytes
    # 来源：libavcodec ff_mp3_default_enc 静音帧头 + 填充
    SILENT_FRAME_417 = (
        b"\xff\xfb\x90\x00"
        + b"\x00" * 413
    )
    FRAME_MS = 26
    frames_needed = max(1, int(duration_ms / FRAME_MS) + 1)
    return SILENT_FRAME_417 * frames_needed


def concat_mp3_files(*parts: bytes) -> bytes:
    """直接拼接 MP3 帧（MP3 支持流式拼接，大多数播放器都兼容）"""
    out = bytearray()
    for p in parts:
        if p:
            out.extend(p)
    return bytes(out)


class MiniMaxTTSProvider(BaseTTSProvider):
    name = "minimax"

    def __init__(self):
        self.api_key = settings.TTS_API_KEY
        self.base_url = settings.TTS_BASE_URL.rstrip("/")
        self.timeout = httpx.Timeout(
            connect=settings.TTS_TIMEOUT,
            read=settings.TTS_TIMEOUT,
            write=settings.TTS_TIMEOUT,
            pool=settings.TTS_TIMEOUT,
        )
        self._voices: list[dict[str, Any]] | None = None

    async def list_voices(self) -> list[dict[str, Any]]:
        if self._voices is None:
            with open(VOICES_FILE, "r", encoding="utf-8") as f:
                self._voices = json.load(f)
        return self._voices

    async def synthesize_to_bytes(self, text: str, voice_id: str) -> bytes:
        if not text.strip():
            # 空文本返回短静音
            return make_silent_mp3(50)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/t2a_stream",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "speech-02",
                    "voice_id": voice_id,
                    "text": text,
                    "speed": 1.0,
                    "vol": 1.0,
                    "pitch": 0,
                    "sample_rate": 24000,
                    "bitrate": 128000,
                    "format": "mp3",
                },
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"TTS HTTP {resp.status_code}: {resp.text[:500]}"
                )
            return resp.content

    async def synthesize_to_file(
        self, text: str, voice_id: str, output_path: str
    ) -> tuple[str, int]:
        data = await self.synthesize_to_bytes(text, voice_id)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        dur = _estimate_mp3_duration_ms(data)
        return output_path, dur
