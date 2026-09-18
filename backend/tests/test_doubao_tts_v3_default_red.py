"""豆包 TTS：默认走 v3 + 上游非 2xx 时把响应体带进异常。

背景（真实日志）：
    2026-09-18 15:48:24 | WARNING | ... [DoubaoTTS] 重试（attempt=1/5）：
    HTTPStatusError: Server error '500 Internal Server Error'
    for url 'https://openspeech.bytedance.com/api/v1/tts' ... logid=2026091815482435...

  音色 BV158_streaming（1.0 小模型）走 v1 端点稳定 500。定位结论：
  v1 的 `_build_payload` 把 `app.appid` / `app.token` 恒写成空串、只带
  `Authorization: Bearer;<key>`，新版控制台的单一 API Key 按这个契约调
  /api/v1/tts 无法工作；正确路径是 v3（X-Api-Key + X-Api-Resource-Id）。

覆盖：
  T-D1 `DOUBAO_TTS_USE_V3` 字段默认值为 True（新装开箱即走 v3）
  T-D2 `_post_json_for_v1` 遇 500 时把上游响应体（业务码/message）带进异常 message
  T-D3 同上：异常仍是 httpx.HTTPStatusError 且保留 .response（重试判定依赖它）
  T-D4 上游响应体为空时也要给出可读提示，不吞成空字符串
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =====================================================================
# T-D1：默认协议 = v3
# =====================================================================


def test_doubao_tts_use_v3_default_is_true():
    """断言字段默认值本身（不受 conftest / 运行时 monkeypatch 影响）。"""
    from backend.app.core.config import Settings

    field = Settings.model_fields["DOUBAO_TTS_USE_V3"]
    assert field.default is True, (
        "DOUBAO_TTS_USE_V3 默认必须为 True：v1 请求体 app.appid/token 恒为空，"
        f"新版控制台 API Key 走 v1 会稳定 500（实际默认值 {field.default!r}）"
    )


# =====================================================================
# T-D2~T-D4：非 2xx 时把上游响应体带进异常
# =====================================================================


def _patch_httpx(monkeypatch, *, status_code: int, body: bytes, logid: str = "log-test-001"):
    """把 httpx.AsyncClient 换成返回固定响应的假 client。"""
    import httpx

    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return httpx.Response(
                status_code,
                content=body,
                headers={"X-Tt-Logid": logid},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return captured


@pytest.mark.asyncio
async def test_http_500_carries_upstream_body(monkeypatch):
    """500 时异常 message 必须含上游 body 里的业务码与原因。"""
    import httpx

    from backend.app.ai.providers.doubao import tts as tts_mod

    upstream = (
        b'{"code":45000001,"message":"[Invalid argument] speaker not found",'
        b'"request_id":"2026091815482435"}'
    )
    _patch_httpx(monkeypatch, status_code=500, body=upstream)

    with pytest.raises(httpx.HTTPStatusError) as ei:
        await tts_mod._post_json_for_v1(
            "https://openspeech.bytedance.com/api/v1/tts", {}, {}
        )

    msg = str(ei.value)
    assert "45000001" in msg, f"异常 message 缺上游业务码：{msg}"
    assert "speaker not found" in msg, f"异常 message 缺上游原因：{msg}"


@pytest.mark.asyncio
async def test_http_500_keeps_exception_type_and_response(monkeypatch):
    """重试判定依赖 isinstance(HTTPStatusError) 与 .response.status_code，不能被破坏。"""
    import httpx

    from backend.app.ai.providers.doubao import tts as tts_mod

    _patch_httpx(monkeypatch, status_code=500, body=b'{"code":55000000}')

    with pytest.raises(httpx.HTTPStatusError) as ei:
        await tts_mod._post_json_for_v1(
            "https://openspeech.bytedance.com/api/v1/tts", {}, {}
        )

    assert isinstance(ei.value, httpx.HTTPStatusError)
    assert ei.value.response is not None
    assert ei.value.response.status_code == 500  # 5xx fastfail 判定要用


@pytest.mark.asyncio
async def test_http_error_with_empty_body_is_readable(monkeypatch):
    """上游返回空 body 时给出占位提示，而不是把 message 截成空串。"""
    import httpx

    from backend.app.ai.providers.doubao import tts as tts_mod

    _patch_httpx(monkeypatch, status_code=503, body=b"")

    with pytest.raises(httpx.HTTPStatusError) as ei:
        await tts_mod._post_json_for_v1(
            "https://openspeech.bytedance.com/api/v1/tts", {}, {}
        )

    msg = str(ei.value)
    assert "503" in msg
    assert "(empty body)" in msg, f"空 body 时应有可读占位：{msg}"
