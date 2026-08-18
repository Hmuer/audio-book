import json
import io
import struct
import asyncio
import logging
import wave
import os
import time as _time
import random
from pathlib import Path
from typing import Any, Optional
import httpx

from ....core.config import settings
from ...base import BaseTTSProvider
from .onomatopoeia import apply_onomatopoeia

logger = logging.getLogger(__name__)

VOICES_FILE = Path(__file__).parent / "voices.json"


# ---------- 全局 RPM 限流：固定间隔 token bucket ----------
# 核心思路：把"60 秒内最多 N 次"转化为"每 60/N 秒放 1 次"，严格匀速。
#
# 为什么滑动窗口实现有 bug：
#   多个协程通过 asyncio.gather 同时进入 _rpm_wait_acquire，
#   前 N 个几乎在同一毫秒内通过 → 第 N+1 个等 60s → 窗口清空后
#   所有等待协程瞬间涌入，形成"脉冲爆发"，MiniMax 按瞬时并发计 429。
#
# 固定间隔 token bucket 保证：
#   任意两个相邻请求之间至少间隔 (60 / RPM_LIMIT) 秒，零脉冲，零突发。
_rpm_next_allowed: float = 0.0
_rpm_lock: Optional[asyncio.Lock] = None
_rpm_interval: float = 0.0  # 延迟初始化


def _get_rpm_lock() -> asyncio.Lock:
    global _rpm_lock
    if _rpm_lock is None:
        _rpm_lock = asyncio.Lock()
    return _rpm_lock


def _get_rpm_interval() -> float:
    global _rpm_interval
    if _rpm_interval <= 0:
        limit = max(1, int(settings.TTS_RPM_LIMIT))
        # 每 interval 秒放一个请求。RPM_LIMIT=12 → interval=5.0s
        _rpm_interval = 60.0 / float(limit)
        logger.info(
            f"[TTS] RPM 限流初始化：RPM_LIMIT={limit}, interval={_rpm_interval:.2f}s"
        )
    return _rpm_interval


async def _rpm_wait_acquire() -> None:
    """发请求前调用：严格保证与上一次调用至少间隔 interval 秒。

    算法（固定间隔 token bucket）：
      interval = 60 / RPM_LIMIT
      now = monotonic()
      if now >= _next_allowed:
          _next_allowed = now + interval
          return  # 立即通过
      else:
          wait = _next_allowed - now
          _next_allowed += interval  # 提前预约下一次放行时间
          await asyncio.sleep(wait)
          # sleep 结束即视为已"放行"，不再额外等待
    """
    global _rpm_next_allowed
    interval = _get_rpm_interval()
    lock = _get_rpm_lock()
    async with lock:
        now = _time.monotonic()
        if now >= _rpm_next_allowed:
            _rpm_next_allowed = now + interval
            return
        wait_s = _rpm_next_allowed - now
        _rpm_next_allowed += interval
    # 释放锁后再 sleep，允许其他协程同时进入计算自己的 wait 时间
    # 每个协程的 _rpm_next_allowed 已经被预排，所以会严格串行放行
    logger.debug(
        f"[TTS] RPM 限流：等待 {wait_s:.2f}s (interval={interval:.2f}s)"
    )
    await asyncio.sleep(wait_s)


def _rpm_remaining_secs() -> float:
    """距离下一次允许发请求还有多少秒（429 兜底路径用）。"""
    global _rpm_next_allowed
    now = _time.monotonic()
    wait = _rpm_next_allowed - now
    return max(0.0, wait)


def _estimate_mp3_duration_ms(mp3_bytes: bytes) -> int:
    if len(mp3_bytes) < 128:
        return 0
    return int(len(mp3_bytes) * 8 / 96000 * 1000)


def make_silent_mp3(duration_ms: int) -> bytes:
    SILENT_FRAME_417 = (
        b"\xff\xfb\x90\x00"
        + b"\x00" * 413
    )
    FRAME_MS = 26
    frames_needed = max(1, int(duration_ms / FRAME_MS) + 1)
    return SILENT_FRAME_417 * frames_needed


def concat_mp3_files(*parts: bytes) -> bytes:
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
        speed: float = 1.0,
    ) -> tuple[bytes, int]:
        """合成音频，返回 (MP3 bytes, duration_ms)。

        duration_ms 优先使用 MiniMax 返回的 extra_info.audio_length（真实时长），
        缺失时回退到字节估算。
        """
        t0 = _time.perf_counter()
        if not text.strip():
            logger.debug(f"[TTS] empty text, return silence voice_id={voice_id}")
            return make_silent_mp3(50), 50
        text = apply_onomatopoeia(text, voice_id=voice_id)
        text_chars = len(text)
        model = "speech-2.8-turbo"
        speed = max(0.5, min(2.0, float(speed)))

        max_attempts = 5
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            trace_id: Optional[str] = None
            http_status: Optional[int] = None
            is_last_attempt = attempt == max_attempts
            try:
                # 固定间隔 token bucket：每次请求都严格等 interval 秒
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
                            "language_boost": "Chinese",
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
                    # 优先用 API 返回的真实时长，缺失时回退到字节估算
                    duration_ms = int(audio_length_ms) if audio_length_ms else _estimate_mp3_duration_ms(audio_bytes)
                    logger.info(
                        f"[TTS] ok model={model} voice={voice_id} chars={text_chars} "
                        f"speed={speed} size={kb:.1f}KB audio_len_ms={audio_length_ms} "
                        f"trace_id={trace_id} status={http_status} attempt={attempt} ms={int(elapsed*1000)}"
                    )
                    return audio_bytes, duration_ms

            except Exception as e:
                last_exc = e
                elapsed = _time.perf_counter() - t0
                if is_last_attempt:
                    logger.error(
                        f"[TTS] FAIL (final attempt={attempt}) model={model} voice={voice_id} chars={text_chars} "
                        f"trace_id={trace_id} status={http_status} ms={int(elapsed*1000)} "
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    raise
                err_str = str(e)
                is_rpm_limit = (
                    "rate limit exceeded" in err_str.lower()
                    and "(rpm)" in err_str.lower()
                ) or (http_status == 429 and "rpm" in err_str.lower())
                if is_rpm_limit:
                    # RPM 超限：按 token bucket 下次放行时间精确等待
                    # 再加 3s 官方统计偏移缓冲
                    wait_s = _rpm_remaining_secs() + 3.0
                    wait_s = max(wait_s, 5.0)
                    logger.warning(
                        f"[TTS] RPM 限流（attempt={attempt}/{max_attempts}）："
                        f"等待 {wait_s:.1f}s trace_id={trace_id}"
                    )
                    await asyncio.sleep(wait_s)
                else:
                    # 指数退避 + jitter（random 0-0.6s）防"惊群"
                    backoff_s = float(2 ** attempt + 1) + random.uniform(0, 0.6)
                    logger.warning(
                        f"[TTS] 重试（attempt={attempt}/{max_attempts}）："
                        f"指数退避 {backoff_s:.1f}s {type(e).__name__}: {e}"
                    )
                    await asyncio.sleep(backoff_s)
                continue

        assert last_exc is not None
        raise last_exc

    async def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
    ) -> tuple[str, int]:
        data, dur = await self.synthesize_to_bytes(text, voice_id, emotion=emotion, speed=speed)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写 .tmp 再 os.replace，避免崩溃留半成品
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, output_path)
        return output_path, dur
