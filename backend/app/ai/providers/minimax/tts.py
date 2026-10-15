from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path
from typing import Any

import httpx

from ....core.config import get_active, get_provider, settings
from ...base import BaseTTSProvider
from ...factory import _minimax_rpm_wait_acquire

_BUILTIN_VOICES = (
    ("male-qn-qingse", "青涩青年音色", "male", "youth"),
    ("male-qn-jingying", "精英青年音色", "male", "youth"),
    ("male-qn-badao", "霸道青年音色", "male", "youth"),
    ("male-qn-daxuesheng", "青年大学生音色", "male", "youth"),
    ("female-shaonv", "少女音色", "female", "teen"),
    ("female-yujie", "御姐音色", "female", ""),
    ("female-chengshu", "成熟女性音色", "female", ""),
    ("female-tianmei", "甜美女性音色", "female", ""),
    ("male-qn-qingse-jingpin", "青涩青年音色-beta", "male", "youth"),
    ("male-qn-jingying-jingpin", "精英青年音色-beta", "male", "youth"),
    ("male-qn-badao-jingpin", "霸道青年音色-beta", "male", "youth"),
    ("male-qn-daxuesheng-jingpin", "青年大学生音色-beta", "male", "youth"),
    ("female-shaonv-jingpin", "少女音色-beta", "female", "teen"),
    ("female-yujie-jingpin", "御姐音色-beta", "female", ""),
    ("female-chengshu-jingpin", "成熟女性音色-beta", "female", ""),
    ("female-tianmei-jingpin", "甜美女性音色-beta", "female", ""),
    ("clever_boy", "聪明男童", "male", "child"),
    ("cute_boy", "可爱男童", "male", "child"),
    ("lovely_girl", "萌萌女童", "female", "child"),
    ("cartoon_pig", "卡通猪小琪", "", ""),
    ("bingjiao_didi", "病娇弟弟", "male", ""),
    ("junlang_nanyou", "俊朗男友", "male", ""),
    ("chunzhen_xuedi", "纯真学弟", "male", ""),
    ("lengdan_xiongzhang", "冷淡学长", "male", ""),
    ("badao_shaoye", "霸道少爷", "male", ""),
    ("tianxin_xiaoling", "甜心小玲", "", ""),
    ("qiaopi_mengmei", "俏皮萌妹", "female", ""),
    ("wumei_yujie", "妩媚御姐", "female", ""),
    ("diadia_xuemei", "嗲嗲学妹", "female", ""),
    ("danya_xuejie", "淡雅学姐", "female", ""),
    ("Chinese (Mandarin)_Reliable_Executive", "沉稳高管", "", ""),
    ("Chinese (Mandarin)_News_Anchor", "新闻主播", "", ""),
    ("Chinese (Mandarin)_Mature_Woman", "成熟女性", "female", ""),
    ("Chinese (Mandarin)_Unrestrained_Young_Man", "不羁青年", "male", "youth"),
    ("Arrogant_Miss", "嚣张小姐", "female", ""),
    ("Robot_Armor", "机械战甲", "", ""),
    ("Chinese (Mandarin)_Kind-hearted_Antie", "热心大婶", "female", ""),
    ("Chinese (Mandarin)_HK_Flight_Attendant", "港普乘务员", "", ""),
    ("Chinese (Mandarin)_Humorous_Elder", "搞笑长者", "", "senior"),
    ("Chinese (Mandarin)_Gentleman", "温润男声", "male", ""),
    ("Chinese (Mandarin)_Warm_Bestie", "温暖挚友", "", ""),
    ("Chinese (Mandarin)_Male_Announcer", "播报男声", "male", ""),
    ("Chinese (Mandarin)_Sweet_Lady", "甜美女声", "female", ""),
    ("Chinese (Mandarin)_Southern_Young_Man", "南方小哥", "male", "youth"),
    ("Chinese (Mandarin)_Wise_Women", "阅历姐姐", "female", ""),
    ("Chinese (Mandarin)_Gentle_Youth", "温润青年", "", "youth"),
    ("Chinese (Mandarin)_Warm_Girl", "温暖女孩", "female", ""),
    ("Chinese (Mandarin)_Kind-hearted_Elder", "热心长者", "", "senior"),
    ("Chinese (Mandarin)_Cute_Spirit", "憨憨萌兽", "", ""),
    ("Chinese (Mandarin)_Radio_Host", "电台主播", "", ""),
    ("Chinese (Mandarin)_Lyrical_Voice", "抒情音色", "", ""),
    ("Chinese (Mandarin)_Straightforward_Boy", "率真弟弟", "male", ""),
    ("Chinese (Mandarin)_Sincere_Adult", "真诚成人", "", ""),
    ("Chinese (Mandarin)_Gentle_Senior", "温柔长者", "", "senior"),
    ("Chinese (Mandarin)_Stubborn_Friend", "倔强朋友", "", ""),
    ("Chinese (Mandarin)_Crisp_Girl", "清脆女孩", "female", ""),
    ("Chinese (Mandarin)_Pure-hearted_Boy", "清澈邻家弟弟", "male", ""),
    ("Chinese (Mandarin)_Soft_Girl", "柔和女孩", "female", ""),
)

