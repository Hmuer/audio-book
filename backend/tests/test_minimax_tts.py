from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.app.ai import factory
from backend.app.ai.providers.minimax import tts
from backend.app.core import config


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(config.settings, "PROVIDERS_CONFIG", json.dumps({
        "providers": [{"id": "minimax", "enabled": True, "api_key": "test-key", "base_url": "https://example.test/v1"}],
        "active": {"tts": {"provider_id": "minimax", "model_id": "speech-2.8-hd"}},
    }))
    monkeypatch.setattr(tts, "_minimax_rpm_wait_acquire", AsyncMock())
    return tts.MiniMaxTTSProvider()


def response(usage=7, audio="494433", duration=1234):
    return {
        "base_resp": {"status_code": 0},
        "data": {"audio": audio, "status": 2},
        "extra_info": {"audio_length": duration, "usage_characters": usage, "word_count": 999},
        "trace_id": "trace-test",
    }


def install_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(tts.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
async def test_official_request_and_tuple_usage(provider, monkeypatch):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response())

    install_http(monkeypatch, handler)
    result = await provider.synthesize_to_bytes(
        "你好。", "minimax:male-qn-qingse", emotion="happy", speed=1.2,
        instruction_text="不要发送", speaker_style="不要发送",
    )
    assert result == (b"ID3", 1234)
    request = captured[0]
    assert str(request.url) == "https://example.test/v1/t2a_v2"
    assert request.headers["Authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body == {
        "model": "speech-2.8-hd", "text": "你好。", "stream": False,
        "language_boost": "Chinese", "output_format": "hex",
        "voice_setting": {"voice_id": "male-qn-qingse", "speed": 1.2, "vol": 1, "pitch": 0, "emotion": "happy"},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
    }
    assert provider.supports_instruction is False
    assert provider.consume_usage() == {"provider": "minimax", "model": "speech-2.8-hd", "usage_characters": 7, "trace_id": "trace-test"}
    assert provider.consume_usage() is None


@pytest.mark.asyncio
async def test_usage_is_call_local_concurrent_and_missing(provider, monkeypatch):
    arrived = 0
    ready = asyncio.Event()

    async def post(url, headers, body):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return response(usage=int(body["text"]))

    monkeypatch.setattr(provider, "_post", post)

    async def run(value):
        await provider.synthesize_to_bytes(str(value), "male-qn-qingse")
        await asyncio.sleep(0)
        usage = provider.get_last_usage()
        usage["usage_characters"] = 888
        return provider.consume_usage()["usage_characters"]

    assert await asyncio.gather(run(10), run(20)) == [10, 20]
    assert provider.get_last_usage() is None
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value=response(usage=0)))
    await provider.synthesize_to_bytes("你好", "male-qn-qingse")
    assert provider.get_last_usage()["usage_characters"] == 0
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value=response(usage=None)))
    await provider.synthesize_to_bytes("你好", "male-qn-qingse")
    assert provider.get_last_usage() is None


@pytest.mark.asyncio
async def test_failed_call_clears_previous_usage(provider, monkeypatch):
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value=response()))
    await provider.synthesize_to_bytes("你好", "male-qn-qingse")
    monkeypatch.setattr(provider, "_post", AsyncMock(side_effect=RuntimeError("failed")))
    with pytest.raises(RuntimeError, match="failed"):
        await provider.synthesize_to_bytes("你好", "male-qn-qingse")
    assert provider.get_last_usage() is None


