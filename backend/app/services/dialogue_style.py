"""逐段情绪标签生成（MiniMax 等按情绪参数合成厂商用）。

背景 / 为什么需要这个模块
--------------------------------------------------------------------
豆包 2.0 支持 `context_texts`（自然语言「语音指令」），由 `voice_instruction.py`
逐段生成。但 **MiniMax T2A v2 只认 `emotion` 枚举**
（happy / sad / angry / fearful / disgusted / surprised / calm），不认自然语言指令。

此前当激活 TTS 是 MiniMax 时，识别流程**整段跳过**了逐句语气生成，合成固定传
`emotion="calm"` —— 结果整本书（含打斗/质问/悲恸）都是同一种平静腔调，听感很平。

本模块用 LLM 为**每一句对白**判定一个情绪枚举，落库到
`ProjectDialogue.emotion`，合成时逐段下发给 MiniMax。与豆包指令互补：
- 豆包：用 `instruction`（自然语言，表现力强）
- MiniMax：用 `emotion`（7 类枚举）

设计范式与 `voice_instruction.py` 保持一致：分批、批级重试、失败不致命
（整批按空串= calm）、配额耗尽穿透以便上层中止并保留 checkpoint。
"""
from __future__ import annotations

import asyncio
import json
import logging

from pydantic import BaseModel

from ..ai.factory import get_llm
from ..ai.base import LLMQuotaExhaustedError
from ..core.config import settings
from .usage import track_llm

logger = logging.getLogger(__name__)

# MiniMax 支持的 7 种情绪；不在集合内一律按 calm 处理
EMOTION_VALUES = frozenset({
    "happy", "sad", "angry", "fearful", "surprised", "disgusted", "calm",
})

# 批级重试次数（业务层）；provider 层另有兜底重试
_BATCH_RETRIES = 2


class DialogueEmotion(BaseModel):
    chapter_idx: int
    segment_index: int
    emotion: str


class DialogueEmotionBatchResponse(BaseModel):
    data: list[DialogueEmotion]


_EMOTION_PROLOGUE = r"""
你是一名有声小说配音导演。给定多章小说正文、每章的对白清单（chapter_idx / segment_index /
speaker / text）以及角色设定（性别/年龄/性格），请为**每一句对白**判定一个「情绪标签」，
用来驱动 TTS 的情绪参数。

情绪标签只能是以下 7 个英文单词之一（全部小写）：
- happy（喜悦/欢快/轻松/调侃）
- sad（悲伤/失落/心痛/哭腔）
- angry（愤怒/怒喝/厉声/杀意）
- fearful（恐惧/惊慌/颤抖/紧张）
- surprised（惊讶/错愕/震惊）
- disgusted（厌恶/鄙夷/嫌恶）
- calm（平静/中性/日常陈述/无法判断）

判定规则（非常重要）：
1. **优先采信对白紧邻的描写与提示语**：如「冷喝」「怒吼」「颤声道」「哭着说」「笑道」
   「淡淡道」「惊呼」等，是最可靠依据。
2. 台词本身有强烈情绪倾向（如悲恸、暴怒、狂喜、惊恐）也应据此判定。
3. **平淡的日常应答/信息陈述用 calm**：如「嗯」「好的」「我知道了」或纯交代信息，
   不要强行编造情绪。
4. 只能依据给定正文与角色设定，不编造情节。
5. 情绪判断只针对「这一句」，同一角色不同句子可以不同。

⚠️ 批量输出格式严格要求：
- 顶层对象 `{"data": [...]}`，每个元素 `{"chapter_idx": int, "segment_index": int, "emotion": "..."}`
- chapter_idx / segment_index 必须原样返回输入给的那两个数字，用于按段对齐
- 必须覆盖输入对白清单里的**全部**对白；即使 calm 也要输出一项
"""


