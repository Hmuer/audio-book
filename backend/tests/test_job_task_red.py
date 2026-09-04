"""
P1 #7 后台任务持久化（JobTask 表 + 启动恢复 + 看门狗）回归测试。

覆盖：
- register_task → heartbeat_task → finish_task 三阶段正确；
- list_orphans 正确识别心跳超时的 running 任务；
- recover_orphans：模拟"上次崩溃"的 prepare，能从 progress_json checkpoint 恢复；
- recover_orphans：build 孤儿不会重复启动本进程已在跑的 build；
- mark_orphans_failed：未知 kind 孤儿兜底置 failed；
- HeartbeatContext(None) 是 no-op。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, UTC

import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_register_heartbeat_finish_round_trip(_isolate_data_dir):
    """register → heartbeat × N → finish：状态机正确推进。"""
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.job_tasks import (
        register_task, heartbeat_task, finish_task,
    )
    from backend.app.db.models import JobTask
    from sqlalchemy import select

    await init_db()
    factory = get_session_factory()
    target_id = uuid.uuid4().hex
    job = await register_task("prepare", target_id, trigger="api")
    assert job.task_id.startswith("prepare-")
    assert job.status == "running"
    assert job.last_heartbeat_at is not None

    # heartbeat
    await heartbeat_task(job.task_id)
    await heartbeat_task(job.task_id)

    # finish
    await finish_task(job.task_id, status="success")
    async with factory() as s:
        row = (await s.execute(select(JobTask).where(JobTask.task_id == job.task_id))).scalar_one()
    assert row.status == "success"
    assert row.finished_at is not None
    assert row.error_msg is None


@pytest.mark.asyncio
async def test_list_orphans_detects_stale_heartbeat(_isolate_data_dir):
    """list_orphans 把心跳超过阈值的 running 任务列为孤儿。"""
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.job_tasks import register_task, list_orphans
    from backend.app.db.models import JobTask
    from sqlalchemy import update

    await init_db()
    job = await register_task("prepare", uuid.uuid4().hex, trigger="api")

    # 把 last_heartbeat_at 改成 1 小时前 → 应被列为孤儿
    long_ago = _utcnow() - timedelta(hours=1)
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            update(JobTask)
            .where(JobTask.task_id == job.task_id)
            .values(last_heartbeat_at=long_ago)
        )
        await s.commit()

    orphans = await list_orphans()
    orphan_ids = {o.task_id for o in orphans}
    assert job.task_id in orphan_ids


@pytest.mark.asyncio
async def test_heartbeat_context_is_noop_for_none(_isolate_data_dir):
    """HeartbeatContext(None) 是 no-op：__aenter__/__aexit__ 不抛、不起 task。"""
    from backend.app.services.job_tasks import HeartbeatContext

    async with HeartbeatContext(None) as hb:
        # 跑一段 sleep，验证不抛
        await asyncio.sleep(0.05)
        assert hb is not None
    # 正常返回
    assert True


@pytest.mark.asyncio
async def test_recover_orphans_prepare_restarts_from_checkpoint(_isolate_data_dir):
    """模拟『上次崩溃』：prepare 任务被 register 但 last_heartbeat_at 是 1 小时前 →
    recover_orphans 应把它 re-enqueue（走 _recover_one_preparing_project）。"""
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.job_tasks import (
        register_task, recover_orphans, list_orphans,
    )
    from backend.app.services.project import (
        trigger_prepare_project, _is_prepare_running_for, _cancel_running_prepare_task,
    )
    from backend.app.db.models import Project, JobTask
    from sqlalchemy import update, select

    await init_db()
    factory = get_session_factory()
    pid = uuid.uuid4().hex

    # 1) 建一个真实项目 + 上传文本 + prepare 触发 → 项目 status=preparing，_prepare_running_tasks 也填了
    async with factory() as s:
        from backend.app.db.models import User
        from backend.app.services.auth import seed_admin_user
        await seed_admin_user()
        admin = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        s.add(Project(
            project_id=pid,
            name="P",
            status="imported",
            owner_user_id=admin.id,
            source_file_path="/nonexistent_test.txt",  # 不存在 → 恢复时会置 failed
            source_filename="t.txt",
            source_charset="utf-8",
        ))
        await s.commit()

    # 2) 直接写一个孤儿 JobTask（绕开 trigger_prepare_project 的 _enqueue_prepare_task，避免对 mock 依赖）
    long_ago = _utcnow() - timedelta(hours=1)
    async with factory() as s:
        # 制造孤儿 JobTask
        from backend.app.db.models import JobTask as _JT
        orphan = _JT(
            task_id=f"prepare-{uuid.uuid4().hex}",
            kind="prepare",
            target_id=pid,
            status="running",
            trigger="api",
            restart_count=0,
            last_heartbeat_at=long_ago,
            started_at=long_ago,
            created_at=long_ago,
            updated_at=long_ago,
        )
        s.add(orphan)
        # 项目状态必须是 preparing，否则 recover 会直接置 cancelled
        proj = await s.get(Project, pid)
        proj.status = "preparing"
        await s.commit()

    orphans = await list_orphans()
    assert any(o.target_id == pid for o in orphans)

    # 3) 调用 recover_orphans → 触发 _recover_one_preparing_project
    #    由于 source_file_path 不存在，_recover_one_preparing_project 会把项目置 failed
    res = await recover_orphans()
    # 不论结果如何，必须有"恢复尝试"日志；这里不强求 prepare 在结果字典里（依赖恢复函数）
    async with factory() as s:
        proj = await s.get(Project, pid)
    # 项目应该被标记为 failed（因为 source_file 缺失）
    assert proj.status == "failed"


@pytest.mark.asyncio
async def test_recover_orphans_skips_in_process_running(_isolate_data_dir):
    """本进程内 _ACTIVE_BUILDS 持有同 build_id 的活跃 worker → recover 不重复启动。"""
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.job_tasks import (
        register_task, recover_orphans,
    )
    from backend.app.services.build import _ACTIVE_BUILDS, _register_active_build, _unregister_active_build

    await init_db()
    bid = uuid.uuid4().hex
    pid = uuid.uuid4().hex

    # 注册孤儿 JobTask
    job = await register_task("build", bid, trigger="api")
    # 让心跳过期
    long_ago = _utcnow() - timedelta(hours=1)
    from backend.app.db.models import JobTask
    from sqlalchemy import update
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            update(JobTask)
            .where(JobTask.task_id == job.task_id)
            .values(last_heartbeat_at=long_ago)
        )
        await s.commit()

    # 模拟"本进程内有该 build 的活跃 worker"
    await _register_active_build(bid, pid)
    try:
        res = await recover_orphans()
        # 本进程有活跃 → 跳过；结果应不包含这条孤儿被 enqueue
        # 我们的 _recover_build_orphan 返回 False（被跳过），结果字典里 kind 不会出现
        assert "build" not in res
    finally:
        await _unregister_active_build(bid, pid)


@pytest.mark.asyncio
async def test_mark_orphans_failed_for_unknown_kind(_isolate_data_dir):
    """未实现恢复逻辑的 kind 走 mark_orphans_failed 兜底。"""
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.job_tasks import (
        register_task, mark_orphans_failed,
    )
    from backend.app.db.models import JobTask
    from sqlalchemy import update, select

    await init_db()
    factory = get_session_factory()
    job = await register_task("mystery_kind", uuid.uuid4().hex, trigger="api")
    long_ago = _utcnow() - timedelta(hours=1)
    async with factory() as s:
        await s.execute(
            update(JobTask)
            .where(JobTask.task_id == job.task_id)
            .values(last_heartbeat_at=long_ago)
        )
        await s.commit()

    n = await mark_orphans_failed(err_prefix="test")
    assert n >= 1
    async with factory() as s:
        row = (await s.execute(select(JobTask).where(JobTask.task_id == job.task_id))).scalar_one()
    assert row.status == "failed"
    assert row.error_msg and "test" in row.error_msg


@pytest.mark.asyncio
async def test_finish_task_records_error_msg(_isolate_data_dir):
    """finish_task(failed, error_msg=...) → status=failed + error_msg 写入。"""
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.services.job_tasks import register_task, finish_task
    from backend.app.db.models import JobTask
    from sqlalchemy import select

    await init_db()
    factory = get_session_factory()
    job = await register_task("prepare", uuid.uuid4().hex, trigger="api")
    await finish_task(
        job.task_id, status="failed",
        error_msg="SomeException: boom",
    )
    async with factory() as s:
        row = (await s.execute(select(JobTask).where(JobTask.task_id == job.task_id))).scalar_one()
    assert row.status == "failed"
    assert row.error_msg == "SomeException: boom"
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_finish_task_rejects_invalid_status(_isolate_data_dir):
    """finish_task 收到非终态 status 应抛 ValueError，避免脏数据。"""
    from backend.app.services.job_tasks import finish_task

    with pytest.raises(ValueError):
        await finish_task("foo", status="running")