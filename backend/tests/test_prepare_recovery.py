"""H-23 幽灵消耗死循环防护测试。

背景（用户实测）：prepare 中途进程死亡（OOM/重启）→ DB 状态卡 preparing →
看门狗（15s 轮询）+ 启动恢复自动从 checkpoint 续跑 → 进程再死 → 再恢复…
restart_count 只增不查 → **无限恢复死循环**：每次恢复重跑未 checkpoint 的
阶段，MiniMax Token Plan 按 5h 滚动窗口释放多少配额就被吃多少 —— 用户看到
「识别任务已停止但 token 还在消耗」（实测无任务时段 7M tokens/小时 × 4h）。

三层修复的对应测试：
1. 上限拦截：restart_count ≥ PREPARE_RECOVERY_MAX_RESTARTS（默认 3）→
   不再 enqueue，置 failed + RecoveryLimit last_error，0 次 LLM 调用。
2. 上限之下正常恢复：restart_count=1 → 恢复续跑，成功后计数清零。
3. restart_count 跨 prog 重建存活：fresh 起跑重建 prog（checkpoint 口径
   不兼容 / 全新起跑两条路径）不能吞掉计数——否则崩溃循环里每次恢复都
   从 0 数起，上限永远到不了（H-23 修复的命脉）。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

pytest_plugins = ("pytest_asyncio",)


def _book_text(n_chapters: int, chars_per_ch: int = 3000) -> str:
    from backend.tests.test_prepare_perf_red import _book_text as _bt

    return _bt(n_chapters, chars_per_ch)


async def _get_project_row(project_id: str):
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import Project

    factory = get_session_factory()
    async with factory() as sess:
        return await sess.get(Project, project_id)


async def _set_preparing_with_restarts(project_id: str, restart_count: int) -> None:
    """DB 手术：把项目置成「崩溃现场」——status=preparing + 恢复计数 +
    过期 updated_at（看门狗的 stuck 判定用）。"""
    from backend.app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as sess:
        proj = await sess.get(type(await _get_project_row(project_id)), project_id)
        prog = {
            "version": 1,
            "stage": "characters",
            "restart_count": restart_count,
            # 远古时刻 → 看门狗必判 stuck（无需等真实 10 分钟）
            "updated_at": "2020-01-01 00:00:00",
        }
        proj.status = "preparing"
        proj.progress_json = json.dumps(prog, ensure_ascii=False)
        await sess.commit()


async def _read_prog(project_id: str) -> dict:
    proj = await _get_project_row(project_id)
    return json.loads(proj.progress_json) if proj and proj.progress_json else {}


async def _make_project(name: str, n_chapters: int = 3):
    from backend.app.db.session import init_db
    from backend.app.services.project import create_project, import_file

    await init_db()
    resp = await create_project(name)
    await import_file(
        resp.project_id, _book_text(n_chapters).encode("utf-8"), f"{name}.txt"
    )
    return resp.project_id


@pytest.mark.asyncio
async def test_h23_cap_blocks_ghost_loop(_isolate_data_dir, caplog):
    """恢复次数达上限 → 不再自动恢复：置 failed + RecoveryLimit 错误，
    0 次 LLM 调用、0 个新任务（旧实现：每 15s 重新拉起，无限烧配额）。"""
    import logging

    from backend.app.ai import factory as ai_factory
    from backend.app.services.project import (
        _scan_preparing_and_recover,
        _prepare_running_tasks,
    )

    pid = await _make_project("H23-上限拦截")
    calls_before = len(ai_factory._llm_instance.calls)
    await _set_preparing_with_restarts(pid, restart_count=3)  # == 默认上限 3

    caplog.set_level(logging.INFO, logger="backend.app.services.project")
    await _scan_preparing_and_recover(trigger="watchdog")

    proj = await _get_project_row(pid)
    assert proj.status == "failed", (
        f"达上限后应置 failed 停止自动重试，实际 status={proj.status}"
    )
    prog = await _read_prog(pid)
    assert "RecoveryLimit" in (prog.get("last_error") or ""), prog.get("last_error")
    # 幽灵消耗的实锤断言：拦截路径一个 LLM 调用都不该发
    assert len(ai_factory._llm_instance.calls) == calls_before, "上限拦截路径不得产生 LLM 调用"
    assert pid not in _prepare_running_tasks, "上限拦截路径不得 enqueue 新任务"
    assert "自动恢复次数达上限" in caplog.text

    # failed 是终态：再扫也不复活、不产生任何调用
    await _scan_preparing_and_recover(trigger="watchdog")
    proj = await _get_project_row(pid)
    assert proj.status == "failed"
    assert len(ai_factory._llm_instance.calls) == calls_before


@pytest.mark.asyncio
async def test_h23_below_cap_recovers_and_resets(_isolate_data_dir):
    """上限之下正常恢复：restart_count=1 → 恢复续跑 → 成功后计数清零
    （连续失败语义：成功一次即重新享有完整恢复额度）。"""
    from backend.app.services.project import (
        _scan_preparing_and_recover,
        _prepare_running_tasks,
    )

    pid = await _make_project("H23-正常恢复")
    await _set_preparing_with_restarts(pid, restart_count=1)

    await _scan_preparing_and_recover(trigger="watchdog")

    # 恢复成功 → 已 enqueue 真实 prepare 任务
    info = _prepare_running_tasks.get(pid)
    assert info is not None, "restart_count=1 < 上限 3，应正常恢复续跑"
    await asyncio.wait_for(asyncio.shield(info["task"]), timeout=30)

    proj = await _get_project_row(pid)
    assert proj.status == "ready", f"恢复续跑应完成，实际 status={proj.status}"
    prog = await _read_prog(pid)
    assert prog.get("restart_count") is None, (
        f"prepare 成功后 restart_count 应清零，实际 {prog.get('restart_count')}"
    )
    assert pid not in _prepare_running_tasks, "任务收尾后登记应清空"


@pytest.mark.asyncio
async def test_h23_restart_count_survives_prog_rebuild(_isolate_data_dir, monkeypatch):
    """restart_count 必须跨 fresh 起跑的 prog 重建存活（H-23 命脉）。

    崩溃循环场景：恢复 → 计数+1 → prepare 全新起跑（prog 整体重建）→
    再次崩溃。若重建吞掉计数，每次恢复都从 0 数起，上限永远到不了。
    本测试：恢复置 restart_count=2 → prepare 因 LLM 全失败而 failed →
    DB prog 里必须仍是 2（经 _polish_summary_keep 带回，而非清零）。
    """
    from backend.app.ai import factory as ai_factory
    from backend.app.services.project import (
        _scan_preparing_and_recover,
        _prepare_running_tasks,
    )

    pid = await _make_project("H23-计数存活")
    await _set_preparing_with_restarts(pid, restart_count=1)

    mock_llm = ai_factory._llm_instance
    orig_chat = mock_llm.chat_structured

    async def all_char_calls_fail(*a, **kw):
        schema = kw.get("output_schema")
        if getattr(schema, "__name__", "") == "_ListWrapper":
            raise RuntimeError("boom: 模拟角色识别全失败")
        return await orig_chat(*a, **kw)

    monkeypatch.setattr(mock_llm, "chat_structured", all_char_calls_fail)

    await _scan_preparing_and_recover(trigger="watchdog")
    info = _prepare_running_tasks.get(pid)
    assert info is not None
    # 全切片失败 → 角色识别阶段抛业务异常 → prepare failed（终态）
    await asyncio.wait_for(asyncio.shield(info["task"]), timeout=30)

    proj = await _get_project_row(pid)
    assert proj.status == "failed"
    prog = await _read_prog(pid)
    assert int(prog.get("restart_count") or 0) == 2, (
        f"fresh 起跑重建 prog 后 restart_count 应存活为 2，"
        f"实际 {prog.get('restart_count')!r} —— 重建吞计数 → 上限永远到不了"
    )
    assert pid not in _prepare_running_tasks
