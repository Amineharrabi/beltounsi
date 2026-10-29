"""Output records (flat, one JSON object per line, one file per UTC day) for the crawl and RSS configs."""

from __future__ import annotations

import json
from pathlib import Path

from . import CRAWL_SOURCE, RSS_SOURCE, TUNISIA_SAT_LICENSE
from .pii import scrub

THREAD_URL = "https://www.tunisia-sat.com/forums/threads/{}/"


def crawl_record(*, text: str, post_id: int, thread_id: int, page: int, thread_title: str,
                 forum: str | None, forum_path: str | None, post_date: str | None,
                 author_hash: str | None, collected_at: str, method: str = "http") -> dict:
    text, n_masked = scrub(text)
    return {
        "text": text,
        "post_id": post_id,
        "thread_id": thread_id,
        "page": page,
        "url": f"{THREAD_URL.format(thread_id)}#post-{post_id}",
        "thread_title": thread_title,
        "forum": forum,
        "forum_path": forum_path,
        "post_date": post_date,
        "author_hash": author_hash,
        "pii_masked": n_masked,
        "collected_at": collected_at,
        "method": method,
        "source": CRAWL_SOURCE,
        "license": TUNISIA_SAT_LICENSE,
    }


def rss_record(item, collected_at: str) -> dict:
    text, n_masked = scrub(item.text)
    return {
        "text": text,
        "title": item.title,
        "thread_id": item.thread_id,
        "url": THREAD_URL.format(item.thread_id),
        "forum": item.forum,
        "forum_path": item.forum_path,
        "pub_date": item.pub_date,
        "replies": item.replies,
        "truncated": item.truncated,
        "author_hash": item.author_hash,
        "pii_masked": n_masked,
        "collected_at": collected_at,
        "source": RSS_SOURCE,
        "license": TUNISIA_SAT_LICENSE,
    }


def append_day(directory: Path, records: list[dict], collected_at: str) -> None:
    if not records:
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{collected_at[:10]}.jsonl").open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
