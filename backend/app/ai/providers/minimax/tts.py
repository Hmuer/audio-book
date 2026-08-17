import json
import io
import struct
import asyncio
import logging
import wave
import os
import time as _time
from collections import deque
from pathlib import Path
from typing import Any, Optional
import httpx

from ....core.config import settings
from ...base import BaseTTSProvider
from .onomatopoeia import apply_onomatopoeia

logger = logging.getLogger(__name__)

VOICES_FILE = Path(__file__).parent / "voices.json"

# 全局 RPM 限流：滑动窗口，60 秒内最多 TTS_RPM_LIMIT 次请求。
# 模块级单例，确保所有 TTS 调用共享同一计数。
_rpm_window: deque[float] = deque()
_rpm_lock: asyncio.Lock | None = None


def _get_rpm_lock() -> asyncio.Lock:
    """惰性初始化 RPM 锁（事件循环内创建，避免跨循环报错）。"""
    global _rpm_lock
    if _rpm_lock is None:
        _rpm_lock = asyncio.Lock()
    return _rpm_lock


async def _rpm_wait_acquire() -> None:
    """获取一个 RPM 令牌，必要时等待。滑动窗口：过去 60 秒内请求数 <= RPM_LIMIT。"""
    rpm_limit = max(1, int(settings.TTS_RPM_LIMIT))
    lock = _get_rpm_lock()
    async with lock:
        now = _time.monotonic()
        # 清理 60 秒之前的记录
        while _rpm_window and now - _rpm_window[0] >= 60.0:
            _rpm_window.popleft()
        if len(_rpm_window) >= rpm_limit:
            # 超过限制，等待直到最老的记录过期
            wait_s = 60.0 - (now - _rpm_window[0]) + 0.05
            logger.debug(
                f"[TTS] RPM 限流：等待 {wait_s:.1f}s "
                f"(limit={rpm_limit}/60s pending={len(_rpm_window)})"
            )
            await asyncio.sleep(wait_s)
            # 等待后再次清理
            now2 = _time.monotonic()
            while _rpm_window and now2 - _rpm_window[0] >= 60.0:
                _rpm_window.popleft()
        _rpm_window.append(_time.monotonic())


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

    def _is_rate_limit_error(self, e: Exception) -> bool:
        """判断错误是否为速率限制类（RPM/TPM 429、rate limit exceeded 等），
        这类错误应退避重试而非直接让整章失败。"""
        msg = str(e).lower()
        return any(kw in msg for kw in (
            "rate limit exceeded",
            "too many requests",
            "429",
            "rpm",
            "tpm",
            "quota",
        ))

    async def synthesize_to_bytes(
        self,
        text: str,
        voice_id: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
    ) -> bytes:
        t0 = _time.perf_counter()
        if not text.strip():
            # 空文本返回短静音
            logger.debug(f"[TTS] empty text, return silence voice_id={voice_id}")
            return make_silent_mp3(50)
        # 拟声词 → MiniMax Sound Tag（如"哈哈哈哈"→(laughs)）
        # 仅对 MiniMax Speech 2.x 系列有效，放在此处统一覆盖合成 + 试听两路调用
        text = apply_onomatopoeia(text, voice_id=voice_id)
        text_chars = len(text)
        # 官方文档模型名：speech-2.8-turbo / speech-2.8-hd / speech-02-turbo / speech-02-hd
        model = "speech-2.8-turbo"
        # MiniMax 官方 speed 范围 [0.5, 2.0]，默认 1.0
        speed = max(0.5, min(2.0, float(speed)))

        last_err: Optional[Exception] = None
        # 速率限制类错误最多重试 5 次，指数退避 2s/4s/8s/16s/32s
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            trace_id: Optional[str] = None
            http_status: Optional[int] = None
            try:
                # 先获取 RPM 令牌（全局滑动窗口限流）
                await _rpm_wait_acquire()

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
                                "speed": speed,
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
                        f"speed={speed} size={kb:.1f}KB audio_len_ms={audio_length_ms} "
                        f"trace_id={trace_id} status={http_status} attempt={attempt}/{max_retries} "
                        f"ms={int(elapsed*1000)}"
                    )
                    return audio_bytes
            except Exception as e:
                last_err = e
                elapsed = _time.perf_counter() - t0
                is_rate_limit = self._is_rate_limit_error(e)
                is_last = attempt == max_retries
                if is_rate_limit and not is_last:
                    # 速率限制：指数退避后重试
                    backoff_s = 2 ** attempt + 1  # 3s / 5s / 9s / 17s
                    lvl = logging.WARNING
                    msg = (
                        f"[TTS] RATE LIMIT model={model} voice={voice_id} chars={text_chars} "
                        f"attempt={attempt}/{max_retries} status={http_status} "
                        f"ms={int(elapsed*1000)} backoff={backoff_s}s "
                        f"{type(e).__name__}: {e}"
                    )
                    logger.log(lvl, msg)
                    await asyncio.sleep(backoff_s)
                    continue
                # 非速率限制 / 最后一次：记录并抛出
                lvl = logging.ERROR if is_last else logging.WARNING
                logger.log(
                    lvl,
                    f"[TTS] FAIL model={model} voice={voice_id} chars={text_chars} "
                    f"trace_id={trace_id} status={http_status} attempt={attempt}/{max_retries} "
                    f"ms={int(elapsed*1000)} {type(e).__name__}: {e}",
                    exc_info=is_last,
                )
                raise
        # 理论上不会到这里（循环内要么 return 要么 raise），兜底抛最后一次错误
        raise last_err or RuntimeError("TTS synthesize failed")

    async def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
    ) -> tuple[str, int]:
        data = await self.synthesize_to_bytes(text, voice_id, emotion=emotion, speed=speed)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        dur = _estimate_mp3_duration_ms(data)
        return output_path, dur
