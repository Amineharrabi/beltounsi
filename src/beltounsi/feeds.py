"""Parse XenForo 2 RSS feeds.

Endpoints checked on tunisia-sat.com (2026-10-29): /forums/forums/-/index.rss (site-wide) and
<forum>/index.rss per forum return the 20 newest threads, each with the first post cut to a
snippet. Category, thread, tag and what's-new feeds exist but are empty; ?page= is ignored.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .clean import html_to_text

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "slash": "http://purl.org/rss/1.0/modules/slash/",
}
THREAD_RE = re.compile(r"/threads/(?:[^/]*?\.)?(\d+)/")
GLOBAL_FEED = "/forums/forums/-/index.rss"


@dataclass
class FeedItem:
    thread_id: int
    title: str
    url: str
    forum: str | None
    forum_path: str | None
    pub_date: str | None  # ISO 8601
    replies: int | None
    text: str
    truncated: bool
    author_hash: str | None


def hash_author(name: str | None, salt: str) -> str | None:
    if not name or not salt:
        return None
    return hashlib.sha256(f"{salt}:{name}".encode()).hexdigest()[:16]


def _iso(rfc822: str | None) -> str | None:
    if not rfc822:
        return None
    try:
        return parsedate_to_datetime(rfc822).isoformat()
    except (TypeError, ValueError):
        return None


def parse_feed(xml: bytes | str, author_salt: str = "") -> tuple[str | None, list[FeedItem]]:
    """Returns (channel title, items). Raises ET.ParseError on a non-feed body (e.g. an HTML error page)."""
    root = ET.fromstring(xml)
    channel = root.find("channel")
    if channel is None:
        raise ET.ParseError("no <channel>")
    items = []
    for it in channel.findall("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or "").strip()
        m = THREAD_RE.search(link)
        thread_id = int(guid) if guid.isdigit() else int(m.group(1)) if m else None
        if thread_id is None:
            continue
        cat = it.find("category")
        replies = it.findtext("slash:comments", namespaces=NS)
        text, truncated = html_to_text(it.findtext("content:encoded", default="", namespaces=NS))
        items.append(
            FeedItem(
                thread_id=thread_id,
                title=(it.findtext("title") or "").strip(),
                url=link,
                forum=(cat.text or "").strip() if cat is not None else None,
                forum_path=(urlparse(cat.get("domain", "")).path or None) if cat is not None else None,
                pub_date=_iso(it.findtext("pubDate")),
                replies=int(replies) if replies and replies.strip().isdigit() else None,
                text=text,
                truncated=truncated,
                author_hash=hash_author(it.findtext("dc:creator", namespaces=NS), author_salt),
            )
        )
    return channel.findtext("title"), items
