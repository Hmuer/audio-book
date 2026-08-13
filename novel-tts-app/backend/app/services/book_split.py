"""
章节识别：从整本小说文本中切分出章节。

策略（按优先级）：
1. 正则匹配常见章节格式（"第X章"、"Chapter X"、"卷X"等）—— 快、零成本、对结构化小说准确
2. 正则识别失败（命中数过少）→ 退化为按字数硬切 + LLM 生成标题
3. 兜底：当文本很短或正则完全无命中时，整本当作一章
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..ai.factory import get_llm
from .chapter import Chapter

logger = logging.getLogger(__name__)


# 常见章节标题正则（行首锚定，避免误命中正文）
# 命中的是「标题行」，章节内容是该行之后到下一个标题行之前
# 注意：用 [ \t]* 代替 \s*，避免贪婪吃掉换行符把下一行内容并进标题
_CHAPTER_TITLE_PATTERNS = [
    # 「第X章 标题」「第X回 标题」「第X节 标题」「第X卷 标题」
    # X 支持：汉字数字（一二三...百千万）、阿拉伯数字、纯数字
    re.compile(
        r"^[ \t]*第[ \t]*([零一二三四五六七八九十百千0-9]+)[ \t]*"
        r"(章|回|节|卷|篇|部)[ \t]*[:：、\.]*[ \t]*([^\n]*)$",
        re.MULTILINE,
    ),
    # 「Chapter 1 Title」「CHAPTER I. Title」
    re.compile(
        r"^[ \t]*Chapter[ \t]+([0-9IVXLCDM]+)[\.\t \-:：]*([^\n]*)$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # 「序章」「楔子」「引子」「尾声」「终章」「后记」「番外」等单关键词
    re.compile(
        r"^[ \t]*(序章|楔子|引子|前言|序言|尾声|终章|后记|番外篇?|番外)[ \t]*[:：]?[ \t]*([^\n]*)$",
        re.MULTILINE,
    ),
]

# 中文数字 → 阿拉伯，用于章节序号归一化（仅前 99）
_CN_NUM_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_to_int(s: str) -> Optional[int]:
    """中文数字（限 0-99）→ int，解析失败返回 None"""
    if s.isdigit():
        return int(s)
    if not s:
        return None
    # 处理 "十" "二十" "二十三" "三十一" 等
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM_MAP.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM_MAP.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    # 纯汉字数字逐字符累加（限两位）
    val = 0
    for ch in s:
        if ch not in _CN_NUM_MAP:
            return None
        val = val * 10 + _CN_NUM_MAP[ch]
    return val


def split_chapters_regex(text: str) -> list[Chapter]:
    """
    用正则识别章节。命中数 >= 2 才认为识别成功（避免误识别）。

    Returns:
        chapters: list[Chapter]，每章 text 包含从该标题到下一个标题之间的全部内容
                  （标题行本身保留在 text 头部，方便后续朗读"第 X 章"）
        若识别失败返回 []。
    """
    # 找所有命中位置
    matches: list[tuple[int, int, str]] = []  # (start, end, matched_line)
    for pat in _CHAPTER_TITLE_PATTERNS:
        for m in pat.finditer(text):
            # 去重：同一位置可能被多个模式命中
            if not any(abs(m.start() - s) < 5 for s, _, _ in matches):
                matches.append((m.start(), m.end(), m.group(0).strip()))

    if len(matches) < 2:
        return []

    # 按位置排序
    matches.sort(key=lambda x: x[0])

    # 切分章节
    chapters: list[Chapter] = []
    for i, (start, end, title_line) in enumerate(matches):
        # 章节内容：从该标题行开头到下一个标题行开头
        # 标题行之前的文本（如果有）算到前一章的尾部
        chapter_start = start
        if i + 1 < len(matches):
            chapter_end = matches[i + 1][0]
        else:
            chapter_end = len(text)
        chapter_text = text[chapter_start:chapter_end].strip()
        if not chapter_text:
            continue
        chapters.append(Chapter(
            idx=len(chapters),
            title=title_line,
            text=chapter_text,
        ))

    # 如果第一章标题前还有内容（如版权页、简介），把它合并进第一章
    # 或单独作为"序"章
    leading = text[: matches[0][0]].strip()
    if leading and len(leading) > 50:
        chapters.insert(0, Chapter(idx=0, title="序", text=leading))
        for i, c in enumerate(chapters):
            c.idx = i

    return chapters


# 兜底：按字数硬切（保留句子边界）
def split_chapters_by_size(text: str, max_chars: int = 30000) -> list[Chapter]:
    """
    正则失败时按字数硬切，尽量在句子边界（。！？.!?）切。
    每章 idx 自动递增，title 用「第 X 部分」。
    """
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [Chapter(idx=0, title="正文", text=text.strip())]

    chapters: list[Chapter] = []
    cursor = 0
    part_idx = 1
    while cursor < len(text):
        end = min(cursor + max_chars, len(text))
        if end < len(text):
            # 在 [cursor, end] 范围内找最后一个句子结束符
            for j in range(end, max(cursor, end - 2000), -1):
                if text[j - 1] in "。！？!?":
                    end = j
                    break
        chunk = text[cursor:end].strip()
        if chunk:
            chapters.append(Chapter(
                idx=len(chapters),
                title=f"第 {part_idx} 部分",
                text=chunk,
            ))
            part_idx += 1
        cursor = end
    return chapters


# LLM 标题优化：对硬切的章节，让 LLM 生成更有意义的标题
async def refine_chapter_titles_with_llm(chapters: list[Chapter]) -> list[Chapter]:
    """
    对按字数硬切的章节，调 LLM 生成章节标题（基于内容摘要）。
    原有正则命中的标题保持不变。
    """
    if not chapters:
        return chapters
    # 只对标题是「第 X 部分」的章节调 LLM
    needs_refine = [
        (i, c) for i, c in enumerate(chapters)
        if c.title.startswith("第 ") and c.title.endswith(" 部分")
    ]
    if not needs_refine:
        return chapters

    # 一次性把所有章节的开头片段喂给 LLM，让它批量生成标题
    snippets = []
    for i, c in needs_refine:
        snippet = c.text[:200].replace("\n", " ").strip()
        snippets.append(f"[idx={i}] {snippet}...")

    prompt = (
        "你是一名小说编辑。下面是若干章节的开头片段，请为每章生成 4-12 字的标题，"
        "概括章节核心内容。\n"
        "输出格式：JSON 数组，每项 {idx, title}。idx 必须与输入一致。\n\n"
        + "\n".join(snippets)
        + "\n\n输出 JSON："
    )

    class _Item:
        idx: int
        title: str

    class _Wrapper:
        data: list[dict]

    try:
        from pydantic import BaseModel
        class _W(BaseModel):
            data: list[dict]

        llm = get_llm()
        wrapped = await llm.chat_structured(
            prompt=prompt,
            output_schema=_W,
            temperature=0.3,
            max_tokens=4000,
        )
        title_map: dict[int, str] = {}
        for item in wrapped.data:
            try:
                title_map[int(item["idx"])] = str(item["title"])[:50]
            except (KeyError, ValueError, TypeError):
                continue
        for i, c in needs_refine:
            if i in title_map and title_map[i].strip():
                chapters[i].title = title_map[i].strip()
    except Exception as e:
        logger.warning(f"[book_split] LLM 标题优化失败，保留默认: {e}")

    return chapters


async def split_book_chapters(text: str) -> list[Chapter]:
    """
    整本小说章节识别主入口。

    流程：
    1. 先用正则识别（快、零成本）
    2. 正则命中 < 2 章 → 按字数硬切 + LLM 生成标题
    3. 硬切也失败 → 单章
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    total_chars = len(text)
    logger.info(f"[book_split] START chars={total_chars}")

    # 1. 正则
    chapters = split_chapters_regex(text)
    if len(chapters) >= 2:
        logger.info(
            f"[book_split] regex ok: chapters={len(chapters)} "
            f"first={chapters[0].title!r} last={chapters[-1].title!r}"
        )
        # 修正 idx（可能插入了序章）
        for i, c in enumerate(chapters):
            c.idx = i
        return chapters

    # 2. 硬切 + LLM 标题
    logger.info(f"[book_split] regex 未命中（{len(chapters)} 章），退化为字数硬切")
    chapters = split_chapters_by_size(text, max_chars=30000)
    if len(chapters) >= 2:
        chapters = await refine_chapter_titles_with_llm(chapters)
        logger.info(
            f"[book_split] size_split ok: chapters={len(chapters)} "
            f"first={chapters[0].title!r} last={chapters[-1].title!r}"
        )
        return chapters

    # 3. 单章兜底
    logger.info(f"[book_split] fallback single chapter")
    return [Chapter(idx=0, title="正文", text=text.strip())]
