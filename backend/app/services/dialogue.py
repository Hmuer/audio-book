from __future__ import annotations
import logging
from pydantic import BaseModel

from ..ai.factory import get_llm
from .character import Character
from .usage import track_llm


logger = logging.getLogger(__name__)


FEW_SHOT = r"""
你是一名小说对白标注员。给定小说正文和已识别的角色列表，请找出文中每一段对白（引号内的说话内容），并判断说话人是谁。
规则（非常重要）：
1. **绝对禁止**使用 narrator/unknown/旁白/其他 作为 speaker。优先从给定角色列表中选；若说话人明显是列表外的人物，按正文中出现的名字原样输出（从"XX 说/道"的提示词里抄名字）。
2. 如果对白前有"XX 说/道/喊/回答/冷喝/喃喃"等提示词，优先用提示词。
3. 如果没有提示词，根据上下文语境、角色性格、对话内容风格合理推断。
4. anchor 的 start/end 是对白原文（包含引号）在**该章的 chapter_text 里**的 0-indexed **字符位置**（Python 字符串索引，不是字节位置）。即 `text[start:end]` 应严格等于 anchor.text。
   ⚠️ anchor.text 字段**必须输出**（与 start/end 一起，不可省略、不可用 null 代替）。
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
    # H-12：text 可选化（M3 实测会省略 anchor.text 只给 start/end —— 142 条校验
    # 错误导致 344s 的大批调用整批作废、重试 3 次全废）。text 缺失时由
    # _backfill_anchor_text 用 chapter_text[start:end] 本地回填，不再依赖 LLM 重试。
    text: str | None = None
    start: int
    end: int


class DialogueAttribution(BaseModel):
    anchor: Anchor
    speaker: str
    confidence: float
    # H-12：对白 text 同样可选化（M3 偶发省略）；_backfill_anchor_text
    # 会用 anchor 切片去引号兜底 → 流出的对象 text 永远非空
    text: str | None = None


def _backfill_anchor_text(
    dialogues: list[DialogueAttribution],
    chapter_text: str,
    *,
    chapter_idx: int | None = None,
) -> list[DialogueAttribution]:
    """H-12：anchor.text 本地回填 + 越界过滤。

    M3 实测（app.log 2026-10-08）：对白归属批返回 anchor 只有 {start, end}，
    缺 text → 142 条 ValidationError → 344s/次的批调用整批作废重试 3 次。
    anchor.start/end 本就是该章 chapter_text 的 0-indexed 偏移，
    text 完全可以本地切片回填，一次调用即成功、不再重试。

    回填后 chapter.py 的「anchor_text 精确定位修正」也恢复工作
    （此前空串落库会退化为裸 start/end 定位）。

    过滤规则（宁缺勿错）：start/end 越界（负数 / end>章长 / end<=start）
    的条目丢弃并计数 warning（一批一条汇总日志，不刷屏）。
    """
    kept: list[DialogueAttribution] = []
    dropped = 0
    ch_len = len(chapter_text)
    where = f"chapter_idx={chapter_idx} " if chapter_idx is not None else ""
    for d in dialogues:
        a = d.anchor
        text_ok = bool(a.text and a.text.strip())
        pos_ok = 0 <= a.start < a.end <= ch_len
        if text_ok and pos_ok:
            kept.append(d)
            continue
        if not pos_ok:
            # 位置非法：无法回填也无法定位 → 丢弃
            dropped += 1
            continue
        # text 缺失但位置合法 → 切片回填（并同时修正对白 text 缺引号场景）
        backfilled = chapter_text[a.start:a.end]
        d.anchor = Anchor(text=backfilled, start=a.start, end=a.end)
        if not (d.text and d.text.strip()):
            # 对白 text 也缺失：从回填的 anchor 去引号兜底
            stripped = backfilled.strip().strip("「」『』“”\"'")
            d.text = stripped
        kept.append(d)
    if dropped:
        logger.warning(
            f"[dialogue] {where}anchor 越界/无效丢弃 {dropped} 条对白"
            f"（start/end 不在 [0, {ch_len}) 区间或 end<=start）"
        )
    return kept


async def attribute_dialogues_with_llm(
    text: str, characters: list[Character]
) -> list[DialogueAttribution]:
    """单章对白归属（保留原接口，兼容未切批的调用方）。"""
    import json as _json

    # H-24：只传名字不传全量 model_dump。归属任务的输出是「从角色列表里选
    # name」，性别/年龄/人设描述对选名字零信息增益；大书角色列表可达
    # 100k+ 字符，塞进每个批 prompt 纯烧 token（实测 27 批 × ~75k 字符
    # ≈ 2M 字符 ≈ 55 万 token 的无效输入）。
    names = [c.name for c in characters]
    prompt = (
        FEW_SHOT
        + "\n【现在处理以下正文（单章）】\n---TEXT START---\n"
        + text
        + "\n---TEXT END---\n"
        + f"\n可用角色列表：{_json.dumps(names, ensure_ascii=False)}"
        + "\n\nspeaker 优先从角色列表中选；说话人明显是列表外人物时按正文里的名字原样输出，绝对不许 narrator/unknown/旁白！"
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
    track_llm(calls=1, chars=len(prompt), detail="dialogue")
    # H-12：anchor.text 本地回填（text=None 但位置合法 → 切片回填；越界 → 丢弃）
    return _backfill_anchor_text(wrapped.data, text)


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
1. **绝对禁止**使用 narrator/unknown/旁白/其他 作为 speaker。优先从给定角色列表里选；说话人明显是列表外人物时，按正文中出现的名字原样输出（从"XX 说/道"的提示词里抄名字）。
2. 如果对白前有"XX 说/道/喊/回答/冷喝/喃喃"等提示词，优先用提示词。
3. anchor 的 start/end 是对白原文（含引号）**在该章 chapter_text 内部**的 0-indexed 字符位置——
   ⚠️ 不是整本书里的位置！必须是 `chapter_text[start:end] == anchor.text`。
   anchor.text 字段**必须输出**（与 start/end 一起，不可省略、不可用 null 代替）。
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
        LLM 实际返回的章节结果列表（chapter_idx 与输入对应）。
        缺失的章节不出现在返回里（H-24：不补空占位——补空会让调用方
        把该章误标 completed，静默丢对白；缺失章保持 pending 由重跑补跑）。
    """
    import json as _json

    # H-24：同单章路径 —— 只传名字（角色描述对「选 speaker 名字」零信息增益，
    # 全量 dump 是每批 ~75k 字符的纯浪费，详见上方注释）
    names = [c.name for c in characters]

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
    track_llm(calls=1, chars=len(prompt), detail="dialogue_batch")
    results = wrapped.data

    # H-12：逐章回填 anchor.text（M3 实测会省略；批调用 344s/次，
    # 校验整批作废重试 3 次全废 —— 回填后一次即成功）
    text_by_idx = {idx: t for idx, t in chapters}
    for r in results:
        if r.chapter_idx in text_by_idx:
            r.dialogues = _backfill_anchor_text(
                r.dialogues, text_by_idx[r.chapter_idx], chapter_idx=r.chapter_idx
            )

    # 按 chapter_idx 做成 map；重复/越界忽略
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

    # H-24：缺失章**不补空占位**——实测 383 章一次跑缺失 59 章，旧逻辑补
    # dialogues=[] 会让调用方把该章标 completed，空对白进 DB 且永远不会被
    # 补跑（静默丢对白）。改为缺失章不出现在返回里 → 调用方保持该章
    # pending → 重跑 prepare 自动补跑。
    final: list[ChapterDialogueBatchResult] = []
    for idx, _ in chapters:
        if idx in got_map:
            final.append(got_map[idx])
        else:
            logger.warning(
                f"[dialogue_batch] LLM 输出缺失 chapter_idx={idx}，"
                f"该章不计入本批结果（保持未完成，重跑 prepare 自动补跑）"
            )
    return final
