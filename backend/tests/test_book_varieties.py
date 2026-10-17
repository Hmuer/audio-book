"""用本地样本发现的标题形态保护切章与对白请求边界。"""
import pytest

from backend.app.services.book_split import (
    split_chapters_by_size, split_chapters_regex, split_book_chapters,
)
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


@pytest.mark.asyncio
async def test_wealth_book_keeps_preface_skips_toc_and_joins_serial_pages():
    from pathlib import Path

    sample = Path("/opt/audio-book/book/other/不可不知的致富心经：穷人穷在哪.txt")
    if not sample.is_file():
        pytest.skip("本地书籍样本未提供")
    chapters = await split_book_chapters(sample.read_bytes().decode("gb18030"))
    assert len(chapters) == 9
    assert chapters[0].title == "前言"
    assert "钱不是万能的" in chapters[0].text
    assert all("目录" not in ch.title for ch in chapters)
    assert not any(" / 3" in ch.text for ch in chapters)
    assert chapters[1].title.startswith("第一章")
    assert chapters[1].title.endswith("得过且过")
    assert chapters[2].title.endswith("不知道争气")
    assert chapters[-1].title.startswith("第八章")
    assert [ch.idx for ch in chapters] == list(range(len(chapters)))


@pytest.mark.asyncio
async def test_toc_without_outer_section_does_not_become_audio():
    text = (
        "前言\n开篇正文。\n\n目录\n第一章 春天\n"
        "春风 / 3\n夏雨 / 5\n秋月 / 8\n"
        "第一章 春天\n春天来了，风吹过窗前。\n"
        "第二章 夏天\n夏天来了，荷花开在池塘。"
    )
    chapters = await split_book_chapters(text)
    assert [ch.title for ch in chapters] == ["前言", "第一章 春天", "第二章 夏天"]
    assert all(" / 3" not in ch.text for ch in chapters)


@pytest.mark.asyncio
async def test_directory_word_in_real_prose_is_not_removed():
    text = "目录\n这里讨论图书目录的编写方法，并介绍各类索引。"
    chapters = await split_book_chapters(text)
    assert len(chapters) == 1
    assert "目录" in chapters[0].text


@pytest.mark.asyncio
async def test_untitled_prose_splits_on_complete_paragraphs():
    paragraph = "春风经过田野。" * 140
    text = "\n\n".join([paragraph] * 24)
    chapters = await split_book_chapters(text)
    assert len(chapters) >= 2
    assert all(len(ch.text) <= 30_000 for ch in chapters)
    assert all(ch.text.count(paragraph) >= 1 for ch in chapters)
    assert "".join(ch.text for ch in chapters).replace("\n", "") == text.replace("\n", "")


@pytest.mark.asyncio
async def test_biography_serial_pages_join_by_topic():
    from pathlib import Path

    sample = Path("/opt/audio-book/book/other/我的梦—奥巴马.txt")
    if not sample.is_file():
        pytest.skip("本地书籍样本未提供")
    chapters = await split_book_chapters(sample.read_bytes().decode("gb18030"))
    assert len(chapters) == 4
    assert chapters[1].title == "第一章美国梦代言人"
    assert chapters[2].title == "第三章肯尼迪式魅力"
    assert chapters[3].title == "第七章奥巴马的幽默"


def test_serialized_pages_only_join_when_numbering_and_topic_continue():
    text = (
        "第1节：山风(1)\n开头。\n"
        "第2节：山风(2)\n继续。\n"
        "第3节：海潮(1)\n另一个主题。\n"
        "第4节：山风(3)\n相同标题但不连续。"
    )
    chapters = split_chapters_regex(text, min_matches=2)
    assert [ch.title for ch in chapters] == ["山风", "第3节：海潮(1)", "第4节：山风(3)"]
    assert chapters[0].text == "开头。\n\n继续。"


def test_single_long_paragraph_prefers_sentence_boundary():
    sentence = "春风经过田野。"
    text = sentence * 4800
    chapters = split_chapters_by_size(text)
    assert len(chapters) > 1
    assert all(len(ch.text) <= 30_000 for ch in chapters)
    assert all(ch.text.endswith("。") for ch in chapters)
    assert "".join(ch.text for ch in chapters) == text
