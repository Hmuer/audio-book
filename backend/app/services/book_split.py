"""
章节识别：从书籍文本中切分朗读单元。**完全不调用 LLM，零成本。**

策略：
1. 按配置 CHAPTER_SPLIT_PATTERNS（支持用户扩展正则）匹配章节标题行
   → 命中时直接复用**原文标题**，不改写
2. 章/卷分级（H-20，剑来实测：『第一卷 笼中雀』被切成第 1 章，正文只有
   一行分隔线，全书章节错位）：
   - 合体行『第一卷 笼中雀 第一章 惊蛰』→ 按章处理，标题归一为从章级
     标记起的原文子串『第一章 惊蛰』（标题朗读 / zip 文件名都更干净）
   - 纯卷行『第一卷 笼中雀』→ 分组头，全书存在章级/关键词级标题时不
     作为章节（否则是只有分隔线的幽灵章）；全书只有卷行（卷即章的书）
     保持原行为
3. 剥离纯分隔线行（------ / ──── / ***** 等）：零朗读价值，不剥离会被
   narrator 段念出来，白占润色/对白归属的 token
4. 无编号的散文、诗集或自传：优先使用 Markdown/连续编号标题；没有可靠
   标题时按段落/句子切成朗读单元，原文仍完整保留。

章节切分规则可在 .env 中扩展 CHAPTER_SPLIT_PATTERNS 覆盖，也可直接改
config.py 默认值。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..core.config import settings
from .chapter import Chapter

logger = logging.getLogger(__name__)


class ChapterSplitError(RuntimeError):
    """章节切分失败：用户可通过补 CHAPTER_SPLIT_PATTERNS 解决。"""


# 章节编号前缀正则：用于从标题中去掉 "第X章"/"Chapter X" 等编号前缀，只保留标题正文
_CHAPTER_PREFIX_RE = re.compile(
    r'^\s*第\s*[零〇一二三四五六七八九十百千两0-9]+\s*'
    r'(?:章|回|节|卷|篇|部)\s*[:：、\.]*\s*'
    r'|^\s*(?:Ep|Episode|Vol|Volume|Ch|Chapter)\s*[\.\-:：]?\s*'
    r'[0-9IVXLCDM]+[\.\t \-:：]*'
    r'|^\s*Chapter\s+[0-9IVXLCDM]+[\.\t \-:：]*',
    re.IGNORECASE,
)


def strip_chapter_prefix(title: str) -> str:
    """去掉章节标题中的 '第X章'/'Chapter X' 等编号前缀，只保留标题正文。

    例: "第六章 神秘峡谷" → "神秘峡谷"
        "第6章 深海" → "深海"
        "Chapter 1 The Beginning" → "The Beginning"
        "序章" → "序章"（不匹配，原样返回）
    """
    if not title:
        return title
    stripped = _CHAPTER_PREFIX_RE.sub('', title).strip()
    return stripped or title


# ---------------------------------------------------------------------
# H-20 章/卷分级与分隔线清理
# ---------------------------------------------------------------------
# 章级标记（第X章/回/节）：一行内出现即按章处理；合体行标题从它起截取。
# 数字字符组与 CHAPTER_SPLIT_PATTERNS 第 1 条保持一致。
_CH_MARKER_RE = re.compile(
    r"第[ \t]*[零〇一二三四五六七八九十百千两0-9]+[ \t]*(?:章|回(?!合)|节)"
)
# 卷级标记（第X卷/篇/部）：仅当行内没有章级标记时才是「卷分组头」
_VOL_MARKER_RE = re.compile(
    r"第[ \t]*[零〇一二三四五六七八九十百千两0-9]+[ \t]*(?:卷|篇|部|集)"
)
_EXTRA_NUMBERED_RE = re.compile(
    r"^[ \t\u3000]*(?:[☆★＊*][、.]?[ \t\u3000]*)?"
    r"(?:(?:正文[ \t\u3000_]+){0,2})?(?:[0-9]{1,4}\.[ \t\u3000]*)?"
    r"(?:第[零〇一二三四五六七八九十百千两0-9]+"
    r"[ \t]*(?:卷|篇|部|集)[^\n]{0,40}?[ \t\u3000]+)?"
    r"第[ \t]*[零〇一二三四五六七八九十百千两0-9]+[ \t]*(?:章|回(?!合)|节)"
    r"[^\n]{0,90}$", re.MULTILINE,
)
_MARKDOWN_HEADING_RE = re.compile(r"^[ \t\u3000]*#{1,6}[ \t\u3000]+([^\n]{1,80})$", re.MULTILINE)
_PLAIN_NUMBERED_RE = re.compile(
    r"^[ \t\u3000]*([0-9]{1,4})[ \t\u3000]*[.．、][ \t\u3000]*([^\n]{2,70})$"
    r"|^[ \t\u3000]*([0-9]{1,4})[ \t\u3000]+([^\n]{2,70})$", re.MULTILINE,
)
_PROLOGUE_RE = re.compile(
    r"^[ \t\u3000]*第[零〇一二三四五六七八九十百千两0-9]+[ \t]*(?:卷|篇|部|集)"
    r"[^\n]{0,40}[ \t\u3000]+(?:引子|楔子|序章)(?:[ \t\u3000]+[^\n]{0,50})?$", re.MULTILINE,
)
# 纯分隔线：整行只有 3 个及以上连续排版符号（------ / ──── / ***** / _____）
# 含半角/全角变体。不匹配行内带正文的破折号对白（—— 你说什么）。
_SEPARATOR_LINE_RE = re.compile(
    r"^[ \t]*[-－—─_=＝+＋*＊~～]{3,}[ \t]*$",
    re.MULTILINE,
)


# 中文数字 → 阿拉伯，用于章节序号归一化（仅前 99）
_CN_NUM_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _compile_patterns(raw_patterns: list[str]) -> list[re.Pattern]:
    """从 settings 的正则字符串列表编译成 Pattern（统一 re.MULTILINE，中英文场景都适用）。"""
    compiled: list[re.Pattern] = []
    for i, raw in enumerate(raw_patterns):
        try:
            # 默认大小写敏感（中文无所谓，英文 Chapter 已独立写大小写两种规则）；
            # 对 Chapter 场景保留 IGNORECASE：如果 raw 里有 [a-zA-Z] 就自动带，否则只 MULTILINE。
            flags = re.MULTILINE
            if re.search(r"[a-zA-Z]", raw):
                flags |= re.IGNORECASE
            compiled.append(re.compile(raw, flags))
        except re.error as e:
            logger.error(
                f"[book_split] CHAPTER_SPLIT_PATTERNS[{i}] 编译失败，已跳过："
                f"pattern={raw!r} err={e}"
            )
    if not compiled:
        logger.warning(
            "[book_split] CHAPTER_SPLIT_PATTERNS 为空或全部编译失败，"
            "切章功能将无法命中任何章节标题。"
        )
    return compiled


# ----------------------------------------------------------------
# 运行时编译缓存：支持运行时通过 settings API 修改正则后即时生效
#   - _PATTERNS_CACHE: 已编译的 Pattern 列表
#   - _PATTERNS_VERSION: 缓存的设置快照标识；与当前 settings 对象引用比对
# ----------------------------------------------------------------
_PATTERNS_CACHE: list[re.Pattern] | None = None
_PATTERNS_SNAPSHOT: tuple[str, ...] | None = None


def refresh_chapter_patterns() -> list[re.Pattern]:
    """强制重新编译 CHAPTER_SPLIT_PATTERNS（写入 settings 后调用）。"""
    global _PATTERNS_CACHE, _PATTERNS_SNAPSHOT
    _PATTERNS_CACHE = _compile_patterns(settings.CHAPTER_SPLIT_PATTERNS)
    _PATTERNS_SNAPSHOT = tuple(settings.CHAPTER_SPLIT_PATTERNS)
    return _PATTERNS_CACHE


def get_chapter_patterns() -> list[re.Pattern]:
    """获取当前生效的章节切分正则（懒加载 + 快照刷新）。"""
    global _PATTERNS_CACHE, _PATTERNS_SNAPSHOT
    current = tuple(settings.CHAPTER_SPLIT_PATTERNS)
    if _PATTERNS_CACHE is None or _PATTERNS_SNAPSHOT != current:
        refresh_chapter_patterns()
    assert _PATTERNS_CACHE is not None
    return _PATTERNS_CACHE


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


def split_chapters_regex(text: str, *, min_matches: Optional[int] = None) -> list[Chapter]:
    """
    用配置里的正则识别章节。命中数 >= min_matches 才认为识别成功。

    关于开头内容（广告/书名/作者/简介/卷头）：
    - 首标题前短内容通常是书名/广告，超过 1000 字的开篇正文保留。
    - 纯卷行（第X卷/篇/部）在全书存在章级/关键词级标题时同样不作为章节
      （H-20 分组头，见模块 docstring）；全书只有卷行时卷即章，保持原行为。
    - 原文标题单独朗读，不保留在正文中重复朗读。

    Args:
        text: 全文（已归一化换行符）
        min_matches: 最少命中章节数；None 时读 settings.CHAPTER_SPLIT_MIN_MATCHES

    Returns:
        chapters: list[Chapter]，每章 text 包含从该标题到下一个标题之间的正文内容（不含标题行本身）
        若命中数不足返回 []（不是抛异常，由主入口决定是否进入硬切/报错分支）。
    """
    min_matches = int(min_matches if min_matches is not None else settings.CHAPTER_SPLIT_MIN_MATCHES)
    # 找所有命中位置
    matches: list[tuple[int, int, str]] = []  # (start, end, matched_line)
    seen_starts: set[int] = set()
    # 内置补充规则也作用于持久化过旧版 CHAPTER_SPLIT_PATTERNS 的安装。
    for pat in [*get_chapter_patterns(), _EXTRA_NUMBERED_RE, _PROLOGUE_RE]:
        for m in pat.finditer(text):
            start = m.start()
            if start in seen_starts:
                continue
            if len(m.group(0).strip()) > 110:
                continue
            if re.match(r"第[^\n]{0,20}回合", m.group(0).strip()):
                continue
            seen_starts.add(start)
            matches.append((start, m.end(), m.group(0).strip()))

    # 无明确章标题时，再尝试排版标题。数字标题必须构成连续序列，避免把
    # 正文中的列表项目或日期误当章节。Markdown 标题本身就是强排版信号。
    if len(matches) < min_matches:
        md = [(m.start(), m.end(), m.group(1).strip()) for m in _MARKDOWN_HEADING_RE.finditer(text)]
        if md:
            matches = md
        else:
            numbered = []
            for m in _PLAIN_NUMBERED_RE.finditer(text):
                n = int(m.group(1) or m.group(3))
                title = (m.group(2) or m.group(4)).strip()
                if (n <= 10000 and re.search(r"[\u4e00-\u9fff]", title)
                        and not title[0].isdigit()
                        and not re.search(r"[。！？!?；;]", title)):
                    numbered.append((m.start(), m.end(), f"{n} {title}", n))
            runs: list[list[tuple[int, int, str, int]]] = []
            for item in numbered:
                if not runs or item[3] != runs[-1][-1][3] + 1:
                    runs.append([])
                runs[-1].append(item)
            best = max(runs, key=len, default=[])
            gaps = [b[0] - a[0] for a, b in zip(best, best[1:])]
            if len(best) >= 5 and sum(gap >= 300 for gap in gaps) >= len(gaps) * 3 // 4:
                matches = [(s, e, title) for s, e, title, _ in best]

    # 按位置排序
    matches.sort(key=lambda x: x[0])

    # 下载站传记常以「第1节、第2节……」包住一份目录；目录里的
    # 「第一章」只是列表项。数字节序列占主导时按节切，避免目录幽灵章。
    arabic_sections = [m for m in matches if re.match(r"(?:正文\s+)?第\d+节", m[2])]
    if len(arabic_sections) >= 5 and len(arabic_sections) >= len(matches) // 2:
        matches = arabic_sections

    # ---- H-20 章/卷分级 ----
    # 「全书存在任何非卷级标题」= 卷行只是分组头，应剔除；否则（全书只有
    # 卷行）卷即章，保持原行为。非卷级 = 有章级标记，或是序章/番外/Chapter X
    # 等无卷标记的命中行。
    has_chapter_level = any(
        _CH_MARKER_RE.search(line) or not _VOL_MARKER_RE.search(line)
        for _, _, line in matches
    )
    kept: list[tuple[int, int, str]] = []
    volume_spans: list[tuple[int, int]] = []  # 被剔除的卷标题行 (start, end)
    for start, end, line in matches:
        ch_m = _CH_MARKER_RE.search(line)
        if ch_m:
            # 章级（含『第X卷 XXX 第X章 YYY』合体行）：
            # 标题归一为从章级标记起的原文子串 →『第一章 惊蛰』
            kept.append((start, end, line[ch_m.start():].strip()))
        elif _VOL_MARKER_RE.search(line) and has_chapter_level and not _PROLOGUE_RE.match(line):
            volume_spans.append((start, end))
        else:
            kept.append((start, end, line))
    if volume_spans:
        preview = [text[s:e].strip() for s, e in volume_spans[:3]]
        logger.info(
            f"[book_split] 卷/篇/部级标题 {len(volume_spans)} 行不作为章节"
            f"（全书存在章级标题）：{preview}{'...' if len(volume_spans) > 3 else ''}"
        )
    matches = kept

    if len(matches) < min_matches:
        return []

    # 切分章节
    chapters: list[Chapter] = []
    # 若首个标题前已有成篇正文（常见于无编号的开篇，再从第二章才有编号），
    # 将其保留为开篇单元；短书名/作者/下载站声明仍不当正文。
    preamble = text[:matches[0][0]]
    if "用户上传之内容开始" in preamble:
        preamble = preamble.split("用户上传之内容开始", 1)[1]
    preamble = _SEPARATOR_LINE_RE.sub("", preamble).strip()
    if len(preamble) >= 1000:
        chapters.append(Chapter(idx=0, title="开篇", text=preamble))
    for i, (start, end, title_line) in enumerate(matches):
        # 章节内容：标题行之后到下一个标题行开头（match span 即标题行本身，
        # 正文从 end 起切片 —— 标题段在 chapter.py 里单独朗读，留在 text 里
        # 会双重朗读）
        body_start = end
        chapter_end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        # 剔除本章区间内残留的卷标题行 span（卷行的正文如卷头语并入上一章
        # 尾部，只删标题行本身，不丢内容）
        pieces: list[str] = []
        cursor = body_start
        for vs, ve in volume_spans:
            if vs < body_start or vs >= chapter_end:
                continue
            pieces.append(text[cursor:vs])
            cursor = ve
        pieces.append(text[cursor:chapter_end])
        chapter_text = "".join(pieces).strip()
        # 剥离纯分隔线行（------ / ──── 等）：零朗读价值，留着会被 narrator
        # 段念出来（TTS 对连续符号会读出怪音），还白占润色/对白归属的 token
        chapter_text = _SEPARATOR_LINE_RE.sub("", chapter_text).strip()
        if not chapter_text:
            continue
        chapters.append(Chapter(
            idx=len(chapters),
            title=title_line,
            text=chapter_text,
        ))

    # 序章/楔子等明确标题按原规则单独成章；长开篇由上方 preamble 保留。

    unique: list[Chapter] = []
    for ch in chapters:
        if (unique and len(ch.text) > 500
                and ch.title.strip() == unique[-1].title.strip()
                and ch.text == unique[-1].text):
            logger.info("[book_split] 跳过相邻重复章节 %r", ch.title)
            continue
        ch.idx = len(unique)
        unique.append(ch)
    return unique


# 兜底：按字数硬切（保留句子边界）
def split_chapters_by_size(text: str, max_chars: Optional[int] = None) -> list[Chapter]:
    """
    正则失败时按字数硬切，尽量在句子边界（。！？.!?）切。
    每章 idx 自动递增，title 用「第 X 部分」。
    """
    max_chars = max(1000, int(max_chars if max_chars is not None else settings.CHAPTER_SPLIT_HARD_FALLBACK_MAX_CHARS))
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
            # 先找段落边界；诗歌尽量不拆断行。没有段落再找句子边界。
            para = text.rfind("\n\n", max(cursor, end - 3000), end)
            if para > cursor + max_chars // 2:
                end = para + 2
            else:
                for j in range(end, max(cursor, end - 2000), -1):
                    if text[j - 1] in "。！？!?\n":
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


def _limit_chapter_size(chapters: list[Chapter], max_chars: int = 30_000) -> list[Chapter]:
    """即使书中缺失部分标题，也不把几十万字送给单次润色/对白请求。"""
    result: list[Chapter] = []
    for ch in chapters:
        if len(ch.text) <= max_chars:
            result.append(Chapter(idx=len(result), title=ch.title, text=ch.text))
            continue
        parts = split_chapters_by_size(ch.text, max_chars=max_chars)
        logger.warning("[book_split] 超长章节 %r chars=%s，拆成 %s 个朗读单元", ch.title, len(ch.text), len(parts))
        for i, part in enumerate(parts, 1):
            result.append(Chapter(
                idx=len(result), title=f"{ch.title}（第 {i} 部分）", text=part.text,
            ))
    return result


async def split_book_chapters(text: str) -> list[Chapter]:
    """
    书籍朗读单元识别主入口。

    **完全不调用 LLM，零成本；无可靠标题时保留全文并按长度切分。**

    流程：
    1. 用 settings.CHAPTER_SPLIT_PATTERNS（用户可扩展）匹配常见章节标题
       → 命中时直接复用**原文标题**，不做任何改写
    2. 命中数 >= CHAPTER_SPLIT_MIN_MATCHES → 返回章节列表
    3. 标题不足时按段落和长度切成朗读单元；短篇保留为单单元。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    total_chars = len(text)
    logger.info(f"[book_split] START chars={total_chars}")

    # 1. 正则 → 复用原文标题（完全不调 LLM）
    chapters = split_chapters_regex(text)
    if not chapters:
        # 短篇或单篇散文集可能恰好只有一个明确标题。
        single = split_chapters_regex(text, min_matches=1)
        if len(single) == 1 and 0 <= text.find(single[0].title) < 1000:
            chapters = single
    if chapters:
        logger.info(
            f"[book_split] regex ok: chapters={len(chapters)} "
            f"first={chapters[0].title!r} last={chapters[-1].title!r}"
        )
        chapters = _limit_chapter_size(chapters)
        for i, c in enumerate(chapters):
            c.idx = i
        return chapters

    logger.info(
        f"[book_split] 标题不足：chapters={len(chapters)} "
        f"< min={settings.CHAPTER_SPLIT_MIN_MATCHES}，按段落切分"
    )

    # 散文、诗集、自传可以没有编号或统一标题格式。不能把整本书判为导入失败。
    chapters = split_chapters_by_size(text)
    if chapters:
        logger.info(f"[book_split] structural fallback chapters={len(chapters)} chars={total_chars}")
        return chapters
    raise ChapterSplitError("源文件没有可朗读的正文")
