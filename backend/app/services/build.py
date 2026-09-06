"""
项目制 Build 服务：基于 Project 表的整本合成（Project → Build → BuildArtifact）。

设计要点：
- start_build: 创建 Build 记录 + 每章一条 pending BuildArtifact，启动后台任务立即返回
- _run_build_inner: 后台 worker（独立 session），逐章合成→更新 BuildArtifact；失败章写占位静音 MP3
- 幂等（合成阶段三层去重）：
  1) start_build 内存锁 _ACTIVE_BUILDS（按 build_id 持有）+ DB Build.status=running/queued
  2) **Build.config_digest 命中**：同一 project + 相同 narrator/speed/voice_assignments 且历史已有成功 Build，直接复用
  3) **段级 + 章级 skip**：worker 每章发现 BuildArtifact.status=done 且 MP3 存在 → 整章跳过；
     段级再走 tts_segment_cache_get/put（sha256(voice+speed+text) → 复用已有 MP3）
- 音频文件命名：build_{build_id}_ch{idx:04d}.mp3，ZIP：build_{build_id}_all.zip
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import time as _time
import uuid
import zipfile
from datetime import datetime, timedelta, UTC
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select

from ..core.config import settings
from ..db.models import (
    Project,
    Build,
    BuildArtifact,
    ProjectDialogue,
    ProjectCharacter,
    ProjectPronunciationRule,
)
from ..db.session import get_session_factory
from ..ai.factory import get_tts
from ..ai.providers.minimax.tts import (
    make_silent_mp3,
    concat_mp3_files,
    _estimate_mp3_duration_ms,
)
from .chapter import Chapter, _Segment, _build_segments_for_chapter
from .book_split import strip_chapter_prefix
from .m4b import m4b_filename
from .project import (
    PronunciationRule as _PronunciationRule,
    apply_pronunciation_rules,
)

logger = logging.getLogger(__name__)


# =====================================================================
# ZIP 打包工具（原 book.py 内联，避免循环依赖）
# =====================================================================

_UNSAFE_FS_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _sanitize_zip_entry(name: str, fallback: str) -> str:
    """给 ZIP 内部文件名用：控制字符/路径分隔符去掉；空字符串用 fallback。"""
    n = _UNSAFE_FS_CHARS.sub("_", name).strip().strip(".")
    n = n[:60]
    return n or fallback


def _build_book_zip(
    zip_path: str,
    *,
    job_id: str,
    job_title: str | None,
    chapter_outputs: list[tuple[str | None, int | None]],
    chapter_titles: list[str],
) -> None:
    """
    把所有章节 MP3 打包到 ZIP。失败章的占位音频也会被打进 ZIP，避免缺文件。
    ZIP 内部命名：《书名》/第001章 标题.mp3
    """
    book_dir = _sanitize_zip_entry(job_title or job_id, f"小说_{job_id[:8]}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for i, (path, _dur) in enumerate(chapter_outputs):
            raw_title = chapter_titles[i] if i < len(chapter_titles) else ""
            clean_title = _sanitize_zip_entry(strip_chapter_prefix(raw_title), f"章节{i+1}")
            base = f"第{i+1:03d}章_{clean_title}"
            entry_name = f"{book_dir}/{base}.mp3"
            if path and os.path.isfile(path):
                zf.write(path, arcname=entry_name)
            else:
                zf.writestr(entry_name, make_silent_mp3(100))


# =====================================================================
# Pydantic response 模型
# =====================================================================

class BuildResp(BaseModel):
    """start_build 立即返回。"""
    build_id: str
    project_id: str
    status: str
    total_chapters: int
    completed_chapters: int
    narrator_voice_id: str
    speed: float
    mode: str = "classic"
    tts_provider: str = "minimax"
    created_at: str | None
    failed_chapters: list[int] | None
    is_retry: bool


class BuildArtifactResp(BaseModel):
    """单章 BuildArtifact 详情。"""
    chapter_idx: int
    title: str
    status: str
    audio_url: str | None
    duration_ms: int | None
    error_msg: str | None


class BuildDetailResp(BaseModel):
    """Build 详情（含 artifacts）。"""
    build_id: str
    project_id: str
    status: str
    progress_msg: str | None
    total_chapters: int
    completed_chapters: int
    narrator_voice_id: str
    speed: float
    mode: str = "classic"
    tts_provider: str = "minimax"
    zip_url: str | None
    total_size_kb: int | None
    total_duration_sec: float | None
    started_at: str | None
    completed_at: str | None
    created_at: str | None
    artifacts: list[BuildArtifactResp]
    failed_chapters: list[int] | None
    is_retry: bool
    # TTS 用量（真实供应商调用，不含缓存命中）
    tts_calls: int = 0
    tts_chars: int = 0


class BuildListItem(BaseModel):
    """Build 列表项（精简）。"""
    build_id: str
    status: str
    total_chapters: int
    completed_chapters: int
    mode: str = "classic"
    tts_provider: str = "minimax"
    started_at: str | None
    completed_at: str | None
    created_at: str | None
    failed_chapters: list[int] | None
    is_retry: bool
    tts_calls: int = 0
    tts_chars: int = 0


class BuildStatusResp(BaseModel):
    """轮询用：progress + artifacts。"""
    build_id: str
    status: str
    progress_msg: str | None
    completed_chapters: int
    total_chapters: int
    mode: str = "classic"
    tts_provider: str = "minimax"
    artifacts: list[BuildArtifactResp]
    failed_chapters: list[int] | None


# =====================================================================
# 内部工具
# =====================================================================

def _parse_failed_chapters_json(s: str | None) -> list[int] | None:
    if not s:
        return None
    try:
        lst = json.loads(s)
        if isinstance(lst, list) and all(isinstance(x, int) for x in lst):
            return sorted(lst) if lst else None
        return None
    except Exception:
        return None


async def _ensure_default_narrator(narrator_voice_id: str | None) -> str:
    """narrator 为空时兜底到 minimax:male-qn-jingying；音色库无此 id 时取第一个。
    注意：新命名空间已统一加 minimax: 前缀；同时兼容「无前缀 legacy id」。
    """
    if narrator_voice_id:
        return narrator_voice_id
    tts = get_tts()
    voices = await tts.list_voices()
    if voices:
        vid = (
            next((v["id"] for v in voices if v.get("id") in ("minimax:male-qn-jingying", "male-qn-jingying")), None)
            or voices[0].get("id", "")
        )
        return vid
    raise RuntimeError("音色库为空，无法合成")


# 进程级运行锁：按 build_id（而非 project_id）维度持有，避免
# 「cancel → retry → 同 project 立即重入」时的误判/竞态：
# - cancel 仅修改 Build.status='cancelled'，不抢锁；
# - worker 在 finally 只释放自己的 build_id；
# - retry 启动新 build_id 时加新锁，不会与旧 worker 相互覆盖。
_ACTIVE_BUILDS: dict[str, str] = {}  # build_id -> project_id
_RUNNING_LOCK = asyncio.Lock()


async def _ensure_project_not_running(project_id: str, action: str):
    async with _RUNNING_LOCK:
        if any(pid == project_id for pid in _ACTIVE_BUILDS.values()):
            raise ValueError(f"项目 {project_id[:8]} 正在合成（Build 运行中），无法{action}。请先取消当前 Build。")


async def _register_active_build(build_id: str, project_id: str) -> None:
    """注册当前活跃 build（retry 时 build_id 一定是新的，不会与旧 worker 冲突）。"""
    async with _RUNNING_LOCK:
        _ACTIVE_BUILDS[build_id] = project_id


async def _unregister_active_build(build_id: str, expected_project_id: str) -> None:
    """释放当前 build 的运行锁：仅当 build_id 仍指向同一 project 时才释放。"""
    async with _RUNNING_LOCK:
        cur = _ACTIVE_BUILDS.get(build_id)
        if cur == expected_project_id:
            _ACTIVE_BUILDS.pop(build_id, None)


def _audio_filename(build_id: str, ch_idx: int, failed: bool = False) -> str:
    """每章 MP3 文件名；failed 章单独命名以便排查。"""
    suffix = "_failed" if failed else ""
    return f"build_{build_id}_ch{ch_idx:04d}{suffix}.mp3"


def _timings_filename(build_id: str, ch_idx: int) -> str:
    """章节时间轴 sidecar（SRT/LRC 生成用）：每段 {kind, speaker, text, start_ms, dur_ms}。"""
    return f"build_{build_id}_ch{ch_idx:04d}_timings.json"


def _timings_filename_of_audio(audio_filename: str) -> str:
    """由章节 MP3 文件名推导同名时间轴 sidecar 文件名。"""
    if audio_filename.endswith(".mp3"):
        return audio_filename[:-4] + "_timings.json"
    return audio_filename + "_timings.json"


async def _load_voice_styles(project_id: str) -> dict[str, dict[str, str]]:
    """从 ProjectCharacter 读取 speaker → {emotion, instruction}（只保留配置过的角色）。"""
    from sqlalchemy import select as _select
    factory = get_session_factory()
    async with factory() as s:
        stmt = _select(ProjectCharacter).where(ProjectCharacter.project_id == project_id)
        rows = list((await s.execute(stmt)).scalars().all())
    styles: dict[str, dict[str, str]] = {}
    for c in rows:
        if not c.name:
            continue
        emo = (c.emotion or "").strip()
        ins = (c.instruction or "").strip()
        if emo or ins:
            styles[c.name] = {"emotion": emo, "instruction": ins}
    return styles


# =====================================================================
# TTS 段级缓存（跨 Build、跨段复用）
# 键：sha256(f"v1|{voice_id}|{speed:.2f}|{text}").hexdigest()
# 值：(mp3_bytes, duration_ms)
# 说明：
# - 进程级内存缓存（LRU 上限 TTS_SEGMENT_CACHE_MAX_ENTRIES）；
# - 同时落盘到 settings.AUDIO_DIR / "_seg_cache" / "{key}.mp3"，元数据 JSON 同目录，
#   这样重启后仍能命中；
# - MP3 是不可压缩/不需要无损的媒体格式，二进制文件直接存即可；
# - 命中时无需调用 TTS API（零花费、零延迟、零 429 风险）。
# =====================================================================

_TTS_SEG_CACHE_MAX_DEFAULT = 20_000
_tts_seg_mem_cache: dict[str, tuple[bytes, int]] = {}
_tts_seg_mem_lock = asyncio.Lock()


def _seg_cache_dir() -> Path:
    p = Path(settings.AUDIO_DIR) / "_seg_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _seg_cache_gc_ts_path() -> Path:
    return Path(settings.AUDIO_DIR) / ".seg_cache_gc_ts"


def _run_seg_cache_gc_if_needed(force: bool = False) -> None:
    try:
        ts_path = _seg_cache_gc_ts_path()
        now = datetime.now(UTC).replace(tzinfo=None)
        if not force:
            try:
                if ts_path.is_file():
                    last_ts_str = ts_path.read_text(encoding="utf-8").strip()
                    last_ts = datetime.fromisoformat(last_ts_str)
                    if (now - last_ts) < timedelta(hours=1):
                        return
            except Exception:
                pass

        cache_dir = _seg_cache_dir()
        mp3_files: list[tuple[float, str, Path]] = []
        for mp3_p in cache_dir.glob("*.mp3"):
            try:
                st = mp3_p.stat()
                key = mp3_p.stem
                mp3_files.append((st.st_mtime, key, mp3_p))
            except Exception:
                continue

        mp3_files.sort(key=lambda x: (x[0], x[1]))

        ttl_days = int(getattr(settings, "TTS_SEGMENT_CACHE_TTL_DAYS", 30) or 30)
        ttl_cutoff = _time.time() - ttl_days * 86400

        remaining: list[tuple[float, str, Path, int]] = []
        for mtime, key, mp3_p in mp3_files:
            if mtime < ttl_cutoff:
                try:
                    mp3_p.unlink(missing_ok=True)
                    meta_p = cache_dir / f"{key}.json"
                    meta_p.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                try:
                    size = mp3_p.stat().st_size
                    remaining.append((mtime, key, mp3_p, size))
                except Exception:
                    pass

        max_size_gb = int(getattr(settings, "TTS_SEGMENT_CACHE_MAX_SIZE_GB", 20) or 20)
        max_size_bytes = max_size_gb * int(1e9)
        total_size = sum(x[3] for x in remaining)

        while total_size > max_size_bytes and remaining:
            _, key, mp3_p, sz = remaining.pop(0)
            try:
                mp3_p.unlink(missing_ok=True)
                meta_p = cache_dir / f"{key}.json"
                meta_p.unlink(missing_ok=True)
                total_size -= sz
            except Exception:
                pass

        try:
            ts_path.write_text(now.isoformat(), encoding="utf-8")
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"[seg_cache_gc] failed: {type(e).__name__}: {e}")


def _seg_cache_key(
    voice_id: str, speed: float, text: str,
    *, emotion: str = "", instruction: str = "",
) -> str:
    """段级缓存键。emotion/instruction 参与哈希（不同情感同一音色音频不同）；
    均为空时与旧版键完全一致，历史缓存仍可命中。"""
    style_part = ""
    if emotion or instruction:
        style_part = f"|e:{emotion}|i:{instruction}"
    raw = f"v1|{voice_id}|{speed:.2f}|{text}{style_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    raw = f"v1|{voice_id}|{speed:.4f}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seg_cache_mp3_path(key: str) -> Path:
    return _seg_cache_dir() / f"{key}.mp3"


def _seg_cache_meta_path(key: str) -> Path:
    return _seg_cache_dir() / f"{key}.json"


async def tts_segment_cache_get(
    voice_id: str, speed: float, text: str,
    *, emotion: str = "", instruction: str = "",
) -> tuple[bytes, int] | None:
    """返回 (mp3_bytes, duration_ms)，未命中返回 None。先查内存，再查磁盘。"""
    key = _seg_cache_key(voice_id, speed, text, emotion=emotion, instruction=instruction)
    async with _tts_seg_mem_lock:
        hit = _tts_seg_mem_cache.get(key)
    if hit is not None:
        return hit
    mp3_p = _seg_cache_mp3_path(key)
    meta_p = _seg_cache_meta_path(key)
    try:
        if mp3_p.is_file() and meta_p.is_file():
            mp3_bytes = mp3_p.read_bytes()
            dur_ms = int(json.loads(meta_p.read_text(encoding="utf-8")).get("dur_ms", 0))
            async with _tts_seg_mem_lock:
                if key not in _tts_seg_mem_cache:
                    _tts_seg_mem_cache[key] = (mp3_bytes, dur_ms)
                    max_entries = max(1000, int(
                        getattr(settings, "TTS_SEGMENT_CACHE_MAX_ENTRIES", _TTS_SEG_CACHE_MAX_DEFAULT)
                        or _TTS_SEG_CACHE_MAX_DEFAULT
                    ))
                    while len(_tts_seg_mem_cache) > max_entries:
                        _tts_seg_mem_cache.popitem(last=False)
            return mp3_bytes, dur_ms
    except Exception:
        return None
    return None


async def tts_segment_cache_put(
    voice_id: str, speed: float, text: str, mp3_bytes: bytes, dur_ms: int,
    *, emotion: str = "", instruction: str = "",
) -> None:
    """写 TTS 段缓存：内存 + 磁盘双写。"""
    key = _seg_cache_key(voice_id, speed, text, emotion=emotion, instruction=instruction)
    async with _tts_seg_mem_lock:
        _tts_seg_mem_cache[key] = (mp3_bytes, int(dur_ms))
        max_entries = max(1000, int(
            getattr(settings, "TTS_SEGMENT_CACHE_MAX_ENTRIES", _TTS_SEG_CACHE_MAX_DEFAULT)
            or _TTS_SEG_CACHE_MAX_DEFAULT
        ))
        while len(_tts_seg_mem_cache) > max_entries:
            _tts_seg_mem_cache.popitem(last=False)
    try:
        mp3_p = _seg_cache_mp3_path(key)
        meta_p = _seg_cache_meta_path(key)
        if not mp3_p.is_file():
            mp3_p.write_bytes(mp3_bytes)
        if not meta_p.is_file():
            meta_p.write_text(
                json.dumps({"dur_ms": int(dur_ms)}, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception as e:
        logger.warning(f"[tts_seg_cache] put disk fail key={key[:12]}... err={e}")

    if random.random() < 0.01:
        _run_seg_cache_gc_if_needed(force=False)


# =====================================================================
# Build 配置哈希：同 project + (narrator, speed, voice_assignments) 相同 → 视为同一配置
# 扩展：新增 mode + tts_provider 作为 key（Task 4 / Task 8）
# =====================================================================


def _calc_config_digest(
    narrator_voice_id: str,
    speed: float,
    voice_assignments: dict[str, str],
    *,
    mode: str = "classic",
    tts_provider: str = "minimax",
    narrator_emotion: str = "",
    narrator_instruction: str = "",
    voice_styles: dict[str, dict[str, str]] | None = None,
) -> str:
    sorted_va = dict(sorted((voice_assignments or {}).items()))
    sorted_styles = dict(sorted((voice_styles or {}).items()))
    raw = json.dumps(
        {
            "narrator": narrator_voice_id or "",
            "speed": round(float(speed), 6),
            "va": sorted_va,
            "mode": (mode or "classic").lower(),
            "tts_provider": (tts_provider or "minimax").lower(),
            # 情感/语气也参与摘要：改了情感但音色没变也应生成新 build
            "narrator_emotion": narrator_emotion or "",
            "narrator_instruction": narrator_instruction or "",
            "styles": sorted_styles,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _zip_filename(build_id: str) -> str:
    return f"build_{build_id}_all.zip"


def _build_to_resp(b: Build) -> BuildResp:
    return BuildResp(
        build_id=b.build_id,
        project_id=b.project_id,
        status=b.status,
        total_chapters=b.total_chapters,
        completed_chapters=b.completed_chapters,
        narrator_voice_id=b.narrator_voice_id,
        speed=b.speed,
        mode=b.mode or "classic",
        tts_provider=b.tts_provider or "minimax",
        created_at=b.created_at.isoformat() if b.created_at else None,
        failed_chapters=_parse_failed_chapters_json(b.failed_chapters_json),
        is_retry=bool(b.is_retry),
    )


def _build_to_detail(b: Build, artifacts: list[BuildArtifact]) -> BuildDetailResp:
    total_kb = (b.total_size_bytes // 1024) if b.total_size_bytes else None
    return BuildDetailResp(
        build_id=b.build_id,
        project_id=b.project_id,
        status=b.status,
        progress_msg=b.progress_msg,
        total_chapters=b.total_chapters,
        completed_chapters=b.completed_chapters,
        narrator_voice_id=b.narrator_voice_id,
        speed=b.speed,
        mode=b.mode or "classic",
        tts_provider=b.tts_provider or "minimax",
        zip_url=f"/media/{b.zip_filename}" if b.zip_filename else None,
        total_size_kb=total_kb,
        total_duration_sec=round((b.total_duration_ms or 0) / 1000.0, 2),
        started_at=b.started_at.isoformat() if b.started_at else None,
        completed_at=b.completed_at.isoformat() if b.completed_at else None,
        created_at=b.created_at.isoformat() if b.created_at else None,
        artifacts=[
            BuildArtifactResp(
                chapter_idx=a.chapter_idx,
                title=a.title,
                status=a.status,
                audio_url=a.audio_url,
                duration_ms=a.duration_ms,
                error_msg=a.error_msg,
            )
            for a in artifacts
        ],
        failed_chapters=_parse_failed_chapters_json(b.failed_chapters_json),
        is_retry=bool(b.is_retry),
        tts_calls=int(b.tts_calls or 0),
        tts_chars=int(b.tts_chars or 0),
    )


def _build_to_list_item(b: Build) -> BuildListItem:
    return BuildListItem(
        build_id=b.build_id,
        status=b.status,
        total_chapters=b.total_chapters,
        completed_chapters=b.completed_chapters,
        mode=b.mode or "classic",
        tts_provider=b.tts_provider or "minimax",
        started_at=b.started_at.isoformat() if b.started_at else None,
        completed_at=b.completed_at.isoformat() if b.completed_at else None,
        created_at=b.created_at.isoformat() if b.created_at else None,
        failed_chapters=_parse_failed_chapters_json(b.failed_chapters_json),
        is_retry=bool(b.is_retry),
        tts_calls=int(b.tts_calls or 0),
        tts_chars=int(b.tts_chars or 0),
    )


def _build_to_status_resp(b: Build, artifacts: list[BuildArtifact]) -> BuildStatusResp:
    return BuildStatusResp(
        build_id=b.build_id,
        status=b.status,
        progress_msg=b.progress_msg,
        completed_chapters=b.completed_chapters,
        total_chapters=b.total_chapters,
        mode=b.mode or "classic",
        tts_provider=b.tts_provider or "minimax",
        artifacts=[
            BuildArtifactResp(
                chapter_idx=a.chapter_idx,
                title=a.title,
                status=a.status,
                audio_url=a.audio_url,
                duration_ms=a.duration_ms,
                error_msg=a.error_msg,
            )
            for a in artifacts
        ],
        failed_chapters=_parse_failed_chapters_json(b.failed_chapters_json),
    )


# =====================================================================
# TTS 命名空间校验（Task 4 / Task 8）
# =====================================================================

_VALID_PROVIDERS = {"minimax", "doubao"}
_VALID_MODES = {"classic", "multicast"}
# 前缀 → 归属哪个 tts_provider
_PREFIX_TO_PROVIDER: dict[str, str] = {
    "minimax": "minimax",
    "doubao": "doubao",
    "icl": "doubao",
}


def _voice_id_provider_of(voice_id: str) -> str | None:
    """根据 voice_id 前缀推断所属 TTS 厂商；无前缀返回 None。"""
    if not voice_id:
        return None
    if ":" in voice_id:
        prefix = voice_id.split(":", 1)[0].lower()
        return _PREFIX_TO_PROVIDER.get(prefix)
    return None


def _validate_tts_namespace(
    tts_provider: str,
    mode: str,
    narrator_voice_id: str,
    voice_assignments: dict[str, str],
) -> None:
    """校验 tts_provider / mode / voice_assignments 是否兼容。不兼容则抛 RuntimeError。"""
    norm_mode = (mode or "classic").lower()
    norm_provider = (tts_provider or "").lower()

    # mode 合法性
    if norm_mode not in _VALID_MODES:
        raise RuntimeError(f"未知 build.mode: {mode}，可选 {sorted(_VALID_MODES)}")
    # 多播剧必须显式指定 tts_provider
    if norm_mode == "multicast" and not norm_provider:
        raise RuntimeError("mode=multicast（多播剧模式）必须显式指定 tts_provider，不能留空")
    # 多播剧 = Seed-Audio 一体化生成，仅豆包支持；minimax + multicast 直接拒绝（不降级）
    if norm_mode == "multicast" and norm_provider and norm_provider != "doubao":
        raise RuntimeError(
            f"mode=multicast（多播剧模式）目前仅支持 tts_provider='doubao'（Seed-Audio 一体化生成），"
            f"当前 tts_provider='{norm_provider}'；请切换豆包或改用 classic 模式。"
        )
    # provider 合法性
    if norm_provider and norm_provider not in _VALID_PROVIDERS:
        raise RuntimeError(f"未知 tts_provider: {tts_provider}，可选 {sorted(_VALID_PROVIDERS)}")

    provider_for_check = norm_provider  # 空字符串时，仍可能 narrator 无前缀 → 走 Legacy
    # narrator 校验
    n_provider = _voice_id_provider_of(narrator_voice_id)
    if n_provider and provider_for_check and n_provider != provider_for_check:
        raise RuntimeError(
            f"narrator_voice_id '{narrator_voice_id}' 属于 {n_provider}，"
            f"但当前 tts_provider='{provider_for_check}'，两者不兼容；请更换 narrator 或调整 tts_provider。"
        )
    # voice_assignments 校验
    for ch, vid in (voice_assignments or {}).items():
        v_provider = _voice_id_provider_of(vid)
        if v_provider and provider_for_check and v_provider != provider_for_check:
            raise RuntimeError(
                f"角色 '{ch}' 的音色 '{vid}' 属于 {v_provider}，"
                f"但当前 tts_provider='{provider_for_check}'，两者不兼容；请更换角色音色或调整 tts_provider。"
            )
    # 若 tts_provider 未给出，但 narrator 或某角色音色有前缀 → 要求显式 provider 防止歧义
    if not norm_provider and (n_provider or any(_voice_id_provider_of(v) for v in (voice_assignments or {}).values())):
        raise RuntimeError(
            "检测到音色使用了带前缀的命名空间 ID（doubao:/minimax:/icl:），"
            "请显式传 tts_provider，避免合成路由歧义。"
        )


def _validate_multicast_provider(build_mode: str, tts_provider_label: str) -> None:
    """严格契约：multicast 只能配豆包（Seed-Audio）。

    retry / 历史数据行不会重新走 start_build 校验，_run_build_inner 里兜底拦截，
    防止静默降级到 classic 逐句合成。
    """
    if (build_mode or "classic").lower() == "multicast" and (tts_provider_label or "minimax").lower() != "doubao":
        raise ValueError(
            f"mode=multicast（多播剧模式）仅支持 tts_provider='doubao'，"
            f"当前 tts_provider={tts_provider_label!r}；拒绝降级为 classic 合成。"
        )


def _should_strict_fail(mode: str) -> bool:
    """严格失败判定（用户需求 #4）：选了多播剧模式 + MULTICAST_STRICT_MODE=True → 任何章节失败直接 Build 失败，不降级（不生成占位静音 MP3）。"""
    from ..core.config import settings
    return bool(settings.MULTICAST_STRICT_MODE) and (mode or "classic").lower() == "multicast"



