"""批次 3（整体优化）— 切模型 / 配豆包 相关回归测试。

覆盖：
- C-3 TTS 实例缓存纳入配置指纹（运行中改配置即时生效，无需重启）
- C-4 MiniMax 区分可重试 / 永久性错误（永久错误不重试、不白等退避）
- C-5 MiniMax `_internal_model` 复用统一的前缀剥离逻辑（未激活分支也尊重 model / ACTIVE_TTS_MODEL）
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest


# ---------------------------------------------------------------------
# C-5 `_internal_model` 前缀剥离
# ---------------------------------------------------------------------
def test_c5_strip_model_prefix_variants():
    from backend.app.ai.providers.minimax.tts import _strip_model_prefix

    assert _strip_model_prefix("MiniMax-speech-01") == "speech-01"
    assert _strip_model_prefix("minimax:speech-2.8-turbo") == "speech-2.8-turbo"
    assert _strip_model_prefix("speech-02") == "speech-02"
    # 空值 → 默认内部名
    assert _strip_model_prefix("") == "speech-2.8-turbo"
    assert _strip_model_prefix(None) == "speech-2.8-turbo"


def test_c5_unactivated_branch_respects_explicit_model(monkeypatch):
    """未走多厂商激活分支时，显式传入的 model 必须生效（旧实现被硬编码死值覆盖）。"""
    from backend.app.core import config as cfgmod
    from backend.app.ai.providers.minimax.tts import MiniMaxTTSProvider

    monkeypatch.setattr(cfgmod, "get_active_tts_provider", lambda: None)

    p = MiniMaxTTSProvider(model="MiniMax-speech-01")
    assert p.model == "MiniMax-speech-01"
    assert p._internal_model == "speech-01"

    p2 = MiniMaxTTSProvider(model="minimax:speech-02")
    assert p2._internal_model == "speech-02"


def test_c5_unactivated_branch_falls_back_to_active_tts_model(monkeypatch):
    from backend.app.core import config as cfgmod
    from backend.app.ai.providers.minimax.tts import MiniMaxTTSProvider

    monkeypatch.setattr(cfgmod, "get_active_tts_provider", lambda: None)
    monkeypatch.setattr(cfgmod.settings, "ACTIVE_TTS_MODEL", "MiniMax-speech-02")

    p = MiniMaxTTSProvider()
    assert p.model == "MiniMax-speech-02"
    assert p._internal_model == "speech-02"


def test_c5_activated_branch_also_strips_prefix(monkeypatch):
    from backend.app.ai.providers.minimax.tts import MiniMaxTTSProvider

    p = MiniMaxTTSProvider(api_key="k", base_url="http://x", model="MiniMax-speech-01")
    assert p._internal_model == "speech-01"


# ---------------------------------------------------------------------
# C-3 配置指纹缓存
# ---------------------------------------------------------------------
def test_c3_fingerprint_tracks_tts_settings(monkeypatch):
    from backend.app.ai import factory as aifact
    from backend.app.core.config import settings

    fp1 = aifact._tts_config_fingerprint()
    monkeypatch.setattr(
        settings, "DOUBAO_TTS_RPM_LIMIT", int(settings.DOUBAO_TTS_RPM_LIMIT) + 7,
    )
    fp2 = aifact._tts_config_fingerprint()
    assert fp1 != fp2, "改动 DOUBAO_* 配置应使指纹变化"

    # 无关配置不应影响指纹（避免无谓重建）
    fp3 = aifact._tts_config_fingerprint()
    monkeypatch.setattr(settings, "LOG_LEVEL", "DEBUG")
    assert aifact._tts_config_fingerprint() == fp3, "日志级别不应影响 TTS 实例指纹"


def test_c3_config_change_rebuilds_instance_without_manual_clear(monkeypatch):
    from backend.app.ai import factory as aifact
    from backend.app.core.config import settings
    from backend.app.ai.providers.doubao.tts import (
        DoubaoTTSProvider,
        DoubaoTTSProviderV3,
    )

    # 关掉 mock 注入，直连 Registry
    monkeypatch.setattr(aifact, "_tts_instance", None)
    aifact.invalidate_tts_cache()
    prev = settings.DOUBAO_TTS_USE_V3
    try:
        settings.DOUBAO_TTS_USE_V3 = True
        inst_v3 = aifact.get_tts("doubao")
        assert isinstance(inst_v3, DoubaoTTSProviderV3)

        # 关键：**不手工清缓存**，仅改配置 → 必须拿到新类型实例
        settings.DOUBAO_TTS_USE_V3 = False
        inst_v1 = aifact.get_tts("doubao")
        assert isinstance(inst_v1, DoubaoTTSProvider), (
            f"C-3：配置变更后应重建实例，实际仍为 {type(inst_v1).__name__}"
        )
        assert inst_v1 is not inst_v3
    finally:
        settings.DOUBAO_TTS_USE_V3 = prev
        aifact.invalidate_tts_cache()


def test_c3_same_config_reuses_same_instance(monkeypatch):
    from backend.app.ai import factory as aifact

    monkeypatch.setattr(aifact, "_tts_instance", None)
    aifact.invalidate_tts_cache()
    try:
        a = aifact.get_tts("doubao")
        b = aifact.get_tts("doubao")
        assert a is b, "配置未变时应复用同一实例"
    finally:
        aifact.invalidate_tts_cache()


# ---------------------------------------------------------------------
# C-4 永久性错误不重试
# ---------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers: dict[str, str] = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeClient:
    def __init__(self, owner: "_FakeHTTP"):
        self._owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self._owner.calls.append((url, kw))
        return self._owner.responder(len(self._owner.calls))


class _FakeHTTP:
    """替换 minimax.tts 模块里的 httpx：只保留 Timeout + AsyncClient 两处用法。"""

    Timeout = httpx.Timeout

    def __init__(self, responder):
        self.responder = responder
        self.calls: list = []

    def AsyncClient(self, **_kw):
        return _FakeClient(self)


def _patch_fast(monkeypatch, mtts):
    """跳过真实 RPM 限流等待与指数退避 sleep，让重试计数快速可测。"""
    async def _noop():
        return None

    monkeypatch.setattr(mtts, "_rpm_wait_acquire", _noop)

    class _AsyncioShim:
        Lock = asyncio.Lock

        @staticmethod
        async def sleep(_s):
            await asyncio.sleep(0)

    monkeypatch.setattr(mtts, "asyncio", _AsyncioShim)


@pytest.mark.asyncio
async def test_c4_permanent_biz_code_is_not_retried(monkeypatch):
    from backend.app.ai.providers.minimax import tts as mtts

    _patch_fast(monkeypatch, mtts)
    fake = _FakeHTTP(lambda n: _FakeResp(
        200, {"base_resp": {"status_code": 1004, "status_msg": "鉴权失败"}, "data": {}},
    ))
    monkeypatch.setattr(mtts, "httpx", fake)

    p = mtts.MiniMaxTTSProvider(api_key="k", base_url="http://x")
    with pytest.raises(mtts.MiniMaxTTSError) as ei:
        await p.synthesize_to_bytes("你好", "minimax:female-tianmei")

    assert ei.value.retryable is False
    assert ei.value.code == 1004
    assert len(fake.calls) == 1, (
        f"C-4：永久业务码不应重试，实际请求 {len(fake.calls)} 次"
    )


@pytest.mark.asyncio
async def test_c4_retryable_biz_code_is_retried(monkeypatch):
    from backend.app.ai.providers.minimax import tts as mtts

    _patch_fast(monkeypatch, mtts)
    fake = _FakeHTTP(lambda n: _FakeResp(
        200, {"base_resp": {"status_code": 1013, "status_msg": "服务内部错误"}, "data": {}},
    ))
    monkeypatch.setattr(mtts, "httpx", fake)

    p = mtts.MiniMaxTTSProvider(api_key="k", base_url="http://x")
    with pytest.raises(mtts.MiniMaxTTSError) as ei:
        await p.synthesize_to_bytes("你好", "minimax:female-tianmei")

    assert ei.value.retryable is True
    assert len(fake.calls) == 5, (
        f"C-4：可重试业务码应重试到上限，实际请求 {len(fake.calls)} 次"
    )


@pytest.mark.asyncio
async def test_c4_http_4xx_is_not_retried(monkeypatch):
    from backend.app.ai.providers.minimax import tts as mtts

    _patch_fast(monkeypatch, mtts)
    fake = _FakeHTTP(lambda n: _FakeResp(
        400, {"base_resp": {"status_msg": "invalid params"}},
    ))
    monkeypatch.setattr(mtts, "httpx", fake)

    p = mtts.MiniMaxTTSProvider(api_key="k", base_url="http://x")
    with pytest.raises(mtts.MiniMaxTTSError) as ei:
        await p.synthesize_to_bytes("你好", "minimax:female-tianmei")

    assert ei.value.retryable is False
    assert len(fake.calls) == 1, (
        f"C-4：HTTP 4xx 属永久错误，不应重试，实际请求 {len(fake.calls)} 次"
    )


@pytest.mark.asyncio
async def test_c4_http_5xx_is_retried(monkeypatch):
    from backend.app.ai.providers.minimax import tts as mtts

    _patch_fast(monkeypatch, mtts)
    fake = _FakeHTTP(lambda n: _FakeResp(500, text="server error"))
    monkeypatch.setattr(mtts, "httpx", fake)

    p = mtts.MiniMaxTTSProvider(api_key="k", base_url="http://x")
    with pytest.raises(Exception):
        await p.synthesize_to_bytes("你好", "minimax:female-tianmei")

    assert len(fake.calls) == 5, (
        f"C-4：HTTP 5xx 应可重试，实际请求 {len(fake.calls)} 次"
    )
