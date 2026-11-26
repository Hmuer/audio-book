"""章节文本清洗。

网络抓取的小说正文里常混入“作者的话 / 求票 / 本章完 / 广告 / 预览页”等
非正文内容，TTS 会照读，非常影响听感。本模块用保守的规则把这些内容剔除：

* 整行都是 meta 的，删掉整行；
* 行内的括号内容如果整体是 meta（如 ``（本章完）``），只删括号部分；
* 超过 200 字的行视为正文，绝不整行删除；
* 单个通用词（``月票``/``打赏``/``加更``）只在短行或被括号包裹时才删，
  避免误删仅仅“提到”这些词的正常叙事。

同时提供 :func:`is_non_story_page`，用于判断整章是否为广告/预览/公告页，
这类页面不应送去做语音合成。
"""
from __future__ import annotations

import re

__all__ = ["clean_chapter_text", "is_non_story_page"]

# 整行匹配即视为 meta 的“强”规则：命中就删。
_STRONG_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"本章完",
        r"未完待续",
        r"求月票",
        r"求推荐票",
        r"求订阅",
        r"求收藏",
        r"求打赏",
        r"推荐票",
        r"码字",
        r"作者的话",
        r"^[Pp][Ss][:：]",
        r"公众号",
        r"微博",
        r"QQ群",
        r"书友群",
        r"更新时间",
        r"今天第.{1,3}更",
        r"感谢.{0,30}(?:打赏|月票|推荐票|收藏)",
        r"明天(?:继续|更新)",
        r"求个?(?:收藏|推荐|月票)",
        r"点击.{0,10}(?:推荐|收藏)",
        r"投.{0,6}票",
    )
)

# 通用单字规则：仅短行 / 括号包裹时才删，避免误删叙事。
_GENERIC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (r"月票", r"打赏", r"加更")
)

_MAX_NARRATIVE_LEN = 200

# 包裹用的括号对
_OPEN_BRACKETS = "（(【[《〈「『"
_CLOSE_BRACKETS = "）)】]》〉」』"

# “本章完 / 未完待续”括号，可能带抓取残留，如 （本章完）8)
_CHAPTER_END_PAREN_RE = re.compile(
    r"[（(]\s*(?:本章完|未完待续)\s*[）)]\s*\d*\s*[）)]?"
)

# 普通括号内容，用于删除“整体是 meta”的行内括号
_PAREN_RE = re.compile(r"[（(]([^（）()]*)[）)]")

# 整章广告/预览页标题关键词
_TITLE_MARKERS: tuple[str, ...] = (
    "新书预览",
    "预览",
    "公告",
    "广告",
    "上架感言",
    "完本感言",
    "请假",
    "感言",
)

# 广告页正文标记
_PAGE_AD_MARKERS: tuple[str, ...] = (
    "公众号",
    "首发",
    "全网",
    "关注",
    "搜索",
    "新书",
    "推荐票",
    "求月票",
)

_QUOTE_CHARS: frozenset[str] = frozenset("“”「」『』＂\"'‘’")


def _is_wrapped(s: str) -> bool:
    """行首行尾是否成对括号包裹。"""
    return len(s) >= 2 and s[0] in _OPEN_BRACKETS and s[-1] in _CLOSE_BRACKETS


def _generic_hit(s: str) -> bool:
    for pat in _GENERIC_PATTERNS:
        if pat.search(s):
            if len(s) <= 30 or _is_wrapped(s):
                return True
    return False


def _is_meta_text(s: str) -> bool:
    """判断一段（已 strip 的）文本整体是否是 meta。"""
    if not s:
        return False
    if len(s) > _MAX_NARRATIVE_LEN:
        return False
    for pat in _STRONG_PATTERNS:
        if pat.search(s):
            return True
    return _generic_hit(s)


def _strip_inline_meta(line: str) -> str:
    """删除行内“整体为 meta”的括号内容，返回处理后的行。"""
    line = _CHAPTER_END_PAREN_RE.sub("", line)

    def _repl(match: re.Match[str]) -> str:
        return "" if _is_meta_text(match.group(1).strip()) else match.group(0)

    return _PAREN_RE.sub(_repl, line)


def clean_chapter_text(text: str) -> tuple[str, dict]:
    """删除明显非正文的元信息行 / 括号内容。

    Returns:
        ``(cleaned_text, report)``，其中 ``report`` 形如::

            {"removed_lines": int, "removed_chars": int, "samples": list[str]}

        行结构（``\\n``）尽量保留；``removed_lines`` 表示被整行删除的行数，
        ``removed_chars`` 表示文本总长度的净减少量，``samples`` 为被删内容的
        少量样例。
    """
    if not text:
        return "", {"removed_lines": 0, "removed_chars": 0, "samples": []}

    lines = text.split("\n")
    kept: list[str] = []
    removed_lines = 0
    samples: list[str] = []

    for line in lines:
        stripped = line.strip()
        # 先尝试去掉行内 meta 括号
        new_line = _strip_inline_meta(line) if stripped else line
        new_stripped = new_line.strip()

        if not stripped:
            # 空行原样保留（只是可能本来带缩进空白，保持原样）
            kept.append(line)
            continue

        if not new_stripped:
            # 原来有内容，去掉 meta 括号后变空 -> 整行算删除
            removed_lines += 1
            if len(samples) < 20:
                samples.append(stripped[:120])
            continue

        if _is_meta_text(new_stripped):
            # 整行都是 meta -> 删除
            removed_lines += 1
            if len(samples) < 20:
                samples.append(new_stripped[:120])
            continue

        if new_line != line and len(samples) < 20:
            # 行内删掉了 meta 括号，记录样例
            samples.append(stripped[:120])
        kept.append(new_line)

    cleaned = "\n".join(kept)
    removed_chars = max(0, len(text) - len(cleaned))
    report = {
        "removed_lines": removed_lines,
        "removed_chars": removed_chars,
        "samples": samples,
    }
    return cleaned, report


def _count_quotes(text: str) -> int:
    return sum(1 for ch in text if ch in _QUOTE_CHARS)


def is_non_story_page(title: str, text: str) -> bool:
    """判断整章是否为广告 / 预览 / 公告页，不应做语音合成。

    保守策略：标题命中关键词直接判定；否则要求正文较短（<1200 字）且含有
    广告标记、且引号很少（对白密度低）才判定，保证正常长章节返回 ``False``。
    """
    if title:
        for marker in _TITLE_MARKERS:
            if marker in title:
                return True

    if len(text) < 1200:
        for marker in _PAGE_AD_MARKERS:
            if marker in text:
                if _count_quotes(text) < 2:
                    return True
    return False
