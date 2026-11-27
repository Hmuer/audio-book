"""多厂商 LLM 通用化 + 思考模式开关测试。

- 任意激活的 OpenAI 兼容厂商（如 magpie group/tiered）都能驱动同一个 LLM provider；
- 请求体：MiniMax 用 json_schema + thinking disabled；其它厂商用 json_object + enable_thinking；
- 思考开关默认关闭（thinking=False → enable_thinking=False / minimax thinking=disabled）。
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel


class _R(BaseModel):
    ok: bool = True


class _FakeResp:
    status_code = 200
    headers: dict = {}
    text = '{"choices":[{"message":{"content":"{\\"ok\\": true}"},"finish_reason":"stop"}],' \
           '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'

    def json(self):
        return json.loads(self.text)


def _install_capture(monkeypatch, llm_mod):
    captured = {}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            captured["headers"] = kw.get("headers")
            return _FakeResp()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    return captured


def _cfg(provider_id: str, model_id: str, base_url: str, thinking: bool, key="sk-test"):
    return json.dumps({
        "providers": [{
            "id": provider_id, "label": provider_id, "enabled": True,
            "api_key": key, "base_url": base_url,
            "models": [{"id": model_id, "label": model_id, "kind": "llm", "thinking": thinking}],
        }],
        "active": {"llm": {"provider_id": provider_id, "model_id": model_id}},
    }, ensure_ascii=False)


@pytest.mark.asyncio
async def test_magpie_openai_compat_thinking_off(monkeypatch):
    from backend.app.ai.providers.minimax import llm as llm_mod
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PROVIDERS_CONFIG", _cfg(
        "magpie", "group/tiered", "https://magpie.ls-lh.cn/v1", thinking=False))
    monkeypatch.setattr(settings, "ACTIVE_LLM_PROVIDER", "magpie")
    monkeypatch.setattr(settings, "ACTIVE_LLM_MODEL", "group/tiered")
    llm_mod.reset_quota_breaker(); llm_mod.reset_rate_cap()
    captured = _install_capture(monkeypatch, llm_mod)

    prov = llm_mod.MiniMaxLLMProvider()
    assert prov.provider_id == "magpie"
    assert prov.base_url == "https://magpie.ls-lh.cn/v1"
    assert prov.model_pro == "group/tiered"
    assert prov.thinking_enabled is False

    out = await prov.chat_structured("返回 ok", _R, max_retries=1)
    assert out.ok is True
    body = captured["json"]
    assert body["model"] == "group/tiered"
    assert body["response_format"]["type"] == "json_object"
    assert body["enable_thinking"] is False
    assert "thinking" not in body and "reasoning_split" not in body
    assert captured["url"].endswith("/v1/chat/completions")


@pytest.mark.asyncio
async def test_magpie_thinking_on(monkeypatch):
    from backend.app.ai.providers.minimax import llm as llm_mod
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PROVIDERS_CONFIG", _cfg(
        "magpie", "group/tiered", "https://magpie.ls-lh.cn/v1", thinking=True))
    monkeypatch.setattr(settings, "ACTIVE_LLM_PROVIDER", "magpie")
    monkeypatch.setattr(settings, "ACTIVE_LLM_MODEL", "group/tiered")
    llm_mod.reset_quota_breaker(); llm_mod.reset_rate_cap()
    captured = _install_capture(monkeypatch, llm_mod)

    prov = llm_mod.MiniMaxLLMProvider()
    assert prov.thinking_enabled is True
    await prov.chat_structured("返回 ok", _R, max_retries=1)
    assert captured["json"]["enable_thinking"] is True


@pytest.mark.asyncio
async def test_minimax_json_schema_and_thinking_disabled(monkeypatch):
    from backend.app.ai.providers.minimax import llm as llm_mod
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PROVIDERS_CONFIG", _cfg(
        "minimax", "MiniMax-M3", "https://api.minimaxi.com/v1", thinking=False))
    monkeypatch.setattr(settings, "ACTIVE_LLM_PROVIDER", "minimax")
    monkeypatch.setattr(settings, "ACTIVE_LLM_MODEL", "MiniMax-M3")
    llm_mod.reset_quota_breaker(); llm_mod.reset_rate_cap()
    captured = _install_capture(monkeypatch, llm_mod)

    prov = llm_mod.MiniMaxLLMProvider()
    assert prov.provider_id == "minimax"
    await prov.chat_structured("返回 ok", _R, max_retries=1)
    body = captured["json"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_split" in body
    assert "enable_thinking" not in body