# =====================================================================
# start_build + 后台 worker
# =====================================================================

async def start_build(
    project_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
    *,
    mode: str = "classic",
    tts_provider: str | None = None,
    narrator_emotion: str = "",
    narrator_instruction: str = "",
) -> BuildResp:
    """
    创建 Build + 每章 BuildArtifact（pending），启动后台 worker，立即返回。

    新增参数：
      - mode: 'classic'（默认，逐章节分段 TTS 拼接）/'multicast'（多播剧，Seed-Audio 一体化，失败直接抛错）
      - tts_provider: 'minimax' | 'doubao' | None（None 时从 Project.default_tts_provider 读取，再兜底 settings.TTS_PROVIDER）
      - narrator_emotion / narrator_instruction: 旁白情感与风格指令（合成时透传 provider）

    合成幂等（三层去重，mode/tts_provider 已纳入 config_digest）：
      1) 内存锁 _ACTIVE_BUILDS（按 build_id 持有）：单进程内同一 project 不重复
      2) DB Build.status：若已有 running/queued 的 build，直接复用
      3) **config_digest 命中**：同一 project + 相同 narrator/speed/voice_assignments/mode/tts_provider/情感配置，
         且历史已有**成功** Build（ZIP 生成过）→ 直接返回旧 build_id，不重建。
    """
    from ..core.config import settings as _settings_mod

    _run_seg_cache_gc_if_needed(force=False)

    narrator_voice_id = await _ensure_default_narrator(narrator_voice_id)

    # 解析 tts_provider （优先级：参数 > Project.default_tts_provider > settings.TTS_PROVIDER > 'minimax'）
    effective_provider = (tts_provider or "").lower() or None
    resolved_mode = (mode or "classic").lower()
    if not effective_provider:
        factory_sess = get_session_factory()
        try:
            async with factory_sess() as s:
                p = await s.get(Project, project_id)
                if p and p.default_tts_provider:
                    effective_provider = p.default_tts_provider.lower()
        except Exception:
            pass
    if not effective_provider:
        effective_provider = (_settings_mod.TTS_PROVIDER or "minimax").lower()

    # mode 默认值：若 Project.default_build_mode 显式设定则覆盖
    if not mode or resolved_mode == "classic":
        factory_sess2 = get_session_factory()
        try:
            async with factory_sess2() as s:
                p = await s.get(Project, project_id)
                if p and p.default_build_mode:
                    resolved_mode = p.default_build_mode.lower()
        except Exception:
            pass
    else:
        resolved_mode = mode.lower()

    # 命名空间校验（不兼容直接抛 RuntimeError）
    _validate_tts_namespace(
        tts_provider=effective_provider,
        mode=resolved_mode,
        narrator_voice_id=narrator_voice_id,
        voice_assignments=voice_assignments,
    )

    # 角色情感/语气快照：从 ProjectCharacter 读取（与 voice_assignments 同属配置快照）
    narrator_emotion = (narrator_emotion or "").strip()[:32]
    narrator_instruction = (narrator_instruction or "").strip()[:512]
    try:
        voice_styles = await _load_voice_styles(project_id)
    except Exception as e:
        logger.warning(f"[build_start] 读取角色情感配置失败（按无情感继续）: {type(e).__name__}: {e}")
        voice_styles = {}

    digest = _calc_config_digest(
        narrator_voice_id, speed, voice_assignments,
        mode=resolved_mode, tts_provider=effective_provider,
        narrator_emotion=narrator_emotion,
        narrator_instruction=narrator_instruction,
        voice_styles=voice_styles,
    )

    async with _RUNNING_LOCK:
        if any(pid == project_id for pid in _ACTIVE_BUILDS.values()):
            logger.info(f"[build_start] project_id={project_id[:8]}... already running")
            factory = get_session_factory()
            async with factory() as s:
                stmt = select(Build).where(
                    Build.project_id == project_id
                ).order_by(Build.created_at.desc()).limit(1)
                b = (await s.execute(stmt)).scalar_one_or_none()
                if b:
                    return _build_to_resp(b)

    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        if not p.chapters_json:
            raise RuntimeError("项目尚未 prepare，chapters_json 为空")
        try:
            chapter_dicts = json.loads(p.chapters_json)
        except Exception:
            raise RuntimeError("项目 chapters_json 损坏，请重新 prepare")
        chapters = [
            Chapter(idx=c["idx"], title=c.get("title", ""), text=c.get("text", ""))
            for c in chapter_dicts
        ]
        if not chapters:
            raise RuntimeError("项目没有章节，无法启动 build")

        stmt_active = select(Build).where(
            Build.project_id == project_id,
            Build.status.in_(("queued", "running")),
        )
        active = (await session.execute(stmt_active)).scalar_one_or_none()
        if active:
            now = datetime.now(UTC).replace(tzinfo=None)
            if active.status == "running" and active.started_at and (now - active.started_at > timedelta(hours=settings.BUILD_RUNNING_TIMEOUT_HOURS)):
                active.status = "cancelled"
                active.progress_msg = f"running 超过 {settings.BUILD_RUNNING_TIMEOUT_HOURS}h，判定为被 kill 的孤儿任务，已取消并起新 Build"
                await session.commit()
            else:
                logger.warning(
                    f"[build_start] project_id={project_id[:8]}... "
                    f"already has active build {active.build_id[:8]}... status={active.status}"
                )
                return _build_to_resp(active)

        stmt_success = (
            select(Build)
            .where(
                Build.project_id == project_id,
                Build.status == "success",
                Build.config_digest == digest,
            )
            .order_by(Build.created_at.desc())
            .limit(1)
        )
        success_hit = (await session.execute(stmt_success)).scalar_one_or_none()
        if success_hit:
            if success_hit.zip_filename:
                expected_zip = Path(settings.AUDIO_DIR) / success_hit.zip_filename
                if expected_zip.is_file():
                    logger.info(
                        f"[build_start] project_id={project_id[:8]}... "
                        f"命中 config_digest 成功历史 build={success_hit.build_id[:8]}... "
                        f"直接复用（不重新合成）"
                    )
                    return _build_to_resp(success_hit)

        build_id = uuid.uuid4().hex
        voice_json = json.dumps(voice_assignments, ensure_ascii=False)
        b = Build(
            build_id=build_id,
            project_id=project_id,
            status="queued",
            progress_msg=f"准备合成 1/{len(chapters)} 章…",
            completed_chapters=0,
            total_chapters=len(chapters),
            narrator_voice_id=narrator_voice_id,
            speed=speed,
            mode=resolved_mode,
            tts_provider=effective_provider,
            voice_assignments_json=voice_json,
            config_digest=digest,
            narrator_emotion=narrator_emotion,
            narrator_instruction=narrator_instruction,
            voice_styles_json=json.dumps(voice_styles, ensure_ascii=False),
        )
        session.add(b)
        for ch in chapters:
            session.add(BuildArtifact(
                build_id=build_id,
                chapter_idx=ch.idx,
                title=ch.title,
                status="pending",
            ))
        await session.commit()
        await session.refresh(b)
        cur_resp = _build_to_resp(b)

    await _register_active_build(build_id, project_id)

    async def _runner() -> None:
        """后台 worker：独立 session，完成后释放锁。"""
        # P1 #7：用 JobTask 跟踪 build worker 生命周期（启动恢复 / 看门狗依赖）
        from .job_tasks import register_task, HeartbeatContext, finish_task
        job_task_id: str | None = None
        try:
            job = await register_task("build", build_id, trigger="api")
            job_task_id = job.task_id
        except Exception as e:
            logger.warning(
                f"[build_worker] 注册 JobTask 失败 build_id={build_id[:8]}... "
                f"{type(e).__name__}: {e}"
            )
            job_task_id = None

        final_status = "failed"
        final_error: str | None = None
        try:
            async with HeartbeatContext(job_task_id):
                try:
                    await _run_build_inner(
                        build_id=build_id,
                        project_id=project_id,
                        voice_assignments=voice_assignments,
                        narrator_voice_id=narrator_voice_id,
                        speed=speed,
                        only_chapter_idxs=None,
                        source_build_id=None,
                    )
                    # 终态由 _run_build_inner 写 Build.status；这里尝试把对应终态同步给 JobTask
                    f1 = get_session_factory()
                    async with f1() as s1:
                        b_after = await s1.get(Build, build_id)
                        if b_after and b_after.status in ("success", "partial_success"):
                            final_status = "success"
                        elif b_after and b_after.status == "cancelled":
                            final_status = "cancelled"
                        elif b_after and b_after.status == "failed":
                            final_status = "failed"
                except Exception as e:
                    logger.error(
                        f"[build_worker] FAIL build_id={build_id[:8]}... "
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    final_error = f"{type(e).__name__}: {e}"
                    try:
                        f2 = get_session_factory()
                        async with f2() as s:
                            b2 = await s.get(Build, build_id)
                            if b2:
                                b2.status = "failed"
                                b2.progress_msg = f"合成失败: {type(e).__name__}: {e}"[:200]
                                b2.completed_at = datetime.now(UTC).replace(tzinfo=None)
                                await s.commit()
                    except Exception as e2:
                        logger.error(f"[build_worker] final status write fail: {e2}")
                    final_status = "failed"
        except asyncio.CancelledError:
            final_status = "cancelled"
            final_error = "build worker cancelled"
            try:
                f2 = get_session_factory()
                async with f2() as s:
                    b2 = await s.get(Build, build_id)
                    if b2:
                        b2.status = "cancelled"
                        b2.progress_msg = "build worker cancelled"
                        b2.completed_at = datetime.now(UTC).replace(tzinfo=None)
                        await s.commit()
            except Exception:
                pass
            raise
        finally:
            if job_task_id:
                try:
                    await finish_task(
                        job_task_id, status=final_status, error_msg=final_error,
                    )
                except Exception as e:
                    logger.warning(
                        f"[build_worker] 写 JobTask 终态失败 task_id={job_task_id}: {e}"
                    )
            await _unregister_active_build(build_id, project_id)

    asyncio.create_task(_runner(), name=f"build_{build_id[:8]}")
    logger.info(
        f"[build_start] launched worker build_id={build_id[:8]}... "
        f"project_id={project_id[:8]}... chapters={len(chapters)}"
    )
    return cur_resp


async def cancel_build(project_id: str, build_id: str, reason: str | None = None) -> BuildResp:
    factory = get_session_factory()
    async with factory() as session:
        b = await session.get(Build, build_id)
        if not b:
            raise ValueError(f"Build 不存在: {build_id}")
        if b.project_id != project_id:
            raise ValueError(f"Build 不属于项目 {project_id}")
        if b.status in ("success", "partial_success", "failed", "cancelled"):
            raise ValueError(f"Build 已是终态 status={b.status}，无法取消")
        b.status = "cancelled"
        msg = f"已被手动取消"
        if reason:
            msg += f": {reason}"
        b.progress_msg = msg[:256]
        b.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
        await session.refresh(b)

    # 不主动抢 _RUNNING_LOCK：worker 会在下一轮循环看到 status='cancelled'
    # 后自然退出并 finally 释放自己的 build_id 锁；这样即便 cancel 紧跟着
    # retry（生成新的 build_id），两条锁也不会互相覆盖。
    # 但为了让 _ensure_project_not_running 立即可见"该 project 已没有 active build"，
    # 这里只在锁存在时按 build_id 精准释放一次；不存在则说明 worker 已自然退出。
    async with _RUNNING_LOCK:
        cur = _ACTIVE_BUILDS.get(build_id)
        if cur == project_id:
            _ACTIVE_BUILDS.pop(build_id, None)

    return _build_to_resp(b)


async def retry_failed_build(source_build_id: str, force_restart_failed_only: bool = True) -> BuildResp:
    factory = get_session_factory()
    async with factory() as session:
        source_build = await session.get(Build, source_build_id)
        if not source_build:
            raise ValueError(f"Build 不存在: {source_build_id}")
        project_id = source_build.project_id

        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        if not p.chapters_json:
            raise RuntimeError("项目尚未 prepare，chapters_json 为空")
        try:
            chapter_dicts = json.loads(p.chapters_json)
        except Exception:
            raise RuntimeError("项目 chapters_json 损坏，请重新 prepare")
        chapters = [
            Chapter(idx=c["idx"], title=c.get("title", ""), text=c.get("text", ""))
            for c in chapter_dicts
        ]
        if not chapters:
            raise RuntimeError("项目没有章节，无法启动 build")

        narrator_voice_id = source_build.narrator_voice_id
        speed = source_build.speed
        try:
            voice_assignments = json.loads(source_build.voice_assignments_json or "{}")
        except Exception:
            voice_assignments = {}

        failed_ch_idxs: list[int] = []
        fc = _parse_failed_chapters_json(source_build.failed_chapters_json)
        if fc:
            failed_ch_idxs = list(fc)
        else:
            stmt_failed = select(BuildArtifact.chapter_idx).where(
                BuildArtifact.build_id == source_build_id,
                BuildArtifact.status == "failed",
            )
            failed_ch_idxs = sorted(list((await session.execute(stmt_failed)).scalars().all()))

        if not failed_ch_idxs:
            raise ValueError("没有失败章可重试")

        async with _RUNNING_LOCK:
            if any(pid == project_id for pid in _ACTIVE_BUILDS.values()):
                logger.info(f"[retry_build] project_id={project_id[:8]}... already running, returning last build")
                stmt_last = select(Build).where(
                    Build.project_id == project_id
                ).order_by(Build.created_at.desc()).limit(1)
                last = (await session.execute(stmt_last)).scalar_one_or_none()
                if last:
                    return _build_to_resp(last)

        stmt_active = select(Build).where(
            Build.project_id == project_id,
            Build.status.in_(("queued", "running")),
        )
        active = (await session.execute(stmt_active)).scalar_one_or_none()
        if active:
            logger.warning(
                f"[retry_build] project_id={project_id[:8]}... "
                f"already has active build {active.build_id[:8]}... status={active.status}"
            )
            return _build_to_resp(active)

        source_art_stmt = select(BuildArtifact).where(
            BuildArtifact.build_id == source_build_id
        ).order_by(BuildArtifact.chapter_idx)
        source_arts = list((await session.execute(source_art_stmt)).scalars().all())
        source_art_by_idx: dict[int, BuildArtifact] = {a.chapter_idx: a for a in source_arts}

        audio_dir = Path(settings.AUDIO_DIR)
        new_build_id = uuid.uuid4().hex
        new_voice_json = json.dumps(voice_assignments, ensure_ascii=False)
        new_b = Build(
            build_id=new_build_id,
            project_id=project_id,
            status="queued",
            progress_msg=f"重试 Build {source_build_id[:8]}…，准备合成 {len(failed_ch_idxs)} 章",
            completed_chapters=0,
            total_chapters=len(chapters),
            narrator_voice_id=narrator_voice_id,
            speed=speed,
            voice_assignments_json=new_voice_json,
            # 重试必须继承源 Build 的合成配置：否则多播剧/豆包 Build 重试会
            # 静默退化为 classic+MiniMax（模型默认值），违反"不降级"契约
            mode=(source_build.mode or "classic"),
            tts_provider=(source_build.tts_provider or "minimax"),
            config_digest=source_build.config_digest,
            is_retry=True,
            # 情感/语气配置同样继承快照（保证 digest 一致 + 合成结果一致）
            narrator_emotion=(source_build.narrator_emotion or ""),
            narrator_instruction=(source_build.narrator_instruction or ""),
            voice_styles_json=source_build.voice_styles_json,
        )
        session.add(new_b)

        new_completed = 0
        for ch in chapters:
            src_art = source_art_by_idx.get(ch.idx)
            if ch.idx not in failed_ch_idxs and src_art and src_art.status == "done" and src_art.audio_filename:
                expected_mp3 = audio_dir / src_art.audio_filename
                if expected_mp3.is_file():
                    session.add(BuildArtifact(
                        build_id=new_build_id,
                        chapter_idx=ch.idx,
                        title=ch.title,
                        status="done",
                        audio_filename=src_art.audio_filename,
                        audio_url=src_art.audio_url,
                        duration_ms=src_art.duration_ms,
                        error_msg=None,
                    ))
                    new_completed += 1
                    continue
            session.add(BuildArtifact(
                build_id=new_build_id,
                chapter_idx=ch.idx,
                title=ch.title,
                status="pending",
            ))

        new_b.completed_chapters = new_completed
        await session.commit()
        await session.refresh(new_b)
        cur_resp = _build_to_resp(new_b)

    await _register_active_build(new_build_id, project_id)

    async def _runner() -> None:
        # P1 #7：retry 路径同样注册 JobTask
        from .job_tasks import register_task, HeartbeatContext, finish_task
        job_task_id: str | None = None
        try:
            job = await register_task("build", new_build_id, trigger="retry")
            job_task_id = job.task_id
        except Exception as e:
            logger.warning(
                f"[retry_worker] 注册 JobTask 失败 build_id={new_build_id[:8]}... "
                f"{type(e).__name__}: {e}"
            )
            job_task_id = None

        final_status = "failed"
        final_error: str | None = None
        try:
            async with HeartbeatContext(job_task_id):
                try:
                    await _run_build_inner(
                        build_id=new_build_id,
                        project_id=project_id,
                        voice_assignments=voice_assignments,
                        narrator_voice_id=narrator_voice_id,
                        speed=speed,
                        only_chapter_idxs=failed_ch_idxs,
                        source_build_id=source_build_id,
                    )
                    f1 = get_session_factory()
                    async with f1() as s1:
                        b_after = await s1.get(Build, new_build_id)
                        if b_after and b_after.status in ("success", "partial_success"):
                            final_status = "success"
                        elif b_after and b_after.status == "cancelled":
                            final_status = "cancelled"
                        elif b_after and b_after.status == "failed":
                            final_status = "failed"
                except Exception as e:
                    logger.error(
                        f"[retry_worker] FAIL build_id={new_build_id[:8]}... "
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    final_error = f"{type(e).__name__}: {e}"
                    try:
                        f2 = get_session_factory()
                        async with f2() as s:
                            b2 = await s.get(Build, new_build_id)
                            if b2:
                                b2.status = "failed"
                                b2.progress_msg = f"合成失败: {type(e).__name__}: {e}"[:200]
                                b2.completed_at = datetime.now(UTC).replace(tzinfo=None)
                                await s.commit()
                    except Exception as e2:
                        logger.error(f"[retry_worker] final status write fail: {e2}")
                    final_status = "failed"
        except asyncio.CancelledError:
            final_status = "cancelled"
            final_error = "retry worker cancelled"
            try:
                f2 = get_session_factory()
                async with f2() as s:
                    b2 = await s.get(Build, new_build_id)
                    if b2:
                        b2.status = "cancelled"
                        b2.progress_msg = "retry worker cancelled"
                        b2.completed_at = datetime.now(UTC).replace(tzinfo=None)
                        await s.commit()
            except Exception:
                pass
            raise
        finally:
            if job_task_id:
                try:
                    await finish_task(
                        job_task_id, status=final_status, error_msg=final_error,
                    )
                except Exception as e:
                    logger.warning(
                        f"[retry_worker] 写 JobTask 终态失败 task_id={job_task_id}: {e}"
                    )
            await _unregister_active_build(new_build_id, project_id)

    asyncio.create_task(_runner(), name=f"retry_{new_build_id[:8]}")
    logger.info(
        f"[retry_build] launched worker build_id={new_build_id[:8]}... "
        f"project_id={project_id[:8]}... retry_chapters={len(failed_ch_idxs)}"
    )
    return cur_resp


# Seed-Audio 分段估算：中文朗读约 4 字/秒（×speed），留安全余量
_MC_CHARS_PER_SEC = 4.0
_MC_CHUNK_TARGET_SECS = 100.0


def _estimate_multicast_secs(seg_dicts: list[dict], speed: float) -> float:
    chars = sum(len((s.get("text") or "")) for s in seg_dicts if (s.get("kind") or "") != "silence")
    return chars / (_MC_CHARS_PER_SEC * max(0.2, float(speed or 1.0)))


async def _multicast_synth_chapter(
    mc: Any,
    seg_dicts: list[dict],
    output_path: str,
    *,
    speed: float = 1.0,
    chapter_title: str = "",
    max_secs: float = 120.0,
) -> tuple[str, int]:
    """Seed-Audio 整章合成；预估时长超过单次上限时按 segment 边界分段生成再拼接。

    单次调用音频上限约 120s（MAX_CHAPTER_AUDIO_SECS），常规 3000 字章节约 5 分钟，
    必须分段：每段目标 ≤_MC_CHUNK_TARGET_SECS，逐段调用后 concat_mp3_files 合并。
    """
    total_secs = _estimate_multicast_secs(seg_dicts, speed)
    if total_secs <= max_secs * 0.85:
        return await mc.synthesize_chapter_to_file(
            seg_dicts, output_path, speed=speed, chapter_title=chapter_title,
        )

    # 按 segment 边界切分（不切断台词行）
    target_chars = int(_MC_CHUNK_TARGET_SECS * _MC_CHARS_PER_SEC * max(0.2, float(speed or 1.0)))
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for s in seg_dicts:
        seg_chars = len((s.get("text") or "")) if (s.get("kind") or "") != "silence" else 0
        if cur and cur_chars + seg_chars > target_chars:
            chunks.append(cur)
            cur, cur_chars = [], 0
        cur.append(s)
        cur_chars += seg_chars
    if cur:
        chunks.append(cur)

    logger.info(
        f"[multicast] 预估 {total_secs:.0f}s 超过单次上限 {max_secs:.0f}s，"
        f"分 {len(chunks)} 段生成后拼接"
    )
    part_paths: list[str] = []
    try:
        for i, chunk in enumerate(chunks):
            part_path = output_path + f".part{i:03d}"
            part_paths.append(part_path)
            title = chapter_title if i == 0 else f"{chapter_title}（续{i}）"
            await mc.synthesize_chapter_to_file(
                chunk, part_path, speed=speed, chapter_title=title,
            )
        merged = concat_mp3_files(*[Path(p).read_bytes() for p in part_paths])
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(merged)
        os.replace(tmp_path, output_path)
        dur_ms = int(len(merged) / 16.0)
        return output_path, dur_ms
    finally:
        for p in part_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


def _write_chapter_timings(
    build_id: str,
    ch_idx: int,
    segs: list[_Segment],
    dur_list: list[int],
    *,
    total_dur_ms: int,
    estimated: bool,
    seg_text_override: dict[int, str] | None = None,
) -> None:
    """写章节时间轴 sidecar JSON（SRT/LRC 生成数据源）。

    - estimated=False（classic）：dur_list 为每段真实合成时长，start_ms 顺序累加
    - estimated=True（multicast 整章一体化）：无逐段时长，按"字符数(静音按 silence_ms)"
      占比把 total_dur_ms 分摊到各段
    sidecar 与章节 MP3 同目录同名（_timings.json 后缀），写失败仅告警不影响合成。
    """
    override = seg_text_override or {}
    try:
        weights: list[int] = []
        for i, s in enumerate(segs):
            if s.kind == "silence":
                weights.append(max(int(s.silence_ms or 0), 1))
            else:
                weights.append(max(len(override.get(i) or s.text or ""), 1))
        sum_w = sum(weights) or 1

        entries: list[dict] = []
        cursor_ms = 0
        for i, s in enumerate(segs):
            if estimated:
                dur_ms = int(total_dur_ms * weights[i] / sum_w)
            else:
                dur_ms = int(dur_list[i] or 0) if i < len(dur_list) else 0
            entries.append({
                "kind": s.kind,
                "speaker": s.speaker or "",
                "text": override.get(i) or s.text or "",
                "start_ms": cursor_ms,
                "dur_ms": dur_ms,
            })
            cursor_ms += dur_ms

        sidecar = Path(settings.AUDIO_DIR) / _timings_filename(build_id, ch_idx)
        tmp = str(sidecar) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"version": 1, "estimated": estimated, "segs": entries},
                f, ensure_ascii=False,
            )
        os.replace(tmp, sidecar)
    except Exception as e:
        logger.warning(
            f"[build_worker] 写章节时间轴失败 build_id={build_id[:8]}... ch={ch_idx}: "
            f"{type(e).__name__}: {e}"
        )


