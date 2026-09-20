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
  T-S8  未知成功码 + message=OK → 按文档口径仍判成功
  T-S9  未知码 + 非 OK message → 判失败（放宽判定不能吞掉真错误）
  T-S10 data 字段存在但为空 → 报错必须区分「字段为空」与「字段不存在」
  T-S11 中文文本 + 纯外语音色 → 报错要指出语种不匹配（2026-09-20 试听实测）
  T-S12 文本语种与音色匹配时不加语种提示（避免误导归因）
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


# ---------------------------------------------------------------------
# T-S8：未知成功码 + message=OK → 按文档口径仍判成功
#
# 文档写的是「message 返回 OK 则表示语音合成成功」。我们已经在「成功码只有 0」
# 上错过一次，不要再赌第二个码，所以以 message 为主判定、code 白名单为辅。
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts8_message_ok_is_success_for_unknown_code(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 20000009, "message": "OK", "data": _b64(_AUDIO)},
    ])
    p = _make_provider(monkeypatch)

    data, _dur = await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert data == _AUDIO


# ---------------------------------------------------------------------
# T-S9：未知码 + 非 OK message → 判失败（放宽判定不能把真错误吞掉）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts9_unknown_code_with_non_ok_message_raises(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 40000001, "message": "some upstream failure"},
    ])
    p = _make_provider(monkeypatch)

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    assert ei.value.code == 40000001


# ---------------------------------------------------------------------
# T-S10：data 字段存在但为空 → 报错要能区分「空」与「字段不存在」
#
# 2026-09-20 线上形态：字段列表里明明有 data，报错却说「既无 data 也无 audio
# 字段」，日志自相矛盾、无法定位。空音频和字段缺失必须分开说。
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts10_empty_data_field_reported_as_empty_not_missing(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 20000000, "message": "OK", "data": ""},
    ])
    p = _make_provider(monkeypatch)

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes("你好", "doubao:zh_female_vv_uranus_bigtts")

    s = str(ei.value)
    assert "data 形态=type=str len=0" in s, f"要说明 data 存在但为空：{s}"
    assert "JSON 行数=1" in s


# ---------------------------------------------------------------------
# T-S11：中文文本 + 纯外语音色 → 归因到语种不匹配
#
# 实测：Stokie（en_female_stokie_uranus_bigtts，音色表声明语种 ["en"]）读中文
# 试听文案时，上游返回 code=20000000 / message=OK 且 data 为空。这类失败既没有
# HTTP 错也没有业务错误码，报错必须自己把语种线索带出来。
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts11_cjk_text_with_english_voice_hints_language_mismatch(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 20000000, "message": "OK", "data": ""},
    ])
    p = _make_provider(monkeypatch)

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes(
            "夜色渐深，风穿过巷口，远处传来零星的犬吠声。",
            "doubao:en_female_stokie_uranus_bigtts",
        )

    s = str(ei.value)
    assert "疑似语种不匹配" in s, f"应指出语种不匹配：{s}"
    assert "en_female_stokie_uranus_bigtts" in s


# ---------------------------------------------------------------------
# T-S12：语种匹配时不加语种提示（空音频还有别的原因，不能一律归咎语种）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ts12_matching_language_gets_no_language_hint(monkeypatch):
    _install_fake_stream(monkeypatch, [
        {"code": 20000000, "message": "OK", "data": ""},
    ])
    p = _make_provider(monkeypatch)

    with pytest.raises(DoubaoTTSResponseV3Error) as ei:
        await p.synthesize_to_bytes(
            "The night grew deeper, and a cold wind slipped through the alley.",
            "doubao:en_female_stokie_uranus_bigtts",
        )

    assert "疑似语种不匹配" not in str(ei.value)
