"""Task 4 RED — /api/voices 聚合 + start_build 命名空间校验（升级版 P-2.5）。

T-VA1 /api/voices 聚合豆包音色；每条 voice['provider'] == 'doubao'（MiniMax TTS 已弃用）。
T-VA2 [P-2.5] 混用 doubao: 音色 → **不抛错**，允许按 voice_id 自动路由。
T-VA3 [P-2.5] narrator 与角色音色来源不一致 → 不报错。
T-VA4 前缀合法（mode/provider 在白名单）→ 通过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# T-VA1：/api/voices 聚合
# ---------------------------------------------------------------------
def test_list_voices_aggregates_providers():
    import asyncio
    from backend.app.api.routes import list_voices
    result = asyncio.run(list_voices())
    voices = result["voices"]
    providers = {v.get("provider") for v in voices}
    assert "doubao" in providers, f"聚合结果中缺少 doubao 音色，providers 集合={providers}"
    assert result["count"] == len(voices)
    # 每条音色必须有 provider 字段，且值为 doubao（MiniMax TTS 已弃用）
    for v in voices:
        assert v.get("provider") == "doubao", f"voice.provider 未知：{v}"


# ---------------------------------------------------------------------
# T-VA2 / T-VA3：[P-2.5] 命名空间校验（升级版：混用允许，不再抛错）
# 直接测 build.py 中的 _validate_tts_namespace 函数。
# ---------------------------------------------------------------------
def test_namespace_allows_mixed_voice_sources():
    """[P-2.5] doubao: 音色混用不再抛错：合成时按 voice_id 前缀自动路由。"""
    from backend.app.services.build import _validate_tts_namespace

    # 未显式指定 provider（空串）：同样允许
    _validate_tts_namespace(
        tts_provider="",
        mode="classic",
        narrator_voice_id="doubao:zh_female_qingxin",
        voice_assignments={"角色A": "doubao:zh_male_qingnianqingche"},
    )


def test_namespace_rejects_unknown_mode():
    """未知 mode 仍必须抛错（防止拼写错误静默通过）。"""
    from backend.app.services.build import _validate_tts_namespace

    with pytest.raises(RuntimeError, match="未知 build.mode"):
        _validate_tts_namespace(
            tts_provider="doubao",
            mode="not_a_real_mode",
            narrator_voice_id="doubao:zh_female_qingxin",
            voice_assignments={},
        )


def test_namespace_rejects_unknown_provider():
    """未知 tts_provider 仍必须抛错（防止拼写错误静默通过）。"""
    from backend.app.services.build import _validate_tts_namespace

    with pytest.raises(RuntimeError, match="未知 tts_provider"):
        _validate_tts_namespace(
            tts_provider="some_typo_provider",
            mode="classic",
            narrator_voice_id="doubao:zh_female_qingxin",
            voice_assignments={},
        )


def test_namespace_allows_matching_prefixes():
    from backend.app.services.build import _validate_tts_namespace

    # 全部 doubao 前缀 + tts_provider=doubao → OK（不抛异常）
    _validate_tts_namespace(
        tts_provider="doubao",
        mode="classic",
        narrator_voice_id="doubao:zh_female_qingxin",
        voice_assignments={"角色A": "doubao:zh_male_qingnianqingche"},
    )
# ---------------------------------------------------------------------
# T-VA4：config_digest 扩展为包含 tts_provider / mode
# ---------------------------------------------------------------------
def test_calc_config_digest_differs_by_tts_provider():
    from backend.app.services.build import _calc_config_digest
    d1 = _calc_config_digest(
        "doubao:zh_male_qingcang_uranus_bigtts", 1.0, {}, mode="classic", tts_provider="doubao",
    )
    d2 = _calc_config_digest(
        "doubao:zh_male_qingcang_uranus_bigtts", 1.0, {}, mode="classic", tts_provider="minimax",
    )
    assert d1 != d2, "相同 narrator/VA，不同 tts_provider 必须产生不同 digest"


def test_calc_config_digest_backward_compatible_no_mode_provider():
    """不提供 mode/tts_provider 参数时，默认用 classic/doubao 填充，保证与旧测试一致。"""
    from backend.app.services.build import _calc_config_digest
    d_kw = _calc_config_digest(
        "zh_male_qingcang_uranus_bigtts", 1.0, {}, mode="classic", tts_provider="doubao",
    )
    d_no = _calc_config_digest("zh_male_qingcang_uranus_bigtts", 1.0, {})
    assert d_kw == d_no


@pytest.mark.asyncio
async def test_list_voices_uses_active_tts_even_with_legacy_query(monkeypatch):
    from types import SimpleNamespace

    from backend.app.api import routes
    from backend.app.core import config

    monkeypatch.setattr(config.settings, "PROVIDERS_CONFIG", '{"active":{"tts":{"provider_id":"minimax"}}}')
    called = []

    async def voices():
        return [{"id": "minimax:a", "provider": "minimax"}, {"id": "minimax:a", "provider": "minimax"}]

    def tts(provider):
        called.append(provider)
        return SimpleNamespace(list_voices=voices)

    monkeypatch.setattr(routes, "get_tts", tts)
    result = await routes.list_voices("doubao")
    assert called == ["minimax"]
    assert result == {"voices": [{"id": "minimax:a", "provider": "minimax"}], "count": 1}


def test_icl_routes_are_removed():
    from backend.app.api.routes import router

    assert not any("/icl/" in route.path for route in router.routes)


@pytest.mark.asyncio
@pytest.mark.parametrize("male,female", [(None, None), ("minimax:male", "doubao:female"), ("", None)])
async def test_build_defaults_are_nullable_and_forwarded(monkeypatch, male, female):
    from types import SimpleNamespace

    from backend.app.api import routes
    from backend.app.db.session import init_db
    from backend.app.services import rate_limit

    await init_db()
    captured = {}

    async def noop(*args, **kwargs):
        return None

    async def build(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(build_id="build", status="queued")

    monkeypatch.setattr(routes, "assert_project_writable", noop)
    monkeypatch.setattr(rate_limit, "enforce_build_rate_limit", noop)
    monkeypatch.setattr(routes, "start_build", build)
    req = routes.StartBuildRequest(default_male_voice_id=male, default_female_voice_id=female)
    await routes.api_start_build("project", req, SimpleNamespace(client=None), SimpleNamespace(id=1, username="user"))
    assert captured["default_male_voice_id"] == male
    assert captured["default_female_voice_id"] == female
