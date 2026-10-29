"""Sitemap index: every forum node (whose feed we poll), every thread and every tag, with lastmod."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

URL_RE = re.compile(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", re.S)
SITEMAP_RE = re.compile(r"<sitemap>\s*<loc>([^<]+)</loc>", re.S)
THREAD_RE = re.compile(r"^/forums/threads/(?:[^/]*?\.)?(\d+)/$")
TAG_RE = re.compile(r"^/forums/tags/[^/]+/$")
# Paths under /forums/ that are not forum nodes. Categories have feeds, but they are always empty.
NOT_NODES = ("/forums/threads/", "/forums/tags/", "/forums/members/", "/forums/categories/", "/forums/pages/")


@dataclass
class SitemapPage:
    nodes: list[tuple[str, str | None]] = field(default_factory=list)  # (path, lastmod)
    threads: list[tuple[int, str, str | None]] = field(default_factory=list)  # (id, url, lastmod)
    tags: list[tuple[str, str | None]] = field(default_factory=list)  # (url, lastmod)


def sitemap_files(index_xml: str) -> list[str]:
    return SITEMAP_RE.findall(index_xml)


def is_node(path: str) -> bool:
    """Forum nodes: /forums/forums/91/ and custom routes such as /forums/sports/."""
    return (
        path.startswith("/forums/")
        and path.endswith("/")
        and path != "/forums/"
        and not path.startswith(NOT_NODES)
    )


def parse_sitemap(xml: str) -> SitemapPage:
    out = SitemapPage()
    for loc, lastmod in URL_RE.findall(xml):
        path = urlparse(loc).path
        lastmod = lastmod or None
        if m := THREAD_RE.match(path):
            out.threads.append((int(m.group(1)), loc, lastmod))
        elif TAG_RE.match(path):
            out.tags.append((loc, lastmod))
        elif is_node(path):
            out.nodes.append((path, lastmod))
    return out
