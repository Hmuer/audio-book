"""P2-2：豆包长文本异步 TTS 客户端测试。

覆盖：
  T-AT1 阈值判断：should_use_async_tts（默认 3 万字符）
  T-AT2 新版鉴权：X-Api-Key + X-Api-Resource-Id
  T-AT3 旧版鉴权：纯数字 APP_ID → X-Api-App-Id + X-Api-Access-Key
  T-AT4 submit 构造：payload 含 req_params/audio_params/emotion/context_texts
  T-AT5 submit 错误处理：响应缺 task_id → RuntimeError
  T-AT6 submit 边界：超 10 万字符抛错
  T-AT7 query 状态机：0=Processing / 1=Success / 2=Failed
  T-AT8 wait_for_result 轮询：第一次 Processing → 第二次 Success → 返回 audio_url
  T-AT9 wait_for_result 超时：连续 N 次 Processing → asyncio.TimeoutError
  T-AT10 download_audio：HTTP GET 拿到 bytes
  T-AT11 synthesize_long_text 顶层 API：submit+wait+download 一次到位
  T-AT12 on_progress 回调：每轮 poll 触发一次
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------
# T-AT1 阈值
# ---------------------------------------------------------------------
def test_p2_2_at1_should_use_async_tts_threshold():
    """< 30000 字符 → False；>= 30000 → True。"""
    from backend.app.ai.providers.doubao.tts_async import (
        LONG_TEXT_THRESHOLD_CHARS,
        should_use_async_tts,
    )

    assert LONG_TEXT_THRESHOLD_CHARS == 30000
    assert should_use_async_tts("") is False
    assert should_use_async_tts("a" * 29999) is False
    assert should_use_async_tts("a" * 30000) is True
    assert should_use_async_tts("a" * 50000) is True


def test_p2_2_at1_should_use_async_tts_none_safe():
    """None 输入不抛错，按不切。"""
    from backend.app.ai.providers.doubao.tts_async import should_use_async_tts

    assert should_use_async_tts(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# T-AT2/3 鉴权
# ---------------------------------------------------------------------
def test_p2_2_at2_new_auth_headers():
    """非数字 APP_ID 走新版 X-Api-Key + Resource-Id。"""
    from backend.app.ai.providers.doubao.tts_async import DoubaoAsyncTTSClient

    c = DoubaoAsyncTTSClient(
        api_key="new-ak", access_key="any", app_id="not-pure-digits",
    )
    headers = c._headers("seed-tts-2.0", "req-123")
    assert headers["X-Api-Key"] == "new-ak"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert headers["X-Api-Request-Id"] == "req-123"
    assert "X-Api-App-Id" not in headers
    assert "X-Api-Access-Key" not in headers


def test_p2_2_at3_legacy_auth_headers_for_pure_numeric_app_id():
    """纯数字 APP_ID 走旧版 X-Api-App-Id + Access-Key。"""
    from backend.app.ai.providers.doubao.tts_async import DoubaoAsyncTTSClient

    c = DoubaoAsyncTTSClient(
        api_key="new-ak", access_key="legacy-ak", app_id="1234567890",
    )
    headers = c._headers("seed-icl-2.0", "req-456")
    assert headers["X-Api-App-Id"] == "1234567890"
    assert headers["X-Api-Access-Key"] == "legacy-ak"
    assert headers["X-Api-Resource-Id"] == "seed-icl-2.0"
    assert "X-Api-Key" not in headers


# ---------------------------------------------------------------------
# T-AT4 submit 构造
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at4_submit_payload_structure(monkeypatch):
    """submit 调用 payload 嵌套结构 + audio_params + emotion + context_texts 正确。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"task_id": "task-xyz", "task_status": 0, "text_length": 12345}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())

    c = mod.DoubaoAsyncTTSClient(
        api_key="new-ak", access_key="ak", app_id="not-numeric",
    )
    task_id = await c.submit(
        "一段超长文本" * 5000,
        "zh_female_vv_uranus_bigtts",
        resource_id="seed-tts-2.0",
        sample_rate=24000,
        speech_rate=10,
        loudness_rate=5,
        emotion="happy",
        context_texts=["语气欢快"],
        enable_subtitle=True,
    )
    assert task_id == "task-xyz"
    assert captured["url"] == mod._SUBMIT_URL
    body = captured["json"]
    assert body["user"]["uid"].startswith("local-")
    rp = body["req_params"]
    assert rp["speaker"] == "zh_female_vv_uranus_bigtts"
    ap = rp["audio_params"]
    assert ap["format"] == "mp3"
    assert ap["sample_rate"] == 24000
    assert ap["speech_rate"] == 10
    assert ap["loudness_rate"] == 5
    assert ap["emotion"] == "happy"
    assert ap["enable_subtitle"] is True
    assert rp["context_texts"] == ["语气欢快"]
    # 新版鉴权
    assert captured["headers"]["X-Api-Key"] == "new-ak"


