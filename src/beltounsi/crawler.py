"""Full crawl over plain HTTP, newest first.

Each forum listing is read from page 1 (sorted by last post) until its threads were last active
before `min_post_date`; each thread is read from its last page backwards until that date. Every
`recrawl_hours` the listings are read again and only threads with newer posts are re-queued.

A Cloudflare challenge pauses the crawl (30 min, doubling up to 12 h). The collector polls every
RSS feed meanwhile, so new threads are still captured. Nothing tries to get past the challenge.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

from .config import Config
from .forum import parse_listing, parse_thread, thread_url
from .http import Http
from .records import append_day, crawl_record
from .store import Store, utc_now

log = logging.getLogger("beltounsi")

MAX_BLOCK_COOLDOWN = 12 * 3600


def event(name: str, **fields) -> None:
    log.info(json.dumps({"event": name, **fields}, ensure_ascii=False))


def recent(date: str | None, cutoff: str | None) -> bool:
    """True if `date` is on or after `cutoff`. Undated items count as recent. ISO dates compare as strings."""
    return not cutoff or not date or date[:10] >= cutoff


class Crawler:
    def __init__(self, cfg: Config, http: Http, store: Store, clock=time.time):
        self.cfg = cfg
        self.http = http
        self.store = store
        self.clock = clock

    # ---- scheduling

    def blocked_until(self) -> float:
        return float(self.store.get_meta("crawl_blocked_until") or 0)

    def blocked(self) -> bool:
        return self.clock() < self.blocked_until()

    def seed_due(self) -> bool:
        if self.store.next_page() is not None:
            return False
        last = float(self.store.get_meta("crawl_seeded_at") or 0)
        return self.clock() - last >= self.cfg.recrawl_hours * 3600

    def seed(self) -> int:
        """(Re)start from page 1 of every section: configured ones first, then all other forums."""
        self.store.forget_listings()
        paths = [p for p in self.cfg.sections if not self.cfg.excluded(p)]
        if self.cfg.crawl_all_nodes:
            paths += [p for p in self.store.node_paths() if p not in paths and not self.cfg.excluded(p)]
        for prio, path in enumerate(paths):
            self.store.enqueue(self.cfg.url(path), "listing", prio, force=True)
        self.store.set_meta("crawl_seeded_at", str(self.clock()))
        self.store.commit()
        event("crawl_seeded", sections=len(paths))
        return len(paths)

    def _block(self, url: str, status: int | None) -> None:
        streak = int(self.store.get_meta("crawl_block_streak") or 0) + 1
        cooldown = min(MAX_BLOCK_COOLDOWN, self.cfg.blocked_cooldown_minutes * 60 * 2 ** (streak - 1))
        self.store.set_meta("crawl_block_streak", str(streak))
        self.store.set_meta("crawl_blocked_until", str(self.clock() + cooldown))
        self.store.commit()
        event("crawl_blocked", url=url, status=status, streak=streak, retry_in=round(cooldown), fallback="rss")

    # ---- one page

    def step(self) -> str:
        """Fetch and process the next queued page. Returns 'idle', 'ok', 'error' or 'blocked'."""
        nxt = self.store.next_page()
        if nxt is None:
            return "idle"
        url, kind, prio = nxt
        if self.cfg.excluded(urlparse(url).path):
            self.store.mark(url, "skipped")
            self.store.commit()
            return "ok"
        try:
            resp = self.http.get(url)
        except PermissionError:
            self.store.mark(url, "skipped")
            self.store.commit()
            return "ok"

        if resp.challenged:
            self._block(url, resp.status)
            return "blocked"
        if resp.status == 404:
            self.store.mark(url, "skipped", 404)
            self.store.commit()
            return "ok"
        if resp.status != 200 or not resp.body:
            self.store.record_failure(url, resp.status)
            self.store.commit()
            event("crawl_failed", url=url, status=resp.status)
            return "error"
        self.store.set_meta("crawl_block_streak", "0")

        html = resp.body.decode("utf-8", "replace")
        if kind == "listing":
            self._listing(url, html, prio)
        else:
            self._thread(url, html, prio)
        self.store.commit()
        return "ok"

    def _listing(self, url: str, html: str, prio: int) -> None:
        cutoff = self.cfg.min_post_date
        listing = parse_listing(html, url)
        queued = 0
        for t in listing.threads:
            if not recent(t.latest_date, cutoff):
                continue
            seen, changed = self.store.thread_changed(t.thread_id, t.latest_date)
            if changed:  # start at the last page; posts already collected are skipped by id
                self.store.enqueue(thread_url(t.thread_id, t.last_page), "thread", prio, force=seen)
                queued += 1
        for sf in listing.subforum_urls:
            if not self.cfg.excluded(urlparse(sf).path):
                self.store.enqueue(sf, "listing", prio)
        # Listings are sorted by last post, newest first: once an unpinned thread is older than the
        # cutoff, every later page is older too.
        unpinned = [t for t in listing.threads if not t.sticky]
        more = bool(unpinned) and all(recent(t.latest_date, cutoff) for t in unpinned)
        if listing.next_url and more:
            self.store.enqueue(listing.next_url, "listing", prio)
        self.store.mark(url, "done", 200, 0)
        event("listing", url=url, threads=len(listing.threads), queued=queued, next=bool(listing.next_url and more))

    def _thread(self, url: str, html: str, prio: int) -> None:
        cutoff = self.cfg.min_post_date
        page = parse_thread(html, url, self.cfg.author_salt or None)
        if page.section_path and self.cfg.excluded(page.section_path):
            self.store.mark(url, "skipped", 200, 0)
            return
        old = [p for p in page.posts if not recent(p.post_date, cutoff)]
        keep = [p for p in page.posts if recent(p.post_date, cutoff)]
        new_ids = self.store.new_post_ids([p.post_id for p in keep])
        now = utc_now()
        records = [
            crawl_record(text=p.text, post_id=p.post_id, thread_id=page.thread_id, page=page.page,
                         thread_title=page.title, forum=page.section_name, forum_path=page.section_path,
                         post_date=p.post_date, author_hash=p.author_hash, collected_at=now)
            for p in keep if p.post_id in new_ids
        ]
        append_day(self.cfg.posts_dir, records, now)
        self.store.add_posts([r["post_id"] for r in records], page.thread_id)
        if page.prev_url and not old:  # walk back until the cutoff
            self.store.enqueue(page.prev_url, "thread", prio)
        self.store.mark(url, "done", 200, len(records))
        event("thread", url=url, posts=len(page.posts), new=len(records), old=len(old), forum=page.section_name)
