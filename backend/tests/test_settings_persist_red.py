"""
P2 #14 持久化回归测试（升级版：runtime_settings → app.db app_settings 表）：
- /api/settings PUT 修改白名单键 → 写到 app.db app_settings 表；
- 启动 lifespan 会从 app_settings 表回填到 settings.* 字段；
- 旧 data/runtime_settings.json 会被一次性导入并删除；
- 敏感键（如 *KEY*）现在允许持久化（app.db chmod 0600）；
- DB 文件权限 0600。
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
    """PUT /settings 修改 → 落 DB → 重启 lifespan 后从 DB 回填。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import (
        save_runtime_settings_async,
        load_runtime_settings_async,
    )
    from sqlalchemy import select

    # 模拟 PUT：选一个稳定的白名单键
    test_key = None
    new_val = None
    try:
        from backend.app.api.routes import _EDITABLE_SETTINGS
        for k, info in _EDITABLE_SETTINGS.items():
            typ = info[0]
            if typ == "int" and ("CHAPTER" in k.upper() or "MAX" in k.upper()):
                test_key = k
                new_val = int(getattr(cfg.settings, k)) + 99
                break
    except Exception:
        pass
    if test_key is None:
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

    from backend.app.core.config import _init_persistable_keys
    _init_persistable_keys()
    res = await save_runtime_settings_async({test_key: new_val})
    assert test_key in res["saved"], res

    # 验证 DB 里确实有
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import AppSetting

    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(select(AppSetting).where(AppSetting.key == test_key))
        ).scalar_one_or_none()
    assert row is not None, f"{test_key} 未写入 app_settings 表"
    assert json.loads(row.value) == new_val

    # 验证旧的 JSON 文件不再被使用（如果存在，不应包含本测试的新值）
    legacy_path = cfg.settings.DATA_DIR / "runtime_settings.json"
    if legacy_path.is_file():
        on_disk = json.loads(legacy_path.read_text(encoding="utf-8"))
        assert on_disk.get(test_key) != new_val, (
            "新版应只写 DB，旧 JSON 文件不应再有新值"
        )

    # 把内存里 settings 改成另一个值，模拟"重启前"
    original_val = getattr(cfg.settings, test_key)
    setattr(cfg.settings, test_key, original_val + 1234)

    # load 回填
    n = await load_runtime_settings_async()
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

    on_disk = json.loads(
        (cfg.settings.DATA_DIR / "providers_config.json").read_text(encoding="utf-8")
    )
    assert on_disk["providers"][0]["id"] == payload["providers"][0]["id"]
    assert on_disk["providers"][0]["api_key"] == "sk-test-xyz123456"

    cfg.settings.PROVIDERS_CONFIG = json.dumps(
        {"providers": [], "active": {"tts": {}, "llm": {}}},
        ensure_ascii=False,
    )

    ok = load_providers_config_from_disk()
    assert ok
    reloaded = json.loads(cfg.settings.PROVIDERS_CONFIG)
    assert reloaded["providers"][0]["id"] == payload["providers"][0]["id"]


@pytest.mark.asyncio
async def test_sensitive_keys_are_now_persisted(admin_token):
    """P3 升级：敏感键（如 *API_KEY*）现在允许持久化到 DB（用户选择）。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import (
        save_runtime_settings_async,
        _init_persistable_keys,
    )
    from sqlalchemy import select
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import AppSetting

    _init_persistable_keys()

    # TTS_API_KEY 确实是 _EDITABLE_SETTINGS 白名单里的 str 键（types=str）
    secret_key = "TTS_API_KEY"
    from backend.app.api.routes import _EDITABLE_SETTINGS
    assert secret_key in _EDITABLE_SETTINGS, "前提：TTS_API_KEY 应在白名单里"
    assert _EDITABLE_SETTINGS[secret_key][0] == "str"

    new_secret = "sk-new-secret-" + uuid.uuid4().hex
    original = getattr(cfg.settings, secret_key, None)

    res = await save_runtime_settings_async({secret_key: new_secret})
    assert secret_key in res["saved"], f"敏感字段应被持久化，但被 skip 了: {res}"

    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(select(AppSetting).where(AppSetting.key == secret_key))
        ).scalar_one_or_none()
    assert row is not None
    assert json.loads(row.value) == new_secret

    # 还原
    setattr(cfg.settings, secret_key, original)
    async with factory() as s:
        async with s.begin():
            from sqlalchemy import delete
            await s.execute(delete(AppSetting).where(AppSetting.key == secret_key))


@pytest.mark.asyncio
async def test_app_db_permissions_0600(admin_token):
    """init_db 后 app.db 应被 chmod 0600（含敏感字段后必须收紧）。"""
    from backend.app.core.config import settings
    from sqlalchemy.engine.url import make_url

    url = make_url(settings.DATABASE_URL)
    if not url.database:
        pytest.skip("非文件型 DB（无 database path）")
    from pathlib import Path
    p = Path(url.database)
    if not p.is_file():
        pytest.skip(f"DB 文件不存在: {p}")
    import stat
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, f"app.db 应为 0600，实际 {oct(mode)}"


@pytest.mark.asyncio
async def test_settings_pers_load_handles_empty_table(admin_token):
    """空表 / 全坏 JSON 加载时不应抛异常。"""
    from backend.app.core.config import load_runtime_settings_async
    # 空表 → 应返回 0
    n = await load_runtime_settings_async()
    assert n >= 0


@pytest.mark.asyncio
async def test_load_providers_returns_false_for_missing_file(admin_token):
    """没有 providers_config.json 时 load 返回 False，不报错。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import load_providers_config_from_disk

    path = cfg.settings.DATA_DIR / "providers_config.json"
    if path.is_file():
        path.unlink()
    ok = load_providers_config_from_disk()
    assert ok is False


