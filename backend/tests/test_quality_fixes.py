"""质量修复回归测试：对白不重复、不漏识别、情绪透传。

对应 docs/quality-optimization.md 的 M1（确定性对白分段）与 M3（逐段情绪）。
纯函数级测试，不依赖 DB / 网络。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services.chapter import Chapter, _build_segments_for_chapter
from backend.app.services.dialogue import (
    Anchor,
    DialogueAttribution,
    complete_dialogues_with_quotes,
)
from backend.app.services.quote_segmenter import segment_quotes


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


def test_guess_gender_from_name():
    from backend.app.services.build import _guess_gender_from_name

    assert _guess_gender_from_name("柳姑娘") == "女"
    assert _guess_gender_from_name("三公子") == "男"
    assert _guess_gender_from_name("王大少爷") == "男"
    assert _guess_gender_from_name("李师姐") == "女"
    assert _guess_gender_from_name("路人") == ""
    assert _guess_gender_from_name("") == ""


class _FakeSession:
    """最小 fake：第一次 execute 返回角色(gender)，第二次返回对白 speaker 列表。"""

    def __init__(self, chars, speakers):
        self._chars = chars
        self._speakers = speakers
        self.calls = 0

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def scalars(self):
            return self

    async def execute(self, _q):
        self.calls += 1
        if self.calls == 1:
            return self._Res(list(self._chars))
        return self._Res(list(self._speakers))


@pytest.mark.asyncio
async def test_unknown_gender_uses_female_default_for_female_lead():
    from backend.app.services.build import _with_unknown_speaker_voices

    allowed = {"minimax:m1", "minimax:f1"}
    # 未知性别 + 女频 → 女声默认
    res = await _with_unknown_speaker_voices(
        _FakeSession([("某甲", "未知")], ["某甲"]), "p", {},
        "minimax:n", "minimax:m1", "minimax:f1", allowed, book_category="女频",
    )
    assert res["某甲"] == "minimax:f1"
    # 未知性别 + 非女频 → 男声默认
    res2 = await _with_unknown_speaker_voices(
        _FakeSession([("某甲", "未知")], ["某甲"]), "p", {},
        "minimax:n", "minimax:m1", "minimax:f1", allowed, book_category="男频",
    )
    assert res2["某甲"] == "minimax:m1"
    # 名字启发式优先：姑娘 → 女声（即使男频）
    res3 = await _with_unknown_speaker_voices(
        _FakeSession([("柳姑娘", "未知")], ["柳姑娘"]), "p", {},
        "minimax:n", "minimax:m1", "minimax:f1", allowed, book_category="男频",
    )
    assert res3["柳姑娘"] == "minimax:f1"
    # 明确性别不受分类影响
    res4 = await _with_unknown_speaker_voices(
        _FakeSession([("铁男", "男")], ["铁男"]), "p", {},
        "minimax:n", "minimax:m1", "minimax:f1", allowed, book_category="女频",
    )
    assert res4["铁男"] == "minimax:m1"


@pytest.mark.asyncio
async def test_loudness_normalize_disabled_is_noop(monkeypatch):
    from backend.app.core.config import settings
    from backend.app.services.build import _normalize_chapter_loudness

    monkeypatch.setattr(settings, "AUDIO_LOUDNESS_NORMALIZE_ENABLED", False, raising=False)
    payload = b"\xff\xfb" + b"\x00" * 200
    assert await _normalize_chapter_loudness(payload) == payload


@pytest.mark.asyncio
async def test_loudness_normalize_enabled_outputs_valid_frames(monkeypatch):
    import shutil

    from backend.app.core.config import settings
    from backend.app.core.mp3_util import make_silent_mp3
    from backend.app.services.build import _normalize_chapter_loudness

    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    monkeypatch.setattr(settings, "AUDIO_LOUDNESS_NORMALIZE_ENABLED", True, raising=False)
    src = make_silent_mp3(400)
    out = await _normalize_chapter_loudness(src)
    assert isinstance(out, bytes) and len(out) > 0
    # 不应带 ID3 头（否则播放器/打包断言会看到非帧首字节）
    assert out[:1] == b"\xff"