# ---------------------------------------------------------------------
# T-AT5 submit 错误
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at5_submit_raises_when_no_task_id(monkeypatch):
    """submit 响应缺 task_id → RuntimeError，错误码透出。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    class _FakeResp:
        def raise_for_status(self): return None
        def json(self): return {"code": 40000, "message": "请求参数错误：text 不能为空"}

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, json, headers): return _FakeResp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())
    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    with pytest.raises(RuntimeError, match="缺 task_id"):
        await c.submit("x", "BV001_streaming")
    # 错误信息里要带 code 和 message
    with pytest.raises(RuntimeError, match="40000"):
        await c.submit("x", "BV001_streaming")


# ---------------------------------------------------------------------
# T-AT6 submit 边界
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at6_submit_rejects_over_100k():
    """超过 10万字符直接抛 RuntimeError（不会发出 HTTP 请求）。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    with pytest.raises(RuntimeError, match="10万字符"):
        await c.submit("x" * 100_001, "BV001_streaming")


@pytest.mark.asyncio
async def test_p2_2_at6_submit_rejects_empty_text():
    """空 text 直接抛错。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    with pytest.raises(RuntimeError, match="text 不能为空"):
        await c.submit("", "BV001_streaming")
    with pytest.raises(RuntimeError, match="text 不能为空"):
        await c.submit("   ", "BV001_streaming")


# ---------------------------------------------------------------------
# T-AT7 query 状态机
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at7_query_processing_success_failed(monkeypatch):
    """query 正确解析 0/1/2 三种状态。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    # 三次连续 query 模拟不同状态
    responses = iter([
        {"task_id": "t1", "task_status": 0},  # Processing
        {"task_id": "t1", "task_status": 1, "audio_url": "https://cdn/x.mp3", "audio_bytes": 12345},
        {"task_id": "t1", "task_status": 2, "error_code": 50000, "error_message": "服务内部错误"},
    ])

    class _FakeResp:
        def __init__(self, d): self._d = d
        def raise_for_status(self): return None
        def json(self): return self._d

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, json, headers): return _FakeResp(next(responses))

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())
    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")

    r1 = await c.query("t1")
    assert r1.status == mod.AsyncTaskStatus.PROCESSING
    assert r1.audio_url is None

    r2 = await c.query("t1")
    assert r2.status == mod.AsyncTaskStatus.SUCCESS
    assert r2.audio_url == "https://cdn/x.mp3"
    assert r2.audio_bytes == 12345

    r3 = await c.query("t1")
    assert r3.status == mod.AsyncTaskStatus.FAILED
    assert r3.error_code == 50000
    assert "服务内部错误" in (r3.error_message or "")


# ---------------------------------------------------------------------
# T-AT8 wait_for_result 轮询
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at8_wait_returns_first_terminal_status(monkeypatch):
    """连续 Processing → Success → wait_for_result 立即返回 Success，不轮询到超时。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    responses = iter([
        {"task_id": "t1", "task_status": 0},
        {"task_id": "t1", "task_status": 0},
        {"task_id": "t1", "task_status": 1, "audio_url": "https://cdn/x.mp3", "audio_bytes": 100},
    ])

    class _FakeResp:
        def __init__(self, d): self._d = d
        def raise_for_status(self): return None
        def json(self): return self._d

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, json, headers): return _FakeResp(next(responses))

    # 加速 sleep
    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    result = await c.wait_for_result("t1", poll_interval_s=0.001, poll_max_times=10)
    assert result.status == mod.AsyncTaskStatus.SUCCESS
    assert result.audio_url == "https://cdn/x.mp3"


# ---------------------------------------------------------------------
# T-AT9 wait 超时
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at9_wait_raises_timeout_when_always_processing(monkeypatch):
    """连续 N 次 Processing → asyncio.TimeoutError。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    class _FakeResp:
        def raise_for_status(self): return None
        def json(self): return {"task_id": "t1", "task_status": 0}

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, json, headers): return _FakeResp()

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    with pytest.raises(asyncio.TimeoutError, match="未完成"):
        await c.wait_for_result("t1", poll_interval_s=0.001, poll_max_times=3)


