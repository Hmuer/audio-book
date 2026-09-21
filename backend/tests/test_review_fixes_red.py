"""Review 修复回归 — 独立代码审查发现的问题（2026-09）。

T-RF1 retry-failed 必须继承源 Build 的 mode/tts_provider（原 Critical：
Build 重试会静默退化为 classic+MiniMax）。
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
# T-RF4：豆包 TTS 端点可被 settings 覆写
# ---------------------------------------------------------------------
def test_doubao_tts_endpoint_reads_settings(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    p = DoubaoTTSProvider()
    assert p._endpoint == DoubaoTTSProvider.DEFAULT_ENDPOINT  # 默认值

    # 改 settings 里的 DOUBAO_TTS_BASE_URL：通过 doubao_field() 兜底链路，provider 没设 tts_endpoint 时生效
    # 先把 doubao provider 的 tts_endpoint 字段清空（模拟"页面没设"），再改 settings
    from backend.app.core.config import save_providers_config, get_provider
    saved_settings = cfgmod.settings.DOUBAO_TTS_BASE_URL
    prov_orig = get_provider("doubao") or {}
    prov = dict(prov_orig)
    saved_tts_endpoint = prov.get("tts_endpoint")
    prov["tts_endpoint"] = ""  # 清空，强制走 settings 回退
    save_providers_config({"providers": [prov], "active": cfgmod._parse_providers_config().get("active", {"tts": {}, "llm": {}})})
    cfgmod.settings.DOUBAO_TTS_BASE_URL = "http://mock:9999/api/v1/tts"
    try:
        assert p._endpoint == "http://mock:9999/api/v1/tts"
    finally:
        cfgmod.settings.DOUBAO_TTS_BASE_URL = saved_settings
        # 恢复 provider 原值
        prov["tts_endpoint"] = saved_tts_endpoint
        save_providers_config({"providers": [prov], "active": cfgmod._parse_providers_config().get("active", {"tts": {}, "llm": {}})})


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

    reqids = [p["custom_speaker_id"] for p in captured]
    assert len(reqids) == 2 and reqids[0] != reqids[1], (
        f"ICL create custom_speaker_id 必须每次唯一，实际 {reqids}"
    )
    # speaker_id 是固定字面值（V3 后付费音色协议），唯一性由 custom_speaker_id 承担
    assert {p["speaker_id"] for p in captured} == {"custom_speaker_id"}


# ---------------------------------------------------------------------
# T-RF1：retry-failed 继承源 Build 的 mode/tts_provider
# ---------------------------------------------------------------------
_BOOK_TXT = """第一章 初遇
今天是美好的一天。阳光透过窗户洒进房间，林小雨伸了个懒腰。

第二章 启程
林小雨拿起背包，走出了家门。张伟在门口等她，微笑着说："我们出发吧。"
"""

@pytest.mark.asyncio
async def test_retry_failed_inherits_provider_and_mode(_isolate_data_dir):
    """#4 不降级契约：mode=multicast 在 start_build 入口直接抛错（不再降级）。
    本用例改用 mode='classic'（合法模式）验证 retry Build 继承源 Build 的
    mode + tts_provider 配置。"""
    from backend.app.services.project import create_project, import_file, prepare_project
    from backend.app.services.build import (
        start_build, get_build_status, retry_failed_build,
        _ACTIVE_BUILDS, _RUNNING_LOCK,
    )
    from backend.app.db.session import init_db, get_session_factory
    from backend.app.db.models import Build
    from backend.app.ai import factory as aifact
    from backend.tests.mock_providers import MockTTSProvider, MockLLMProvider

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

    prev_tts, prev_llm = aifact._tts_instance, aifact._llm_instance
    aifact._tts_instance = _FailFirstCallDoubaoTTS()
    aifact._llm_instance = MockLLMProvider()
    try:
        await init_db()
        pid = (await create_project("重试继承配置测试（#4 不降级）")).project_id
        await import_file(pid, _BOOK_TXT.encode("utf-8"), "book.txt")
        await prepare_project(pid)

        # 用户传 mode='classic' + tts_provider='doubao'（multicast 已改为直接抛错）
        resp = await start_build(
            project_id=pid, voice_assignments={},
            narrator_voice_id="doubao:BV001_streaming",
            tts_provider="doubao", mode="classic",
        )
        # resp.mode 保持用户传入值（不降级）
        assert resp.mode == "classic", (
            f"mode=classic 应原样保留，实际 {resp.mode!r}"
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

        # 触发 retry-failed：新 Build 必须继承源 Build 的 mode=classic + tts_provider=doubao
        retry_resp = await retry_failed_build(source_build_id=bid)
        factory = get_session_factory()
        async with factory() as sess:
            nb = await sess.get(Build, retry_resp.build_id)
            assert nb is not None
            assert (nb.mode or "classic").lower() == "classic", (
                f"retry Build 必须继承 mode=classic，实际 {nb.mode!r}"
            )
            assert (nb.tts_provider or "doubao").lower() == "doubao", (
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
        async with _RUNNING_LOCK:
            # 清掉可能残留的 (build_id -> pid) 键，避免污染后续测试
            stale_keys = [bid for bid, pidv in _ACTIVE_BUILDS.items() if pidv == pid]
            for k in stale_keys:
                _ACTIVE_BUILDS.pop(k, None)