@pytest.mark.asyncio
async def test_official_58_mandarin_voices_and_get_voice_filter(provider, monkeypatch):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={
            "base_resp": {"status_code": 0},
            "system_voice": [
                {"voice_id": "male-qn-qingse", "voice_name": "青涩青年音色", "description": ["标准普通话"]},
                {"voice_id": "English_Trustworthy_Man", "description": ["中文描述不能证明音色是普通话"]},
                {"voice_id": "Cantonese_GentleLady"},
            ],
            "voice_cloning": [{"voice_id": "unknown"}],
        })

    install_http(monkeypatch, handler)
    voices = await provider.list_voices()
    assert len(voices) == 58
    ids = {v["id"] for v in voices}
    assert len(ids) == 58
    assert "minimax:Chinese (Mandarin)_Soft_Girl" in ids
    assert "minimax:Arrogant_Miss" in ids
    assert "minimax:Robot_Armor" in ids
    assert not any("English" in value or "Cantonese" in value for value in ids)
    assert all(v["languages"] == ["zh"] and v["supports_instruction"] is False for v in voices)
    assert voices[0]["description"] == "标准普通话"
    assert str(captured[0].url).endswith("/v1/get_voice")
    assert json.loads(captured[0].content) == {"voice_type": "system"}


@pytest.mark.asyncio
async def test_file_return_and_usage(provider, monkeypatch, tmp_path):
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value=response()))
    path = str(tmp_path / "audio" / "test.mp3")
    assert await provider.synthesize_to_file("你好", "male-qn-qingse", path) == (path, 1234)
    assert (tmp_path / "audio" / "test.mp3").read_bytes() == b"ID3"
    assert provider.consume_usage()["usage_characters"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"text": ""}, {"text": "字" * 10000}, {"voice_id": "doubao:abc"},
    {"voice_id": ""}, {"emotion": "invalid"}, {"speed": 0.4}, {"speed": 2.1}, {"speed": float("nan")},
])
async def test_invalid_input_does_not_request(provider, monkeypatch, kwargs):
    post = AsyncMock()
    monkeypatch.setattr(provider, "_post", post)
    with pytest.raises(ValueError):
        await provider.synthesize_to_bytes(**({"text": "你好", "voice_id": "male-qn-qingse"} | kwargs))
    post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("audio,duration", [("zz", 1), ("", 1), ("4944", None), ("4944", -1)])
async def test_invalid_audio_response(provider, monkeypatch, audio, duration):
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value=response(audio=audio, duration=duration)))
    with pytest.raises(RuntimeError):
        await provider.synthesize_to_bytes("你好", "male-qn-qingse")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,count", [(429, 0, 2), (500, 0, 2), (200, 1002, 2), (401, 0, 1), (200, 1004, 1)])
async def test_http_and_business_errors(provider, monkeypatch, status, code, count):
    calls = []

    def handler(request):
        calls.append(request)
        payload = response()
        if len(calls) == 1:
            payload["base_resp"]["status_code"] = code
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json=payload)

    install_http(monkeypatch, handler)
    monkeypatch.setattr(tts.asyncio, "sleep", AsyncMock())
    if count == 1:
        with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
            await provider.synthesize_to_bytes("你好", "male-qn-qingse")
    else:
        assert await provider.synthesize_to_bytes("你好", "male-qn-qingse") == (b"ID3", 1234)
    assert len(calls) == count
    assert tts._minimax_rpm_wait_acquire.await_count == count


def test_factory_active_default_prefix_cache(provider, monkeypatch):
    monkeypatch.setattr(factory, "_tts_instance", None)
    assert factory.get_tts().provider == "minimax"
    assert factory.get_tts_by_voice_id("minimax:male-qn-qingse") is factory.get_tts()
    assert factory.get_tts("minimax:male-qn-qingse") is factory.get_tts()
    assert factory.get_tts_by_voice_id("doubao:abc").provider == "doubao"
    assert factory.get_tts_by_voice_id("bare") is factory.get_tts()
    assert set(factory.TTSRegistry) == {"minimax", "doubao"}
    previous = factory.get_tts()
    cfg = json.loads(config.settings.PROVIDERS_CONFIG)
    cfg["providers"][0]["api_key"] = "new-key"
    monkeypatch.setattr(config.settings, "PROVIDERS_CONFIG", json.dumps(cfg))
    assert factory.get_tts() is not previous


