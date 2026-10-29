"""Pure HTML parsing for XenForo 2 pages on Tunisia-Sat (thread and forum listing pages). No network access.

Selectors were checked against live pages on 2026-10-28:
post = article.message--post[data-content="post-<id>"], text = .message-body .bbWrapper,
date = time.u-dt[datetime], title = h1.p-title-value, section = last breadcrumb link.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

THREAD_RE = re.compile(r"/forums/threads/(?:[^/]*?\.)?(\d+)/?(?:page-(\d+))?")
POST_ID_RE = re.compile(r"post-(\d+)")

# Removed from post bodies before taking text.
DROP_SELECTORS = (
    "blockquote.bbCodeBlock--quote",  # quoted replies: would duplicate other posts' text
    ".bbCodeBlock--code",
    ".bbCodeSpoiler",
    ".bbCodeBlock--spoiler",
    ".bbCodeBlock--unfurl",  # link previews
    ".bbMediaWrapper",
    ".bbMediaJustifier",
    ".message-signature",
    ".js-selectToQuoteEnd",
    "iframe",
    "img",
    "video",
    "script",
    "style",
    "noscript",
)
BLOCK_TAGS = ("div", "p", "li", "tr", "h1", "h2", "h3", "h4")
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‎‏﻿"), None)
LINE_BREAK = ""  # private-use sentinel for <br>/block ends; not whitespace, so \s+ leaves it alone

@dataclass
class Post:
    post_id: int
    text: str
    post_date: str | None
    author_hash: str | None


@dataclass
class ThreadPage:
    thread_id: int
    page: int
    title: str
    section_path: str | None
    section_name: str | None
    posts: list[Post]
    next_url: str | None
    prev_url: str | None = None


@dataclass
class ListingThread:
    thread_id: int
    last_page: int  # from the row's page-jump links; 1 if the thread has one page
    latest_date: str | None  # date of the thread's last post (listings are sorted by it, newest first)
    sticky: bool  # pinned to the top of page 1 regardless of date


@dataclass
class ListingPage:
    thread_urls: list[str] = field(default_factory=list)
    threads: list[ListingThread] = field(default_factory=list)
    subforum_urls: list[str] = field(default_factory=list)
    next_url: str | None = None


def thread_url(thread_id: int, page: int = 1) -> str:
    base = f"https://www.tunisia-sat.com/forums/threads/{thread_id}/"
    return base if page <= 1 else f"{base}page-{page}"


def parse_thread_ref(url: str) -> tuple[int, int] | None:
    m = THREAD_RE.search(urlparse(url).path)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 1)


def hash_author(user_id: str | None, salt: str | None) -> str | None:
    if not user_id or not salt:
        return None
    return hashlib.sha256(f"{salt}:{user_id}".encode()).hexdigest()[:16]


def clean_text(text: str) -> str:
    text = text.translate(ZERO_WIDTH).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def body_text(body: Node) -> str:
    for sel in DROP_SELECTORS:
        for n in body.css(sel):
            n.decompose()
    # Mentions render as links to member profiles; keep the fact of a mention, not the name.
    for n in body.css("a.username"):
        n.replace_with("@USER")
    # Raw newlines in the HTML source are just whitespace (XenForo renders line breaks as <br>),
    # so mark real breaks with a sentinel, flatten whitespace, then restore the breaks.
    for n in body.css("br"):
        n.replace_with(LINE_BREAK)
    for tag in BLOCK_TAGS:
        for n in body.css(tag):
            n.insert_after(LINE_BREAK)
    text = re.sub(r"\s+", " ", body.text(deep=True))
    return clean_text(text.replace(LINE_BREAK, "\n"))


SITE_HOSTS = {"", "www.tunisia-sat.com", "tunisia-sat.com"}


def site_path(href: str | None) -> str | None:
    """Path of an on-site link, relative or absolute (Firecrawl's rawHtml makes links absolute)."""
    if not href:
        return None
    u = urlparse(href)
    return u.path if u.netloc in SITE_HOSTS else None


def _breadcrumb(tree: HTMLParser) -> tuple[str | None, str | None]:
    crumbs = tree.css_first("ul.p-breadcrumbs")
    if crumbs is None:
        return None, None
    links = crumbs.css("a[href]")
    if not links:
        return None, None
    last = links[-1]
    return site_path(last.attributes.get("href")), last.text(strip=True)


def _next_page_url(tree: HTMLParser, page_url: str, direction: str = "next") -> str | None:
    nxt = tree.css_first(f"a.pageNav-jump--{direction}[href]")
    return urljoin(page_url, nxt.attributes["href"]) if nxt else None


def parse_thread(html: str, url: str, author_salt: str | None = None) -> ThreadPage:
    ref = parse_thread_ref(url)
    if ref is None:
        raise ValueError(f"not a thread URL: {url}")
    thread_id, page = ref
    tree = HTMLParser(html)

    title_node = tree.css_first("h1.p-title-value")
    section_path, section_name = _breadcrumb(tree)

    posts: list[Post] = []
    for art in tree.css("article.message--post"):
        m = POST_ID_RE.search(art.attributes.get("data-content") or "")
        body = art.css_first(".message-body .bbWrapper")
        if not m or body is None:
            continue
        time_node = art.css_first("time.u-dt[datetime]")
        user_node = art.css_first(".message-user [data-user-id]") or art.css_first("[data-user-id]")
        text = body_text(body)
        if not text:
            continue
        posts.append(
            Post(
                post_id=int(m.group(1)),
                text=text,
                post_date=time_node.attributes.get("datetime") if time_node else None,
                author_hash=hash_author(
                    user_node.attributes.get("data-user-id") if user_node else None, author_salt
                ),
            )
        )

    return ThreadPage(
        thread_id=thread_id,
        page=page,
        title=clean_text(title_node.text()) if title_node else "",
        section_path=section_path,
        section_name=section_name,
        posts=posts,
        next_url=_next_page_url(tree, url),
        prev_url=_next_page_url(tree, url, "prev"),
    )


def parse_listing(html: str, url: str) -> ListingPage:
    tree = HTMLParser(html)
    out = ListingPage()
    seen: set[int] = set()
    sticky_ids = {
        int(m.group(1))
        for n in tree.css(".structItemContainer-group--sticky .structItem--thread")
        if (m := re.search(r"js-threadListItem-(\d+)", n.attributes.get("class") or ""))
    }
    for item in tree.css(".structItem--thread"):
        for a in item.css(".structItem-title a[href]"):
            ref = parse_thread_ref(a.attributes["href"])
            # Skip "/unread", "/latest", "/post-123" variants and prefix-label links.
            if ref and ref[0] not in seen and not re.search(r"/(unread|latest|post-\d+)", a.attributes["href"]):
                seen.add(ref[0])
                out.thread_urls.append(thread_url(ref[0]))
                pages = [r[1] for j in item.css(".structItem-pageJump a[href]")
                         if (r := parse_thread_ref(j.attributes["href"]))]
                latest = item.css_first("time.structItem-latestDate[datetime]")
                out.threads.append(
                    ListingThread(
                        thread_id=ref[0],
                        last_page=max(pages, default=1),
                        latest_date=latest.attributes["datetime"] if latest else None,
                        sticky=ref[0] in sticky_ids,
                    )
                )
    for a in tree.css(".node--forum .node-title a[href]"):
        path = site_path(a.attributes["href"])
        if path and path.startswith("/forums/"):
            out.subforum_urls.append(urljoin(url, path))
    out.next_url = _next_page_url(tree, url)
    return out


def parse_number(s: str) -> float:
    """Forum stat like '69,6K' or '1,1M' or '372' (comma is the decimal mark here)."""
    s = s.strip().replace("\xa0", "").replace(" ", "")
    mult = 1
    if s and s[-1] in "KkMm":
        mult = 1_000 if s[-1] in "Kk" else 1_000_000
        s = s[:-1]
    s = s.replace(",", ".")
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def parse_index_stats(html: str) -> list[dict]:
    """Per-forum thread/post counts from the forum index page."""
    tree = HTMLParser(html)
    rows = []
    for node in tree.css(".node--forum"):
        a = node.css_first(".node-title a[href]")
        dds = [d.text(strip=True) for d in node.css(".node-stats dd")]
        if a is None or site_path(a.attributes["href"]) is None:
            continue
        rows.append(
            {
                "path": site_path(a.attributes["href"]),
                "name": a.text(strip=True),
                "threads": parse_number(dds[0]) if len(dds) > 0 else None,
                "posts": parse_number(dds[1]) if len(dds) > 1 else None,
            }
        )
    return rows
