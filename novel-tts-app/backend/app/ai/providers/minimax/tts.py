import json
import io
import struct
import asyncio
import logging
import wave
import os
from pathlib import Path
from typing import Any, Optional
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

    async def synthesize_to_bytes(
        self,
        text: str,
        voice_id: str,
        *,
        emotion: str = "calm",
    ) -> bytes:
        import time as _time
        t0 = _time.perf_counter()
        if not text.strip():
            # 空文本返回短静音
            logger.debug(f"[TTS] empty text, return silence voice_id={voice_id}")
            return make_silent_mp3(50)
        trace_id: Optional[str] = None
        http_status: Optional[int] = None
        text_chars = len(text)
        # 官方文档模型名：speech-2.8-turbo / speech-2.8-hd / speech-02-turbo / speech-02-hd
        model = "speech-2.8-turbo"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/t2a_v2",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "text": text,
                        "stream": False,
                        "voice_setting": {
                            "voice_id": voice_id,
                            "speed": 1.0,
                            "vol": 1.0,
                            "pitch": 0,
                            "emotion": emotion,
                        },
                        "audio_setting": {
                            "sample_rate": 32000,
                            "bitrate": 128000,
                            "format": "mp3",
                            "channel": 1,
                        },
                        # output_format 默认 hex，非流式下 data.audio 是 hex 编码音频
                    },
                )
                http_status = resp.status_code
                trace_id = (
                    resp.headers.get("x-request-id")
                    or resp.headers.get("X-Request-Id")
                    or None
                )
                if resp.status_code >= 400:
                    try:
                        err_data = resp.json()
                        trace_id = err_data.get("trace_id") or trace_id
                        base_msg = (
                            err_data.get("base_resp", {}).get("status_msg")
                            or err_data.get("error", {}).get("message")
                            or None
                        )
                        err_msg = base_msg or resp.text[:500]
                    except Exception:
                        err_msg = resp.text[:500]
                    raise RuntimeError(
                        f"TTS HTTP {resp.status_code}: {err_msg}"
                    )
                # 官方 t2a_v2 非流式返回 JSON: data.audio = hex编码音频
                try:
                    resp_json = resp.json()
                except Exception as je:
                    raise RuntimeError(
                        f"TTS 响应不是有效 JSON (status={http_status}): {resp.text[:200]}"
                    ) from je
                trace_id = resp_json.get("trace_id") or trace_id
                data = resp_json.get("data") or {}
                hex_audio = data.get("audio")
                if not hex_audio:
                    # fallback: output_format=url 可能返回 URL 而非 hex
                    # 若 data 为 null 或缺失 audio 再尝试从 base_resp 看错误
                    base_resp = resp_json.get("base_resp") or {}
                    if base_resp.get("status_code", 0) != 0:
                        raise RuntimeError(
                            f"TTS 错误: {base_resp.get('status_msg') or resp.text[:200]}"
                        )
                    raise RuntimeError(
                        f"TTS 响应缺失 data.audio: {resp_json}"
                    )
                audio_bytes = bytes.fromhex(hex_audio)
                kb = len(audio_bytes) / 1024
                elapsed = _time.perf_counter() - t0
                extra_info = resp_json.get("extra_info") or {}
                audio_length_ms = extra_info.get("audio_length")
                logger.info(
                    f"[TTS] ok model={model} voice={voice_id} chars={text_chars} "
                    f"size={kb:.1f}KB audio_len_ms={audio_length_ms} "
                    f"trace_id={trace_id} status={http_status} ms={int(elapsed*1000)}"
                )
                return audio_bytes
        except Exception as e:
            elapsed = _time.perf_counter() - t0
            logger.error(
                f"[TTS] FAIL model={model} voice={voice_id} chars={text_chars} "
                f"trace_id={trace_id} status={http_status} ms={int(elapsed*1000)} "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

    async def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        *,
        emotion: str = "calm",
    ) -> tuple[str, int]:
        data = await self.synthesize_to_bytes(text, voice_id, emotion=emotion)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        dur = _estimate_mp3_duration_ms(data)
        return output_path, dur
