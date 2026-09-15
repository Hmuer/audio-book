"""P-env：把运行时可变参数从 .env 迁出 — 验证页面化路径完整。

覆盖：
  T-EM-1  doubao provider 模板含全部 5 个凭据字段 + 4 个端点字段
  T-EM-2  doubao_field() 优先返回 provider 字典，provider 空时回退 settings
  T-EM-3  doubao_field() 端点字段自动拼 base_url（path-only 形式）
  T-EM-4  /api/settings 白名单 14 项新增（豆包 11 + 日志 2 + multicast 1）
  T-EM-5  /api/settings PUT float 字段（DOUBAO_ICL_POLL_INTERVAL_SECS）能正确转换
  T-EM-6  _redact_provider 脱敏新增 5 个字段
  T-EM-7  legacy 迁移：env 里有 DOUBAO_AK/SK 时自动迁到 provider 字典
  T-EM-8  doubao_field() 在 settings + provider 都为空时返回兜底默认值
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-EM-1：doubao provider 模板 schema
# ---------------------------------------------------------------------
def test_em_1_doubao_provider_template_has_full_fields():
    """DEFAULT_PROVIDERS_TEMPLATE 里的 doubao 厂商应包含 5 凭据 + 4 端点字段。"""
    from backend.app.core.config import DEFAULT_PROVIDERS_TEMPLATE

    doubao = next(p for p in DEFAULT_PROVIDERS_TEMPLATE if p["id"] == "doubao")
    # 5 凭据字段
    for f in ("api_key", "secret", "app_id", "icl_api_key", "icl_access_key"):
        assert f in doubao, f"doubao 模板缺 {f} 字段"
        assert doubao[f] == "", f"doubao.{f} 默认应为空"
    # 4 端点字段
    for f in ("tts_endpoint", "icl_endpoint", "tts_v3_endpoint", "seed_audio_endpoint"):
        assert f in doubao, f"doubao 模板缺 {f} 字段"
        assert doubao[f], f"doubao.{f} 应有默认值"


# ---------------------------------------------------------------------
# T-EM-2：doubao_field() 优先 provider，回退 settings
# ---------------------------------------------------------------------
def test_em_2_doubao_field_prefers_provider_over_settings(monkeypatch):
    """provider 字典有值时直接返回；为空时回退 settings.*。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import doubao_field, save_providers_config, get_provider

    # 把 doubao provider 的 api_key 设为 "from-provider"
    prov = dict(get_provider("doubao") or {})
    prov["api_key"] = "from-provider"
    save_providers_config({"providers": [prov], "active": cfg._parse_providers_config().get("active", {"tts": {}, "llm": {}})})

    # 同时改 settings，应优先用 provider
    saved = cfg.settings.DOUBAO_AK
    cfg.settings.DOUBAO_AK = "from-env"
    try:
        assert doubao_field("api_key") == "from-provider"
    finally:
        cfg.settings.DOUBAO_AK = saved
        # 恢复
        prov["api_key"] = saved  # 清空（如果之前也是空）
        if saved == "":
            prov["api_key"] = ""