_EMOTION_ALIASES = {"": "calm", "neutral": "calm", "gentle": "calm", "cold": "calm"}

_EMOTIONS = frozenset({"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm"})


class MiniMaxTTSProvider(BaseTTSProvider):
    name = "minimax_tts"
    provider = "minimax"
    supports_instruction = False
    DEFAULT_MODEL = "speech-2.8-hd"
    MIN_SPEED = 0.5
    MAX_SPEED = 2.0
    MAX_RETRIES = 3

    def _api_config(self) -> tuple[str, dict[str, str]]:
        provider = get_provider("minimax") or {}
        key = str(provider.get("api_key") or settings.LLM_API_KEY).strip()
        if key.lower().startswith("bearer "):
            key = key.split(None, 1)[1].strip()
        if not key:
            raise RuntimeError("未配置 MiniMax API Key，请设置现有 MiniMax 厂商 key 或 LLM_API_KEY")
        base_url = str(provider.get("base_url") or settings.LLM_BASE_URL).rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        headers = dict(provider.get("extra_headers") or {})
        headers = {k: v for k, v in headers.items() if k.lower() not in {"authorization", "content-type"}}
        headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        return base_url, headers

    def _model(self) -> str:
        pid, mid, _ = get_active("tts")
        if pid == self.provider and mid and mid.startswith("speech-"):
            return mid
        return self.DEFAULT_MODEL

    def _builtin_voices_sync(self) -> list[dict[str, Any]]:
        return [
            {
                "id": f"minimax:{voice_id}",
                "name": name,
                "gender": gender,
                "age": age,
                "description": name,
                "provider": self.provider,
                "languages": ["zh"],
                "dialect": "",
                "scene": ["有声书"],
                "zh_tags": ["普通话"],
                "model": self._model(),
                "supports_emotion": True,
                "supports_instruction": False,
            }
            for voice_id, name, gender, age in _BUILTIN_VOICES
        ]

    async def list_voices(self) -> list[dict[str, Any]]:
        voices = self._builtin_voices_sync()
        provider = get_provider("minimax") or {}
        if not (provider.get("api_key") or settings.LLM_API_KEY):
            return voices
        try:
            base_url, headers = self._api_config()
            payload = await self._post(f"{base_url}/get_voice", headers, {"voice_type": "system"})
            remote_voices = payload.get("system_voice")
            if not isinstance(remote_voices, list):
                return voices
            available = {voice["id"]: voice for voice in voices}
            for remote in remote_voices:
                if not isinstance(remote, dict):
                    continue
                voice_id = remote.get("voice_id")
                if not isinstance(voice_id, str) or not voice_id.strip():
                    continue
                descriptions = remote.get("description") or []
                description = "；".join(str(d) for d in descriptions) if isinstance(descriptions, list) else str(descriptions)
                language = remote.get("language") or remote.get("languages") or ""
                features = f"{voice_id} {remote.get('voice_name') or ''} {description} {language}"
                known = available.get(f"minimax:{voice_id}")
                if known is None:
                    if re.search(r"\b(english|cantonese|japanese|korean|french|german|spanish|russian|arabic|portuguese)\b|粤语|粵語|英语|英語|日语|韩语", f"{voice_id} {remote.get('voice_name') or ''} {language}".replace("_", " "), re.IGNORECASE):
                        continue
                    if not re.search(r"中文|普通话|普通話|\bchinese\b|\bmandarin\b|\bzh(?:-cn)?\b", features.replace("_", " "), re.IGNORECASE):
                        continue
                    known = dict(voices[0], id=f"minimax:{voice_id}", name=voice_id, gender="", age="", description="")
                    voices.append(known)
                    available[known["id"]] = known
                known["description"] = description or known["description"]
                known["name"] = remote.get("voice_name") or known["name"]
                gender = remote.get("gender")
                if isinstance(gender, str):
                    mapped_gender = {"male": "male", "female": "female", "neutral": "neutral", "男": "male", "女": "female", "男性": "male", "女性": "female", "中性": "neutral"}.get(gender.strip().lower())
                    if mapped_gender:
                        known["gender"] = mapped_gender
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
            return self._builtin_voices_sync()
        return voices

    async def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.TTS_TIMEOUT) as client:
            for attempt in range(self.MAX_RETRIES):
                await _minimax_rpm_wait_acquire()
                response = await client.post(url, headers=headers, json=body)
                if (response.status_code == 429 or response.status_code >= 500) and attempt + 1 < self.MAX_RETRIES:
                    await asyncio.sleep(0.6 * 2 ** attempt)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("MiniMax 返回了无效 JSON 结构")
                status = payload.get("base_resp") or {}
                code = status.get("status_code")
                if code != 0:
                    if code in (1001, 1002) and attempt + 1 < self.MAX_RETRIES:
                        await asyncio.sleep(0.6 * 2 ** attempt)
                        continue
                    raise RuntimeError(f"MiniMax API 错误，status_code={code}")
                return payload
        raise RuntimeError("MiniMax API 请求失败")

    async def synthesize_to_bytes(
        self,
        text: str,
        voice_id: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> tuple[bytes, int]:
        self._set_usage(None)
        if not text.strip() or len(text) >= 10000:
            raise ValueError("MiniMax 合成文本须非空且少于 10000 字符")
        if ":" in voice_id:
            prefix, voice_id = voice_id.split(":", 1)
            if prefix.lower() != self.provider:
                raise ValueError("MiniMax 不支持其他厂商的音色前缀")
        if not voice_id:
            raise ValueError("MiniMax voice_id 不能为空")
        emotion = (emotion or "").strip().lower()
        emotion = _EMOTION_ALIASES.get(emotion, emotion)
        if emotion not in _EMOTIONS:
            raise ValueError(f"MiniMax 不支持 emotion={emotion}")
        if not math.isfinite(speed) or not self.MIN_SPEED <= speed <= self.MAX_SPEED:
            raise ValueError("MiniMax speed 必须位于 [0.5, 2.0]")
        base_url, headers = self._api_config()
        model = self._model()
        payload = await self._post(f"{base_url}/t2a_v2", headers, {
            "model": model,
            "text": text,
            "stream": False,
            "language_boost": "Chinese",
            "output_format": "hex",
            "voice_setting": {"voice_id": voice_id, "speed": speed, "vol": 1, "pitch": 0, "emotion": emotion},
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
        })
        extra = payload.get("extra_info") or {}
        usage_characters = extra.get("usage_characters")
        if isinstance(usage_characters, int) and not isinstance(usage_characters, bool) and usage_characters >= 0:
            self._set_usage({"provider": self.provider, "model": model, "usage_characters": usage_characters, "trace_id": payload.get("trace_id")})
        else:
            from ....services.build import track_tts_usage

            track_tts_usage(calls=1, chars=0)
        data = payload.get("data") or {}
        encoded = data.get("audio")
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("MiniMax 返回了空音频")
        try:
            audio = bytes.fromhex(encoded)
        except ValueError as exc:
            raise RuntimeError("MiniMax 返回了无效 hex 音频") from exc
        if not audio:
            raise RuntimeError("MiniMax 返回了空音频")
        duration = extra.get("audio_length")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration < 0:
            raise RuntimeError("MiniMax 返回了无效 audio_length")
        return audio, int(duration)

    async def synthesize_to_file(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        *,
        emotion: str = "calm",
        speed: float = 1.0,
        instruction_text: str | None = None,
        speaker_style: str | None = None,
    ) -> tuple[str, int]:
        audio, duration = await self.synthesize_to_bytes(
            text, voice_id, emotion=emotion, speed=speed,
            instruction_text=instruction_text, speaker_style=speaker_style,
        )
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return output_path, duration
