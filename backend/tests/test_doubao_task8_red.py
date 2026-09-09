"""Task 8 RED — Build 管线严格失败模式（用户需求点 #4）。

P0-5 之后：mode=multicast 已自动降级为 classic，多播剧 strict 模式不再触发
（恒 `_should_strict_fail(mode) == False`）。strict 失败判定目前对任何
mode 都不生效；保留测试仅为覆盖未来 strict 重新启用时的回归。

T-ST1 (multicast + strict → 单章失败 → Build failed，不生成占位静音)
  （P0-5 改写：mode=multicast 降级为 classic，mock TTS 失败 → 走 partial_success
   不再走 strict failed）
T-ST2 (classic + 默认 non-strict → 单章失败 → 仍生成占位静音，Build 状态为 success 或 success_with_failures，至少不为 failed)
T-ST3 (multicast + strict → 章节失败时 BuildArtifact.status='failed'，且 audio_url/audio_filename 为空)
  （P0-5 改写：strict 不再触发；mock TTS 失败时 BuildArtifact 落 partial 状态 + 占位静音）
T-ST5 (strict 模式触发条件判断函数：恒返回 False，P0-5 后)
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
# T-ST1 + T-ST3（P0-5 改写）：mode=multicast 降级为 classic 后 strict 不再触发
# 改用 mock TTS 失败 → 走 partial_success + 占位静音 MP3（classic 默认行为）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multicast_mode_is_downgraded_then_partial_success(_isolate_data_dir):
    """P0-5 改写：用户传 mode=multicast + MULTICAST_STRICT_MODE=True →

    - resp.mode 自动降级为 classic
    - Build 走逐段 TTS（mock TTS 抛错）
    - 由于 strict 判定恒 False（mode=multicast 已降级），Build 不被严格失败，
      而是走 partial_success（占位静音 MP3 兜底）
    """
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, get_build_status, _ACTIVE_BUILDS, _RUNNING_LOCK
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build, BuildArtifact
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact

    cfgmod.settings.MULTICAST_STRICT_MODE = True

    # mock LLM 不参与章节切分（章节切分走正则，不走 LLM）
    from backend.tests.mock_providers import MockLLMProvider

    # mock TTS：第一章成功、第二章失败 → 验证 strict 不再触发，整包走 partial_success
    # 策略：monkeypatch build 模块的 _build_segments_for_chapter，让每章只返回
    # 一个简化 segment（直接用章节标题作 voice_id 文本）。然后 mock TTS 按
    # text 内容判定章节：含"启程"即第 2 章（失败），否则第 1 章（成功）。
    import backend.app.services.build as _build_mod

    class _PartialFailTTS:
        provider = "doubao"

        def __init__(self):
            self.calls: list = []

        async def list_voices(self):
            return []

        async def synthesize_to_bytes(self, text, voice_id, *, emotion="calm", speed=1.0, **kw):
            from backend.app.ai.providers.minimax.tts import make_silent_mp3
            dur_ms = max(200, int(len(text) * 200 / max(0.5, min(2.0, float(speed)))))
            self.calls.append({"text": text, "voice_id": voice_id})
            if "启程" in text or "远行" in text or "背包" in text or "我们出发" in text:
                raise RuntimeError(f"模拟第 2 章合成失败（partial_success 兜底测试）")
            return make_silent_mp3(dur_ms), dur_ms

    def _fake_build_segments_for_chapter(
        ch, dialogues, narrator_voice_id, voice_assignments,
        segment_overrides, start_idx, *,
        narrator_emotion="", narrator_instruction="", speaker_styles=None, **kwargs,
    ):
        """简化版 _build_segments_for_chapter：每章仅 1 个 narrator segment + 1 个
        尾部 silence，避免 segs 切分复杂导致 mock 难控制成败。"""
        from backend.app.services.chapter import _Segment, SILENCE_AFTER_TITLE_MS
        segs: list[_Segment] = []
        segs.append(_Segment(
            kind="narrator", chapter_idx=ch.idx, idx=start_idx,
            voice_id=narrator_voice_id, text=ch.text,
            emotion=narrator_emotion or "calm",
            instruction=narrator_instruction or "",
        ))
        segs.append(_Segment(
            kind="silence", chapter_idx=ch.idx, idx=start_idx + 1,
            silence_ms=SILENCE_AFTER_TITLE_MS,
        ))
        return segs, start_idx + 2

    prev_tts = aifact._tts_instance
    prev_llm = aifact._llm_instance
    prev_mc = aifact._multicast_instance
    prev_build_segments_fn = _build_mod._build_segments_for_chapter
    aifact._tts_instance = _PartialFailTTS()
    aifact._llm_instance = MockLLMProvider()
    aifact._multicast_instance = None  # 即便注入 sentinel 也不该被触发
    _build_mod._build_segments_for_chapter = _fake_build_segments_for_chapter
    try:
        await init_db()
        pid = (await create_project("multicast-降级-partial-success")).project_id
        await import_file(pid, _BOOK_TXT.encode("utf-8"), "book.txt")
        await prepare_project(pid)

        resp = await start_build(
            project_id=pid,
            voice_assignments={},
            narrator_voice_id="doubao:BV001_streaming",
            tts_provider="doubao",
            mode="multicast",  # 用户传 multicast，实际降级为 classic
        )
        # resp.mode 必须已降级
        assert resp.mode == "classic", (
            f"resp.mode 必须降级为 classic，实际 {resp.mode!r}"
        )
        assert resp.tts_provider == "doubao"
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

        factory = get_session_factory()
        async with factory() as sess:
            b = await sess.get(Build, bid)
            # P0-5：strict 不再触发 → partial_success（第 1 章成功 / 第 2 章失败 → 占位 MP3）
            assert b.status == "partial_success", (
                f"降级 + 第 2 章失败期望 partial_success，实际 {b.status} "
                f"msg={b.progress_msg!r}"
            )
            stmt = await sess.execute(
                __import__("sqlalchemy").select(BuildArtifact).where(
                    BuildArtifact.build_id == bid
                ).order_by(BuildArtifact.chapter_idx)
            )
            arts = list(stmt.scalars().all())
            assert len(arts) >= 2, f"期望至少 2 个章节产物，实际 {len(arts)}"
            # 第 1 章成功：status='done' 且有真实音频文件名
            first = next((a for a in arts if a.chapter_idx == 0), None)
            assert first is not None, "缺少第 1 章产物"
            assert first.status == "done", f"第 1 章应成功，实际 {first.status}"
            assert first.audio_filename, "第 1 章音频文件名应非空"
            # 第 2 章失败：status='failed' 但有占位 MP3（audio_filename 非空）
            second = next((a for a in arts if a.chapter_idx == 1), None)
            assert second is not None, "缺少第 2 章产物"
            assert second.status == "failed", f"第 2 章应失败，实际 {second.status}"
            assert second.audio_filename, (
                f"partial_success 下失败章节必须有占位 MP3，chapter={second.chapter_idx} "
                f"audio_filename={second.audio_filename!r}"
            )
            # failed_chapters 需准确记录章节 idx（仅第 2 章）
            parsed_from_build = set(json.loads(b.failed_chapters_json or "[]"))
            assert parsed_from_build == {1}, (
                f"failed_chapters 期望只包含第 2 章 idx=1，实际 {parsed_from_build}"
            )
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        aifact._multicast_instance = prev_mc
        _build_mod._build_segments_for_chapter = prev_build_segments_fn
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale:
                _ACTIVE_BUILDS.pop(k, None)


# ---------------------------------------------------------------------
# T-ST2：classic + 默认 NON-strict → 单章失败生成占位 MP3，Build 不被判定为 failed
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_classic_nonstrict_build_survives_chapter_failure(_isolate_data_dir):
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import start_build, get_build_status, _ACTIVE_BUILDS, _RUNNING_LOCK
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
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        async with _RUNNING_LOCK:
            stale = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale:
                _ACTIVE_BUILDS.pop(k, None)


# ---------------------------------------------------------------------
# T-ST5（P0-5 改写）：strict 模式触发条件恒返回 False
# ---------------------------------------------------------------------
def test_build_should_raise_on_failure_helper():
    """`_should_strict_fail` 辅助函数（P0-5 后语义）：

    - 任意 mode（multicast/classic）+ 任意 MULTICAST_STRICT_MODE 值 → 都返回 False
    - 多播剧端点已停止迭代，strict 失败判定当前对任何 mode 都不生效
    - 即便 MULTICAST_STRICT_MODE=True 也不会让任何章节触发严格失败
    """
    from backend.app.services.build import _should_strict_fail
    from backend.app.core import config as cfgmod

    saved = cfgmod.settings.MULTICAST_STRICT_MODE
    try:
        for flag in (True, False):
            cfgmod.settings.MULTICAST_STRICT_MODE = flag
            for mode in ("multicast", "classic", "MULTICAST", "", "anything"):
                assert _should_strict_fail(mode) is False, (
                    f"P0-5 后 _should_strict_fail 必须恒 False，"
                    f"mode={mode!r} strict={flag} 时仍返回 True"
                )
    finally:
        cfgmod.settings.MULTICAST_STRICT_MODE = saved