async def _run_build_inner(
    build_id: str,
    project_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
    only_chapter_idxs: list[int] | None = None,
    source_build_id: str | None = None,
) -> None:
    """
    后台合成 worker（独立 session）。
    - 每章独立合成 → BuildArtifact 更新
    - 失败章写 1s 占位静音 MP3
    - 最后生成 ZIP，Build status → success/partial_success/failed/cancelled
    """
    t0 = _time.perf_counter()
    logger.info(
        f"[build_worker] START build_id={build_id[:8]}... "
        f"project_id={project_id[:8]}... narrator={narrator_voice_id} "
        f"voices={len(voice_assignments)} speed={speed}"
        + (f" only_chapters={only_chapter_idxs}" if only_chapter_idxs else "")
    )

    factory = get_session_factory()
    only_set: set[int] | None = set(only_chapter_idxs) if only_chapter_idxs else None

    source_art_by_idx: dict[int, BuildArtifact] = {}
    if source_build_id:
        async with factory() as s:
            stmt_src = select(BuildArtifact).where(
                BuildArtifact.build_id == source_build_id
            )
            src_arts = list((await s.execute(stmt_src)).scalars().all())
            source_art_by_idx = {a.chapter_idx: a for a in src_arts}

    async with factory() as s:
        p = await s.get(Project, project_id)
        if not p:
            raise RuntimeError(f"项目不存在: {project_id}")
        chapters_dicts = json.loads(p.chapters_json or "[]")
        chapters = [
            Chapter(idx=c["idx"], title=c.get("title", ""), text=c.get("text", ""))
            for c in chapters_dicts
        ]
        total = len(chapters)
        job_title: str | None = p.book_title or p.source_filename or None
        stmt_d = select(ProjectDialogue).where(ProjectDialogue.project_id == project_id)
        all_dialogues_rows = list((await s.execute(stmt_d)).scalars().all())
        # 发音规则
        stmt_r = select(ProjectPronunciationRule).where(
            ProjectPronunciationRule.project_id == project_id
        )
        rules_rows = list((await s.execute(stmt_r)).scalars().all())
        pronunciation_rules: list[_PronunciationRule] = [
            _PronunciationRule.model_validate(r, from_attributes=True) for r in rules_rows
        ]
        if pronunciation_rules:
            logger.info(
                f"[build_worker] 加载 {len(pronunciation_rules)} 条发音规则"
            )
        # 角色表 → speaker_name → character_id 映射
        stmt_c = select(ProjectCharacter).where(ProjectCharacter.project_id == project_id)
        char_rows = list((await s.execute(stmt_c)).scalars().all())
        speaker_to_char_id: dict[str, int] = {
            c.name: c.id for c in char_rows if c.name
        }


    dialogues_by_chapter: dict[int, list[ProjectDialogue]] = {}
    for d in all_dialogues_rows:
        dialogues_by_chapter.setdefault(d.chapter_idx, []).append(d)
    for lst in dialogues_by_chapter.values():
        lst.sort(key=lambda x: x.anchor_start)

    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b:
            raise RuntimeError(f"Build 不存在: {build_id}")
        build_mode = (b.mode or "classic").lower()
        strict_mode = _should_strict_fail(build_mode)
        tts_provider_label = b.tts_provider or "minimax"
        # 情感/语气配置快照（build 启动时从 ProjectCharacter 拷贝，这里只读快照）
        narrator_emotion = (b.narrator_emotion or "").strip()
        narrator_instruction = (b.narrator_instruction or "").strip()
        try:
            voice_styles: dict[str, dict[str, str]] = json.loads(b.voice_styles_json or "{}")
        except Exception:
            voice_styles = {}
        # 条件状态迁移 queued/running → running：
        # worker 启动与用户 cancel 存在竞态（cancel 可能已写终态），
        # 终态一律不复活，worker 直接退出（否则被取消的 build 会被
        # 迟到的 running 覆盖，后续 start_build 又误判"已有活跃 build"）。
        from sqlalchemy import update as _sa_update
        res = await s.execute(
            _sa_update(Build)
            .where(
                Build.build_id == build_id,
                Build.status.in_(("queued", "running")),
            )
            .values(
                status="running",
                started_at=datetime.now(UTC).replace(tzinfo=None),
                progress_msg=f"开始合成 1/{total} 章…",
            )
        )
        await s.commit()
        if (res.rowcount or 0) == 0:
            logger.warning(
                f"[build_worker] build_id={build_id[:8]}... 启动时已是终态 "
                f"status={b.status}（已取消/失败），不复活，worker 直接退出"
            )
            return
        # 严格契约：multicast 只能配豆包（Seed-Audio）。
        # retry/历史数据行不会重新走 start_build 校验，这里兜底拦截，
        # 防止静默降级到 classic 逐句合成。
        _validate_multicast_provider(build_mode, tts_provider_label)

    logger.info(
        f"[build_worker] build_id={build_id[:8]}... total_chapters={total} "
        f"mode={build_mode} strict={strict_mode} tts_provider={tts_provider_label} "
        f"styles={len(voice_styles)} narrator_emo={narrator_emotion!r}"
    )

    from ..ai.factory import get_tts_sem
    tts = get_tts(None if tts_provider_label in ("", None) else tts_provider_label)
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)
    sem = get_tts_sem()

    chapter_outputs: list[tuple[str | None, int | None]] = [(None, None)] * total
    completed = 0
    failed_count = 0
    chapter_ok_flag: dict[int, bool] = {}
    strict_abort_flag = False  # strict 模式下首次失败即标记后续章节 skip
    # TTS 用量计数（真实供应商调用；缓存命中不计 calls，multicast 每章 1 次）
    tts_calls_used = 0
    tts_chars_used = 0

    for ch_idx, ch in enumerate(chapters):
        # strict 模式一旦出现失败，后续章节直接 skip 标记 failed（不写文件）
        if strict_abort_flag:
            chapter_ok_flag[ch_idx] = False
            failed_count += 1
            chapter_outputs[ch_idx] = (None, None)
            async with factory() as s:
                stmt_art = select(BuildArtifact).where(
                    BuildArtifact.build_id == build_id,
                    BuildArtifact.chapter_idx == ch_idx,
                )
                art = (await s.execute(stmt_art)).scalar_one_or_none()
                if art:
                    art.status = "failed"
                    art.error_msg = "strict 模式：前序章节失败，已中止后续章节合成"
                    art.audio_filename = None
                    art.audio_url = None
                    art.duration_ms = None
                await s.commit()
            continue
        cancelled_now = False
        async with factory() as s:
            b_check = await s.get(Build, build_id)
            if b_check and b_check.status == "cancelled":
                cancelled_now = True
        if cancelled_now:
            logger.warning(
                f"[build_worker] build_id={build_id[:8]}... "
                f"检测到 cancelled 标志，停止合成 at ch {ch_idx+1}/{total}"
            )
            async with factory() as s:
                b2 = await s.get(Build, build_id)
                if b2:
                    b2.completed_chapters = completed
                    b2.progress_msg = f"已取消：已完成 {completed}/{total} 章"
                    b2.completed_at = datetime.now(UTC).replace(tzinfo=None)
                    await s.commit()
            break

        ch_t0 = _time.perf_counter()

        if only_set is not None and ch.idx not in only_set:
            src_art = source_art_by_idx.get(ch.idx)
            ch_ok_path: Path | None = None
            ch_ok_duration_ms: int | None = None
            if src_art and src_art.status == "done" and src_art.audio_filename:
                p_src = audio_dir / src_art.audio_filename
                if p_src.is_file():
                    ch_ok_path = p_src
                    ch_ok_duration_ms = src_art.duration_ms
            if ch_ok_path is None and src_art and src_art.audio_filename:
                p_src = audio_dir / src_art.audio_filename
                if p_src.is_file():
                    ch_ok_path = p_src
            if ch_ok_path is None:
                p_normal = audio_dir / _audio_filename(build_id, ch_idx, failed=False)
                if p_normal.is_file():
                    ch_ok_path = p_normal

            if ch_ok_path is not None:
                mp3_bytes = ch_ok_path.read_bytes()
                ch_ok_duration_ms = int(ch_ok_duration_ms or 0) or _estimate_mp3_duration_ms(mp3_bytes)
                chapter_outputs[ch_idx] = (str(ch_ok_path), ch_ok_duration_ms)
                completed += 1
                chapter_ok_flag[ch_idx] = True
                async with factory() as s:
                    stmt_art = select(BuildArtifact).where(
                        BuildArtifact.build_id == build_id,
                        BuildArtifact.chapter_idx == ch_idx,
                    )
                    art = (await s.execute(stmt_art)).scalar_one_or_none()
                    if art and art.status != "done":
                        art.status = "done"
                        art.audio_filename = ch_ok_path.name
                        art.audio_url = f"/media/{ch_ok_path.name}"
                        art.duration_ms = ch_ok_duration_ms
                        art.error_msg = None
                        await s.commit()
                    b = await s.get(Build, build_id)
                    if b:
                        b.completed_chapters = completed
                        await s.commit()
                logger.info(
                    f"[build_worker] build_id={build_id[:8]}... "
                    f"ch {ch_idx+1}/{total} 从 source_build 复用 → skip"
                )
                continue
            else:
                chapter_outputs[ch_idx] = (None, None)
                chapter_ok_flag[ch_idx] = True
                continue

        skip_this_chapter = False
        ch_ok_path = None
        ch_ok_duration_ms = None
        async with factory() as s:
            stmt_art = select(BuildArtifact).where(
                BuildArtifact.build_id == build_id,
                BuildArtifact.chapter_idx == ch_idx,
            )
            art = (await s.execute(stmt_art)).scalar_one_or_none()

            if art and art.status == "done" and art.audio_filename:
                p = audio_dir / art.audio_filename
                if p.is_file():
                    ch_ok_path = p
                    ch_ok_duration_ms = art.duration_ms
            if ch_ok_path is None:
                p_normal = audio_dir / _audio_filename(build_id, ch_idx, failed=False)
                if p_normal.is_file():
                    ch_ok_path = p_normal
                    ch_ok_duration_ms = None
                    if art is not None:
                        art.status = "done"
                        art.audio_filename = p_normal.name
                        art.audio_url = f"/media/{p_normal.name}"
                        art.error_msg = None

            if ch_ok_path is not None:
                mp3_bytes = ch_ok_path.read_bytes()
                ch_ok_duration_ms = int(ch_ok_duration_ms or 0) or _estimate_mp3_duration_ms(mp3_bytes)
                chapter_outputs[ch_idx] = (str(ch_ok_path), ch_ok_duration_ms)
                completed += 1
                chapter_ok_flag[ch_idx] = True
                skip_this_chapter = True
                if art is not None and art in s.dirty:
                    art.duration_ms = ch_ok_duration_ms
                    await s.commit()
                logger.info(
                    f"[build_worker] build_id={build_id[:8]}... "
                    f"ch {ch_idx+1}/{total} 已完成 → skip (MP3={ch_ok_path.name})"
                )
        if skip_this_chapter:
            async with factory() as s:
                b = await s.get(Build, build_id)
                if b and b.status == "cancelled":
                    logger.warning(f"[build_worker] cancelled after skip ch {ch_idx+1}")
                    async with factory() as s2:
                        b2 = await s2.get(Build, build_id)
                        if b2:
                            b2.completed_chapters = completed
                            b2.progress_msg = f"已取消：已完成 {completed}/{total} 章"
                            b2.completed_at = datetime.now(UTC).replace(tzinfo=None)
                            await s2.commit()
                    break
                if b:
                    b.completed_chapters = completed
                    b.progress_msg = f"已跳过第 {ch_idx+1}/{total} 章（已完成）：{ch.title}"
                await s.commit()
            continue

        async with factory() as s:
            stmt_art = select(BuildArtifact).where(
                BuildArtifact.build_id == build_id,
                BuildArtifact.chapter_idx == ch_idx,
            )
            art = (await s.execute(stmt_art)).scalar_one_or_none()
            if art:
                art.status = "synthesizing"
                art.error_msg = None
            b = await s.get(Build, build_id)
            if b:
                b.progress_msg = f"正在合成第 {ch_idx+1}/{total} 章：{ch.title}"
            await s.commit()

        try:
            ch_dialogues = dialogues_by_chapter.get(ch_idx, [])

            segs, _ = _build_segments_for_chapter(
                ch, ch_dialogues,
                narrator_voice_id=narrator_voice_id,
                voice_assignments=voice_assignments,
                segment_overrides=None,
                start_idx=0,
                narrator_emotion=narrator_emotion,
                narrator_instruction=narrator_instruction,
                speaker_styles=voice_styles,
            )

            # ---------- 多播剧模式：Seed-Audio 整章一体化生成 ----------
            if build_mode == "multicast" and (tts_provider_label or "").lower() == "doubao":
                from ..ai.factory import get_multicast_tts
                from ..ai.providers.doubao.multicast import MAX_CHAPTER_AUDIO_SECS
                mc = get_multicast_tts()
                ch_fname = _audio_filename(build_id, ch_idx, failed=False)
                ch_fpath = str(audio_dir / ch_fname)
                seg_dicts = [
                    {
                        "kind": s.kind,
                        "speaker": s.speaker,
                        "text": apply_pronunciation_rules(
                            s.text, pronunciation_rules,
                            character_id=speaker_to_char_id.get(s.speaker or ""),
                        ) if pronunciation_rules else s.text,
                        # 角色未分配音色时兜底旁白音色：否则该角色出现在剧本
                        # prompt 里却没有对应 roles 音色映射
                        "voice_id": s.voice_id or narrator_voice_id,
                        "silence_ms": s.silence_ms,
                    }
                    for s in segs
                ]
                _, ch_dur_ms = await _multicast_synth_chapter(
                    mc, seg_dicts, ch_fpath,
                    speed=speed,
                    chapter_title=ch.title,
                    max_secs=MAX_CHAPTER_AUDIO_SECS,
                )
                chapter_outputs[ch_idx] = (ch_fpath, ch_dur_ms)
                # 用量：Seed-Audio 整章一次调用；chars 计非静音段文本
                tts_calls_used += 1
                tts_chars_used += sum(
                    len(sd.get("text") or "") for sd in seg_dicts
                    if (sd.get("kind") or "") != "silence"
                )
                # 整章一体化无逐段时长 → 按字符占比估算时间轴（SRT 用）
                _write_chapter_timings(
                    build_id, ch_idx, segs,
                    [0] * len(segs),
                    total_dur_ms=ch_dur_ms,
                    estimated=True,
                    seg_text_override={i: sd["text"] for i, sd in enumerate(seg_dicts)},
                )

                async with factory() as s:
                    stmt_art = select(BuildArtifact).where(
                        BuildArtifact.build_id == build_id,
                        BuildArtifact.chapter_idx == ch_idx,
                    )
                    art = (await s.execute(stmt_art)).scalar_one_or_none()
                    if art:
                        art.status = "done"
                        art.audio_filename = Path(ch_fpath).name
                        art.audio_url = f"/media/{Path(ch_fpath).name}"
                        art.duration_ms = ch_dur_ms
                        art.error_msg = None
                    completed += 1
                    chapter_ok_flag[ch_idx] = True
                    await s.commit()

                logger.info(
                    f"[build_worker] build_id={build_id[:8]}... ch {ch_idx+1}/{total} "
                    f"multicast done title={ch.title!r} dur_ms={ch_dur_ms}"
                )
                continue

            async def _synth_seg(s: _Segment) -> tuple[_Segment, bytes, int]:
                nonlocal tts_calls_used, tts_chars_used
                if s.kind == "silence":
                    return s, make_silent_mp3(max(s.silence_ms, 1)), s.silence_ms
                vid = s.voice_id or narrator_voice_id
                # 应用发音规则
                if pronunciation_rules:
                    char_id = speaker_to_char_id.get(s.speaker or "")
                    s.text = apply_pronunciation_rules(s.text, pronunciation_rules, character_id=char_id)
                seg_emo = (s.emotion or "").strip()
                seg_ins = (s.instruction or "").strip()
                cached = await tts_segment_cache_get(
                    vid, speed, s.text, emotion=seg_emo, instruction=seg_ins,
                )
                if cached is not None:
                    mp3_b, dur_ms = cached
                    return s, mp3_b, dur_ms
                async with sem:
                    data, dur = await tts.synthesize_to_bytes(
                        s.text, vid, speed=speed,
                        # 未配置时传 provider 默认（"calm"），与历史行为一致
                        emotion=seg_emo or "calm",
                        instruction_text=seg_ins or None,
                    )
                tts_calls_used += 1
                tts_chars_used += len(s.text)
                await tts_segment_cache_put(
                    vid, speed, s.text, data, dur, emotion=seg_emo, instruction=seg_ins,
                )
                return s, data, dur

            tasks = [_synth_seg(seg) for seg in segs]
            results = await asyncio.gather(*tasks)

            ch_bytes = concat_mp3_files(*[r[1] for r in results])
            ch_fname = _audio_filename(build_id, ch_idx, failed=False)
            ch_fpath = str(audio_dir / ch_fname)
            # 原子写：先写 .tmp 再 os.replace，避免崩溃留半成品
            tmp_fpath = ch_fpath + ".tmp"
            with open(tmp_fpath, "wb") as f:
                f.write(ch_bytes)
            os.replace(tmp_fpath, ch_fpath)
            ch_dur_ms = _estimate_mp3_duration_ms(ch_bytes)
            chapter_outputs[ch_idx] = (ch_fpath, ch_dur_ms)

            # 章内时间轴 sidecar（SRT/LRC 用）：gather 保序 → results[i] 对应 segs[i]
            _write_chapter_timings(
                build_id, ch_idx, segs,
                [r[2] for r in results],
                total_dur_ms=ch_dur_ms,
                estimated=False,
            )

            async with factory() as s:
                stmt_art = select(BuildArtifact).where(
                    BuildArtifact.build_id == build_id,
                    BuildArtifact.chapter_idx == ch_idx,
                )
                art = (await s.execute(stmt_art)).scalar_one_or_none()
                if art:
                    art.status = "done"
                    art.audio_filename = ch_fname
                    art.audio_url = f"/media/{ch_fname}"
                    art.duration_ms = ch_dur_ms
                    art.error_msg = None
                b = await s.get(Build, build_id)
                completed += 1
                chapter_ok_flag[ch_idx] = True
                if b:
                    b.completed_chapters = completed
                    if b.status == "cancelled":
                        logger.warning(f"[build_worker] cancelled after done ch {ch_idx+1}")
                        b.progress_msg = f"已取消：已完成 {completed}/{total} 章"
                        b.completed_at = datetime.now(UTC).replace(tzinfo=None)
                await s.commit()
                if b and b.status == "cancelled":
                    break

            logger.info(
                f"[build_worker] build_id={build_id[:8]}... ch {ch_idx+1}/{total} "
                f"done title={ch.title!r} dur_ms={ch_dur_ms} "
                f"ms={int((_time.perf_counter()-ch_t0)*1000)}"
            )

        except Exception as ch_err:
            logger.error(
                f"[build_worker] build_id={build_id[:8]}... ch {ch_idx+1}/{total} "
                f"FAIL: {type(ch_err).__name__}: {ch_err}",
                exc_info=True,
            )
            failed_count += 1
            chapter_ok_flag[ch_idx] = False

            # strict 模式（multicast + MULTICAST_STRICT_MODE）：
            #   - 不写占位静音 MP3
            #   - BuildArtifact 仅 status=failed + error_msg，audio_filename/audio_url 留空
            #   - 置 strict_abort_flag，后续章跳过多走 failed
            if strict_mode:
                strict_abort_flag = True
                chapter_outputs[ch_idx] = (None, None)
                async with factory() as s:
                    stmt_art = select(BuildArtifact).where(
                        BuildArtifact.build_id == build_id,
                        BuildArtifact.chapter_idx == ch_idx,
                    )
                    art = (await s.execute(stmt_art)).scalar_one_or_none()
                    if art:
                        art.status = "failed"
                        art.audio_filename = None
                        art.audio_url = None
                        art.duration_ms = None
                        art.error_msg = f"{type(ch_err).__name__}: {ch_err}"[:500]
                    b = await s.get(Build, build_id)
                    if b:
                        b.completed_chapters = completed
                        if b.status == "cancelled":
                            logger.warning(f"[build_worker] cancelled after fail ch {ch_idx+1}")
                            b.progress_msg = f"已取消：已完成 {completed}/{total} 章"
                            b.completed_at = datetime.now(UTC).replace(tzinfo=None)
                    await s.commit()
                    if b and b.status == "cancelled":
                        break
                # strict 模式：首次失败立即终止后续章节合成
                logger.warning(
                    f"[build_worker] strict 模式下中止：build_id={build_id[:8]}... "
                    f"ch {ch_idx+1}/{total} 失败 → 直接整包 failed"
                )
                break  # 退出 for ch_idx, ch in enumerate(chapters)
            # --- 非 strict：降级逻辑（占位静音 MP3 + partial_success）---
            placeholder_bytes = make_silent_mp3(1000)
            ph_fname = _audio_filename(build_id, ch_idx, failed=True)
            ph_fpath = str(audio_dir / ph_fname)
            with open(ph_fpath, "wb") as f:
                f.write(placeholder_bytes)
            chapter_outputs[ch_idx] = (ph_fpath, 1000)

            async with factory() as s:
                stmt_art = select(BuildArtifact).where(
                    BuildArtifact.build_id == build_id,
                    BuildArtifact.chapter_idx == ch_idx,
                )
                art = (await s.execute(stmt_art)).scalar_one_or_none()
                if art:
                    art.status = "failed"
                    art.audio_filename = ph_fname
                    art.audio_url = f"/media/{ph_fname}"
                    art.duration_ms = 1000
                    art.error_msg = f"{type(ch_err).__name__}: {ch_err}"[:500]
                b = await s.get(Build, build_id)
                if b:
                    b.completed_chapters = completed
                    if b.status == "cancelled":
                        logger.warning(f"[build_worker] cancelled after fail ch {ch_idx+1}")
                        b.progress_msg = f"已取消：已完成 {completed}/{total} 章"
                        b.completed_at = datetime.now(UTC).replace(tzinfo=None)
                await s.commit()
                if b and b.status == "cancelled":
                    break
            continue

    was_cancelled = False
    async with factory() as s:
        b_check2 = await s.get(Build, build_id)
        if b_check2 and b_check2.status == "cancelled":
            was_cancelled = True
    if was_cancelled:
        total_elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[build_worker] CANCELLED build_id={build_id[:8]}... total_ms={total_elapsed_ms} "
            f"completed={completed}/{total} failed={failed_count}"
        )
        return

    logger.info(
        f"[build_worker] build_id={build_id[:8]}... packaging {total} chapters "
        f"(completed={completed} failed={failed_count})"
    )
    total_ms = 0
    total_size_bytes = 0
    for _p, _d in chapter_outputs:
        if _p:
            try:
                total_size_bytes += os.path.getsize(_p)
            except OSError:
                pass
        total_ms += _d or 0

    # strict 模式 + 有失败：不生成 ZIP（避免打包无意义文件），最终状态强制 failed
    strict_final_failed = False
    if strict_mode and failed_count > 0:
        strict_final_failed = True
        final_status = "failed"
        zip_fname = None  # type: ignore
    else:
        zip_fname = _zip_filename(build_id)
        zip_path = str(audio_dir / zip_fname)
        _build_book_zip(
            zip_path,
            job_id=build_id,
            job_title=job_title,
            chapter_outputs=chapter_outputs,
            chapter_titles=[c.title for c in chapters],
        )

        if failed_count == 0:
            final_status = "success"
        elif completed >= 1 and total >= 1:
            final_status = "partial_success"
        else:
            final_status = "failed"

    if only_set is not None:
        this_retry_failed = sorted([ch_idx for ch_idx, ok in chapter_ok_flag.items() if ch_idx in only_set and not ok])
    else:
        this_retry_failed = [i for i, ok in chapter_ok_flag.items() if not ok]

    async with factory() as s:
        b = await s.get(Build, build_id)
        if b:
            b.status = final_status
            if strict_final_failed:
                b.progress_msg = (
                    f"strict 多播剧模式合成失败：{failed_count}/{total} 章出错，已中止（无占位降级）"
                )
            else:
                b.progress_msg = (
                    f"全部完成 {completed}/{total} 章"
                    + (f"（{failed_count} 章失败已用静音占位）" if failed_count else "")
                )
            b.zip_filename = zip_fname
            b.total_size_bytes = 0 if strict_final_failed else total_size_bytes
            b.total_duration_ms = 0 if strict_final_failed else total_ms
            b.completed_at = datetime.now(UTC).replace(tzinfo=None)
            b.failed_chapters_json = json.dumps(sorted(this_retry_failed), ensure_ascii=False)
            # TTS 用量：真实供应商调用（缓存命中不计次）
            b.tts_calls = tts_calls_used
            b.tts_chars = tts_chars_used
            await s.commit()

    # 项目级用量汇总（UsageEvent 表，供 /usage 聚合展示）
    from .usage import record_tts_usage
    record_tts_usage(
        project_id, build_id,
        calls=tts_calls_used, chars=tts_chars_used,
        detail=f"build_{build_mode}",
    )

    # 构建结束同步项目状态：全量成功 → done（前端"已完成"）；
    # 部分成功 → partial_success；失败/取消保持 ready（用户可重新构建）。
    # 仅当这是该项目最近一次构建时才回写，避免旧 build 完成覆盖新状态。
    if final_status in ("success", "partial_success"):
        async with factory() as s:
            stmt_latest = select(Build).where(
                Build.project_id == project_id
            ).order_by(Build.created_at.desc()).limit(1)
            latest_b = (await s.execute(stmt_latest)).scalar_one_or_none()
            if latest_b and latest_b.build_id == build_id:
                p = await s.get(Project, project_id)
                if p:
                    p.status = "done" if final_status == "success" else "partial_success"
                    await s.commit()

    total_elapsed_ms = int((_time.perf_counter() - t0) * 1000)
    logger.info(
        f"[build_worker] DONE build_id={build_id[:8]}... total_ms={total_elapsed_ms} "
        f"status={final_status} completed={completed}/{total} failed={failed_count} "
        f"size_kb={total_size_bytes//1024} dur_s={round(total_ms/1000,1)} zip={zip_fname}"
    )


