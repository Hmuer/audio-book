"""EPUB 解析模块：从 .epub 文件读取元数据、封面、章节有序列表。

设计要点：
- ebooklib 负责解压 + 读 opf/toc 元数据
- beautifulsoup4 + lxml-xml 负责清理每个章节的 XHTML，提取纯文本
- 章节顺序严格按 spine 顺序（不是 get_items_of_type 的遍历顺序，那不可靠）
- 跳过 nav / cover / stylesheet / script / advertisement 等非正文内容
"""
from __future__ import annotations

import io
import logging
import re
import warnings
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)

_DOCUMENT_TYPE = 9
_IMAGE_TYPE = 4

# 要跳过的章节标题（非常短且带这些关键词的章节才跳）
_SKIP_TITLE_KEYWORDS = re.compile(r"(目录页|版权页|封面页|扉页)", re.IGNORECASE)
# 正文中的广告关键词（只有章节非常短时才触发跳过）
_SKIP_BODY_KEYWORDS = re.compile(r"(www\.|\.com/|免费下载|更多精彩内容)")


@dataclass
class EpubChapter:
    idx: int
    title: str
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EpubBook:
    title: str
    author: str
    language: str
    cover_bytes: bytes | None
    chapters: list[EpubChapter]

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "cover": self.cover_bytes.hex() if self.cover_bytes else None,
            "chapters": [c.to_dict() for c in self.chapters],
        }


def _html_to_text(html: str) -> str:
    """把 XHTML 片段清洗成纯文本。"""
    soup = BeautifulSoup(html, "lxml-xml")
    for tag in soup.find_all(["script", "style", "nav", "iframe", "svg", "noscript"]):
        tag.decompose()
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        h.insert_before("\n")
        h.insert_after("\n")
    for p in soup.find_all("p"):
        p.append("\n")
    for li in soup.find_all("li"):
        li.append("\n")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(separator="", strip=False)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [l.strip() for l in text.split("\n")]
    return "\n".join(lines).strip()


def _pick_chapter_title(item_id: str, html: str, fallback_idx: int) -> str:
    soup = BeautifulSoup(html, "lxml-xml")
    for tag in soup.find_all(re.compile(r"^h[1-2]$")):
        t = tag.get_text(strip=True)
        if t and len(t) <= 80:
            return t
    for tag in soup.find_all("h3"):
        t = tag.get_text(strip=True)
        if t and len(t) <= 80:
            return t
    return Path(item_id or f"chapter_{fallback_idx}").stem


def read_epub(epub_path: str | Path | bytes) -> EpubBook:
    """解析 EPUB 文件（bytes / 路径均可）。"""
    try:
        if isinstance(epub_path, (bytes, bytearray)):
            book = epub.read_epub(io.BytesIO(bytes(epub_path)))
        else:
            book = epub.read_epub(str(epub_path))
    except zipfile.BadZipFile as e:
        raise ValueError(f"文件不是合法的 EPUB（坏 zip）: {e}") from e
    except Exception as e:
        msg = str(e).lower()
        if "encrypted" in msg or "drm" in msg or "aes" in msg:
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
            c = creators[0]
            author = (c[0] if isinstance(c, (list, tuple)) else str(c)).strip()
        langs = book.get_metadata("DC", "language") or []
        if langs:
            l = langs[0]
            language = (l[0] if isinstance(l, (list, tuple)) else str(l)).strip() or "zh"
    except Exception as e:
        logger.warning(f"[epub_reader] 元数据读取失败（可忽略）: {e}")

    # ---- 封面 ----
    cover_bytes: bytes | None = None
    try:
        cover_item = book.get_item_with_id("cover") or book.get_item_with_id("cover-image")
        if cover_item is None:
            for it in book.get_items_of_type(_IMAGE_TYPE):
                if it and "cover" in (it.id or "").lower():
                    cover_item = it
                    break
        if cover_item is not None:
            cover_bytes = bytes(cover_item.get_content())
    except Exception as e:
        logger.warning(f"[epub_reader] 封面提取失败（可忽略）: {e}")

    # ---- 章节：严格按 spine 顺序 ----
    chapter_items: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    raw_spine = getattr(book, "spine", None) or []

    for entry in raw_spine:
        item_id: str | None = None
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
            item_id = str(entry[0])
        elif hasattr(entry, "href"):
            target = getattr(entry, "href", "").split("#")[0]
            if target:
                for it in book.get_items():
                    if it.file_name and it.file_name.endswith(target):
                        item_id = it.id
                        break
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        item = book.get_item_with_id(item_id)
        if item is None or getattr(item, "get_type", lambda: None)() != _DOCUMENT_TYPE:
            continue
        try:
            html = item.get_content().decode("utf-8", errors="replace")
        except Exception:
            continue
        chapter_items.append((item_id, html))

    if not chapter_items:
        logger.warning("[epub_reader] spine 为空，fallback 遍历所有文档节点")
        for it in book.get_items_of_type(_DOCUMENT_TYPE):
            try:
                html = it.get_content().decode("utf-8", errors="replace")
            except Exception:
                continue
            chapter_items.append((it.id or f"ch_{len(chapter_items)}", html))

    # ---- 清洗 ----
    chapters: list[EpubChapter] = []
    skipped = 0
    for idx, (item_id, html) in enumerate(chapter_items):
        text = _html_to_text(html)
        if not text or len(text.strip()) < 20:
            skipped += 1
            continue
        chapter_title = _pick_chapter_title(item_id, html, idx)

        is_susp_title = bool(_SKIP_TITLE_KEYWORDS.search(chapter_title))
        has_ad = bool(_SKIP_BODY_KEYWORDS.search(text[:100])) or bool(_SKIP_BODY_KEYWORDS.search(text[-100:]))
        if (is_susp_title or (has_ad and len(text) < 500)) and len(text) < 2000:
            skipped += 1
            continue

        chapters.append(EpubChapter(idx=len(chapters), title=chapter_title, text=text))

    if not chapters and chapter_items:
        logger.warning("[epub_reader] skip 规则全过滤了，回退保留全部")
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
