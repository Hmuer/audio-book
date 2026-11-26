"""text_cleanup 单元测试。"""
from __future__ import annotations

from backend.app.services.text_cleanup import clean_chapter_text, is_non_story_page


def test_remove_chapter_end_line() -> None:
    text = "“你好。”\n本章完\n"
    cleaned, report = clean_chapter_text(text)
    assert "本章完" not in cleaned
    assert "你好" in cleaned
    assert report["removed_lines"] >= 1


def test_remove_trailing_scrape_noise() -> None:
    text = "他愣住了。（本章完）8)\n"
    cleaned, report = clean_chapter_text(text)
    assert "本章完" not in cleaned
    assert "8)" not in cleaned
    assert "他愣住了。" in cleaned


def test_remove_vote_line_in_parens() -> None:
    text = "（月中了，大家有月票吗，请投给元尊吧，谢谢。）\n"
    cleaned, report = clean_chapter_text(text)
    assert cleaned.strip() == ""
    assert report["removed_lines"] >= 1


def test_remove_wechat_search_line() -> None:
    text = "微信搜索公众号天蚕土豆即可\n"
    cleaned, report = clean_chapter_text(text)
    assert cleaned.strip() == ""
    assert report["removed_lines"] >= 1


def test_keep_narrative_line_mentioning_yuepiao() -> None:
    line = (
        "“你说的月票，我早就投给别人了，别再提这件事了，"
        "这本小说里的角色从来不在乎这些虚名，他们只想安静地种田过日子，"
        "谁也不愿再提起从前那些纷争和往事。”"
    )
    assert len(line.strip()) > 30
    cleaned, report = clean_chapter_text(line)
    assert "月票" in cleaned
    assert report["removed_lines"] == 0


def test_keep_long_narrative_over_200_chars() -> None:
    line = "月票" + "风吹过田野，麦浪一层层起伏。" * 30  # 远大于 200 字
    cleaned, report = clean_chapter_text(line)
    assert cleaned == line
    assert report["removed_lines"] == 0


def test_remove_inline_meta_paren_but_keep_narrative() -> None:
    text = "他走了。（求月票，谢谢）\n"
    cleaned, report = clean_chapter_text(text)
    assert "求月票" not in cleaned
    assert "他走了。" in cleaned


def test_report_fields() -> None:
    text = "正文第一段。\n求收藏\n正文第二段。\n"
    cleaned, report = clean_chapter_text(text)
    assert set(report.keys()) == {"removed_lines", "removed_chars", "samples"}
    assert report["removed_lines"] == 1
    assert report["removed_chars"] > 0
    assert isinstance(report["samples"], list) and report["samples"]
    assert "求收藏" not in cleaned


def test_preserve_blank_lines() -> None:
    text = "第一段。\n\n本章完\n\n第二段。"
    cleaned, _ = clean_chapter_text(text)
    assert "第一段。" in cleaned
    assert "第二段。" in cleaned
    assert "\n\n" in cleaned


def test_is_non_story_page_by_title() -> None:
    short_ad = "关注公众号，首发全网，搜索新书，求推荐票。"
    assert is_non_story_page("第一章 土豆新书预览", short_ad) is True


def test_is_non_story_page_normal_long_chapter_false() -> None:
    text = ("“这是对白。”他说道。\n" * 200)  # 远超 1200 字
    assert is_non_story_page("第一章 风起", text) is False


def test_is_non_story_page_short_ad_without_title_marker() -> None:
    text = "关注公众号，首发全网，搜索我们的新书，求推荐票。"
    assert is_non_story_page("第一章", text) is True


def test_is_non_story_page_short_narrative_with_dialogue_false() -> None:
    text = "“你好。”他说，“今天天气不错。”然后继续赶路。"
    assert is_non_story_page("第一章 风起", text) is False
