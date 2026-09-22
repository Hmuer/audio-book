"""LRC 歌词格式回归测试。

覆盖（均为纯函数，不依赖 DB / 网络）：
  T-LRC1  多段落 cue → 拆成多行 LRC，每行都有时间戳（不再出现"没有时间戳的行"）
  T-LRC2  拆行后按字符占比分摊 dur_ms（段内时间轴单调递增）
  T-LRC3  纯标点/引号 cue（如 "”\\n　　“"）→ 丢弃，不产出空行
  T-LRC4  旁白段首尾残留的孤立引号 → 去掉
  T-LRC5  旁白切片里混入的对白原文 → 与 dialogue 行去重
  T-LRC6  全角空格/换行折叠，不残留裸换行（保证一行 LRC == 一行文本）
"""
from __future__ import annotations

import re

from app.services.subtitles import (
    _dialogue_text_set,
    _iter_cue_lines,
    _normalize_lrc_line,
)


def _render(segs: list[dict], *, with_speaker: bool = True) -> list[tuple[int, str]]:
    """复刻 generate_chapter_lrc 的行生成逻辑（不含 DB 取数）。"""
    dialogue_texts = _dialogue_text_set(segs)
    out: list[tuple[int, str]] = []
    last = ""
    for e in segs:
        is_dialogue = (e.get("kind") or "") == "dialogue"
        for ms, text in _iter_cue_lines(e, with_speaker=with_speaker):
            if not is_dialogue and text in dialogue_texts:
                continue
            if text == last:
                continue
            out.append((ms, text))
            last = text
    return out


# ---------------------------------------------------------------------
# T-LRC1 / T-LRC6 多段落拆行 + 无裸换行
# ---------------------------------------------------------------------
def test_lrc1_multiline_cue_splits_into_timestamped_lines():
    segs = [{
        "kind": "narrator", "speaker": "",
        "text": "林若雪小声说。\n街角，王大爷手里拎着一串糖葫芦，正朝他们招手。",
        "start_ms": 10500, "dur_ms": 6200,
    }]
    lines = _render(segs)
    assert len(lines) == 2, "多段落必须拆成两行，否则第二行没有时间戳"
    for ms, text in lines:
        assert "\n" not in text and "\r" not in text
        assert re.match(r"^\[\d{2}:\d{2}\.\d{2}\]", f"[{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms // 10 % 100:02d}]{text}")


# ---------------------------------------------------------------------
# T-LRC2 段内时间按字符占比分摊
# ---------------------------------------------------------------------
def test_lrc2_intra_cue_timing_split_is_monotonic_and_proportional():
    segs = [{
        "kind": "narrator", "speaker": "",
        "text": "甲" * 10 + "\n" + "乙" * 30,
        "start_ms": 1000, "dur_ms": 4000,
    }]
    lines = _render(segs)
    assert [ms for ms, _ in lines] == [1000, 2000], "10:30 字符 → 1000ms 后进入第二行"


# ---------------------------------------------------------------------
# T-LRC3 纯引号/标点 cue 丢弃
# ---------------------------------------------------------------------
def test_lrc3_junk_quote_only_cues_are_dropped():
    assert _normalize_lrc_line("”\n　　“") == ""
    assert _normalize_lrc_line("“") == ""
    assert _normalize_lrc_line("　　。") == ""
    segs = [
        {"kind": "narrator", "speaker": "", "text": "”\n　　“", "start_ms": 0, "dur_ms": 800},
        {"kind": "narrator", "speaker": "", "text": "正朝他们招手。", "start_ms": 900, "dur_ms": 1000},
    ]
    assert _render(segs) == [(900, "正朝他们招手。")]


# ---------------------------------------------------------------------
# T-LRC4 孤立引号清理
# ---------------------------------------------------------------------
def test_lrc4_orphan_quotes_are_stripped():
    assert _normalize_lrc_line("　　“呵呵，这不是林师弟么？”") == "呵呵，这不是林师弟么？"
    assert _normalize_lrc_line("传入耳「") == "传入耳"
    # 正常句末句号不能被吃掉
    assert _normalize_lrc_line("林若雪低着头，缓慢走在街道一侧。") == "林若雪低着头，缓慢走在街道一侧。"


# ---------------------------------------------------------------------
# T-LRC5 旁白中重复的对白原文去重
# ---------------------------------------------------------------------
def test_lrc5_dialogue_text_duplicated_in_narrator_is_deduped():
    segs = [
        {"kind": "narrator", "speaker": "",
         "text": "传入耳朵。\n　　“呵呵，这不是林师弟么？”", "start_ms": 18000, "dur_ms": 9000},
        {"kind": "dialogue", "speaker": "林轩", "text": "呵呵，这不是林师弟么？",
         "start_ms": 21000, "dur_ms": 2700},
    ]
    texts = [t for _, t in _render(segs)]
    assert texts.count("呵呵，这不是林师弟么？") == 0, "旁白里的重复行应被剔除"
    assert "林轩：呵呵，这不是林师弟么？" in texts, "对白行保留并带说话人前缀"
    assert "传入耳朵。" in texts


# ---------------------------------------------------------------------
# 对白行带说话人前缀；旁白行不带
# ---------------------------------------------------------------------
def test_lrc_dialogue_gets_speaker_prefix():
    segs = [{"kind": "dialogue", "speaker": "李明", "text": "怎么了？",
             "start_ms": 6150, "dur_ms": 800}]
    assert _render(segs) == [(6150, "李明：怎么了？")]
    assert _render(segs, with_speaker=False) == [(6150, "怎么了？")]
