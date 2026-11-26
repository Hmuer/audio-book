"""质量修复回归测试：对白不重复、不漏识别、情绪透传。

对应 docs/quality-optimization.md 的 M1（确定性对白分段）与 M3（逐段情绪）。
纯函数级测试，不依赖 DB / 网络。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.chapter import Chapter, _build_segments_for_chapter
from app.services.dialogue import (
    Anchor,
    DialogueAttribution,
    complete_dialogues_with_quotes,
)
from app.services.quote_segmenter import segment_quotes


def _ormlike(d, emotion: str = "") -> SimpleNamespace:
    """把 DialogueAttribution 转成 build 期 ProjectDialogue 的字段形态。"""
    a = d.anchor
    return SimpleNamespace(
        anchor_start=a.start, anchor_end=a.end, anchor_text=a.text,
        speaker=d.speaker, text=d.text, confidence=d.confidence,
        emotion=getattr(d, "emotion", emotion) or emotion, instruction="",
    )


def _segments(text: str, dialogues, *, narrator="narrator", voice_map=None):
    objs = [_ormlike(d) for d in dialogues]
    segs, _ = _build_segments_for_chapter(
        Chapter(idx=0, title="正文", text=text),
        objs,
        narrator_voice_id=narrator,
        voice_assignments=voice_map or {},
        segment_overrides=None,
        start_idx=0,
    )
    return segs


def _narrator_text(segs) -> str:
    return "\n".join(s.text for s in segs if s.kind == "narrator")


def test_all_quote_spans_covered_when_llm_missed_them():
    text = "林若雪低着头。「你来了？」李明说。「嗯。」她轻轻应了一声。"
    spans = segment_quotes(text)
    assert len(spans) == 2
    # LLM 只认出第一句，第二句漏了；锚点还是错的
    partial = [DialogueAttribution(
        anchor=Anchor(text="「你来了？」", start=3, end=3), speaker="李明", confidence=0.9,
        text="你来了？",
    )]
    completed = complete_dialogues_with_quotes(text, partial, {"李明", "林若雪"})
    assert len(completed) == 2  # 漏的「嗯。」被补回
    inners = {d.text for d in completed}
    assert "你来了？" in inners and "嗯。" in inners
    for d in completed:
        assert text[d.anchor.start:d.anchor.end] == d.anchor.text


def test_narrator_never_contains_dialogue_text():
    text = (
        "清晨，山门外的雾气还没散。周元负手而立。\n"
        "「你终于来了。」一道清冷的声音传来。\n"
        "「嗯。」周元点了点头。\n"
        "「那就开始吧。」那道声音又说。随后远处传来钟声。"
    )
    spans = segment_quotes(text)
    # 故意给一批错位锚点（模拟线上 LLM 偏移不准 / build 按错误 anchor 排序）
    bad = [
        DialogueAttribution(
            anchor=Anchor(text=text[s.start:s.end], start=s.start + 7, end=s.start + 7),
            speaker="周元", confidence=0.9, text=s.inner,
        )
        for s in spans
    ]
    completed = complete_dialogues_with_quotes(text, bad, {"周元"})
    segs = _segments(text, completed, voice_map={"周元": "male1", "": "male2"})
    narrator = _narrator_text(segs)
    for s in spans:
        assert s.inner not in narrator, f"旁白里出现了对白原文：{s.inner!r}"
    dialogue_text = "\n".join(s.text for s in segs if s.kind == "dialogue")
    for s in spans:
        assert dialogue_text.count(s.inner.strip()) >= 1


def test_duplicate_anchor_rows_do_not_double_read():
    text = "「好。」周元说。「好。」他又重复了一遍。"
    spans = segment_quotes(text)
    assert len(spans) == 2
    # 两条相同文本的对白，锚点都指向第一处（旧 bug 会重复朗读）
    dup = [
        DialogueAttribution(
            anchor=Anchor(text=text[s.start:s.end], start=spans[0].start, end=spans[0].end),
            speaker="周元", confidence=0.8, text=s.inner,
        )
        for s in spans
    ]
    completed = complete_dialogues_with_quotes(text, dup, {"周元"})
    segs = _segments(text, completed, voice_map={"周元": "male1"})
    dlg = [s for s in segs if s.kind == "dialogue" and s.text.strip() == "好。"]
    assert len(dlg) == 2


def test_dialogue_emotion_is_propagated_to_segment():
    text = "「你给我住手！」周元怒喝。"
    spans = segment_quotes(text)
    d = DialogueAttribution(
        anchor=Anchor(text=spans[0].outer, start=spans[0].start, end=spans[0].end),
        speaker="周元", confidence=0.95, text=spans[0].inner,
    )
    # 模拟 prepare 阶段写入的情绪
    objs = [_ormlike(d, emotion="angry")]
    segs, _ = _build_segments_for_chapter(
        Chapter(idx=0, title="正文", text=text), objs,
        narrator_voice_id="narrator", voice_assignments={"周元": "male1"},
        segment_overrides=None, start_idx=0,
    )
    dlg = [s for s in segs if s.kind == "dialogue"]
    assert dlg and all(s.emotion == "angry" for s in dlg)
    assert all(s.voice_id == "male1" for s in dlg)


def test_untitled_body_does_not_read_title():
    text = "「走吧。」他说。"
    spans = segment_quotes(text)
    d = DialogueAttribution(
        anchor=Anchor(text=spans[0].outer, start=spans[0].start, end=spans[0].end),
        speaker="他", confidence=0.9, text=spans[0].inner,
    )
    segs = _segments(text, [d])
    assert not any(s.kind == "title" for s in segs)
    assert any(s.kind == "silence" for s in segs)
