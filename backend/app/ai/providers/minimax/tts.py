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


# ---------- 全局 RPM 限流：滑动窗口（60s） ----------
# MiniMax t2a_v2 是按 60 秒窗口计 RPM，官方充值用户 = 20 RPM，免费 = 10 RPM。
# 做两层保障：
#   1) 发请求前先走 _rpm_wait_acquire：窗口内还没达到 TTS_RPM_LIMIT 才放行；
#      达到则精确等到"窗口内最老请求过期 + 缓冲"再继续。
#   2) 即便如此官方仍可能 429（多个服务共享 key、官方自身统计偏移等），
#      synthesize 层在捕获 "rate limit exceeded(RPM)" 后，
#      按 _rpm_window[0] 精确算还要等多少秒（而不是短退避重试 5 次浪费次数）。
_rpm_window: deque[float] = deque()
_rpm_lock: Optional[asyncio.Lock] = None


def _get_rpm_lock() -> asyncio.Lock:
    """懒初始化 asyncio.Lock：必须在事件循环存在之后创建（否则不同事件循环会死锁）。"""
    global _rpm_lock
    if _rpm_lock is None:
        _rpm_lock = asyncio.Lock()
    return _rpm_lock


def _rpm_window_remaining_secs(now: float | None = None) -> float:
    """返回还需要等多少秒，窗口才能释放出至少 1 个额度。若当前还有额度返回 0。

    这个函数故意不拿锁，调用方必须自己持锁 / 或接受略脏读（在 429 兜底路径使用脏读没问题，
    因为等久一点总比等短了又 429 强）。
    """
    if now is None:
        now = _time.monotonic()
    # 清理 60 秒之前的记录（不 popleft，只是检查逻辑上还在窗口内的请求数）
    cut = now - 60.0
    active_pending = 0
    oldest_active_ts: Optional[float] = None
    for ts in _rpm_window:
        if ts >= cut:
            active_pending += 1
            if oldest_active_ts is None:
                oldest_active_ts = ts
    limit = max(1, int(settings.TTS_RPM_LIMIT))
    if active_pending < limit:
        return 0.0
    # 额度满了：最老的请求再过 (60 - (now - oldest)) 秒 + 安全缓冲 0.1s 才会过期
    assert oldest_active_ts is not None
    wait = 60.0 - (now - oldest_active_ts) + 0.1
    return max(0.05, wait)


async def _rpm_wait_acquire() -> None:
    """发请求前调用：若窗口内请求数已达 TTS_RPM_LIMIT，则阻塞 sleep 到下一次窗口过期。"""
    limit = max(1, int(settings.TTS_RPM_LIMIT))
    lock = _get_rpm_lock()
    async with lock:
        now = _time.monotonic()
        # 清理 60 秒之前的记录
        while _rpm_window and now - _rpm_window[0] >= 60.0:
            _rpm_window.popleft()
        if len(_rpm_window) >= limit:
            wait_s = 60.0 - (now - _rpm_window[0]) + 0.1
            logger.debug(
                f"[TTS] RPM 主动限流：等待 {wait_s:.1f}s (limit={limit}/60s pending={len(_rpm_window)})"
            )
            # 注意：sleep 时释放锁，让其他协程也能判断（他们都会算出相同的 wait_s，
            # 然后依次串行地在各自的 acquire 里排队拿锁→清理→判断→放行→append）。
            await asyncio.sleep(wait_s)
            now2 = _time.monotonic()
            while _rpm_window and now2 - _rpm_window[0] >= 60.0:
                _rpm_window.popleft()
        # 无论是否 sleep 过，最终 append 本次请求的开始时间戳
        _rpm_window.append(_time.monotonic())


async def _rpm_mark_attempt() -> None:
    """非 acquire 路径（429 重试）重发请求时也追加一次时间戳。

    主动限流路径在 _rpm_wait_acquire 里已经 append 过了；重试之前这里再记一次，
    保证"失败重试"也会占用自己的窗口额度，避免重试浪把官方额度打死。
    """
    lock = _get_rpm_lock()
    async with lock:
        now = _time.monotonic()
        while _rpm_window and now - _rpm_window[0] >= 60.0:
            _rpm_window.popleft()
        _rpm_window.append(now)


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

        # 最大尝试次数：首次 + 4 次重试 = 共 5 次。
        # 对 "rate limit exceeded(RPM)"，每次都精确等到窗口最老请求过期，
        # 所以重试次数不用多，一般 1~2 次就能过。
        max_attempts = 5
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            trace_id: Optional[str] = None
            http_status: Optional[int] = None
            is_last_attempt = attempt == max_attempts
            try:
                # ---------- 层 1：发请求前主动 RPM 限流 ----------
                # 首次尝试走 _rpm_wait_acquire：窗口满则精确 sleep 到过期再放。
                # 429 重试时也要再走一次（否则重试瞬间就又撞到窗口）。
                await _rpm_wait_acquire()

                # 真正发起 HTTP
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
                        f"trace_id={trace_id} status={http_status} attempt={attempt} ms={int(elapsed*1000)}"
                    )
                    return audio_bytes

            except Exception as e:
                last_exc = e
                elapsed = _time.perf_counter() - t0
                # 不是最后一次尝试：按错误类型 sleep，再 retry
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
                # ---------- 层 2：精确等窗口过期 / 指数退避 ----------
                if is_rpm_limit:
                    # RPM 耗尽：按窗口最老请求算剩余秒数，精确睡到窗口过期再试
                    wait_s = _rpm_window_remaining_secs()
                    # 官方自己的窗口统计和我们可能有几秒钟偏移，再额外加 2s 缓冲
                    wait_s = max(wait_s + 2.0, 5.0)
                    logger.warning(
                        f"[TTS] RPM 限流（attempt={attempt}/{max_attempts}）："
                        f"精确等待 {wait_s:.1f}s 到下一个窗口 trace_id={trace_id}"
                    )
                    await asyncio.sleep(wait_s)
                else:
                    # 其他错误（HTTP 5xx、网络超时等）：指数退避 3s/5s/9s/17s
                    backoff_s = float(2 ** attempt + 1)  # attempt=1→3, 2→5, 3→9, 4→17
                    logger.warning(
                        f"[TTS] 重试（attempt={attempt}/{max_attempts}）："
                        f"指数退避 {backoff_s:.1f}s {type(e).__name__}: {e}"
                    )
                    await asyncio.sleep(backoff_s)
                continue

        # 理论走不到这里（循环最后一次会 raise），兜底
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
        data = await self.synthesize_to_bytes(text, voice_id, emotion=emotion, speed=speed)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        dur = _estimate_mp3_duration_ms(data)
        return output_path, dur
