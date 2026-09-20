"""音色推荐（基于 LLM 的特征匹配）。

聚合所有可用音色 + 当前用户的 ICL 复刻音色，按 id 去重；
prompt 给 LLM 看到每个音色的 `provider` 字段，让它能在豆包官方音色与复刻音色间选择。

历史：MiniMax TTS 已弃用，音色池不再包含 minimax 音色（仅 doubao + icl）。
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
4. 多个角色尽量不要选同一个音色，保证辨识度；若 prompt 里有【旁白音色】，该音色旁白专用，**任何角色都不得使用**
5. 返回 reason 简要说明匹配点

⚠️ 音色列表里每个音色带有 `provider` 字段（doubao / icl）：
- doubao:  豆包官方音色（带 model 字段，1.0 小模型 vs 2.0 大模型）
- icl:     当前用户上传训练的声音复刻音色（专属该用户）

你可以跨类选择：建议优先看音色特征是否匹配角色，不局限音色来源；但请在
reason 里简明说明选择理由。

输出格式：
{
  "data": [
    {"character_name": "林若雪", "suggested_voice_id": "doubao:zh_female_vv_uranus_bigtts", "reason": "17岁内向少女，匹配甜美少女音色的温柔轻声风格。"}
  ]
}
⚠️输出必须是 JSON，顶层一定有 data 字段，每个角色一条。
⚠️suggested_voice_id 必须是音色列表里出现的 id（含命名空间前缀，如 "doubao:zh_female_vv_uranus_bigtts" 或 "icl:clone_xxx"），不要自创。
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
    # 并发拉 doubao + ICL（MiniMax TTS 已弃用，不再拉 minimax 音色）
    tasks = [
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
    before = len(pool)
    pool = [v for v in pool if _is_usable_voice(v)]
    if len(pool) != before:
        logger.info(
            f"[voice_recommender] 剔除 v3 走不通的豆包 1.0 音色 "
            f"{before - len(pool)} 个（剩余 {len(pool)}）"
        )
    logger.info(
        f"[voice_recommender] 聚合音色池 user_id={user_id} count={len(pool)} "
        f"providers={sorted({v.get('provider', '?') for v in pool})}"
    )
    return pool


def _is_usable_voice(voice: dict) -> bool:
    """推荐池只保留当前豆包链路真能合成的音色。

    豆包走 v3（默认）时，v3 端点官方只支持 seed-tts-2.0 / seed-icl-2.0 两类资源；
    音色表里那 94 个 model=seed-tts-1.0 的 BV* 小模型音色会被要求 1.0 资源
    （volc.service_type.10029），账号未开通时合成必然 403 + code=45000030
    —— 推荐给用户等于保证 Build 失败。关掉 DOUBAO_TTS_USE_V3 退回 v1 时不过滤。
    """
    try:
        from ..core.config import settings
        if not bool(getattr(settings, "DOUBAO_TTS_USE_V3", True)):
            return True
    except Exception:
        # 读不到配置时不筛（宁可多给候选，也不要因配置异常清空音色池）
        return True
    from ..ai.providers.doubao.tts import is_voice_usable_on_v3
    return is_voice_usable_on_v3(voice)


# ----------------------------------------------------------------------
# 旁白音色互斥：旁白专用，不得被任何角色复用
# ----------------------------------------------------------------------

# 旁白兜底音色，口径与 build._ensure_default_narrator 一致
# （豆包 2.0 擎苍；同时兼容无前缀 legacy id）。
_DEFAULT_NARRATOR_IDS = ("doubao:zh_male_qingcang_uranus_bigtts", "zh_male_qingcang_uranus_bigtts")


def _resolve_narrator_voice_id(
    explicit: str | None,
    project: Any | None,
    pool: list[dict],
) -> str:
    """确定「旁白专用、角色不可复用」的音色 id。

    优先级：显式入参 > Project.default_narrator_voice_id > 兜底默认。

    兜底默认只在音色池里确实存在擎苍 2.0（doubao:zh_male_qingcang_uranus_bigtts）
    时才采用（与 build 的兜底同源）；池中没有该 id 时返回空串，宁可不约束也不误排一个无关音色。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    if project is not None:
        v = (getattr(project, "default_narrator_voice_id", None) or "").strip()
        if v:
            return v
    for vid in _DEFAULT_NARRATOR_IDS:
        if any(x.get("id") == vid for x in pool):
            return vid
    return ""