# =====================================================================
# 查询 / 删除
# =====================================================================

async def get_build(project_id: str, build_id: str) -> BuildDetailResp:
    """返回 build 详情（含 artifacts）。"""
    factory = get_session_factory()
    async with factory() as session:
        b = await session.get(Build, build_id)
        if not b:
            raise ValueError(f"Build 不存在: {build_id}")
        if b.project_id != project_id:
            raise ValueError(f"Build 不属于项目 {project_id}")
        stmt = select(BuildArtifact).where(
            BuildArtifact.build_id == build_id
        ).order_by(BuildArtifact.chapter_idx)
        arts = list((await session.execute(stmt)).scalars().all())
        return _build_to_detail(b, arts)


async def list_builds(project_id: str) -> list[BuildListItem]:
    """Build 历史列表（按 created_at desc）。"""
    factory = get_session_factory()
    async with factory() as session:
        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        stmt = select(Build).where(
            Build.project_id == project_id
        ).order_by(Build.created_at.desc())
        rows = list((await session.execute(stmt)).scalars().all())
        return [_build_to_list_item(b) for b in rows]


async def get_build_status(build_id: str) -> BuildStatusResp:
    """轮询用：progress + artifacts（不需要 project_id，路由里可以直接传 build_id）。"""
    factory = get_session_factory()
    async with factory() as session:
        b = await session.get(Build, build_id)
        if not b:
            raise ValueError(f"Build 不存在: {build_id}")
        stmt = select(BuildArtifact).where(
            BuildArtifact.build_id == build_id
        ).order_by(BuildArtifact.chapter_idx)
        arts = list((await session.execute(stmt)).scalars().all())
        return _build_to_status_resp(b, arts)


