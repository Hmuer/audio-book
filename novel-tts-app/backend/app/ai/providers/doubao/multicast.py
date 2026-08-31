"""豆包 Seed-Audio 1.0 多播剧 Provider（多角色章节一体化生成）。

职责：
  - build_prompt(segments)：把章节 segment 列表转为多角色剧本 prompt
    （"旁白：…" / "角色名：台词"，跳过静音段）。
  - synthesize_chapter_to_file(segments, output_path)：整章一次调用 Seed-Audio
    生成 MP3（人声 + 情绪 + 背景氛围一体化，单次 ≤120s 音频）。
    - roles 数组携带每个角色的音色（剥离 doubao:/icl: 前缀后传真实 id）
    - speed / instruction_text（导演级风格指令）支持
    - 走 factory._doubao_rpm_wait_acquire("seed_audio") 限流
    - 原子写（.tmp → os.replace）
"""
from __future__ import annotations

import logging
import os
import time as _time
from pathlib import Path
from typing import Any

from backend.app.ai.factory import _doubao_rpm_wait_acquire
from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# Seed-Audio 单次生成上限（秒）：超长章节需要业务层分段
MAX_CHAPTER_AUDIO_SECS = 120


class DoubaoMulticastProvider:
    name = "doubao_multicast"
    provider = "doubao"

    MAX_RETRIES = 3
    BASE_BACKOFF_SECS = 1.0
    JITTER_SECS = 0.5

    @property
    def base_url(self) -> str:
        return (settings.DOUBAO_SEED_AUDIO_BASE_URL or "").rstrip("/")

    # -----------------------------------------------------------------
    # prompt 构建
    # -----------------------------------------------------------------
    def build_prompt(self, segments: list[dict[str, Any]], *, chapter_title: str = "") -> str:
        """把 segments 转为多角色剧本 prompt。跳过 silence 段。"""
        lines: list[str] = []
        if chapter_title:
            lines.append(f"《{chapter_title}》")
        for s in segments or []:
            kind = (s.get("kind") or "").lower()
            if kind == "silence":
                continue
            text = (s.get("text") or "").strip()
            if not text:
                continue
            speaker = (s.get("speaker") or "").strip()
            role = speaker if speaker else "旁白"
            lines.append(f"{role}：{text}")
        return "\n".join(lines)

    def _collect_roles(self, segments: list[dict[str, Any]]) -> list[dict[str, str]]:
        """按出现顺序收集角色 → 音色（剥离前缀）。旁白/标题归入 narrator 音色。"""
        seen: dict[str, str] = {}
        roles: list[dict[str, str]] = []
        for s in segments or []:
            kind = (s.get("kind") or "").lower()
            if kind == "silence":
                continue
            vid = s.get("voice_id")
            if not vid:
                continue
            speaker = (s.get("speaker") or "").strip()
            role = speaker if speaker else "旁白"
            if role in seen:
                continue
            seen[role] = vid
            roles.append({"name": role, "voice": self._strip_prefix(vid)})
        return roles

    @staticmethod
    def _strip_prefix(voice_id: str) -> str:
        if voice_id and ":" in voice_id:
            prefix, rest = voice_id.split(":", 1)
            if prefix.lower() in ("doubao", "icl"):
                return rest
        return voice_id

    # -----------------------------------------------------------------
    # 整章合成
    # -----------------------------------------------------------------
    def _build_payload(
        self,
        segments: list[dict[str, Any]],
        *,
        speed: float = 1.0,
        chapter_title: str = "",
        instruction_text: str | None = None,
    ) -> dict[str, Any]:
        prompt = self.build_prompt(segments, chapter_title=chapter_title)
        return {
            "model": "seed-audio-1.0",
            "prompt": prompt,
            "roles": self._collect_roles(segments),
            "audio_config": {
                "format": "mp3",
                "sample_rate": 24000,
                "max_duration_secs": MAX_CHAPTER_AUDIO_SECS,
            },
            "speed_ratio": max(0.2, min(3.0, float(speed or 1.0))),
            "instruction_text": instruction_text or "",
            "reqid": f"mc-{int(_time.time()*1000)}",
        }

    async def synthesize_chapter_to_file(
        self,
        segments: list[dict[str, Any]],
        output_path: str,
        *,
        speed: float = 1.0,
        chapter_title: str = "",
        instruction_text: str | None = None,
    ) -> tuple[str, int]:
        """整章多角色一次性生成 MP3，返回 (output_path, duration_ms)。原子写。"""
        import asyncio

        payload = self._build_payload(
            segments, speed=speed, chapter_title=chapter_title,
            instruction_text=instruction_text,
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._authorization(),
        }
        url = f"{self.base_url}/generate"

        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                await _doubao_rpm_wait_acquire("seed_audio")
                mp3_bytes = await self._http_post_bytes(url, headers, payload)
                dur_ms = int(len(mp3_bytes) / 16.0)
                tmp_path = output_path + ".tmp"
                try:
                    with open(tmp_path, "wb") as f:
                        f.write(mp3_bytes)
                    os.replace(tmp_path, output_path)
                finally:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                return output_path, dur_ms
            except Exception as e:
                last_exc = e
                resp_obj = getattr(e, "response", None)
                status_code = int(getattr(resp_obj, "status_code", 0)) if resp_obj is not None else 0
                is_retryable = status_code == 429 or 500 <= status_code < 600
                if attempt >= self.MAX_RETRIES or not is_retryable:
                    break
                wait_s = self.BASE_BACKOFF_SECS * (2 ** (attempt - 1)) + self.JITTER_SECS
                logger.warning(
                    f"[DoubaoMulticast] 重试 attempt={attempt}/{self.MAX_RETRIES} "
                    f"退避 {wait_s:.1f}s：{type(e).__name__}: {e}"
                )
                await asyncio.sleep(wait_s)
        assert last_exc is not None
        raise RuntimeError(
            f"Seed-Audio 多播剧合成失败：{type(last_exc).__name__}: {last_exc}"
        ) from last_exc

    # -----------------------------------------------------------------
    # HTTP / 凭据
    # -----------------------------------------------------------------
    def _authorization(self) -> str:
        ak = settings.DOUBAO_AK
        if not ak:
            raise RuntimeError("未配置豆包凭据：请设置 DOUBAO_AK 后再使用多播剧模式")
        return ak if " " in ak else f"Bearer;{ak}"

    async def _http_post_bytes(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> bytes:
        import httpx

        # 多播剧单次生成可达 120s，read 超时放宽
        timeout = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return await resp.aread()
