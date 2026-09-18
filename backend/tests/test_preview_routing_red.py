"""Review 回归 — /api/tts/preview 必须按音色 ID 前缀路由 TTS 厂商。

B1：preview 路由此前固定 get_tts()（全局默认 minimax），导致 doubao:/icl: 音色试听
必然失败（音色 id 原样传给 MiniMax API 报错）。修复后走 get_tts_by_voice_id。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeRequest:
    client = SimpleNamespace(host="test-client")


@pytest.mark.asyncio
async def test_preview_uses_voice_id_routing(monkeypatch):
    """preview 路由必须调用 get_tts_by_voice_id(voice_id) 并用其返回的 provider 合成。"""
    from backend.app.api import routes as routes_mod
    from backend.app.api.routes import TtsPreviewRequest
    from backend.tests.mock_providers import MockTTSProvider

    routed: list[str] = []

    def _fake_get_tts_by_voice_id(voice_id: str):
        routed.append(voice_id)
        return MockTTSProvider()

    monkeypatch.setattr(routes_mod, "get_tts_by_voice_id", _fake_get_tts_by_voice_id)

    req = TtsPreviewRequest(text="你好世界", voice_id="doubao:zh_female_qingxin", speed=1.0)
    resp = await routes_mod.api_tts_preview(req, _FakeRequest())

    assert routed == ["doubao:zh_female_qingxin"], (
        f"preview 必须按音色前缀路由，实际调用了 {routed}"
    )
    assert resp["audio_url"].startswith("/media/preview_")
    assert resp["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_preview_routes_doubao_and_icl_prefix_to_doubao_provider(monkeypatch):
    """doubao:/icl: 前缀在 factory 层路由到豆包 provider（v1 或 v3 均可；preview 依赖该行为）。"""
    from backend.app.ai.factory import get_tts_by_voice_id
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider, DoubaoTTSProviderV3
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")

    # 走哪一版协议由 settings.DOUBAO_TTS_USE_V3 决定（默认 v3），这里只关心
    # 「必须落到豆包 provider，而不是 minimax」。
    doubao_providers = (DoubaoTTSProvider, DoubaoTTSProviderV3)
    assert isinstance(get_tts_by_voice_id("doubao:zh_female_qingxin"), doubao_providers)
    assert isinstance(get_tts_by_voice_id("icl:clone_x"), doubao_providers)
