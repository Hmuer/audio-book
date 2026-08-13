"""
项目制 Build 服务：基于 Project 表的整本合成（Project → Build → BuildArtifact）。

设计要点：
- start_build: 创建 Build 记录 + 每章一条 pending BuildArtifact，启动后台任务立即返回
- _run_build_inner: 后台 worker（独立 session），逐章合成→更新 BuildArtifact；失败章写占位静音 MP3
- 双重去重：内存锁 _RUNNING_BUILDS（按 project_id） + DB Build.status 检查
- 音频文件命名：build_{build_id}_ch{idx:04d}.mp3，ZIP：build_{build_id}_all.zip
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
import uuid
from datetime import datetime
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
from .book import _sanitize_zip_entry, _build_book_zip  # 复用旧 book.py 的打包工具

logger = logging.getLogger(__name__)


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


class BuildArtifactResp(BaseModel):
    """单章 BuildArtifact 详情。"""
    chapter_idx: int
    title: str
    status: str  # pending / synthesizing / done / failed
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


class BuildListItem(BaseModel):
    """Build 列表项（精简）。"""
    build_id: str
    status: str
    total_chapters: int
    completed_chapters: int
    started_at: str | None
    completed_at: str | None
    created_at: str | None


class BuildStatusResp(BaseModel):
    """轮询用：progress + artifacts。"""
    build_id: str
    status: str
    progress_msg: str | None
    completed_chapters: int
    total_chapters: int
    artifacts: list[BuildArtifactResp]


# =====================================================================
# 内部工具
# =====================================================================

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


# 防止同一项目同时多个 build 的内存锁（单 worker 内绝对去重；
# 多 worker 下由 DB Build.status=running 兜底）
_RUNNING_BUILDS: set[str] = set()
_RUNNING_LOCK = asyncio.Lock()


def _audio_filename(build_id: str, ch_idx: int, failed: bool = False) -> str:
    """每章 MP3 文件名；failed 章单独命名以便排查。"""
    suffix = "_failed" if failed else ""
    return f"build_{build_id}_ch{ch_idx:04d}{suffix}.mp3"


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

    双重去重：
    1. 内存锁 _RUNNING_BUILDS：单 worker 内同一 project 不重复
    2. DB Build.status：若已有 running/queued 的 build，拒绝新建
    """
    narrator_voice_id = await _ensure_default_narrator(narrator_voice_id)

    # 内存锁检查（不持锁长时间占用，只快速判断）
    async with _RUNNING_LOCK:
        if project_id in _RUNNING_BUILDS:
            logger.info(f"[build_start] project_id={project_id[:8]}... already running")
            # 返回最近一个 build 状态
            factory = get_session_factory()
            async with factory() as s:
                stmt = select(Build).where(
                    Build.project_id == project_id
                ).order_by(Build.created_at.desc()).limit(1)
                b = (await s.execute(stmt)).scalar_one_or_none()
                if b:
                    return _build_to_resp(b)
                # 极端情况：锁里有但 DB 没有，往下走创建逻辑

    factory = get_session_factory()
    async with factory() as session:
        # 1. 加载 project + chapters_json
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

        # 2. DB 去重：检查是否有 running/queued 的 build
        stmt_active = select(Build).where(
            Build.project_id == project_id,
            Build.status.in_(("queued", "running")),
        )
        active = (await session.execute(stmt_active)).scalar_one_or_none()
        if active:
            logger.warning(
                f"[build_start] project_id={project_id[:8]}... "
                f"already has active build {active.build_id[:8]}... status={active.status}"
            )
            return _build_to_resp(active)

        # 3. 创建 Build + BuildArtifact
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

    # 4. 写内存锁 + 后台启动
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
            )
        except Exception as e:
            logger.error(
                f"[build_worker] FAIL build_id={build_id[:8]}... "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            # 兜底把 build 标记为 failed
            try:
                f2 = get_session_factory()
                async with f2() as s:
                    b2 = await s.get(Build, build_id)
                    if b2:
                        b2.status = "failed"
                        b2.progress_msg = f"合成失败: {type(e).__name__}: {e}"[:200]
                        b2.completed_at = datetime.utcnow()
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


async def _run_build_inner(
    build_id: str,
    project_id: str,
    voice_assignments: dict[str, str],
    narrator_voice_id: str,
    speed: float = 1.0,
) -> None:
    """
    后台合成 worker（独立 session）。
    - 每章独立合成 → BuildArtifact 更新
    - 失败章写 1s 占位静音 MP3
    - 最后生成 ZIP，Build status → success/failed
    """
    t0 = _time.perf_counter()
    logger.info(
        f"[build_worker] START build_id={build_id[:8]}... "
        f"project_id={project_id[:8]}... narrator={narrator_voice_id} "
        f"voices={len(voice_assignments)} speed={speed}"
    )

    factory = get_session_factory()

    # 1. 加载 project + chapters + 对白（一次性读，避免循环里反复查 DB）
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
        # 加载所有对白
        stmt_d = select(ProjectDialogue).where(ProjectDialogue.project_id == project_id)
        all_dialogues_rows = list((await s.execute(stmt_d)).scalars().all())

    # 对白按 chapter_idx 分桶
    dialogues_by_chapter: dict[int, list[ProjectDialogue]] = {}
    for d in all_dialogues_rows:
        dialogues_by_chapter.setdefault(d.chapter_idx, []).append(d)
    for lst in dialogues_by_chapter.values():
        lst.sort(key=lambda x: x.anchor_start)

    # 2. Build.status → running，写 started_at
    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b:
            raise RuntimeError(f"Build 不存在: {build_id}")
        b.status = "running"
        b.started_at = datetime.utcnow()
        b.progress_msg = f"开始合成 1/{total} 章…"
        await s.commit()

    logger.info(f"[build_worker] build_id={build_id[:8]}... total_chapters={total}")

    from ..ai.factory import get_tts_sem
    tts = get_tts()
    audio_dir = Path(settings.AUDIO_DIR)
    audio_dir.mkdir(parents=True, exist_ok=True)
    sem = get_tts_sem()  # 全局共享 semaphore，多 worker 不叠加并发

    # chapter_outputs[i] = (audio_path, duration_ms)
    chapter_outputs: list[tuple[str | None, int | None]] = [(None, None)] * total
    completed = 0
    failed_count = 0

    for ch_idx, ch in enumerate(chapters):
        ch_t0 = _time.perf_counter()
        # BuildArtifact.status → synthesizing + progress_msg
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

            # _build_segments_for_chapter 接受任意带 anchor_start/anchor_end/anchor_text/
            # speaker/text/confidence 属性的对象，ProjectDialogue 完全兼容
            segs, _ = _build_segments_for_chapter(
                ch, ch_dialogues,
                narrator_voice_id=narrator_voice_id,
                voice_assignments=voice_assignments,
                segment_overrides=None,
                start_idx=0,
            )

            # 章内并发合成
            async def _synth_seg(s: _Segment) -> tuple[_Segment, bytes, int]:
                if s.kind == "silence":
                    return s, make_silent_mp3(max(s.silence_ms, 1)), s.silence_ms
                async with sem:
                    vid = s.voice_id or narrator_voice_id
                    data = await tts.synthesize_to_bytes(s.text, vid, speed=speed)
                    dur = _estimate_mp3_duration_ms(data)
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

            # 更新 BuildArtifact + Build.completed_chapters
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
                if b:
                    b.completed_chapters = completed
                await s.commit()

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
            # 失败章写 1s 占位静音 MP3，保证 ZIP 不缺章
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
                    b.completed_chapters = completed  # 失败章不递增
                await s.commit()
            continue

    # 3. 生成 ZIP
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
    # 复用 book.py 的 _build_book_zip：以 build_id 当作 job_id 入参
    _build_book_zip(
        zip_path,
        job_id=build_id,
        job_title=job_title,
        chapter_outputs=chapter_outputs,
        chapter_titles=[c.title for c in chapters],
    )

    # 4. 写 Build 最终状态
    final_status = "success" if failed_count == 0 else "success"  # 仍标记 success（部分章失败但已用静音占位）
    # 注：即使有失败章，整包 ZIP 已生成，前端可以正常下载；progress_msg 中说明失败数
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
            b.completed_at = datetime.utcnow()
            await s.commit()

    total_elapsed_ms = int((_time.perf_counter() - t0) * 1000)
    logger.info(
        f"[build_worker] DONE build_id={build_id[:8]}... total_ms={total_elapsed_ms} "
        f"completed={completed}/{total} failed={failed_count} "
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
        return [
            BuildListItem(
                build_id=b.build_id,
                status=b.status,
                total_chapters=b.total_chapters,
                completed_chapters=b.completed_chapters,
                started_at=b.started_at.isoformat() if b.started_at else None,
                completed_at=b.completed_at.isoformat() if b.completed_at else None,
                created_at=b.created_at.isoformat() if b.created_at else None,
            )
            for b in rows
        ]


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
        )


async def delete_build(project_id: str, build_id: str) -> None:
    """删除 build + 磁盘 MP3 / ZIP 文件。"""
    factory = get_session_factory()
    async with factory() as session:
        b = await session.get(Build, build_id)
        if not b:
            return  # 幂等
        if b.project_id != project_id:
            raise ValueError(f"Build 不属于项目 {project_id}")

        # 收集所有要删的磁盘文件名
        stmt_art = select(BuildArtifact.audio_filename).where(
            BuildArtifact.build_id == build_id
        )
        art_filenames = [r for r in (await session.execute(stmt_art)).scalars().all() if r]
        zip_fname = b.zip_filename

        # 删 DB（cascade 会连带 BuildArtifact）
        await session.delete(b)
        await session.commit()

    # 删磁盘文件
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
