"""Review 修复回归 — 独立代码审查发现的问题（2026-09）。

T-RF1 retry-failed 必须继承源 Build 的 mode/tts_provider（原 Critical：
多播剧/豆包 Build 重试会静默退化为 classic+MiniMax）。
T-RF2 _validate_multicast_provider：multicast + 非 doubao → 抛 ValueError（禁止降级）。
T-RF3 _multicast_synth_chapter：预估超 120s 上限的章节分段生成再拼接。
T-RF4 DoubaoTTSProvider 端点读 settings.DOUBAO_TTS_BASE_URL（.env 覆写生效）。
T-RF5 ICL create reqid 每次唯一（不再用 id(self)）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-RF2（P0-5 改写）：_validate_multicast_provider 现在是 noop（不再拒绝降级）
# ---------------------------------------------------------------------
def test_validate_multicast_provider_is_noop_after_deprecation():
    from backend.app.services.build import _validate_multicast_provider

    # P0-5 之后：mode=multicast + tts_provider=任意 都不再抛错
    _validate_multicast_provider("multicast", "doubao")   # 历史合法
    _validate_multicast_provider("classic", "minimax")    # 历史合法
    _validate_multicast_provider("classic", "doubao")     # 历史合法
    _validate_multicast_provider("multicast", "minimax")  # 历史非法 → 现在不抛
    _validate_multicast_provider("MULTICAST", "")         # 历史非法 → 现在不抛
    # 函数返回 None
    assert _validate_multicast_provider("multicast", "doubao") is None


# ---------------------------------------------------------------------
# T-RF3（P0-5 改写）：_multicast_synth_chapter / _estimate_multicast_secs 已废弃
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multicast_synth_chapter_raises_after_deprecation(tmp_path):
    """P0-5 之后：_multicast_synth_chapter 调用立刻抛 RuntimeError（提示用户改 classic）。"""
    from backend.app.services.build import _multicast_synth_chapter

    class _RecordingMC:
        def __init__(self):
            self.calls: list[list[dict]] = []

        async def synthesize_chapter_to_file(self, segments, output_path, *, speed=1.0,
                                             chapter_title="", instruction_text=None):
            self.calls.append(list(segments))
            Path(output_path).write_bytes(b"\xff\xfb\x90\x64\x00" + (b"\x00" * 1024))
            return output_path, 1000

    mc = _RecordingMC()
    out = str(tmp_path / "ch0000.mp3")
    segs = [{"kind": "narration", "speaker": "", "text": "测试",
             "voice_id": "doubao:zh_female_qingxin", "silence_ms": 0}]
    with pytest.raises(RuntimeError, match="mode=multicast 已废弃"):
        await _multicast_synth_chapter(mc, segs, out, speed=1.0, max_secs=120.0)
    # mc 不应被调用
    assert mc.calls == []


def test_estimate_multicast_secs_returns_zero_after_deprecation():
    """P0-5 之后：_estimate_multicast_secs 永远返回 0（不再用于任何调度逻辑）。"""
    from backend.app.services.build import _estimate_multicast_secs

    segs = [{"kind": "narration", "speaker": "", "text": "字" * 500,
             "voice_id": "doubao:zh_female_qingxin", "silence_ms": 0} for _ in range(10)]
    assert _estimate_multicast_secs(segs, 1.0) == 0.0
    assert _estimate_multicast_secs(segs, 0.5) == 0.0


# ---------------------------------------------------------------------
# T-RF4：豆包 TTS 端点可被 settings 覆写
# ---------------------------------------------------------------------
def test_doubao_tts_endpoint_reads_settings(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    p = DoubaoTTSProvider()
    assert p._endpoint == DoubaoTTSProvider.DEFAULT_ENDPOINT  # 默认值

    # 直接覆写 settings 单例（不要走 monkeypatch 字符串路径，避免模块解析失败）
    saved = cfgmod.settings.DOUBAO_TTS_BASE_URL
    cfgmod.settings.DOUBAO_TTS_BASE_URL = "http://mock:9999/api/v1/tts"
    try:
        assert p._endpoint == "http://mock:9999/api/v1/tts"
    finally:
        cfgmod.settings.DOUBAO_TTS_BASE_URL = saved


# ---------------------------------------------------------------------
# T-RF5：ICL create reqid 唯一
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_icl_create_reqid_unique(monkeypatch):
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient

    captured: list[dict] = []

    async def _fake_post(url, payload, **_kwargs):
        captured.append(payload)
        return {"code": 0, "data": {"speaker_id": f"icl_88_{len(captured)}"}}

    monkeypatch.setattr(
        "backend.app.core.config.settings.DOUBAO_AK", "test-ak", raising=False
    )
    client = DoubaoICLClient()
    client._http_post_json = _fake_post  # type: ignore[method-assign]

    fake_mp3 = b"\xff\xfb\x90\x64\x00" + (b"\x00" * 2048)
    await client.create_training("声线A", fake_mp3)
    await client.create_training("声线B", fake_mp3)

    reqids = [p["speaker_id"] for p in captured]
    assert len(reqids) == 2 and reqids[0] != reqids[1], (
        f"ICL create speaker_id 必须每次唯一，实际 {reqids}"
    )


# ---------------------------------------------------------------------
# T-RF1：retry-failed 继承源 Build 的 mode/tts_provider
# ---------------------------------------------------------------------
_BOOK_TXT = """第一章 初遇
今天是美好的一天。阳光透过窗户洒进房间，林小雨伸了个懒腰。

