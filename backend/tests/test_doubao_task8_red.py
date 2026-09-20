"""Task 8 回归 — 单章合成失败时的降级行为。

多播剧（Seed-Audio）严格失败模式已随模式一起下线：
mode=multicast 在 start_build 入口直接抛错，永远进不到合成；
因此只剩 classic 的占位降级路径需要覆盖。

T-ST2 (classic + 单章失败 → 生成占位静音 MP3，Build 状态为 success / partial_success)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_BOOK_TXT = """第一章 初遇
今天是美好的一天。阳光透过窗户洒进房间，林小雨伸了个懒腰。
"早上好！"她对着镜子里的自己说。

第二章 启程
林小雨拿起背包，走出了家门。张伟在门口等她。
"我们出发吧。"张伟微笑着说。
"""


# ---------------------------------------------------------------------
# T-ST2：classic + 单章失败 → 单章失败生成占位 MP3，Build 不被判定为 failed
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_classic_nonstrict_build_survives_chapter_failure(_isolate_data_dir):
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, get_build_status, _ACTIVE_BUILDS, _RUNNING_LOCK
    from backend.app.db.session import init_db
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    class _FailFirstCall(MockTTSProvider):
        def __init__(self):
            super().__init__()
            self._n = 0
        async def synthesize_to_bytes(self, text, voice_id, *, emotion="calm", speed=1.0, **kw):
            self._n += 1
            if self._n == 1:
                raise RuntimeError("模拟第一次合成失败（非严格模式）")
            return await super().synthesize_to_bytes(text, voice_id, emotion=emotion, speed=speed, **kw)

    prev_tts = aifact._tts_instance
    prev_llm = aifact._llm_instance
    aifact._tts_instance = _FailFirstCall()
    aifact._llm_instance = MockLLMProvider()
    try:
        await init_db()
        pid = (await create_project("非严格模式测试")).project_id
        await import_file(pid, _BOOK_TXT.encode("utf-8"), "book.txt")
        await prepare_project(pid)
        resp = await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="doubao:zh_male_qingcang_uranus_bigtts",
            tts_provider="doubao", mode="classic",
        )
        bid = resp.build_id
        for _ in range(60):
            s = await get_build_status(bid)
            if s.status in ("success", "partial_success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
        else:
            async with _RUNNING_LOCK:
                stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
                for k in stale:
                    _ACTIVE_BUILDS.pop(k, None)
            pytest.fail("build worker 超时未结束")
        # 非严格模式：不应当因为单章失败就直接 failed
        from backend.app.db.session import get_session_factory
        from backend.app.db.models import Build, BuildArtifact
        import sqlalchemy
        factory = get_session_factory()
        async with factory() as sess:
            b = await sess.get(Build, bid)
            # classic 默认占位 MP3 降级 → 部分成功也可；允许 partial_success 或 success
            assert b.status in ("success", "partial_success"), (
                f"非严格模式期望 success/partial_success，实际={b.status} msg={b.progress_msg!r}"
            )
            stmt = await sess.execute(
                sqlalchemy.select(BuildArtifact).where(
                    BuildArtifact.build_id == bid, BuildArtifact.status == "failed"
                )
            )
            failed_count = len(list(stmt.scalars().all()))
            # 非严格模式应产生至少 1 个失败章（降级为占位 MP3）；
            # status 允许 success 或 partial_success
            assert failed_count >= 1, "非严格模式应当记录失败章节（降级为占位静音 MP3）"
            assert b.status in ("success", "partial_success")
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale:
                _ACTIVE_BUILDS.pop(k, None)
