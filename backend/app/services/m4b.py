"""
M4B 有声书打包：把一次 Build 的全部章节 MP3 合成一个带章节元数据的 .m4b。

- 依赖系统 ffmpeg（运行时检测；缺失时报可读错误，不影响 MP3/ZIP 主流程）
- 章节时间轴来自 BuildArtifact.duration_ms 累加（与 SRT 使用同一数据口径）
- 生成是后台任务（JobTask kind='m4b'），服务重启后孤儿任务兜底标记 failed，
  用户重新点击即可（转码可安全重跑）
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime, UTC
from pathlib import Path

from sqlalchemy import select

from ..core.config import settings
from ..db.models import Build, BuildArtifact, Project
from ..db.session import get_session_factory

logger = logging.getLogger(__name__)


def m4b_filename(build_id: str) -> str:
    return f"build_{build_id}_book.m4b"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _fmt_ffmeta_ms(ms: int) -> str:
    """FFMETADATA1 章节时间戳：单位毫秒的整数。"""
    return str(max(int(ms), 0))


def _safe_meta_text(s: str) -> str:
    # FFMETADATA1 特殊字符：= ; # \ 换行
    for ch in "=;#\\\r\n":
        s = s.replace(ch, " ")
    return s.strip()


async def _collect_build_info(build_id: str) -> dict:
    """读取 Build + Artifacts + Project 标题，校验可打包。"""
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, build_id)
        if not b:
            raise ValueError(f"Build 不存在: {build_id}")
        if b.status not in ("success", "partial_success", "failed"):
            raise ValueError(f"Build 尚未完成（status={b.status}），无法打包 M4B")
        stmt = (
            select(BuildArtifact)
            .where(BuildArtifact.build_id == build_id)
            .order_by(BuildArtifact.chapter_idx)
        )
        arts = list((await s.execute(stmt)).scalars().all())
        if not arts:
            raise ValueError("Build 没有章节产物")
        proj = await s.get(Project, b.project_id)
        book_title = (proj.book_title if proj else None) or ""
    return {
        "build_id": build_id,
        "project_id": b.project_id,
        "book_title": book_title,
        "artifacts": arts,
    }


def _run_ffmpeg(concat_list_path: str, meta_path: str, out_path: str) -> None:
    """重编码为 AAC 并写入章节元数据（-f ipod = m4b 容器）。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_list_path,
        "-i", meta_path,
        "-map_metadata", "1",
        "-c:a", "aac", "-b:a", "64k", "-vn",
        "-f", "ipod", out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-800:]
        raise RuntimeError(f"ffmpeg 转码失败 (exit={proc.returncode}): {tail}")


def _generate_m4b_sync(build_id: str) -> str:
    """同步执行打包，返回 m4b 文件名。由后台任务线程调用。"""
    import asyncio

    info = asyncio.run(_collect_build_info(build_id))
    audio_dir = Path(settings.AUDIO_DIR)
    out_fname = m4b_filename(build_id)
    out_path = audio_dir / out_fname

    # 1) 逐章校验音频文件存在 + 累加章节时间
    chapters_meta: list[tuple[str, Path, int]] = []  # (title, path, dur_ms)
    total_ms = 0
    for art in info["artifacts"]:
        if not art.audio_filename:
            continue
        fpath = audio_dir / art.audio_filename
        if not fpath.is_file():
            continue
        dur_ms = int(art.duration_ms or 0)
        if dur_ms <= 0:
            # 兜底估算：MP3 128kbps ≈ 16KB/s
            dur_ms = int(fpath.stat().st_size / 16.0)
        title = art.title or f"第{art.chapter_idx + 1}章"
        chapters_meta.append((title, fpath, dur_ms))
        total_ms += dur_ms
    if not chapters_meta:
        raise ValueError("没有可用的章节音频文件，无法打包 M4B")

    # 2) concat 列表 + FFMETADATA1 章节元数据
    work_dir = audio_dir
    concat_list = work_dir / f"_m4b_{build_id[:12]}_list.txt"
    meta_file = work_dir / f"_m4b_{build_id[:12]}_meta.txt"
    try:
        lines = ["ffconcat version 1.0"]
        for _, fpath, _ in chapters_meta:
            # concat demuxer 需要转义单引号
            escaped = str(fpath.name).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

        meta_lines = [
            ";FFMETADATA1",
            f"title={_safe_meta_text(info['book_title'] or 'Audiobook')}",
            "artist=AI 有声小说生成器",
        ]
        cursor = 0
        for title, _, dur_ms in chapters_meta:
            start = cursor
            cursor += dur_ms
            meta_lines.append("[CHAPTER]")
            meta_lines.append("TIMEBASE=1/1000")
            meta_lines.append(f"START={_fmt_ffmeta_ms(start)}")
            meta_lines.append(f"END={_fmt_ffmeta_ms(cursor)}")
            meta_lines.append(f"title={_safe_meta_text(title)}")
        meta_file.write_text("\n".join(meta_lines) + "\n", encoding="utf-8")

        # 3) 转码（原子写）
        tmp_out = str(out_path) + ".tmp"
        _run_ffmpeg(str(concat_list), str(meta_file), tmp_out)
        os.replace(tmp_out, out_path)
    finally:
        for p in (concat_list, meta_file):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass

    logger.info(
        f"[m4b] 打包完成 build_id={build_id[:8]}... 文件={out_fname} "
        f"章节数={len(chapters_meta)} 总时长≈{total_ms / 60000:.1f}min "
        f"大小={out_path.stat().st_size // 1024 // 1024}MB"
    )
    return out_fname