# =====================================================================
# 迁移测试：旧 runtime_settings.json → app_settings → 删除 JSON
# =====================================================================

@pytest.mark.asyncio
async def test_legacy_json_migrates_to_db_and_deleted(admin_token):
    """首次启动：旧 JSON 文件应被导入到 app_settings，然后 JSON 被删。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import (
        migrate_legacy_runtime_settings_json_once,
        _init_persistable_keys,
    )
    from sqlalchemy import select
    from backend.app.db.session import get_session_factory
    from backend.app.db.models import AppSetting

    _init_persistable_keys()

    # 准备一个白名单内的键 + 一个非白名单（应被 skip）
    target_key = None
    for k in ("CHAPTER_SPLIT_MIN_MATCHES", "CHAPTER_SPLIT_MAX_CHARS",
              "CHAPTER_SPLIT_HARD_FALLBACK_MAX_CHARS"):
        from backend.app.api.routes import _EDITABLE_SETTINGS
        if k in _EDITABLE_SETTINGS:
            target_key = k
            break
    if target_key is None:
        pytest.skip("找不到稳定的 int 白名单键")

    original_val = getattr(cfg.settings, target_key)
    new_val = original_val + 7
    legacy_path = cfg.settings.DATA_DIR / "runtime_settings.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({target_key: new_val, "NON_EXIST_KEY": "should be skipped"},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    # 执行迁移
    n = await migrate_legacy_runtime_settings_json_once()
    assert n >= 1, "应至少导入 1 个白名单键"

    # 验证 DB 有
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(select(AppSetting).where(AppSetting.key == target_key))
        ).scalar_one_or_none()
    assert row is not None
    assert json.loads(row.value) == new_val

    # 验证 JSON 文件被删
    assert not legacy_path.is_file(), "迁移后旧 JSON 必须被删除"

    # 清理：删 DB 行 + 还原 settings
    async with factory() as s:
        async with s.begin():
            from sqlalchemy import delete
            await s.execute(delete(AppSetting).where(AppSetting.key == target_key))
    setattr(cfg.settings, target_key, original_val)


@pytest.mark.asyncio
async def test_legacy_json_no_file_is_noop(admin_token):
    """没有旧 JSON 文件时，迁移函数应直接返回 0 不报错。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import migrate_legacy_runtime_settings_json_once

    legacy_path = cfg.settings.DATA_DIR / "runtime_settings.json"
    if legacy_path.is_file():
        legacy_path.unlink()

    n = await migrate_legacy_runtime_settings_json_once()
    assert n == 0


@pytest.mark.asyncio
async def test_legacy_json_corrupted_is_skipped(admin_token):
    """损坏的 JSON 不应让启动崩溃：迁移函数返回 0，文件保留以便人工排查。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import migrate_legacy_runtime_settings_json_once

    legacy_path = cfg.settings.DATA_DIR / "runtime_settings.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{ not valid json", encoding="utf-8")

    # 不应抛
    n = await migrate_legacy_runtime_settings_json_once()
    # 损坏 JSON：解析失败，函数返回 0，但日志有警告
    assert n == 0
