"""采集：RSS / Atom → `NewsItem`。

设计取舍：
- **不用 feedparser**：只需要 title/link/summary/date 四个字段，标准库
  `xml.etree` 足够，少一个依赖。
- **逐源容错**：单个源超时/403 只记 error，不影响整期；errors 会写进
  `run_summary.json`，连续两期异常触发告警/熔断（见 pipeline）。
- **24h 窗口**：按"距现在多少小时"过滤，而不是按自然日，避免早上跑
  不到前一天的晚间新闻。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .types import NewsItem

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; audio-book-daily/1.0; "
    "+https://audio-book.ls-lh.cn/bot)"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
#: 摘要截断长度：太长会让打分/脚本 prompt 膨胀
SUMMARY_LIMIT = 400

_DATE_FIELDS = ("pubdate", "published", "updated", "date", "dc:date", "created")
_LINK_FIELDS = ("link", "guid", "id")


def strip_html(raw: str, limit: int = SUMMARY_LIMIT) -> str:
    """去标签 + 解实体 + 折叠空白，并截断。"""
    text = _HTML_TAG_RE.sub(" ", raw or "")
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


_EMOJI_CODE_RE = re.compile(r":[a-z0-9_+\-]{2,20}:")
# 中文源常见前缀：【重磅】【独家】… 整段去掉，而不是只去掉开头的括号
_BRACKET_PREFIX_RE = re.compile(r"^【[^】]{0,12}】\s*")
_LEADING_PUNCT_RE = re.compile(r"^[\s\-–—|·:：,，。.!！?？\[\]()（）]+")


def clean_title(raw: str) -> str:
    """清洗标题：去标签、去 `:fire:` 之类短代码、去前导标点。"""
    text = strip_html(raw, 200)
    text = _EMOJI_CODE_RE.sub(" ", text)
    text = _BRACKET_PREFIX_RE.sub("", text)
    text = _LEADING_PUNCT_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()[:200]


def _local(tag: str) -> str:
    """去掉 XML 命名空间并小写：`{http://…}item` → `item`。"""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el: ET.Element, names: tuple[str, ...]) -> str:
    for child in el:
        if _local(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _child_link(el: ET.Element) -> str:
    """RSS 的 `<link>text</link>` 与 Atom 的 `<link href="…"/>` 都要支持。"""
    for child in el:
        if _local(child.tag) not in _LINK_FIELDS:
            continue
        href = (child.get("href") or "").strip()
        if href:
            return href
        text = (child.text or "").strip()
        if text:
            return text
    return ""


def parse_date(raw: str) -> datetime | None:
    """解析 RFC822 / ISO8601 时间，统一成 UTC aware。"""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        if dt is not None:
            return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        iso = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_feed_xml(content: bytes | str, source_name: str) -> list[NewsItem]:
    """解析 RSS 2.0 / Atom / RDF 的 XML。纯函数，便于单测。"""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # 有些源带非法控制字符 / BOM；清掉常见噪音再试一次
        cleaned = bytes(b for b in raw if b >= 0x20 or b in (0x09, 0x0A, 0x0D))
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            raise ValueError("不是合法的 XML/RSS")

    nodes = [
        el for el in root.iter()
        if _local(el.tag) in ("item", "entry")
    ]
    items: list[NewsItem] = []
    for node in nodes:
        title = clean_title(_child_text(node, ("title",)))
        link = _child_link(node)
        if not title or not link:
            continue
        summary = strip_html(
            _child_text(node, ("description", "summary", "content", "encoded"))
        )
        published = parse_date(_child_text(node, _DATE_FIELDS))
        items.append(NewsItem(
            title=title,
            link=link,
            summary=summary,
            source=source_name,
            published=published,
        ))
    return items


def _dedupe_key(item: NewsItem) -> str:
    link = (item.link or "").split("?", 1)[0].rstrip("/").lower()
    base = link or item.title
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


async def fetch_source(
    client: httpx.AsyncClient, source: dict[str, Any], *,
    window_hours: float, now: datetime,
) -> tuple[list[NewsItem], dict[str, Any] | None]:
    """抓一个源。返回 (items, error)；error 为 None 表示成功。"""
    name = str(source.get("name") or source.get("url"))
    url = str(source.get("url") or "")
    try:
        resp = await client.get(url, timeout=20.0, follow_redirects=True)
        if resp.status_code >= 400:
            return [], {"source": name, "url": url, "error": f"HTTP {resp.status_code}"}
        items = parse_feed_xml(resp.content, name)
    except Exception as e:  # 网络/解析异常都按"该源失败"处理
        return [], {"source": name, "url": url, "error": f"{type(e).__name__}: {e}"}

    cutoff = now - timedelta(hours=window_hours)
    kept: list[NewsItem] = []
    for item in items:
        if item.published is None:
            # 无日期的条目按"刚刚"处理（有些中文源不写 pubDate）
            item.published = now
        if item.published < cutoff:
            continue
        if item.published > now + timedelta(hours=2):
            # 时钟偏差/未来时间：压回 now，避免排到最前
            item.published = now
        kept.append(item)
    return kept, None


async def collect(
    sources: list[dict[str, Any]], *,
    window_hours: float = 26,
    now: datetime | None = None,
    max_items: int = 200,
) -> tuple[list[NewsItem], list[dict[str, Any]]]:
    """并发采集所有启用源，去重后返回 (items, errors)。"""
    now = now or datetime.now(UTC)
    enabled = [s for s in sources if s.get("enabled", True) and s.get("url")]
    if not enabled:
        return [], [{"source": "-", "url": "-", "error": "没有启用的信源"}]

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(*[
            fetch_source(client, src, window_hours=window_hours, now=now)
            for src in enabled
        ])

    items: list[NewsItem] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for batch, err in results:
        if err:
            errors.append(err)
            logger.warning("[daily] 源失败 %s: %s", err["source"], err["error"])
            continue
        for item in batch:
            key = _dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

    # 新的在前；同时间按标题稳定排序，保证可复现
    items.sort(key=lambda i: (i.published_ts, i.title), reverse=True)
    return items[:max_items], errors
