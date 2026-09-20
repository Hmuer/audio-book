"""v3 HTTP 单向流式「响应体形状」RED。

背景（2026-09-20 实测，音色试听 500）：
  DoubaoTTSResponseV3Error: v3 TTS 业务错：code=20000000 msg=OK
  logid=2026092013535759634A20BCF37CCA033B

这里同时踩了两个坑，而且互相掩盖：

1. **成功码不止 0**。官方文档《单向流式语音合成HTTP》响应示例写 `"code": 0`，
   并注明「message 返回 OK 则表示合成成功」；实测返回的是 `code=20000000,
   message=OK`（HTTP 200、耗时 2s、音频正常）。旧实现只认 0 → 把成功当失败。
2. **音频字段名猜错了**。官方响应示例是 `"data": "<base64 encoded audio chunk>"`，
   旧实现读的是 `audio` → 即使成功也收不到音频，只会抛「响应无 audio chunk」。

坑 1 之所以掩盖了坑 2：错误分支会 `continue`，同一条 chunk 里的音频被直接跳过，
所以先报出来的是「业务错」而不是「没有音频」。

覆盖：
  T-S1  code=20000000 + message=OK + data → 成功（实测形态）
  T-S2  code=0 + message=OK + data → 成功（官方文档形态）
  T-S3  audio 字段兜底仍可用（兼容其它版本）
  T-S4  多 chunk 拼接：data 分片 + 20000000 收尾 chunk
  T-S5  真正的业务错误码（45000001）仍抛错，且 code 透传
  T-S6  成功码但无任何音频字段 → 报错里带出实际收到的字段名（便于定位）
  T-S7  先收到音频分片、中途才报错 → 仍必须抛错（不得返回被截断的音频）
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.ai.providers.doubao.tts import (  # noqa: E402
    DoubaoTTSProviderV3,
    DoubaoTTSResponseV3Error,
)

_ENDPOINT = "https://example.test/v3/tts/unidirectional"
_AUDIO = b"\xff\xfb\x90\x64" + b"\x11" * 64


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _install_fake_stream(monkeypatch, chunks: list[dict], *, status_code: int = 200):
    """把 httpx.AsyncClient 换成按行返回给定 JSON chunk 的假流式 client。"""
    lines = [json.dumps(c, ensure_ascii=False) for c in chunks]

    class _FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.headers = {"X-Tt-Logid": "v3-stream-log"}

        def raise_for_status(self):
            return None

        async def aread(self):
            return b""

        async def aiter_lines(self):
            for ln in lines:
                yield ln

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


def _make_provider(monkeypatch) -> DoubaoTTSProviderV3:
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_TTS_V3_BASE_URL", _ENDPOINT)
    monkeypatch.setattr(DoubaoTTSProviderV3, "BASE_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(DoubaoTTSProviderV3, "JITTER_SECS", 0.0)
    monkeypatch.setattr(DoubaoTTSProviderV3, "HTTP_5XX_BACKOFF_SECS", 0.0)
    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "fake-api-key"  # type: ignore[method-assign]
    return p


# ---------------------------------------------------------------------
# T-S1 / T-S2：两种成功码都要能合成出音频
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts1_code_20000000_is_success(monkeypatch):
    """实测形态：code=20000000 + message=OK + data → 必须成功。"""
    _install_fake_stream(monkeypatch, [
        {"code": 20000000, "message": "OK", "data": _b64(_AUDIO)},
    ])
    p = _make_provider(monkeypatch)

    data, _dur = await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert data == _AUDIO, "code=20000000 是成功码，音频必须被正确取出"


@pytest.mark.asyncio
async def test_ts2_code_0_is_success(monkeypatch):
    """官方文档形态：code=0 + message=OK + data。"""
    _install_fake_stream(monkeypatch, [
        {"code": 0, "message": "OK", "data": _b64(_AUDIO)},
    ])
    p = _make_provider(monkeypatch)

    data, _dur = await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert data == _AUDIO


# ---------------------------------------------------------------------
# T-S3：audio 字段兜底
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts3_audio_field_still_accepted(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 0, "message": "OK", "audio": _b64(_AUDIO)},
    ])
    p = _make_provider(monkeypatch)

    data, _dur = await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert data == _AUDIO


# ---------------------------------------------------------------------
# T-S4：多 chunk 拼接（含只带 code/message 的收尾 chunk）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts4_multi_chunk_concatenation(monkeypatch):
    second = b"\x22" * 32
    _install_fake_stream(monkeypatch, [
        {"code": 20000000, "message": "OK", "data": _b64(_AUDIO)},
        # 收尾 chunk：只有状态码，没有音频 —— 不应被当成错误
        {"code": 20000000, "message": "OK", "usage": {"text_words": 2}},
        {"code": 0, "message": "OK", "data": _b64(second)},
    ])
    p = _make_provider(monkeypatch)

    data, _dur = await p.synthesize_to_bytes("你好世界", "doubao:zh_female_vv_uranus_bigtts")

    assert data == _AUDIO + second


# ---------------------------------------------------------------------
# T-S5：真业务错误码仍要抛错
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts5_real_error_code_still_raises(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 45000001, "message": "[Invalid argument] speaker not found"},
    ])
    p = _make_provider(monkeypatch)

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert ei.value.code == 45000001
    assert "speaker not found" in str(ei.value)
    assert ei.value.logid == "v3-stream-log"


# ---------------------------------------------------------------------
# T-S6：成功码但没有音频字段 → 报错必须带上实际字段名
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts6_no_audio_field_error_reports_observed_keys(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 20000000, "message": "OK", "payload": "???"},
    ])
    p = _make_provider(monkeypatch)

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    s = str(ei.value)
    assert "无音频分片" in s
    # 字段名一旦再猜错，报错本身要能提供线索
    assert "payload" in s, f"应列出实际收到的字段名：{s}"


# ---------------------------------------------------------------------
# T-S7：先收到音频分片、中途才报错 → 不得返回被截断的音频（回归 A-6）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts7_error_after_audio_must_not_return_truncated(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 20000000, "message": "OK", "data": _b64(_AUDIO)},
        {"code": 55000000, "message": "synthesis processing timeout"},
    ])
    p = _make_provider(monkeypatch)

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert ei.value.code == 55000000
