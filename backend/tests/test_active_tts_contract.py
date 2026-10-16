"""激活 TTS、官方音色和构建兜底音色的关键契约。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
async def test_minimax_lists_only_chinese_system_voices_and_synthesizes(monkeypatch):
    from backend.app.ai.providers.minimax import tts as module

    monkeypatch.setattr(module, "get_provider", lambda _id: {
        "api_key": "test-key", "base_url": "https://api.minimaxi.com/v1",
    })
    monkeypatch.setattr(module, "get_active", lambda _kind: ("minimax", "speech-2.8-hd", {}))
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/get_voice"):
            assert json.loads(request.content) == {"voice_type": "system"}
            return httpx.Response(200, json={"base_resp": {"status_code": 0}, "system_voice": [
                {"voice_id": "Chinese (Mandarin)_Reliable_Executive", "voice_name": "可靠主管", "description": ["中文"]},
                {"voice_id": "English_Test", "voice_name": "English", "description": ["English"]},
            ]})
        assert request.url.path.endswith("/t2a_v2")
        payload = json.loads(request.content)
        assert payload["model"] == "speech-2.8-hd"
        assert payload["voice_setting"]["voice_id"] == "male-qn-qingse"
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"audio": "494433"},
                                         "extra_info": {"audio_length": 750000}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    provider = module.MiniMaxTTSProvider()
    voices = await provider.list_voices()
    assert len(voices) >= 64
    assert all(v["provider"] == "minimax" and v["id"].startswith("minimax:") for v in voices)
    assert "minimax:English_Test" not in {v["id"] for v in voices}
    data, duration = await provider.synthesize_to_bytes("你好", "minimax:male-qn-qingse")
    assert data == b"ID3"
    assert duration == 750000
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_build_defaults_are_current_provider_and_different_from_narrator(monkeypatch):
    from backend.app.services import build
    voices = [
        {"id": "minimax:male-1", "provider": "minimax", "gender": "male"},
        {"id": "minimax:male-2", "provider": "minimax", "gender": "male"},
        {"id": "minimax:female-1", "provider": "minimax", "gender": "female"},
    ]
    monkeypatch.setattr(build, "get_tts", lambda _provider: SimpleNamespace(list_voices=lambda: _voices()))

    async def _voices():
        return voices

    narrator, male, female, allowed = await build._resolve_build_voice_defaults("minimax", "")
    assert (narrator, male, female) == ("minimax:male-1", "minimax:male-2", "minimax:female-1")
    assert allowed == {v["id"] for v in voices}
    with pytest.raises(ValueError, match="不属于"):
        await build._resolve_build_voice_defaults("minimax", "doubao:old")
    with pytest.raises(ValueError, match="旁白"):
        await build._resolve_build_voice_defaults("minimax", narrator, narrator)


@pytest.mark.asyncio
async def test_unknown_dialogue_uses_gender_defaults_and_rejects_other_provider():
    from backend.app.services import build

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

        def scalars(self):
            return self

    class Session:
        calls = 0

        async def execute(self, _query):
            self.calls += 1
            if self.calls == 1:
                return Result([("阿明", "男"), ("小雨", "女")])
            return Result(["阿明", "小雨", "路人", None])

    allowed = {"minimax:male-1", "minimax:male-2", "minimax:female-1"}
    result = await build._with_unknown_speaker_voices(
        Session(), "project", {"阿明": "minimax:male-2"},
        "minimax:male-1", "minimax:male-2", "minimax:female-1", allowed,
    )
    assert result["阿明"] == "minimax:male-2"
    assert result["小雨"] == "minimax:female-1"
    assert result["路人"] == result[""] == "minimax:male-2"
    with pytest.raises(ValueError, match="当前激活"):
        await build._with_unknown_speaker_voices(
            Session(), "project", {"阿明": "doubao:voice"},
            "minimax:male-1", "minimax:male-2", "minimax:female-1", allowed,
        )


def test_build_digest_includes_gender_defaults_even_without_unknown_dialogue():
    from backend.app.services.build import _calc_config_digest
    base = _calc_config_digest("minimax:narrator", 1.0, {}, default_male_voice_id="minimax:m1",
                               default_female_voice_id="minimax:f1")
    changed = _calc_config_digest("minimax:narrator", 1.0, {}, default_male_voice_id="minimax:m2",
                                  default_female_voice_id="minimax:f1")
    assert base != changed


@pytest.mark.asyncio
async def test_voice_api_lists_only_active_provider(monkeypatch):
    from backend.app.api import routes
    from backend.app.core import config
    from backend.app.ai import factory

    class Provider:
        def __init__(self, name):
            self.name = name

        async def list_voices(self):
            return [{"id": f"{self.name}:official", "provider": self.name, "gender": "male"}]

    monkeypatch.setattr(config, "get_active", lambda _kind: ("minimax", "speech-2.8-hd", {}))
    monkeypatch.setattr(factory, "get_tts", lambda name: Provider(name))
    response = await routes.list_voices()
    assert [v["id"] for v in response["voices"]] == ["minimax:official"]
    with pytest.raises(ValueError, match="当前激活"):
        await routes.list_voices("doubao")
