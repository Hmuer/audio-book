"""MiniMax 官方系统中文音色与非流式 T2A 合成。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from ...base import BaseTTSProvider
from ....core.config import get_active, get_provider, settings
from ....core.mp3_util import mp3_duration_ms


_SYSTEM_VOICES = json.loads(Path(__file__).with_name("system_voices_zh.json").read_text(encoding="utf-8"))
_BY_ID = {v["id"]: v for v in _SYSTEM_VOICES}
BUILD_TTS_MODEL: ContextVar[str] = ContextVar("minimax_build_tts_model", default="")
SUPPORTED_TTS_MODELS = frozenset({
    "speech-2.8-hd", "speech-2.8-turbo", "speech-2.6-hd", "speech-2.6-turbo",
    "speech-02-hd", "speech-02-turbo",
})
logger = logging.getLogger(__name__)


class MiniMaxTTSRateLimitError(RuntimeError):
    """短时重试后仍被限流；构建 worker 可等待额度恢复后重试本章。"""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _is_quota_exhausted(status_code: int | None, message: str) -> bool:
    """MiniMax 套餐用量上限可能以 HTTP 429，也可能以 200 + base_resp 返回。"""
    normalized = message.lower()
    return (
        status_code == 2062
        or "token plan" in normalized
        or "token_plan" in normalized
        or "用量上限" in message
        or "额度用尽" in message
        or "额度不足" in message
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class _MiniMaxTTSPacer:
    """所有 MiniMax TTS 调用共享的匀速节流器，含上游 429 冷却窗口。"""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._next_allowed = 0.0
        self._cooldown_until = 0.0
        self._adaptive_rpm: int | None = None
        self._success_streak = 0

    async def acquire(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # 等待期间也可能有另一个请求触发限流；醒来后重新检查冷却截止时间。
            while True:
                now = time.monotonic()
                target = max(self._next_allowed, self._cooldown_until)
                if target <= now:
                    break
                await asyncio.sleep(target - now)
            # 锁保持到请求放行，避免大量等待者提前预约。
            configured_rpm = max(1, int(settings.MINIMAX_TTS_RPM_LIMIT))
            effective_rpm = min(configured_rpm, self._adaptive_rpm or configured_rpm)
            self._next_allowed = time.monotonic() + 60.0 / effective_rpm

    def cooldown(self, seconds: float = 65.0) -> None:
        until = time.monotonic() + seconds
        self._cooldown_until = max(self._cooldown_until, until)
        configured_rpm = max(1, int(settings.MINIMAX_TTS_RPM_LIMIT))
        self._adaptive_rpm = max(1, min(configured_rpm, self._adaptive_rpm or configured_rpm) // 2)
        self._success_streak = 0
        logger.warning("MiniMax TTS 节流已自动降至 %d RPM", self._adaptive_rpm)

    def note_success(self) -> None:
        """限流恢复后逐步回升，避免一次限流让本进程永久停在 1 RPM。"""
        if self._adaptive_rpm is None:
            return
        self._success_streak += 1
        if self._success_streak < 3:
            return
        self._success_streak = 0
        configured_rpm = max(1, int(settings.MINIMAX_TTS_RPM_LIMIT))
        next_rpm = self._adaptive_rpm + 1
        self._adaptive_rpm = None if next_rpm >= configured_rpm else next_rpm

    def reset(self) -> None:
        self._lock = None
        self._next_allowed = 0.0
        self._cooldown_until = 0.0
        self._adaptive_rpm = None
        self._success_streak = 0


_tts_pacer = _MiniMaxTTSPacer()


def _gender_for_system_voice(voice_id: str, name: str, description: str) -> str:
    known = _BY_ID.get(voice_id)
    if known:
        return known.get("gender") or "neutral"
    hint = " ".join((voice_id, name, description)).lower()
    female = ("female", "woman", "women", "lady", "girl", "miss", "女", "少女", "姐姐", "学妹")
    male = ("male", "man", "boy", "gentleman", "男", "小弟", "学弟", "大叔")
    if any(word in hint for word in female):
        return "female"
    if any(word in hint for word in male):
        return "male"
    return "neutral"


class MiniMaxTTSProvider(BaseTTSProvider):
    name = "minimax"
    provider = "minimax"

    def __init__(self) -> None:
        config = get_provider("minimax") or {}
        self.api_key = str(config.get("api_key") or "")
        base_url = str(config.get("base_url") or "https://api.minimax.cn/v1").rstrip("/")
        # 老配置的 .com 是 LLM 默认地址；中文语音文档使用 api.minimax.cn。
        self.base_url = ("https://api.minimax.cn/v1"
                         if base_url == "https://api.minimaxi.com/v1" else base_url)
        self._voices: list[dict[str, Any]] | None = None
        self._voices_at = 0.0

    def _url(self, endpoint: str) -> str:
        base = self.base_url
        return f"{base}{endpoint}" if base.endswith("/v1") else f"{base}/v1{endpoint}"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("MiniMax TTS 未配置 API Key")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def list_voices(self) -> list[dict[str, Any]]:
        """只展示系统中文音色；官方 API 有新增音色时动态补齐。"""
        if self._voices is not None and time.monotonic() - self._voices_at < 3600:
            return list(self._voices)
        found = {v["id"]: dict(v) for v in _SYSTEM_VOICES}
        if self.api_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(
                        self._url("/get_voice"), headers=self._headers(), json={"voice_type": "system"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                if (payload.get("base_resp") or {}).get("status_code", 0) != 0:
                    raise RuntimeError(str(payload["base_resp"]))
                for item in payload.get("system_voice") or []:
                    voice_id = str(item.get("voice_id") or "").strip()
                    raw_description = item.get("description") or []
                    if isinstance(raw_description, str):
                        raw_description = [raw_description]
                    description = "；".join(str(x) for x in raw_description)
                    # 官方查询包含多语种；新增的中文音色需有中文语言说明才补入。
                    if not voice_id or (voice_id not in found and not any(
                        word in f"{voice_id}；{description}" for word in
                        ("中文", "普通话", "粤语", "Mandarin", "Cantonese")
                    )):
                        continue
                    old = found.get(voice_id, {})
                    found[voice_id] = {
                        "id": voice_id,
                        "name": item.get("voice_name") or old.get("name") or voice_id,
                        "gender": old.get("gender") or _gender_for_system_voice(
                            voice_id, str(item.get("voice_name") or ""), description),
                        "language": old.get("language") or "zh-CN",
                        "description": description,
                    }
            except (httpx.HTTPError, ValueError, RuntimeError):
                # 官方静态中文系统清单仍可用于浏览；合成错误不在这里吞掉。
                pass
        self._voices = [
            {**v, "id": f"minimax:{v['id']}", "provider": "minimax",
             "languages": ["zh"], "scene": ["有声书"], "free": False}
            for v in found.values()
        ]
        self._voices_at = time.monotonic()
        return list(self._voices)

    async def synthesize_to_bytes(
        self, text: str, voice_id: str, *, emotion: str = "calm", speed: float = 1.0,
        instruction_text: str | None = None, speaker_style: str | None = None,
    ) -> tuple[bytes, int]:
        if not text.strip():
            raise ValueError("合成文本为空")
        if len(text) > 10_000:
            raise ValueError("MiniMax 同步合成单次最多 10000 字符，请先分段")
        bare_id = voice_id.removeprefix("minimax:")
        if bare_id not in {v["id"].removeprefix("minimax:") for v in await self.list_voices()}:
            raise ValueError(f"MiniMax 官方中文音色不存在: {voice_id}")
        _, active_model, _ = get_active("tts")
        model = BUILD_TTS_MODEL.get() or active_model
        if model not in SUPPORTED_TTS_MODELS:
            raise RuntimeError("设置页未激活有效的 MiniMax 语音模型")
        voice_setting: dict[str, Any] = {"voice_id": bare_id, "speed": speed, "vol": 1, "pitch": 0}
        if emotion and emotion in {"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"}:
            voice_setting["emotion"] = emotion
        payload = {
            "model": model, "text": text, "stream": False,
            "text_normalization_mode": "quality" if model.startswith(("speech-2.8", "speech-2.6")) else "basic",
            "voice_setting": voice_setting,
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
        }
        # 并发 semaphore 只限制在飞数量，无法限制每分钟调用次数。
        # 在这里统一节流，覆盖构建和音色试听；上游限流时冷却后重试同一段。
        async with httpx.AsyncClient(timeout=180) as client:
            for attempt in range(3):
                await _tts_pacer.acquire()
                response = await client.post(self._url("/t2a_v2"), headers=self._headers(), json=payload)
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    # 套餐额度耗尽时，本地 65 秒 RPM 冷却和同段短重试都无用。
                    # 交给 build 的长等待循环；试听调用方收到明确的限额错误。
                    if _is_quota_exhausted(None, response.text):
                        raise MiniMaxTTSRateLimitError(
                            "MiniMax TTS Token Plan 用量上限，等待额度恢复", retry_after,
                        )
                    # 长 Retry-After 交给构建层等待，那里能展示进度并响应取消。
                    _tts_pacer.cooldown()
                    if retry_after is not None and retry_after > 65:
                        raise MiniMaxTTSRateLimitError("MiniMax TTS HTTP 429：请求受限", retry_after)
                    if attempt < 2:
                        logger.warning("MiniMax TTS HTTP 429，冷却后重试（%d/2）", attempt + 1)
                        continue
                    raise MiniMaxTTSRateLimitError("MiniMax TTS HTTP 429：请求受限", retry_after)
                response.raise_for_status()
                data = response.json()
                base_resp = data.get("base_resp") or {}
                status_msg = str(base_resp.get("status_msg") or base_resp.get("status_code") or "")
                if base_resp.get("status_code", 0) != 0:
                    if _is_quota_exhausted(base_resp.get("status_code"), status_msg):
                        raise MiniMaxTTSRateLimitError(
                            f"MiniMax TTS 用量上限：{status_msg}",
                            _retry_after_seconds(response.headers.get("Retry-After")),
                        )
                    if "rate limit" in status_msg.lower():
                        _tts_pacer.cooldown()
                        if attempt < 2:
                            logger.warning("MiniMax TTS RPM 限流，冷却 65 秒后重试（%d/2）", attempt + 1)
                            continue
                        raise MiniMaxTTSRateLimitError(f"MiniMax 合成失败: {status_msg}")
                    raise RuntimeError(f"MiniMax 合成失败: {status_msg}")
                break
        audio_hex = (data.get("data") or {}).get("audio")
        if not audio_hex:
            raise RuntimeError("MiniMax 合成返回空音频")
        audio = bytes.fromhex(audio_hex)
        duration = int((data.get("extra_info") or {}).get("audio_length") or 0) or mp3_duration_ms(audio)
        _tts_pacer.note_success()
        return audio, duration

    async def synthesize_to_file(
        self, text: str, voice_id: str, output_path: str, *, emotion: str = "calm",
        speed: float = 1.0, instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> tuple[str, int]:
        audio, duration = await self.synthesize_to_bytes(text, voice_id, emotion=emotion, speed=speed)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return str(path), duration