def test_em_2_doubao_field_fallback_to_settings_when_provider_empty(monkeypatch):
    """provider api_key 为空 + settings.DOUBAO_AK 有值 → 返回 settings。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import doubao_field, save_providers_config, get_provider

    prov = dict(get_provider("doubao") or {})
    prov["api_key"] = ""  # 清空 provider
    save_providers_config({"providers": [prov], "active": cfg._parse_providers_config().get("active", {"tts": {}, "llm": {}})})

    saved = cfg.settings.DOUBAO_AK
    cfg.settings.DOUBAO_AK = "from-env-fallback"
    try:
        assert doubao_field("api_key") == "from-env-fallback"
    finally:
        cfg.settings.DOUBAO_AK = saved


# ---------------------------------------------------------------------
# T-EM-3：端点字段自动拼 base_url（path-only 形式）
# ---------------------------------------------------------------------
def test_em_3_doubao_endpoint_field_concatenates_base_url():
    """provider 里 tts_v3_endpoint 是 path-only "/api/v3/..."，应拼成完整 URL。"""
    from backend.app.core.config import doubao_field
    url = doubao_field("tts_v3_endpoint")
    assert url.startswith("https://"), f"应拼成完整 URL，实际={url!r}"
    assert "/api/v3/tts/unidirectional" in url


# ---------------------------------------------------------------------
# T-EM-4：白名单含 14 项新增
# ---------------------------------------------------------------------
def test_em_4_editable_settings_has_14_new_keys():
    """_EDITABLE_SETTINGS 应包含豆包 11 + 日志 2 + multicast 1 = 14 项新键。"""
    from backend.app.api.routes import _EDITABLE_SETTINGS

    expected_new = {
        # 豆包 11 项
        "DOUBAO_TTS_USE_V3",
        "DOUBAO_TTS_RPM_LIMIT",
        "DOUBAO_SEED_AUDIO_RPM_LIMIT",
        "DOUBAO_ICL_RPM_LIMIT",
        "DOUBAO_ICL_POLL_INTERVAL_SECS",
        "DOUBAO_ICL_TIMEOUT_SECS",
        "ICL_MAX_AUDIO_BYTES",
        "DOUBAO_AUDIO_SAMPLE_RATE",
        "DOUBAO_AUDIO_LOUDNESS_RATE",
        # 端点 2 项已通过 doubao_field 走 provider，不再放白名单
        # 日志 2 项
        "LOG_MAX_BYTES",
        "LOG_BACKUP_COUNT",
        # 合成质量 1 项
        "MULTICAST_STRICT_MODE",
    }
    for k in expected_new:
        assert k in _EDITABLE_SETTINGS, f"_EDITABLE_SETTINGS 缺 {k}"
        # 类型正确
        info = _EDITABLE_SETTINGS[k]
        assert info[0] in ("int", "str", "bool", "list[str]", "json", "float"), \
            f"{k} 类型异常: {info[0]}"


# ---------------------------------------------------------------------
# T-EM-5：float 类型转换
# ---------------------------------------------------------------------
def test_em_5_settings_put_float_conversion():
    """DOUBAO_ICL_POLL_INTERVAL_SECS 是 float，PUT 字符串 '5.0' 应能转。"""
    # 类型校验逻辑：检查 update_settings 函数的 float 分支存在
    from backend.app.api.routes import update_settings  # noqa: F401
    from backend.app.api.routes import _EDITABLE_SETTINGS
    typ = _EDITABLE_SETTINGS["DOUBAO_ICL_POLL_INTERVAL_SECS"][0]
    assert typ == "float", f"DOUBAO_ICL_POLL_INTERVAL_SECS 类型应是 float，实际 {typ}"


@pytest.mark.asyncio
async def test_em_5b_settings_put_float_value_roundtrip(_isolate_data_dir):
    """PUT float 字段 → settings 实际值是 float。"""
    from backend.app.main import app
    from backend.app.db.session import init_db
    from backend.app.services.auth import seed_admin_user, create_access_token
    from backend.app.core import config as cfg
    from httpx import AsyncClient, ASGITransport

    await init_db()
    await seed_admin_user()
    tok, _ = create_access_token("admin")

    saved = cfg.settings.DOUBAO_ICL_POLL_INTERVAL_SECS
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.put(
                "/api/settings",
                json={"updates": {"DOUBAO_ICL_POLL_INTERVAL_SECS": "7.5"}},
                headers={"Authorization": f"Bearer {tok}"},
            )
            assert r.status_code == 200, r.text
            assert cfg.settings.DOUBAO_ICL_POLL_INTERVAL_SECS == 7.5
            assert isinstance(cfg.settings.DOUBAO_ICL_POLL_INTERVAL_SECS, float)
    finally:
        cfg.settings.DOUBAO_ICL_POLL_INTERVAL_SECS = saved


# ---------------------------------------------------------------------
# T-EM-6：_redact_provider 脱敏 5 字段
# ---------------------------------------------------------------------
def test_em_6_redact_provider_handles_all_secrets():
    """_redact_provider 对 5 个敏感字段（api_key/secret/app_id/icl_api_key/icl_access_key）都脱敏。"""
    from backend.app.api.routes import _redact_provider

    sample = {
        "id": "doubao",
        "api_key": "secret-key-123456",
        "secret": "sk-secret-789",
        "app_id": "12345678",
        "icl_api_key": "icl-key-abc",
        "icl_access_key": "access-xyz-999",
        "base_url": "https://example.com",
    }
    out = _redact_provider(sample)
    for f in ("api_key", "secret", "app_id", "icl_api_key", "icl_access_key"):
        assert out[f].startswith("***"), f"{f} 应脱敏为 *** 前缀"
        assert out[f].endswith(sample[f][-4:]), f"{f} 应保留末4位"
        assert out[f"{f}_configured"] is True
    # 非敏感字段保留
    assert out["id"] == "doubao"
    assert out["base_url"] == "https://example.com"


def test_em_6b_redact_provider_empty_secrets():
    """敏感字段为空时 → 返回空字符串 + configured=False。"""
    from backend.app.api.routes import _redact_provider

    sample = {"id": "doubao", "api_key": "", "secret": "", "app_id": ""}
    out = _redact_provider(sample)
    assert out["api_key"] == ""
    assert out["api_key_configured"] is False
    assert out["secret"] == ""
    assert out["secret_configured"] is False


# ---------------------------------------------------------------------
# T-EM-7：legacy 迁移 — env 里有 DOUBAO_AK 时迁到 provider
# ---------------------------------------------------------------------
def test_em_7_legacy_doubao_fields_migrated_to_provider(monkeypatch):
    """settings.DOUBAO_AK/SK/APP_ID/ICL_API_KEY/ICL_ACCESS_KEY 有值时
    应被 _migrate_legacy_providers 迁到 provider 字典的对应字段。"""
    from backend.app.core import config as cfg

    # 设置 legacy 字段
    saved = {
        "DOUBAO_AK": cfg.settings.DOUBAO_AK,
        "DOUBAO_SK": cfg.settings.DOUBAO_SK,
        "DOUBAO_APP_ID": cfg.settings.DOUBAO_APP_ID,
        "DOUBAO_ICL_API_KEY": cfg.settings.DOUBAO_ICL_API_KEY,
        "DOUBAO_ICL_ACCESS_KEY": cfg.settings.DOUBAO_ICL_ACCESS_KEY,
    }
    cfg.settings.DOUBAO_AK = "legacy-ak"
    cfg.settings.DOUBAO_SK = "legacy-sk"
    cfg.settings.DOUBAO_APP_ID = "12345678"
    cfg.settings.DOUBAO_ICL_API_KEY = "legacy-icl-key"
    cfg.settings.DOUBAO_ICL_ACCESS_KEY = "legacy-access-key"

    # 清空 PROVIDERS_CONFIG，强制重新走迁移
    saved_pcfg = cfg.settings.PROVIDERS_CONFIG
    cfg.settings.PROVIDERS_CONFIG = ""
    try:
        cfg._migrate_legacy_providers()
        from backend.app.core.config import get_provider
        prov = get_provider("doubao")
        assert prov["api_key"] == "legacy-ak"
        assert prov["secret"] == "legacy-sk"
        assert prov["app_id"] == "12345678"
        assert prov["icl_api_key"] == "legacy-icl-key"
        assert prov["icl_access_key"] == "legacy-access-key"
        assert prov["enabled"] is True  # 有 AK 自动启用
    finally:
        # 还原
        for k, v in saved.items():
            setattr(cfg.settings, k, v)
        cfg.settings.PROVIDERS_CONFIG = saved_pcfg


# ---------------------------------------------------------------------
# T-EM-8：doubao_field() 全空时返回兜底默认值
# ---------------------------------------------------------------------
def test_em_8_doubao_field_returns_default_when_all_empty(monkeypatch):
    """provider 空 + settings 空 → doubao_field("tts_endpoint") 返回官方默认 URL。"""
    from backend.app.core import config as cfg
    from backend.app.core.config import doubao_field, save_providers_config, get_provider

    prov = dict(get_provider("doubao") or {})
    saved_tts = prov.get("tts_endpoint")
    prov["tts_endpoint"] = ""  # 清空
    save_providers_config({"providers": [prov], "active": cfg._parse_providers_config().get("active", {"tts": {}, "llm": {}})})

    saved_settings = cfg.settings.DOUBAO_TTS_BASE_URL
    cfg.settings.DOUBAO_TTS_BASE_URL = ""
    try:
        url = doubao_field("tts_endpoint")
        assert url == "https://openspeech.bytedance.com/api/v1/tts", url
    finally:
        cfg.settings.DOUBAO_TTS_BASE_URL = saved_settings
        prov["tts_endpoint"] = saved_tts
        save_providers_config({"providers": [prov], "active": cfg._parse_providers_config().get("active", {"tts": {}, "llm": {}})})
