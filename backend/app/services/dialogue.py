from __future__ import annotations
import asyncio
import logging
import re
from pydantic import BaseModel

from ..ai.factory import get_llm
from ..core.config import settings
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
    # 模型偶发只给 text/speaker；本地用原文定位，定位不到才丢弃该条。
    anchor: Anchor | None = None
    speaker: str
    confidence: float = 0.5
    # H-12：对白 text 同样可选化（M3 偶发省略）；_backfill_anchor_text
    # 会用 anchor 切片去引号兜底 → 流出的对象 text 永远非空
    text: str | None = None


def _relocate_positions(chapter_text: str, needle: str, hint: int) -> list[int]:
    """在 chapter_text 中找出 needle 的全部出现位置（防御上限 20 个）。

    hint（LLM 给出的原 start，可能系统性偏移不准）用于调用方挑选
    「离原偏移最近的匹配」——M3 的偏移经常是接近正确但差几个字符。
    """
    if not needle:
        return []
    out: list[int] = []
    start = 0
    while len(out) < 20:
        pos = chapter_text.find(needle, start)
        if pos < 0:
            break
        out.append(pos)
        start = pos + 1
    return out


# 对白 text 通常是去引号形式，正文里带引号 → 二次定位时尝试常见引号包裹
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"))


def _backfill_anchor_text(
    dialogues: list[DialogueAttribution],
    chapter_text: str,
    *,
    chapter_idx: int | None = None,
) -> list[DialogueAttribution]:
    """H-12：anchor.text 本地回填 + 越界二次定位 + 丢弃过滤。

    M3 实测（app.log 2026-10-08）：对白归属批返回 anchor 只有 {start, end}，
    缺 text → 142 条 ValidationError → 344s/次的批调用整批作废重试 3 次。
    anchor.start/end 本就是该章 chapter_text 的 0-indexed 偏移，
    text 完全可以本地切片回填，一次调用即成功、不再重试。

    回填后 chapter.py 的「anchor_text 精确定位修正」也恢复工作
    （此前空串落库会退化为裸 start/end 定位）。

    H-26 越界二次定位（798 角色实测 app.log 2026-10-10）：M3 的字符偏移
    系统性不可靠（数不准字符位置），全书 496 条对白（4.2%）因 start/end
    非法被直接丢弃 = 有声书静默丢台词。但 LLM 返回的对白 text 本身
    可信——用它在正文中 find 定位即可重建 anchor：
    - 候选 needle：anchor.text → 对白 text 原样 → 常见引号包裹的 text
    - LLM 偏移经常「接近正确」→ 多处匹配时取离原 start 最近的一个
    - 全部 needle 都找不到才丢弃（此时确实无法定位）
    """
    kept: list[DialogueAttribution] = []
    dropped = 0
    relocated = 0
    ch_len = len(chapter_text)
    where = f"chapter_idx={chapter_idx} " if chapter_idx is not None else ""
    search_from = 0
    for d in dialogues:
        a = d.anchor
        if a is None:
            # 缺 anchor 时用模型给的对白文本在原文中寻找带引号的片段。
            # 先向后找，避免同一句对白多次出现时全部锚到首次出现位置。
            needle = (d.text or "").strip().strip('「」『』“”"')
            if not needle:
                dropped += 1
                continue
            candidates = [left + needle + right for left, right in _QUOTE_PAIRS]
            hits = [(pos, candidate) for candidate in candidates
                    if (pos := chapter_text.find(candidate, search_from)) >= 0]
            if not hits:
                hits = [(pos, candidate) for candidate in candidates
                        if (pos := chapter_text.find(candidate)) >= 0]
            if not hits:
                dropped += 1
                continue
            pos, match_text = min(hits, key=lambda item: item[0])
            a = Anchor(text=match_text, start=pos, end=pos + len(match_text))
            d.anchor = a
        text_ok = bool(a.text and a.text.strip())
        pos_ok = 0 <= a.start < a.end <= ch_len
        if text_ok and pos_ok:
            if not (d.text and d.text.strip()):
                d.text = (a.text or "").strip().strip("「」『』“”\"'")
            kept.append(d)
            search_from = max(search_from, a.end)
            continue
        if not pos_ok:
            # 位置非法 → 用文本在正文中二次定位重建 anchor
            dt = (d.text or "").strip()
            needles: list[str] = []
            if dt:
                # 引号包裹形式优先：命中即对齐正文完整引号区间；
                # 原样（去引号）会命中引号内部，anchor 两端少引号
                needles.extend(f"{o}{dt}{c}" for o, c in _QUOTE_PAIRS)
            if text_ok:
                at = (a.text or "").strip()
                if at and at not in needles:
                    needles.append(at)
            if dt and dt not in needles:
                needles.append(dt)
            found = -1
            for n in needles:
                if not n:
                    continue
                positions = _relocate_positions(chapter_text, n, a.start)
                if not positions:
                    continue
                # 偏移虽不可靠但常「接近正确」→ 取离原 start 最近的匹配
                if 0 <= a.start < ch_len:
                    found = min(positions, key=lambda p: abs(p - a.start))
                else:
                    found = positions[0]
                break
            if found < 0:
                dropped += 1
                continue
            start = found
            end = start + len(n)
            d.anchor = Anchor(text=chapter_text[start:end], start=start, end=end)
            if not (d.text and d.text.strip()):
                stripped = chapter_text[start:end].strip().strip("「」『』“”\"'")
                d.text = stripped
            relocated += 1
            kept.append(d)
            search_from = max(search_from, end)
            continue
        # text 缺失但位置合法 → 切片回填（并同时修正对白 text 缺引号场景）
        backfilled = chapter_text[a.start:a.end]
        d.anchor = Anchor(text=backfilled, start=a.start, end=a.end)
        if not (d.text and d.text.strip()):
            # 对白 text 也缺失：从回填的 anchor 去引号兜底
            stripped = backfilled.strip().strip("「」『』“”\"'")
            d.text = stripped
        kept.append(d)
        search_from = max(search_from, d.anchor.end)
    if dropped or relocated:
        logger.warning(
            f"[dialogue] {where}anchor 越界/无效：{relocated} 条已按对白文本"
            f"在正文中重定位修正，{dropped} 条无法定位丢弃"
            f"（原 start/end 不在 [0, {ch_len}) 区间或 end<=start）"
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


async def _attribute_dialogues_batch_direct(
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
    wrapped = await asyncio.wait_for(
        llm.chat_structured(
            prompt=prompt,
            output_schema=DialogueBatchResponse,
            temperature=0.1,
            max_tokens=24000,
            max_retries=2,
            use_fast_model=True,
        ),
        timeout=max(30, int(settings.DIALOGUE_BATCH_TIMEOUT_SECONDS)),
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
            # 成章内存在多处明确对白线索却返回空列表，不能写成「已完成」。
            # 非虚构作品中的引文可能也带引号，所以要求多处引号且伴随说话动词。
            text = text_by_idx[idx]
            if not got_map[idx].dialogues and _has_likely_dialogue(text):
                raise ValueError(f"chapter_idx={idx} 含多处对白线索，但模型返回 0 条对白")
            final.append(got_map[idx])
        else:
            logger.warning(
                f"[dialogue_batch] LLM 输出缺失 chapter_idx={idx}，"
                f"该章不计入本批结果（保持未完成，重跑 prepare 自动补跑）"
            )
    return final


_SPEECH_NEAR_QUOTE_RE = re.compile(
    r"(?:说|问|道|答|喊|叫|开口|低声)[^\n]{0,30}[“「『]"
    r"|[”」』][^\n]{0,30}(?:说|问|道|答|喊|叫|开口|低声)"
)


def _has_likely_dialogue(text: str) -> bool:
    if len(text) < 300:
        return False
    pairs = sum(text.count(left) for left, _ in _QUOTE_PAIRS)
    return pairs >= 4 and bool(_SPEECH_NEAR_QUOTE_RE.search(text))


def _text_chunks_with_offsets(text: str, limit: int) -> list[tuple[int, str]]:
    """保留所有字符，优先在段落边界拆开；偏移供锚点回写原章。"""
    chunks: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + limit // 2, end)
            if boundary >= 0:
                end = boundary + 1
        chunks.append((start, text[start:end]))
        start = end
    return chunks


async def attribute_dialogues_batch_with_llm(
    chapters: list[tuple[int, str]], characters: list[Character],
) -> list[ChapterDialogueBatchResult]:
    """限制请求正文长度；超长单章分块后把锚点还原到原章坐标。"""
    limit = max(1000, int(settings.DIALOGUE_BATCH_MAX_CHARS))
    if sum(len(t) for _, t in chapters) <= limit and all(len(t) <= limit for _, t in chapters):
        return await _attribute_dialogues_batch_direct(chapters, characters)

    by_idx: dict[int, ChapterDialogueBatchResult] = {}
    group: list[tuple[int, str]] = []
    group_chars = 0

    async def flush_group() -> None:
        nonlocal group, group_chars
        if group:
            for item in await _attribute_dialogues_batch_direct(group, characters):
                by_idx[item.chapter_idx] = item
            group = []
            group_chars = 0

    for idx, text in chapters:
        if len(text) <= limit:
            if group and group_chars + len(text) > limit:
                await flush_group()
            group.append((idx, text))
            group_chars += len(text)
            continue
        await flush_group()
        attrs: list[DialogueAttribution] = []
        complete = True
        for offset, chunk in _text_chunks_with_offsets(text, limit):
            # 同一章的块各自调用，避免按 chapter_idx 去重时丢掉其余块。
            response = await _attribute_dialogues_batch_direct([(idx, chunk)], characters)
            if not response:
                complete = False
                break
            for attr in response[0].dialogues:
                assert attr.anchor is not None
                attr.anchor.start += offset
                attr.anchor.end += offset
                attrs.append(attr)
        if complete:
            by_idx[idx] = ChapterDialogueBatchResult(chapter_idx=idx, dialogues=attrs)
    await flush_group()
    return [by_idx[idx] for idx, _ in chapters if idx in by_idx]
