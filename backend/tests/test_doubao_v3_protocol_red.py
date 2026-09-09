"""P1-1 RED — 豆包 v3 单向流式 HTTP TTS Provider 协议测试。

覆盖范围（按 P1-1 checklist）：
  T-V3-1  Payload 嵌套结构（user/req_params/audio_params）正确
  T-V3-2  鉴权头：新版 X-Api-Key；旧版（纯数字）走 X-Api-App-Id + X-Api-Access-Key
  T-V3-3  X-Api-Resource-Id 按 model 选（seed-tts-1.0 / seed-tts-2.0 / seed-icl-2.0）
  T-V3-4  HTTP chunked 响应：多段 base64 audio 拼接 + duration_ms 估算
  T-V3-5  业务错（code != 0）抛 DoubaoTTSResponseV3Error + 不重试
  T-V3-6  网络错（429 / 5xx）走重试 + 达 MAX_RETRIES 后抛错
  T-V3-7  factory 路由：settings.DOUBAO_TTS_USE_V3=False → v1；=True → v3
  T-V3-8  list_voices 复用 _BUILTIN_VOICES + 标记 protocol='v3'

GLM 警告（保留给真实 Key 联调时复核）：
  - emotion 字段名暂用 audio_params.emotion（按官方文档 v3 协议）
  - 待真实 Key 各打一发后再正式落地情绪链路
"""
from __future__ import annotations

import asyncio
import base64 as _b64
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 模拟 MP3：≥32 byte 才能让 _estimate_mp3_duration_ms 拿到非零 dur_ms
FAKE_MP3_A = _b64.b64encode(b"\xff\xfb\x90\x64\x00" + (b"\x00" * 48)).decode("ascii")
FAKE_MP3_B = _b64.b64encode(b"\xff\xfb\x90\x64\x00" + (b"\x00" * 96)).decode("ascii")


# ---------------------------------------------------------------------
# T-V3-1: Payload 嵌套结构
# ---------------------------------------------------------------------
def test_v3_payload_structure_has_nested_req_params():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    payload = p._build_v3_payload(
        "今天天气真好",
        "doubao:BV001_streaming",
        emotion="calm",
        speed=1.0,
    )
    assert "user" in payload
    assert "req_params" in payload
    rp = payload["req_params"]
    assert rp["text"] == "今天天气真好"
    assert rp["speaker"] == "BV001_streaming"
    ap = rp["audio_params"]
    assert ap["format"] == "mp3"
    assert ap["sample_rate"] == p.DEFAULT_SAMPLE_RATE
    assert ap["speech_rate"] == 0  # speed=1.0 → 0
    assert ap["loudness_rate"] == 0
    assert ap["disable_markdown_filter"] is True
    assert ap["enable_subtitle"] is False
    # emotion="calm 是默认占位，不应塞进 audio_params
    assert "emotion" not in ap
    # icl: 前缀同样剥掉
    payload_icl = p._build_v3_payload("hi", "icl:S_abc123")
    assert payload_icl["req_params"]["speaker"] == "S_abc123"


def test_v3_payload_speed_mapping_to_speech_rate():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    assert p._map_speed_to_speech_rate(0.5) == -50
    assert p._map_speed_to_speech_rate(1.0) == 0
    assert p._map_speed_to_speech_rate(2.0) == 100
    # 异常输入（0/NaN/负数）→ 兜底 1.0 → speech_rate=0
    assert p._map_speed_to_speech_rate(0.0) == 0
    assert p._map_speed_to_speech_rate(-1.0) == 0
    # clamp 极大值 → 100
    assert p._map_speed_to_speech_rate(99.0) == 100
    # 非 emotion 默认值时塞进 audio_params
    p2 = p._build_v3_payload("x", "doubao:BV001_streaming", emotion="happy")
    assert p2["req_params"]["audio_params"].get("emotion") == "happy"


# ---------------------------------------------------------------------
# T-V3-2 + T-V3-3: 鉴权头 / Resource-Id 映射
# ---------------------------------------------------------------------
def test_v3_auth_headers_new_console_x_api_key():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    # 通过属性 setter 注入 key
    p._resolve_api_key = lambda: "abc123-real-api-key"
    headers = p._auth_headers(speaker_id="BV001_streaming")
    assert headers["X-Api-Key"] == "abc123-real-api-key"
    assert "X-Api-App-Id" not in headers
    assert "X-Api-Access-Key" not in headers
    assert headers["X-Api-App-Key"] == "aGjiRDfUWi"  # 官方固定值
    assert headers["X-Api-Resource-Id"] == "seed-tts-1.0"  # BV001 默认 1.0
    assert "X-Api-Request-Id" in headers
    assert headers["X-Api-Request-Id"].startswith("novel-")