async def start_m4b_task(build_id: str) -> dict:
    """启动 M4B 打包后台任务（幂等：文件已存在直接返回 ready）。"""
    if not ffmpeg_available():
        raise RuntimeError(
            "未检测到 ffmpeg，无法打包 M4B。请先安装 ffmpeg（https://ffmpeg.org）并加入 PATH。"
        )

    out_fname = m4b_filename(build_id)
    out_path = Path(settings.AUDIO_DIR) / out_fname
    state = await get_m4b_status(build_id)
    if state.get("state") == "running":
        return state
    if out_path.is_file():
        return {"state": "ready", "filename": out_fname, "url": f"/media/{out_fname}"}

    # 预校验（Build 终态 + 有产物）
    await _collect_build_info(build_id)

    from .job_tasks import register_task, finish_task

    async def _runner() -> None:
        job = None
        job_task_id = None
        final_status, final_error = "success", None
        try:
            job = await register_task("m4b", build_id, trigger="api")
            job_task_id = job.task_id
        except Exception as e:
            logger.warning(f"[m4b] 注册 JobTask 失败: {type(e).__name__}: {e}")
        try:
            fname = await asyncio.get_running_loop().run_in_executor(
                None, _generate_m4b_sync, build_id,
            )
            logger.info(f"[m4b] 后台打包完成: {fname}")
        except Exception as e:
            final_status = "failed"
            final_error = f"{type(e).__name__}: {e}"
            logger.error(f"[m4b] 打包失败 build_id={build_id[:8]}...: {e}", exc_info=True)
        finally:
            if job_task_id:
                try:
                    await finish_task(job_task_id, status=final_status, error_msg=final_error)
                except Exception as e:
                    logger.warning(f"[m4b] 写 JobTask 终态失败: {e}")

    import asyncio
    asyncio.create_task(_runner(), name=f"m4b_{build_id[:8]}")
    return {"state": "running", "filename": out_fname}


async def get_m4b_status(build_id: str) -> dict:
    """查询 M4B 状态：ready / running / failed / none。"""
    from ..db.models import JobTask

    out_fname = m4b_filename(build_id)
    out_path = Path(settings.AUDIO_DIR) / out_fname

    factory = get_session_factory()
    async with factory() as s:
        stmt = (
            select(JobTask)
            .where(JobTask.kind == "m4b", JobTask.target_id == build_id)
            .order_by(JobTask.created_at.desc())
            .limit(1)
        )
        task = (await s.execute(stmt)).scalar_one_or_none()

    if out_path.is_file():
        return {"state": "ready", "filename": out_fname, "url": f"/media/{out_fname}"}
    if task and task.status == "running":
        return {"state": "running"}
    if task and task.status == "pending":
        return {"state": "running"}
    if task and task.status == "failed":
        return {"state": "failed", "error": task.error_msg}
    return {"state": "none"}