def _field(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_dialogues(dialogues: list) -> list[dict]:
    out: list[dict] = []
    for i, d in enumerate(dialogues):
        try:
            seg_idx = int(_field(d, "segment_index", i))
        except (TypeError, ValueError):
            seg_idx = i
        out.append({
            "segment_index": seg_idx,
            "speaker": str(_field(d, "speaker", "") or ""),
            "text": str(_field(d, "text", "") or ""),
        })
    return out


def _char_dict(c) -> dict:
    if isinstance(c, dict):
        return c
    if hasattr(c, "model_dump"):
        try:
            return c.model_dump()
        except Exception:
            pass
    return {
        "name": str(_field(c, "name", "") or ""),
        "gender": str(_field(c, "gender", "") or ""),
        "age": str(_field(c, "age", "") or ""),
        "personality": str(_field(c, "personality", "") or ""),
    }


def _normalize_emotion(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in EMOTION_VALUES else "calm"


def _build_batch_prompt(
    batch: list,
    text_by_idx: dict[int, str],
    per_chapter: dict[int, list[dict]],
    characters: list,
) -> str:
    blocks: list[str] = []
    for ch in batch:
        dlg_lines = json.dumps(
            [{"chapter_idx": ch.idx, **d} for d in per_chapter[ch.idx]],
            ensure_ascii=False,
        )
        blocks.append(
            f"===== CHAPTER idx={ch.idx} START =====\n"
            f"【正文】\n{text_by_idx.get(ch.idx, '')}\n"
            f"【本章对白清单】\n{dlg_lines}\n"
            f"===== CHAPTER idx={ch.idx} END ====="
        )
    char_json = json.dumps(
        [_char_dict(c) for c in characters], ensure_ascii=False, indent=2
    )
    total = sum(len(per_chapter[ch.idx]) for ch in batch)
    return (
        _EMOTION_PROLOGUE
        + "\n\n【以下是本次要处理的所有章节】\n\n"
        + "\n\n".join(blocks)
        + "\n\n【角色设定（所有章节共用）】\n"
        + char_json
        + f"\n\n⚠️ 本次一共需要输出 {total} 条情绪项，chapter_idx / segment_index 必须与对白清单一一对应，必须齐全。"
        + ' 顶层格式必须是 {"data": [{"chapter_idx":..,"segment_index":..,"emotion":".."}, ...]}。'
    )


async def generate_dialogue_emotions(
    chapters: list,
    dialogues_by_chapter: dict[int, list],
    characters: list,
    *,
    batch_chapters: int | None = None,
) -> dict[tuple[int, int], str]:
    """为每一句对白生成情绪标签（MiniMax 用）。

    Returns: {(chapter_idx, segment_index): emotion}；**包含全部期望对白**（缺失补 calm）。
    """
    if not bool(getattr(settings, "DIALOGUE_EMOTION_ENABLED", True)):
        logger.info("[dialogue_emotion] DIALOGUE_EMOTION_ENABLED=False，跳过情绪生成")
        return {}

    text_by_idx: dict[int, str] = {}
    for ch in chapters:
        text_by_idx[_field(ch, "idx", -1)] = str(_field(ch, "text", "") or "")

    per_chapter: dict[int, list[dict]] = {}
    for ch in chapters:
        ch_idx = _field(ch, "idx", -1)
        dlgs = dialogues_by_chapter.get(ch_idx) or []
        if not dlgs:
            continue
        norm = _normalize_dialogues(dlgs)
        if norm:
            per_chapter[ch_idx] = norm

    result: dict[tuple[int, int], str] = {}
    for ch_idx, norm in per_chapter.items():
        for d in norm:
            result[(ch_idx, d["segment_index"])] = "calm"

    if not per_chapter:
        return result

    cfg = settings
    batch_n = max(1, int(batch_chapters or getattr(cfg, "VOICE_INSTRUCTION_BATCH_CHAPTERS", 6) or 6))
    concurrency = max(1, int(getattr(cfg, "VOICE_INSTRUCTION_BATCH_CONCURRENCY", 2) or 2))

    target_chapters = [ch for ch in chapters if _field(ch, "idx", -1) in per_chapter]
    batches: list[list] = [
        target_chapters[i: i + batch_n] for i in range(0, len(target_chapters), batch_n)
    ]

    sem = asyncio.Semaphore(concurrency)

    async def _process_one_batch(batch: list) -> dict[tuple[int, int], str]:
        last_err: str | None = None
        for attempt in range(_BATCH_RETRIES + 1):
            try:
                prompt = _build_batch_prompt(batch, text_by_idx, per_chapter, characters)
                async with sem:
                    llm = get_llm()
                    wrapped = await llm.chat_structured(
                        prompt=prompt,
                        output_schema=DialogueEmotionBatchResponse,
                        temperature=0.2,
                        max_tokens=8192,
                        use_fast_model=True,
                    )
                track_llm(calls=1, chars=len(prompt), detail="dialogue_emotion")
                out: dict[tuple[int, int], str] = {}
                for item in wrapped.data:
                    key = (int(item.chapter_idx), int(item.segment_index))
                    if key in result:
                        out[key] = _normalize_emotion(item.emotion)
                return out
            except LLMQuotaExhaustedError:
                raise
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"[dialogue_emotion] batch attempt={attempt + 1}/{_BATCH_RETRIES + 1} FAIL: {last_err}"
                )
        logger.error(f"[dialogue_emotion] 整批重试耗尽，按 calm 处理：{last_err}")
        return {}

    batch_results = await asyncio.gather(*[_process_one_batch(b) for b in batches])
    for br in batch_results:
        result.update(br)

    non_calm = sum(1 for v in result.values() if v and v != "calm")
    logger.info(
        f"[dialogue_emotion] done batches={len(batches)} dialogues={len(result)} non_calm={non_calm}"
    )
    return result
