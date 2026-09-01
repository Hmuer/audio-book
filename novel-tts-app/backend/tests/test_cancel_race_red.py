"""回归测试：cancel_build 后的 start_build 不应出现 worker 锁竞态。

修复前 (按 project_id 持有锁) 的典型竞态：
1) Build A 启动，_RUNNING_BUILDS.add(pid)；
2) 用户 cancel A：cancel_build 立即 discard(pid)，A 的 worker 仍可能在合成；
3) A worker 在 finally 再次 discard(pid) — 幂等但已经晚了；
4) 用户紧接着再次 start_build → 重新 add(pid)；
5) A 的旧 worker 终于完成 → finally discard(pid) → **误删** 第二次 worker 的注册；
6) 此时允许第二次 cancel → 第三个 start_build 启动 → 项目同时跑 2 个 worker。

修复后 (按 build_id 持有)：
- _ACTIVE_BUILDS = {build_id: project_id}
- cancel_build 按 build_id 精准释放；不影响其他 build；
- worker finally 只释放自己的 build_id；如果 build_id 已不在 _ACTIVE_BUILDS（被
  cancel 主动释放）→ 不再误删后续启动的新 worker。
"""
from __future__ import annotations

import asyncio

import pytest

FAKE_MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 4096


# ---------------------------------------------------------------------
# T-RACE1：cancel → 立即再 start_build → 旧 worker 迟延释放不应误删新 build_id 锁
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_then_restart_old_worker_finally_does_not_release_new(_isolate_data_dir):
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import (
        start_build, get_build_status, cancel_build,
        _ACTIVE_BUILDS, _RUNNING_LOCK,
    )
    from backend.app.db.session import init_db
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    cfgmod.settings.MULTICAST_STRICT_MODE = True

    class _BlockMC:
        """阻塞直到外部释放事件，然后抛错模拟 cancel 后 worker 退出。"""
        provider = "doubao"

        def __init__(self):
            self.unblock = asyncio.Event()

        async def synthesize_chapter_to_file(self, segments, output_path, *, speed=1.0,
                                             chapter_title="", instruction_text=None):
            try:
                await asyncio.wait_for(self.unblock.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            raise RuntimeError("cancel 后旧 worker 终于失败")

    block_mc = _BlockMC()
    prev_tts, prev_llm, prev_mc = (
        aifact._tts_instance, aifact._llm_instance, aifact._multicast_instance,
    )
    aifact._tts_instance = MockTTSProvider()
    aifact._llm_instance = MockLLMProvider()
    aifact._multicast_instance = block_mc
    try:
        await init_db()
        pid = (await create_project("cancel→restart race 测试")).project_id
        await import_file(
            pid,
            (
                "第一章 出发\n"
                "他们出发了。\n\n"
                "第二章 远方\n"
                "走向远方。\n\n"
                "第三章 归来\n"
                "他们终于归来。\n"
            ).encode("utf-8"),
            "book.txt",
        )
        await prepare_project(pid)

        # 1) 启动 Build A（multicast + strict → 一定会 failed）
        resp_a = await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="doubao:zh_female_qingxin",
            tts_provider="doubao", mode="multicast",
        )
        bid_a = resp_a.build_id
        assert bid_a in _ACTIVE_BUILDS, "Build A 必须注册到 _ACTIVE_BUILDS"

        # 2) 用户立刻 cancel → cancel 按 build_id 精准释放
        await cancel_build(project_id=pid, build_id=bid_a)

        async with _RUNNING_LOCK:
            assert bid_a not in _ACTIVE_BUILDS, (
                f"cancel 必须按 build_id 精准释放；bid_a 不应再注册在 _ACTIVE_BUILDS；"
                f"当前={list(_ACTIVE_BUILDS.keys())}"
            )

        # 3) 立即重新启动新 Build B（同样 multicast strict → 同样会失败/被 cancel）
        resp_b = await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="doubao:zh_female_qingxin",
            tts_provider="doubao", mode="multicast",
        )
        bid_b = resp_b.build_id
        assert bid_b != bid_a, "restart 必须生成新的 build_id"
        assert bid_b in _ACTIVE_BUILDS, (
            f"restart 后 bid_b 必须立即注册；实际 _ACTIVE_BUILDS={list(_ACTIVE_BUILDS.keys())}"
        )

        # 4) 释放旧 worker 阻塞，让旧 worker 终于进入 finally
        block_mc.unblock.set()

        # 5) 等旧 worker 退出（finally 调 _unregister_active_build），但只释放自己的 build_id
        #    → 即使 build_id 已不在 _ACTIVE_BUILDS（被 cancel 释放过），也只释放自己的 bid_a。
        #    注意 bid_b 仍必须存在。
        for _ in range(40):
            async with _RUNNING_LOCK:
                if bid_a not in _ACTIVE_BUILDS:
                    break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("旧 worker finally 未在合理时间内释放 bid_a")

        # 6) 关键断言：bid_b 仍应保留在 _ACTIVE_BUILDS 中（不被旧 worker 误删）
        async with _RUNNING_LOCK:
            assert bid_b in _ACTIVE_BUILDS, (
                "旧 worker finally 释放必须只释放自己的 build_id；"
                f"但 bid_b 被误删了，实际 _ACTIVE_BUILDS={list(_ACTIVE_BUILDS.keys())}"
            )

        # 7) 清理：取消 B 也应能正常完成
        await cancel_build(project_id=pid, build_id=bid_b)
        async with _RUNNING_LOCK:
            assert bid_b not in _ACTIVE_BUILDS
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        aifact._multicast_instance = prev_mc
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        # 清理残留
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale:
                _ACTIVE_BUILDS.pop(k, None)


