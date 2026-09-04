"""Task 4 RED — /api/voices 聚合 + start_build 命名空间校验。

T-VA1 /api/voices 必须聚合 minimax: + doubao: 两套音色；返回结构里每条 voice['provider'] ∈ {'minimax','doubao'}。
T-VA2 voice_assignments 中音色前缀与 tts_provider 不兼容时（tts_provider=doubao 但用 minimax:xxx）→ RuntimeError。
T-VA3 narrator_voice_id 前缀与 tts_provider 不一致也需报错。
T-VA4 前缀兼容时（voice_id 无任何前缀，或前缀与 provider 语义匹配）→ 通过。
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
    assert "minimax" in providers, f"聚合结果中缺少 minimax 音色，providers 集合={providers}"
    assert "doubao" in providers, f"聚合结果中缺少 doubao 音色，providers 集合={providers}"
    assert result["count"] == len(voices)
    # 每条音色必须有 provider 字段，且值为 minimax/doubao 之一
    for v in voices:
        assert v.get("provider") in ("minimax", "doubao"), f"voice.provider 未知：{v}"


# ---------------------------------------------------------------------
# T-VA2 / T-VA3：命名空间校验（tts_provider 与 narrator/voice_assignments 一致）
# 直接测 build.py 中的 _validate_tts_namespace 函数。
# ---------------------------------------------------------------------
def test_namespace_reject_minimax_voice_in_doubao_provider():
    from backend.app.services.build import _validate_tts_namespace

    with pytest.raises(RuntimeError) as ei:
        _validate_tts_namespace(
            tts_provider="doubao",
            mode="classic",
            narrator_voice_id="minimax:male-qn-jingying",
            voice_assignments={"角色A": "minimax:female-qn-lanyin"},
        )
    assert "narrator" in str(ei.value).lower() or "minimax" in str(ei.value)


def test_namespace_reject_doubao_voice_in_minimax_provider():
    from backend.app.services.build import _validate_tts_namespace

    with pytest.raises(RuntimeError) as ei:
        _validate_tts_namespace(
            tts_provider="minimax",
            mode="classic",
            narrator_voice_id="doubao:zh_female_qingxin",
            voice_assignments={"角色A": "minimax:female-qn-lanyin"},
        )
    msg = str(ei.value).lower()
    assert "narrator" in msg or "doubao" in msg


def test_namespace_reject_incompatible_voice_assignment():
    from backend.app.services.build import _validate_tts_namespace

    with pytest.raises(RuntimeError) as ei:
        _validate_tts_namespace(
            tts_provider="doubao",
            mode="classic",
            narrator_voice_id="doubao:zh_female_qingxin",
            voice_assignments={"角色A": "minimax:male-qn-jingying"},
        )
    msg = str(ei.value).lower()
    assert "角色a" in msg or "voice" in msg or "minimax" in msg


def test_namespace_allows_matching_prefixes():
    from backend.app.services.build import _validate_tts_namespace

    # 全部 doubao 前缀 + tts_provider=doubao → OK（不抛异常）
    _validate_tts_namespace(
        tts_provider="doubao",
        mode="classic",
        narrator_voice_id="doubao:zh_female_qingxin",
        voice_assignments={"角色A": "doubao:zh_male_qingnianqingche"},
    )
    # minimax 前缀 + tts_provider=minimax → OK
    _validate_tts_namespace(
        tts_provider="minimax",
        mode="classic",
        narrator_voice_id="minimax:male-qn-jingying",
        voice_assignments={"角色A": "minimax:female-qn-lanyin"},
    )


def test_namespace_allows_icl_prefix_for_doubao():
    """icl:* → 属于豆包 ICL；tts_provider=doubao 时通过。"""
    from backend.app.services.build import _validate_tts_namespace
    _validate_tts_namespace(
        tts_provider="doubao",
        mode="classic",
        narrator_voice_id="doubao:zh_female_qingxin",
        voice_assignments={"角色A": "icl:custom_clone_id_123"},
    )


def test_namespace_multicast_mode_requires_tts_provider_not_empty():
    """多播剧模式（multicast）tts_provider 必须显式给出（不能为空）。"""
    from backend.app.services.build import _validate_tts_namespace
    with pytest.raises(RuntimeError):
        _validate_tts_namespace(
            tts_provider="",
            mode="multicast",
            narrator_voice_id="doubao:zh_female_qingxin",
            voice_assignments={},
        )


# ---------------------------------------------------------------------
# T-VA4：config_digest 扩展为包含 tts_provider / mode
# ---------------------------------------------------------------------
def test_calc_config_digest_differs_by_tts_provider():
    from backend.app.services.build import _calc_config_digest
    d1 = _calc_config_digest(
        "minimax:male-qn-jingying", 1.0, {}, mode="classic", tts_provider="minimax",
    )
    d2 = _calc_config_digest(
        "minimax:male-qn-jingying", 1.0, {}, mode="classic", tts_provider="doubao",
    )
    assert d1 != d2, "相同 narrator/VA，不同 tts_provider 必须产生不同 digest"


def test_calc_config_digest_differs_by_mode():
    from backend.app.services.build import _calc_config_digest
    d1 = _calc_config_digest(
        "doubao:zh_female_qingxin", 1.0, {}, mode="classic", tts_provider="doubao",
    )
    d2 = _calc_config_digest(
        "doubao:zh_female_qingxin", 1.0, {}, mode="multicast", tts_provider="doubao",
    )
    assert d1 != d2, "相同 narrator/VA，不同 mode 必须产生不同 digest"


def test_calc_config_digest_backward_compatible_no_mode_provider():
    """不提供 mode/tts_provider 参数时，默认用 classic/minimax 填充，保证与旧测试一致。"""
    from backend.app.services.build import _calc_config_digest
    d_kw = _calc_config_digest(
        "male-qn-jingying", 1.0, {}, mode="classic", tts_provider="minimax",
    )
    d_no = _calc_config_digest("male-qn-jingying", 1.0, {})
    assert d_kw == d_no