def _avoid_narrator_reuse(
    recs: list["VoiceRecommendation"],
    narrator_voice_id: str,
    pool: list[dict],
    characters: list[Any],
) -> list["VoiceRecommendation"]:
    """兜底：LLM 若仍把旁白音色分配给角色，改选同性别、未被占用的其他音色。

    正常情况下旁白音色已从候选池剔除，LLM 无从选中；本函数只防「模型不守约束」。
    不能直接丢弃该条推荐——合成时未分配角色的对白会回退到旁白音色
    （chapter.py `voice_assignments.get(speaker, narrator_voice_id)`），
    丢了反而会静默复用旁白。
    """
    if not recs or not narrator_voice_id:
        return recs
    used = {r.suggested_voice_id for r in recs}
    gender_by_name = {
        getattr(c, "name", ""): (getattr(c, "gender", "") or "") for c in characters
    }
    for r in recs:
        if r.suggested_voice_id != narrator_voice_id:
            continue
        want = _norm_gender_for_prompt(gender_by_name.get(r.character_name, ""))
        cands = [
            v for v in pool
            if v.get("id") != narrator_voice_id
            and v.get("id") not in used
            and _norm_gender_for_prompt(v.get("gender", "")) == want
        ] or [
            v for v in pool
            if v.get("id") != narrator_voice_id and v.get("id") not in used
        ]
        if not cands:
            logger.warning(
                f"[voice_recommender] 角色 {r.character_name} 被分到旁白音色 "
                f"{narrator_voice_id}，但池中已无可用替代音色"
            )
            continue
        new_id = cands[0]["id"]
        logger.info(
            f"[voice_recommender] 角色 {r.character_name} 的推荐音色 "
            f"{narrator_voice_id} 与旁白冲突，改为 {new_id}"
        )
        used.add(new_id)
        r.suggested_voice_id = new_id
        r.reason = f"{r.reason}（旁白音色不可复用，已改选）"
    return recs


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------


async def recommend_voices_with_llm(
    characters: list[Any],
    *,
    user_id: int | None = None,
    project_id: str | None = None,
    narrator_voice_id: str | None = None,
) -> list[VoiceRecommendation]:
    """为角色列表生成音色推荐。

    旁白音色是**旁白专用**的：它会被移出角色候选池，LLM 无从把它分配给角色；
    LLM 若不守约束仍返回了它，还会被 `_avoid_narrator_reuse` 改选掉。

    Args:
        characters: 角色列表（Character pydantic）
        user_id: 当前用户 id；非 None 时把该用户的 ICL 复刻音色纳入候选池
        project_id: 项目 id；用于读取项目偏好与项目旁白音色
        narrator_voice_id: 旁白音色 id；显式传入时优先于项目设置

    Returns:
        每个角色一条建议（LLM 输出）
    """
    pool = await _aggregate_voice_pool(user_id)

    project = None
    if project_id:
        try:
            from ..db.session import get_session_factory
            from ..db.models import Project
            factory = get_session_factory()
            async with factory() as s:
                project = await s.get(Project, project_id)
        except Exception as e:
            logger.warning(
                f"[voice_recommender] 读取项目失败 project_id={project_id}: "
                f"{type(e).__name__}: {e}"
            )
            project = None

    # 旁白音色：从候选池整体剔除，保证推荐结果不会与旁白撞车
    narrator_id = _resolve_narrator_voice_id(narrator_voice_id, project, pool)
    if narrator_id:
        remaining = [v for v in pool if v.get("id") != narrator_id]
        if remaining:
            pool = remaining
        else:
            logger.warning(
                f"[voice_recommender] 候选池只剩旁白音色 {narrator_id}，无法剔除"
            )

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
    if project is not None and project.default_tts_provider:
        # 透传项目偏好给 LLM（软提示，不强制）
        project_hint = (
            f"\n【项目偏好】本项目 default_tts_provider={project.default_tts_provider}；"
            f"如选其他厂商音色，build 时可能不直接生效（前端会提示）。"
        )

    narrator_hint = ""
    if narrator_id:
        narrator_hint = (
            f"\n【旁白音色】本项目旁白使用 `{narrator_id}`，该音色为旁白专用，"
            f"**不得分配给任何角色**（已从下方音色列表中移除）。\n"
        )

    prompt = (
        PROMPT_BASE
        + project_hint
        + narrator_hint
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
    return _avoid_narrator_reuse(wrapped.data, narrator_id, pool, characters)


def _norm_gender_for_prompt(gender: str) -> str:
    """统一音色 gender 字符串为"男声/女声/中性"。

    doubao 是 male/female/neutral；icl 是中性；同时兼容中文写法。
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