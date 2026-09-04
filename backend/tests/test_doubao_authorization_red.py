"""豆包 TTS _get_authorization 凭据读取优先级回归。

Bug 背景：用户反馈「在音色库中试听豆包音色失败，报错『未配置豆包凭据』」，
即使在「设置 → 模型厂商 → 火山引擎豆包语音」里已经启用并填了 API Key。

根因：旧实现只读 settings.DOUBAO_AK（.env 扁平字段），不读 PROVIDERS_CONFIG
结构里 id="doubao" 厂商的 api_key。前端设置页保存的 key 落到 PROVIDERS_CONFIG
而不是 settings.DOUBAO_AK，所以业务路径上完全读不到。

修复后优先级：
  1) PROVIDERS_CONFIG[id="doubao"].api_key  ← 设置页保存的
  2) settings.DOUBAO_AK                       ← .env 兼容
  3) os.environ["MEGACORE_ACCESS_KEY_FROM_ENV"]← 容器部署兼容
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def reset_providers_config():
    """每次用例前后保存/恢复 PROVIDERS_CONFIG，避免污染其它测试。"""
    from backend.app.core import config as cfgmod

    original = cfgmod.settings.PROVIDERS_CONFIG
    yield
    cfgmod.settings.PROVIDERS_CONFIG = original


def _set_providers_config(cfgmod, providers: list[dict]) -> None:
    """把 providers 写入 settings.PROVIDERS_CONFIG（持久化为空字符串触发回退是
    模拟『配置从未保存』状态）。"""
    import json as _json
    cfgmod.settings.PROVIDERS_CONFIG = _json.dumps(
        {"providers": providers, "active": {"tts": {}, "llm": {}}},
        ensure_ascii=False,
    )


def test_providers_config_key_is_used_when_present(reset_providers_config, monkeypatch):
    """核心修复用例：PROVIDERS_CONFIG 里有 doubao 厂商 + api_key 时必须用它。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    # 关键：.env 扁平字段和 ENV 都不设置，模拟「用户完全没配 .env / 容器环境」，
    # 仅靠设置页保存了 key
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "")
    monkeypatch.delenv("MEGACORE_ACCESS_KEY_FROM_ENV", raising=False)
    _set_providers_config(cfgmod, [
        {"id": "minimax", "label": "MiniMax", "enabled": True, "api_key": "",
         "base_url": "", "models": []},
        {"id": "doubao", "label": "火山引擎豆包语音", "enabled": True,
         "api_key": "sk-from-providers-config-5781",
         "base_url": "https://openspeech.bytedance.com/api/v1", "models": []},
    ])

    p = DoubaoTTSProvider()
    auth = p._get_authorization()
    # 凭据不含空格 → 应自动套上 "Bearer;{ak}" 风格（豆包接受这种）
    assert auth == "Bearer;sk-from-providers-config-5781", (
        f"应该从 PROVIDERS_CONFIG 读取 key，但得到 {auth!r}"
    )


def test_providers_config_takes_priority_over_dotenv(reset_providers_config, monkeypatch):
    """PROVIDERS_CONFIG 里的 key 必须优先于 .env 的 DOUBAO_AK。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "sk-from-dotenv")
    _set_providers_config(cfgmod, [
        {"id": "doubao", "label": "豆包", "enabled": True,
         "api_key": "sk-from-providers-config",
         "base_url": "", "models": []},
    ])

    p = DoubaoTTSProvider()
    assert p._get_authorization() == "Bearer;sk-from-providers-config"


def test_dotenv_used_when_providers_config_has_empty_key(reset_providers_config, monkeypatch):
    """PROVIDERS_CONFIG 存在但 doubao.api_key 为空（用户清空时）→ 兜底 .env。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "sk-from-dotenv")
    _set_providers_config(cfgmod, [
        {"id": "doubao", "label": "豆包", "enabled": True,
         "api_key": "",
         "base_url": "", "models": []},
    ])

    p = DoubaoTTSProvider()
    assert p._get_authorization() == "Bearer;sk-from-dotenv"


def test_env_fallback_used_when_dotenv_and_providers_empty(reset_providers_config, monkeypatch):
    """PROVIDERS_CONFIG 和 .env 都为空 → 兜底进程 ENV。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "")
    monkeypatch.setenv("MEGACORE_ACCESS_KEY_FROM_ENV", "sk-from-env")
    _set_providers_config(cfgmod, [])

    p = DoubaoTTSProvider()
    assert p._get_authorization() == "Bearer;sk-from-env"


def test_authorization_value_preserved_when_contains_space(reset_providers_config, monkeypatch):
    """用户已经在 .env 里写了 'Bearer xxxx' 形式 → 不重复套 Bearer。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    # 含空格 → 视为已经组装好的完整 Authorization 头
    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "Bearer my-ak")
    _set_providers_config(cfgmod, [
        {"id": "doubao", "label": "豆包", "enabled": True,
         "api_key": "Bearer provider-ak",  # 这里也含空格
         "base_url": "", "models": []},
    ])

    p = DoubaoTTSProvider()
    # PROVIDERS_CONFIG 优先，且含空格 → 原样返回
    assert p._get_authorization() == "Bearer provider-ak"


def test_clear_error_message_when_no_credential(reset_providers_config, monkeypatch):
    """三个来源都没配时，错误消息应指向设置页路径，方便用户自查。"""
    from backend.app.ai.providers.doubao.tts import DoubaoTTSProvider
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_AK", "")
    monkeypatch.delenv("MEGACORE_ACCESS_KEY_FROM_ENV", raising=False)
    _set_providers_config(cfgmod, [
        {"id": "doubao", "label": "豆包", "enabled": False, "api_key": "",
         "base_url": "", "models": []},
    ])

    p = DoubaoTTSProvider()
    with pytest.raises(RuntimeError) as ei:
        p._get_authorization()
    msg = str(ei.value)
    # 必须能引导到设置页 + 环境变量两种修法
    assert "模型厂商" in msg or "DOUBAO_AK" in msg, (
        f"报错消息应给用户明确的修复路径，实际：{msg!r}"
    )