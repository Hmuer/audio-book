"""H-20 章节识别分级专项测试：卷/篇/部级标题不再产生幽灵章节。

背景（剑来 5MB txt 实测）：
- 书的开头是『第一卷 笼中雀』独立卷行 + 『第一卷 笼中雀 第一章 惊蛰』卷章合体行
- 旧逻辑把卷行也切成章 → 第 1 章标题是『第一卷 笼中雀』、正文只有一行
  分隔线 '------------'，全书章节错位（383 个"章"里混着卷头）
- 章与章之间的 '------------' 分隔线留在正文里 → narrator 段会朗读
  连续符号、白占润色/对白归属 token

新语义：
- 合体行按章处理，标题归一为从章级标记起的原文子串（『第一章 惊蛰』）
- 纯卷行在全书存在章级/关键词级标题时剔除（分组头）；全书只有卷行的书
  （卷即章）保持原行为
- 纯分隔线行从正文剥离
"""
from __future__ import annotations

from backend.app.services.book_split import split_chapters_regex


# 复刻剑来开头的结构：书名 | 分隔线 | 独立卷行 | 分隔线 | 卷章合体行 | 正文……
_JIANLAI_HEAD = """剑来

------------

第一卷 笼中雀

------------

第一卷 笼中雀 第一章 惊蛰

    二月二，龙抬头。

    暮色里，小镇名叫泥瓶巷。

------------

第二章 泥瓶巷

    正文二。

------------

第二卷 洞天顶峰

------------

第二卷 洞天顶峰 第三章 风起

    正文三。
"""


def test_h20_jianlai_volume_headers_not_chapters():
    """剑来结构：卷行剔除、合体行归一为章、分隔线剥离、无幽灵章。"""
    chapters = split_chapters_regex(_JIANLAI_HEAD, min_matches=2)
    titles = [c.title for c in chapters]
    assert titles == ["第一章 惊蛰", "第二章 泥瓶巷", "第三章 风起"], (
        f"卷行必须剔除、合体行必须归一：{titles}"
    )
    # idx 连续无错位
    assert [c.idx for c in chapters] == [0, 1, 2]
    # 正文：无分隔线残留、各章内容正确归属
    for c in chapters:
        assert "----" not in c.text, f"第 {c.idx+1} 章正文残留分隔线"
    assert "二月二" in chapters[0].text
    assert "正文二" in chapters[1].text
    assert "正文三" in chapters[2].text
    # 卷标题行不得混进上一章正文（会在 narrator 段被朗读出来）
    assert "笼中雀" not in chapters[1].text
    assert "洞天顶峰" not in chapters[1].text
    # 标题行本身不在正文里（标题段单独朗读，正文再出现会双重朗读）
    assert "第一章 惊蛰" not in chapters[0].text


def test_h20_volume_only_book_keeps_volumes():
    """全书只有卷行（卷即章）：保持原行为，卷行仍是章节。"""
    text = """第一卷 风起

    卷一正文。

第二卷 云涌

    卷二正文。
"""
    chapters = split_chapters_regex(text, min_matches=2)
    titles = [c.title for c in chapters]
    assert titles == ["第一卷 风起", "第二卷 云涌"], titles
    assert "卷一正文" in chapters[0].text
    assert "卷二正文" in chapters[1].text


def test_h20_separator_lines_stripped_from_body():
    """章内分隔线（开头/中间/结尾）全部剥离，正文段落完整保留。"""
    text = """第一章 山雨

------------

    段落甲。

------------

    段落乙。

------------

第二章 欲来

    段落丙。
"""
    chapters = split_chapters_regex(text, min_matches=2)
    assert len(chapters) == 2
    assert "段落甲" in chapters[0].text and "段落乙" in chapters[0].text
    assert "----" not in chapters[0].text
    assert "段落丙" in chapters[1].text


def test_h20_plain_chapter_book_unchanged():
    """回归保护：无卷、无分隔线的普通书，切分行为与旧版完全一致。"""
    text = """第一章 起点

    林若雪说：你好。

    李明说：你好啊。

第二章 转折

    正文。
"""
    chapters = split_chapters_regex(text, min_matches=2)
    assert [c.title for c in chapters] == ["第一章 起点", "第二章 转折"]
    assert [c.idx for c in chapters] == [0, 1]
    assert "林若雪说：你好。" in chapters[0].text
    # 标题行不在正文
    assert "第一章 起点" not in chapters[0].text


def test_h20_combined_line_keyword_chapters_kept():
    """序章/番外等关键词标题行 + 卷行混排：关键词行是章级 → 卷行剔除。"""
    text = """第一卷 楔子卷

    无关内容。

序章 大梦

    大梦正文。

第一章 开端

    正文。
"""
    chapters = split_chapters_regex(text, min_matches=2)
    titles = [c.title for c in chapters]
    # 序章/章级行存在 → 卷行是分组头必须剔除
    assert titles == ["序章 大梦", "第一章 开端"], titles
    assert "大梦正文" in chapters[0].text
    # 卷行正文（『无关内容。』）并入上一章尾部；卷行在最前 → 属于被丢弃的前言区
    assert all("第一卷 楔子卷" not in c.text for c in chapters)