第二章 启程
林小雨拿起背包，走出了家门。张伟在门口等她，微笑着说："我们出发吧。"
"""

@pytest.mark.asyncio
async def test_retry_failed_inherits_provider_and_downgraded_mode(_isolate_data_dir):
    """P0-5 改写：mode=multicast 在 start_build 入口自动降级为 classic，
    retry Build 必须继承降级后的 mode（classic）而不是源 Build 的入参 mode。"""
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import (
        start_build, get_build_status, retry_failed_build,
        _ACTIVE_BUILDS, _RUNNING_LOCK,
    )
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build
    from backend.app.core import config as cfgmod
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

    cfgmod.settings.MULTICAST_STRICT_MODE = True

    # mock TTS 标 provider='doubao'（让 factory.get_tts('doubao') 命中 mock），
    # 第一次合成失败触发 retry；后续正常 → 最终 success。
    class _FailFirstCallDoubaoTTS:
        provider = "doubao"
        name = "mock_tts_doubao_fail_first"

        def __init__(self):
            self._inner = MockTTSProvider()
            self._n = 0

        async def list_voices(self):
            return await self._inner.list_voices()

        async def synthesize_to_bytes(self, text, voice_id, *, emotion="calm", speed=1.0, **kw):
            self._n += 1
            if self._n == 1:
                raise RuntimeError("模拟首次合成失败")
            return await self._inner.synthesize_to_bytes(
                text, voice_id, emotion=emotion, speed=speed, **kw
            )

        async def synthesize_to_file(self, text, voice_id, output_path, *, emotion="calm",
                                     speed=1.0, instruction_text=None, speaker_style=None):
            return await self._inner.synthesize_to_file(
                text, voice_id, output_path, emotion=emotion, speed=speed,
                instruction_text=instruction_text, speaker_style=speaker_style,
            )

    prev_tts, prev_llm, prev_mc = aifact._tts_instance, aifact._llm_instance, aifact._multicast_instance
    aifact._tts_instance = _FailFirstCallDoubaoTTS()
    aifact._llm_instance = MockLLMProvider()
    try:
        await init_db()
        pid = (await create_project("重试继承配置测试（P0-5 降级）")).project_id
        await import_file(pid, _BOOK_TXT.encode("utf-8"), "book.txt")
        await prepare_project(pid)

        # 用户传 mode='multicast' + tts_provider='doubao'
        resp = await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="doubao:BV001_streaming",
            tts_provider="doubao", mode="multicast",
        )
        # resp.mode 已经是降级后的 classic
        assert resp.mode == "classic", (
            f"mode=multicast 必须降级为 classic，实际 {resp.mode!r}"
        )
        bid = resp.build_id
        for _ in range(60):
            s = await get_build_status(bid)
            if s.status in ("success", "partial_success", "failed", "cancelled"):
                break
            await asyncio.sleep(0.5)

        # 源 Build：mock 首次失败 → 至少有一个章节 failed
        status0 = (await get_build_status(bid)).status
        assert status0 in ("partial_success", "failed")

        # 触发 retry-failed：新 Build 必须继承降级后的 mode=classic + tts_provider=doubao
        retry_resp = await retry_failed_build(source_build_id=bid)
        factory = get_session_factory()
        async with factory() as sess:
            nb = await sess.get(Build, retry_resp.build_id)
            assert nb is not None
            assert (nb.mode or "classic").lower() == "classic", (
                f"retry Build 必须继承降级后的 mode=classic，实际 {nb.mode!r}"
            )
            assert (nb.tts_provider or "minimax").lower() == "doubao", (
                f"retry Build 必须继承 tts_provider=doubao，实际 {nb.tts_provider!r}"
            )
        # 等 retry worker 结束，避免污染后续测试
        for _ in range(60):
            s = await get_build_status(retry_resp.build_id)
            if s.status in ("success", "failed", "cancelled", "partial_success"):
                break
            await asyncio.sleep(0.5)
    finally:
        aifact._tts_instance = prev_tts
        aifact._llm_instance = prev_llm
        aifact._multicast_instance = prev_mc
        cfgmod.settings.MULTICAST_STRICT_MODE = False
        async with _RUNNING_LOCK:
            # 清掉可能残留的 (build_id -> pid) 键，避免污染后续测试
            stale_keys = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale_keys:
                _ACTIVE_BUILDS.pop(k, None)
