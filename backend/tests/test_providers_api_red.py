"""多厂商 API 端到端测试。

覆盖：
- GET /api/providers 返回默认 minimax + doubao 厂商
- GET /api/providers active 字段正确指向 minimax 默认模型
- PUT /api/providers 完整保存：新建厂商 / 切换激活 / 新增模型
- PUT /api/providers 校验：active.provider_id 不存在 → 400
- PUT /api/providers 校验：active.model_id 不存在 → 400
- PUT /api/providers 校验：active model kind 不匹配 → 400
- 一厂商多模型（同一 provider 下多个 tts/llm）可独立激活
- 一厂商共用一个 api_key（tts 和 llm 用同一 key）
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app as _app
from app.core.config import list_providers, save_providers_config, _parse_providers_config


@pytest.fixture()
def client_and_state():
    """用 TestClient + 隔离/恢复 PROVIDERS_CONFIG 状态。"""
    original = _parse_providers_config()
    with TestClient(_app) as client:
        yield client, original
    # 测试后恢复
    save_providers_config(original)


def _login(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_get_providers_default_shape(client_and_state):
    """GET /api/providers 返回默认 minimax + doubao + active 默认指向 minimax。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    providers = body["providers"]
    ids = {p["id"] for p in providers}
    assert "minimax" in ids
    assert "doubao" in ids
    # 默认激活：minimax + speech-01（tts） + MiniMax-M3（llm）
    assert body["active"]["tts"]["provider_id"] == "minimax"
    assert body["active"]["tts"]["model_id"] == "MiniMax-speech-01"
    assert body["active"]["llm"]["provider_id"] == "minimax"
    assert body["active"]["llm"]["model_id"] == "MiniMax-M3"


def test_put_providers_add_new_model_and_switch_active(client_and_state):
    """PUT：给 minimax 加自定义 LLM + 切换 llm active 到它。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()

    # 在 minimax.models 里加一个自定义 llm
    cfg["providers"][0]["models"].append(
        {"id": "minimax-custom-llm", "label": "My Custom LLM", "kind": "llm"}
    )
    cfg["active"]["llm"] = {"provider_id": "minimax", "model_id": "minimax-custom-llm"}
    r = client.put("/api/providers", headers=_auth(token), json=cfg)
    assert r.status_code == 200, r.text

    # 再 GET 验证
    r = client.get("/api/providers", headers=_auth(token))
    body = r.json()
    assert body["active"]["llm"] == {"provider_id": "minimax", "model_id": "minimax-custom-llm"}
    ids = {m["id"] for m in body["providers"][0]["models"]}
    assert "minimax-custom-llm" in ids


def test_put_providers_switch_tts_to_doubao(client_and_state):
    """PUT：切换 tts active 到 doubao/volcano_tts。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()

    # 启用 doubao + 给个 api_key
    cfg["providers"][1]["enabled"] = True
    cfg["providers"][1]["api_key"] = "test-doubao-key"
    cfg["active"]["tts"] = {"provider_id": "doubao", "model_id": "volcano_tts"}
    r = client.put("/api/providers", headers=_auth(token), json=cfg)
    assert r.status_code == 200, r.text

    r = client.get("/api/providers", headers=_auth(token))
    body = r.json()
    assert body["active"]["tts"]["provider_id"] == "doubao"
    assert body["active"]["tts"]["model_id"] == "volcano_tts"


def test_put_providers_reject_nonexistent_provider(client_and_state):
    """PUT：active.provider_id 指向不存在的厂商 → 400。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()
    cfg["active"]["tts"] = {"provider_id": "openai", "model_id": "gpt-4"}
    r = client.put("/api/providers", headers=_auth(token), json=cfg)
    assert r.status_code == 400
    assert "openai" in r.json()["detail"]


def test_put_providers_reject_nonexistent_model(client_and_state):
    """PUT：active.model_id 不在厂商 models 列表中 → 400。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()
    cfg["active"]["tts"] = {"provider_id": "minimax", "model_id": "no-such-model"}
    r = client.put("/api/providers", headers=_auth(token), json=cfg)
    assert r.status_code == 400
    assert "no-such-model" in r.json()["detail"]


def test_put_providers_reject_kind_mismatch(client_and_state):
    """PUT：active.tts 指向一个 kind=llm 的 model → 400。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()
    # MiniMax-M3 是 llm 模型，强行让 tts 激活它
    cfg["active"]["tts"] = {"provider_id": "minimax", "model_id": "MiniMax-M3"}
    r = client.put("/api/providers", headers=_auth(token), json=cfg)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "MiniMax-M3" in detail
    assert "kind" in detail or "不匹配" in detail


def test_one_provider_one_apikey_shared_by_tts_and_llm(client_and_state):
    """一厂商只需一个 api_key，TTS/LLM 共用（Cherry Studio 风格契约）。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()

    # 验证 minimax 的 api_key 字段就是单一字段（不是 api_key_tts / api_key_llm）
    minimax = next(p for p in cfg["providers"] if p["id"] == "minimax")
    assert "api_key" in minimax
    # 没有 api_key_tts / api_key_llm 这种分离字段
    assert "api_key_tts" not in minimax
    assert "api_key_llm" not in minimax


def test_multiple_models_in_one_provider(client_and_state):
    """一个厂商下可配置多个 tts/llm 模型，并可切换激活。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()

    # 给 minimax 加多个 tts 模型 + 多个 llm 模型
    cfg["providers"][0]["models"].extend([
        {"id": "minimax-tts-v2", "label": "Speech v2", "kind": "tts"},
        {"id": "minimax-llm-fast", "label": "Fast LLM", "kind": "llm"},
    ])
    # 切换 tts 激活到新模型
    cfg["active"]["tts"] = {"provider_id": "minimax", "model_id": "minimax-tts-v2"}
    cfg["active"]["llm"] = {"provider_id": "minimax", "model_id": "minimax-llm-fast"}
    r = client.put("/api/providers", headers=_auth(token), json=cfg)
    assert r.status_code == 200, r.text

    r = client.get("/api/providers", headers=_auth(token))
    body = r.json()
    minimax = next(p for p in body["providers"] if p["id"] == "minimax")
    model_ids = {m["id"] for m in minimax["models"]}
    assert {"minimax-tts-v2", "minimax-llm-fast"}.issubset(model_ids)
    assert body["active"]["tts"]["model_id"] == "minimax-tts-v2"
    assert body["active"]["llm"]["model_id"] == "minimax-llm-fast"


def test_put_providers_rejects_missing_providers_field(client_and_state):
    """PUT 请求体缺少 providers 字段 → 400。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.put("/api/providers", headers=_auth(token), json={"active": {"tts": {}, "llm": {}}})
    assert r.status_code == 400


def test_put_providers_requires_provider_id(client_and_state):
    """PUT：providers 数组里有项缺 id → 400。"""
    client, _ = client_and_state
    token = _login(client)
    r = client.get("/api/providers", headers=_auth(token))
    cfg = r.json()
    cfg["providers"].append({"label": "no-id-provider", "enabled": False, "api_key": "", "models": []})
    r = client.put("/api/providers", headers=_auth(token), json=cfg)
    assert r.status_code == 400
    assert "id" in r.json()["detail"]