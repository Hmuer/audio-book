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
import functools
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
from ..core.mp3_util import (
    make_silent_mp3,
    concat_mp3_files,
    mp3_duration_ms,
)
from .chapter import Chapter, _Segment, _build_segments_for_chapter
from .book_split import strip_chapter_prefix
from .chapter_store import load_chapters
from .storage import get_storage, media_key, cleanup_local_enabled
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
    chapter_lrcs: list[str] | None = None,
    start: int = 0,
    end: int | None = None,
) -> None:
    """
    把 [start, end]（0-based，闭区间）范围内的章节 MP3 打包到 ZIP。

    失败章的占位音频也会被打进 ZIP，避免缺文件。
    每章再写一份同名 .lrc 歌词（chapter_lrcs 与 MP3 按位置对齐；缺则略过）。

    F-7：支持分片 —— 每个分片是**自包含**的 ZIP（内部仍是《书名》/第NNN章.mp3+.lrc），
    章节序号按全书统一编号，因此任意一卷都能单独解压使用。
    """
    total = len(chapter_outputs)
    if total == 0:
        return
    first = max(0, int(start))
    last = total - 1 if end is None else min(int(end), total - 1)
    book_dir = _sanitize_zip_entry(job_title or job_id, f"小说_{job_id[:8]}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(first, last + 1):
            path, _dur = chapter_outputs[i]
            raw_title = chapter_titles[i] if i < len(chapter_titles) else ""
            clean_title = _sanitize_zip_entry(strip_chapter_prefix(raw_title), f"章节{i+1}")
            base = f"第{i+1:03d}章_{clean_title}"
            entry_name = f"{book_dir}/{base}.mp3"
            if path and os.path.isfile(path):
                zf.write(path, arcname=entry_name)
            else:
                zf.writestr(entry_name, make_silent_mp3(100, sample_rate=settings.DOUBAO_AUDIO_SAMPLE_RATE))
            if chapter_lrcs and i < len(chapter_lrcs) and (chapter_lrcs[i] or "").strip():
                zf.writestr(f"{book_dir}/{base}.lrc", chapter_lrcs[i])


async def _archive_file(key: str, local_path: Path, content_type: str) -> bool:
    """把本地产物归档到对象存储（G-3）。local 后端直接返回 False，不产生任何动作。

    失败只告警不抛：归档是「分发」环节，**不能**因为对象存储不可用就让整次合成失败；
    调用方约定是「归档成功才允许删本地」，所以失败时本地文件仍在（交付物不会丢）。
    """
    try:
        return await get_storage().archive(
            key=key, local_path=local_path, content_type=content_type
        )
    except Exception as e:
        logger.warning(f"[build_worker] 归档异常 key={key}: {type(e).__name__}: {e}")
        return False


async def _restore_from_object_storage(
    project_id: str, build_id: str, filename: str, dest: Path
) -> bool:
    """G-5：把对象存储里的产物回源到本地 `dest`（本地副本已被清理时用）。

    注意 `build_id` 传**产物所属的那个 build**（key 里带 build_id）——
    retry 复用时要回源的是**源 build** 的产物，而不是新 build 的。
    """
    st = get_storage()
    if not st.archives_remotely:
        return False
    try:
        return await st.fetch_to(
            key=media_key(project_id, build_id, filename), dest=dest
        )
    except Exception as e:
        logger.warning(
            f"[build_worker] 回源异常 key={filename}: {type(e).__name__}: {e}"
        )
        return False


def _zip_shard_size() -> int:
    """每个 ZIP 分片的章节数；<=0 表示不分片（退回单包）。"""
    try:
        return int(getattr(settings, "ZIP_SHARD_CHAPTERS", 0) or 0)
    except Exception:
        return 0


def _zip_shard_ranges(total: int, shard_size: int) -> list[tuple[int, int]]:
    """把 [0, total) 切成若干 (start, end) 闭区间；shard_size<=0 → 单包。"""
    if total <= 0:
        return []
    if shard_size <= 0:
        return [(0, total - 1)]
    return [
        (s, min(s + shard_size - 1, total - 1))
        for s in range(0, total, shard_size)
    ]


def _zip_shard_filename(build_id: str, start: int, end: int, total: int) -> str:
    """分片 ZIP 文件名。覆盖全部章节时沿用历史命名，避免旧路径/旧库对不上。"""
    if start <= 0 and end >= total - 1:
        return _zip_filename(build_id)
    return f"build_{build_id}_ch{start + 1:04d}-{end + 1:04d}.zip"


def _parse_zip_shards(b: Build) -> list[dict]:
    """解析 Build 的 ZIP 分片清单。

    返回 [{"filename","start","end","size_bytes"}...]（start/end 为 0-based 闭区间）。
    老库没有 zip_filenames_json → 由单个 zip_filename 合成一条，行为与改造前一致。
    """
    total = int(b.total_chapters or 0)
    last = max(total - 1, 0)
    raw = getattr(b, "zip_filenames_json", None)
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, list):
            out: list[dict] = []
            for d in data:
                if not isinstance(d, dict) or not d.get("filename"):
                    continue
                e = d.get("end")
                out.append({
                    "filename": str(d["filename"]),
                    "start": int(d.get("start") or 0),
                    "end": last if e is None else int(e),
                    "size_bytes": d.get("size_bytes"),
                })
            if out:
                return out
    if b.zip_filename:
        return [{"filename": b.zip_filename, "start": 0, "end": last, "size_bytes": None}]
    return []


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
    tts_provider: str = "doubao"
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


class ZipShardResp(BaseModel):
    """ZIP 分片（F-7）。每片自包含，可单独解压。"""
    idx: int
    filename: str
    url: str
    # 1-based，便于前端直接显示「第001-050章」
    start_chapter: int
    end_chapter: int
    size_kb: int | None


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
    tts_provider: str = "doubao"
    # 兼容字段：指向第一个分片（老前端/老链接仍可用）
    zip_url: str | None
    # F-7：全部分片清单（单包时长度为 1）
    zip_shards: list[ZipShardResp] = []
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
    tts_provider: str = "doubao"
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
    tts_provider: str = "doubao"
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
    """narrator 为空时兜底到擎苍 2.0（豆包 2.0 中音色）；音色库无此 id 时取第一个。
    注意：MiniMax TTS 已弃用，兜底改为豆包 2.0 音色；同时兼容「无前缀 legacy id」。
    """
    if narrator_voice_id:
        return narrator_voice_id
    tts = get_tts()
    voices = await tts.list_voices()
    if voices:
        vid = (
            next((v["id"] for v in voices if v.get("id") in ("doubao:zh_male_qingcang_uranus_bigtts", "zh_male_qingcang_uranus_bigtts")), None)
            or voices[0].get("id", "")
        )
        return vid
    raise RuntimeError("音色库为空，无法合成")


# ---------------------------------------------------------------------
# 未归属对白（无名说话人）的兜底音色
#
# 背景（2026-09-20 反馈）：像
#   「哈哈，知道吗，那个叫林轩的废物，得到了两瓶废丹。」
#   「……废物配废丹，岂不是正好？」另一人毫不顾忌的嘲笑道
# 这类对白在原文里**没有明确的角色归属**（"有人"/"另一人"/路人的议论）。
# 对白归属阶段会给出一个不在角色表里的 speaker（或空 speaker），于是
# `voice_assignments.get(speaker, narrator_voice_id)` 直接回退到**旁白音色** ——
# 听感上变成旁白在自言自语，完全没有"有人在说话"的层次。
#
# 处理：为每个未知 speaker 稳定地兜一个**非旁白**音色，写进本次 Build 的
# voice_assignments 快照（chapter.py 只认这张表，不必认识音色池）。
# ---------------------------------------------------------------------

