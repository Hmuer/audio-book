"""音色推荐（基于 LLM 的特征匹配）。

历史 bug：原本只取单一 provider 的音色（默认 minimax），导致推荐结果永远是
minimax 的 id，豆包官方 200+ 音色和用户 ICL 复刻音色从未出现在候选池。

修复：聚合所有已启用厂商的音色 + 当前用户的 ICL 复刻音色，按 id 去重；
prompt 给 LLM 看到每个音色的 `provider` 字段，让它能跨厂商选择。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel

from ..ai.factory import get_tts

logger = logging.getLogger("novel-tts")


class VoiceMeta(BaseModel):
    id: str
    name: str
    gender: str
    description: str
    provider: str = ""


class VoiceRecommendation(BaseModel):
    character_name: str
    suggested_voice_id: str
    reason: str


PROMPT_BASE = r"""
你是一名有声小说配音导演。请为每个角色从给定的音色列表中挑选最合适的一个。
匹配策略（按优先级）：
1. 性别必须匹配或兼容：男角色→男声；女角色→女声；老年角色可以选偏低沉的；中性角色选"中性"音色或其他合适的
2. 年龄感匹配：老年→有"沧桑/老年"标签；少女→甜/少女标签；小孩→童声；中年→沉稳/雅致
3. 性格匹配：开朗→有活力；内向→温柔轻声；威严→厚重沉稳；古灵精怪→俏皮灵动
4. 多个角色尽量不要选同一个音色，保证辨识度
5. 返回 reason 简要说明匹配点

⚠️ 音色列表里每个音色带有 `provider` 字段（minimax / doubao / icl）：
- minimax: MiniMax 官方音色
- doubao:  豆包官方音色（带 model 字段，1.0 小模型 vs 2.0 大模型）
- icl:     当前用户上传训练的声音复刻音色（专属该用户）

你可以跨厂商选择：建议优先看音色特征是否匹配角色，不局限厂商；但请在
reason 里简明说明选择理由。LLM 选出来的 voice_id 在 build 阶段如果与项目
默认 TTS 厂商不一致，前端会提示用户手动切换或重新推荐。

