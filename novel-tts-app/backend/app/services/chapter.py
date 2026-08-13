"""
章节 segment 构建：把一章的文本 + 对白 anchor 切成 TTS 段（title / narrator / dialogue / silence）。

【注意】单章链路（prepare_chapter / synthesize_chapter）已移除。
- 旧单章模式 / 旧整本模式（services/book.py、/api/chapter/*、/api/book/*）已废弃删除。
- 现在唯一入口：项目制（Project → Build → BuildArtifact）。
- Project 制的章节识别、角色识别、对白归属走 services/project.py，分章走 services/book_split.py（正则 + 硬切，不调 LLM）。
"""
from __future__ import annotations

from pydantic import BaseModel

SILENCE_AFTER_TITLE_MS = 1500
SILENCE_BETWEEN_SEGMENTS_MS = 250


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
    # 仅在标题非占位（"正文"是旧单章模式 synthesize 产生的默认值）时才朗读，
    # 否则用户输入任何内容都会被先读一句"第1章 正文"，体验异常。
    if ch.title and ch.title.strip() != "正文":
        segs.append(_Segment(
            kind="title", chapter_idx=ch.idx, idx=idx,
            voice_id=narrator_voice_id, text=f"第{ch.idx+1}章 {ch.title}",
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