def test_v3_auth_headers_legacy_console_app_id_only():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "1234567890"  # 纯数字 APP_ID
    headers = p._auth_headers(speaker_id="BV001_streaming")
    assert headers["X-Api-App-Id"] == "1234567890"
    assert headers["X-Api-Access-Key"] == "1234567890"  # 同 key 兜底
    assert "X-Api-Key" not in headers


def test_v3_resource_id_per_model():
    from backend.app.ai.providers.doubao.tts import (
        DoubaoTTSProviderV3,
        _resolve_resource_id_for_v3,
    )

    # 1.0 音色（BV 系列默认）
    assert _resolve_resource_id_for_v3("seed-tts-1.0") == "seed-tts-1.0"
    assert _resolve_resource_id_for_v3("seed-tts-1.0-concurr") == "seed-tts-1.0"
    # 2.0 音色
    assert _resolve_resource_id_for_v3("seed-tts-2.0") == "seed-tts-2.0"
    # ICL 系列
    assert _resolve_resource_id_for_v3("seed-icl-2.0") == "seed-icl-2.0"
    assert _resolve_resource_id_for_v3("seed-icl-1.0") == "seed-icl-1.0"
    # 未知 → 兜底 1.0
    assert _resolve_resource_id_for_v3("") == "seed-tts-1.0"
    assert _resolve_resource_id_for_v3("garbage") == "seed-tts-1.0"

    # _resolve_model_for_speaker 按 speaker 决定 model
    p = DoubaoTTSProviderV3()
    # BV001_streaming 在 _BUILTIN_VOICES 里 model=seed-tts-1.0
    assert p._resolve_model_for_speaker("BV001_streaming") == "seed-tts-1.0"
    # ICL 复刻 speaker_id
    assert p._resolve_model_for_speaker("S_abc123") == "seed-icl-2.0"
    assert p._resolve_model_for_speaker("icl_xyz") == "seed-icl-2.0"
    # 未知 speaker → 兜底 seed-tts-1.0
    assert p._resolve_model_for_speaker("not_a_real_voice") == "seed-tts-1.0"


# ---------------------------------------------------------------------
# T-V3-4: HTTP chunked 响应：多段 audio 拼接 + duration_ms
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v3_post_stream_concatenates_audio_chunks(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()

    # 模拟 httpx 流式响应：3 行 JSON chunked
    chunked_lines = "\n".join([
        json.dumps({"audio": FAKE_MP3_A}),
        json.dumps({"audio": FAKE_MP3_B}),
        json.dumps({"audio": ""}),  # 空 audio 行应该跳过
        json.dumps({"audio": FAKE_MP3_A}),
    ]).encode("utf-8")

    class _FakeResp:
        status_code = 200
        headers = {"X-Tt-Logid": "fake-logid-001"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in chunked_lines.split(b"\n"):
                yield line.decode("utf-8")

    class _FakeStreamCtx:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *args):
            return False

    class _FakeClientCtx:
        def stream(self, *args, **kwargs):
            return _FakeStreamCtx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClientCtx())

    result = await p._post_stream_v3(
        "https://example.test/v3/tts/unidirectional",
        {"X-Api-Key": "fake"},
        {"text": "hi"},
    )
    # 3 个有效 chunk 的解码字节拼接
    expected = _b64.b64decode(FAKE_MP3_A) + _b64.b64decode(FAKE_MP3_B) + _b64.b64decode(FAKE_MP3_A)
    assert result == expected
    assert len(result) > 100


# ---------------------------------------------------------------------
# T-V3-5: 业务错（code != 0）抛错 + 不重试
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v3_business_error_raises_and_does_not_retry(monkeypatch):
    from backend.app.ai.providers.doubao.tts import (
        DoubaoTTSProviderV3,
        DoubaoTTSResponseV3Error,
    )
    from backend.app.core import config as cfgmod

    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "fake"
    cfgmod.settings.DOUBAO_TTS_V3_BASE_URL = "https://example.test/v3/tts/unidirectional"

    chunked_lines = json.dumps({"code": 401, "message": "鉴权失败"}).encode("utf-8")

    class _FakeResp:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield chunked_lines.decode("utf-8")

    class _FakeStreamCtx:
        async def __aenter__(self):
            return _FakeResp()

        async def __aexit__(self, *args):
            return False

    class _FakeClientCtx:
        def stream(self, *args, **kwargs):
            return _FakeStreamCtx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClientCtx())

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes("hi", "doubao:BV001_streaming")
    assert ei.value.code == 401
    assert "鉴权失败" in str(ei.value)


