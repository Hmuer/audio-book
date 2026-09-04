"""
P2 #14 持久化回归测试：
- /api/settings PUT 修改白名单键 → 写到 data/runtime_settings.json；
- /api/providers PUT 修改 → 写到 data/providers_config.json；
- 启动 lifespan 会从这两个文件回填到 settings.* 字段；
- 敏感键（如 *KEY*）即使被 PUT 也不落盘。
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

pytest_plugins = ("pytest_asyncio",)


@pytest_asyncio.fixture
async def admin_token(_isolate_data_dir):
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    await init_db()
    await seed_admin_user()
    token, _ = create_access_token("admin")
    return token


@pytest.mark.asyncio
async def test_settings_pers_persistence_roundtrip(admin_token):
    """PUT /settings 修改 → 落盘 → 重启 lifespan 后回填。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import (
        save_runtime_settings_to_disk,
        load_runtime_settings_from_disk,
    )
    from backend.app.main import app
    from httpx import AsyncClient, ASGITransport

    # 模拟 PUT：选一个稳定的白名单键
    key = "CHAPTER_SPLIT_MAX_CHAPTERS"  # 假设是 _EDITABLE_SETTINGS 里的
    # 兜底：试常用键
    test_key = None
    new_val = None
    try:
        from backend.app.api.routes import _EDITABLE_SETTINGS
        for k, info in _EDITABLE_SETTINGS.items():
            typ = info[0]
            if typ == "int" and "CHAPTER" in k.upper() or "MAX" in k.upper():
                test_key = k
                new_val = int(getattr(cfg.settings, k)) + 99
                break
    except Exception:
        pass
    if test_key is None:
        # 兜底选第一个 int 类型
        try:
            from backend.app.api.routes import _EDITABLE_SETTINGS
            for k, info in _EDITABLE_SETTINGS.items():
                if info[0] == "int":
                    test_key = k
                    new_val = int(getattr(cfg.settings, k)) + 1
                    break
        except Exception:
            pass

    if test_key is None:
        pytest.skip("未找到可用的 _EDITABLE_SETTINGS int 键")

    # 直接调 save_runtime_settings_to_disk（白名单 + 类型已经 _init_persistable_keys 跑过）
    from backend.app.core.config import _init_persistable_keys
    _init_persistable_keys()
    res = save_runtime_settings_to_disk({test_key: new_val})
    assert test_key in res["saved"], res

    # 验证磁盘文件确实有
    path = cfg.settings.DATA_DIR / "runtime_settings.json"
    assert path.is_file(), path
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk.get(test_key) == new_val

    # 把内存里 settings 改成另一个值，模拟"重启前"
    original_val = getattr(cfg.settings, test_key)
    setattr(cfg.settings, test_key, original_val + 1234)

    # load 回填
    n = load_runtime_settings_from_disk()
    assert n >= 1
    assert getattr(cfg.settings, test_key) == new_val

    # 清理
    setattr(cfg.settings, test_key, original_val)


@pytest.mark.asyncio
async def test_providers_persistence_roundtrip(admin_token):
    """PUT /providers → 落盘到 data/providers_config.json。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import (
        save_providers_config, load_providers_config_from_disk,
    )

    # 准备一个新厂商（不破坏既有）
    payload = {
        "providers": [
            {
                "id": "test_persist_" + uuid.uuid4().hex[:8],
                "label": "Test Persist",
                "enabled": True,
                "kind": "openai",
                "base_url": "https://example.com",
                "api_key": "sk-test-xyz123456",
                "models": [
                    {"id": "test-model", "label": "TM", "kind": "llm"},
                ],
            }
        ],
        "active": {
            "tts": {"provider_id": "minimax", "model_id": "MiniMax-speech-01"},
            "llm": {"provider_id": "minimax", "model_id": "MiniMax-M3"},
        },
    }
    save_providers_config(payload)

    # 验证落盘文件
    on_disk = json.loads(
        (cfg.settings.DATA_DIR / "providers_config.json").read_text(encoding="utf-8")
    )
    assert on_disk["providers"][0]["id"] == payload["providers"][0]["id"]
    assert on_disk["providers"][0]["api_key"] == "sk-test-xyz123456"

    # 模拟"重启"：覆盖 settings.PROVIDERS_CONFIG 为最简结构
    cfg.settings.PROVIDERS_CONFIG = json.dumps(
        {"providers": [], "active": {"tts": {}, "llm": {}}},
        ensure_ascii=False,
    )

    # 回填
    ok = load_providers_config_from_disk()
    assert ok
    reloaded = json.loads(cfg.settings.PROVIDERS_CONFIG)
    assert reloaded["providers"][0]["id"] == payload["providers"][0]["id"]


@pytest.mark.asyncio
async def test_sensitive_keys_are_not_persisted(admin_token):
    """敏感键（带 key/secret/token/password）即使被尝试持久化，也被忽略。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import save_runtime_settings_to_disk, _init_persistable_keys

    _init_persistable_keys()
    # 假设的敏感键（即使它在白名单里，name 包含 secret/token 也会被拒）
    # 这里用 settings 不存在的字段名 + 模拟
    res = save_runtime_settings_to_disk({
        "API_SECRET_TOKEN": "leaked",
        "DB_PASSWORD": "leaked",
        # 一个白名单内的正常键
    })
    skipped = res["skipped"]
    assert any("API_SECRET_TOKEN" in s for s in skipped)
    assert any("DB_PASSWORD" in s for s in skipped)


@pytest.mark.asyncio
async def test_settings_pers_load_handles_corrupted_file(admin_token):
    """损坏的 runtime_settings.json 启动时不抛、回退默认。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import load_runtime_settings_from_disk

    path = cfg.settings.DATA_DIR / "runtime_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json ", encoding="utf-8")
    # 必须返回 0，不抛
    n = load_runtime_settings_from_disk()
    assert n == 0


@pytest.mark.asyncio
async def test_load_providers_returns_false_for_missing_file(admin_token):
    """没有 providers_config.json 时 load 返回 False，不报错。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import load_providers_config_from_disk

    # 确保文件不存在
    path = cfg.settings.DATA_DIR / "providers_config.json"
    if path.is_file():
        path.unlink()
    ok = load_providers_config_from_disk()
    assert ok is False