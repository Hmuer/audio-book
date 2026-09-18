"""批次 2（整体优化）任务生命周期 / 数据正确性回归测试。

覆盖：
- B-1 孤儿 build 恢复会注册 _ACTIVE_BUILDS + 维持 JobTask 心跳（不再每周期重复 spawn）
- B-2 start_build 按 project 串行化，并发调用不会创建重复 Build
- B-3 终态写入是条件更新（打包期间被取消时不复活）
- B-4 queued 孤儿超时兜底（不会永久阻塞项目）
- B-5 retry 复用章另存为独立文件（删任一 build 不连带删另一个）
- B-8 取消路径补记 TTS 用量；中途异常也保留已消耗用量
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, UTC

import pytest

BOOK_TXT = (
    "第一章 初遇\n"
    "林若雪说：「你好。」\n\n"
    "第二章 告别\n"
    "李明说：「再见。」\n"
).encode("utf-8")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clear_seg_cache() -> None:
    """段级缓存的内存表是模块级、跨测试共享；这里显式清空以保证真实调用 TTS。"""
    from backend.app.services import build as build_mod

    build_mod._tts_seg_mem_cache.clear()


async def _setup_project(name: str) -> str:
    from backend.app.services.project import (
        create_project, import_file, prepare_project,
    )
    from backend.app.db.session import init_db

    await init_db()
    pid = (await create_project(name)).project_id
    await import_file(pid, BOOK_TXT, "book.txt")
    await prepare_project(pid)
    return pid


async def _wait_terminal(build_id: str, timeout: float = 15.0) -> str:
    from backend.app.services.build import get_build_status

    deadline = time.monotonic() + timeout
    status = "unknown"
    while time.monotonic() < deadline:
        st = await get_build_status(build_id)
        status = st.status
        if status in ("success", "partial_success", "failed", "cancelled"):
            return status
        await asyncio.sleep(0.05)
    return status


async def _wait_active_gone(build_id: str, timeout: float = 10.0) -> bool:
    from backend.app.services.build import _ACTIVE_BUILDS, _RUNNING_LOCK

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with _RUNNING_LOCK:
            if build_id not in _ACTIVE_BUILDS:
                return True
        await asyncio.sleep(0.05)
    return False


async def _drop_active(pid: str) -> None:
    from backend.app.services.build import _ACTIVE_BUILDS, _RUNNING_LOCK

    async with _RUNNING_LOCK:
        for bid in [k for k, v in _ACTIVE_BUILDS.items() if v == pid]:
            _ACTIVE_BUILDS.pop(bid, None)


# ---------------------------------------------------------------------
# B-1：孤儿恢复注册 _ACTIVE_BUILDS + 心跳
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b1_orphan_recovery_registers_active_and_heartbeats(_isolate_data_dir, monkeypatch):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build, JobTask
    from backend.app.services import build as build_mod
    from backend.app.services import job_tasks as jt_mod
    from backend.app.services.build import _ACTIVE_BUILDS, _RUNNING_LOCK
    from sqlalchemy import select, update

    await init_db()
    pid = await _setup_project("B-1 孤儿恢复")
    bid = uuid.uuid4().hex
    factory = get_session_factory()
    async with factory() as s:
        s.add(Build(
            build_id=bid, project_id=pid, status="running", total_chapters=2,
            narrator_voice_id="female-tianmei", speed=1.0, mode="classic",
            tts_provider="minimax", started_at=_utcnow(),
        ))
        await s.commit()

    # 造一个心跳已过期的孤儿 JobTask
    job = await jt_mod.register_task("build", bid, trigger="api")
    long_ago = _utcnow() - timedelta(hours=1)
    async with factory() as s:
        await s.execute(
            update(JobTask).where(JobTask.task_id == job.task_id).values(
                last_heartbeat_at=long_ago,
            )
        )
        await s.commit()

    # 心跳间隔调小，便于在测试内观察
    monkeypatch.setattr(jt_mod, "JOB_TASK_HEARTBEAT_INTERVAL_SECONDS", 0.05)

    gate = asyncio.Event()

    async def _fake_inner(**_kw):
        await gate.wait()

    monkeypatch.setattr(build_mod, "_run_build_inner", _fake_inner)

    try:
        res = await jt_mod.recover_orphans()
        assert res.get("build") == 1, f"应恢复 1 个 build 孤儿，实际 {res}"
        async with _RUNNING_LOCK:
            assert bid in _ACTIVE_BUILDS, (
                f"B-1：恢复出的 runner 必须注册进 _ACTIVE_BUILDS；当前={list(_ACTIVE_BUILDS.keys())}"
            )

        # 心跳应被恢复 runner 刷新（从 1 小时前变成近似 now）
        async with factory() as s:
            row = (await s.execute(
                select(JobTask).where(JobTask.task_id == job.task_id)
            )).scalar_one()
            refreshed = row.last_heartbeat_at
        assert refreshed is not None and refreshed > long_ago, (
            f"B-1：恢复 runner 必须维持心跳；last_heartbeat_at 未刷新: {refreshed}"
        )

        # 再次制造孤儿心跳 → 因为 _ACTIVE_BUILDS 已持有，不应重复 spawn
        async with factory() as s:
            await s.execute(
                update(JobTask).where(JobTask.task_id == job.task_id).values(
                    last_heartbeat_at=long_ago,
                )
            )
            await s.commit()
        res2 = await jt_mod.recover_orphans()
        assert "build" not in res2, (
            f"B-1：本进程已在跑的 build 不应被重复恢复；实际 {res2}"
        )
    finally:
        gate.set()
        await _wait_active_gone(bid, timeout=5.0)
        await _drop_active(pid)


# ---------------------------------------------------------------------
# B-2：start_build 按 project 串行化
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b2_start_serializer_never_overlaps_same_project():
    from backend.app.services.build import _serialize_start_per_project

    state = {"active": 0, "max": 0}

    @_serialize_start_per_project
    async def _fake(project_id, tag):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return tag

    out = await asyncio.gather(_fake("p1", "a"), _fake("p1", "b"))
    assert set(out) == {"a", "b"}
    assert state["max"] == 1, (
        f"B-2：同 project 的 start 必须串行；检测到并发度={state['max']}"
    )


@pytest.mark.asyncio
async def test_b2_concurrent_start_creates_single_build(_isolate_data_dir):
    from backend.app.services.build import start_build, cancel_build
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Build
    from sqlalchemy import func, select

    pid = await _setup_project("B-2 并发去重")

    async def _do():
        return await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="female-tianmei",
        )

    r1, r2 = await asyncio.gather(_do(), _do())
    try:
        assert r1.build_id == r2.build_id, (
            f"B-2：并发 start_build 应复用同一 build；实际 {r1.build_id} vs {r2.build_id}"
        )
        factory = get_session_factory()
        async with factory() as s:
            n = (await s.execute(
                select(func.count()).select_from(Build).where(Build.project_id == pid)
            )).scalar_one()
        assert n == 1, f"B-2：同项目只应创建 1 个 Build，实际 {n}"
    finally:
        try:
            await cancel_build(project_id=pid, build_id=r1.build_id)
        except Exception:
            pass
        await _wait_terminal(r1.build_id, timeout=10.0)
        await _wait_active_gone(r1.build_id, timeout=5.0)
        await _drop_active(pid)


# ---------------------------------------------------------------------
# B-3：终态写入是条件更新
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b3_terminal_write_skipped_if_not_running(_isolate_data_dir):
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build
    from backend.app.services.build import _apply_terminal_status

    await init_db()
    pid = await _setup_project("B-3 终态保护")
    factory = get_session_factory()

    # 1) running → 正常写回
    bid_run = uuid.uuid4().hex
    async with factory() as s:
        s.add(Build(
            build_id=bid_run, project_id=pid, status="running", total_chapters=1,
            narrator_voice_id="female-tianmei", speed=1.0, mode="classic",
            tts_provider="minimax",
        ))
        await s.commit()
    applied = await _apply_terminal_status(
        bid_run, final_status="success", progress_msg="done", zip_filename="x.zip",
        total_size_bytes=1, total_duration_ms=1, failed_chapters=[],
        tts_calls=2, tts_chars=8,
    )
    assert applied is True
    async with factory() as s:
        b = await s.get(Build, bid_run)
        assert b.status == "success" and b.tts_calls == 2

    # 2) 打包期间已被取消 → 不写回，保持 cancelled
    bid_cancel = uuid.uuid4().hex
    async with factory() as s:
        s.add(Build(
            build_id=bid_cancel, project_id=pid, status="cancelled", total_chapters=1,
            narrator_voice_id="female-tianmei", speed=1.0, mode="classic",
            tts_provider="minimax", progress_msg="已取消：已完成 1/1 章",
        ))
        await s.commit()
    applied2 = await _apply_terminal_status(
        bid_cancel, final_status="success", progress_msg="done", zip_filename="y.zip",
        total_size_bytes=1, total_duration_ms=1, failed_chapters=[],
        tts_calls=2, tts_chars=8,
    )
    assert applied2 is False, "B-3：非 running 的终态不应被写回"
    async with factory() as s:
        b2 = await s.get(Build, bid_cancel)
        assert b2.status == "cancelled", "B-3：已取消的 build 不能被复活为 success"
        assert b2.zip_filename is None


# ---------------------------------------------------------------------
# B-4：queued 孤儿超时兜底
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b4_stale_queued_build_is_cancelled(_isolate_data_dir):
    from backend.app.core.config import settings
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Build
    from backend.app.services.build import start_build, cancel_build

    pid = await _setup_project("B-4 queued 孤儿")
    factory = get_session_factory()
    stale_id = uuid.uuid4().hex
    old = _utcnow() - timedelta(minutes=int(settings.BUILD_QUEUED_TIMEOUT_MINUTES) + 5)
    async with factory() as s:
        s.add(Build(
            build_id=stale_id, project_id=pid, status="queued", total_chapters=2,
            progress_msg="卡住的 queued", narrator_voice_id="female-tianmei",
            speed=1.0, mode="classic", tts_provider="minimax",
            created_at=old,
        ))
        await s.commit()

    resp = await start_build(
        project_id=pid, voice_assignments={}, narrator_voice_id="female-tianmei",
    )
    try:
        assert resp.build_id != stale_id, "B-4：超时的 queued 孤儿不应再阻塞新构建"
        async with factory() as s:
            stale = await s.get(Build, stale_id)
            assert stale.status == "cancelled", (
                f"B-4：超时 queued 孤儿应被判定取消，实际 {stale.status}"
            )
    finally:
        try:
            await cancel_build(project_id=pid, build_id=resp.build_id)
        except Exception:
            pass
        await _wait_terminal(resp.build_id, timeout=10.0)
        await _wait_active_gone(resp.build_id, timeout=5.0)
        await _drop_active(pid)


@pytest.mark.asyncio
async def test_b4_fresh_queued_build_is_reused_not_cancelled(_isolate_data_dir):
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Build
    from backend.app.services.build import start_build

    pid = await _setup_project("B-4 新鲜 queued")
    factory = get_session_factory()
    fresh_id = uuid.uuid4().hex
    async with factory() as s:
        s.add(Build(
            build_id=fresh_id, project_id=pid, status="queued", total_chapters=2,
            progress_msg="刚排队", narrator_voice_id="female-tianmei",
            speed=1.0, mode="classic", tts_provider="minimax",
        ))
        await s.commit()

    resp = await start_build(
        project_id=pid, voice_assignments={}, narrator_voice_id="female-tianmei",
    )
    try:
        assert resp.build_id == fresh_id, (
            f"B-4：未超时的 queued build 应被复用；实际 {resp.build_id} vs {fresh_id}"
        )
        async with factory() as s:
            fresh = await s.get(Build, fresh_id)
            assert fresh.status != "cancelled"
    finally:
        await _drop_active(pid)


# ---------------------------------------------------------------------
# B-5：retry 复用章另存为独立文件
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b5_retry_reused_chapter_has_own_file(_isolate_data_dir):
    from backend.app.core.config import settings
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Build, BuildArtifact
    from backend.app.services.build import (
        start_build, retry_failed_build, delete_build, get_build,
    )
    from pathlib import Path
    from sqlalchemy import select

    pid = await _setup_project("B-5 retry 文件独立")
    src = await start_build(
        project_id=pid, voice_assignments={}, narrator_voice_id="female-tianmei",
    )
    src_id = src.build_id
    assert await _wait_terminal(src_id) == "success"

    # 人为把第 2 章标为失败并写入 failed_chapters_json，制造"可重试"的源 build
    factory = get_session_factory()
    async with factory() as s:
        b = await s.get(Build, src_id)
        b.status = "partial_success"
        b.failed_chapters_json = "[1]"
        art = (await s.execute(
            select(BuildArtifact).where(
                BuildArtifact.build_id == src_id,
                BuildArtifact.chapter_idx == 1,
            )
        )).scalar_one()
        art.status = "failed"
        await s.commit()

    detail = await get_build(pid, src_id)
    src_reused_fname = next(
        a.audio_url.rsplit("/", 1)[-1]
        for a in detail.artifacts if a.chapter_idx == 0
    )

    retry = await retry_failed_build(src_id)
    new_id = retry.build_id
    try:
        assert new_id != src_id
        assert await _wait_terminal(new_id) in ("success", "partial_success")

        detail_new = await get_build(pid, new_id)
        new_reused = next(a for a in detail_new.artifacts if a.chapter_idx == 0)
        new_reused_fname = new_reused.audio_url.rsplit("/", 1)[-1]
        assert new_reused_fname != src_reused_fname, (
            f"B-5：retry 复用章不应与源 build 共享文件名；"
            f"源={src_reused_fname} 新={new_reused_fname}"
        )

        audio_dir = Path(settings.AUDIO_DIR)
        new_file = audio_dir / new_reused_fname
        src_file = audio_dir / src_reused_fname
        assert new_file.is_file() and src_file.is_file(), "两 build 的章文件都应存在"

        # 删除源 build → retry build 的复用章文件必须仍然存在
        assert await _wait_active_gone(new_id, timeout=5.0), "retry worker 应及时释放锁"
        await delete_build(pid, src_id)
        assert new_file.is_file(), (
            "B-5：删除源 build 不应连带删掉 retry build 引用的章节 MP3"
        )
    finally:
        await _wait_active_gone(new_id, timeout=5.0)
        await _drop_active(pid)


# ---------------------------------------------------------------------
# B-8：取消路径补记用量
# ---------------------------------------------------------------------
class _GateTTS:
    """第一次真实合成即阻塞，便于测试在「某章正在合成」时精确触发取消。"""

    name = "gate_tts"
    provider = "minimax"

    def __init__(self):
        self._super = None
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    def _base(self):
        from backend.tests.mock_providers import MockTTSProvider
        if self._super is None:
            self._super = MockTTSProvider()
        return self._super

    async def list_voices(self):
        return await self._base().list_voices()

    async def synthesize_to_bytes(self, text, voice_id, **kw):
        self.started.set()
        await self.gate.wait()
        return await self._base().synthesize_to_bytes(text, voice_id, **kw)

    async def synthesize_to_file(self, text, voice_id, output_path, **kw):
        data, dur = await self.synthesize_to_bytes(text, voice_id, **kw)
        with open(output_path, "wb") as f:
            f.write(data)
        return output_path, dur


@pytest.mark.asyncio
async def test_b8_cancelled_build_still_records_usage(_isolate_data_dir, monkeypatch):
    from backend.app.ai import factory as aifact
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build, UsageEvent
    from backend.app.services.build import start_build, cancel_build
    from sqlalchemy import select

    await init_db()
    _clear_seg_cache()
    pid = await _setup_project("B-8 取消补记用量")

    tts = _GateTTS()
    monkeypatch.setattr(aifact, "_tts_instance", tts)

    resp = await start_build(
        project_id=pid, voice_assignments={}, narrator_voice_id="female-tianmei",
    )
    bid = resp.build_id
    try:
        # 等某章真正进入合成，确保取消发生在合成中（该章仍会完成并计入 tts_calls）
        await asyncio.wait_for(tts.started.wait(), timeout=5.0)
        await cancel_build(project_id=pid, build_id=bid)
        tts.gate.set()

        # 注意：cancel_build 会立刻把 status 置为 cancelled，不能靠 status 轮询等 worker；
        # 这里直接等「取消路径补记的用量」落库，证明 worker 已走完取消分支。
        factory = get_session_factory()
        b = None
        for _ in range(120):
            async with factory() as s:
                b = await s.get(Build, bid)
                if b and (b.tts_calls or 0) > 0:
                    break
            await asyncio.sleep(0.05)
        assert b is not None and (b.tts_calls or 0) > 0, (
            f"B-8：取消路径也要把已消耗的 tts_calls 落库 "
            f"(status={getattr(b, 'status', None)} progress={getattr(b, 'progress_msg', None)!r})"
        )
        assert b.status == "cancelled"

        # UsageEvent 由 fire-and-forget 任务写入，轮询等待
        found = False
        for _ in range(40):
            async with factory() as s:
                rows = (await s.execute(
                    select(UsageEvent).where(
                        UsageEvent.build_id == bid,
                        UsageEvent.kind == "tts",
                    )
                )).scalars().all()
            if rows:
                found = True
                break
            await asyncio.sleep(0.05)
        assert found, "B-8：取消路径也必须写入项目级 TTS 用量（UsageEvent）"
    finally:
        tts.gate.set()
        await _wait_active_gone(bid, timeout=5.0)
        await _drop_active(pid)


@pytest.mark.asyncio
async def test_b8_exception_path_keeps_consumed_usage(_isolate_data_dir, monkeypatch):
    """打包阶段抛异常 → 前面各章已真实消耗的用量仍应留在 Build 行（增量落库）。"""
    from backend.app.services import build as build_mod
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build

    await init_db()
    _clear_seg_cache()
    pid = await _setup_project("B-8 异常保用量")

    def _boom(*_a, **_kw):
        raise RuntimeError("模拟打包失败")

    monkeypatch.setattr(build_mod, "_build_book_zip", _boom)

    resp = await build_mod.start_build(
        project_id=pid, voice_assignments={}, narrator_voice_id="female-tianmei",
    )
    bid = resp.build_id
    try:
        assert await _wait_terminal(bid) == "failed"
        await _wait_active_gone(bid, timeout=5.0)
        factory = get_session_factory()
        async with factory() as s:
            b = await s.get(Build, bid)
            assert (b.tts_calls or 0) > 0, (
                "B-8：中途异常退出时，已完成章节的 TTS 用量不应丢失"
            )
    finally:
        await _drop_active(pid)
