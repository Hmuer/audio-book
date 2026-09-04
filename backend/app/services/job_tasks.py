"""
P1 #7 后台任务持久化（JobTask 表 + 心跳 + 启动恢复 + 看门狗）。

设计目标：服务重启 / 进程崩溃 / OOM 后，
- 已经"没在跑"但 DB 还显示 running/pending 的孤儿任务能被自动恢复（带 checkpoint），
- 恢复失败的孤儿任务会被标记为 failed 并写 last_error，避免 UI 永远卡"合成中"。

与现有进程级 dict（_prepare_running_tasks / _ACTIVE_BUILDS）的关系：
- 进程 dict 仅在进程内有效，重启就丢；
- JobTask 表是持久层，看门狗负责把"DB 还有 + 进程 dict 没有"的孤儿重新 enqueue 一次。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import select

from ..db.models import JobTask
from ..db.session import get_session_factory

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------
# 配置（settings 可覆盖；兜底默认）
# ---------------------------------------------------------------------

def _settings_int(name: str, default: int) -> int:
    try:
        from ..core.config import settings
        v = getattr(settings, name, None)
        if v is None or v == "":
            return default
        return int(v)
    except Exception:
        return default


def _settings_float(name: str, default: float) -> float:
    try:
        from ..core.config import settings
        v = getattr(settings, name, None)
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


# 单次心跳间隔：worker 跑 asyncio 循环里每 N 秒 touch 一下
JOB_TASK_HEARTBEAT_INTERVAL_SECONDS: float = _settings_float(
    "JOB_TASK_HEARTBEAT_INTERVAL_SECONDS", 10.0
)
# 孤儿阈值：last_heartbeat_at 距离现在超过这个秒数视为"上次进程崩了，task 丢了"
JOB_TASK_ORPHAN_THRESHOLD_SECONDS: float = _settings_float(
    "JOB_TASK_ORPHAN_THRESHOLD_SECONDS", 30.0
)
# 启动恢复延迟：lifespan 起来后等几秒再扫，让事件循环/连接池就绪
JOB_TASK_STARTUP_SCAN_DELAY_SECONDS: float = _settings_float(
    "JOB_TASK_STARTUP_SCAN_DELAY_SECONDS", 5.0
)
# 看门狗轮询间隔
JOB_TASK_WATCHDOG_INTERVAL_SECONDS: float = _settings_float(
    "JOB_TASK_WATCHDOG_INTERVAL_SECONDS", 30.0
)


# ---------------------------------------------------------------------
# 基础 CRUD
# ---------------------------------------------------------------------

def _new_task_id(kind: str) -> str:
    """task_id 前缀用 kind，便于日志/调试一眼分清（实际唯一性靠 uuid4 hex）。"""
    return f"{kind}-{uuid.uuid4().hex}"


async def register_task(
    kind: str,
    target_id: str,
    *,
    trigger: str = "api",
) -> JobTask:
    """
    后台 worker 启动时调用：写一行 JobTask (pending → running 一次提交)。
    返回 JobTask 对象（包含 task_id）。
    """
    factory = get_session_factory()
    task_id = _new_task_id(kind)
    now = _utcnow()
    async with factory() as s:
        row = JobTask(
            task_id=task_id,
            kind=kind,
            target_id=target_id,
            status="running",
            trigger=trigger,
            restart_count=0,
            last_heartbeat_at=now,
            started_at=now,
            finished_at=None,
            error_msg=None,
            progress_json=None,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    logger.info(
        f"[job_task] register kind={kind} target={target_id[:16]}... "
        f"task_id={task_id} trigger={trigger}"
    )
    return row


async def heartbeat_task(task_id: str) -> None:
    """worker 每 N 秒调一次：刷新 last_heartbeat_at + updated_at。"""
    factory = get_session_factory()
    now = _utcnow()
    try:
        async with factory() as s:
            row = await s.get(JobTask, task_id)
            if not row:
                return
            row.last_heartbeat_at = now
            row.updated_at = now
            await s.commit()
    except Exception as e:
        # 心跳失败只警告：DB 短暂不可用时不应阻塞 worker
        logger.debug(f"[job_task] heartbeat fail task_id={task_id[:16]}...: {type(e).__name__}: {e}")


async def update_task_progress(task_id: str, progress: dict[str, Any] | None) -> None:
    """worker 把"白名单进度"写进 progress_json（前端/恢复时读取）。"""
    if progress is None:
        return
    factory = get_session_factory()
    now = _utcnow()
    try:
        async with factory() as s:
            row = await s.get(JobTask, task_id)
            if not row:
                return
            try:
                row.progress_json = json.dumps(progress, ensure_ascii=False, default=str)
            except Exception:
                row.progress_json = json.dumps({}, ensure_ascii=False)
            row.last_heartbeat_at = now
            row.updated_at = now
            await s.commit()
    except Exception as e:
        logger.debug(f"[job_task] update_progress fail task_id={task_id[:16]}...: {type(e).__name__}: {e}")


async def finish_task(
    task_id: str,
    *,
    status: str,
    error_msg: str | None = None,
) -> None:
    """worker 终止：写终态 + finished_at + 失败时记 error_msg。"""
    if status not in ("success", "failed", "cancelled"):
        raise ValueError(f"非法终态: {status}")
    factory = get_session_factory()
    now = _utcnow()
    async with factory() as s:
        row = await s.get(JobTask, task_id)
        if not row:
            logger.debug(f"[job_task] finish_task 已不存在 task_id={task_id[:16]}...")
            return
        row.status = status
        row.finished_at = now
        row.error_msg = (error_msg or "")[:2000] if error_msg else None
        row.updated_at = now
        await s.commit()
    logger.info(
        f"[job_task] finish kind={_kind_of_task_id(task_id)} task_id={task_id} "
        f"status={status}" + (f" err={error_msg[:120]}" if error_msg else "")
    )


def _kind_of_task_id(task_id: str) -> str:
    """从 task_id 前缀还原 kind（"prepare-abc..." → "prepare"）；解析失败返回 "unknown"。"""
    try:
        return task_id.split("-", 1)[0]
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------
# 心跳协程（worker 用 try/finally 包起来）
# ---------------------------------------------------------------------

class HeartbeatContext:
    """
    上下文管理器 / 异步上下文管理器：在 with 块内周期性 heartbeat task。

    用法：
        async with HeartbeatContext(task_id) as hb:
            ... 真正的工作 ...

    cancel 时会先 flush 一次"最后一次 heartbeat"再退出，确保 DB 不被误判为孤儿。
    """

    def __init__(self, task_id: str | None, interval: float | None = None) -> None:
        self.task_id = task_id
        self.interval = (
            float(interval) if interval is not None
            else JOB_TASK_HEARTBEAT_INTERVAL_SECONDS
        )
        self._task: asyncio.Task | None = None
        self._stop_evt: asyncio.Event | None = None
        # 未注册成功的 task_id → 整个上下文就是 no-op（仍可正常 __aenter__/__aexit__）
        self._enabled: bool = bool(task_id)

    async def __aenter__(self) -> "HeartbeatContext":
        if not self._enabled:
            return self
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(), name=f"job_task_heartbeat:{self.task_id[:16]}"
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._enabled:
            return
        if self._stop_evt is not None:
            self._stop_evt.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        # 退出前再 heartbeat 一次（即使正在 cancelled，确保最后一次心跳尽量接近 now）
        try:
            await heartbeat_task(self.task_id)
        except Exception:
            pass

    async def _loop(self) -> None:
        # 第一次 sleep 后再开始 heartbeat，避免刚 register 完就空跑一轮
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    await asyncio.wait_for(
                        self._stop_evt.wait(), timeout=self.interval,
                    )
                    return  # stop 触发
                except asyncio.TimeoutError:
                    pass
                await heartbeat_task(self.task_id)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(
                f"[job_task] heartbeat loop exit err={type(e).__name__}: {e}"
            )


# ---------------------------------------------------------------------
# 启动恢复 + 看门狗
# ---------------------------------------------------------------------

async def list_orphans() -> list[JobTask]:
    """
    列出当前孤儿任务：
      status = running AND
      (last_heartbeat_at IS NULL OR last_heartbeat_at < now - ORPHAN_THRESHOLD)
    """
    factory = get_session_factory()
    now = _utcnow()
    cutoff = now.timestamp() - JOB_TASK_ORPHAN_THRESHOLD_SECONDS
    cutoff_dt = datetime.fromtimestamp(cutoff, UTC).replace(tzinfo=None)
    async with factory() as s:
        q = select(JobTask).where(
            JobTask.status == "running",
        ).order_by(JobTask.created_at.asc())
        rows = list((await s.execute(q)).scalars().all())
    result: list[JobTask] = []
    for r in rows:
        if r.last_heartbeat_at is None:
            result.append(r)
            continue
        if r.last_heartbeat_at < cutoff_dt:
            result.append(r)
    return result


async def list_pending_tasks() -> list[JobTask]:
    """status=pending 的任务（理论上很少 —— 留作占位，目前都是 register 时直接 running）。"""
    factory = get_session_factory()
    async with factory() as s:
        q = select(JobTask).where(JobTask.status == "pending").order_by(
            JobTask.created_at.asc()
        )
        return list((await s.execute(q)).scalars().all())


async def mark_orphans_failed(err_prefix: str) -> int:
    """
    把 status=running 但已超过 ORPHAN_THRESHOLD 的孤儿直接置为 failed。
    兜底：万一目标 kind 不在 RECOVERY_KINDS 内（没有恢复逻辑），也保证不卡 UI。

    返回置为 failed 的数量。
    """
    orphans = await list_orphans()
    if not orphans:
        return 0
    factory = get_session_factory()
    now = _utcnow()
    n = 0
    async with factory() as s:
        for r in orphans:
            # 调用方先在外部尝试恢复；如果走不到这里说明恢复了 -> 这里其实是"恢复后被标 running 的孤儿"
            # 为避免重复处理，仅当 restart_count 仍为 0 且已经孤儿 → 直接 failed
            if r.restart_count == 0:
                # r 是 list_orphans() 的另一个 session 取出来的对象，本 session 不知情；
                # 用 merge 把它 attach 进来再改属性，否则 s.commit() 不会写库。
                r_attached = await s.merge(r)
                r_attached.status = "failed"
                r_attached.finished_at = now
                r_attached.error_msg = (
                    f"{err_prefix}: 任务心跳超过 {int(JOB_TASK_ORPHAN_THRESHOLD_SECONDS)}s "
                    f"未更新，且未匹配到任何恢复流程，置为 failed。请重试或联系管理员。"
                )[:2000]
                r_attached.updated_at = now
                n += 1
        if n:
            await s.commit()
    if n:
        logger.warning(
            f"[job_task] 孤儿兜底置 failed {n} 条 (threshold={int(JOB_TASK_ORPHAN_THRESHOLD_SECONDS)}s)"
        )
    return n


# 支持恢复的 kind；其它 kind 走 mark_orphans_failed 兜底
RECOVERY_KINDS: tuple[str, ...] = ("prepare", "build")


async def recover_orphans() -> dict[str, int]:
    """
    lifespan 启动 / 看门狗 周期调用：扫一遍孤儿，按 kind 调用各自的恢复函数。

    返回 {kind: re_enqueued_count}（用于日志/测试断言）。
    """
    orphans = await list_orphans()
    if not orphans:
        return {}

    by_kind: dict[str, list[JobTask]] = {}
    for o in orphans:
        by_kind.setdefault(o.kind, []).append(o)

    factory = get_session_factory()
    re_counts: dict[str, int] = {}

    async with factory() as s:
        # 标记 restart_count++ 并保持 running（直到 enqueue 内部真正接手）
        attached: list[JobTask] = []
        for o in orphans:
            o_attached = await s.merge(o)
            o_attached.restart_count = int(o_attached.restart_count or 0) + 1
            # 更新一次 heartbeat，避免恢复函数还没启动就被二次判为孤儿
            o_attached.last_heartbeat_at = _utcnow()
            o_attached.updated_at = _utcnow()
            attached.append(o_attached)
        await s.commit()
        # 后续恢复函数用 attached 版本（已 merge → 本 session 知道，改属性会写库）
        orphans = attached

    for kind, rows in by_kind.items():
        n_re = 0
        if kind == "prepare":
            for o in rows:
                try:
                    if await _recover_prepare_orphan(o):
                        n_re += 1
                except Exception as e:
                    logger.warning(
                        f"[job_task] 恢复 prepare 孤儿失败 task_id={o.task_id[:16]}...: "
                        f"{type(e).__name__}: {e}"
                    )
        elif kind == "build":
            for o in rows:
                try:
                    if await _recover_build_orphan(o):
                        n_re += 1
                except Exception as e:
                    logger.warning(
                        f"[job_task] 恢复 build 孤儿失败 task_id={o.task_id[:16]}...: "
                        f"{type(e).__name__}: {e}"
                    )
        else:
            # 不在 RECOVERY_KINDS → 走 mark_orphans_failed 兜底
            n_re = 0
        if n_re:
            re_counts[kind] = n_re

    if re_counts:
        logger.info(
            f"[job_task] 孤儿恢复结果: {re_counts} (total scanned={len(orphans)})"
        )
    return re_counts


async def _recover_prepare_orphan(job: JobTask) -> bool:
    """
    恢复一个 prepare 孤儿：
      1) 取最新 Project 状态；若不是 preparing 直接置 cancelled（说明用户已手动修改）
      2) 走既有 _recover_one_preparing_project（看 DB checkpoint 自动续跑）
    返回 True 表示已 enqueue 新 task，False 表示跳过。
    """
    from .project import (
        _is_prepare_running_for,
        _recover_one_preparing_project,
        _parse_progress,
    )
    pid = job.target_id
    if _is_prepare_running_for(pid):
        logger.info(
            f"[job_task][prepare] 本进程已有 {pid[:8]}... 的 prepare 在跑，跳过恢复 task_id={job.task_id[:16]}"
        )
        return False

    factory = get_session_factory()
    async with factory() as sess:
        from ..db.models import Project
        proj = await sess.get(Project, pid)
        # job 是外部传进来的孤儿 JobTask（可能来自另一 session）；merge 后改属性才会被 commit
        j_attached = await sess.merge(job)
        if not proj:
            # 项目被删了，把这条 JobTask 置 cancelled（失败兜底）
            j_attached.status = "cancelled"
            j_attached.finished_at = _utcnow()
            j_attached.error_msg = "项目已不存在，无法恢复"
            j_attached.updated_at = _utcnow()
            await sess.commit()
            return False
        if proj.status != "preparing":
            j_attached.status = "cancelled"
            j_attached.finished_at = _utcnow()
            j_attached.error_msg = (
                f"项目当前 status={proj.status}，不是 preparing，无需恢复 prepare"
            )
            j_attached.updated_at = _utcnow()
            await sess.commit()
            return False
        prog = _parse_progress(proj)
    # 触发恢复：既有的 _recover_one_preparing_project 会 _enqueue_prepare_task
    # （进程锁 + 看门狗双重覆盖）
    await _recover_one_preparing_project(proj, prog, trigger="job_task_recover")
    return True


async def _recover_build_orphan(job: JobTask) -> bool:
    """
    恢复一个 build 孤儿：
      1) 取 Build 状态；终态直接覆盖 JobTask
      2) 进程内 _ACTIVE_BUILDS 没持有 → 用既有 _run_build_inner 重新跑（已 done 章会 skip）
    返回 True 表示已 enqueue 新 task。
    """
    from .build import _ACTIVE_BUILDS, _run_build_inner
    factory = get_session_factory()
    async with factory() as sess:
        from ..db.models import Build
        b = await sess.get(Build, job.target_id)
        j_attached = await sess.merge(job)
        if not b:
            j_attached.status = "cancelled"
            j_attached.finished_at = _utcnow()
            j_attached.error_msg = "Build 已不存在，无法恢复"
            j_attached.updated_at = _utcnow()
            await sess.commit()
            return False
        if b.status not in ("queued", "running"):
            j_attached.status = "cancelled"
            j_attached.finished_at = _utcnow()
            j_attached.error_msg = (
                f"Build 当前 status={b.status}，无需恢复（终态）"
            )
            j_attached.updated_at = _utcnow()
            await sess.commit()
            return False
        # 复制所需字段，session 关闭后仍可用
        build_snapshot = {
            "build_id": b.build_id,
            "project_id": b.project_id,
            "narrator_voice_id": b.narrator_voice_id,
            "speed": b.speed,
            "voice_assignments_json": b.voice_assignments_json or "",
        }

    # 进程锁内已有同 build_id 的活跃 worker → 跳过（避免双跑）
    if build_snapshot["build_id"] in _ACTIVE_BUILDS:
        logger.info(
            f"[job_task][build] 本进程已有 build_id={build_snapshot['build_id'][:8]}... 在跑，跳过恢复"
        )
        return False

    # 重新跑（已 done 的 BuildArtifact 会自动跳过；cancelled 标志继续生效）
    try:
        voice_assignments = json.loads(build_snapshot["voice_assignments_json"] or "{}")
    except Exception:
        voice_assignments = {}

    async def _runner() -> None:
        try:
            await _run_build_inner(
                build_id=build_snapshot["build_id"],
                project_id=build_snapshot["project_id"],
                voice_assignments=voice_assignments,
                narrator_voice_id=build_snapshot["narrator_voice_id"],
                speed=build_snapshot["speed"],
                only_chapter_idxs=None,
                source_build_id=None,
            )
        except Exception as e:
            logger.error(
                f"[job_task][build][runner] FAIL build_id={build_snapshot['build_id'][:8]}...: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )

    asyncio.create_task(_runner(), name=f"build_recover_{build_snapshot['build_id'][:8]}")
    logger.info(
        f"[job_task][build] 恢复孤儿 build_id={build_snapshot['build_id'][:8]}... "
        f"restart_count={job.restart_count}"
    )
    return True


# ---------------------------------------------------------------------
# 启动入口（lifespan 调用）
# ---------------------------------------------------------------------

_watchdog_started = False


async def _watchdog_loop() -> None:
    logger.info(
        f"[job_task][watchdog] 启动 interval={JOB_TASK_WATCHDOG_INTERVAL_SECONDS}s "
        f"orphan_threshold={JOB_TASK_ORPHAN_THRESHOLD_SECONDS}s "
        f"heartbeat_interval={JOB_TASK_HEARTBEAT_INTERVAL_SECONDS}s"
    )
    while True:
        try:
            await asyncio.sleep(JOB_TASK_WATCHDOG_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("[job_task][watchdog] 被 Cancelled，退出。")
            return
        try:
            await recover_orphans()
        except Exception as e:
            logger.warning(
                f"[job_task][watchdog] recover 异常: {type(e).__name__}: {e}"
            )
        try:
            # 兜底：未知 kind 孤儿直接置 failed
            await mark_orphans_failed(
                err_prefix="P1 #7 watchdog"
            )
        except Exception as e:
            logger.warning(
                f"[job_task][watchdog] mark_orphans_failed 异常: {type(e).__name__}: {e}"
            )


async def _delayed_startup_scan() -> None:
    try:
        await asyncio.sleep(JOB_TASK_STARTUP_SCAN_DELAY_SECONDS)
    except asyncio.CancelledError:
        return
    try:
        re = await recover_orphans()
        if re:
            logger.info(f"[job_task][startup] 恢复孤儿任务: {re}")
    except Exception as e:
        logger.warning(
            f"[job_task][startup] recover 异常: {type(e).__name__}: {e}"
        )
    try:
        await mark_orphans_failed(err_prefix="P1 #7 startup")
    except Exception as e:
        logger.warning(
            f"[job_task][startup] mark_orphans_failed 异常: {type(e).__name__}: {e}"
        )


def ensure_job_task_watchdog_started() -> None:
    """
    lifespan startup 调用一次：起一个启动扫描 + 一个周期性看门狗协程。
    同进程内只起一次。
    """
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True
    try:
        asyncio.create_task(_delayed_startup_scan(), name="job_task-startup-scan")
        asyncio.create_task(_watchdog_loop(), name="job_task-watchdog")
        logger.info(
            f"[job_task] 启动恢复/看门狗已安排: "
            f"startup_delay={JOB_TASK_STARTUP_SCAN_DELAY_SECONDS}s "
            f"watchdog_interval={JOB_TASK_WATCHDOG_INTERVAL_SECONDS}s"
        )
    except RuntimeError:
        # 极早期调用（无事件循环）→ 静默失败，lifespan 兜底
        _watchdog_started = False
        logger.debug("[job_task] ensure_job_task_watchdog_started: 无事件循环，跳过")