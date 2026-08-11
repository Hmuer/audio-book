from __future__ import annotations
import json
from pydantic import BaseModel

from ..ai.factory import get_llm, get_tts
from .character import Character


class VoiceMeta(BaseModel):
    id: str
    name: str
    gender: str
    description: str


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

输出格式：
{
  "data": [
    {"character_name": "林若雪", "suggested_voice_id": "female_tianmei_01", "reason": "17岁内向少女，匹配甜美少女音色的温柔轻声风格。"}
  ]
}
⚠️输出必须是 JSON，顶层一定有 data 字段，每个角色一条。
"""


async def recommend_voices_with_llm(
    characters: list[Character],
) -> list[VoiceRecommendation]:
    tts = get_tts()
    voices_raw = await tts.list_voices()
    # 标准化
    voices: list[VoiceMeta] = [
        VoiceMeta(
            id=v["id"],
            name=v.get("name", v["id"]),
            gender=v.get("gender", "中性"),
            description=v.get("description", ""),
        )
        for v in voices_raw
    ]

    prompt = (
        PROMPT_BASE
        + "\n【角色列表】\n"
        + json.dumps([c.model_dump() for c in characters], ensure_ascii=False, indent=2)
        + "\n【音色列表】\n"
        + json.dumps([v.model_dump() for v in voices], ensure_ascii=False, indent=2)
    )

    class _Wrapper(BaseModel):
        data: list[VoiceRecommendation]

    llm = get_llm()
    wrapped = await llm.chat_structured(
        prompt=prompt,
        output_schema=_Wrapper,
        temperature=0.2,
        max_tokens=8000,
    )
    return wrapped.data