async def delete_build(project_id: str, build_id: str) -> None:
    """删除 build + 磁盘 MP3 / ZIP 文件。"""
    await _ensure_project_not_running(project_id, "删除 Build")

    factory = get_session_factory()
    async with factory() as session:
        b = await session.get(Build, build_id)
        if not b:
            return
        if b.project_id != project_id:
            raise ValueError(f"Build 不属于项目 {project_id}")

        stmt_art = select(BuildArtifact.audio_filename).where(
            BuildArtifact.build_id == build_id
        )
        art_filenames = [r for r in (await session.execute(stmt_art)).scalars().all() if r]
        zip_fname = b.zip_filename

        await session.delete(b)
        await session.commit()

    audio_dir = Path(settings.AUDIO_DIR)
    for fname in art_filenames:
        try:
            fpath = audio_dir / fname
            if fpath.is_file():
                fpath.unlink()
        except OSError as e:
            logger.warning(f"[build_delete] 删音频文件失败: {fname} -> {e}")
        # 同名时间轴 sidecar 一并清理
        sidecar = audio_dir / _timings_filename_of_audio(fname)
        try:
            if sidecar.is_file():
                sidecar.unlink()
        except OSError as e:
            logger.warning(f"[build_delete] 删时间轴文件失败: {sidecar.name} -> {e}")
    if zip_fname:
        try:
            fpath = audio_dir / zip_fname
            if fpath.is_file():
                fpath.unlink()
        except OSError as e:
            logger.warning(f"[build_delete] 删 ZIP 失败: {zip_fname} -> {e}")
    # M4B 产物一并清理
    m4b_fname = m4b_filename(build_id)
    try:
        fpath = audio_dir / m4b_fname
        if fpath.is_file():
            fpath.unlink()
    except OSError as e:
        logger.warning(f"[build_delete] 删 M4B 失败: {m4b_fname} -> {e}")

    logger.info(
        f"[build_delete] build_id={build_id[:8]}... "
        f"deleted audio_files={len(art_filenames)} zip={'yes' if zip_fname else 'no'}"
    )