# ---------------------------------------------------------------------
# T-AT10 download_audio
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at10_download_audio(monkeypatch):
    """download_audio 通过 httpx GET 拿到 bytes。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    fake_bytes = b"\xff\xfb\x90\x64" + b"\x00" * 32

    class _FakeResp:
        def raise_for_status(self): return None
        @property
        def content(self): return fake_bytes

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url): return _FakeResp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())
    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    got = await c.download_audio("https://cdn/x.mp3")
    assert got == fake_bytes


# ---------------------------------------------------------------------
# T-AT11 synthesize_long_text
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at11_synthesize_long_text_end_to_end(monkeypatch):
    """submit+wait+download 一站式：mock 三步，返回 audio_bytes + audio_url。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    fake_audio = b"\xff\xfb\x90\x64" + b"\x00" * 16

    submit_done = {"called": False}
    wait_done = {"called": False}
    download_done = {"called": False}

    async def _fake_submit(self, text, voice_id, **kw):
        submit_done["called"] = True
        assert voice_id == "BV001_streaming"
        return "task-final"

    async def _fake_wait(self, task_id, **kw):
        wait_done["called"] = True
        return mod.AsyncTaskResult(
            task_id=task_id,
            status=mod.AsyncTaskStatus.SUCCESS,
            audio_url="https://cdn/final.mp3",
        )

    async def _fake_download(self, url):
        download_done["called"] = True
        assert url == "https://cdn/final.mp3"
        return fake_audio

    monkeypatch.setattr(mod.DoubaoAsyncTTSClient, "submit", _fake_submit)
    monkeypatch.setattr(mod.DoubaoAsyncTTSClient, "wait_for_result", _fake_wait)
    monkeypatch.setattr(mod.DoubaoAsyncTTSClient, "download_audio", _fake_download)

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    audio, url = await c.synthesize_long_text(
        "long text " * 5000,
        "BV001_streaming",
        resource_id="seed-tts-2.0",
    )
    assert audio == fake_audio
    assert url == "https://cdn/final.mp3"
    assert submit_done["called"]
    assert wait_done["called"]
    assert download_done["called"]


@pytest.mark.asyncio
async def test_p2_2_at11_synthesize_long_text_propagates_failure(monkeypatch):
    """wait 返回 FAILED → synthesize_long_text 抛 RuntimeError（不下载）。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    async def _fake_wait(self, task_id, **kw):
        return mod.AsyncTaskResult(
            task_id=task_id,
            status=mod.AsyncTaskStatus.FAILED,
            error_code=50000,
            error_message="服务内部错误",
        )

    monkeypatch.setattr(mod.DoubaoAsyncTTSClient, "submit", AsyncMock(return_value="t"))
    monkeypatch.setattr(mod.DoubaoAsyncTTSClient, "wait_for_result", _fake_wait)
    monkeypatch.setattr(
        mod.DoubaoAsyncTTSClient, "download_audio",
        AsyncMock(side_effect=AssertionError("失败任务不应下载")),
    )

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    with pytest.raises(RuntimeError, match="服务内部错误"):
        await c.synthesize_long_text("text", "BV001_streaming")


# ---------------------------------------------------------------------
# T-AT12 on_progress 回调
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_2_at12_on_progress_called_each_poll(monkeypatch):
    """每次轮询都触发 on_progress(elapsed_s) 一次（直到终态）。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    responses = iter([
        {"task_id": "t1", "task_status": 0},
        {"task_id": "t1", "task_status": 0},
        {"task_id": "t1", "task_status": 1, "audio_url": "https://cdn/x.mp3"},
    ])

    class _FakeResp:
        def __init__(self, d): self._d = d
        def raise_for_status(self): return None
        def json(self): return self._d

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, json, headers): return _FakeResp(next(responses))

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())

    progress_calls: list[float] = []

    async def _on_progress(elapsed: float) -> None:
        progress_calls.append(elapsed)

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    await c.wait_for_result(
        "t1", poll_interval_s=2.0, poll_max_times=10, on_progress=_on_progress,
    )
    # Processing 了 2 次 → on_progress 触发 2 次（sleep 后回调），间隔 2.0s
    assert progress_calls == [2.0, 4.0]


@pytest.mark.asyncio
async def test_p2_2_at12_on_progress_exception_swallowed(monkeypatch):
    """on_progress 内部抛错不应阻塞 wait 流程。"""
    from backend.app.ai.providers.doubao import tts_async as mod

    responses = iter([
        {"task_id": "t1", "task_status": 0},
        {"task_id": "t1", "task_status": 1, "audio_url": "https://cdn/x.mp3"},
    ])

    class _FakeResp:
        def __init__(self, d): self._d = d
        def raise_for_status(self): return None
        def json(self): return self._d

    class _FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, json, headers): return _FakeResp(next(responses))

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda timeout: _FakeClient())

    async def _bad_on_progress(elapsed: float) -> None:
        raise RuntimeError("回调故意抛错")

    c = mod.DoubaoAsyncTTSClient(api_key="ak", access_key="ak", app_id="abc")
    result = await c.wait_for_result(
        "t1", poll_interval_s=0.001, poll_max_times=5, on_progress=_bad_on_progress,
    )
    assert result.status == mod.AsyncTaskStatus.SUCCESS
