"""A-5（整体优化批次 1）— start_build 的 mode 解析回归测试。

历史缺陷：
    `if not mode or resolved_mode == "classic":`
    由于 `mode` 的默认值本身就是 "classic"（且 `resolved_mode` 由 `mode or "classic"`
    得出），该条件**恒为真** → 显式传 `mode="classic"` 也会被
    `Project.default_build_mode` 覆盖。只要某项目的 default_build_mode 是 "multicast"，
    它的**所有**构建都会在入口直接抛 RuntimeError，且没有任何接口可以绕过
    （只能改数据库）。

修复后契约：
    mode=None（未指定）  → 回落 Project.default_build_mode，再兜底 "classic"
    mode="classic"（显式）→ 以调用方为准，不被项目默认值覆盖
    mode="multicast"      → 仍按「不降级契约」直接抛错
"""
from __future__ import annotations

import pytest

_BOOK = (
    "第一章 出发\n"
    "他们出发了。\n\n"
    "第二章 远方\n"
    "走向远方。\n"
)


async def _setup_project(default_build_mode: str | None) -> str:
    """建项目 → 导入 → prepare，并按需写入 Project.default_build_mode。"""
    from sqlalchemy import update as _sa_update

    from backend.app.db.models import Project
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services.project import (
        create_project,
        import_file,
        prepare_project,
    )

    await init_db()
    pid = (await create_project("mode 解析回归测试")).project_id
    await import_file(pid, _BOOK.encode("utf-8"), "book.txt")
    await prepare_project(pid)

    if default_build_mode is not None:
        factory = get_session_factory()
        async with factory() as s:
            await s.execute(
                _sa_update(Project)
                .where(Project.project_id == pid)
                .values(default_build_mode=default_build_mode)
            )
            await s.commit()
    return pid


@pytest.fixture
def _mock_providers():
    """注入 Mock TTS / LLM，避免真实外呼；用例结束后还原并清活跃 build。"""
    from backend.app.ai import factory as aifact
    from backend.app.services.build import _ACTIVE_BUILDS
    from backend.tests.mock_providers import MockLLMProvider, MockTTSProvider

    prev_tts, prev_llm = aifact._tts_instance, aifact._llm_instance
    aifact._tts_instance = MockTTSProvider()
    aifact._llm_instance = MockLLMProvider()
    try:
        yield
    finally:
        aifact._tts_instance, aifact._llm_instance = prev_tts, prev_llm
        _ACTIVE_BUILDS.clear()


@pytest.mark.asyncio
async def test_explicit_classic_not_overridden_by_project_multicast_default(
    _isolate_data_dir, _mock_providers,
):
    """项目默认 multicast 时，显式传 classic 必须按 classic 构建（不再抛错）。"""
    from backend.app.services.build import cancel_build, start_build

    pid = await _setup_project("multicast")
    resp = await start_build(
        project_id=pid,
        voice_assignments={},
        narrator_voice_id="male-qn-jingying",
        mode="classic",  # 显式指定
    )
    try:
        assert resp.mode == "classic", (
            f"显式 mode='classic' 不应被项目默认 multicast 覆盖，实际 mode={resp.mode!r}"
        )
    finally:
        try:
            await cancel_build(resp.build_id)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_omitted_mode_falls_back_to_project_default(
    _isolate_data_dir, _mock_providers,
):
    """未传 mode 时仍应回落到 Project.default_build_mode（multicast → 抛错）。"""
    from backend.app.services.build import start_build

    pid = await _setup_project("multicast")
    with pytest.raises(RuntimeError, match="已不再支持"):
        await start_build(
            project_id=pid,
            voice_assignments={},
            narrator_voice_id="male-qn-jingying",
            # mode 省略（None）→ 走项目默认 multicast
        )


@pytest.mark.asyncio
async def test_omitted_mode_defaults_to_classic_when_project_has_no_default(
    _isolate_data_dir, _mock_providers,
):
    """项目未设默认模式时，未传 mode → 兜底 classic。"""
    from backend.app.services.build import cancel_build, start_build

    pid = await _setup_project(None)
    resp = await start_build(
        project_id=pid,
        voice_assignments={},
        narrator_voice_id="male-qn-jingying",
    )
    try:
        assert resp.mode == "classic"
    finally:
        try:
            await cancel_build(resp.build_id)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_explicit_multicast_still_raises(
    _isolate_data_dir, _mock_providers,
):
    """显式 multicast 仍按「不降级契约」抛错。"""
    from backend.app.services.build import start_build

    pid = await _setup_project(None)
    with pytest.raises(RuntimeError, match="已不再支持"):
        await start_build(
            project_id=pid,
            voice_assignments={},
            narrator_voice_id="male-qn-jingying",
            mode="multicast",
        )
