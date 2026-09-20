"""逐段语音指令生成（豆包 2.0 的 `context_texts`）。

背景：
- 豆包语音合成 2.0 支持 `req_params.context_texts`（「语音指令」）—— 用自然语言
  描述「情绪 + 语气 + 节奏 + 音色质感」，指导配音演员怎么念这一句。
  官方明确该字段文字**不参与计费**，且仅 2.0 音色支持。
  ⚠️ 但**仅高表现力版 `seed-tts-2.0-expressive` 生效**：接口默认的
  `seed-tts-2.0-standard` 不支持语音指令，指令会被上游**静默忽略**
  （须由 `settings.DOUBAO_TTS_MODEL` 下发，见 config.py 注释）。
- 此前只有**角色级** emotion / instruction（ProjectCharacter），没有任何 LLM 参与，
  也没有逐段存储。本模块用 LLM 为每一句对白生成逐段指令，写入
  `ProjectDialogue.instruction`，合成时逐段下发。

设计取舍（与 dialogue.py / character.py 保持同一范式）：
- 批量：一次 LLM 请求处理多章（`VOICE_INSTRUCTION_BATCH_CHAPTERS`），显著降低 HTTP 开销；
- 防御式回填：LLM 漏返回 / 返回不存在的 (chapter_idx, segment_index) → 该段按空串，
  绝不抛错中断整条 prepare 流水线；
- 批级重试 + 失败不致命：整批失败时记 warning 并把该批全部按空串处理；
- 指令 clamp 到 512 字符，与角色级 instruction 截断口径一致。

本模块**只做语音指令**，不涉及「语音标签 CoT」（use_tag_parser / seed-tts-2.0-expressive），
那是后续批次；因此 prompt 里显式禁止输出 `[...]` 方括号标签语法。
"""
from __future__ import annotations

import asyncio
import json
import logging

from pydantic import BaseModel

from ..ai.factory import get_llm
from ..core.config import settings
from .usage import track_llm


logger = logging.getLogger(__name__)

# 指令长度上限：与 ProjectCharacter.instruction / Build.narrator_instruction 的 512 口径一致
INSTRUCTION_MAX_LEN = 512

# 批级重试次数（业务层）。provider 层另有兜底重试；这里只保证「整批失败不致命」。
_BATCH_RETRIES = 2


class DialogueInstruction(BaseModel):
    """单句对白的语音指令（携带定位键以便回填对齐）。"""
    chapter_idx: int
    segment_index: int
    instruction: str


class VoiceInstructionBatchResponse(BaseModel):
    data: list[DialogueInstruction]


_INSTRUCTION_PROLOGUE = r"""
你是一名有声小说配音导演。给定多章小说正文、每章的对白清单（chapter_idx / segment_index / speaker / text）
以及角色设定（性别 / 年龄 / 性格），请为**每一句对白**写一条「语音指令」，指导配音演员怎么念这句话。

语音指令写作规则（非常重要）：
1. 只能写**自然语言**，一句话说清「情绪 + 语气 + 节奏 + 音色质感」，口语化中文，约 15~40 字。
   例：「用颤抖沙哑、带着崩溃与绝望的哭腔，夹杂着质问与心碎的语气说」。
2. **绝对不要复述、引用或改写对白原文内容**——指令是给配音演员的语气指导，不是台词。
3. **优先采信对白紧邻的描写与提示语**：正文里对这句对白「怎么说的」描写是最可靠的依据，
   例如「一虚弱的声音」「语气中带着关切」「XX 冷喝」「XX 喃喃道」「声音发颤」。
   这类描写往往比台词本身更直接地指明说话方式，必须据此写指令
   （如「用虚弱沙哑、气息不足的语气，带着勉强支撑的疲惫感说」）。
   **台词本身看不出情绪、但前后描写给了情绪时，不要留空。**
4. **平淡的叙述型对白可以留空字符串**：如果一句对白及其前后描写都没有明显情绪线索
   （如日常应答「嗯」「好的」「我知道了」，或仅用于交代信息的普通陈述），
   instruction 请写成 ""，不要强行编造情绪——每句都硬加指令反而会让整体失真、听感浮夸。
5. 只能依据给定的正文与角色设定判断，**不要编造原文没有的情节、身份或场景**。
6. **禁止使用语音标签语法**：不得输出 `[...]` 这类方括号标记（如 `[whisper]`、`[笑]`、`[sad]`）。
   本轮只输出自然语言指令。

⚠️ 批量输出格式严格要求：
- 顶层是一个对象 `{"data": [...]}`
- data 里每一项是 `{"chapter_idx": int, "segment_index": int, "instruction": "..."}`
- `chapter_idx` / `segment_index` 必须原样返回输入给你的那两个数字，用于按段回填对齐。
- 必须覆盖输入对白清单里的**全部**对白；留空的对白也要输出一项（instruction 写 ""）。

【示例】
输入对白清单：
[
  {"chapter_idx": 0, "segment_index": 0, "speaker": "林若雪", "text": "全都没了……我什么都没有了……"},
  {"chapter_idx": 0, "segment_index": 1, "speaker": "李明", "text": "嗯，我知道了。"}
]
角色设定：[{"name": "林若雪", "gender": "女", "age": "少女", "personality": "内向、敏感"}]

输出：
{
  "data": [
    {"chapter_idx": 0, "segment_index": 0,
     "instruction": "用颤抖沙哑、带着崩溃与绝望的哭腔，夹杂着质问与心碎的语气说"},
    {"chapter_idx": 0, "segment_index": 1, "instruction": ""}
  ]
}
"""


