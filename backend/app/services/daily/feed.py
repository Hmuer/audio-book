"""播客 RSS（RSS 2.0 + iTunes 扩展）。

- 音频 enclosure 必须是**绝对地址**（Apple Podcasts / 小宇宙都要求）；
- 播客客户端无法带 JWT，所以 enclosure 用 `?key=` 鉴权（`DAILY_FEED_KEY`）；
- 日期用 RFC 822（RSS 规范）；`guid` 用日期，跨期稳定。
"""
from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from ...core.config import settings
from .constants import show_config


def public_base_url(request: Any | None = None) -> str:
    """对外可访问的 base URL（优先显式配置，其次请求头，最后本机地址）。"""
    explicit = (settings.DAILY_PUBLIC_BASE_URL or "").strip().rstrip("/")
    if explicit:
        return explicit
    if request is not None:
        headers = request.headers
        proto = headers.get("x-forwarded-proto") or request.url.scheme or "http"
        host = headers.get("x-forwarded-host") or headers.get("host") or request.url.netloc
        if host:
            return f"{proto}://{host}".rstrip("/")
    return "http://127.0.0.1:28000"


def _pub_date(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat(str(iso)) if iso else datetime.now()
    except ValueError:
        dt = datetime.now()
    return format_datetime(dt if dt.tzinfo else dt.astimezone())


def build_feed(
    issues: list[dict[str, Any]], *,
    base_url: str,
    feed_key: str,
    issue_dir: Path | None = None,
) -> str:
    show = show_config()
    channel_link = f"{base_url}/#/daily"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        "<channel>",
        f"  <title>{escape(show['title'])}</title>",
        f"  <link>{escape(channel_link)}</link>",
        f"  <description>{escape(show['description'])}</description>",
        "  <language>zh-cn</language>",
        f"  <itunes:author>{escape(show['author'])}</itunes:author>",
        "  <itunes:explicit>false</itunes:explicit>",
        "  <itunes:category text=\"Technology\"/>",
    ]
    key_q = f"?key={quote(feed_key, safe='')}" if feed_key else ""
    for issue in issues:
        date = str(issue.get("date") or "")
        if not date:
            continue
        title = str(issue.get("title") or date)
        audio_path = (issue_dir / date / "audio.mp3") if issue_dir else None
        size = audio_path.stat().st_size if audio_path and audio_path.is_file() else 0
        enclosure = f"{base_url}/api/daily/issues/{date}/audio{key_q}"
        lines += [
            "  <item>",
            f"    <title>{escape(title)}</title>",
            f"    <link>{escape(f'{base_url}/#/daily/{date}')}</link>",
            f"    <guid isPermaLink=\"false\">{escape(f'daily-{date}')}</guid>",
            f"    <pubDate>{_pub_date(issue.get('created_at'))}</pubDate>",
            f"    <description>{escape(str(issue.get('description') or title))}</description>",
            f"    <enclosure url={quoteattr(enclosure)} length=\"{size}\" type=\"audio/mpeg\"/>",
            "    <itunes:explicit>false</itunes:explicit>",
            "  </item>",
        ]
    lines += ["</channel>", "</rss>", ""]
    return "\n".join(lines)