def test_existing_key_fallback_and_active_model(provider, monkeypatch):
    monkeypatch.setattr(config.settings, "LLM_API_KEY", "Bearer legacy-key")
    monkeypatch.setattr(config.settings, "LLM_BASE_URL", "https://legacy.test")
    monkeypatch.setattr(config.settings, "PROVIDERS_CONFIG", "{}")
    base, headers = provider._api_config()
    assert base == "https://legacy.test/v1"
    assert headers["Authorization"] == "Bearer legacy-key"
    assert provider._model() == "speech-2.8-hd"
    monkeypatch.setattr(config.settings, "ACTIVE_TTS_PROVIDER", "minimax")
    monkeypatch.setattr(config.settings, "ACTIVE_TTS_MODEL", "speech-2.8-turbo")
    assert provider._model() == "speech-2.8-turbo"


@pytest.mark.asyncio
async def test_independent_rpm_and_removed_icl(monkeypatch):
    monkeypatch.setattr(factory, "_minimax_tts_bucket", factory._RPMBucket(lambda: 30))
    monkeypatch.setattr(factory, "_doubao_tts_bucket", factory._RPMBucket(lambda: 60))
    monkeypatch.setattr(factory._time, "monotonic", lambda: 100.0)
    sleep = AsyncMock()
    monkeypatch.setattr(factory.asyncio, "sleep", sleep)
    await factory._minimax_rpm_wait_acquire()
    await factory._doubao_rpm_wait_acquire()
    sleep.assert_not_awaited()
    await factory._minimax_rpm_wait_acquire()
    sleep.assert_awaited_once_with(2.0)
    assert not hasattr(factory, "_doubao_icl_bucket")
    assert not any("ICL" in name for name in config.Settings.model_fields)
    assert not any("icl" in name for p in config.DEFAULT_PROVIDERS_TEMPLATE for name in p)


@pytest.mark.asyncio
async def test_builtin_voices_without_key(provider, monkeypatch):
    monkeypatch.setattr(config.settings, "PROVIDERS_CONFIG", "{}")
    monkeypatch.setattr(config.settings, "LLM_API_KEY", "")
    post = AsyncMock()
    monkeypatch.setattr(provider, "_post", post)
    assert len(await provider.list_voices()) == 58
    post.assert_not_awaited()


def test_saved_icl_fields_are_removed(monkeypatch, tmp_path):
    cfg = {
        "providers": [{"id": "doubao", "api_key": "test-key", "icl_api_key": "old", "icl_access_key": "old", "icl_endpoint": "/old"}],
        "active": {"tts": {"provider_id": "doubao", "model_id": "volcano_tts"}},
    }
    monkeypatch.setattr(config.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config.settings, "PROVIDERS_CONFIG", json.dumps(cfg))
    assert not any(key.startswith("icl_") for key in config.get_provider("doubao"))
    config.save_providers_config(cfg)
    saved = json.loads((tmp_path / "providers_config.json").read_text())
    assert not any(key.startswith("icl_") for key in saved["providers"][0])
    assert "icl_api_key" in cfg["providers"][0]


def test_legacy_migration_respects_active_tts(monkeypatch):
    monkeypatch.setattr(config.settings, "PROVIDERS_CONFIG", "")
    monkeypatch.setattr(config.settings, "ACTIVE_TTS_PROVIDER", "minimax")
    monkeypatch.setattr(config.settings, "ACTIVE_TTS_MODEL", "speech-2.8-hd")
    config._migrate_legacy_providers()
    assert config.get_active("tts")[:2] == ("minimax", "speech-2.8-hd")
    assert any(m["id"] == "speech-2.8-hd" for m in config.get_provider("minimax")["models"])


