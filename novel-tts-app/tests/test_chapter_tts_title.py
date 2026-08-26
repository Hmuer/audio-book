"""端到端测试：章节标题不重复、首段垃圾内容不入库。

直接调用 backend/services/{book_split, chapter} 的真实函数/类（不依赖完整应用启动）。
"""
from __future__ import annotations

import sys
import os
import types

TEST_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.normpath(os.path.join(TEST_ROOT, "..", "backend"))
sys.path.insert(0, BACKEND)


# ---------------------------------------------------------------------------
# 最小 stub：book_split 依赖 settings + Chapter class，我们不启动完整 App，
# 自己构造一个 config stub 模块，再让 import 能找到它。
# ---------------------------------------------------------------------------

def _install_stubs():
    # 1. 造 config.settings（book_split 读 CHAPTER_SPLIT_PATTERNS 等）
    if "app.core.config" in sys.modules:
        return
    patterns = [
        r"^[ \t]*第[ \t]*([零〇一二三四五六七八九十百千0-9]+)[ \t]*(章|回|节|卷|篇|部)[ \t]*[:：、\.]*[ \t]*([^\n]*)$",
        r"^[ \t]*(Ep|Episode|Vol|Volume|Ch|Chapter)[ \t]*[\.\-:：]?[ \t]*([0-9IVXLCDM]+)[\.\t \-:：]*([^\n]*)$",
        r"^[ \t]*Chapter[ \t]+([0-9IVXLCDM]+)[\.\t \-:：]*([^\n]*)$",
        r"^[ \t]*(序章|楔子|引子|前言|序言|尾声|终章|后记|缘起|题辞|自叙|番外篇|番外|结尾语|写在最后|附录)[ \t]*[:：]?[ \t]*([^\n]*)$",
    ]

    class _Settings:
        CHAPTER_SPLIT_PATTERNS = list(patterns)
        CHAPTER_SPLIT_MIN_MATCHES = 2
        CHAPTER_SPLIT_HARD_FALLBACK_ENABLED = False
        CHAPTER_SPLIT_HARD_FALLBACK_MAX_CHARS = 30_000

    app_pkg = types.ModuleType("app")
    core_pkg = types.ModuleType("app.core")
    config_mod = types.ModuleType("app.core.config")
    config_mod.settings = _Settings()
    services_pkg = types.ModuleType("app.services")

    for name, mod in [
        ("app", app_pkg),
        ("app.core", core_pkg),
        ("app.core.config", config_mod),
        ("app.services", services_pkg),
    ]:
        sys.modules[name] = mod

    # 手动让 app.services 能被 book_split 识别为包
    app_pkg.__path__ = [os.path.join(BACKEND, "app")]
    core_pkg.__path__ = [os.path.join(BACKEND, "app", "core")]
    services_pkg.__path__ = [os.path.join(BACKEND, "app", "services")]


_install_stubs()

# 现在直接 import 真实模块
from app.services import book_split, chapter


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------
USER_SAMPLE = """\
==========================================================================================
本站发的所有小说都是校对全本-精校全本-精校的无错TXT小说下载：`http://www.freexiaoshuo.com/`
==========================================================================================
书名：百炼成仙
作者：幻雨

内容介绍：
　　仙路崎岖，百般磨练终成正果！
　　奇妙的仙侠世界，一个平凡的少年林轩，机缘巧合踏上了修真之路，然而没有灵根，让他付出十倍的努力，修真速度也远逊于同门……
　　讥讽、白眼，林轩不为所动，一心一意的修炼。
　　神秘的蓝色星海，变废为宝的奇妙异能，被视为垃圾的林轩，意外获得了这神奇的本领，将给修真界带来怎样的改变？
　　玄妙的仙术、神奇的法宝、强大的妖兽、仙魔妖鬼，各界高手一一粉墨登场，林轩的修真之路，将是超乎想像的波澜壮阔！


【第一卷 飘云谷】


第一章 林轩
　　"唉！"
　　失望的叹息传来，一个相貌平凡的少年，满脸木然的表情，又失败了。

第二章 惊变
　　第二天清晨，林轩早早地爬了起来。
"""


ORDERED_WITH_PREFACE = """\
序章 黎明之前
　　故事从一个夜晚开始。

第一章 启程
　　少年出发了。
"""