_run_seg_cache_gc_if_needed(force=False)


# =====================================================================
# 合成前预估（不调用任何外部 API，零成本）
# =====================================================================

class BuildEstimateResp(BaseModel):
    """GET /projects/{id}/estimate：开始合成前的成本/时长预估。"""
    chapter_count: int
    total_chars: int
    est_tts_segments: int          # 非静音分段数 ≈ TTS 调用上限（未计缓存命中）
    est_audio_minutes: float       # 预估成品音频时长（分钟）
    est_zip_mb: float              # 预估音频体积（MB，128kbps 经验值）
    est_llm_calls: int             # prepare 阶段 LLM 调用估算（已 prepare 过则为 0）
    prepared: bool                 # 项目是否已 prepare（决定 est_llm_calls 是否有意义）
    has_dialogues: bool            # 是否已有对白归属数据


async def estimate_project_build(project_id: str, speed: float = 1.0) -> BuildEstimateResp:
    """按当前章节/对白数据估算一次 build 的规模。

    中文 TTS 语速经验值：约 4.2 字/秒（1.0x），MP3 128kbps ≈ 16KB/s。
    缓存命中会显著减少真实调用量，这里给的是"冷缓存上限"。
    """
    from ..core.config import settings as _settings

    factory = get_session_factory()
    async with factory() as s:
        p = await s.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        chapters_dicts = json.loads(p.chapters_json or "[]") if p.chapters_json else []
        chapters = [
            Chapter(idx=c["idx"], title=c.get("title", ""), text=c.get("text", ""))
            for c in chapters_dicts
        ]
        prepared = bool(p.chapters_json)
        stmt_d = select(ProjectDialogue).where(ProjectDialogue.project_id == project_id)
        all_dialogues = list((await s.execute(stmt_d)).scalars().all())

    dialogues_by_chapter: dict[int, list] = {}
    for d in all_dialogues:
        dialogues_by_chapter.setdefault(d.chapter_idx, []).append(d)
    for lst in dialogues_by_chapter.values():
        lst.sort(key=lambda x: x.anchor_start)

    total_chars = sum(len(ch.text) for ch in chapters)
    seg_count = 0
    for ch in chapters:
        segs, _ = _build_segments_for_chapter(
            ch, dialogues_by_chapter.get(ch.idx, []),
            narrator_voice_id="est", voice_assignments={},
            segment_overrides=None, start_idx=0,
        )
        seg_count += sum(1 for sg in segs if sg.kind != "silence")

    sp = max(0.5, min(2.0, float(speed or 1.0)))
    chars_per_sec = 4.2 * sp
    est_audio_sec = total_chars / chars_per_sec if total_chars else 0
    # MP3 128kbps ≈ 16KB/s
    est_zip_mb = est_audio_sec * 16.0 / (1024.0 * 1024.0)

    # LLM prepare 估算（角色切片 + 消歧批次 + 对白批次 + 音色推荐）
    slice_size = max(1, int(_settings.LLM_CHAR_EXTRACT_SLICE_SIZE))
    dlg_batch = max(1, int(_settings.DIALOGUE_BATCH_CHAPTERS))
    est_llm_calls = 0
    if not prepared:
        import math
        est_llm_calls = (
            math.ceil(total_chars / slice_size)      # 角色提取切片
            + 2                                       # 消歧（通常 1-2 批）
            + math.ceil(max(len(chapters), 1) / dlg_batch)  # 对白归属批次
            + 1                                       # 音色推荐
        )

    return BuildEstimateResp(
        chapter_count=len(chapters),
        total_chars=total_chars,
        est_tts_segments=seg_count,
        est_audio_minutes=round(est_audio_sec / 60.0, 1),
        est_zip_mb=round(est_zip_mb, 1),
        est_llm_calls=est_llm_calls,
        prepared=prepared,
        has_dialogues=len(all_dialogues) > 0,
    )