@pytest.mark.asyncio
@pytest.mark.parametrize("emotion,expected", [
    ("", "calm"), ("   ", "calm"), (None, "calm"),
    ("neutral", "calm"), ("gentle", "calm"), ("cold", "calm"),
    (" HAPPY ", "happy"), ("sad", "sad"), ("angry", "angry"),
    ("fearful", "fearful"), ("disgusted", "disgusted"), ("surprised", "surprised"),
])
async def test_project_emotion_mapping(provider, monkeypatch, emotion, expected):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    install_http(monkeypatch, handler)
    assert await provider.synthesize_to_bytes("你好", "male-qn-qingse", emotion=emotion) == (b"ID3", 1234)
    assert captured[0]["voice_setting"]["emotion"] == expected


@pytest.mark.asyncio
async def test_remote_mandarin_voices_are_added_without_invented_metadata(provider, monkeypatch):
    remote = [
        {"voice_id": "Chinese (Mandarin)_New_Voice", "voice_name": "全新音色"},
        {"voice_id": "new_zh", "description": ["中文", "清晰"]},
        {"voice_id": "new_mandarin", "description": "标准普通话"},
        {"voice_id": "new_language", "language": "Chinese Mandarin", "gender": "female"},
        {"voice_id": "new_named", "voice_name": "中文新音色", "gender": "unknown"},
        {"voice_id": "new_tags", "languages": ["zh-CN"]},
        {"voice_id": "Chinese (Mandarin)_New_Voice", "description": ["更新描述"]},
        {"voice_id": "Chinese (Mandarin)_News_Anchor", "voice_name": "官方主播", "gender": "男"},
        {"voice_id": "English_New", "description": ["普通话风格中文描述"]},
        {"voice_id": "Cantonese_New", "description": ["中文"]},
        {"voice_id": "Japanese_New", "description": ["Chinese Mandarin"]},
        {"voice_id": "unclassified", "voice_name": "温暖男声"},
        {"voice_id": "explicit_foreign", "language": "English", "description": ["普通话"]},
        {"voice_id": 123, "description": ["中文"]},
        {}, None,
    ]
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value={"system_voice": remote, "voice_cloning": [{"voice_id": "Chinese (Mandarin)_Clone"}]}))
    voices = await provider.list_voices()
    by_id = {v["id"]: v for v in voices}
    assert len(voices) == len(by_id) == 64
    for voice_id in ("Chinese (Mandarin)_New_Voice", "new_zh", "new_mandarin", "new_named", "new_tags"):
        assert by_id[f"minimax:{voice_id}"]["gender"] == ""
        assert by_id[f"minimax:{voice_id}"]["age"] == ""
    assert by_id["minimax:new_language"]["gender"] == "female"
    assert by_id["minimax:Chinese (Mandarin)_News_Anchor"]["gender"] == "male"
    assert by_id["minimax:Chinese (Mandarin)_New_Voice"]["description"] == "更新描述"
    assert by_id["minimax:new_mandarin"]["description"] == "标准普通话"
    assert all(v["provider"] == "minimax" and v["languages"] == ["zh"] for v in voices)
    assert len(provider._builtin_voices_sync()) == 58


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    httpx.ConnectError("unavailable"), httpx.ReadTimeout("timeout"),
    RuntimeError("business error"), ValueError("invalid JSON"), TypeError("invalid payload"),
])
async def test_remote_voice_failure_falls_back_to_58(provider, monkeypatch, error):
    monkeypatch.setattr(provider, "_post", AsyncMock(side_effect=error))
    assert await provider.list_voices() == provider._builtin_voices_sync()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [None, {}, "invalid"])
async def test_malformed_remote_voice_list_falls_back(provider, monkeypatch, remote):
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value={"system_voice": remote}))
    assert await provider.list_voices() == provider._builtin_voices_sync()


