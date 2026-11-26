"""quote_segmenter 单元测试。"""
from __future__ import annotations

from backend.app.services.quote_segmenter import (
    QuoteSpan,
    normalize_quotes,
    segment_quotes,
)


def test_standard_multiple_in_one_paragraph() -> None:
    text = "他说：“你好。”她回答：“我很好。”"
    spans = segment_quotes(text)
    assert len(spans) == 2
    assert [s.inner for s in spans] == ["你好。", "我很好。"]
    assert spans[0].start < spans[1].start
    assert all(text[s.start : s.end] == s.outer for s in spans)


def test_mixed_quote_types() -> None:
    text = "「你好」他说『走吧』然后“再见”"
    spans = segment_quotes(text)
    assert [s.inner for s in spans] == ["你好", "走吧", "再见"]
    assert [s.outer for s in spans] == ["「你好」", "『走吧』", "“再见”"]


def test_unbalanced_closed_at_sentence_end() -> None:
    text = "他说：“我来了。"
    spans = segment_quotes(text)
    assert len(spans) == 1
    assert spans[0].inner == "我来了。"
    assert spans[0].outer == "“我来了。"


def test_unbalanced_does_not_swallow_next_paragraph() -> None:
    text = "他说：“我来了。\n她走了。"
    spans = segment_quotes(text)
    assert len(spans) == 1
    assert "她走了" not in spans[0].inner


def test_nested_yields_single_outer_span() -> None:
    text = "“他说『快走』然后离开。”"
    spans = segment_quotes(text)
    assert len(spans) == 1
    assert "『快走』" in spans[0].inner


def test_nested_single_quotes_inside_double() -> None:
    text = "“他说‘快走’然后离开。”"
    spans = segment_quotes(text)
    assert len(spans) == 1
    assert "‘快走’" in spans[0].inner


def test_straight_quotes_pairing() -> None:
    text = '他说"你好"然后走了'
    spans = segment_quotes(text)
    assert len(spans) == 1
    assert spans[0].inner == "你好"
    assert spans[0].outer == '"你好"'


def test_adjacent_quotes() -> None:
    text = "“甲”“乙”"
    spans = segment_quotes(text)
    assert len(spans) == 2
    assert [s.inner for s in spans] == ["甲", "乙"]


def test_empty_quote_dropped() -> None:
    assert segment_quotes("“”") == []
    assert segment_quotes("他说“”然后走了") == []


def test_whitespace_only_quote_dropped() -> None:
    assert segment_quotes("“   ”") == []


def test_short_real_line_kept() -> None:
    spans = segment_quotes("“嗯。”")
    assert len(spans) == 1
    assert spans[0].inner == "嗯。"


def test_realistic_multi_paragraph_chapter() -> None:
    text = (
        "第一章 风起\n\n"
        "“你终于来了。”老人缓缓开口。\n"
        "“路上有些耽搁。”年轻人答道，“不过还算顺利。”\n"
        "老人点点头：“那就好。”\n"
        "“『小心』这两个字，你要记住。”老人压低声音。\n"
        "“我记住了。”\n"
        "“去吧。”\n"
        "他转身离去，背影很快消失在夜色里。\n"
    )
    spans = segment_quotes(text)
    assert len(spans) >= 6
    # 有序且不重叠
    for earlier, later in zip(spans, spans[1:]):
        assert earlier.start < later.start
        assert earlier.end <= later.start
    # outer 与原文一致
    for span in spans:
        assert isinstance(span, QuoteSpan)
        assert text[span.start : span.end] == span.outer
        assert span.outer.startswith(("“", "「", "『", '"', "＂"))
        assert span.inner


def test_idempotent() -> None:
    text = "“你好。”他说，“再见。”"
    assert segment_quotes(text) == segment_quotes(text)


def test_normalize_quotes_maps_variants() -> None:
    assert normalize_quotes('"你好"') == "“你好”"
    assert normalize_quotes("＂你好＂") == "“你好”"
    assert normalize_quotes("〝你好〞") == "“你好”"
    # 已规范化的引号保持不变
    assert normalize_quotes("“你好”") == "“你好”"


def test_large_text_smoke() -> None:
    # 100k 级别文本不应报错，且数量合理
    line = "“这是第%d句对白。”他说。\n"
    text = "".join(line % i for i in range(4000))
    spans = segment_quotes(text)
    assert len(spans) == 4000
