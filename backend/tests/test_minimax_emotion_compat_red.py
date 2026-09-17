"""[P-fix] MiniMax provider 兼容老 model（speech-01 等不支持 emotion 的版本）。

回归测试：
- emotion 为空 / "calm" / "neutral" 时，voice_setting 字典里**不**应包含 emotion 字段
  （这样老 model 也能合成，不会触发服务端 "model does not support emotion setting" 错误）
- emotion 为非中性值（happy/sad/angry/fearful/disgusted/surprised）时，voice_setting 应包含 emotion
- 实际 HTTP 请求体里也应反映同样的过滤逻辑

历史：
  用户在前端为 narrator 配置了 emotion='calm'，MiniMax 服务端返回
  "invalid params, this model does not support emotion setting, model: speech-01"，
  导致构建失败。根因：MiniMaxTTSProvider 把 emotion="calm" 也写进 voice_setting.emotion，
  但老 model 不接受该字段。
"""
import json
import pytest
import httpx


# ===== 拦截 httpx.AsyncClient.post 的 transport =====
class _CaptureTransport(httpx.AsyncBaseTransport):
    """捕获每次 POST 的 (url, body) 供断言。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # 仅关心 POST /t2a_v2
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except Exception:
            body = {"_raw": request.content[:200].decode("utf-8", errors="replace")}
        self.calls.append((str(request.url), body))
        # 返回合法 JSON 响应，避免 raise RuntimeError("响应不是有效 JSON")
        return httpx.Response(
            200,
            json={
                # 至少 1 byte hex（"ab"），避免 "data.audio 空" raise
                "data": {"audio": "ab"},
                "extra_info": {"audio_length": 100},
                "base_resp": {"status_code": 0, "status_msg": "OK"},
                "trace_id": "test-trace",
            },
        )


@pytest.mark.asyncio
async def test_minimax_emotion_calm_omitted_from_voice_setting(monkeypatch):
    """emotion="calm" 时不应出现在 voice_setting 里（兼容老 model）。"""
    from app.ai.providers.minimax.tts import MiniMaxTTSProvider

    transport = _CaptureTransport()
    # 替换 httpx.AsyncClient 内部使用的 transport
    orig_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)

    # 用 ACTIVE_TTS_MODEL 默认值（已经是 MiniMax-speech-2.8-turbo）
    provider = MiniMaxTTSProvider()
    await provider.synthesize_to_bytes(
        "测试文本", voice_id="male-qn-jingying", emotion="calm", speed=1.0,
    )

    assert len(transport.calls) >= 1, "MiniMax provider 没发起 HTTP 请求"
    _url, body = transport.calls[0]
    vs = body.get("voice_setting", {})
    assert "emotion" not in vs, (
        f"emotion='calm' 时不应发送 emotion 字段，实际 voice_setting={vs}"
    )


@pytest.mark.asyncio
async def test_minimax_emotion_neutral_omitted_from_voice_setting(monkeypatch):
    """emotion='neutral' 也不发送。"""
    from app.ai.providers.minimax.tts import MiniMaxTTSProvider

    transport = _CaptureTransport()
    orig_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)

    provider = MiniMaxTTSProvider()
    await provider.synthesize_to_bytes(
        "测试文本", voice_id="male-qn-jingying", emotion="neutral", speed=1.0,
    )

    assert len(transport.calls) >= 1
    _url, body = transport.calls[0]
    vs = body.get("voice_setting", {})
    assert "emotion" not in vs


@pytest.mark.asyncio
async def test_minimax_emotion_empty_omitted_from_voice_setting(monkeypatch):
    """emotion='' 也不发送。"""
    from app.ai.providers.minimax.tts import MiniMaxTTSProvider

    transport = _CaptureTransport()
    orig_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)

    provider = MiniMaxTTSProvider()
    await provider.synthesize_to_bytes(
        "测试文本", voice_id="male-qn-jingying", emotion="", speed=1.0,
    )

    assert len(transport.calls) >= 1
    _url, body = transport.calls[0]
    vs = body.get("voice_setting", {})
    assert "emotion" not in vs


@pytest.mark.asyncio
async def test_minimax_emotion_happy_included_in_voice_setting(monkeypatch):
    """emotion='happy'（用户显式配置）应保留在 voice_setting 里。"""
    from app.ai.providers.minimax.tts import MiniMaxTTSProvider

    transport = _CaptureTransport()
    orig_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)

    provider = MiniMaxTTSProvider()
    await provider.synthesize_to_bytes(
        "测试文本", voice_id="male-qn-jingying", emotion="happy", speed=1.0,
    )

    assert len(transport.calls) >= 1
    _url, body = transport.calls[0]
    vs = body.get("voice_setting", {})
    assert vs.get("emotion") == "happy", (
        f"emotion='happy' 应保留在 voice_setting，实际={vs}"
    )


@pytest.mark.asyncio
async def test_minimax_emotion_sad_included_in_voice_setting(monkeypatch):
    """emotion='sad' 应保留。"""
    from app.ai.providers.minimax.tts import MiniMaxTTSProvider

    transport = _CaptureTransport()
    orig_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)

    provider = MiniMaxTTSProvider()
    await provider.synthesize_to_bytes(
        "测试文本", voice_id="male-qn-jingying", emotion="sad", speed=1.0,
    )

    assert len(transport.calls) >= 1
    _url, body = transport.calls[0]
    vs = body.get("voice_setting", {})
    assert vs.get("emotion") == "sad"


def test_active_tts_model_default_is_modern_model():
    """回归测试：默认 ACTIVE_TTS_MODEL 应是支持 emotion 的现代 model。

    历史默认值 'MiniMax-speech-01' 是 MiniMax 老模型，服务端拒收 emotion。
    现已升到 'MiniMax-speech-2.8-turbo'，新装用户默认就是支持 emotion 的版本。
    老用户 DB 里残留的旧值不会自动迁移（避免破坏用户当前配置），
    但 MiniMax provider 的 emotion 兼容逻辑保证老 model 也能合成（不带情感）。
    """
    from app.core.config import settings
    assert settings.ACTIVE_TTS_MODEL == "MiniMax-speech-2.8-turbo", (
        f"默认 ACTIVE_TTS_MODEL 应为 MiniMax-speech-2.8-turbo，实际={settings.ACTIVE_TTS_MODEL}"
    )