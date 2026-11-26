"""确定性中文小说引号切分。

历史实现依赖 LLM 找对白，会出现两个线上问题：

1. 重复朗读：旁白把整句读了一遍，对白音色又把引号里的内容读一遍；
2. 漏读对白：遇到 ``「」``、``『』``、直引号等非标准引号时识别不到。

本模块用纯规则、纯函数的方式扫描章节文本，返回有序、互不重叠的
:class:`QuoteSpan`。设计目标是“够用且稳定”：不追求语义理解，只把
“看起来像对白的引号片段”切出来，交给后续配音环节。

设计要点：

* 支持 ``“”``、``「」``、``『』``、直/全角双引号 ``"``/``＂``，以及
  不在双引号内部的单引号 ``‘’``；
* 嵌套引号只产出一个外层 span，内层引号保留在 ``inner`` 里；
* 不闭合的引号在换行 / 段末 / 文末之前，截到最后一个句末标点；
* 空引号（strip 后为空）会被丢弃。
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["QuoteSpan", "segment_quotes", "normalize_quotes"]


@dataclass(frozen=True)
class QuoteSpan:
    """一个引号片段。

    Attributes:
        start: 开引号在 ``text`` 中的下标。
        end: 闭引号之后一个字符的下标（``text[start:end]`` 即完整片段）。
        inner: 去掉一层外层引号与首尾空白后的内部文本。
        outer: 原始片段 ``text[start:end]``。
    """

    start: int
    end: int
    inner: str
    outer: str


# 成对引号：开 -> 闭（均为单码位字符）
_PAIRS: dict[str, str] = {
    "“": "”",
    "「": "」",
    "『": "』",
    "‘": "’",
}

# 直/全角双引号，开闭同形，按“最近的后续同类引号”配对
_STRAIGHT_DOUBLE: frozenset[str] = frozenset({'"', "＂"})

# 句末标点：不闭合引号截断到这里
_SENTENCE_ENDS: frozenset[str] = frozenset("。！？…；")

# 归一化用的稀有引号映射（1:1）
_NORMALIZE_MAP: dict[str, str] = {
    "〝": "“",
    "〞": "”",
    "﹁": "「",
    "﹂": "」",
    "﹃": "『",
    "﹄": "』",
    "„": "“",
    "‟": "”",
}

_STRAIGHT_SINGLE: frozenset[str] = frozenset({"'", "＇"})


def _line_end(text: str, start: int) -> int:
    """返回从 ``start`` 之后第一个换行符的位置，没有则返回文本长度。"""
    nl = text.find("\n", start + 1)
    return len(text) if nl == -1 else nl


def _find_balanced(text: str, start: int, opener: str, closer: str) -> int | None:
    """在同一行内寻找与 ``opener`` 配对的外层 ``closer``。

    对同形嵌套（如 ``“a“b”c”``）做深度计数，返回最外层闭合位置 +1。
    找不到返回 ``None``。
    """
    line_end = _line_end(text, start)
    depth = 1
    j = start + 1
    while j < line_end:
        ch = text[j]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return None


def _find_straight(text: str, start: int) -> int | None:
    """直/全角双引号：在同一行内找最近的后续同类引号。"""
    line_end = _line_end(text, start)
    for j in range(start + 1, line_end):
        if text[j] in _STRAIGHT_DOUBLE:
            return j + 1
    return None


def _find_unbalanced(text: str, start: int) -> int | None:
    """不闭合引号：截到本行最后一个句末标点之后。

    没有句末标点则返回 ``None``（宁可不产出，也不吞掉大段旁白）。
    """
    line_end = _line_end(text, start)
    for j in range(line_end - 1, start, -1):
        if text[j] in _SENTENCE_ENDS:
            return j + 1
    return None


def _make_span(
    text: str,
    start: int,
    end: int,
    closer_chars: frozenset[str],
    closed: bool,
) -> QuoteSpan | None:
    """构造 QuoteSpan；``inner`` 为空则返回 ``None``。"""
    inner_start = start + 1
    inner_end = end
    if closed and inner_end - 1 >= inner_start and text[inner_end - 1] in closer_chars:
        inner_end -= 1
    inner = text[inner_start:inner_end].strip()
    if not inner:
        return None
    return QuoteSpan(start=start, end=end, inner=inner, outer=text[start:end])


def segment_quotes(text: str) -> list[QuoteSpan]:
    """返回有序、互不重叠、覆盖对白引号的 :class:`QuoteSpan` 列表。"""
    if not text:
        return []

    n = len(text)
    spans: list[QuoteSpan] = []
    i = 0
    while i < n:
        ch = text[i]
        if ch in _PAIRS:
            closer = _PAIRS[ch]
            end = _find_balanced(text, i, ch, closer)
            closed = end is not None
            if end is None:
                end = _find_unbalanced(text, i)
            if end is not None and end > i + 1:
                span = _make_span(text, i, end, frozenset({closer}), closed)
                if span is not None:
                    spans.append(span)
                    i = end
                    continue
            i += 1
        elif ch in _STRAIGHT_DOUBLE:
            end = _find_straight(text, i)
            closed = end is not None
            if end is None:
                end = _find_unbalanced(text, i)
            if end is not None and end > i + 1:
                span = _make_span(text, i, end, _STRAIGHT_DOUBLE, closed)
                if span is not None:
                    spans.append(span)
                    i = end
                    continue
            i += 1
        else:
            i += 1

    # 兜底：保证有序、非重叠（若重叠保留更早/更长的一个）。
    spans.sort(key=lambda s: (s.start, -s.end))
    result: list[QuoteSpan] = []
    last_end = -1
    for span in spans:
        if span.start >= last_end:
            result.append(span)
            last_end = span.end
    return result


def normalize_quotes(text: str) -> str:
    """把稀有/全角/直引号尽量归一化到 ``“ ” 「 」 『 』``。

    直/全角双引号、单引号为开闭同形字符，这里用简单的开闭交替规则
    还原；其余变体按 1:1 字符映射。不做长度对齐。
    """
    out: list[str] = []
    double_open = True
    single_open = True
    for ch in text:
        if ch in _STRAIGHT_DOUBLE:
            out.append("“" if double_open else "”")
            double_open = not double_open
        elif ch in _STRAIGHT_SINGLE:
            out.append("‘" if single_open else "’")
            single_open = not single_open
        else:
            out.append(_NORMALIZE_MAP.get(ch, ch))
    return "".join(out)
