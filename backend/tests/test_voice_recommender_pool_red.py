"""音色推荐：聚合多厂商 + ICL 复刻的候选池 + prompt 透传测试。

覆盖：
  T-VR-1  _aggregate_voice_pool 聚合 minimax + doubao + 用户 ICL 三方
  T-VR-2  _aggregate_voice_pool 在 user_id=None 时跳过 ICL
  T-VR-3  recommend_voices_with_llm 的 prompt 同时含 minimax/doubao/icl 三方音色 id
  T-VR-4  recommend_voices_with_llm 的 prompt 含 provider 字段（让 LLM 能跨厂商）
  T-VR-5  recommend_voices_with_llm 单 provider 异常时降级（其他仍能返回）
  T-VR-6  recommend_voices_with_llm 向后兼容旧调用（不传 user_id / project_id）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------
# Mock 工厂：返回定制音色集合
# ---------------------------------------------------------------------


def _make_minimax_voices() -> list[dict]:
    return [
        {"id": "minimax:male-qn-qingse", "name": "青涩青年音色", "gender": "男声",
         "description": "青年·清涩·干净", "provider": "minimax"},
        {"id": "minimax:female-tianmei", "name": "甜美女性音色", "gender": "女声",
         "description": "少女·甜蜜·软糯", "provider": "minimax"},
    ]


def _make_doubao_voices() -> list[dict]:
    return [
        {"id": "doubao:zh_male_ahu_conversation_wvae_bigtts", "name": "阿虎",
         "gender": "male", "description": "中年·大叔·亲切", "provider": "doubao",
         "model": "seed-tts-2.0", "free": False},
        {"id": "doubao:zh_female_vv_uranus_bigtts", "name": "悠悠",
         "gender": "female", "description": "大模型 2.0 音色；温柔知性女主",
         "provider": "doubao", "model": "seed-tts-2.0", "free": False},
        {"id": "doubao:BV120_streaming", "name": "BV120", "gender": "female",
         "description": "小模型 1.0 免费音色", "provider": "doubao",
         "model": "seed-tts-1.0", "free": True},
    ]


def _make_icl_voices(user_id: int) -> list[dict]:
    return [
        {"id": "icl:clone_abc", "name": "我的声线·小雅", "gender": "neutral",
         "description": "用户上传训练的声音复刻", "provider": "icl"},
    ]


@pytest.fixture
def patch_factory(monkeypatch):
    """monkeypatch service._fetch_provider_voices 与 _fetch_icl_voices。

    get_tts("minimax"/"doubao") 在测试默认会被 conftest 注入的 MockTTSProvider
    拦截；为了让 doubao 也返回"豆包"音色，需要替换 service 层的辅助函数。
    """
    from backend.app.services import voice_recommender as vr

    minimax_voices = _make_minimax_voices()
    doubao_voices = _make_doubao_voices()
    # 用一个可变容器，便于单测 override 抛错场景
    state = {
        "minimax": minimax_voices,
        "doubao": doubao_voices,
        "icl": lambda user_id: _make_icl_voices(user_id) if user_id else [],
        "icl_raise": False,
        "minimax_raise": False,
        "doubao_raise": False,
    }

    async def _fake_provider(p: str) -> list[dict]:
        # 直接模拟"provider 异常"行为：返回空列表（service 真实 helper 内部吞错）
        if state.get(f"{p}_raise"):
            return []
        return list(state[p])

    async def _fake_icl(user_id):
        # 同上：模拟 ICL 异常
        if state["icl_raise"]:
            return []
        return state["icl"](user_id) if user_id else []

    monkeypatch.setattr(vr, "_fetch_provider_voices", _fake_provider)
    monkeypatch.setattr(vr, "_fetch_icl_voices", _fake_icl)
    return state


# ---------------------------------------------------------------------
# T-VR-1：聚合 minimax + doubao + ICL
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_voice_pool_includes_all_providers(patch_factory):
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    pool = await _aggregate_voice_pool(user_id=42)
    ids = {v["id"] for v in pool}
    # 三个 provider 都有
    assert "minimax:male-qn-qingse" in ids
    assert "doubao:zh_female_vv_uranus_bigtts" in ids
    assert "icl:clone_abc" in ids
    # 共 6 个（2 + 3 + 1）
    assert len(pool) == 6
    # 每项带 provider 字段
    providers = {v.get("provider") for v in pool}
    assert providers == {"minimax", "doubao", "icl"}


# ---------------------------------------------------------------------
# T-VR-2：user_id=None 时不含 ICL
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_voice_pool_no_icl_when_user_none(patch_factory):
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    pool = await _aggregate_voice_pool(user_id=None)
    providers = {v.get("provider") for v in pool}
    assert "icl" not in providers
    assert providers == {"minimax", "doubao"}


# ---------------------------------------------------------------------
# T-VR-5：单 provider 抛错时降级
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_voice_pool_degrades_on_provider_failure(patch_factory):
    """minimax 抛错时，doubao + icl 仍返回。"""
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    patch_factory["minimax_raise"] = True
    pool = await _aggregate_voice_pool(user_id=1)
    ids = {v["id"] for v in pool}
    # minimax 音色没拿到，但 doubao/icl 仍在
    assert not any(v.startswith("minimax:") for v in ids)
    assert any(v.startswith("doubao:") for v in ids)
    assert "icl:clone_abc" in ids


@pytest.mark.asyncio
async def test_aggregate_voice_pool_degrades_on_icl_failure(patch_factory):
    """icl 抛错时，minimax + doubao 仍返回。"""
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    patch_factory["icl_raise"] = True
    pool = await _aggregate_voice_pool(user_id=1)
    ids = {v["id"] for v in pool}
    assert not any(v.startswith("icl:") for v in ids)
    assert any(v.startswith("doubao:") for v in ids)
    assert any(v.startswith("minimax:") for v in ids)


# ---------------------------------------------------------------------
# T-VR-3 / T-VR-4：prompt 同时含三家音色 id + provider 字段
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recommend_prompt_contains_all_three_providers(patch_factory):
    from backend.app.ai import factory as _factory
    from backend.app.ai.factory import get_llm
    from backend.app.services.voice_recommender import recommend_voices_with_llm
    from backend.app.services.character import Character

    llm = get_llm()  # conftest 注入了 MockLLMProvider
    assert hasattr(llm, "calls"), "测试需要 MockLLMProvider"

    characters = [
        Character(name="林若雪", gender="女", age="少女", personality="内向"),
    ]
    await recommend_voices_with_llm(characters, user_id=42)

    # 找到最后一条 voice recommend 的 prompt（按 schema 名 "_Wrapper" + 含 "配音导演"）
    prompt = None
    for c in reversed(llm.calls):
        if "配音导演" in c["prompt"]:
            prompt = c["prompt"]
            break
    assert prompt is not None, "应触发 LLM 调用，calls 中应有 配音导演 prompt"

    # T-VR-3：三家音色 id 都出现
    assert "minimax:female-tianmei" in prompt
    assert "doubao:zh_female_vv_uranus_bigtts" in prompt
    assert "icl:clone_abc" in prompt

    # T-VR-4：每条音色带 provider 字段
    # 解析 【音色列表】 块的 JSON
    m = re.search(r"【音色列表】\s*(\[.*?\])\s*$", prompt, re.DOTALL)
    assert m, "prompt 末尾应有【音色列表】JSON 块"
    voices_block = json.loads(m.group(1))
    assert isinstance(voices_block, list) and len(voices_block) >= 6
    # 每条音色 dict 含 provider 字段
    for v in voices_block:
        assert "id" in v and "provider" in v, f"音色 {v} 缺少 id/provider"
    providers = {v["provider"] for v in voices_block}
    assert providers == {"minimax", "doubao", "icl"}


# ---------------------------------------------------------------------
# T-VR-6：向后兼容旧调用（不传 user_id / project_id）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recommend_works_without_user_id(patch_factory):
    """旧调用 recommend_voices_with_llm(characters) 不传 user_id，应仍工作。"""
    from backend.app.services.voice_recommender import recommend_voices_with_llm
    from backend.app.services.character import Character

    characters = [
        Character(name="林若雪", gender="女", age="少女", personality="内向"),
    ]
    # 不抛错即可
    recs = await recommend_voices_with_llm(characters)
    assert isinstance(recs, list)