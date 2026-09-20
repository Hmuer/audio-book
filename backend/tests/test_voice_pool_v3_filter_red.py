"""音色推荐池的 v3 资源过滤 RED。

背景（实测 2026-09-20）：Build 章节报
  HTTP 403: {"code":45000030,"message":"[resource_id=volc.service_type.10029]
   requested resource not granted"}
音色是 BV158_streaming，它在音色表里标 `model: seed-tts-1.0`，于是请求的是
「语音合成大模型 1.0」资源（服务端别名 volc.service_type.10029）—— 账号没开通
就直接 403，跟文本无关。

v3 端点官方只支持 seed-tts-2.0 / seed-icl-2.0 两类资源，所以音色表里那 94 个
1.0 小模型音色在 v3 路径下不该进推荐候选池（推荐出来 = 保证 Build 失败）。

覆盖：
  T-F-1  is_voice_usable_on_v3：seed-tts-1.0 的 BV* 音色 → False
  T-F-2  is_voice_usable_on_v3：seed-tts-2.0 音色 → True
  T-F-3  is_voice_usable_on_v3：非 doubao / icl: 前缀 / 缺 model → True（不误伤）
  T-F-4  池过滤：DOUBAO_TTS_USE_V3=True 时 1.0 音色被剔除
  T-F-5  池过滤：DOUBAO_TTS_USE_V3=False（退回 v1）时不过滤
  T-F-6  内置音色表：过滤后剩下的全是 2.0 音色，且不会把池筛空
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _v1_voice() -> dict:
    return {
        "id": "doubao:BV158_streaming", "name": "智慧老者", "gender": "male",
        "description": "小模型 1.0 音色", "provider": "doubao",
        "model": "seed-tts-1.0",
    }


def _v2_voice() -> dict:
    return {
        "id": "doubao:zh_female_vv_uranus_bigtts", "name": "Vivi 2.0",
        "gender": "female", "description": "大模型 2.0 音色", "provider": "doubao",
        "model": "seed-tts-2.0",
    }


# ---------------------------------------------------------------------
# T-F-1 / T-F-2 / T-F-3：判定函数
# ---------------------------------------------------------------------
def test_f1_v1_doubao_voice_is_not_usable_on_v3():
    from backend.app.ai.providers.doubao.tts import is_voice_usable_on_v3

    assert is_voice_usable_on_v3(_v1_voice()) is False


def test_f2_v2_doubao_voice_is_usable_on_v3():
    from backend.app.ai.providers.doubao.tts import is_voice_usable_on_v3

    assert is_voice_usable_on_v3(_v2_voice()) is True


def test_f3_other_providers_and_missing_model_are_passed():
    """非豆包 / 复刻音色 / 元数据缺失 → 一律放行，避免误伤。"""
    from backend.app.ai.providers.doubao.tts import is_voice_usable_on_v3

    assert is_voice_usable_on_v3(
        {"id": "minimax:male-qn-jingying", "provider": "minimax"}
    ) is True
    assert is_voice_usable_on_v3(
        {"id": "icl:clone_abc", "provider": "icl"}
    ) is True
    # 远程同步来的复刻音色：provider 被标成 doubao，但 id 带 icl: 前缀
    assert is_voice_usable_on_v3(
        {"id": "icl:S_abc123", "provider": "doubao", "model": "seed-tts-1.0"}
    ) is True
    # 用户自定义音色：list_voices 不写 model 字段
    assert is_voice_usable_on_v3(
        {"id": "doubao:my_custom", "provider": "doubao"}
    ) is True


# ---------------------------------------------------------------------
# T-F-4 / T-F-5：推荐池过滤（受 DOUBAO_TTS_USE_V3 控制）
# ---------------------------------------------------------------------
@pytest.fixture
def patch_pool_sources(monkeypatch):
    from backend.app.services import voice_recommender as vr

    async def _fake_provider(p: str) -> list[dict]:
        return [_v1_voice(), _v2_voice()] if p == "doubao" else []

    async def _fake_icl(user_id):
        return []

    monkeypatch.setattr(vr, "_fetch_provider_voices", _fake_provider)
    monkeypatch.setattr(vr, "_fetch_icl_voices", _fake_icl)
    return vr


@pytest.mark.asyncio
async def test_f4_pool_drops_v1_voice_when_v3_enabled(patch_pool_sources, monkeypatch):
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_TTS_USE_V3", True)
    pool = await patch_pool_sources._aggregate_voice_pool(user_id=None)
    ids = {v["id"] for v in pool}
    assert "doubao:zh_female_vv_uranus_bigtts" in ids
    assert "doubao:BV158_streaming" not in ids


@pytest.mark.asyncio
async def test_f5_pool_keeps_v1_voice_when_v3_disabled(patch_pool_sources, monkeypatch):
    """退回 v1 端点时 BV* 是可用的，不能过滤。"""
    from backend.app.core import config as cfgmod

    monkeypatch.setattr(cfgmod.settings, "DOUBAO_TTS_USE_V3", False)
    pool = await patch_pool_sources._aggregate_voice_pool(user_id=None)
    ids = {v["id"] for v in pool}
    assert "doubao:BV158_streaming" in ids


# ---------------------------------------------------------------------
# T-F-6：内置音色表过滤后仍有足量 2.0 音色
# ---------------------------------------------------------------------
def test_f6_builtin_table_keeps_only_2_0_voices():
    from backend.app.ai.providers.doubao.tts import (
        _BUILTIN_VOICES,
        is_voice_usable_on_v3,
    )

    voices = [
        {
            "id": f"doubao:{v['id']}",
            "provider": "doubao",
            "model": v.get("model"),
        }
        for v in _BUILTIN_VOICES
    ]
    kept = [v for v in voices if is_voice_usable_on_v3(v)]
    # 不能把池筛空（音色表里 2.0 音色约 93 个）
    assert len(kept) >= 50, f"过滤后只剩 {len(kept)} 个豆包音色，疑似误伤"
    assert {v["model"] for v in kept} == {"seed-tts-2.0"}
