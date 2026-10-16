"""官方音色推荐池只接受当前 TTS 厂商及可合成的模型。"""
import pytest


def test_doubao_requires_system_tts_2_0():
    from backend.app.ai.providers.doubao.tts import is_voice_usable_on_v3
    assert is_voice_usable_on_v3({"id": "doubao:zh_voice", "provider": "doubao", "model": "seed-tts-2.0"})
    assert not is_voice_usable_on_v3({"id": "doubao:BV158", "provider": "doubao", "model": "seed-tts-1.0"})
    assert not is_voice_usable_on_v3({"id": "doubao:custom", "provider": "doubao"})
    assert is_voice_usable_on_v3({"id": "minimax:male-qn-qingse", "provider": "minimax"})


@pytest.mark.asyncio
async def test_recommendation_pool_follows_active_provider(monkeypatch):
    from backend.app.core import config
    from backend.app.services import voice_recommender as vr
    voices = {
        "doubao": [{"id": "doubao:official", "provider": "doubao", "model": "seed-tts-2.0"}],
        "minimax": [{"id": "minimax:male-qn-qingse", "provider": "minimax"}],
    }

    async def fake_fetch(provider):
        return voices[provider]

    monkeypatch.setattr(vr, "_fetch_provider_voices", fake_fetch)
    monkeypatch.setattr(config, "get_active", lambda _kind: ("minimax", "speech-2.8-hd", {}))
    assert [v["id"] for v in await vr._aggregate_voice_pool()] == ["minimax:male-qn-qingse"]
    monkeypatch.setattr(config, "get_active", lambda _kind: ("doubao", "volcano_tts", {}))
    assert [v["id"] for v in await vr._aggregate_voice_pool()] == ["doubao:official"]
