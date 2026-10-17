"""用本地样本发现的标题形态保护切章与对白请求边界。"""
import pytest

from backend.app.services.book_split import split_chapters_regex, split_book_chapters
from backend.app.services.dialogue import (
    _has_likely_dialogue,
    _text_chunks_with_offsets,
    _backfill_anchor_text,
    DialogueAttribution,
)


def test_web_novel_title_variants_and_duplicate_navigation():
    text = """第一章 土豆新书预览
预览。
正文 第一章 蟒雀吞龙
正文甲。
    第四章 源纹的力量
重复正文。""" + "故事继续。" * 100 + """
正文 第四章 源纹的力量
重复正文。""" + "故事继续。" * 100 + """
正文 第两百章 待遇之差
正文乙。
正文 正文_第201章 新篇
正文丙。
"""
    chapters = split_chapters_regex(text, min_matches=2)
    assert [c.title for c in chapters] == [
        "第一章 土豆新书预览", "第一章 蟒雀吞龙",
        "第四章 源纹的力量", "第两百章 待遇之差", "第201章 新篇",
    ]


@pytest.mark.asyncio
async def test_prose_poetry_and_markdown_without_chapter_number():
    markdown = "# 春天\n桃花开。\n# 夏天\n荷花开。"
    chapters = await split_book_chapters(markdown)
    assert [c.title for c in chapters] == ["春天", "夏天"]
    single = await split_book_chapters("# 一首小诗\n山间清泉，缓缓流过。")
    assert len(single) == 1 and single[0].title == "一首小诗"

    prose = "\n\n".join("窗前有风，树叶轻响。" * 250 for _ in range(15))
    parts = await split_book_chapters(prose)
    assert len(parts) >= 2
    assert all(len(p.text) <= 30_000 for p in parts)
    assert "".join(p.text for p in parts).replace("\n", "") == prose.replace("\n", "")


def test_dialogue_chunks_cover_text_and_missing_anchor_relocated():
    text = "前文。\n" * 100 + "李明说：“你好。”\n" + "后文。" * 100
    chunks = _text_chunks_with_offsets(text, 250)
    assert "".join(chunk for _, chunk in chunks) == text
    assert all(text[offset:offset + len(chunk)] == chunk for offset, chunk in chunks)
    attrs = [DialogueAttribution(text="你好。", speaker="李明")]
    fixed = _backfill_anchor_text(attrs, text)
    assert len(fixed) == 1
    assert text[fixed[0].anchor.start:fixed[0].anchor.end] == "“你好。”"
    assert _has_likely_dialogue("李明说：“你好。”" * 80)
    assert not _has_likely_dialogue("诗人写道：“春风吹来。”" * 3)


def test_numbered_headings_preserve_substantial_opening():
    opening = "少年在街上奔跑。" * 160
    text = opening + "\n" + "\n".join(
        f"{n} 小节标题\n" + ("小节正文。" * 100)
        for n in range(2, 8)
    )
    chapters = split_chapters_regex(text, min_matches=2)
    assert chapters[0].title == "开篇"
    assert opening in chapters[0].text
    assert [c.title for c in chapters[1:]] == [f"{n} 小节标题" for n in range(2, 8)]