def _field(obj, key: str, default=None):
    """兼容 dict / ORM 对象 / Pydantic 对象三种形态取字段。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _char_dict(c) -> dict:
    """把 Character（或类似对象）转成 prompt 用的 dict（性别/年龄/性格）。"""
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


def _normalize_dialogues(dialogues: list) -> list[dict]:
    """把一章的对白统一成 [{segment_index, speaker, text}]。

    segment_index 缺失时用列表下标兜底——与 prepare 落库时 `enumerate(attrs)` 的下标
    完全一致；因此对白归属阶段原始 attrs（无 segment_index 字段）也能正确回填。
    """
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


def _clamp(s: str) -> str:
    """指令裁剪：去空白 + 截断到 512 字符（与角色级 instruction 口径一致）。"""
    return (s or "").strip()[:INSTRUCTION_MAX_LEN]


def _build_batch_prompt(
    batch: list,
    text_by_idx: dict[int, str],
    per_chapter: dict[int, list[dict]],
    characters: list,
) -> str:
    """拼装单批 prompt：本批各章正文 + 对白清单 + 角色设定。"""
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
        _INSTRUCTION_PROLOGUE
        + "\n\n【以下是本次要处理的所有章节】\n\n"
        + "\n\n".join(blocks)
        + "\n\n【角色设定（所有章节共用）】\n"
        + char_json
        + f"\n\n⚠️ 本次一共需要输出 {total} 条指令项，"
        + "chapter_idx / segment_index 必须与上面的对白清单一一对应，顺序任意但必须齐全。"
        + ' 顶层格式必须是 {"data": [{"chapter_idx":..,"segment_index":..,"instruction":".."}, ...]}，'
        + "顶层一定要有 data 字段！"
    )


async def generate_dialogue_instructions(
    chapters: list,
    dialogues_by_chapter: dict[int, list],
    characters: list,
    *,
    batch_chapters: int | None = None,
) -> dict[tuple[int, int], str]:
    """为每一句对白生成豆包 2.0 语音指令。

    Args:
        chapters: 章节列表（需有 .idx / .text）。
        dialogues_by_chapter: {chapter_idx: [对白对象或 attrs dict, ...]}，
            对白项的 speaker/text/segment_index 通过 getattr/dict 兼容读取。
        characters: 角色设定（Character，含性别/年龄/性格）。
        batch_chapters: 覆盖「一次 LLM 调用处理几章」；缺省读 settings。

    Returns:
        {(chapter_idx, segment_index): instruction}。
        **包含全部期望的对白**——LLM 漏返回或返回非法键时对应段为空串，
        调用方直接 `.get(key, "")` 落库即可。
    """
    # 总开关关闭：不调 LLM、不生成任何指令（调用方按空串落库）
    if not bool(getattr(settings, "VOICE_INSTRUCTION_ENABLED", True)):
        logger.info(
            "[voice_instruction] VOICE_INSTRUCTION_ENABLED=False，跳过逐段语音指令生成"
        )
        return {}

    text_by_idx: dict[int, str] = {}
    for ch in chapters:
        text_by_idx[_field(ch, "idx", -1)] = str(_field(ch, "text", "") or "")

    # 只处理「有对白」的章节：无对白的章节不产生任何指令，也无需浪费 LLM 调用
    per_chapter: dict[int, list[dict]] = {}
    for ch in chapters:
        ch_idx = _field(ch, "idx", -1)
        dlgs = dialogues_by_chapter.get(ch_idx) or []
        if not dlgs:
            continue
        norm = _normalize_dialogues(dlgs)
        if norm:
            per_chapter[ch_idx] = norm

    # 先全部按空串填充：LLM 漏返回 / 中途失败时这些段自动保持空串，绝不中断 prepare
    result: dict[tuple[int, int], str] = {}
    for ch_idx, norm in per_chapter.items():
        for d in norm:
            result[(ch_idx, d["segment_index"])] = ""

    if not per_chapter:
        return result

    cfg = settings
    batch_n = max(
        1,
        int(
            batch_chapters
            or getattr(cfg, "VOICE_INSTRUCTION_BATCH_CHAPTERS", 6)
            or 6
        ),
    )
    concurrency = max(
        1, int(getattr(cfg, "VOICE_INSTRUCTION_BATCH_CONCURRENCY", 2) or 2)
    )

    # 按输入 chapters 的原顺序切批，保证同一章的对白落在同一批里
    target_chapters = [ch for ch in chapters if _field(ch, "idx", -1) in per_chapter]
    batches: list[list] = [
        target_chapters[i : i + batch_n]
        for i in range(0, len(target_chapters), batch_n)
    ]

    sem = asyncio.Semaphore(concurrency)

    async def _process_one_batch(batch: list) -> dict[tuple[int, int], str]:
        """处理单批（带批级重试）；失败返回 {}（该批全部保持空串）。"""
        last_err: str | None = None
        for attempt in range(_BATCH_RETRIES + 1):
            try:
                prompt = _build_batch_prompt(
                    batch, text_by_idx, per_chapter, characters
                )
                async with sem:
                    llm = get_llm()
                    wrapped = await llm.chat_structured(
                        prompt=prompt,
                        output_schema=VoiceInstructionBatchResponse,
                        # 配音指导需要一点创造性，但不宜过高以免偏离正文；0.3 与润色/推荐同档
                        temperature=0.3,
                        # 一批最多 ~6 章 × 数十句对白 × ~100 token ≈ 16k，给足余量
                        max_tokens=16384,
                        use_fast_model=True,
                    )
                track_llm(calls=1, chars=len(prompt), detail="voice_instruction")

                out: dict[tuple[int, int], str] = {}
                for item in wrapped.data:
                    key = (int(item.chapter_idx), int(item.segment_index))
                    if key in result:
                        out[key] = _clamp(item.instruction)
                    else:
                        # 幻觉出的 (chapter_idx, segment_index)：丢弃，不污染回填
                        logger.warning(
                            f"[voice_instruction] LLM 返回不存在的键 key={key}，已忽略"
                        )
                return out
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"[voice_instruction] batch attempt={attempt + 1}/"
                    f"{_BATCH_RETRIES + 1} FAIL: {last_err}"
                )
        logger.error(
            f"[voice_instruction] 整批重试耗尽仍失败，该批对白按空串处理：{last_err}"
        )
        return {}

    batch_results = await asyncio.gather(
        *[_process_one_batch(b) for b in batches]
    )
    for br in batch_results:
        # 只覆盖成功返回的键；失败批的键保留初始空串
        result.update(br)

    non_empty = sum(1 for v in result.values() if v)
    logger.info(
        f"[voice_instruction] done batches={len(batches)} "
        f"dialogues={len(result)} non_empty={non_empty}"
    )
    return result