# 兜底音色只从内置音色的「通用」场景里挑：避免给路边议论的路人配上
# 「猴哥 2.0」「佩奇猪 2.0」这类辨识度极高、明显不符合语境的音色。
# 注意官方音色表里该 scene 的字面值就是「通用」（不是「通用场景」）。
_UNKNOWN_SPEAKER_VOICE_SCENE = "通用"


def _unknown_speaker_voice_candidates(narrator_voice_id: str) -> list[str]:
    """兜底对白音色候选：内置「通用」场景音色（排序后 id，排除旁白音色）。

    - 排序是为了**稳定**：候选顺序固定，同一个说话人每次都会拿到同一个音色。
    - 只保留声明了中文（`zh`）能力的音色：纯外语音色（如
      `en_female_stokie_uranus_bigtts`）读中文会命中上游「成功码但音频为空」，
      中文正文配上它们等于把对白变成静音。
    """
    try:
        from ..ai.providers.doubao.tts import _BUILTIN_VOICES
    except Exception:  # pragma: no cover - 极少见的导入失败
        return []

    def _usable(v: dict[str, Any]) -> bool:
        langs = [str(x).lower() for x in (v.get("languages") or [])]
        return not langs or "zh" in langs

    general = [
        f"doubao:{v['id']}" for v in _BUILTIN_VOICES
        if _UNKNOWN_SPEAKER_VOICE_SCENE in (v.get("scene") or []) and _usable(v)
    ]
    pool = general or [
        f"doubao:{v['id']}" for v in _BUILTIN_VOICES if _usable(v)
    ]
    return sorted(vid for vid in pool if vid != narrator_voice_id)


def _fallback_voice_for_unknown_speaker(speaker: str, narrator_voice_id: str) -> str:
    """给「没有角色归属的说话人」稳定地挑一个非旁白音色。

    用 speaker 名字的哈希取模：同一个名字永远拿到同一个音色（否则同一段对白在不同
    build 里换嗓子，缓存全失效且听感漂移）；不同的名字大概率拿到不同音色 ——
    一章里同时出现「有人」「另一人」时不会撞成同一个人。
    """
    cands = _unknown_speaker_voice_candidates(narrator_voice_id)
    if not cands:
        return ""
    h = int(hashlib.sha256((speaker or "").encode("utf-8")).hexdigest()[:8], 16)
    return cands[h % len(cands)]


async def _with_unknown_speaker_voices(
    session: Any,
    project_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
) -> dict[str, str]:
    """把「未归属对白」的说话人补进 voice_assignments（不覆盖已有分配）。

    - 已有角色分配一律不动；
    - 项目**完全没有**任何角色分配时不动（那时连主角都没有音色，逐句兜底只会
      让全书对白变成同一个路人音色，还不如维持原样让用户先去做识别）；
    - 返回新 dict，调用方需用返回值覆盖原变量。
    """
    if not voice_assignments:
        return voice_assignments
    stmt = select(ProjectDialogue.speaker).where(
        ProjectDialogue.project_id == project_id
    ).distinct()
    raw_speakers = list((await session.execute(stmt)).scalars().all())

    out = dict(voice_assignments)
    unknown: set[str] = set()
    for raw in raw_speakers:
        spk = (raw or "").strip()
        if spk not in out:
            unknown.add(spk)
    if not unknown:
        return out

    filled: dict[str, str] = {}
    for spk in sorted(unknown):
        vid = _fallback_voice_for_unknown_speaker(spk or "未知说话人", narrator_voice_id)
        if vid:
            out[spk] = vid
            filled[spk or "(空)"] = vid
    if filled:
        logger.info(
            f"[build_start] project_id={project_id[:8]}... 未归属对白兜底音色 "
            f"{len(filled)} 个（非旁白）：{filled}"
        )
    return out


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


async def _record_build_usage(
    build_id: str,
    project_id: str,
    build_mode: str,
    calls: int,
    chars: int,
) -> None:
    """B-8：把 TTS 用量补记到 Build 行 + 项目级 UsageEvent。

    worker 正常结束、被取消、异常退出都会调用本函数 —— 旧实现只在「正常走到
    打包结束」时写用量，一旦检测到 cancelled 就 `return`，已真实消耗的调用与
    字数全部不入账。各退出路径保证只调一次；calls<=0（全是缓存命中）时跳过，
    不产生 0 值脏行。
    """
    if calls <= 0:
        return
    try:
        factory = get_session_factory()
        async with factory() as s:
            b = await s.get(Build, build_id)
            if b:
                b.tts_calls = calls
                b.tts_chars = chars
                await s.commit()
    except Exception as e:
        logger.warning(
            f"[build_worker] 补记 Build 用量失败 build_id={build_id[:8]}...: "
            f"{type(e).__name__}: {e}"
        )
    from .usage import record_tts_usage
    record_tts_usage(
        project_id, build_id, calls=calls, chars=chars, detail=f"build_{build_mode}",
    )


async def _apply_terminal_status(
    build_id: str,
    *,
    final_status: str,
    progress_msg: str,
    zip_filename: str | None,
    total_size_bytes: int,
    total_duration_ms: int,
    failed_chapters: list[int],
    tts_calls: int,
    tts_chars: int,
    zip_shards: list[dict] | None = None,
) -> bool:
    """B-3：仅当 Build 仍为 running 时写终态（条件更新），返回是否真的写回。

    打包 ZIP（大书可能耗时较久）期间用户可能已 cancel → status 已变 cancelled；
    旧实现无条件把 status 改回 success/partial_success，等于把用户已取消的任务
    "复活"。这里用 `UPDATE ... WHERE build_id=? AND status='running'` + rowcount
    判定：任何非 running 的既有终态都不会被覆盖。
    """
    from sqlalchemy import update as _sa_update
    factory = get_session_factory()
    async with factory() as s:
        res = await s.execute(
            _sa_update(Build)
            .where(Build.build_id == build_id, Build.status == "running")
            .values(
                status=final_status,
                progress_msg=progress_msg,
                zip_filename=zip_filename,
                zip_filenames_json=(
                    json.dumps(zip_shards, ensure_ascii=False) if zip_shards else None
                ),
                total_size_bytes=total_size_bytes,
                total_duration_ms=total_duration_ms,
                completed_at=datetime.now(UTC).replace(tzinfo=None),
                failed_chapters_json=json.dumps(sorted(failed_chapters), ensure_ascii=False),
                tts_calls=tts_calls,
                tts_chars=tts_chars,
            )
        )
        await s.commit()
        applied = (res.rowcount or 0) > 0
        if not applied:
            b_cur = await s.get(Build, build_id)
            logger.warning(
                f"[build_worker] build_id={build_id[:8]}... 终态未写回："
                f"打包期间已被取消/改变（当前 status="
                f"{b_cur.status if b_cur else 'unknown'}），保持取消态而非 {final_status}"
            )
        return applied


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