# ---------------------------------------------------------------------------
# 断言帮助
# ---------------------------------------------------------------------------
def _title_segment_text(ch_obj: chapter.Chapter) -> str:
    """用 _build_segments_for_chapter 跑出的第一段 title 文本，模拟 TTS 实际朗读内容。"""
    segs, _ = chapter._build_segments_for_chapter(
        ch=ch_obj,
        dialogues=[],
        narrator_voice_id="nv",
        voice_assignments={},
        segment_overrides=None,
        start_idx=0,
    )
    title_segs = [s for s in segs if s.kind == "title"]
    return title_segs[0].text if title_segs else ""


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
def test_junk_before_ch1_is_completely_ignored():
    """第一章前的广告/书名/作者/简介/【第一卷】全部丢弃，不生成任何"序"章。"""
    chs = book_split.split_chapters_regex(USER_SAMPLE, min_matches=1)
    titles = [c.title for c in chs]
    assert "序" not in titles, f"广告/简介不该合并为'序'章！titles={titles}"
    assert len(chs) == 2 and titles[0] == "第一章 林轩" and titles[1] == "第二章 惊变", \
        f"应该只识别出 第一/二章: {titles}"
    # 第一章正文不以标题行开头
    assert not chs[0].text.startswith("第一章 林轩"), "第一章 text 不该再包含标题行"
    # 第一章正文不以卷头残留开头
    first = chs[0].text[:30]
    assert "飘云谷" not in first and "第一卷" not in first, \
        f"【第一卷】不该残留到第一章正文！前30字: {first!r}"


def test_title_segment_no_double_numbering():
    """标题段绝不出现"第2章 第一章 林轩"双重章节号。"""
    chs = book_split.split_chapters_regex(USER_SAMPLE, min_matches=1)
    # 场景1：第一章 idx=0（正常情况）
    ch1 = chs[0]
    tts = _title_segment_text(ch1)
    assert tts == "第一章 林轩", f"标题段错误，实际: {tts!r}"
    # 场景2：第一章被偏移 idx=1（比如前面插入一章）
    ch1_shifted = chapter.Chapter(idx=1, title=ch1.title, text=ch1.text)
    tts2 = _title_segment_text(ch1_shifted)
    assert tts2 == "第一章 林轩", \
        f"即使 idx 偏移，标题段也必须直接用原文标题！实际: {tts2!r}"


def test_explicit_preface_chapter_still_works():
    """原文明确写了'序章 黎明之前'标题行 → 正确识别为独立章节。"""
    chs = book_split.split_chapters_regex(ORDERED_WITH_PREFACE, min_matches=1)
    titles = [c.title for c in chs]
    assert titles[0] == "序章 黎明之前", f"序章标题未命中 titles={titles}"
    # 序章标题段直接读'序章 黎明之前'，不拼接额外序号
    assert _title_segment_text(chs[0]) == "序章 黎明之前"
    # 第一章 idx=1，但标题段仍是"第一章 启程"（原文标题本身带序号）
    assert chs[1].idx == 1
    assert _title_segment_text(chs[1]) == "第一章 启程"


def test_hard_fallback_placeholder_still_prefixed():
    """硬切兜底标题『第 1 部分』不是正则命中的章节格式，
    必须由 _title_tts_text 加前缀，避免所有章都同一句"第 N 部分"。"""
    ch = chapter.Chapter(idx=3, title="第 1 部分", text="正文内容")
    tts = chapter._title_tts_text(ch)
    # 占位标题的前缀：因为不是正则命中的"第X章"格式（是"第 N 部分"硬切），
    # 会被识别为"占位标题"，补 第{idx+1}章 前缀。
    # 该断言精确说明输出：若将来策略变化需要同步调整测试。
    assert tts.startswith("第4章 "), f"占位标题应加 idx+1=4 前缀: {tts!r}"


def test_text_body_no_title_line_repeat():
    """旁白段不会重复朗读标题（验证 text 剥离标题行后的输出）。"""
    chs = book_split.split_chapters_regex(USER_SAMPLE, min_matches=1)
    # 跑一遍完整 segment 构建（无对白），所有 narrator 文本不应再包含章节标题
    segs, _ = chapter._build_segments_for_chapter(
        ch=chs[0], dialogues=[],
        narrator_voice_id="nv",
        voice_assignments={},
        segment_overrides=None,
        start_idx=0,
    )
    narrator_texts = "|".join(s.text for s in segs if s.kind == "narrator")
    assert "第一章 林轩" not in narrator_texts, \
        f"旁白不应再读标题行！narrator: {narrator_texts!r}"


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import traceback
    tests = [
        test_junk_before_ch1_is_completely_ignored,
        test_title_segment_no_double_numbering,
        test_explicit_preface_chapter_still_works,
        test_hard_fallback_placeholder_still_prefixed,
        test_text_body_no_title_line_repeat,
    ]
    failed = 0
    for t in tests:
        print(f"\n▶ {t.__name__}")
        try:
            t()
            print(f"  ✓ PASS")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ FAIL: {e}")
            traceback.print_exc()
        except Exception as e:
            failed += 1
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'All tests PASS' if failed == 0 else f'{failed} FAIL'}")
    sys.exit(0 if failed == 0 else 1)
