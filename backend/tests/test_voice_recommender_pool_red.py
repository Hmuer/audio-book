from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_doubao_voices():
    return [
        {"id": "doubao:zh_male_ahu_conversation_wvae_bigtts", "name": "阿虎", "gender": "male", "description": "沉稳", "provider": "doubao", "model": "seed-tts-2.0"},
        {"id": "doubao:zh_female_vv_uranus_bigtts", "name": "悠悠", "gender": "female", "description": "温柔", "provider": "doubao", "model": "seed-tts-2.0"},
        {"id": "doubao:BV120_streaming", "name": "BV120", "gender": "female", "description": "旧音色", "provider": "doubao", "model": "seed-tts-1.0"},
    ]


@pytest.fixture
def patch_factory(monkeypatch):
    from backend.app.services import voice_recommender as vr

    state = {"doubao": _make_doubao_voices(), "doubao_raise": False, "active": "doubao", "calls": []}

    async def fetch(provider):
        state["calls"].append(provider)
        return [] if state.get(f"{provider}_raise") else list(state[provider])

    monkeypatch.setattr(vr, "_fetch_provider_voices", fetch)
    monkeypatch.setattr(vr, "get_active", lambda kind: (state["active"], None, None))
    return state


@pytest.mark.asyncio
async def test_aggregate_voice_pool_uses_active_provider(patch_factory):
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    pool = await _aggregate_voice_pool(user_id=42)
    assert len(pool) == 2
    assert {v["provider"] for v in pool} == {"doubao"}
    patch_factory["active"] = "minimax"
    patch_factory["minimax"] = [{"id": "minimax:a", "provider": "minimax"}]
    assert await _aggregate_voice_pool() == patch_factory["minimax"]
    assert patch_factory["calls"] == ["doubao", "minimax"]


@pytest.mark.asyncio
async def test_aggregate_voice_pool_degrades_on_provider_failure(patch_factory):
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    patch_factory["doubao_raise"] = True
    assert await _aggregate_voice_pool() == []


@pytest.mark.asyncio
async def test_recommend_prompt_contains_active_provider(patch_factory):
    from backend.app.ai.factory import get_llm
    from backend.app.services.voice_recommender import recommend_voices_with_llm
    from backend.app.services.character import Character

    await recommend_voices_with_llm([Character(name="林若雪", gender="女", age="少女", personality="内向")])
    prompt = next(c["prompt"] for c in reversed(get_llm().calls) if "配音导演" in c["prompt"])
    match = re.search(r"【音色列表】\s*(\[.*?\])\s*$", prompt, re.DOTALL)
    voices = json.loads(match.group(1))
    assert len(voices) == 2
    assert {v["provider"] for v in voices} == {"doubao"}


@pytest.mark.asyncio
async def test_recommend_works_without_user_id(patch_factory):
    from backend.app.services.voice_recommender import recommend_voices_with_llm
    from backend.app.services.character import Character

    recs = await recommend_voices_with_llm([Character(name="林若雪", gender="女", age="少女", personality="内向")])
    assert isinstance(recs, list)


def _batch_char_list(n: int):
    from backend.app.services.character import Character

    return [
        Character(
            name=f"角色{i:03d}",
            gender="女" if i % 2 == 0 else "男",
            age="青年",
            personality="沉稳",
        )
        for i in range(n)
    ]


def _rec_prompts(llm, since: int) -> list[str]:
    return [c["prompt"] for c in llm.calls[since:] if "配音导演" in c["prompt"]]


@pytest.mark.asyncio
async def test_recommend_batches_large_character_list(patch_factory):
    """100 角色 → 3 次 LLM 调用（40/批），批间并集覆盖全部角色，全部拿到推荐。"""
    from backend.app.ai.factory import get_llm
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    llm = get_llm()
    assert hasattr(llm, "calls"), "测试需要 MockLLMProvider"
    before = len(llm.calls)

    characters = _batch_char_list(100)
    recs = await recommend_voices_with_llm(characters, user_id=42)

    prompts = _rec_prompts(llm, before)
    assert len(prompts) == 3, "100 角色 / 40 每批 → 应分 3 次 LLM 调用"

    seen_names: set[str] = set()
    for p in prompts:
        m = re.search(r"【角色列表】\s*(\[.*?\])\s*\n【音色列表】", p, re.DOTALL)
        assert m, "每批 prompt 应含【角色列表】JSON 块"
        names = re.findall(r'"name"\s*:\s*"([^"]+)"', m.group(1))
        assert len(names) <= 40, "单批角色数不得超过 40（防输出超限截断）"
        seen_names.update(names)
    assert len(seen_names) == 100, "各批并集应覆盖全部 100 个角色"

    # 全部角色都拿到推荐
    assert len(recs) == 100
    assert {r.character_name for r in recs} == {c.name for c in characters}


@pytest.mark.asyncio
async def test_recommend_single_batch_under_limit(patch_factory):
    """角色数 ≤ 40 时保持单次调用（不引入多余批次）。"""
    from backend.app.ai.factory import get_llm
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    llm = get_llm()
    before = len(llm.calls)

    characters = _batch_char_list(40)
    recs = await recommend_voices_with_llm(characters, user_id=42)
    assert len(_rec_prompts(llm, before)) == 1
    assert len(recs) == 40


@pytest.mark.asyncio
async def test_recommend_partial_batch_failure_keeps_others(patch_factory, monkeypatch):
    """单批失败只损失该批：其余批次照常返回（部分结果好过全军覆没）。"""
    from backend.app.ai.factory import get_llm
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    llm = get_llm()
    orig = llm.chat_structured
    state = {"n": 0}

    async def _flaky(prompt, output_schema, **kw):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("simulated output truncation")
        return await orig(prompt, output_schema, **kw)

    monkeypatch.setattr(llm, "chat_structured", _flaky)

    characters = _batch_char_list(90)  # 3 批：40 + 40 + 10
    recs = await recommend_voices_with_llm(characters, user_id=42)
    # 第 2 批失败 → 只损失该批 40 个，第 1、3 批共 50 条正常返回
    assert len(recs) == 50
    assert state["n"] == 3, "失败后应继续调用后续批次"


@pytest.mark.asyncio
async def test_recommend_quota_exhausted_propagates(patch_factory, monkeypatch):
    """配额耗尽必须穿透（调用方要靠它阻止写投毒 checkpoint），不能吞成部分结果。"""
    from backend.app.ai.base import LLMQuotaExhaustedError
    from backend.app.ai.factory import get_llm
    from backend.app.services.voice_recommender import recommend_voices_with_llm

    llm = get_llm()

    async def _quota(prompt, output_schema, **kw):
        raise LLMQuotaExhaustedError("配额已耗尽")

    monkeypatch.setattr(llm, "chat_structured", _quota)

    characters = _batch_char_list(90)
    with pytest.raises(LLMQuotaExhaustedError):
        await recommend_voices_with_llm(characters, user_id=42)
