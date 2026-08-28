"""EPUB 解析模块：从 .epub 文件读取元数据、封面、章节有序列表。

设计要点：
- ebooklib 负责解压 + 读 opf/toc 元数据
- beautifulsoup4 + lxml 负责清理每个章节的 XHTML，提取纯文本
- 章节顺序严格按 spine 顺序（不是 get_items_of_type 的遍历顺序，那不可靠）
- 跳过 nav / cover / stylesheet / script / advertisement 等非正文内容
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub

logger = logging.getLogger(__name__)

# 典型要跳过的章节内容特征
_SKIP_KEYWORDS = re.compile(
    r"(目录|目录页|版权页|封面|扉页|序言|序章|楔子|前言|导语|"
    r"后记|跋|尾声|出版后记|附录|出版说明|"
    r"关于本书|关于作者|免费下载|更多精彩|欢迎访问|www\.|\.com|\.net)",
    re.IGNORECASE,
)


@dataclass
class EpubChapter:
    idx: int
    title: str
    text: str


@dataclass
class EpubBook:
    title: str
    author: str
    language: str
    cover_bytes: bytes | None
    chapters: list[EpubChapter]


def _html_to_text(html: str) -> str:
    """把一个 XHTML 片段清洗成纯文本。"""
    # lxml parser 比 html.parser 对 XHTML 更稳
    soup = BeautifulSoup(html, "lxml")
    # 直接干掉这些标签（连同内容）
    for tag in soup.find_all(["script", "style", "nav", "iframe", "svg", "noscript"]):
        tag.decompose()
    # 把 <br> / <p> / <h1~h6> / <div> 等块级元素替换成换行
    # 先处理标题（保留标题文字，加换行）
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        h.insert_after("\n")
        h.insert_before("\n")
    # <p> → 末尾加换行
    for p in soup.find_all("p"):
        p.append("\n")
    # <li> → 末尾加换行
    for li in soup.find_all("li"):
        li.append("\n")
    # <br> → 换行
    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text(separator="", strip=False)
    # 统一换行：\r\n → \n，连续 3+ 个空行压成 2 个
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 行首行尾空白
    lines = [l.strip() for l in text.split("\n")]
    return "\n".join(lines).strip()


def _pick_chapter_title(item_id: str, html: str, fallback_idx: int) -> str:
    """从章节 HTML 里猜一个标题。"""
    soup = BeautifulSoup(html, "lxml")
    # 先试第一个 h1/h2
    for tag in soup.find_all(re.compile(r"^h[1-2]$")):
        t = tag.get_text(strip=True)
        if t and len(t) <= 80:
            return t
    # 再试 h3
    for tag in soup.find_all("h3"):
        t = tag.get_text(strip=True)
        if t and len(t) <= 80:
            return t
    # 兜底：用文件名
    return Path(item_id or f"chapter_{fallback_idx}").stem


def read_epub(epub_path: str | Path | bytes) -> EpubBook:
    """解析 EPUB 文件。

    Args:
        epub_path: 可以是 str/Path（磁盘路径）或 bytes（内存中的文件内容）。

    Returns:
        EpubBook 包含元数据 + 有序章节列表。

    Raises:
        ValueError: 不是合法的 EPUB、损坏、或加密。
    """
    try:
        if isinstance(epub_path, (bytes, bytearray)):
            book = epub.read_epub(io.BytesIO(bytes(epub_path)))
        else:
            book = epub.read_epub(str(epub_path))
    except zipfile.BadZipFile as e:
        raise ValueError(f"文件不是合法的 EPUB（坏 zip）: {e}") from e
    except Exception as e:
        # ebooklib 对加密 / 损坏 EPUB 抛的异常类型不稳定
        msg = str(e).lower()
        if "encrypted" in msg or "drm" in msg:
            raise ValueError("EPUB 已加密（DRM），无法解析") from e
        raise ValueError(f"EPUB 解析失败: {type(e).__name__}: {e}") from e

    # ---- 元数据 ----
    title = ""
    author = ""
    language = "zh"
    try:
        if book.title:
            title = book.title.strip()
        creators = book.get_metadata("DC", "creator") or []
        if creators:
            # creator 可能是 list，第一个值是名字
            c = creators[0]
            author = (c[0] if isinstance(c, (list, tuple)) else str(c)).strip()
        langs = book.get_metadata("DC", "language") or []
        if langs:
            l = langs[0]
            language = (l[0] if isinstance(l, (list, tuple)) else str(l)).strip() or "zh"
    except Exception as e:
        logger.warning(f"[epub_reader] 读取元数据失败（可忽略）: {e}")

    # ---- 封面 ----
    cover_bytes: bytes | None = None
    try:
        # ebooklib 18+ 的接口
        cover_item = book.get_item_with_id("cover") or book.get_item_with_id("cover-image")
        if cover_item is None:
            # 遍历找 image 类型 + id 里带 cover 的
            for it in book.get_items_of_type(ebooklib.ITEM_IMAGE):
                if it and "cover" in (it.id or "").lower():
                    cover_item = it
                    break
        if cover_item is not None:
            cover_bytes = bytes(cover_item.get_content())
    except Exception as e:
        logger.warning(f"[epub_reader] 封面提取失败（可忽略）: {e}")

    # ---- 章节：严格按 spine 顺序 ----
    # spine = book.spine 是 list of (idref, linear)，idref 对应 item.id
    # 也有 toc = book.toc，是 list of Section(Title, Href, Children)
    # spine 顺序是 EPUB 规范里保证的阅读顺序，更靠谱
    chapter_items: list[tuple[str, str]] = []  # (item_id, html_content)

    seen_ids: set[str] = set()
    # spine 可能有两种形式：list[tuple[str, str]] 或 list[Section]
    # 我们做个兼容
    raw_spine = getattr(book, "spine", None) or []

    for entry in raw_spine:
        item_id: str | None = None
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
            item_id = str(entry[0])
        elif hasattr(entry, "href"):
            # Section 对象 — 从 href 里找 item
            href = getattr(entry, "href", "")
            if href:
                for it in book.get_items():
                    if it.file_name and it.file_name.endswith(href.split("#")[0]):
                        item_id = it.id
                        break
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        item = book.get_item_with_id(item_id)
        if item is None:
            continue
        # 只处理 DOCUMENT 类型（type=9）
        if getattr(item, "get_type", lambda: None)() != 9:
            continue
        try:
            html = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        chapter_items.append((item_id, html))

    # 如果 spine 拿不到，fallback：遍历所有 DOCUMENT item（顺序不稳定，但聊胜于无）
    if not chapter_items:
        logger.warning("[epub_reader] spine 为空，fallback 到遍历所有文档节点")
        for it in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            try:
                html = it.get_content().decode("utf-8", errors="replace")
            except Exception:
                continue
            chapter_items.append((it.id or f"ch_{len(chapter_items)}", html))

    # ---- 清洗 + 生成 EpubChapter ----
    chapters: list[EpubChapter] = []
    skipped = 0
    for idx, (item_id, html) in enumerate(chapter_items):
        text = _html_to_text(html)
        if not text or len(text.strip()) < 20:
            skipped += 1
            continue

        chapter_title = _pick_chapter_title(item_id, html, idx)

        # 过滤明显的目录页 / 版权页 / 广告
        first_100 = text[:100]
        if _SKIP_KEYWORDS.search(chapter_title) or _SKIP_KEYWORDS.search(first_100):
            # 但如果内容够长（>2000字），说明可能只是标题带了"序"，还是保留
            if len(text) < 2000:
                skipped += 1
                continue

        chapters.append(EpubChapter(idx=len(chapters), title=chapter_title, text=text))

    # 兜底：如果全部被跳过了（极少），把没过滤的也放进来
    if not chapters and chapter_items:
        logger.warning("[epub_reader] 所有章节被 skip 规则过滤，回退到全部保留")
        for idx, (item_id, html) in enumerate(chapter_items):
            text = _html_to_text(html)
            if text:
                chapters.append(EpubChapter(idx=len(chapters), title=_pick_chapter_title(item_id, html, idx), text=text))

    if not chapters:
        raise ValueError("EPUB 未解析到任何有效章节")

    logger.info(
        f"[epub_reader] title={title!r} author={author!r} "
        f"chapters={len(chapters)} skipped={skipped} cover={bool(cover_bytes)}"
    )

    return EpubBook(
        title=title,
        author=author,
        language=language,
        cover_bytes=cover_bytes,
        chapters=chapters,
    )