输出格式：
{
  "data": [
    {"character_name": "林若雪", "suggested_voice_id": "female-tianmei", "reason": "17岁内向少女，匹配甜美少女音色的温柔轻声风格。"}
  ]
}
⚠️输出必须是 JSON，顶层一定有 data 字段，每个角色一条。
⚠️suggested_voice_id 必须是音色列表里出现的 id（含命名空间前缀，如 "minimax:female-tianmei" 或 "doubao:zh_female_vv_uranus_bigtts" 或 "icl:clone_xxx"），不要自创。
"""


# ----------------------------------------------------------------------
# 音色池聚合
# ----------------------------------------------------------------------


async def _fetch_provider_voices(provider: str) -> list[dict]:
    """并发拉取单个 provider 的音色，异常降级返回空列表。"""
    try:
        tts = get_tts(provider)
        return await tts.list_voices()
    except Exception as e:
        logger.warning(
            f"[voice_recommender] provider={provider} list_voices 失败: "
            f"{type(e).__name__}: {e}"
        )
        return []


async def _fetch_icl_voices(user_id: int | None) -> list[dict]:
    """拉取当前用户可用的 ICL 复刻音色；user_id 为 None 时跳过。"""
    if user_id is None:
        return []
    try:
        from .icl import icl_voices_for_user
        return await icl_voices_for_user(user_id)
    except Exception as e:
        logger.warning(
            f"[voice_recommender] ICL 音色聚合失败 user_id={user_id}: "
            f"{type(e).__name__}: {e}"
        )
        return []


async def _aggregate_voice_pool(user_id: int | None = None) -> list[dict]:
    """聚合多厂商音色 + 当前用户 ICL 复刻音色，按 id 去重。

    返回 list[dict]，每个 dict 至少含 id/name/gender/description/provider 字段。
    """
    # 并发拉 minimax + doubao + ICL
    tasks = [
        _fetch_provider_voices("minimax"),
        _fetch_provider_voices("doubao"),
        _fetch_icl_voices(user_id),
    ]
    results = await asyncio.gather(*tasks)
    merged: dict[str, dict] = {}
    for voices in results:
        for v in voices:
            vid = v.get("id")
            if not vid:
                continue
            # 后到的同 id（理论上不应该出现）覆盖前者；保留 provider 信息
            merged[vid] = v
    pool = list(merged.values())
    logger.info(
        f"[voice_recommender] 聚合音色池 user_id={user_id} count={len(pool)} "
        f"providers={sorted({v.get('provider', '?') for v in pool})}"
    )
    return pool


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------


async def recommend_voices_with_llm(
    characters: list[Any],
    *,
    user_id: int | None = None,
    project_id: str | None = None,
) -> list[VoiceRecommendation]:
    """为角色列表生成音色推荐。

    Args:
        characters: 角色列表（Character pydantic）
        user_id: 当前用户 id；非 None 时把该用户的 ICL 复刻音色纳入候选池
        project_id: 项目 id（保留用于未来按项目偏好约束；本轮不强制使用）

    Returns:
        每个角色一条建议（LLM 输出）
    """
    pool = await _aggregate_voice_pool(user_id)

    # 标准化音色元数据（LLM prompt 输入）
    voices: list[VoiceMeta] = [
        VoiceMeta(
            id=v["id"],
            name=v.get("name", v["id"]),
            gender=_norm_gender_for_prompt(v.get("gender", "中性")),
            description=v.get("description", ""),
            provider=v.get("provider", ""),
        )
        for v in pool
    ]

    project_hint = ""
    if project_id:
        # 透传项目偏好给 LLM（软提示，不强制）
        try:
            from ..db.session import get_session_factory
            from ..db.models import Project
            factory = get_session_factory()
            async with factory() as s:
                p = await s.get(Project, project_id)
                if p and p.default_tts_provider:
                    project_hint = (
                        f"\n【项目偏好】本项目 default_tts_provider={p.default_tts_provider}；"
                        f"如选其他厂商音色，build 时可能不直接生效（前端会提示）。"
                    )
        except Exception:
            project_hint = ""

    prompt = (
        PROMPT_BASE
        + project_hint
        + "\n【角色列表】\n"
        + json.dumps([_character_dump(c) for c in characters], ensure_ascii=False, indent=2)
        + "\n【音色列表】\n"
        + json.dumps([v.model_dump() for v in voices], ensure_ascii=False, indent=2)
    )

    class _Wrapper(BaseModel):
        data: list[VoiceRecommendation]

    from ..ai.factory import get_llm
    llm = get_llm()
    wrapped = await llm.chat_structured(
        prompt=prompt,
        output_schema=_Wrapper,
        temperature=0.2,
        max_tokens=8000,
        use_fast_model=True,  # 音色推荐是"特征匹配"任务；M2.7-highspeed 准确率足够且速度快
    )
    from .usage import track_llm
    track_llm(calls=1, chars=len(prompt), detail="voice_recommend")
    return wrapped.data


def _norm_gender_for_prompt(gender: str) -> str:
    """统一音色 gender 字符串为"男声/女声/中性"。

    minimax 已是男声/女声/中性；doubao 是 male/female/neutral；icl 是中性。
    """
    g = (gender or "").strip().lower()
    if g in ("male", "男", "男声", "m"):
        return "男声"
    if g in ("female", "女", "女声", "f"):
        return "女声"
    return "中性"


def _character_dump(c: Any) -> dict:
    """把 Character 模型安全 dump 成 dict，兼容 pydantic v1/v2。"""
    if hasattr(c, "model_dump"):
        return c.model_dump()
    if hasattr(c, "dict"):
        return c.dict()
    return dict(c)