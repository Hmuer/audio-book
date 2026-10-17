"""激活 TTS、官方音色和构建兜底音色的关键契约。"""
from __future__ import annotations

import asyncio
import json
import uuid
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
async def test_minimax_rpm_response_retries_after_cooldown(monkeypatch):
    from backend.app.ai.providers.minimax import tts as module

    monkeypatch.setattr(module, "get_provider", lambda _id: {
        "api_key": "test-key", "base_url": "https://api.minimax.cn/v1",
    })
    monkeypatch.setattr(module, "get_active", lambda _kind: ("minimax", "speech-2.8-hd", {}))
    monkeypatch.setattr(module.settings, "MINIMAX_TTS_RPM_LIMIT", 600)
    provider = module.MiniMaxTTSProvider()
    provider._voices = [{"id": "minimax:male-qn-qingse"}]
    provider._voices_at = module.time.monotonic()
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, json={"base_resp": {
                "status_code": 1001, "status_msg": "rate limit exceeded(RPM)",
            }})
        return httpx.Response(200, json={"base_resp": {"status_code": 0},
                                         "data": {"audio": "494433"},
                                         "extra_info": {"audio_length": 1000}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    waits = []
    original_acquire = module._tts_pacer.acquire

    async def fake_acquire():
        waits.append(True)
        if len(waits) > 1:
            module._tts_pacer.reset()
        else:
            await original_acquire()

    monkeypatch.setattr(module._tts_pacer, "acquire", fake_acquire)
    audio, duration = await provider.synthesize_to_bytes("你好", "minimax:male-qn-qingse")
    assert (audio, duration) == (b"ID3", 1000)
    assert len(seen) == 2 and len(waits) == 2


@pytest.mark.asyncio
async def test_minimax_rpm_failure_cools_down_other_requests_after_last_attempt(monkeypatch):
    from backend.app.ai.providers.minimax import tts as module

    monkeypatch.setattr(module, "get_provider", lambda _id: {"api_key": "test-key"})
    monkeypatch.setattr(module, "get_active", lambda _kind: ("minimax", "speech-2.8-hd", {}))
    provider = module.MiniMaxTTSProvider()
    provider._voices = [{"id": "minimax:male-qn-qingse"}]
    provider._voices_at = module.time.monotonic()

    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
            "base_resp": {"status_code": 1001, "status_msg": "rate limit exceeded(RPM)"},
        })), **kwargs,
    ))

    async def no_wait():
        pass

    cooldowns = []
    monkeypatch.setattr(module._tts_pacer, "acquire", no_wait)
    monkeypatch.setattr(module._tts_pacer, "cooldown", lambda: cooldowns.append(True))
    with pytest.raises(RuntimeError, match="rate limit exceeded"):
        await provider.synthesize_to_bytes("你好", "minimax:male-qn-qingse")
    assert len(cooldowns) == 3


@pytest.mark.asyncio
async def test_minimax_http_429_with_retry_after_returns_to_build_without_long_provider_sleep(monkeypatch):
    from backend.app.ai.providers.minimax import tts as module

    monkeypatch.setattr(module, "get_provider", lambda _id: {"api_key": "test-key"})
    monkeypatch.setattr(module, "get_active", lambda _kind: ("minimax", "speech-2.8-hd", {}))
    provider = module.MiniMaxTTSProvider()
    provider._voices = [{"id": "minimax:male-qn-qingse"}]
    provider._voices_at = module.time.monotonic()
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "18000"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    with pytest.raises(module.MiniMaxTTSRateLimitError) as exc:
        await provider.synthesize_to_bytes("你好", "minimax:male-qn-qingse")
    assert exc.value.retry_after == 18000
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_build_quota_wait_exposes_progress_and_honors_cancel(_isolate_data_dir, monkeypatch):
    from backend.app.db.models import Build, Project
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services import build as module

    await init_db()
    project_id, build_id = uuid.uuid4().hex, uuid.uuid4().hex
    factory = get_session_factory()
    async with factory() as session:
        session.add(Project(project_id=project_id, name="额度等待", status="ready"))
        session.add(Build(
            build_id=build_id, project_id=project_id, status="running",
            total_chapters=1, completed_chapters=0, narrator_voice_id="minimax:male-1",
            mode="classic", tts_provider="minimax", speed=1.0,
        ))
        await session.commit()

    async def fake_sleep(_seconds):
        async with factory() as session:
            row = await session.get(Build, build_id)
            assert "MiniMax TTS 限额等待" in row.progress_msg
            row.status = "cancelled"
            await session.commit()

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    assert not await module._wait_for_minimax_quota(
        build_id, 1, 1, 1, None, module._time.monotonic(),
    )


