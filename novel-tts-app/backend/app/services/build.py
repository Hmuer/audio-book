"""
项目制 Build 服务：基于 Project 表的整本合成（Project → Build → BuildArtifact）。

设计要点：
- start_build: 创建 Build 记录 + 每章一条 pending BuildArtifact，启动后台任务立即返回
- _run_build_inner: 后台 worker（独立 session），逐章合成→更新 BuildArtifact；失败章写占位静音 MP3
- 幂等（合成阶段三层去重）：
  1) start_build 内存锁 _RUNNING_BUILDS（按 project_id）+ DB Build.status=running/queued
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
)
from ..db.session import get_session_factory
from ..ai.factory import get_tts
from ..ai.providers.minimax.tts import (
    make_silent_mp3,
    concat_mp3_files,
    _estimate_mp3_duration_ms,
)
from .chapter import Chapter, _Segment, _build_segments_for_chapter

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
            clean_title = _sanitize_zip_entry(raw_title, f"章节{i+1}")
            base = f"{i+1:03d}_{clean_title}"
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
    zip_url: str | None
    total_size_kb: int | None
    total_duration_sec: float | None
    started_at: str | None
    completed_at: str | None
    created_at: str | None
    artifacts: list[BuildArtifactResp]
    failed_chapters: list[int] | None
    is_retry: bool


class BuildListItem(BaseModel):
    """Build 列表项（精简）。"""
    build_id: str
    status: str
    total_chapters: int
    completed_chapters: int
    started_at: str | None
    completed_at: str | None
    created_at: str | None
    failed_chapters: list[int] | None
    is_retry: bool


class BuildStatusResp(BaseModel):
    """轮询用：progress + artifacts。"""
    build_id: str
    status: str
    progress_msg: str | None
    completed_chapters: int
    total_chapters: int
    artifacts: list[BuildArtifactResp]
    failed_chapters: list[int] | None
    is_retry: bool


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
    """narrator 为空时兜底到 male-qn-jingying；音色库无此 id 时取第一个。"""
    if narrator_voice_id:
        return narrator_voice_id
    tts = get_tts()
    voices = await tts.list_voices()
    if voices:
        vid = (
            next((v["id"] for v in voices if v.get("id") == "male-qn-jingying"), None)
            or voices[0].get("id", "")
        )
        return vid
    raise RuntimeError("音色库为空，无法合成")


_RUNNING_BUILDS: set[str] = set()
_RUNNING_LOCK = asyncio.Lock()


async def _ensure_project_not_running(project_id: str, action: str):
    async with _RUNNING_LOCK:
        if project_id in _RUNNING_BUILDS:
            raise ValueError(f"项目 {project_id[:8]} 正在合成（Build 运行中），无法{action}。请先取消当前 Build。")


def _audio_filename(build_id: str, ch_idx: int, failed: bool = False) -> str:
    """每章 MP3 文件名；failed 章单独命名以便排查。"""
    suffix = "_failed" if failed else ""
    return f"build_{build_id}_ch{ch_idx:04d}{suffix}.mp3"


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


def _seg_cache_key(voice_id: str, speed: float, text: str) -> str:
    raw = f"v1|{voice_id}|{speed:.4f}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seg_cache_mp3_path(key: str) -> Path:
    return _seg_cache_dir() / f"{key}.mp3"


def _seg_cache_meta_path(key: str) -> Path:
    return _seg_cache_dir() / f"{key}.json"


async def tts_segment_cache_get(voice_id: str, speed: float, text: str) -> tuple[bytes, int] | None:
    """返回 (mp3_bytes, duration_ms)，未命中返回 None。先查内存，再查磁盘。"""
    key = _seg_cache_key(voice_id, speed, text)
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
    voice_id: str, speed: float, text: str, mp3_bytes: bytes, dur_ms: int
) -> None:
    """写 TTS 段缓存：内存 + 磁盘双写。"""
    key = _seg_cache_key(voice_id, speed, text)
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
# =====================================================================


def _calc_config_digest(narrator_voice_id: str, speed: float, voice_assignments: dict[str, str]) -> str:
    sorted_va = dict(sorted((voice_assignments or {}).items()))
    raw = json.dumps(
        {
            "narrator": narrator_voice_id or "",
            "speed": round(float(speed), 6),
            "va": sorted_va,
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
    )


def _build_to_list_item(b: Build) -> BuildListItem:
    return BuildListItem(
        build_id=b.build_id,
        status=b.status,
        total_chapters=b.total_chapters,
        completed_chapters=b.completed_chapters,
        started_at=b.started_at.isoformat() if b.started_at else None,
        completed_at=b.completed_at.isoformat() if b.completed_at else None,
        created_at=b.created_at.isoformat() if b.created_at else None,
        failed_chapters=_parse_failed_chapters_json(b.failed_chapters_json),
        is_retry=bool(b.is_retry),
    )


# =====================================================================
# start_build + 后台 worker
# =====================================================================

async def start_build(
    project_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
) -> BuildResp:
    """
    创建 Build + 每章 BuildArtifact（pending），启动后台 worker，立即返回。

    合成幂等（三层去重）：
      1) 内存锁 _RUNNING_BUILDS：单 worker 内同一 project 不重复
      2) DB Build.status：若已有 running/queued 的 build，直接复用
      3) **config_digest 命中**：同一 project + 相同 narrator/speed/voice_assignments，
         且历史已有**成功** Build（ZIP 生成过）→ 直接返回旧 build_id，不重建。
         （细粒度"某章没完成"的情况，由 worker 内部章级 skip 继续补做，而不是直接复用整个 build）
    """
    _run_seg_cache_gc_if_needed(force=False)

    narrator_voice_id = await _ensure_default_narrator(narrator_voice_id)
    digest = _calc_config_digest(narrator_voice_id, speed, voice_assignments)

    async with _RUNNING_LOCK:
        if project_id in _RUNNING_BUILDS:
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
            voice_assignments_json=voice_json,
            config_digest=digest,
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

    async with _RUNNING_LOCK:
        _RUNNING_BUILDS.add(project_id)

    async def _runner() -> None:
        """后台 worker：独立 session，完成后释放锁。"""
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
        except Exception as e:
            logger.error(
                f"[build_worker] FAIL build_id={build_id[:8]}... "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
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
        finally:
            async with _RUNNING_LOCK:
                _RUNNING_BUILDS.discard(project_id)

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

    async with _RUNNING_LOCK:
        _RUNNING_BUILDS.discard(project_id)

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
            if project_id in _RUNNING_BUILDS:
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
            config_digest=source_build.config_digest,
            is_retry=True,
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

    async with _RUNNING_LOCK:
        _RUNNING_BUILDS.add(project_id)

    async def _runner() -> None:
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
        except Exception as e:
            logger.error(
                f"[retry_worker] FAIL build_id={new_build_id[:8]}... "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
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
        finally:
            async with _RUNNING_LOCK:
                _RUNNING_BUILDS.discard(project_id)

    asyncio.create_task(_runner(), name=f"retry_{new_build_id[:8]}")
    logger.info(
        f"[retry_build] launched worker build_id={new_build_id[:8]}... "
        f"project_id={project_id[:8]}... retry_chapters={len(failed_ch_idxs)}"
    )
    return cur_resp


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

    dialogues_by_chapter: dict[int, list[ProjectDialogue]] = {}
    for d in all_dialogues_rows:
        dialogues_by_chapter.setdefault(d.chapter_idx, []).append(d)
    for lst in dialogues_by_chapter.values():
        lst.sort(key=lambda x: x.anchor_start)

    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b:
            raise RuntimeError(f"Build 不存在: {build_id}")
        b.status = "running"
        b.started_at = datetime.now(UTC).replace(tzinfo=None)
        b.progress_msg = f"开始合成 1/{total} 章…"
        await s.commit()

    logger.info(f"[build_worker] build_id={build_id[:8]}... total_chapters={total}")

    from ..ai.factory import get_tts_sem
    tts = get_tts()
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)
    sem = get_tts_sem()

    chapter_outputs: list[tuple[str | None, int | None]] = [(None, None)] * total
    completed = 0
    failed_count = 0
    chapter_ok_flag: dict[int, bool] = {}

    for ch_idx, ch in enumerate(chapters):
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
            )

            async def _synth_seg(s: _Segment) -> tuple[_Segment, bytes, int]:
                if s.kind == "silence":
                    return s, make_silent_mp3(max(s.silence_ms, 1)), s.silence_ms
                vid = s.voice_id or narrator_voice_id
                cached = await tts_segment_cache_get(vid, speed, s.text)
                if cached is not None:
                    mp3_b, dur_ms = cached
                    return s, mp3_b, dur_ms
                async with sem:
                    data = await tts.synthesize_to_bytes(s.text, vid, speed=speed)
                dur = _estimate_mp3_duration_ms(data)
                await tts_segment_cache_put(vid, speed, s.text, data, dur)
                return s, data, dur

            tasks = [_synth_seg(seg) for seg in segs]
            results = await asyncio.gather(*tasks)

            ch_bytes = concat_mp3_files(*[r[1] for r in results])
            ch_fname = _audio_filename(build_id, ch_idx, failed=False)
            ch_fpath = str(audio_dir / ch_fname)
            with open(ch_fpath, "wb") as f:
                f.write(ch_bytes)
            ch_dur_ms = _estimate_mp3_duration_ms(ch_bytes)
            chapter_outputs[ch_idx] = (ch_fpath, ch_dur_ms)

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
            b.progress_msg = (
                f"全部完成 {completed}/{total} 章"
                + (f"（{failed_count} 章失败已用静音占位）" if failed_count else "")
            )
            b.zip_filename = zip_fname
            b.total_size_bytes = total_size_bytes
            b.total_duration_ms = total_ms
            b.completed_at = datetime.now(UTC).replace(tzinfo=None)
            b.failed_chapters_json = json.dumps(sorted(this_retry_failed), ensure_ascii=False)
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
        return BuildStatusResp(
            build_id=build_id,
            status=b.status,
            progress_msg=b.progress_msg,
            completed_chapters=b.completed_chapters,
            total_chapters=b.total_chapters,
            artifacts=[
                BuildArtifactResp(
                    chapter_idx=a.chapter_idx,
                    title=a.title,
                    status=a.status,
                    audio_url=a.audio_url,
                    duration_ms=a.duration_ms,
                    error_msg=a.error_msg,
                )
                for a in arts
            ],
            failed_chapters=_parse_failed_chapters_json(b.failed_chapters_json),
            is_retry=bool(b.is_retry),
        )


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
    if zip_fname:
        try:
            fpath = audio_dir / zip_fname
            if fpath.is_file():
                fpath.unlink()
        except OSError as e:
            logger.warning(f"[build_delete] 删 ZIP 失败: {zip_fname} -> {e}")

    logger.info(
        f"[build_delete] build_id={build_id[:8]}... "
        f"deleted audio_files={len(art_filenames)} zip={'yes' if zip_fname else 'no'}"
    )


_run_seg_cache_gc_if_needed(force=False)
