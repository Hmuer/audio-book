"""v3 非 2xx 响应体透传 + 失败诊断信息 RED。

背景：Build 章节合成报
  RuntimeError: 豆包 TTS v3 合成失败：HTTPStatusError:
    Client error '403 Forbidden' for url '.../api/v3/tts/unidirectional'

403 既可能是「Key 没有该资源权限」，也可能是「X-Api-Resource-Id 与音色不匹配」，
上游会把真正原因放在响应体里（`{"code":45000001,"message":"..."}`），
但旧实现只对 429/5xx 调 `aread()`，4xx 直接 `raise_for_status()` → body 被丢弃，
日志里只剩一个状态码，排查无路可走。

覆盖：
  T-B1 403 带 JSON body：body 文本 + speaker + resource_id 都进入最终异常
  T-B2 4xx 空 body → 占位 "(empty body)"，message 不留空白
  T-B3 403 属网关级拒绝：不重试（只发 1 次请求）
  T-B4 45000030「requested resource not granted」→ 翻译成可操作提示
  T-B5 其它 401/403 → 兜底提示（不猜具体 code）
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.ai.providers.doubao.tts import DoubaoTTSProviderV3  # noqa: E402

_ENDPOINT = "https://example.test/v3/tts/unidirectional"


def _install_fake_client(monkeypatch, *, status_code, body, headers=None):
    """把 httpx.AsyncClient 换成固定状态码/响应体的假 client，返回调用计数 dict。"""
    counter: dict[str, int] = {"n": 0}

    class _FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.headers = dict(headers or {})

        def raise_for_status(self):
            # 修复后不应再走到这里（非 2xx 由 _post_stream_v3 显式读 body 后构造异常）
            raise AssertionError(
                "非 2xx 不应走裸 raise_for_status（会丢掉上游响应体）"
            )

        async def aread(self):
            return body

        async def aiter_lines(self):
            return
            yield ""  # unreachable，仅保持 async generator

    class _FakeStreamCtx:
        async def __aenter__(self):
            counter["n"] += 1
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

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeClientCtx())
    return counter


def _make_provider(monkeypatch) -> DoubaoTTSProviderV3:
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_TTS_V3_BASE_URL", _ENDPOINT)
    # 退避清零：即使出现回归（误重试）也能快速失败，不拖慢用例
    monkeypatch.setattr(DoubaoTTSProviderV3, "BASE_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(DoubaoTTSProviderV3, "JITTER_SECS", 0.0)
    monkeypatch.setattr(DoubaoTTSProviderV3, "HTTP_5XX_BACKOFF_SECS", 0.0)
    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "fake-api-key"  # type: ignore[method-assign]
    return p


# ---------------------------------------------------------------------
# T-B1：403 + JSON body → body / speaker / resource_id 全部透传
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b1_403_body_and_diagnostics_surface(monkeypatch):
    body = b'{"code":45000001,"message":"[Invalid argument] speaker not found"}'
    _install_fake_client(
        monkeypatch,
        status_code=403,
        body=body,
        headers={"X-Tt-Logid": "v3-403-log"},
    )
    p = _make_provider(monkeypatch)

    with pytest.raises(RuntimeError) as ei:
        await p.synthesize_to_bytes("你好", "doubao:BV158_streaming")

    err = ei.value
    s = str(err)
    # 上游 status + body 必须出现在异常里
    assert "HTTP 403" in s, f"缺少 HTTP 状态码：{s}"
    assert "[Invalid argument] speaker not found" in s, f"缺少上游响应体：{s}"
    # 诊断：实际发出的音色 + resource id
    assert "speaker=BV158_streaming" in s, f"缺少 speaker 诊断：{s}"
    assert "X-Api-Resource-Id=seed-tts-1.0" in s, f"缺少 resource_id 诊断：{s}"
    # logid 仍要透传（P1-6 行为不回退）
    assert "logid=v3-403-log" in s, f"缺少 logid：{s}"
    assert getattr(err, "logid", None) == "v3-403-log"
    # __cause__ 必须是 HTTPStatusError 且保留 response（重试分支靠它读 status_code）
    assert isinstance(err.__cause__, httpx.HTTPStatusError)
    assert err.__cause__.response.status_code == 403


# ---------------------------------------------------------------------
# T-B2：4xx 空 body → 占位 "(empty body)"
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b2_empty_body_gets_placeholder(monkeypatch):
    _install_fake_client(monkeypatch, status_code=401, body=b"")
    p = _make_provider(monkeypatch)

    with pytest.raises(RuntimeError) as ei:
        await p.synthesize_to_bytes("你好", "doubao:BV001_streaming")

    s = str(ei.value)
    assert "HTTP 401" in s
    assert "(empty body)" in s, f"空响应体应有占位：{s}"


# ---------------------------------------------------------------------
# T-B3：403 不重试（网关级拒绝，重试无意义）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b3_403_is_not_retried(monkeypatch):
    counter = _install_fake_client(monkeypatch, status_code=403, body=b"forbidden")
    p = _make_provider(monkeypatch)

    with pytest.raises(RuntimeError):
        await p.synthesize_to_bytes("你好", "doubao:BV001_streaming")

    assert counter["n"] == 1, f"403 不应重试，实际请求 {counter['n']} 次"


# ---------------------------------------------------------------------
# T-B4：45000030「requested resource not granted」→ 翻译成可操作提示
#
# 实测响应体（2026-09-20，音色 BV158_streaming / resource_id=seed-tts-1.0）：
#   {"header":{"reqid":"...","code":45000030,
#    "message":"[resource_id=volc.service_type.10029] requested resource not granted"}}
# 这是「账号没开通该资源」，跟文本/音色无关，裸 403 无法自解释。
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b4_resource_not_granted_gets_actionable_hint(monkeypatch):
    body = (
        b'{"header":{"reqid":"novel-x","code":45000030,'
        b'"message":"[resource_id=volc.service_type.10029] '
        b'requested resource not granted"}}'
    )
    _install_fake_client(monkeypatch, status_code=403, body=body)
    p = _make_provider(monkeypatch)

    with pytest.raises(RuntimeError) as ei:
        await p.synthesize_to_bytes("你好", "doubao:BV158_streaming")

    s = str(ei.value)
    assert "requested resource not granted" in s  # 原始响应体仍在
    assert "账号未开通" in s, f"应给出可操作提示：{s}"
    assert "seed-tts-2.0" in s, f"应指出替代方案：{s}"


# ---------------------------------------------------------------------
# T-B5：其它 401/403 也给一句兜底提示（不猜具体 code）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b5_generic_403_gets_fallback_hint(monkeypatch):
    _install_fake_client(monkeypatch, status_code=403, body=b"forbidden")
    p = _make_provider(monkeypatch)

    with pytest.raises(RuntimeError) as ei:
        await p.synthesize_to_bytes("你好", "doubao:BV001_streaming")

    assert "鉴权/资源未授权" in str(ei.value)