@pytest.mark.asyncio
async def test_build_stops_other_segments_when_one_is_rate_limited():
    from backend.app.ai.providers.minimax.tts import MiniMaxTTSRateLimitError
    from backend.app.services.build import _gather_cancel_on_error

    stopped = asyncio.Event()

    async def blocked_segment():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def limited_segment():
        await asyncio.sleep(0)
        raise MiniMaxTTSRateLimitError("rate limit exceeded")

    with pytest.raises(MiniMaxTTSRateLimitError):
        await _gather_cancel_on_error([blocked_segment(), limited_segment()])
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_minimax_build_retries_same_chapter_without_placeholder(_isolate_data_dir, monkeypatch):
    from sqlalchemy import select
    from backend.app.ai.providers.minimax.tts import MiniMaxTTSRateLimitError
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.db.models import Build, BuildArtifact, Project, ProjectChapter
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services import build as module

    await init_db()
    project_id, build_id = uuid.uuid4().hex, uuid.uuid4().hex
    factory = get_session_factory()
    async with factory() as session:
        session.add(Project(project_id=project_id, name="限额恢复", status="ready"))
        for idx in range(2):
            session.add(ProjectChapter(
                project_id=project_id, idx=idx, title=f"第{idx + 1}章",
                text=f"正文{idx + 1}。", text_len=4,
            ))
            session.add(BuildArtifact(
                build_id=build_id, chapter_idx=idx, title=f"第{idx + 1}章", status="pending",
            ))
        session.add(Build(
            build_id=build_id, project_id=project_id, status="queued",
            total_chapters=2, completed_chapters=0, narrator_voice_id="minimax:male-1",
            mode="classic", tts_provider="minimax", tts_model_id="speech-2.8-hd",
            speed=1.0,
        ))
        await session.commit()

    calls = 0
    async def synthesize(_text, _voice_id, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MiniMaxTTSRateLimitError("rate limit exceeded(RPM)")
        return make_silent_mp3(150, sample_rate=32000), 150

    waits = []
    async def fake_wait(*_args):
        waits.append(True)
        return True

    async def no_archive(*_args):
        return False

    monkeypatch.setattr(module, "get_tts", lambda _provider: SimpleNamespace(synthesize_to_bytes=synthesize))
    monkeypatch.setattr(module, "_wait_for_minimax_quota", fake_wait)
    monkeypatch.setattr(module, "_archive_file", no_archive)
    await module._run_build_inner(build_id, project_id, {}, "minimax:male-1")

    async with factory() as session:
        row = await session.get(Build, build_id)
        arts = (await session.execute(select(BuildArtifact).where(
            BuildArtifact.build_id == build_id,
        ).order_by(BuildArtifact.chapter_idx))).scalars().all()
        assert row.status == "success"
        assert [art.status for art in arts] == ["done", "done"]
        assert all(art.audio_filename and "failed" not in art.audio_filename for art in arts)
    assert waits == [True]
    assert calls >= 3


@pytest.mark.asyncio
async def test_minimax_quota_timeout_stops_build_and_keeps_retry_targets(_isolate_data_dir, monkeypatch):
    from sqlalchemy import select
    from backend.app.ai.providers.minimax.tts import MiniMaxTTSRateLimitError
    from backend.app.db.models import Build, BuildArtifact, Project, ProjectChapter
    from backend.app.db.session import get_session_factory, init_db
    from backend.app.services import build as module

    await init_db()
    project_id, build_id = uuid.uuid4().hex, uuid.uuid4().hex
    factory = get_session_factory()
    async with factory() as session:
        session.add(Project(project_id=project_id, name="限额超时", status="ready"))
        for idx in range(2):
            session.add(ProjectChapter(
                project_id=project_id, idx=idx, title=f"第{idx + 1}章",
                text=f"正文{idx + 1}。", text_len=4,
            ))
            session.add(BuildArtifact(
                build_id=build_id, chapter_idx=idx, title=f"第{idx + 1}章", status="pending",
            ))
        session.add(Build(
            build_id=build_id, project_id=project_id, status="queued",
            total_chapters=2, completed_chapters=0, narrator_voice_id="minimax:male-2",
            mode="classic", tts_provider="minimax", tts_model_id="speech-2.8-hd",
            speed=1.0,
        ))
        await session.commit()

    async def always_limited(*_args, **_kwargs):
        raise MiniMaxTTSRateLimitError("rate limit exceeded(RPM)")

    async def timeout(*_args):
        raise module._MiniMaxQuotaWaitExceeded("限额等待超时")

    monkeypatch.setattr(module, "get_tts", lambda _provider: SimpleNamespace(
        synthesize_to_bytes=always_limited,
    ))
    monkeypatch.setattr(module, "_wait_for_minimax_quota", timeout)
    with pytest.raises(module._MiniMaxQuotaWaitExceeded):
        await module._run_build_inner(build_id, project_id, {}, "minimax:male-2")

    async with factory() as session:
        row = await session.get(Build, build_id)
        first = (await session.execute(select(BuildArtifact).where(
            BuildArtifact.build_id == build_id, BuildArtifact.chapter_idx == 0,
        ))).scalar_one()
        assert json.loads(row.failed_chapters_json) == [0, 1]
        assert first.status == "failed" and first.audio_filename is None


@pytest.mark.asyncio
async def test_minimax_pacer_spreads_concurrent_requests(monkeypatch):
    from backend.app.ai.providers.minimax import tts as module

    monkeypatch.setattr(module.settings, "MINIMAX_TTS_RPM_LIMIT", 30)
    pacer = module._MiniMaxTTSPacer()
    clock = [100.0]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    await pacer.acquire()
    await pacer.acquire()
    await pacer.acquire()
    assert sleeps == [2.0, 2.0]
    pacer.cooldown(seconds=0)
    assert pacer._adaptive_rpm == 15


def test_minimax_pacer_recovers_after_quota_is_available(monkeypatch):
    from backend.app.ai.providers.minimax import tts as module

    monkeypatch.setattr(module.settings, "MINIMAX_TTS_RPM_LIMIT", 10)
    pacer = module._MiniMaxTTSPacer()
    pacer.cooldown(seconds=0)
    pacer.cooldown(seconds=0)
    pacer.cooldown(seconds=0)
    assert pacer._adaptive_rpm == 1
    for _ in range(27):
        pacer.note_success()
    assert pacer._adaptive_rpm is None


@pytest.mark.asyncio
async def test_minimax_pacer_honors_cooldown_started_while_waiting(monkeypatch):
    from backend.app.ai.providers.minimax import tts as module

    monkeypatch.setattr(module.settings, "MINIMAX_TTS_RPM_LIMIT", 10)
    pacer = module._MiniMaxTTSPacer()
    clock = [100.0]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        if len(sleeps) == 1:
            pacer.cooldown(seconds=65)

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    await pacer.acquire()
    await pacer.acquire()
    assert sleeps == [6.0, 65.0]
    assert pacer._next_allowed == 183.0  # 171 + 60 / 5 RPM


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
