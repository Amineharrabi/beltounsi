"""SQLite state: crawl queue and seen posts, RSS feed schedule and items, sitemap index (nodes, threads, tags)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('listing', 'thread')),
    priority    INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    n_posts     INTEGER,
    fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS pages_queue ON pages (status, priority);
CREATE TABLE IF NOT EXISTS posts (
    post_id   INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS thread_latest (
    thread_id   INTEGER PRIMARY KEY,
    latest_date TEXT  -- last post date shown in the forum listing when we last queued the thread
);
CREATE TABLE IF NOT EXISTS feeds (
    url           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('global', 'node')),
    title         TEXT,
    interval_s    REAL NOT NULL,
    next_due      REAL NOT NULL,
    last_polled   TEXT,
    last_status   INTEGER,
    etag          TEXT,
    last_modified TEXT,
    failures      INTEGER NOT NULL DEFAULT 0,
    polls         INTEGER NOT NULL DEFAULT 0,
    new_items     INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS items (
    thread_id   INTEGER PRIMARY KEY,
    title       TEXT,
    text        TEXT,
    truncated   INTEGER,
    forum       TEXT,
    forum_path  TEXT,
    pub_date    TEXT,
    replies     INTEGER,
    author_hash TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id INTEGER PRIMARY KEY,
    url       TEXT NOT NULL,
    lastmod   TEXT
);
CREATE TABLE IF NOT EXISTS tags (
    url     TEXT PRIMARY KEY,
    lastmod TEXT
);
CREATE TABLE IF NOT EXISTS pushed (
    file      TEXT PRIMARY KEY,
    pushed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Feed:
    url: str
    kind: str
    interval_s: float
    next_due: float
    etag: str | None
    last_modified: str | None
    failures: int


class Store:
    def __init__(self, path: str | Path, clock=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.clock = clock

    def close(self) -> None:
        self.db.close()

    # ---- crawl queue

    def enqueue(self, url: str, kind: str, priority: int, force: bool = False) -> None:
        """Queue a page. `force` re-queues a page already fetched (its thread has new posts)."""
        if force:
            self.db.execute(
                "INSERT INTO pages (url, kind, priority) VALUES (?, ?, ?) ON CONFLICT(url) DO UPDATE SET"
                " status = 'pending', attempts = 0, priority = MIN(priority, excluded.priority)",
                (url, kind, priority),
            )
        else:
            self.db.execute("INSERT OR IGNORE INTO pages (url, kind, priority) VALUES (?, ?, ?)", (url, kind, priority))

    def next_page(self) -> tuple[str, str, int] | None:
        # Insertion order within a priority interleaves a listing page's threads before the next
        # listing page, so posts start flowing immediately.
        return self.db.execute(
            "SELECT url, kind, priority FROM pages WHERE status = 'pending' ORDER BY priority, rowid LIMIT 1"
        ).fetchone()

    def mark(self, url: str, status: str, http_status: int | None = None, n_posts: int | None = None) -> None:
        self.db.execute(
            "UPDATE pages SET status = ?, http_status = ?, n_posts = ?, fetched_at = ? WHERE url = ?",
            (status, http_status, n_posts, utc_now(), url),
        )

    def record_failure(self, url: str, http_status: int | None, max_attempts: int = 3) -> None:
        self.db.execute("UPDATE pages SET attempts = attempts + 1, http_status = ? WHERE url = ?", (http_status, url))
        (attempts,) = self.db.execute("SELECT attempts FROM pages WHERE url = ?", (url,)).fetchone()
        if attempts >= max_attempts:
            self.mark(url, "failed", http_status)

    def forget_listings(self) -> int:
        """Listings are re-read from page 1 on each recrawl; fetched thread pages are kept."""
        return self.db.execute("DELETE FROM pages WHERE kind = 'listing'").rowcount

    def thread_changed(self, thread_id: int, latest_date: str | None) -> tuple[bool, bool]:
        """Record a thread's last-post date from a listing. Returns (seen before, has new posts since)."""
        row = self.db.execute("SELECT latest_date FROM thread_latest WHERE thread_id = ?", (thread_id,)).fetchone()
        self.db.execute("INSERT OR REPLACE INTO thread_latest (thread_id, latest_date) VALUES (?, ?)",
                        (thread_id, latest_date))
        if row is None:
            return False, True
        return True, bool(latest_date and (row[0] is None or latest_date > row[0]))

    def new_post_ids(self, post_ids: list[int]) -> set[int]:
        if not post_ids:
            return set()
        marks = ",".join("?" * len(post_ids))
        seen = {r[0] for r in self.db.execute(f"SELECT post_id FROM posts WHERE post_id IN ({marks})", post_ids)}
        return set(post_ids) - seen

    def add_posts(self, post_ids: list[int], thread_id: int) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO posts (post_id, thread_id) VALUES (?, ?)", [(p, thread_id) for p in post_ids]
        )

    # ---- feeds

    def add_feed(self, url: str, kind: str, interval_s: float) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO feeds (url, kind, interval_s, next_due) VALUES (?, ?, ?, ?)",
            (url, kind, interval_s, self.clock()),
        )
        if not cur.rowcount:  # known feed: reactivate it if the sitemap lists it again
            self.db.execute("UPDATE feeds SET active = 1 WHERE url = ?", (url,))
        return bool(cur.rowcount)

    def deactivate_node_feeds_except(self, urls: set[str]) -> int:
        """Nodes gone from the sitemap stop being polled (they keep their history)."""
        rows = self.db.execute("SELECT url FROM feeds WHERE kind = 'node' AND active = 1").fetchall()
        gone = [(u,) for (u,) in rows if u not in urls]
        self.db.executemany("UPDATE feeds SET active = 0 WHERE url = ?", gone)
        return len(gone)

    def next_feed(self, global_only: bool = False) -> Feed | None:
        row = self.db.execute(
            "SELECT url, kind, interval_s, next_due, etag, last_modified, failures FROM feeds"
            " WHERE active = 1" + (" AND kind = 'global'" if global_only else "") + " ORDER BY next_due, rowid LIMIT 1"
        ).fetchone()
        return Feed(*row) if row else None

    def feed_polled(self, feed: Feed, status: int | None, *, interval_s: float, next_due: float,
                    title: str | None = None, etag: str | None = None, last_modified: str | None = None,
                    failures: int = 0, new_items: int = 0) -> None:
        self.db.execute(
            "UPDATE feeds SET interval_s = ?, next_due = ?, last_polled = ?, last_status = ?,"
            " title = COALESCE(?, title), etag = ?, last_modified = ?, failures = ?,"
            " polls = polls + 1, new_items = new_items + ? WHERE url = ?",
            (interval_s, next_due, utc_now(), status, title, etag, last_modified, failures, new_items, feed.url),
        )

    # ---- items

    def upsert_item(self, item) -> bool:
        """Returns True if the thread is new."""
        now = utc_now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO items (thread_id, title, text, truncated, forum, forum_path, pub_date,"
            " replies, author_hash, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.thread_id, item.title, item.text, int(item.truncated), item.forum, item.forum_path,
             item.pub_date, item.replies, item.author_hash, now, now),
        )
        if cur.rowcount:
            return True
        self.db.execute(
            "UPDATE items SET replies = COALESCE(?, replies), last_seen = ? WHERE thread_id = ?",
            (item.replies, now, item.thread_id),
        )
        return False

    # ---- sitemap index

    def add_threads(self, rows: list[tuple[int, str, str | None]]) -> None:
        self.db.executemany(
            "INSERT INTO threads (thread_id, url, lastmod) VALUES (?, ?, ?)"
            " ON CONFLICT(thread_id) DO UPDATE SET url = excluded.url, lastmod = excluded.lastmod",
            rows,
        )

    def add_tags(self, rows: list[tuple[str, str | None]]) -> None:
        self.db.executemany(
            "INSERT INTO tags (url, lastmod) VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET lastmod = excluded.lastmod",
            rows,
        )

    # ---- misc

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def unpushed(self, files: list[str]) -> list[str]:
        done = {r[0] for r in self.db.execute("SELECT file FROM pushed")}
        return [f for f in files if f not in done]

    def mark_pushed(self, files: list[str]) -> None:
        now = utc_now()
        self.db.executemany("INSERT OR REPLACE INTO pushed (file, pushed_at) VALUES (?, ?)", [(f, now) for f in files])

    def node_paths(self) -> list[str]:
        """Forum paths of active node feeds, in discovery order."""
        rows = self.db.execute("SELECT url FROM feeds WHERE kind = 'node' AND active = 1 ORDER BY rowid")
        return [urlparse(u).path.removesuffix("index.rss") for (u,) in rows]

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        pages = dict(self.db.execute("SELECT kind || ':' || status, COUNT(*) FROM pages GROUP BY 1").fetchall())
        return {
            "posts": q("SELECT COUNT(*) FROM posts"),
            "pages": pages,
            "crawl_blocked_until": self.get_meta("crawl_blocked_until"),
            "rss_items": q("SELECT COUNT(*) FROM items"),
            "rss_items_today": q("SELECT COUNT(*) FROM items WHERE first_seen >= date('now')"),
            "feeds_active": q("SELECT COUNT(*) FROM feeds WHERE active = 1"),
            "feeds_failing": q("SELECT COUNT(*) FROM feeds WHERE active = 1 AND failures > 0"),
            "polls": q("SELECT COALESCE(SUM(polls), 0) FROM feeds"),
            "threads_indexed": q("SELECT COUNT(*) FROM threads"),
            "tags_indexed": q("SELECT COUNT(*) FROM tags"),
            "sitemap_refreshed_at": self.get_meta("sitemap_refreshed_at"),
            "files_pushed": q("SELECT COUNT(*) FROM pushed"),
        }

    def commit(self) -> None:
        self.db.commit()