def test_builtin_metadata_requires_evidence(provider):
    voices = {v["id"].split(":", 1)[1]: v for v in provider._builtin_voices_sync()}
    assert len(voices) == 58
    for voice_id in (
        "cartoon_pig", "Robot_Armor", "tianxin_xiaoling",
        "Chinese (Mandarin)_Reliable_Executive", "Chinese (Mandarin)_News_Anchor",
        "Chinese (Mandarin)_Warm_Bestie", "Chinese (Mandarin)_Cute_Spirit",
        "Chinese (Mandarin)_Radio_Host", "Chinese (Mandarin)_Lyrical_Voice",
        "Chinese (Mandarin)_Sincere_Adult", "Chinese (Mandarin)_Stubborn_Friend",
        "Chinese (Mandarin)_HK_Flight_Attendant",
    ):
        assert voices[voice_id]["gender"] == ""
        assert voices[voice_id]["age"] == ""
    for voice_id in ("Chinese (Mandarin)_Humorous_Elder", "Chinese (Mandarin)_Kind-hearted_Elder", "Chinese (Mandarin)_Gentle_Senior"):
        assert voices[voice_id]["gender"] == ""
        assert voices[voice_id]["age"] == "senior"
    assert voices["female-tianmei"]["age"] == ""
    assert voices["male-qn-qingse"]["gender"] == "male"
    assert voices["clever_boy"]["age"] == "child"


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, -1, True, "7", 1.5, 0, 7])
async def test_build_usage_never_estimates_missing_minimax_bill(provider, monkeypatch, usage):
    from backend.app.services.build import _synthesize_with_usage, tts_usage_scope

    payload = response(usage=usage)
    if usage is None:
        payload["extra_info"].pop("usage_characters")
    monkeypatch.setattr(provider, "_post", AsyncMock(return_value=payload))
    with tts_usage_scope() as totals:
        result = await _synthesize_with_usage(provider, "这段文本不能作为账单字符数", "minimax:male-qn-qingse", asyncio.Semaphore(1), emotion="")
    assert result == (b"ID3", 1234)
    billed = usage if type(usage) is int and usage >= 0 else 0
    assert (totals["calls"], totals["chars"]) == (1, billed)
    assert provider.consume_usage() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, 7])
async def test_worker_real_minimax_handles_dialogue_empty_emotion(provider, monkeypatch, usage):
    from sqlalchemy import select

    from backend.app.db.models import Build, UsageEvent
    from backend.app.db.session import get_session_factory
    from backend.app.services import build as build_mod
    from backend.app.services.chapter import _build_segments_for_chapter
    from backend.tests.mock_providers import make_silent_mp3
    from backend.tests.test_unknown_speaker_voice_red import _seed_prepared_project

    pid = await _seed_prepared_project()
    monkeypatch.setattr(factory, "_tts_instance", None)
    captured = []
    segments = []
    original = _build_segments_for_chapter

    def assemble(*args, **kwargs):
        result = original(*args, **kwargs)
        segments.extend(result[0])
        return result

    def handler(request):
        captured.append(json.loads(request.content))
        payload = response(usage=usage, audio=make_silent_mp3(200).hex(), duration=200)
        if usage is None:
            payload["extra_info"].pop("usage_characters")
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(build_mod, "_build_segments_for_chapter", assemble)
    install_http(monkeypatch, handler)
    started = await build_mod.start_build(pid, {}, "minimax:male-qn-qingse", narrator_emotion="gentle")
    async def wait_for_worker():
        while started.build_id in build_mod._ACTIVE_BUILDS:
            await asyncio.sleep(0.025)

    await asyncio.wait_for(wait_for_worker(), timeout=15)
    dialogue_segments = [s for s in segments if s.kind == "dialogue"]
    assert dialogue_segments and all(s.emotion == "" for s in dialogue_segments)
    assert all(any(body["text"] == s.text for body in captured) for s in dialogue_segments)
    assert captured and all(body["voice_setting"]["emotion"] == "calm" for body in captured)
    async with get_session_factory()() as session:
        row = await session.get(Build, started.build_id)
        assert row.status == "success"
        assert row.tts_calls == len(captured)
        assert row.tts_chars == len(captured) * (usage or 0)
        events = list((await session.execute(select(UsageEvent).where(UsageEvent.project_id == pid))).scalars())
    assert sum(e.chars for e in events) == len(captured) * (usage or 0)
    assert sum(e.calls for e in events) == len(captured)
