from __future__ import annotations
import logging
from pydantic import BaseModel

from ..ai.factory import get_llm
from .character import Character


logger = logging.getLogger(__name__)


FEW_SHOT = r"""
你是一名小说对白标注员。给定小说正文和已识别的角色列表，请找出文中每一段对白（引号内的说话内容），并判断说话人是谁。
规则（非常重要）：
1. **绝对禁止**使用 narrator/unknown/旁白/其他 作为 speaker，必须从给定角色列表中选一个最可能的。
2. 如果对白前有"XX 说/道/喊/回答/冷喝/喃喃"等提示词，优先用提示词。
3. 如果没有提示词，根据上下文语境、角色性格、对话内容风格合理推断。
4. anchor 的 start/end 是对白原文（包含引号）在**该章的 chapter_text 里**的 0-indexed **字符位置**（Python 字符串索引，不是字节位置）。即 `text[start:end]` 应严格等于 anchor.text。
5. confidence 0-1：0.7 以下表示你不太确定，让人工复核。
6. 每段对白 text 字段去掉引号后的纯对白文本。

【示例】
章节正文：
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
    # text 可省略：LLM 在长上下文里经常只填 start/end 不填 text，
    # 让其可选可避免 ValidationError；下游消费时 anchor.text 缺失会回退到空串，
    # 再由 chapter.text[start:end] 兜底。
    text: str | None = None
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
    """单章对白归属（保留原接口，兼容未切批的调用方）。"""
    import json as _json

    names = [c.model_dump() for c in characters]
    prompt = (
        FEW_SHOT
        + "\n【现在处理以下正文（单章）】\n---TEXT START---\n"
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
        use_fast_model=True,  # 对白归属任务结构化、prompt 内带角色名/少样本；M2.7-highspeed 足够快
    )
    return wrapped.data


# =====================================================================
# 批量对白归属（14 章/批，一次 LLM 请求处理多章）
# =====================================================================


class ChapterDialogueBatchResult(BaseModel):
    """单章对白归属，携带 chapter_idx 以便调用方按章归位。"""
    chapter_idx: int
    dialogues: list[DialogueAttribution]


class DialogueBatchResponse(BaseModel):
    data: list[ChapterDialogueBatchResult]


_BATCH_PROLOGUE = r"""
你是一名小说对白标注员。**一次处理多个章节**，对每个章节分别输出对白归属结果。
每个章节的规则完全相同（单章规则复述一遍）：
1. **绝对禁止**使用 narrator/unknown/旁白/其他 作为 speaker，必须从给定角色列表里选。
2. 如果对白前有"XX 说/道/喊/回答/冷喝/喃喃"等提示词，优先用提示词。
3. anchor 的 start/end 是对白原文（含引号）**在该章 chapter_text 内部**的 0-indexed 字符位置——
   ⚠️ 不是整本书里的位置！必须是 `chapter_text[start:end] == anchor.text`。
4. confidence 0-1，0.7 以下表示不确定。
5. 每段对白 text 字段去掉引号后的纯对白文本。

⚠️ **批量输出格式严格要求**：
- 顶层是一个对象 `{"data": [...]}`
- data 里的每一项对应一个输入章节，必须携带 `chapter_idx`（原样返回输入给你的那个数字），
  以及 `dialogues: [...]`，里面装该章的对白归属。
- 如果某一章完全没有对白，也必须输出 `{"chapter_idx": xxx, "dialogues": []}` 占位置。
- 不要把不同章节的对白混到同一个 dialogues 列表里。
"""


async def attribute_dialogues_batch_with_llm(
    chapters: list[tuple[int, str]],
    characters: list[Character],
) -> list[ChapterDialogueBatchResult]:
    """
    批量对白归属：一次 LLM 请求处理 N 章。

    Args:
        chapters: [(chapter_idx, chapter_text), ...]，chapter_idx 会原样写进响应，
                  用于调用方知道结果属于哪一章。
        characters: 角色列表（整本共用）。

    Returns:
        与输入长度相同、chapter_idx 一一对应的结果列表。
        若 LLM 输出少了某一章，会补空 dialogues 占位并打 warning。
    """
    import json as _json

    names = [c.model_dump() for c in characters]

    # 每个章节单独封装一个 "CHAPTER_XXX" 块，让 LLM 清楚章节边界。
    chapter_blocks: list[str] = []
    for idx, text in chapters:
        chapter_blocks.append(
            f"===== CHAPTER idx={idx} START =====\n"
            f"{text}\n"
            f"===== CHAPTER idx={idx} END ====="
        )

    prompt = (
        _BATCH_PROLOGUE
        + "\n\n【以下是本次要处理的所有章节】\n\n"
        + "\n\n".join(chapter_blocks)
        + "\n\n【角色列表（所有章节共用）】\n"
        + _json.dumps(names, ensure_ascii=False, indent=2)
        + f"\n\n⚠️ 一共需要输出 {len(chapters)} 个 ChapterDialogueBatchResult，"
        + "chapter_idx 必须和输入里的 idx 一致，顺序任意，但必须覆盖全部章节。"
        + " 某章没对白也要输出 dialogues=[] 占位。"
        + "\n⚠️ 顶层格式必须是 {\"data\": [ChapterDialogueBatchResult, ...]}，顶层一定要有 data 字段!"
    )

    llm = get_llm()
    wrapped = await llm.chat_structured(
        prompt=prompt,
        output_schema=DialogueBatchResponse,
        temperature=0.1,
        # 单章 16k × 14 章 ≈ 224k tokens 输出上限。正常情况下对白远没那么多，
        # 这里设 96k 既能容纳异常长输出又不至于触发模型本身 max_tokens 限制。
        max_tokens=96000,
        use_fast_model=True,
    )
    results = wrapped.data

    # 按 chapter_idx 做成 map，缺的补空
    idx_set = {idx for idx, _ in chapters}
    got_map: dict[int, ChapterDialogueBatchResult] = {}
    for r in results:
        if r.chapter_idx in idx_set and r.chapter_idx not in got_map:
            got_map[r.chapter_idx] = r
        else:
            logger.warning(
                f"[dialogue_batch] LLM 返回重复/越界 chapter_idx={r.chapter_idx}，"
                f"已忽略（需要的 idx={sorted(idx_set)}）"
            )

    final: list[ChapterDialogueBatchResult] = []
    for idx, _ in chapters:
        if idx in got_map:
            final.append(got_map[idx])
        else:
            logger.warning(
                f"[dialogue_batch] LLM 输出缺失 chapter_idx={idx}，"
                f"补空 dialogues=[] 占位（后续可单独重跑该章）"
            )
            final.append(ChapterDialogueBatchResult(chapter_idx=idx, dialogues=[]))
    return final
