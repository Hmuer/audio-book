"""试听只能使用激活 TTS 的官方音色。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_preview_uses_active_provider_and_rejects_other_voice(monkeypatch, tmp_path):
    from backend.app.api import routes
    from backend.app.ai import factory
    from backend.app.core import config

    calls = []

    class Provider:
        async def list_voices(self):
            return [{"id": "minimax:male-qn-qingse", "provider": "minimax"}]

        async def synthesize_to_file(self, text, voice_id, path, **kwargs):
            calls.append((text, voice_id))
            Path(path).write_bytes(b"ID3")
            return path, 1500

    monkeypatch.setattr(config, "get_active", lambda _kind: ("minimax", "speech-2.8-hd", {}))
    monkeypatch.setattr(factory, "get_tts", lambda _name: Provider())
    monkeypatch.setattr(config.settings, "AUDIO_DIR", tmp_path)
    request = SimpleNamespace(client=SimpleNamespace(host="test"))
    user = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as error:
        await routes.api_tts_preview(routes.TtsPreviewRequest(
            text="你好", voice_id="doubao:zh_female_vv_uranus_bigtts", speed=1.0), request, user)
    assert error.value.status_code == 400
    response = await routes.api_tts_preview(routes.TtsPreviewRequest(
        text="你好", voice_id="minimax:male-qn-qingse", speed=1.0), request, user)
    assert response["duration_ms"] == 1500
    assert calls == [("你好", "minimax:male-qn-qingse")]
