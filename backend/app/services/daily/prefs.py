"""日报偏好：音色与信源可在页面切换，落 `data/daily/prefs.json`。

为什么不用 DB：这两个偏好只服务日报一个功能，且必须与产物目录一起备份/迁移；
写成同目录下的 JSON 最直观（删掉目录 = 回到默认设置）。

优先级：
    音色：prefs.voice_id → settings.DAILY_TTS_VOICE_ID → 自动挑（新闻/播报优先）
    信源：prefs.sources（页面保存过）→ settings.DAILY_SOURCES_JSON → 内置默认
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ...core.config import settings
from . import store
from .constants import DEFAULT_SOURCES, source_list
from .store import DailyError

logger = logging.getLogger(__name__)

PREFS_NAME = "prefs.json"
MAX_SOURCES = 20
#: 音色列表缓存（避免每次打开页面都去问 TTS 厂商）
VOICE_CACHE_TTL = 300.0
_voice_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])


def prefs_path() -> Path:
    return store.daily_root() / PREFS_NAME


def load() -> dict[str, Any]:
    try:
        value = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """合并保存。只更新传入的键（voice_id="" 表示恢复自动挑选）。"""
    current = load()
    for key in ("voice_id", "sources"):
        if key in updates:
            current[key] = updates[key]
    current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    store.write_json(prefs_path(), current)
    logger.info(
        "[daily] 偏好已保存 voice=%s sources=%s",
        current.get("voice_id"), len(current.get("sources") or []),
    )
    return current


def builtin_urls() -> set[str]:
    return {str(s.get("url") or "") for s in DEFAULT_SOURCES}


def normalize_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验页面提交的信源：http(s)、去重、至少一个启用、最多 20 个。"""
    if not isinstance(items, list) or not items:
        raise DailyError("信源列表不能为空")
    if len(items) > MAX_SOURCES:
        raise DailyError(f"信源最多 {MAX_SOURCES} 个")
    builtin = builtin_urls()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise DailyError("信源格式不正确")
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise DailyError(f"信源必须是 http(s) 地址：{url[:80] or '（空）'}")
        if url in seen:
            continue
        seen.add(url)
        name = str(item.get("name") or "").strip()[:40] or url.split("//", 1)[-1].split("/", 1)[0]
        out.append({
            "name": name,
            "url": url,
            "enabled": bool(item.get("enabled", True)),
            "custom": url not in builtin,
        })
    if not any(s["enabled"] for s in out):
        raise DailyError("至少要启用一个信源")
    return out


def effective_sources(prefs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    prefs = prefs if prefs is not None else load()
    saved = prefs.get("sources")
    if isinstance(saved, list) and saved:
        out: list[dict[str, Any]] = []
        for item in saved:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            out.append({
                "name": str(item.get("name") or url)[:40],
                "url": url,
                "enabled": bool(item.get("enabled", True)),
                "custom": bool(item.get("custom", url not in builtin_urls())),
            })
        if out:
            return out
    return [{**s, "custom": False} for s in source_list()]


def effective_voice_id(prefs: dict[str, Any] | None = None) -> str:
    prefs = prefs if prefs is not None else load()
    return str(prefs.get("voice_id") or settings.DAILY_TTS_VOICE_ID or "")


async def available_voices(*, force: bool = False) -> list[dict[str, Any]]:
    """当前激活 TTS 厂商的音色列表（带 5 分钟缓存；失败返回空列表）。"""
    global _voice_cache
    ts, cached = _voice_cache
    if not force and cached and time.time() - ts < VOICE_CACHE_TTL:
        return cached
    try:
        from ...ai.factory import get_tts

        raw = await get_tts().list_voices()
    except Exception as e:
        logger.warning("[daily] 获取音色列表失败: %s: %s", type(e).__name__, e)
        return cached or []
    voices: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        voices.append({
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"])[:60],
            "gender": str(item.get("gender") or ""),
            "provider": str(item.get("provider") or ""),
            "scene": item.get("scene") if isinstance(item.get("scene"), list) else [],
            "description": str(item.get("description") or "")[:120],
        })
    _voice_cache = (time.time(), voices)
    return voices


def pick_default_voice(voices: list[dict[str, Any]]) -> str:
    """默认音色：新闻/播报/主播优先 → 有声书场景 → 第一个成年音色。

    当前 MiniMax 音色表里有「新闻女声」（Chinese (Mandarin)_News_Anchor），
    它就是日报的默认主播；换厂商时按名字/场景回退，不会硬编码某个 ID。
    """
    if not voices:
        raise DailyError("当前 TTS 厂商没有可用音色，请先在设置页配置 TTS")

    keywords = ("新闻", "播报", "主播", "资讯")
    for voice in voices:
        text = f"{voice.get('name') or ''} {voice.get('description') or ''}"
        if any(k in text for k in keywords):
            return str(voice["id"])
    for voice in voices:
        scenes = voice.get("scene") or []
        if isinstance(scenes, list) and any("有声书" in str(s) for s in scenes):
            if str(voice.get("gender") or "") != "neutral":
                return str(voice["id"])
    for voice in voices:
        if str(voice.get("gender") or "") in ("male", "female"):
            return str(voice["id"])
    return str(voices[0]["id"])


def resolve_voice_id(voices: list[dict[str, Any]] | None = None) -> str:
    """解析最终音色：偏好 → 配置 → 自动挑。"""
    chosen = effective_voice_id()
    if chosen:
        return chosen
    return pick_default_voice(voices or [])


def voice_name_of(voices: list[dict[str, Any]], voice_id: str) -> str | None:
    for voice in voices:
        if voice.get("id") == voice_id:
            return str(voice.get("name") or "")
    return None
