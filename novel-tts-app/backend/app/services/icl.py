"""ICL 声音复刻服务：训练任务生命周期管理 + 用户音色查询。

流程（T-IC3/T-IC4）：
  start_icl_training → 落库（status=0）→ 参考音频存盘 DATA_DIR/icl/ →
  后台 worker：DoubaoICLClient.create_training → 轮询 query_training →
  status ∈ {2,4} → 任务 status=4 + cloned_voice_id；status=3 → status=3 + error_msg。

`_icl_client` 模块级单例可被测试注入（与 factory._tts_instance 同一模式）。
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db.models import IclTrainingTask
from backend.app.db.session import get_session_factory

logger = logging.getLogger(__name__)

# 可用状态：2 成功 / 4 可用（豆包 ICL 文档语义）
_USABLE_STATUSES = (2, 4)

_icl_client: Any | None = None

# 后台 worker 任务强引用（asyncio 只持弱引用，不持会被 GC 中断训练轮询）
_icl_bg_tasks: set[asyncio.Task] = set()

# 每任务最近一次出站（豆包侧）状态刷新时间：前端 5s 轮询 + worker 轮询双路触发，
# 节流避免同一任务在 RPM 桶上堆积排队
_icl_last_refresh: dict[str, float] = {}
_ICL_REFRESH_MIN_INTERVAL_SECS = 4.0

# 允许的参考音频扩展名（豆包 ICL 2.0 支持 wav/mp3/m4a 等）
_ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".aac", ".flac"}


def get_icl_client() -> Any:
    global _icl_client
    if _icl_client is None:
        from ..ai.providers.doubao.icl import DoubaoICLClient
        _icl_client = DoubaoICLClient()
    return _icl_client


def _ext_from_filename(filename: str | None) -> str:
    ext = (Path(filename or "ref.mp3").suffix or ".mp3").lower()
    return ext if ext in _ALLOWED_EXTS else ".mp3"


def _task_to_dict(t: IclTrainingTask) -> dict[str, Any]:
    return {
        "task_id": t.task_id,
        "user_id": t.user_id,
        "voice_name": t.voice_name,
        "status": t.status,
        "status_label": _status_label(t.status),
        "progress": t.progress,
        "cloned_voice_id": t.cloned_voice_id,
        "voice_id": f"icl:{t.cloned_voice_id}" if t.cloned_voice_id else None,
        "usable": t.status in _USABLE_STATUSES,
        "doubao_task_id": t.doubao_task_id,
        "error_msg": t.error_msg,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _status_label(status: int) -> str:
    return {
        0: "排队中",
        1: "训练中",
        2: "训练成功",
        3: "训练失败",
        4: "可用",
    }.get(int(status), f"未知({status})")


# =====================================================================
# 训练任务启动 + worker
# =====================================================================

async def start_icl_training(
    user_id: int,
    voice_name: str,
    audio_bytes: bytes,
    filename: str | None = None,
) -> dict[str, Any]:
    """创建训练任务并启动后台 worker。返回任务 dict（status=0）。"""
    name = (voice_name or "").strip()[:64] or "我的声线"
    if not audio_bytes or len(audio_bytes) < 512:
        raise ValueError("参考音频过小：请上传 3 秒以上（建议 6~10 秒）的清晰人声录音")
    max_bytes = getattr(settings, "ICL_MAX_AUDIO_BYTES", 10 * 1024 * 1024)
    if len(audio_bytes) > max_bytes:
        raise ValueError(f"参考音频过大：{len(audio_bytes)} > {max_bytes} 字节")

    task_id = f"icl_{uuid.uuid4().hex}"
    ext = _ext_from_filename(filename)
    ref_dir = Path(settings.DATA_DIR) / "icl"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_path = ref_dir / f"{task_id}{ext}"
    ref_path.write_bytes(audio_bytes)

    factory = get_session_factory()
    async with factory() as s:
        t = IclTrainingTask(
            task_id=task_id,
            user_id=user_id,
            voice_name=name,
            reference_audio_path=str(ref_path),
            reference_audio_size_bytes=len(audio_bytes),
            status=0,
            progress=0,
        )
        s.add(t)
        await s.commit()
        row = await s.get(IclTrainingTask, task_id)
        resp = _task_to_dict(row)

    task = asyncio.create_task(_icl_training_worker(task_id))
    _icl_bg_tasks.add(task)
    task.add_done_callback(_icl_bg_tasks.discard)
    return resp


async def _icl_training_worker(task_id: str) -> None:
    """后台训练 worker：create → 轮询 → 终态写库。异常不抛出（写 error_msg）。"""
    factory = get_session_factory()
    client = get_icl_client()
    try:
        # 1) 读取参考音频 → 创建豆包训练任务
        async with factory() as s:
            t = await s.get(IclTrainingTask, task_id)
            if not t:
                return
            audio_bytes = Path(t.reference_audio_path).read_bytes()
            voice_name = t.voice_name
            audio_format = Path(t.reference_audio_path).suffix.lstrip(".").lower() or "mp3"

        doubao_task_id = await client.create_training(
            voice_name, audio_bytes, audio_format=audio_format,
        )

        async with factory() as s:
            t = await s.get(IclTrainingTask, task_id)
            if not t:
                return
            t.doubao_task_id = doubao_task_id
            t.status = 1
            t.progress = 10
            await s.commit()

        # 2) 轮询直到终态
        poll_interval = float(getattr(settings, "DOUBAO_ICL_POLL_INTERVAL_SECS", 5.0) or 5.0)
        timeout_secs = int(getattr(settings, "DOUBAO_ICL_TIMEOUT_SECS", 1800) or 1800)
        waited = 0.0
        while True:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            r = await client.query_training(doubao_task_id)
            status = int(r.get("status", 0))
            progress = int(r.get("progress", 0))
            cloned = r.get("cloned_voice_id")
            err = r.get("error")

            async with factory() as s:
                t = await s.get(IclTrainingTask, task_id)
                if not t:
                    return
                t.status = status
                t.progress = max(t.progress, progress)
                if status in _USABLE_STATUSES and cloned:
                    t.cloned_voice_id = str(cloned)
                    t.progress = 100
                if status == 3:
                    t.error_msg = (err or "豆包 ICL 训练失败")[:500]
                await s.commit()

            if status in (2, 3, 4):
                logger.info(
                    f"[icl_worker] task={task_id[:12]}... 终态 status={status} "
                    f"cloned={cloned} waited={waited:.0f}s"
                )
                return
            if waited >= timeout_secs:
                async with factory() as s:
                    t = await s.get(IclTrainingTask, task_id)
                    if t:
                        t.status = 3
                        t.error_msg = f"训练超时（>{timeout_secs}s）"
                        await s.commit()
                logger.warning(f"[icl_worker] task={task_id[:12]}... 轮询超时")
                return
    except Exception as e:
        logger.error(f"[icl_worker] task={task_id[:12]}... FAIL {type(e).__name__}: {e}", exc_info=True)
        try:
            async with factory() as s:
                t = await s.get(IclTrainingTask, task_id)
                if t:
                    t.status = 3
                    t.error_msg = f"{type(e).__name__}: {e}"[:500]
                    await s.commit()
        except Exception:
            logger.exception(f"[icl_worker] task={task_id[:12]}... 终态写库失败")


# =====================================================================
# 查询 / 删除
# =====================================================================

async def list_icl_tasks(user_id: int) -> list[dict[str, Any]]:
    factory = get_session_factory()
    async with factory() as s:
        stmt = select(IclTrainingTask).where(
            IclTrainingTask.user_id == user_id
        ).order_by(IclTrainingTask.created_at.desc())
        rows = list((await s.execute(stmt)).scalars().all())
        return [_task_to_dict(r) for r in rows]


async def get_icl_task(
    task_id: str, user_id: int | None = None,
) -> dict[str, Any] | None:
    """详情；若任务仍进行中（0/1），顺带触发一次状态刷新。

    user_id 传入时先做归属校验再触发出站刷新（避免替他人任务消耗 ICL RPM）。
    """
    factory = get_session_factory()
    async with factory() as s:
        t = await s.get(IclTrainingTask, task_id)
        if not t:
            return None
        if user_id is not None and t.user_id != user_id:
            return None
        d = _task_to_dict(t)
        in_progress = t.status in (0, 1)
    if in_progress and d.get("doubao_task_id"):
        await refresh_icl_task(task_id)
        async with factory() as s:
            t = await s.get(IclTrainingTask, task_id)
            return _task_to_dict(t) if t else None
    return d


async def refresh_icl_task(task_id: str) -> dict[str, Any] | None:
    """主动向豆包查询一次状态并回写（供前端轮询接口使用）。

    出站查询按任务节流（_ICL_REFRESH_MIN_INTERVAL_SECS）：worker 轮询与前端
    详情轮询双路触发时，间隔内的重复刷新直接返回库内状态，避免 RPM 桶堆积。
    """
    factory = get_session_factory()
    async with factory() as s:
        t = await s.get(IclTrainingTask, task_id)
        if not t or not t.doubao_task_id or t.status in (2, 3, 4):
            return _task_to_dict(t) if t else None
        doubao_task_id = t.doubao_task_id

    now = _time.monotonic()
    last = _icl_last_refresh.get(task_id, 0.0)
    if now - last < _ICL_REFRESH_MIN_INTERVAL_SECS:
        async with factory() as s:
            t = await s.get(IclTrainingTask, task_id)
            return _task_to_dict(t) if t else None
    _icl_last_refresh[task_id] = now

    client = get_icl_client()
    try:
        r = await client.query_training(doubao_task_id)
    except Exception as e:
        logger.warning(f"[icl_refresh] task={task_id[:12]}... 查询失败 {e}")
        async with factory() as s:
            t = await s.get(IclTrainingTask, task_id)
            return _task_to_dict(t) if t else None

    status = int(r.get("status", 0))
    async with factory() as s:
        t = await s.get(IclTrainingTask, task_id)
        if not t:
            return None
        t.status = status
        t.progress = max(t.progress, int(r.get("progress", 0)))
        if status in _USABLE_STATUSES and r.get("cloned_voice_id"):
            t.cloned_voice_id = str(r["cloned_voice_id"])
            t.progress = 100
        if status == 3:
            t.error_msg = (r.get("error") or "豆包 ICL 训练失败")[:500]
        await s.commit()
        return _task_to_dict(t)


async def delete_icl_task(user_id: int, task_id: str) -> bool:
    """删除训练任务（含磁盘参考音频）。他人任务返回 False。"""
    factory = get_session_factory()
    async with factory() as s:
        t = await s.get(IclTrainingTask, task_id)
        if not t or t.user_id != user_id:
            return False
        ref_path = t.reference_audio_path
        await s.delete(t)
        await s.commit()
    try:
        if ref_path and Path(ref_path).is_file():
            Path(ref_path).unlink()
    except OSError:
        logger.warning(f"[icl_delete] 参考音频删除失败: {ref_path}")
    return True


async def icl_voices_for_user(user_id: int) -> list[dict[str, Any]]:
    """返回当前用户可用的 ICL 音色（供 /api/voices 聚合）。"""
    factory = get_session_factory()
    async with factory() as s:
        stmt = select(IclTrainingTask).where(
            IclTrainingTask.user_id == user_id,
            IclTrainingTask.status.in_(_USABLE_STATUSES),
            IclTrainingTask.cloned_voice_id.isnot(None),
        ).order_by(IclTrainingTask.created_at.desc())
        rows = list((await s.execute(stmt)).scalars().all())
        return [
            {
                "id": f"icl:{r.cloned_voice_id}",
                "name": r.voice_name or "我的克隆音色",
                "provider": "icl",
                "gender": "neutral",
                "age": "youth",
                "scene": ["复刻"],
                "dialect": "",
                "zh_tags": ["声音复刻", "自定义"],
                "task_id": r.task_id,
            }
            for r in rows
        ]
