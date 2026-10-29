"""Turn the HTML in a feed item's <content:encoded> into plain text."""

from __future__ import annotations

import re
from html.parser import HTMLParser

SKIP_TAGS = {"blockquote", "script", "style", "iframe", "video", "img", "noscript", "pre", "code"}
BREAK_TAGS = {"br", "div", "p", "li", "tr", "h1", "h2", "h3", "h4"}
READ_MORE = {"قراءة المزيد", "read more", "lire la suite"}
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‎‏﻿"), None)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0  # depth inside dropped elements
        self.link: list[str] | None = None  # text of the <a> being read
        self.link_is_user = False
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            if tag not in {"img"}:  # void element: no end tag
                self.skip += 1
            return
        if self.skip:
            return
        if tag in BREAK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self.link = []
            self.link_is_user = "username" in (dict(attrs).get("class") or "")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and tag != "img":
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a" and self.link is not None:
            text = "".join(self.link).strip()
            if text.lower() in READ_MORE:
                self.truncated = True
            elif self.link_is_user:
                self.parts.append("@USER")  # keep the fact of a mention, not the name
            else:
                self.parts.append("".join(self.link))
            self.link = None
        elif tag in BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        (self.link if self.link is not None else self.parts).append(data)


def html_to_text(html: str) -> tuple[str, bool]:
    """Returns (text, truncated). Feeds cut long first posts and append a "read more" link."""
    p = _Text()
    p.feed(html)
    p.close()
    text = "".join(p.parts).translate(ZERO_WIDTH).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r]+", " ", ln).strip() for ln in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if p.truncated:
        text = re.sub(r"\s*(\.\.\.|…)$", "", text)
    return text, p.truncated
