"""音色推荐：聚合豆包官方 + ICL 复刻的候选池 + prompt 透传测试。

覆盖：
  T-VR-1  _aggregate_voice_pool 聚合 doubao + 用户 ICL 两方
  T-VR-2  _aggregate_voice_pool 在 user_id=None 时跳过 ICL
  T-VR-3  recommend_voices_with_llm 的 prompt 同时含 doubao/icl 两方音色 id
  T-VR-4  recommend_voices_with_llm 的 prompt 含 provider 字段
  T-VR-5  recommend_voices_with_llm 单 provider 异常时降级（其他仍能返回）
  T-VR-6  recommend_voices_with_llm 向后兼容旧调用（不传 user_id / project_id）

历史：MiniMax TTS 已弃用，候选池不再聚合 minimax 音色。
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

    get_tts("doubao") 在测试默认会被 conftest 注入的 MockTTSProvider 拦截；
    为了让 doubao 返回定制"豆包"音色，需要替换 service 层的辅助函数。
    """
    from backend.app.services import voice_recommender as vr

    doubao_voices = _make_doubao_voices()
    # 用一个可变容器，便于单测 override 抛错场景
    state = {
        "doubao": doubao_voices,
        "icl": lambda user_id: _make_icl_voices(user_id) if user_id else [],
        "icl_raise": False,
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
# T-VR-1：聚合 doubao + ICL
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_voice_pool_includes_all_providers(patch_factory):
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    pool = await _aggregate_voice_pool(user_id=42)
    ids = {v["id"] for v in pool}
    # doubao + icl 都有
    assert "doubao:zh_female_vv_uranus_bigtts" in ids
    assert "icl:clone_abc" in ids
    # BV120 是 model=seed-tts-1.0 的小模型音色，v3 端点不支持该资源
    # （实测 403 + code=45000030），推荐池里必须已经剔除
    assert "doubao:BV120_streaming" not in ids
    # 共 3 个（2 doubao + 1 icl；BV120 被过滤）
    assert len(pool) == 3
    # 每项带 provider 字段
    providers = {v.get("provider") for v in pool}
    assert providers == {"doubao", "icl"}


# ---------------------------------------------------------------------
# T-VR-2：user_id=None 时不含 ICL
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_voice_pool_no_icl_when_user_none(patch_factory):
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    pool = await _aggregate_voice_pool(user_id=None)
    providers = {v.get("provider") for v in pool}
    assert "icl" not in providers
    assert providers == {"doubao"}


# ---------------------------------------------------------------------
# T-VR-5：单 provider 抛错时降级
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aggregate_voice_pool_degrades_on_provider_failure(patch_factory):
    """doubao 抛错时，icl 仍返回。"""
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    patch_factory["doubao_raise"] = True
    pool = await _aggregate_voice_pool(user_id=1)
    ids = {v["id"] for v in pool}
    # doubao 音色没拿到，但 icl 仍在
    assert not any(v.startswith("doubao:") for v in ids)
    assert "icl:clone_abc" in ids


@pytest.mark.asyncio
async def test_aggregate_voice_pool_degrades_on_icl_failure(patch_factory):
    """icl 抛错时，doubao 仍返回。"""
    from backend.app.services.voice_recommender import _aggregate_voice_pool

    patch_factory["icl_raise"] = True
    pool = await _aggregate_voice_pool(user_id=1)
    ids = {v["id"] for v in pool}
    assert not any(v.startswith("icl:") for v in ids)
    assert any(v.startswith("doubao:") for v in ids)


# ---------------------------------------------------------------------
# T-VR-3 / T-VR-4：prompt 同时含两类音色 id + provider 字段
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recommend_prompt_contains_all_providers(patch_factory):
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

    # T-VR-3：doubao / icl 音色 id 都出现
    assert "doubao:zh_female_vv_uranus_bigtts" in prompt
    assert "icl:clone_abc" in prompt

    # T-VR-4：每条音色带 provider 字段
    # 解析 【音色列表】 块的 JSON
    m = re.search(r"【音色列表】\s*(\[.*?\])\s*$", prompt, re.DOTALL)
    assert m, "prompt 末尾应有【音色列表】JSON 块"
    voices_block = json.loads(m.group(1))
    assert isinstance(voices_block, list) and len(voices_block) >= 3
    # 每条音色 dict 含 provider 字段
    for v in voices_block:
        assert "id" in v and "provider" in v, f"音色 {v} 缺少 id/provider"
    providers = {v["provider"] for v in voices_block}
    assert providers == {"doubao", "icl"}


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


# ---------------------------------------------------------------------
# T-VR-7：大角色列表分批推荐（真实案例：798 角色单批输出 >8000 token 被截断，
#         3 次重试全废 → 全部角色无自动音色）
# ---------------------------------------------------------------------
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
