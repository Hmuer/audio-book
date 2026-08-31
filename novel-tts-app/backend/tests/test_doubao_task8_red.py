"""Task 8 RED — Build 管线严格失败模式（用户需求点 #4）。

T-ST1 (multicast + strict → 单章失败 → Build failed，不生成占位静音)
T-ST2 (classic + 默认 non-strict → 单章失败 → 仍生成占位静音，Build 状态为 success 或 success_with_failures，至少不为 failed)
T-ST3 (multicast + strict → 章节失败时 BuildArtifact.status='failed'，且 audio_url/audio_filename 为空)
T-ST4 (strict 模式下，retry-failed 能再次触发，重试范围是已失败章节)
"""
from __future__ import annotations

import asyncio
import json
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
# T-ST1 + T-ST3：multicast + STRICT 模式严格失败
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multicast_strict_single_chapter_fail_causes_build_failed(_isolate_data_dir):
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, get_build_status, _RUNNING_BUILDS, _RUNNING_LOCK
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build, BuildArtifact
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact

    # 开启严格模式 + TTS_PROVIDER=doubao
    # 注意：monkeypatch 必须在每个测试内部作用（因为有 autouse fixture 已生效）
    import asyncio as _aio
    cfgmod.settings.MULTICAST_STRICT_MODE = True

    # 用一个假的 TTS，第一次调用 synthesize_to_bytes 就抛错（Mock provider 通常不会错）
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    class _FailOnEvenChapter(MockTTSProvider):
        async def synthesize_to_bytes(self, text, voice_id, *, emotion="calm", speed=1.0, **kw):
            # 任何合成都失败（模拟豆包 Seed-Audio / TTS 异常）
            raise RuntimeError("模拟豆包 TTS 严格失败：第 2 章合成错误")

    # 替换全局单例（get_tts(None) 会读 _tts_instance）
    prev_tts = aifact._tts_instance
    prev_llm = aifact._llm_instance
    aifact._tts_instance = _FailOnEvenChapter()
    aifact._llm_instance = MockLLMProvider()
    try:
        await init_db()
        pid = (await create_project("严格模式测试")).project_id
        await import_file(pid, _BOOK_TXT.encode("utf-8"), "book.txt")
        await prepare_project(pid)

        resp = await start_build(
            project_id=pid,
            voice_assignments={},
            narrator_voice_id="minimax:male-qn-jingying",
            tts_provider="minimax",
            mode="multicast",  # 多播剧模式
        )
        assert resp.mode == "multicast"
        assert resp.tts_provider == "minimax"
        bid = resp.build_id

        # 等待 worker 完成（最多 60s）
        for _ in range(60):
            s = await get_build_status(bid)
            if s.status in ("success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
        else:
            # 强制释放
            async with _RUNNING_LOCK:
                _RUNNING_BUILDS.discard(pid)
            pytest.fail("build worker 超时未结束")

        factory = get_session_factory()
        async with factory() as sess:
            b = await sess.get(Build, bid)
            # 严格模式：任何章失败 → Build.status=failed
            assert b.status == "failed", (
                f"strict+multicast 下期望 failed，实际 status={b.status} msg={b.progress_msg}"
            )
            stmt = await sess.execute(
                __import__("sqlalchemy").select(BuildArtifact).where(
                    BuildArtifact.build_id == bid
                ).order_by(BuildArtifact.chapter_idx)
            )
            arts = list(stmt.scalars().all())
            failed_arts = [a for a in arts if a.status == "failed"]
            assert len(failed_arts) >= 1, "strict 模式下至少应有 1 章 status='failed'"
            # 失败章节不得生成占位 MP3（audio_filename 必须为空）
            for fa in failed_arts:
                assert fa.audio_filename is None or fa.audio_filename == "", (
                    f"strict+multicast 下失败章节不得生成占位 MP3: audio_filename={fa.audio_filename}"
                )
            # failed_chapters 需准确记录章节 idx
            failed_idxes = {a.chapter_idx for a in failed_arts}
            parsed_from_build = set(json.loads(b.failed_chapters_json or "[]"))
            assert parsed_from_build == failed_idxes
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        async with _RUNNING_LOCK:
            _RUNNING_BUILDS.discard(pid)


# ---------------------------------------------------------------------
# T-ST2：classic + 默认 NON-strict → 单章失败生成占位 MP3，Build 不被判定为 failed
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_classic_nonstrict_build_survives_chapter_failure(_isolate_data_dir):
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, get_build_status, _RUNNING_BUILDS, _RUNNING_LOCK
    from backend.app.db.session import init_db
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    cfgmod.settings.MULTICAST_STRICT_MODE = False

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
            narrator_voice_id="minimax:male-qn-jingying",
            tts_provider="minimax", mode="classic",
        )
        bid = resp.build_id
        for _ in range(60):
            s = await get_build_status(bid)
            if s.status in ("success", "partial_success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)
        else:
            async with _RUNNING_LOCK:
                _RUNNING_BUILDS.discard(pid)
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
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        async with _RUNNING_LOCK:
            _RUNNING_BUILDS.discard(pid)


# ---------------------------------------------------------------------
# T-ST5：strict 模式触发条件判断函数（便于单测覆盖）
# ---------------------------------------------------------------------
def test_build_should_raise_on_failure_helper():
    """`_should_strict_fail` 辅助函数：
    - mode=multicast + MULTICAST_STRICT_MODE=True → True
    - mode=multicast + MULTICAST_STRICT_MODE=False → False（用户允许降级）
    - mode=classic 任何情况 → False
    """
    from backend.app.services.build import _should_strict_fail
    from backend.app.core import config as cfgmod

    saved = cfgmod.settings.MULTICAST_STRICT_MODE
    try:
        cfgmod.settings.MULTICAST_STRICT_MODE = True
        assert _should_strict_fail("multicast") is True
        assert _should_strict_fail("classic") is False
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        assert _should_strict_fail("multicast") is False
        # 大小写不敏感
        cfgmod.settings.MULTICAST_STRICT_MODE = True
        assert _should_strict_fail("MULTICAST") is True
    finally:
        cfgmod.settings.MULTICAST_STRICT_MODE = saved
