from __future__ import annotations
from pydantic import BaseModel

from ..ai.factory import get_llm
from .character import Character


FEW_SHOT = r"""
你是一名小说对白标注员。给定小说正文和已识别的角色列表，请找出文中每一段对白（引号内的说话内容），并判断说话人是谁。
规则（非常重要）：
1. **绝对禁止**使用 narrator/unknown/旁白/其他 作为 speaker，必须从给定角色列表中选一个最可能的。
2. 如果对白前有"XX 说/道/喊/回答/冷喝/喃喃"等提示词，优先用提示词。
3. 如果没有提示词，根据上下文语境、角色性格、对话内容风格合理推断。
4. anchor 的 start/end 是对白原文（包含引号）在正文中的 0-indexed **字符位置**（Python 字符串索引，不是字节位置）。即 `text[start:end]` 应严格等于 anchor.text。
5. confidence 0-1：0.7 以下表示你不太确定，让人工复核。
6. 每段对白 text 字段去掉引号后的纯对白文本。

【示例】
正文：
林若雪低着头，手里捏着衣角。李明走过来拍她肩膀：「怎么了？谁欺负你了？」
「没……没什么。」她小声说。
「还说没什么，眼睛都红了。」王大爷从远处走来，手里拿着两串糖葫芦。
角色列表：[{"name":"林若雪"},{"name":"李明"},{"name":"王大爷"}]

输出：
{
  "data": [
    {"anchor": {"text": "「怎么了？谁欺负你了？」", "start": 24, "end": 40}, "speaker": "李明", "confidence": 0.98, "text": "怎么了？谁欺负你了？"},
    {"anchor": {"text": "「没……没什么。」", "start": 43, "end": 54}, "speaker": "林若雪", "confidence": 0.95, "text": "没……没什么。"},
    {"anchor": {"text": "「还说没什么，眼睛都红了。」", "start": 68, "end": 86}, "speaker": "王大爷", "confidence": 0.92, "text": "还说没什么，眼睛都红了。"}
  ]
}
"""


class Anchor(BaseModel):
    text: str
    start: int
    end: int


class DialogueAttribution(BaseModel):
    anchor: Anchor
    speaker: str
    confidence: float
    text: str


async def attribute_dialogues_with_llm(
    text: str, characters: list[Character]
) -> list[DialogueAttribution]:
    import json as _json

    names = [c.model_dump() for c in characters]
    prompt = (
        FEW_SHOT
        + "\n【现在处理以下正文】\n---TEXT START---\n"
        + text
        + "\n---TEXT END---\n"
        + f"\n可用角色列表：{_json.dumps(names, ensure_ascii=False)}"
        + "\n\nspeaker 必须是角色列表中的 name 之一，绝对不许 narrator/unknown/旁白！"
        + "\n⚠️输出格式必须是 {\"data\": [DialogueAttribution,...]}，顶层一定要有 data 字段!"
    )

    class _Wrapper(BaseModel):
        data: list[DialogueAttribution]

    llm = get_llm()
    wrapped = await llm.chat_structured(
        prompt=prompt,
        output_schema=_Wrapper,
        temperature=0.1,
        max_tokens=16000,
    )
    return wrapped.data