def _link_or_copy(src: Path, dst: Path) -> bool:
    """B-5：把 src 另存为 dst（优先硬链接，跨设备/不支持时退化为复制）。

    retry 复用章时用：让新 build 拥有以自身命名、自己引用的章节文件，而不是与
    source build 共享同一文件名 —— 否则 `delete_build` 按 audio_filename 无条件
    unlink，删任一方都会连带删掉另一方仍在引用的章节 MP3 / 时间轴 sidecar。
    硬链接同盘零拷贝，正常不会失败；src 不存在时返回 False（调用方静默跳过）。
    """
    if not src.is_file():
        return False
    if dst.exists():
        return True
    try:
        try:
            os.link(src, dst)
        except OSError:
            # 跨文件系统等场景硬链接不可用 → 退化为实体复制
            shutil.copy2(src, dst)
        return True
    except OSError as e:
        logger.warning(f"[build] 另存复用章文件失败 {src.name} -> {dst.name}: {e}")
        return False


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
    model: str = "", context_texts_hash: str = "",
    sample_rate: int | str = "",
    tts_model: str = "",
) -> str:
    """段级缓存键。emotion/instruction/model/context_texts/sample_rate/tts_model 参与哈希：
    不同情感 / 不同 model / 不同上下文音频 / 不同采样率一般不同，必须参与键。
    全部为空时与旧版键完全一致，历史缓存仍可命中。
    sample_rate 仅在非空时参与键（P1-4 settings 改了采样率后必须失效）。
    tts_model 是 `req_params.model`（standard / expressive）—— 它决定语音指令是否生效，
    切了它必须失效，否则改完设置重建仍会命中旧的 standard 无情绪音频。"""
    style_part = ""
    if emotion or instruction or model or context_texts_hash or sample_rate or tts_model:
        # P1-7：model + context_texts_hash 参与哈希（v3 切 model / 切 instruction_text 后必须失效）
        # P1-4：sample_rate 参与哈希（settings 改采样率后必须失效）
        style_part = (
            f"|e:{emotion}|i:{instruction}|m:{model}|c:{context_texts_hash}|sr:{sample_rate}"
            f"|tm:{tts_model}"
        )
    raw = f"v1|{voice_id}|{speed:.2f}|{text}{style_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _voice_model_lookup(voice_id: str) -> str:
    """P1-7：根据 voice_id 反查豆包模型名（seed-tts-2.0 / seed-icl-2.0）。

    优先复用 tts.py 内置的 `_voice_supports_emotion` 体系：找不到则用启发式
    （ICL 复刻音色前缀 S_ / icl_ / iclvoice → seed-icl-2.0；其他 → seed-tts-2.0）。
    供缓存 key 区分用。
    兜底用 2.0 而不是 1.0：内置表已无 1.0 音色（1.0 资源账号也未开通），
    继续兜底 1.0 只会让缓存键里留一个永远不会被真实使用的模型名。
    """
    if not voice_id:
        return ""
    # 1. ICL 复刻（判定与合成路由共用 icl.py 的同一份实现，避免前缀漂移）
    try:
        from backend.app.ai.providers.doubao.icl import is_cloned_speaker_id
        if is_cloned_speaker_id(voice_id):
            return "seed-icl-2.0"
    except Exception:  # pragma: no cover - 导入失败时退回前缀启发式
        bare = voice_id
        if bare.startswith("icl:"):
            bare = bare[4:]
        if bare.startswith("doubao:"):
            bare = bare[7:]
        if bare.startswith(("S_", "icl_", "iclvoice")):
            return "seed-icl-2.0"
    # 2. 内置音色表
    try:
        from backend.app.ai.providers.doubao.tts import _BUILTIN_VOICES
        bare = voice_id
        if bare.startswith("doubao:"):
            bare = bare[7:]
        for v in _BUILTIN_VOICES:
            if v["id"] == bare:
                return str(v.get("model") or "seed-tts-2.0")
    except Exception:
        pass
    # 3. 兜底：默认 2.0
    return "seed-tts-2.0"


def _tts_model_param() -> str:
    """`req_params.model`（settings.DOUBAO_TTS_MODEL）。

    它决定「语音指令 / 语音标签」是否生效（standard 不支持、expressive 支持，
    见 config.py 里 DOUBAO_TTS_MODEL 的注释），因此属于**会改变产出**的合成配置：
    必须参与段缓存键与 build 的 config_digest，否则用户改完设置重建，会命中
    「旧 model + 无情绪」的历史缓存 / 历史成功 build，听不出任何变化。
    空串 = 不下发该字段（旧行为）。
    """
    return str(getattr(settings, "DOUBAO_TTS_MODEL", "") or "").strip()


def _context_texts_hash(instruction: str) -> str:
    """P1-7：context_texts 字符串哈希（前 16 hex），参与缓存键区分。
    空 instruction → 返回空字符串（与旧缓存兼容）。"""
    s = (instruction or "").strip()
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _seg_cache_mp3_path(key: str) -> Path:
    return _seg_cache_dir() / f"{key}.mp3"


def _seg_cache_meta_path(key: str) -> Path:
    return _seg_cache_dir() / f"{key}.json"