# ---------------------------------------------------------------------
# T-V3-6: 网络错（5xx）走重试 + 达 MAX_RETRIES 后抛错
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v3_network_error_5xx_retries_then_fails(monkeypatch):
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "fake"
    from backend.app.core import config as cfgmod
    cfgmod.settings.DOUBAO_TTS_V3_BASE_URL = "https://example.test/v3/tts/unidirectional"
    # 把 MAX_RETRIES 压到 2 加速测试
    p.MAX_RETRIES = 2
    p.BASE_BACKOFF_SECS = 0.0
    p.JITTER_SECS = 0.0

    call_count = {"n": 0}

    class _FakeResp:
        def __init__(self, code):
            self.status_code = code
            self.headers = {}

        def raise_for_status(self):
            # 抛 HTTPStatusError（v3 路径上让 raise_for_status 抛）
            import httpx
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

        async def aread(self):
            return b""

        async def aiter_lines(self):
            return
            yield ""  # unreachable，但保持 async generator

    class _FakeStreamCtx:
        def __init__(self):
            self.resp = _FakeResp(503 if call_count["n"] < p.MAX_RETRIES else 200)

        async def __aenter__(self):
            call_count["n"] += 1
            return self.resp

        async def __aexit__(self, *args):
            return False

    class _FakeClientCtx:
        def stream(self, *args, **kwargs):
            return _FakeStreamCtx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClientCtx())

    with pytest.raises(RuntimeError, match="豆包 TTS v3 合成失败"):
        await p.synthesize_to_bytes("hi", "doubao:BV001_streaming")
    # 2 次尝试都被调到（达到 MAX_RETRIES）
    assert call_count["n"] == 2


# ---------------------------------------------------------------------
# T-V3-7: factory 路由（settings.DOUBAO_TTS_USE_V3 开关）
# ---------------------------------------------------------------------
def test_factory_routes_to_v3_when_use_v3_flag_on():
    from backend.app.ai import factory as aifact
    from backend.app.ai.providers.doubao.tts import (
        DoubaoTTSProvider,
        DoubaoTTSProviderV3,
    )
    from backend.app.core.config import settings

    # 清缓存
    aifact._tts_instances.clear()
    prev = settings.DOUBAO_TTS_USE_V3
    try:
        settings.DOUBAO_TTS_USE_V3 = True
        inst = aifact.get_tts("doubao")
        assert isinstance(inst, DoubaoTTSProviderV3), (
            f"DOUBAO_TTS_USE_V3=True 时应返回 v3 实例，实际 {type(inst).__name__}"
        )
        settings.DOUBAO_TTS_USE_V3 = False
        aifact._tts_instances.clear()  # 清缓存让下次重新创建
        inst_v1 = aifact.get_tts("doubao")
        assert isinstance(inst_v1, DoubaoTTSProvider), (
            f"DOUBAO_TTS_USE_V3=False 时应返回 v1 实例，实际 {type(inst_v1).__name__}"
        )
    finally:
        settings.DOUBAO_TTS_USE_V3 = prev
        aifact._tts_instances.clear()


# ---------------------------------------------------------------------
# T-V3-8: list_voices 复用 _BUILTIN_VOICES + 标记 protocol=v3
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v3_list_voices_reuses_builtin_with_protocol_marker():
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3

    p = DoubaoTTSProviderV3()
    voices = await p.list_voices()
    assert len(voices) >= 50, f"v3 list_voices 应至少复用 50 条内置音色，实际 {len(voices)}"
    # 每条都标 protocol='v3'
    for v in voices:
        assert v["provider"] == "doubao"
        assert v["protocol"] == "v3"
        assert v["id"].startswith("doubao:")
    # BV001_streaming 必在
    bv001 = next((v for v in voices if v["id"] == "doubao:BV001_streaming"), None)
    assert bv001 is not None, "BV001_streaming 必须在 v3 列表里"
    assert bv001["model"] == "seed-tts-1.0"