"""
章节 segment 构建：把一章的文本 + 对白 anchor 切成 TTS 段（title / narrator / dialogue / silence）。

【注意】单章链路（prepare_chapter / synthesize_chapter）已移除。
- 旧单章模式 / 旧整本模式（services/book.py、/api/chapter/*、/api/book/*）已废弃删除。
- 现在唯一入口：项目制（Project → Build → BuildArtifact）。
- Project 制的章节识别、角色识别、对白归属走 services/project.py，分章走 services/book_split.py（正则 + 硬切，不调 LLM）。
"""
from __future__ import annotations

import re
from pydantic import BaseModel

SILENCE_AFTER_TITLE_MS = 1500
SILENCE_BETWEEN_SEGMENTS_MS = 250

# 判定 ch.title 本身是否已经带了"第X章/序章/楔子"等章节标识前缀。
# 如果已经带了，标题段就直接读 ch.title，不再重复拼 f"第{idx+1}章 {title}"，
# 否则会出现"第2章 第一章 林轩"这种双重章节号朗读。
# 注意：字符组里不含"部"，因为硬切兜底标题『第 N 部分』中的"部"不是真正的章节前缀；
# 如需要『第一部 XXX』这种册级命中，用户可在 config.py CHAPTER_SPLIT_PATTERNS 中自定义。
_TITLE_HAS_CH_PREFIX_NUM = re.compile(
    r"^\s*第[ \t]*[零〇一二三四五六七八九十百千0-9]+[ \t]*(章|回|节|卷|篇)(?!分)"
)
_TITLE_HAS_CH_PREFIX_SPECIAL = re.compile(
    r"^\s*(序章|楔子|引子|前言|序言|尾声|终章|后记|缘起|题辞|自叙|番外篇?|番外|结尾语|写在最后|附录)"
)
# 英文标题前缀兜底（Chapter / Vol / Ep. 等）
_TITLE_HAS_CH_PREFIX_EN = re.compile(
    r"^\s*(Chapter|Episode|Ep|Volume|Vol|Ch)[\s\.\-:：]",
    re.IGNORECASE,
)


def _title_tts_text(ch: "Chapter") -> str:
    """根据标题内容生成 TTS 朗读文本，避免"第X章 第一章 XXX"双重章节号。

    规则：
    - 标题本身已是『第X章 XXX』/『序章 XXX』/『Chapter X』→ 直接读原文
    - 否则（兜底占位标题如『序』『正文』『第 N 部分』等）才拼上章节序号
    """
    title = (ch.title or "").strip()
    if not title:
        return ""
    if (
        _TITLE_HAS_CH_PREFIX_NUM.match(title)
        or _TITLE_HAS_CH_PREFIX_SPECIAL.match(title)
        or _TITLE_HAS_CH_PREFIX_EN.match(title)
    ):
        # 原文已带章节标识，直接用
        return title
    # 占位标题才加前缀
    return f"第{ch.idx + 1}章 {title}"


class Chapter(BaseModel):
    idx: int
    title: str
    text: str


class _Segment(BaseModel):
    """合成内部 segment：title/narrator/dialogue/silence"""
    kind: str
    chapter_idx: int
    idx: int  # 全局 segment_index
    speaker: str | None = None
    voice_id: str | None = None
    text: str = ""
    confidence: float | None = None
    silence_ms: int = 0  # kind=silence 时用


def _build_segments_for_chapter(
    ch: Chapter,
    dialogues: list,  # 本章的对白（已按 anchor_start 排序；DbDialogue 或 ProjectDialogue 均可，字段兼容）
    narrator_voice_id: str,
    voice_assignments: dict[str, str],
    segment_overrides: dict[int, str] | None,
    start_idx: int,
) -> tuple[list[_Segment], int]:
    """
    把一章切成：title(+1.5s静音) + 对白/旁白交替段 + 段间短静音
    返回 (segments, next_start_idx)
    """
    segs: list[_Segment] = []
    idx = start_idx

    # 1. 标题段
    # - "正文"：旧单章 synthesize 默认值，不读
    # - 其他情况用 _title_tts_text() 智能拼，避免"第2章 第一章 林轩"双重章节号
    title_tts = _title_tts_text(ch)
    if title_tts:
        segs.append(_Segment(
            kind="title", chapter_idx=ch.idx, idx=idx,
            voice_id=narrator_voice_id, text=title_tts,
        ))
        idx += 1
        segs.append(_Segment(
            kind="silence", chapter_idx=ch.idx, idx=idx, silence_ms=SILENCE_AFTER_TITLE_MS,
        ))
        idx += 1

    # 2. 扫描 text，按对白 anchor 间隙切片
    cursor = 0
    chapter_len = len(ch.text)
    for dlg in dialogues:
        # anchor 是全局位置或本章内位置，调用方保证传入时已转换为"本章内位置"
        local_start = dlg.anchor_start
        local_end = dlg.anchor_end
        # 校验并修正 anchor 位置：LLM 返回的位置可能不准（尤其中文/字节位置错位），
        # 优先用 anchor_text 在 ch.text 中精确定位，避免 narrator 段误切片包含对白内容
        if getattr(dlg, "anchor_text", None):
            found = ch.text.find(dlg.anchor_text)
            if found >= 0:
                local_start = found
                local_end = found + len(dlg.anchor_text)
        # narrator 段：[cursor, local_start)
        if cursor < local_start:
            narrator_text = ch.text[cursor:local_start].strip()
            if narrator_text:
                segs.append(_Segment(
                    kind="narrator", chapter_idx=ch.idx, idx=idx,
                    voice_id=narrator_voice_id, text=narrator_text,
                ))
                idx += 1
                segs.append(_Segment(
                    kind="silence", chapter_idx=ch.idx, idx=idx,
                    silence_ms=SILENCE_BETWEEN_SEGMENTS_MS,
                ))
                idx += 1

        # dialogue 段：对白文本（去掉引号的 text）
        seg_voice_id = voice_assignments.get(getattr(dlg, "speaker", ""), narrator_voice_id)
        dlg_seg = _Segment(
            kind="dialogue", chapter_idx=ch.idx, idx=idx,
            speaker=getattr(dlg, "speaker", None),
            voice_id=seg_voice_id,
            text=getattr(dlg, "text", ""),
            confidence=getattr(dlg, "confidence", None),
        )
        segs.append(dlg_seg)
        idx += 1

        segs.append(_Segment(
            kind="silence", chapter_idx=ch.idx, idx=idx,
            silence_ms=SILENCE_BETWEEN_SEGMENTS_MS,
        ))
        idx += 1

        # 推进 cursor 到对白结束位置（防止 anchor 错位时 cursor 倒退导致重复切片）
        if local_end > cursor:
            cursor = local_end

    # 收尾 narrator
    if cursor < chapter_len:
        tail = ch.text[cursor:].strip()
        if tail:
            segs.append(_Segment(
                kind="narrator", chapter_idx=ch.idx, idx=idx,
                voice_id=narrator_voice_id, text=tail,
            ))
            idx += 1

    return segs, idx