class ProjectUsageResp(BaseModel):
    """GET /projects/{id}/usage：项目累计供应商用量。"""
    llm_calls: int
    llm_chars: int
    tts_calls: int
    tts_chars: int
    # 最近若干条明细（时间倒序）
    recent: list[dict]


async def get_project_usage(project_id: str, limit: int = 20) -> ProjectUsageResp:
    from ..db.models import UsageEvent
    factory = get_session_factory()
    async with factory() as s:
        p = await s.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        stmt = select(UsageEvent).where(UsageEvent.project_id == project_id).order_by(
            UsageEvent.id.desc()
        )
        rows = list((await s.execute(stmt)).scalars().all())
    llm_calls = sum(r.calls for r in rows if r.kind == "llm")
    llm_chars = sum(r.chars for r in rows if r.kind == "llm")
    tts_calls = sum(r.calls for r in rows if r.kind == "tts")
    tts_chars = sum(r.chars for r in rows if r.kind == "tts")
    recent = [
        {
            "id": r.id,
            "kind": r.kind,
            "detail": r.detail,
            "calls": r.calls,
            "chars": r.chars,
            "build_id": r.build_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows[: max(1, min(int(limit), 100))]
    ]
    return ProjectUsageResp(
        llm_calls=llm_calls, llm_chars=llm_chars,
        tts_calls=tts_calls, tts_chars=tts_chars,
        recent=recent,
    )