# ---------------------------------------------------------------------
# T-RACE2：_ensure_project_not_running 在 cancel 后应立刻放行
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ensure_project_not_running_releases_after_cancel(_isolate_data_dir):
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import (
        _ACTIVE_BUILDS, _ensure_project_not_running, start_build, cancel_build, _RUNNING_LOCK,
    )
    from backend.app.db.session import init_db
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    cfgmod.settings.MULTICAST_STRICT_MODE = True

    class _BlockMC:
        provider = "doubao"

        def __init__(self):
            self._evt = asyncio.Event()

        async def synthesize_chapter_to_file(self, segments, output_path, *, speed=1.0,
                                             chapter_title="", instruction_text=None):
            await self._evt.wait()
            raise RuntimeError("block")

    block_mc = _BlockMC()
    prev_tts, prev_llm, prev_mc = (
        aifact._tts_instance, aifact._llm_instance, aifact._multicast_instance,
    )
    aifact._tts_instance = MockTTSProvider()
    aifact._llm_instance = MockLLMProvider()
    aifact._multicast_instance = block_mc
    try:
        await init_db()
        pid = (await create_project("ensure-not-running 测试")).project_id
        await import_file(
            pid,
            (
                "第一章 初见\n内容一。\n\n"
                "第二章 启程\n内容二。\n"
            ).encode("utf-8"),
            "book.txt",
        )
        await prepare_project(pid)

        resp = await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="doubao:zh_female_qingxin",
            tts_provider="doubao", mode="multicast",
        )
        bid = resp.build_id

        # 1) _ensure_project_not_running 应拒绝
        with pytest.raises(ValueError, match="正在合成"):
            await _ensure_project_not_running(pid, "测试")

        # 2) cancel → 应允许 retry
        await cancel_build(project_id=pid, build_id=bid)
        await _ensure_project_not_running(pid, "测试")  # 不应抛错

        # 3) 释放 block，让 worker 退出
        block_mc._evt.set()
        for _ in range(40):
            async with _RUNNING_LOCK:
                if bid not in _ACTIVE_BUILDS:
                    break
            await asyncio.sleep(0.1)
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        aifact._multicast_instance = prev_mc
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale:
                _ACTIVE_BUILDS.pop(k, None)


# ---------------------------------------------------------------------
# T-RACE3：同一 project 内不能并发两个 build（最基础的回归保险）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_start_same_project_is_blocked(_isolate_data_dir):
    """两个并发 start_build 同时进入：只有一个能进入 _register_active_build 路径。
    另一个会被 _ensure_project_not_running 拒绝（或 start_build 内的 DB active 检查复用）。"""
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import (
        _ACTIVE_BUILDS, _ensure_project_not_running, _RUNNING_LOCK,
    )
    from backend.app.db.session import init_db
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    cfgmod.settings.MULTICAST_STRICT_MODE = True

    class _BlockMC:
        provider = "doubao"

        def __init__(self):
            self._evt = asyncio.Event()

        async def synthesize_chapter_to_file(self, segments, output_path, *, speed=1.0,
                                             chapter_title="", instruction_text=None):
            await self._evt.wait()
            raise RuntimeError("block")

    block_mc = _BlockMC()
    prev_tts, prev_llm, prev_mc = (
        aifact._tts_instance, aifact._llm_instance, aifact._multicast_instance,
    )
    aifact._tts_instance = MockTTSProvider()
    aifact._llm_instance = MockLLMProvider()
    aifact._multicast_instance = block_mc
    try:
        await init_db()
        pid = (await create_project("concurrent 测试")).project_id
        await import_file(
            pid,
            (
                "第一章 初见\n内容一。\n\n"
                "第二章 启程\n内容二。\n"
            ).encode("utf-8"),
            "book.txt",
        )
        await prepare_project(pid)

        # 模拟"已经有一个活跃 build"：直接注册一个 fake build_id
        async with _RUNNING_LOCK:
            _ACTIVE_BUILDS["fake_active_build"] = pid
        try:
            with pytest.raises(ValueError, match="正在合成"):
                await _ensure_project_not_running(pid, "测试")

            # 直接添加第二个 fake 不应影响原有检测（按 build_id 维度）
            async with _RUNNING_LOCK:
                _ACTIVE_BUILDS["another_fake"] = pid
            with pytest.raises(ValueError, match="正在合成"):
                await _ensure_project_not_running(pid, "测试")

            # 释放其中一个
            async with _RUNNING_LOCK:
                _ACTIVE_BUILDS.pop("fake_active_build", None)
            with pytest.raises(ValueError, match="正在合成"):
                await _ensure_project_not_running(pid, "测试")

            # 全释放
            async with _RUNNING_LOCK:
                _ACTIVE_BUILDS.pop("another_fake", None)
            await _ensure_project_not_running(pid, "测试")  # 不抛错
        finally:
            async with _RUNNING_LOCK:
                _ACTIVE_BUILDS.pop("fake_active_build", None)
                _ACTIVE_BUILDS.pop("another_fake", None)
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        aifact._multicast_instance = prev_mc
        cfgmod.settings.MULTICAST_STRICT_MODE = False