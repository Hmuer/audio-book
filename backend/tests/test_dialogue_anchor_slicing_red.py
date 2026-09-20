"""对白 anchor 切片 RED：重复对白导致「对白与附近旁白混乱」。

背景（2026-09-20 听感反馈：「生成的音频里对白及对白附近的旁白内容混乱」）：

`_build_segments_for_chapter` 用 `ch.text.find(anchor_text)` 修正 LLM 给的对白位置，
而 `str.find` 只返回**第一处**。同一句短对白在一章里重复出现是常态（「嗯。」「什么？」
「好。」），于是第 2..N 句对白全被搬到第一处：

- 两处对白之间的旁白切片错位，被挤到章尾；
- 收尾旁白把**已经读过的对白原文再朗读一遍**（旁白读对白）。

修复：查找必须从 `cursor` 单调往后；LLM 给的 start 与原文对得上时直接采信。

覆盖：
  SA-1 重复对白：旁白按序切成多段，且不得包含对白原文（核心回归）
  SA-2 LLM 偏移量全错（给 0）但 anchor_text 可定位 → 仍按原文顺序切
  SA-3 不变式：所有旁白段按序拼接 == 原文去掉各对白 anchor 区间
  SA-4 anchor_text 在旁白里也被"提及"时，采信 LLM 的准确 start（不能退化成第一处）
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.db.models import ProjectDialogue  # noqa: E402
from backend.app.services.chapter import (  # noqa: E402
    Chapter,
    _build_segments_for_chapter,
)


def _dlg(*, seg_idx: int, speaker: str, text: str, anchor_text: str,
         anchor_start: int = 0) -> ProjectDialogue:
    return ProjectDialogue(
        chapter_idx=0,
        segment_index=seg_idx,
        anchor_start=anchor_start,
        anchor_end=anchor_start + len(anchor_text),
        anchor_text=anchor_text,
        speaker=speaker,
        text=text,
        confidence=1.0,
        instruction="",
    )


def _segments(text: str, rows: list[ProjectDialogue]):
    ch = Chapter(idx=0, title="第一章 测试", text=text)
    segs, _ = _build_segments_for_chapter(
        ch, rows, narrator_voice_id="narr",
        voice_assignments={}, segment_overrides=None, start_idx=0,
    )
    return segs


def _narrator(segs) -> str:
    return "".join(s.text for s in segs if s.kind == "narrator")


def _dialogues(segs) -> list[str]:
    return [s.text for s in segs if s.kind == "dialogue"]


def _narrator_pieces(segs) -> list[str]:
    return [s.text for s in segs if s.kind == "narrator"]


def _squash(s: str) -> str:
    return "".join(s.split())


# ---------------------------------------------------------------------
# SA-1 / SA-2：重复对白
# ---------------------------------------------------------------------
_TEXT = "小明走进房间。「你好。」他说。「你好。」小红回答。他笑了笑，转身离开。"


def test_sa1_repeated_dialogue_keeps_narration_in_place():
    """两句文本相同的对白（LLM 位置正确）→ 旁白必须按序夹在两句之间，且不含对白原文。"""
    rows = [
        _dlg(seg_idx=0, speaker="小明", text="你好。", anchor_text="「你好。」", anchor_start=7),
        _dlg(seg_idx=1, speaker="小红", text="你好。", anchor_text="「你好。」", anchor_start=15),
    ]
    segs = _segments(_TEXT, rows)

    assert _dialogues(segs) == ["你好。", "你好。"]
    # 旧实现：第 2 句被搬到第 1 处 → 收尾旁白 = "他说。「你好。」小红回答。…"
    assert _squash(_narrator(segs)) == _squash(
        "小明走进房间。他说。小红回答。他笑了笑，转身离开。"
    ), f"旁白内容错位：{_narrator_pieces(segs)}"
    assert "「你好。」" not in _narrator(segs), "旁白不得把对白原文再读一遍"
    assert len(_narrator_pieces(segs)) >= 3, "对白前后的旁白应各自成段"


def test_sa2_zero_offsets_still_located_in_order():
    """LLM 把 start/end 全填 0（位置完全不可信），只要 anchor_text 在文中就要按序定位。"""
    rows = [
        _dlg(seg_idx=0, speaker="小明", text="你好。", anchor_text="「你好。」", anchor_start=0),
        _dlg(seg_idx=1, speaker="小红", text="你好。", anchor_text="「你好。」", anchor_start=0),
    ]
    segs = _segments(_TEXT, rows)

    assert _dialogues(segs) == ["你好。", "你好。"]
    assert _squash(_narrator(segs)) == _squash(
        "小明走进房间。他说。小红回答。他笑了笑，转身离开。"
    ), f"旁白内容错位：{_narrator_pieces(segs)}"
    assert "「你好。」" not in _narrator(segs)


# ---------------------------------------------------------------------
# SA-3：不变式 —— 旁白 = 原文去掉所有对白 anchor 区间
# ---------------------------------------------------------------------
def test_sa3_narration_equals_text_minus_dialogue_anchors():
    text = (
        "夜色渐深。"
        "「你来了。」老张说。"
        "风从门缝里钻进来。"
        "「嗯。」林若雪应了一声。"
        "「嗯。」老张也点了点头。"
        "两个人都没再说话。"
    )
    rows = [
        _dlg(seg_idx=0, speaker="老张", text="你来了。", anchor_text="「你来了。」"),
        _dlg(seg_idx=1, speaker="林若雪", text="嗯。", anchor_text="「嗯。」"),
        _dlg(seg_idx=2, speaker="老张", text="嗯。", anchor_text="「嗯。」"),
    ]
    segs = _segments(text, rows)

    assert _dialogues(segs) == ["你来了。", "嗯。", "嗯。"]
    assert _squash(_narrator(segs)) == _squash(
        "夜色渐深。老张说。风从门缝里钻进来。林若雪应了一声。老张也点了点头。两个人都没再说话。"
    ), f"旁白应等于原文去掉对白 anchor：{_narrator_pieces(segs)}"


# ---------------------------------------------------------------------
# SA-4：anchor_text 在旁白里也被"提及"时，采信 LLM 的准确 start
# ---------------------------------------------------------------------
def test_sa4_trusts_accurate_llm_start_when_phrase_also_quoted_in_narration():
    text = "他想起那句「好的。」，心里不是滋味。「好的。」他随口应道。"
    real_start = text.rindex("「好的。」")
    rows = [
        _dlg(seg_idx=0, speaker="他", text="好的。", anchor_text="「好的。」",
             anchor_start=real_start),
    ]
    segs = _segments(text, rows)

    assert _dialogues(segs) == ["好的。"]
    # 旧实现 find() 落在旁白里那处引用上 → 旁白被切成 "他想起那句" + "，心里不是滋味。"
    # 且"「好的。」他随口应道。"被整段当旁白读掉
    assert _squash(_narrator(segs)) == _squash(
        "他想起那句「好的。」，心里不是滋味。他随口应道。"
    ), f"旁白切片应围绕真正的对白：{_narrator_pieces(segs)}"
