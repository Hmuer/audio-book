"""P1-6 RED — X-Tt-Logid 全链路透传测试。

覆盖范围（按 P1-6 checklist）：
  T-L1  _extract_logid_from_exc 直接从异常 .logid 取到
  T-L2  _extract_logid_from_exc 沿 __cause__ 链向上找
  T-L3  _extract_logid_from_exc 异常无 logid → None
  T-L4  _http_exc_with_logid 异常带 logid → HTTPException.detail 是 dict 含 logid
  T-L5  _http_exc_with_logid 异常无 logid → HTTPException.detail 是纯字符串
  T-L6  v1 provider 业务错 → final RuntimeError.logid 透传
  T-L7  v3 provider 业务错（响应头带 X-Tt-Logid）→ final RuntimeError.logid 透传
  T-L8  ICL client create_training 业务错 → RuntimeError.logid 透传
  T-L9  端到端：POST /api/icl/voices 5xx 响应 detail 含 logid 字段
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# =====================================================================
# T-L1 / T-L2 / T-L3: _extract_logid_from_exc
# =====================================================================
def test_l1_extract_logid_from_exc_direct_attr():
    """异常对象直接带 .logid 属性 → 立刻返回。"""
    from backend.app.api.routes import _extract_logid_from_exc

    e = RuntimeError("boom")
    e.logid = "202609091100-abc-DEF"  # type: ignore[attr-defined]
    assert _extract_logid_from_exc(e) == "202609091100-abc-DEF"


def test_l2_extract_logid_from_exc_via_cause_chain():
    """异常本身无 logid，但 __cause__ 链上有 → 递归取到。

    真实场景：provider 抛 RuntimeError(logid=...)，routes 层把它包成
    ValueError("...") from provider_exc，最终 HTTP 层从 ValueError 链里仍能取出。
    """
    from backend.app.api.routes import _extract_logid_from_exc

    inner = RuntimeError("provider 失败")
    inner.logid = "cause-logid-123"  # type: ignore[attr-defined]
    # 正确构造 __cause__：用 raise ... from ...
    try:
        raise ValueError("上层包装") from inner
    except ValueError as outer_exc:
        assert _extract_logid_from_exc(outer_exc) == "cause-logid-123"


def test_l3_extract_logid_from_exc_no_logid_returns_none():
    """普通异常无 logid 属性、无 __cause__ → 返回 None。"""
    from backend.app.api.routes import _extract_logid_from_exc

    e = RuntimeError("plain boom")
    assert _extract_logid_from_exc(e) is None

    # 带 __cause__ 但 cause 也无 logid → 仍 None
    try:
        raise ValueError("outer") from RuntimeError("inner no logid")
    except ValueError as e2:
        assert _extract_logid_from_exc(e2) is None


# =====================================================================
# T-L4 / T-L5: _http_exc_with_logid
# =====================================================================
def test_l4_http_exc_with_logid_includes_logid_in_detail():
    """异常带 logid → HTTPException.detail 是 dict，含 message + logid 字段。"""
    from fastapi import HTTPException

    from backend.app.api.routes import _http_exc_with_logid

    e = RuntimeError("豆包 500")
    e.logid = "log-xyz"  # type: ignore[attr-defined]
    http_exc = _http_exc_with_logid(502, "TTS 失败: RuntimeError", e)
    assert isinstance(http_exc, HTTPException)
    assert http_exc.status_code == 502
    # detail 必须是 dict 且同时含 message 和 logid
    assert isinstance(http_exc.detail, dict)
    assert http_exc.detail["message"] == "TTS 失败: RuntimeError"
    assert http_exc.detail["logid"] == "log-xyz"


def test_l5_http_exc_with_logid_without_logid_is_plain_string():
    """异常无 logid → HTTPException.detail 保持原样（纯字符串）。"""
    from fastapi import HTTPException

    from backend.app.api.routes import _http_exc_with_logid

    http_exc = _http_exc_with_logid(500, "普通失败", RuntimeError("no logid here"))
    assert isinstance(http_exc, HTTPException)
    assert http_exc.status_code == 500
    assert http_exc.detail == "普通失败"


# =====================================================================
# T-L6: v1 provider 业务错 → final RuntimeError.logid 透传
# =====================================================================
@pytest.mark.asyncio
async def test_l6_v1_provider_business_error_propagates_logid(monkeypatch):
    """v1 路径：_post_json_for_v1 抛 DoubaoTTSResponseError(code, logid=...)，
    provider 包装成 final RuntimeError，仍应带 logid。
    """
    from backend.app.ai.providers.doubao import tts as tts_mod
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "test-ak")
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "BASE_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(tts_mod.DoubaoTTSProvider, "JITTER_SECS", 0.0)

    async def _fake_post_json(url, headers, payload, **_kw):
        raise tts_mod.DoubaoTTSResponseError(
            "业务码非 0", code=4001, logid="v1-log-aaa",
        )

    monkeypatch.setattr(tts_mod, "_post_json_for_v1", _fake_post_json)

    provider = tts_mod.DoubaoTTSProvider()
    with pytest.raises(RuntimeError) as ei:
        await provider.synthesize_to_bytes("你好", "BV001_streaming")

    err = ei.value
    # logid 应透传到 final RuntimeError 上
    assert getattr(err, "logid", None) == "v1-log-aaa"
    # 异常消息里也应带 logid 字样，便于日志检索
    assert "logid=v1-log-aaa" in str(err)


# =====================================================================
# T-L7: v3 provider 业务错 → final RuntimeError.logid 透传
# =====================================================================
@pytest.mark.asyncio
async def test_l7_v3_provider_business_error_propagates_logid(monkeypatch):
    """v3 路径：响应头带 X-Tt-Logid + 业务码非 0 → final RuntimeError 带 logid。"""
    from backend.app.ai.providers.doubao.tts import (
        DoubaoTTSProviderV3,
        DoubaoTTSResponseV3Error,
    )
    from backend.app.core import config as cfgmod

    p = DoubaoTTSProviderV3()
    p._resolve_api_key = lambda: "fake"
    monkeypatch.setattr(
        cfgmod.settings,
        "DOUBAO_TTS_V3_BASE_URL",
        "https://example.test/v3/tts/unidirectional",
    )

    chunked_lines = json.dumps({"code": 401, "message": "鉴权失败"}).encode("utf-8")

    class _FakeResp:
        status_code = 200
        # 关键：响应头带 X-Tt-Logid
        headers = {"X-Tt-Logid": "v3-log-bbb"}

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

    with pytest.raises(RuntimeError) as ei:
        await p.synthesize_to_bytes("hi", "doubao:BV001_streaming")

    err = ei.value
    # 业务错路径直接 re-raise DoubaoTTSResponseV3Error（继承自 RuntimeError），
    # logid 在 .logid 属性上（不在 message 里，message 仅含 code/msg）。
    # routes.py 的 _extract_logid_from_exc 取的是 .logid 属性，所以这里断言属性即可。
    assert getattr(err, "logid", None) == "v3-log-bbb"
    # 异常类应是 DoubaoTTSResponseV3Error（确认走的是业务错分支而非网络错 final 包装）
    from backend.app.ai.providers.doubao.tts import DoubaoTTSResponseV3Error
    assert isinstance(err, DoubaoTTSResponseV3Error)


# =====================================================================
# T-L8: ICL client create_training 业务错 → RuntimeError.logid 透传
# =====================================================================
@pytest.mark.asyncio
async def test_l8_icl_client_create_training_business_error_carries_logid():
    """ICL create_training：fake _http_post_json 返回 {code:5001, _logid:...}
    → 抛 RuntimeError 且 .logid 透传。
    """
    from backend.app.ai.providers.doubao.icl import DoubaoICLClient

    client = DoubaoICLClient()

    async def _fake_post(url, payload, **_kwargs):
        return {
            "code": 5001,
            "message": "音频太短",
            "_logid": "icl-log-ccc",
        }

    client._http_post_json = _fake_post  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as ei:
        await client.create_training("test-voice", b"\x00" * 1024)

    err = ei.value
    assert getattr(err, "logid", None) == "icl-log-ccc"
    assert "logid=icl-log-ccc" in str(err)


# =====================================================================
# T-L9: 端到端 — POST /api/icl/voices 5xx 响应 detail 含 logid
# =====================================================================
@pytest.mark.asyncio
async def test_l9_icl_route_500_includes_logid_in_detail(_isolate_data_dir, monkeypatch):
    """端到端：mock start_icl_training 抛 RuntimeError(logid=...)，
    验证 HTTP 500 响应 detail 是 dict 含 message + logid。
    """
    from fastapi.testclient import TestClient

    # 用 monkeypatch 让 start_icl_training 抛带 logid 的 RuntimeError。
    # routes.py 内部用 `from ..services.icl import start_icl_training` 延迟导入，
    # 当 app 以 `app.main` 加载时 `..services.icl` 解析为 `app.services.icl`；
    # 当以 `backend.app.main` 加载时解析为 `backend.app.services.icl`。
    # 两个命名空间都要 patch（与 conftest.py 处理 rate_limit 的策略一致）。
    async def _fake_start(**kwargs):
        err = RuntimeError("豆包 ICL 训练失败")
        err.logid = "e2e-log-ddd"  # type: ignore[attr-defined]
        raise err

    import sys as _sys
    # 显式 import 触发模块加载，确保 sys.modules 里有 key 可 patch
    import importlib
    for mod_name in ("backend.app.services.icl", "app.services.icl"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        monkeypatch.setattr(mod, "start_icl_training", _fake_start, raising=False)
    # 兜底：若 app.services.icl 此刻还没加载，先注册 backend 版本，
    # 等 TestClient 启动触发 app.* 加载时，下面的 import 也已就绪
    _b_svc = _sys.modules.get("backend.app.services.icl")
    if _b_svc is not None:
        monkeypatch.setattr(_b_svc, "start_icl_training", _fake_start, raising=False)

    # DB seed admin 用户 + 取 token
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    await init_db()
    await seed_admin_user()
    token, _ = create_access_token("admin")

    from app.main import app as _app
    with TestClient(_app) as client:
        # POST /api/icl/voices：multipart form
        r = client.post(
            "/api/icl/voices",
            headers={"Authorization": f"Bearer {token}"},
            data={"voice_name": "logid-e2e"},
            files={"file": ("a.mp3", b"\x00" * 1024, "audio/mpeg")},
        )
    assert r.status_code == 500, r.text
    body = r.json()
    # detail 应是 dict（FastAPI 把 HTTPException.detail 序列化成 {"detail": ...}）
    detail = body.get("detail") if isinstance(body, dict) and "detail" in body else body
    assert isinstance(detail, dict), f"detail 应为 dict，实际：{detail!r}"
    assert detail.get("logid") == "e2e-log-ddd"
    assert "训练任务失败" in detail.get("message", "")