async def tts_segment_cache_get(
    voice_id: str, speed: float, text: str,
    *, emotion: str = "", instruction: str = "",
    model: str = "", context_texts_hash: str = "",
    sample_rate: int | str = "",
    tts_model: str = "",
) -> tuple[bytes, int] | None:
    """返回 (mp3_bytes, duration_ms)，未命中返回 None。先查内存，再查磁盘。
    P1-7：model + context_texts_hash 参与键计算；P1-4：sample_rate 参与键计算。

    ⚠️ 这里必须与 tts_segment_cache_put 传**完全相同**的关键字参数：
    之前 get 漏传 sample_rate，导致 put 写入的键带 `|sr:<n>`、get 查询的键不带，
    缓存命中率恒为 0（每次构建都全额重调 TTS）。
    """
    key = _seg_cache_key(
        voice_id, speed, text, emotion=emotion, instruction=instruction,
        model=model, context_texts_hash=context_texts_hash,
        sample_rate=sample_rate, tts_model=tts_model,
    )
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
    model: str = "", context_texts_hash: str = "",
    sample_rate: int | str = "",
    tts_model: str = "",
) -> None:
    """写 TTS 段缓存：内存 + 磁盘双写。
    P1-7：model + context_texts_hash 参与键；P1-4：sample_rate 参与键。"""
    key = _seg_cache_key(
        voice_id, speed, text, emotion=emotion, instruction=instruction,
        model=model, context_texts_hash=context_texts_hash,
        sample_rate=sample_rate, tts_model=tts_model,
    )
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
    tts_provider: str = "doubao",
    narrator_emotion: str = "",
    narrator_instruction: str = "",
    voice_styles: dict[str, dict[str, str]] | None = None,
    content_digest: str = "",
    tts_model: str = "",
) -> str:
    sorted_va = dict(sorted((voice_assignments or {}).items()))
    sorted_styles = dict(sorted((voice_styles or {}).items()))
    raw = json.dumps(
        {
            "narrator": narrator_voice_id or "",
            "speed": round(float(speed), 6),
            "va": sorted_va,
            "mode": (mode or "classic").lower(),
            "tts_provider": (tts_provider or "doubao").lower(),
            # 情感/语气也参与摘要：改了情感但音色没变也应生成新 build
            "narrator_emotion": narrator_emotion or "",
            "narrator_instruction": narrator_instruction or "",
            "styles": sorted_styles,
            # A-7：正文/对白/发音规则的内容哈希。缺了它会出现「改了内容却复用旧产物」：
            # 用户润色正文、重新识别对白、增删发音规则后，只要音色语速不变，
            # digest 不变 → 直接命中历史成功 build，新内容永远不会被合成。
            "content": content_digest or "",
            # req_params.model（standard / expressive）决定语音指令是否真的生效，
            # 属于会影响产出的合成配置：不纳入 digest 的话，切了 model 重新合成会
            # 命中历史成功 build 直接复用旧产物，用户听不出任何变化。
            "tts_model": tts_model or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _calc_content_digest(
    session: Any, project_id: str, chapters: list[Chapter],
) -> str:
    """A-7：计算「会影响产出内容」的数据摘要（章节正文 + 对白 + 发音规则）。

    为什么需要：`_calc_config_digest` 原先只覆盖 narrator/speed/voice_assignments/
    mode/provider/情感，**不含任何内容数据**。于是「改了内容但没改音色」的场景
    （润色正文、重新 prepare 识别对白、增删发音规则）会命中历史成功 build 的复用
    逻辑，用户以为重新合成了，实际拿到的是旧产物。

    实现：流式 update sha256，避免为大部头小说额外构造大字符串。
    排序固定（按 idx / 段序 / priority），保证同一内容得到同一摘要。
    """
    h = hashlib.sha256()
    # 1) 章节正文
    for ch in chapters:
        h.update(
            f"c|{ch.idx}|{ch.title or ''}|{ch.text or ''}\n".encode("utf-8")
        )
    # 2) 对白归属
    dlg_rows = (
        await session.execute(
            select(ProjectDialogue)
            .where(ProjectDialogue.project_id == project_id)
            .order_by(
                ProjectDialogue.chapter_idx,
                ProjectDialogue.segment_index,
                ProjectDialogue.anchor_start,
                ProjectDialogue.id,
            )
        )
    ).scalars().all()
    for d in dlg_rows:
        # 逐段语音指令（ProjectDialogue.instruction）参与内容摘要：它由 prepare 的
        # instructions 阶段生成、逐段下发给豆包 TTS 2.0（context_texts），会直接改变
        # 该段的合成结果。若不纳入哈希，用户只改了/重新生成了逐段指令而音色语速没变时，
        # content_digest 不变 → 命中历史成功 build 被直接复用，新指令不会生效。
        h.update(
            (
                f"d|{d.chapter_idx}|{d.segment_index}|{d.anchor_start}|{d.anchor_end}|"
                f"{d.speaker or ''}|{d.anchor_text or ''}|{d.text or ''}|"
                f"{getattr(d, 'instruction', '') or ''}\n"
            ).encode("utf-8")
        )
    # 3) 发音规则（仅启用中的参与替换，故 enabled 一并纳入）
    rule_rows = (
        await session.execute(
            select(ProjectPronunciationRule)
            .where(ProjectPronunciationRule.project_id == project_id)
            .order_by(
                ProjectPronunciationRule.priority,
                ProjectPronunciationRule.id,
            )
        )
    ).scalars().all()
    for r in rule_rows:
        h.update(
            (
                f"r|{r.character_id}|{r.rule_type}|{r.pattern}|{r.replacement}|"
                f"{r.priority}|{int(bool(r.enabled))}\n"
            ).encode("utf-8")
        )
    return h.hexdigest()[:32]


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
        tts_provider=b.tts_provider or "doubao",
        created_at=b.created_at.isoformat() if b.created_at else None,
        failed_chapters=_parse_failed_chapters_json(b.failed_chapters_json),
        is_retry=bool(b.is_retry),
    )


def _build_to_detail(b: Build, artifacts: list[BuildArtifact]) -> BuildDetailResp:
    total_kb = (b.total_size_bytes // 1024) if b.total_size_bytes else None
    shards = _parse_zip_shards(b)
    zip_shards = [
        ZipShardResp(
            idx=i,
            filename=s["filename"],
            url=f"/media/{s['filename']}",
            start_chapter=int(s["start"]) + 1,
            end_chapter=int(s["end"]) + 1,
            size_kb=(int(s["size_bytes"]) // 1024) if s.get("size_bytes") else None,
        )
        for i, s in enumerate(shards)
    ]
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
        tts_provider=b.tts_provider or "doubao",
        zip_url=f"/media/{b.zip_filename}" if b.zip_filename else None,
        zip_shards=zip_shards,
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
        tts_provider=b.tts_provider or "doubao",
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
        tts_provider=b.tts_provider or "doubao",
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

_VALID_PROVIDERS = {"doubao"}
# 多播剧（Seed-Audio）模式已下线，合法构建模式只剩 classic
_VALID_MODES = {"classic"}
# 前缀 → 归属哪个 tts_provider（MiniMax TTS 已弃用，minimax: 不再映射）
_PREFIX_TO_PROVIDER: dict[str, str] = {
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
    """[P-2.5] 校验 mode 合法性；不再校验 tts_provider 与音色命名空间一致性。

    历史：旧版硬约束「旁白/角色音色必须属于同一 tts_provider」，导致用户每
    次配置都要先选厂商、然后只能用该厂商的音色。新行为：合成时**按 voice_id
    前缀自动路由 TTS 厂商**（doubao:/icl: → 豆包；无前缀 → tts_provider 兜底），
    用户可以混用任意音色。

    MiniMax TTS 已弃用：老数据里 Build.tts_provider == "minimax" 时给一条明确的
    迁移提示，而不是抛一个没有信息量的「未知 tts_provider」。
    """
    norm_mode = (mode or "classic").lower()
    if norm_mode not in _VALID_MODES:
        raise RuntimeError(f"未知 build.mode: {mode}，可选 {sorted(_VALID_MODES)}")
    norm_provider = (tts_provider or "").lower()
    if norm_provider == "minimax":
        raise RuntimeError(
            "该构建使用的 MiniMax 语音合成已弃用，请重新推荐音色后新建构建"
        )
    if norm_provider and norm_provider not in _VALID_PROVIDERS:
        raise RuntimeError(f"未知 tts_provider: {tts_provider}，可选 {sorted(_VALID_PROVIDERS)}")


# =====================================================================
# start_build + 后台 worker
# =====================================================================

# B-2：start_build 的「检查活跃 → 创建 Build → 注册 _ACTIVE_BUILDS」不是原子的。
# 两个并发请求（典型：前端双击「开始合成」）会同时通过 _RUNNING_LOCK 检查、
# 各自插入一条 queued Build；此后任何 `status in (queued, running)` 的
# `.scalar_one_or_none()` 都会抛 MultipleResultsFound → 接口 500，并留下两个
# 互相抢跑的 worker（重复 TTS 调用、章节文件互写、状态互相覆盖）。
#
# 修复：给「同一 project 的 start_build」加一把 project 粒度的进程内串行锁，
# 把整个 start_build（含首个活跃检查与末尾的 _ACTIVE_BUILDS 注册）串起来。
# 第二个并发请求会等第一个注册完成后再进入，于是走「already running」分支复用
# 同一 build。不同 project 互不阻塞；单机部署下 start_build 仅在用户点击时发生，
# 串行化开销可忽略。
_START_LOCKS: dict[str, asyncio.Lock] = {}


def _serialize_start_per_project(func):
    """B-2：按 project_id 串行化 start_build，消除并发创建重复 Build 的竞态。"""

    @functools.wraps(func)
    async def _wrapper(*args, **kwargs):
        project_id = kwargs.get("project_id")
        if project_id is None and args:
            project_id = args[0]
        lock = _START_LOCKS.get(project_id)
        if lock is None:
            # 无 await 点 → 事件循环内原子，不会重复创建
            lock = asyncio.Lock()
            _START_LOCKS[project_id] = lock
        async with lock:
            return await func(*args, **kwargs)

    return _wrapper


async def _start_build_impl(
    project_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
    *,
    mode: str | None = None,
    tts_provider: str | None = None,
    narrator_emotion: str = "",
    narrator_instruction: str = "",
) -> BuildResp:
    """
    创建 Build + 每章 BuildArtifact（pending），启动后台 worker，立即返回。

    新增参数：
      - mode: None（未指定 → 回落 Project.default_build_mode，再兜底 'classic'）
              / 'classic'（逐章节分段 TTS 拼接）。多播剧（Seed-Audio）模式已下线，
              传 "multicast" 会直接抛错。
              注意：显式传 'classic' 不会被项目默认值覆盖
      - tts_provider: 'doubao' | None（None 时从 Project.default_tts_provider 读取，再兜底 settings.TTS_PROVIDER）
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

    # 解析 tts_provider （优先级：参数 > Project.default_tts_provider > settings.TTS_PROVIDER > 'doubao'）
    effective_provider = (tts_provider or "").lower() or None
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
        effective_provider = (_settings_mod.TTS_PROVIDER or "doubao").lower()

    # mode 解析：只按「调用方是否显式指定」分流。
    #   mode is None → 未指定，回落 Project.default_build_mode（再兜底 classic）
    #   mode 有值     → 以调用方为准
    # 历史缺陷：旧判断 `if not mode or resolved_mode == "classic"` 因 mode 的默认值
    # 本身就是 "classic"（且 resolved_mode 由 `mode or "classic"` 得出）而**恒为真**，
    # 于是显式传 classic 也会被项目默认值覆盖 —— 只要某项目的 default_build_mode
    # 是已下线模式，其所有构建都会在下面直接抛错，且无接口可绕过（只能改数据库）。
    if mode is None:
        resolved_mode = "classic"
        factory_sess2 = get_session_factory()
        try:
            async with factory_sess2() as s:
                p = await s.get(Project, project_id)
                if p and p.default_build_mode:
                    resolved_mode = p.default_build_mode.lower()
        except Exception:
            pass
    else:
        resolved_mode = str(mode).strip().lower() or "classic"

    # 多播剧（Seed-Audio）模式已下线（P0-5）：Seed-Audio 多播剧端点已停止迭代。
    # 按用户要求（#4 不降级契约）：选了 multicast 就直接抛错，让用户明确知道
    # 该模式已不支持、需改用 classic，而不是静默切到 classic 后还按 classic
    # 跑（那会让用户以为多播剧仍在生效）。classic 模式同样支持多角色
    # （voice_assignments 角色分配 + narrator），功能上等价。
    if resolved_mode == "multicast":
        raise RuntimeError(
            "多播剧（Seed-Audio）模式已下线：mode=multicast 已不再支持，"
            "请改用 mode=classic；classic 模式同样支持多角色（voice_assignments）"
        )

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
        # F-4：正文以 project_chapters 为主路径（表为空时自动回落 chapters_json 快照）
        chapters = await load_chapters(session, project_id)
        if not chapters:
            raise RuntimeError("项目尚未 prepare（没有章节数据），请先执行识别")

        # 未归属对白兜底音色（见 _with_unknown_speaker_voices）。必须放在 digest 计算
        # **之前**：它改变了实际使用的音色映射，digest 不跟着变的话，用户重新合成会命中
        # 历史成功 build 的旧产物（匿名对白仍由旁白念）而完全听不出变化。
        voice_assignments = await _with_unknown_speaker_voices(
            session, project_id, voice_assignments, narrator_voice_id
        )

        # A-7：把正文/对白/发音规则的内容哈希纳入 digest（放在读书 chapters 之后，
        # 需要 session 查对白与发音规则）。
        content_digest = await _calc_content_digest(session, project_id, chapters)
        digest = _calc_config_digest(
            narrator_voice_id, speed, voice_assignments,
            mode=resolved_mode, tts_provider=effective_provider,
            narrator_emotion=narrator_emotion,
            narrator_instruction=narrator_instruction,
            voice_styles=voice_styles,
            content_digest=content_digest,
            tts_model=_tts_model_param(),
        )

        stmt_active = select(Build).where(
            Build.project_id == project_id,
            Build.status.in_(("queued", "running")),
        )
        active = (await session.execute(stmt_active)).scalar_one_or_none()
        if active:
            now = datetime.now(UTC).replace(tzinfo=None)
            # B-4：queued / running 都要有孤儿超时兜底。
            # running 用 started_at（无则退化到 created_at）判超时；queued 没有
            # started_at，用 created_at 判定「提交后迟迟没被任何 worker 接管」。
            # 旧实现只对 running 兜底，queued 分支直接返回该 build —— 若进程在
            # 「提交 Build(queued)、注册 worker 之前」被杀，它会永远 queued，
            # 之后每次 start_build 都返回它，项目永久无法合成。
            orphan_reason: str | None = None
            if active.status == "running":
                ref = active.started_at or active.created_at
                if ref and (now - ref > timedelta(hours=settings.BUILD_RUNNING_TIMEOUT_HOURS)):
                    orphan_reason = (
                        f"running 超过 {settings.BUILD_RUNNING_TIMEOUT_HOURS}h，"
                        "判定为被 kill 的孤儿任务，已取消并起新 Build"
                    )
            elif active.created_at and (
                now - active.created_at > timedelta(minutes=settings.BUILD_QUEUED_TIMEOUT_MINUTES)
            ):
                orphan_reason = (
                    f"queued 超过 {settings.BUILD_QUEUED_TIMEOUT_MINUTES}min，"
                    "判定为未被 worker 接管的孤儿任务，已取消并起新 Build"
                )
            if orphan_reason:
                logger.warning(
                    f"[build_start] project_id={project_id[:8]}... 孤儿 build "
                    f"{active.build_id[:8]}... status={active.status} → cancelled：{orphan_reason}"
                )
                active.status = "cancelled"
                active.progress_msg = orphan_reason
                active.completed_at = now
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


# B-2：对外暴露的 start_build 是「按 project 串行化」的包装版本。
start_build = _serialize_start_per_project(_start_build_impl)


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

        # MiniMax TTS 已弃用：老 build 无法重试（其 tts_provider/minimax: 音色都已失效），
        # 这里显式报出可操作的迁移提示，而不是继承 minimax 后在别处抛出难懂的内部错误。
        if (source_build.tts_provider or "").lower() == "minimax":
            raise RuntimeError(
                "该构建使用的 MiniMax 语音合成已弃用，请重新推荐音色后新建构建"
            )

        p = await session.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        # F-4：正文以 project_chapters 为主路径（表为空时自动回落 chapters_json 快照）
        chapters = await load_chapters(session, project_id)
        if not chapters:
            raise RuntimeError("项目尚未 prepare（没有章节数据），请先执行识别")

        narrator_voice_id = source_build.narrator_voice_id
        speed = source_build.speed
        try:
            voice_assignments = json.loads(source_build.voice_assignments_json or "{}")
        except Exception:
            voice_assignments = {}
        # 未归属对白兜底音色：新建 build 时已写进快照，但**本次改动之前**生成的
        # source_build 快照里没有 → 这里补一次，否则「重试失败章」出来的匿名对白
        # 仍会是旁白音色。
        voice_assignments = await _with_unknown_speaker_voices(
            session, project_id, voice_assignments, narrator_voice_id
        )

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
            # 重试必须继承源 Build 的合成配置：否则豆包 Build 重试会
            # 静默退化为默认厂商，违反"不降级"契约
            mode=(source_build.mode or "classic"),
            tts_provider=(source_build.tts_provider or "doubao"),
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
                # G-5：源 build 的本地副本可能已被 STORAGE_CLEANUP_LOCAL 清理 →
                # 先从对象存储回源，再走下面「另存为自身命名」的既有逻辑。
                # 回源失败（对象存储不可用/无此对象）则跳过该章，让它照常重新合成。
                if not expected_mp3.is_file():
                    await _restore_from_object_storage(
                        project_id, source_build_id, src_art.audio_filename, expected_mp3
                    )
                # B-5：不再直接复用 source build 的 audio_filename（两个 build 共享
                # 同一文件名 → delete_build 按 audio_filename 无条件 unlink，删任一方
                # 都会连带删掉另一方仍在引用的章节 MP3 / 时间轴 sidecar）。改为把源
                # 文件以硬链接"另存"为新 build 自己命名的文件（同盘零拷贝；异常时退化
                # 为复制），并同步另存时间轴 sidecar，使新 build 的下载/字幕自洽。
                new_fname = _audio_filename(new_build_id, ch.idx, failed=False)
                if _link_or_copy(expected_mp3, audio_dir / new_fname):
                    _link_or_copy(
                        audio_dir / _timings_filename_of_audio(src_art.audio_filename),
                        audio_dir / _timings_filename_of_audio(new_fname),
                    )
                    session.add(BuildArtifact(
                        build_id=new_build_id,
                        chapter_idx=ch.idx,
                        title=ch.title,
                        status="done",
                        audio_filename=new_fname,
                        audio_url=f"/media/{new_fname}",
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


def _write_chapter_timings(
    build_id: str,
    ch_idx: int,
    segs: list[_Segment],
    dur_list: list[int],
    *,
    seg_text_override: dict[int, str] | None = None,
) -> None:
    """写章节时间轴 sidecar JSON（SRT/LRC 生成数据源）。

    dur_list 为每段真实合成时长，start_ms 顺序累加。
    sidecar 与章节 MP3 同目录同名（_timings.json 后缀），写失败仅告警不影响合成。
    """
    override = seg_text_override or {}
    try:
        entries: list[dict] = []
        cursor_ms = 0
        for i, s in enumerate(segs):
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
            # estimated 恒为 False：classic 逐段合成，时长均为真实值（保留字段兼容读取方）
            json.dump(
                {"version": 1, "estimated": False, "segs": entries},
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
        chapters = await load_chapters(s, project_id)
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
        tts_provider_label = b.tts_provider or "doubao"
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

    logger.info(
        f"[build_worker] build_id={build_id[:8]}... total_chapters={total} "
        f"mode={build_mode} tts_provider={tts_provider_label} "
        f"styles={len(voice_styles)} narrator_emo={narrator_emotion!r}"
    )

    from ..ai.factory import get_tts_sem, get_tts_by_voice_id
    # [P-2.5] 不再预先拿一个全局 tts 实例。改为每段按 voice_id 路由：
    #   - doubao:/icl: 前缀 → 豆包
    #   - 无前缀 → 用 tts_provider_label 兜底（默认 settings.TTS_PROVIDER）
    # 这样旁白/角色可以混用任意音色，不再被「跨厂商音色」约束。
    fallback_provider = (tts_provider_label or "").lower() or None
    sem = get_tts_sem()
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)

    chapter_outputs: list[tuple[str | None, int | None]] = [(None, None)] * total
    # G-3/G-5：已成功归档到对象存储的产物名 → 打包完成后据此清理本地副本
    archived_files: set[str] = set()
    completed = 0
    failed_count = 0
    chapter_ok_flag: dict[int, bool] = {}
    # TTS 用量计数（真实供应商调用；缓存命中不计 calls）
    tts_calls_used = 0
    tts_chars_used = 0

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
                ch_ok_duration_ms = int(ch_ok_duration_ms or 0) or mp3_duration_ms(mp3_bytes)
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
                ch_ok_duration_ms = int(ch_ok_duration_ms or 0) or mp3_duration_ms(mp3_bytes)
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

            async def _synth_seg(s: _Segment) -> tuple[_Segment, bytes, int]:
                nonlocal tts_calls_used, tts_chars_used
                if s.kind == "silence":
                    # 静音延迟生成：先返回空字节标记，gather 后按本章真实音频的
                    # 采样率补齐（见下方 _matched_silence），消除拼接点采样率不一致
                    return s, b"", s.silence_ms
                vid = s.voice_id or narrator_voice_id
                # 应用发音规则
                if pronunciation_rules:
                    char_id = speaker_to_char_id.get(s.speaker or "")
                    s.text = apply_pronunciation_rules(s.text, pronunciation_rules, character_id=char_id)
                seg_emo = (s.emotion or "").strip()
                seg_ins = (s.instruction or "").strip()
                # P1-7：model + context_texts_hash 参与缓存键（切 model / 切 instruction 必须失效）
                # P1-4：sample_rate 参与缓存键（settings 改采样率后必须失效）
                seg_model = _voice_model_lookup(vid)
                seg_ctx_hash = _context_texts_hash(seg_ins)
                # req_params.model（standard / expressive）：决定指令是否真的生效，
                # 必须进缓存键，否则切完设置还会命中旧的「无情绪」音频
                seg_tts_model = _tts_model_param()
                seg_sample_rate = int(
                    getattr(settings, "DOUBAO_AUDIO_SAMPLE_RATE", 24000) or 24000
                )
                cached = await tts_segment_cache_get(
                    vid, speed, s.text, emotion=seg_emo, instruction=seg_ins,
                    model=seg_model, context_texts_hash=seg_ctx_hash,
                    sample_rate=seg_sample_rate, tts_model=seg_tts_model,
                )
                if cached is not None:
                    mp3_b, dur_ms = cached
                    return s, mp3_b, dur_ms
                # [P-2.5] 按 voice_id 前缀自动路由 TTS 厂商；无前缀走 fallback_provider
                seg_tts = get_tts_by_voice_id(vid) if ":" in (vid or "") else get_tts(fallback_provider)
                async with sem:
                    data, dur = await seg_tts.synthesize_to_bytes(
                        s.text, vid, speed=speed,
                        # 未配置时传 provider 默认（"calm"），与历史行为一致
                        emotion=seg_emo or "calm",
                        instruction_text=seg_ins or None,
                    )
                tts_calls_used += 1
                tts_chars_used += len(s.text)
                await tts_segment_cache_put(
                    vid, speed, s.text, data, dur,
                    emotion=seg_emo, instruction=seg_ins,
                    model=seg_model, context_texts_hash=seg_ctx_hash,
                    sample_rate=seg_sample_rate, tts_model=seg_tts_model,
                )
                return s, data, dur

            tasks = [_synth_seg(seg) for seg in segs]
            results = await asyncio.gather(*tasks)

            # 按本章第一个真实音频段的采样率补齐静音帧；无真实段时用豆包默认采样率
            silence_ms_rate: int = int(settings.DOUBAO_AUDIO_SAMPLE_RATE)
            for _s, _b, _d in results:
                if _b:
                    from ..core.mp3_util import mp3_sample_rate as _sr
                    silence_ms_rate = _sr(_b) or int(settings.DOUBAO_AUDIO_SAMPLE_RATE)
                    break
            from ..core.mp3_util import make_silent_mp3 as _mk_silent
            filled: list[tuple[_Segment, bytes, int]] = []
            for _s, _b, _d in results:
                if not _b and _s.kind == "silence":
                    _b = _mk_silent(_d, sample_rate=silence_ms_rate, kbps=128)
                filled.append((_s, _b, _d))

            ch_bytes = concat_mp3_files(*[r[1] for r in filled])
            ch_fname = _audio_filename(build_id, ch_idx, failed=False)
            ch_fpath = str(audio_dir / ch_fname)
            # 原子写：先写 .tmp 再 os.replace，避免崩溃留半成品
            tmp_fpath = ch_fpath + ".tmp"
            with open(tmp_fpath, "wb") as f:
                f.write(ch_bytes)
            os.replace(tmp_fpath, ch_fpath)
            ch_dur_ms = mp3_duration_ms(ch_bytes)
            chapter_outputs[ch_idx] = (ch_fpath, ch_dur_ms)

            # G-3：章节 MP3 归档到对象存储。
            # ⚠️ 此处**不能**删本地文件 —— 下面 _finalize 打包 ZIP 需要读本地 MP3，
            # 本地缺失会被当作失败章写入**静音占位**，等于静默损坏交付物。
            # 本地清理统一放在 _finalize 的「ZIP 也归档成功」之后。
            if await _archive_file(
                media_key(project_id, build_id, ch_fname), Path(ch_fpath), "audio/mpeg"
            ):
                archived_files.add(ch_fname)

            # 章内时间轴 sidecar（SRT/LRC 用）：gather 保序 → results[i] 对应 segs[i]
            _write_chapter_timings(
                build_id, ch_idx, segs,
                [r[2] for r in results],
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
                    # B-8：每章结束就把当前 TTS 用量落库，这样即便后续打包/DB
                    # 抛异常导致 worker 异常退出，Build 行也已保留到最近一章的
                    # 真实用量（不再只有正常走完全程才写一次）。
                    b.tts_calls = tts_calls_used
                    b.tts_chars = tts_chars_used
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

            # --- 降级逻辑（占位静音 MP3 + partial_success）---
            # 占位静音按最近一章成功音频的采样率生成，避免章界拼接点采样率跳变；
            # 找不到历史音频时回退到豆包默认采样率
            ph_sr = int(settings.DOUBAO_AUDIO_SAMPLE_RATE)
            from ..core.mp3_util import mp3_sample_rate as _msr
            for _prev_path, _ in reversed(chapter_outputs[:ch_idx]):
                if _prev_path:
                    try:
                        ph_sr = _msr(Path(_prev_path).read_bytes()) or int(settings.DOUBAO_AUDIO_SAMPLE_RATE)
                    except OSError:
                        pass
                    break
            from ..core.mp3_util import make_silent_mp3 as _mk_ph
            placeholder_bytes = _mk_ph(1000, sample_rate=ph_sr, kbps=128)
            ph_fname = _audio_filename(build_id, ch_idx, failed=True)
            ph_fpath = str(audio_dir / ph_fname)
            with open(ph_fpath, "wb") as f:
                f.write(placeholder_bytes)
            chapter_outputs[ch_idx] = (ph_fpath, 1000)
            # 占位音频也要归档：它会被打进 ZIP（避免缺文件），单章下载也应能拿到
            if await _archive_file(
                media_key(project_id, build_id, ph_fname), Path(ph_fpath), "audio/mpeg"
            ):
                archived_files.add(ph_fname)

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
        # B-8：取消路径不能丢已真实消耗的 TTS 用量（旧实现直接 return，
        # 跳过了写 tts_calls/tts_chars 与 UsageEvent）。
        await _record_build_usage(
            build_id, project_id, build_mode, tts_calls_used, tts_chars_used,
        )
        total_elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        logger.info(
            f"[build_worker] CANCELLED build_id={build_id[:8]}... total_ms={total_elapsed_ms} "
            f"completed={completed}/{total} failed={failed_count} "
            f"tts_calls={tts_calls_used} tts_chars={tts_chars_used}"
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

    # 打包前为每章生成 LRC 歌词（与 MP3 按位置对齐，供 ZIP 内同名 .lrc）。失败章 skip。
    # 注意 1：此刻 build 终态尚未写回（status 仍是 running），必须 require_final=False。
    # 注意 2：必须走批量入口 —— 逐章调用会退化成 O(N²)（每章都重新解析整本
    #         chapters_json + 全项目对白），大书（数千章）下打包无法完成。见 plan.md F-1。
    from ..services.subtitles import generate_chapters_lrc
    try:
        lrc_by_chapter = await generate_chapters_lrc(build_id, require_final=False)
    except Exception as e:
        logger.warning(
            f"[build_worker] build_id={build_id[:8]}... 生成章节 LRC 失败，"
            f"ZIP 将不含 .lrc: {type(e).__name__}: {e}"
        )
        lrc_by_chapter = {}
    chapter_lrcs: list[str] = [lrc_by_chapter.get(c.idx, "") for c in chapters]

    # G-3：每章 LRC 作为独立交付物归档（决策 4 明确 LRC 属于交付物）。
    # 没有本地文件，直接 put_bytes；失败只告警（ZIP 里仍内嵌同名 .lrc）。
    # 注：单章 LRC 按钮走的是 /chapters/{idx}/lrc 实时生成（依赖 sidecar，不依赖此处），
    # 归档的 .lrc 主要用于「直接给外部玩家/脚本一个稳定直链」。
    if get_storage().archives_remotely:
        for _c in chapters:
            _lrc_text = lrc_by_chapter.get(_c.idx, "")
            if not (_lrc_text or "").strip():
                continue
            await get_storage().put_bytes(
                key=media_key(project_id, build_id, f"ch{_c.idx:04d}.lrc"),
                data=_lrc_text.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )

    # F-7：按 ZIP_SHARD_CHAPTERS 分片打包。每片自包含（内部章节序号保持全书统一编号），
    # 避免数千章时产出数十 GB 单包（本地峰值磁盘翻倍、浏览器也无法可靠下载）。
    try:
        shard_size = _zip_shard_size()
    except Exception:
        shard_size = 0
    shard_ranges = _zip_shard_ranges(total, shard_size)
    zip_shards: list[dict] = []
    chapter_titles = [c.title for c in chapters]
    for _s, _e in shard_ranges:
        shard_name = _zip_shard_filename(build_id, _s, _e, total)
        shard_path = str(audio_dir / shard_name)
        try:
            _build_book_zip(
                shard_path,
                job_id=build_id,
                job_title=job_title,
                chapter_outputs=chapter_outputs,
                chapter_titles=chapter_titles,
                chapter_lrcs=chapter_lrcs,
                start=_s,
                end=_e,
            )
        except Exception as e:
            logger.error(
                f"[build_worker] build_id={build_id[:8]}... 打包分片失败 "
                f"ch{_s + 1}-{_e + 1}: {type(e).__name__}: {e}",
                exc_info=True,
            )
            raise
        try:
            _size = os.path.getsize(shard_path)
        except OSError:
            _size = None
        zip_shards.append({
            "filename": shard_name,
            "start": _s,
            "end": _e,
            "size_bytes": _size,
        })
        # G-3：分片 ZIP 归档（在本地 MP3 清理之前 —— 打包必须读本地文件）
        if await _archive_file(
            media_key(project_id, build_id, shard_name),
            Path(shard_path),
            "application/zip",
        ):
            archived_files.add(shard_name)
    # zip_filename 保留为首个分片（旧代码路径 / 媒体签名兜底都依赖它）
    zip_fname = zip_shards[0]["filename"] if zip_shards else _zip_filename(build_id)
    if len(zip_shards) > 1:
        logger.info(
            f"[build_worker] build_id={build_id[:8]}... 已生成 {len(zip_shards)} 个 ZIP 分片"
            f"（每片 {shard_size} 章）"
        )

    # G-5：所有产物都已归档 → 按 STORAGE_CLEANUP_LOCAL 清理本地副本（决策 6）。
    # 只删「确认归档成功」的文件；**保留 timings sidecar**（KB 级，是 LRC 生成与
    # retry 复用回源后的时间轴来源，删了会让重试的歌词退化成估算）。
    if get_storage().archives_remotely and cleanup_local_enabled():
        freed = 0
        for _fname in sorted(archived_files):
            _p = audio_dir / _fname
            try:
                if _p.is_file():
                    freed += _p.stat().st_size
                    _p.unlink()
            except OSError as e:
                logger.warning(f"[build_worker] 清理本地副本失败 {_fname}: {e}")
        logger.info(
            f"[build_worker] build_id={build_id[:8]}... 本地副本已清理 "
            f"files={len(archived_files)} freed_mb={freed // (1024 * 1024)}"
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

    # B-3：终态写入加取消保护（条件更新，仅当仍为 running 才写回）。
    _progress_msg = (
        f"全部完成 {completed}/{total} 章"
        + (f"（{failed_count} 章失败已用静音占位）" if failed_count else "")
        + (f"，共 {len(zip_shards)} 个 ZIP 分片" if len(zip_shards) > 1 else "")
    )
    terminal_applied = await _apply_terminal_status(
        build_id,
        final_status=final_status,
        progress_msg=_progress_msg,
        zip_filename=zip_fname,
        total_size_bytes=total_size_bytes,
        total_duration_ms=total_ms,
        failed_chapters=this_retry_failed,
        # TTS 用量：真实供应商调用（缓存命中不计次）
        tts_calls=tts_calls_used,
        tts_chars=tts_chars_used,
        zip_shards=zip_shards,
    )

    # B-8：无论终态是否写回，已真实消耗的 TTS 用量都要入账（打包期间被取消同理）。
    await _record_build_usage(
        build_id, project_id, build_mode, tts_calls_used, tts_chars_used,
    )

    # 构建结束同步项目状态：全量成功 → done（前端"已完成"）；
    # 部分成功 → partial_success；失败/取消保持 ready（用户可重新构建）。
    # 仅当这是该项目最近一次构建、且终态确已写回时才回写，避免旧 build 完成
    # 覆盖新状态，也避免「已取消」的 build 把项目置为 done。
    if terminal_applied and final_status in ("success", "partial_success"):
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

        stmt_art = select(BuildArtifact.chapter_idx, BuildArtifact.audio_filename).where(
            BuildArtifact.build_id == build_id
        )
        _art_rows = (await session.execute(stmt_art)).all()
        art_filenames = [af for _ci, af in _art_rows if af]
        # G-5：删对象时要连每章 LRC 一起删，故保留章节号
        art_idxs = [ci for ci, _af in _art_rows]
        # F-7：删除该 build 的**全部分片**（含旧的单包场景 —— _parse_zip_shards 会兜底）
        zip_fnames = [s["filename"] for s in _parse_zip_shards(b)]

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
    for _zf in zip_fnames:
        try:
            fpath = audio_dir / _zf
            if fpath.is_file():
                fpath.unlink()
        except OSError as e:
            logger.warning(f"[build_delete] 删 ZIP 失败: {_zf} -> {e}")

    # G-5：对象存储里的对象也要删（公有读桶不会有「本地已清理」的兜底）
    st = get_storage()
    if st.archives_remotely:
        for fname in [*art_filenames, *zip_fnames]:
            await st.delete_key(media_key(project_id, build_id, fname))
        for _idx in art_idxs:
            await st.delete_key(media_key(project_id, build_id, f"ch{_idx:04d}.lrc"))

    logger.info(
        f"[build_delete] build_id={build_id[:8]}... "
        f"deleted audio_files={len(art_filenames)} zips={len(zip_fnames)}"
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
    tts_chars: int                 # 计费字符数：实际下发的 TTS 文本字数（含标点、含标题）
    price_per_char_cny: float      # 单价（元/字），由后端下发，避免前端写死
    est_tts_cost_cny: float        # 预估合成费用（元）= tts_chars × 单价


async def estimate_project_build(project_id: str, speed: float = 1.0) -> BuildEstimateResp:
    """按当前章节/对白数据估算一次 build 的规模。

    中文 TTS 语速经验值：约 4.2 字/秒（1.0x），MP3 128kbps ≈ 16KB/s。
    缓存命中会显著减少真实调用量，这里给的是"冷缓存上限"。

    费用预估：豆包字符版按「文本字符数（含标点）」计费，单价见
    `settings.DOUBAO_TTS_PRICE_PER_CHAR`（默认 0.0003 元/字）。计费基数取
    **实际下发给 TTS 的段文本**之和（正文旁白 + 对白 + 标题），与逐段请求一一对应；
    语音指令（context_texts）官方明确不计费，故不计入。
    """
    from ..core.config import settings as _settings

    factory = get_session_factory()
    async with factory() as s:
        p = await s.get(Project, project_id)
        if not p:
            raise ValueError(f"项目不存在: {project_id}")
        chapters = await load_chapters(s, project_id)
        prepared = bool(chapters)
        stmt_d = select(ProjectDialogue).where(ProjectDialogue.project_id == project_id)
        all_dialogues = list((await s.execute(stmt_d)).scalars().all())

    dialogues_by_chapter: dict[int, list] = {}
    for d in all_dialogues:
        dialogues_by_chapter.setdefault(d.chapter_idx, []).append(d)
    for lst in dialogues_by_chapter.values():
        lst.sort(key=lambda x: x.anchor_start)

    total_chars = sum(len(ch.text) for ch in chapters)
    seg_count = 0
    # 计费字符数 = 非静音段文本之和（标题段也算，它同样要发给 TTS）
    tts_chars = 0
    for ch in chapters:
        segs, _ = _build_segments_for_chapter(
            ch, dialogues_by_chapter.get(ch.idx, []),
            narrator_voice_id="est", voice_assignments={},
            segment_overrides=None, start_idx=0,
        )
        for sg in segs:
            if sg.kind == "silence":
                continue
            seg_count += 1
            tts_chars += len(sg.text or "")

    price_per_char = float(
        getattr(_settings, "DOUBAO_TTS_PRICE_PER_CHAR", 0.0003) or 0.0003
    )

    sp = max(0.5, min(2.0, float(speed or 1.0)))
    chars_per_sec = 4.2 * sp
    est_audio_sec = total_chars / chars_per_sec if total_chars else 0
    # MP3 128kbps ≈ 16KB/s
    est_zip_mb = est_audio_sec * 16.0 / (1024.0 * 1024.0)

    # LLM prepare 估算（角色切片 + 消歧批次 + 对白批次 + 逐段语音指令批次 + 音色推荐）
    slice_size = max(1, int(_settings.LLM_CHAR_EXTRACT_SLICE_SIZE))
    dlg_batch = max(1, int(_settings.DIALOGUE_BATCH_CHAPTERS))
    # 逐段语音指令：按章切批，一次 LLM 调用处理 VOICE_INSTRUCTION_BATCH_CHAPTERS 章
    vi_enabled = bool(getattr(_settings, "VOICE_INSTRUCTION_ENABLED", True))
    vi_batch = max(1, int(getattr(_settings, "VOICE_INSTRUCTION_BATCH_CHAPTERS", 6) or 6))
    est_llm_calls = 0
    if not prepared:
        import math
        est_llm_calls = (
            math.ceil(total_chars / slice_size)      # 角色提取切片
            + 2                                       # 消歧（通常 1-2 批）
            + math.ceil(max(len(chapters), 1) / dlg_batch)  # 对白归属批次
            + (math.ceil(max(len(chapters), 1) / vi_batch) if vi_enabled else 0)  # 逐段语音指令批次
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
        tts_chars=tts_chars,
        price_per_char_cny=price_per_char,
        est_tts_cost_cny=round(tts_chars * price_per_char, 2),
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
