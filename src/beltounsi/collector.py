"""The 24/7 loop: full crawl when the site lets plain HTTP through, RSS always, sitemap index weekly,
daily push to Hugging Face. All requests share one pacer (Http), so the site sees one polite client.

While the crawl is working, only the site-wide feed is polled (the crawl reads everything else).
While it is blocked or has nothing to do, every forum feed is polled on its adaptive schedule.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .crawler import Crawler
from .feeds import GLOBAL_FEED, parse_feed
from .http import Http
from .records import append_day, rss_record
from .sitemap import parse_sitemap, sitemap_files
from .store import Feed, Store, utc_now

log = logging.getLogger("beltounsi")

MAX_COOLDOWN = 3600
MAX_FEED_BACKOFF = 6 * 3600


def event(name: str, **fields) -> None:
    log.info(json.dumps({"event": name, **fields}, ensure_ascii=False))


class Collector:
    def __init__(self, cfg: Config, http: Http | None = None, store: Store | None = None,
                 clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.http = http or Http(cfg)
        self.store = store or Store(cfg.db_path, clock=clock)
        self.clock = clock
        self.sleep = sleep
        self.crawler = Crawler(cfg, self.http, self.store, clock=clock)
        self.block_streak = 0
        self.store.add_feed(cfg.url(GLOBAL_FEED), "global", cfg.global_feed_minutes * 60)
        self.store.commit()

    def close(self) -> None:
        self.store.close()

    # ---- sitemap index

    def index_due(self) -> bool:
        last = float(self.store.get_meta("sitemap_attempted_at") or 0)
        done = self.store.get_meta("sitemap_refreshed_at")
        wait = self.cfg.sitemap_refresh_hours * 3600 if done else 3600  # retry hourly until one succeeds
        return self.clock() - last >= wait

    def refresh_index(self) -> dict:
        """Read every sitemap file: register a feed per forum node, and index all threads and tags."""
        self.store.set_meta("sitemap_attempted_at", str(self.clock()))
        self.store.commit()
        resp = self.http.get(self.cfg.url("/forums/sitemap.php"))
        if resp.status != 200:
            event("sitemap_failed", url="/forums/sitemap.php", status=resp.status)
            return {}
        files = sitemap_files(resp.body.decode("utf-8", "replace"))
        node_feeds: set[str] = set()
        counts = {"files": len(files), "files_failed": 0, "nodes": 0, "threads": 0, "tags": 0, "new_feeds": 0}
        for url in files:
            r = self.http.get(url)
            if r.status != 200:
                counts["files_failed"] += 1
                event("sitemap_failed", url=url, status=r.status)
                continue
            page = parse_sitemap(r.body.decode("utf-8", "replace"))
            for path, _ in page.nodes:
                if self.cfg.excluded(path):
                    continue
                feed_url = self.cfg.url(path) + "index.rss"
                node_feeds.add(feed_url)
                counts["new_feeds"] += self.store.add_feed(feed_url, "node", self.cfg.node_min_minutes * 4 * 60)
            self.store.add_threads(page.threads)
            self.store.add_tags(page.tags)
            counts["nodes"] += len(page.nodes)
            counts["threads"] += len(page.threads)
            counts["tags"] += len(page.tags)
            self.store.commit()
        if files and not counts["files_failed"]:
            counts["deactivated"] = self.store.deactivate_node_feeds_except(node_feeds)
            self.store.set_meta("sitemap_refreshed_at", utc_now())
        self.store.commit()
        event("sitemap_refreshed", **counts)
        return counts

    # ---- RSS

    def _next_interval(self, feed: Feed, new: int) -> float:
        if feed.kind == "global":
            return self.cfg.global_feed_minutes * 60
        lo, hi = self.cfg.node_min_minutes * 60, self.cfg.node_max_minutes * 60
        if new >= 10:  # half the feed is new: we may be missing threads, poll faster
            interval = feed.interval_s / 2
        elif new == 0:
            interval = feed.interval_s * 1.5
        else:
            interval = feed.interval_s
        return min(hi, max(lo, interval))

    def poll(self, feed: Feed) -> int:
        """Poll one feed. Returns the number of new threads."""
        now = self.clock()
        try:
            resp = self.http.get(feed.url, feed.etag, feed.last_modified)
        except PermissionError:
            self.store.db.execute("UPDATE feeds SET active = 0 WHERE url = ?", (feed.url,))
            self.store.commit()
            event("feed_disallowed", url=feed.url)
            return 0

        if resp.status == 304:
            interval = self._next_interval(feed, 0)
            self.store.feed_polled(feed, 304, interval_s=interval, next_due=now + interval,
                                   etag=feed.etag, last_modified=feed.last_modified)
            self.store.commit()
            self.block_streak = 0
            return 0
        if resp.status == 404:
            self.store.feed_polled(feed, 404, interval_s=feed.interval_s, next_due=now + MAX_FEED_BACKOFF)
            self.store.db.execute("UPDATE feeds SET active = 0 WHERE url = ?", (feed.url,))
            self.store.commit()
            event("feed_gone", url=feed.url)
            return 0

        title, items, error = None, [], None
        if resp.status == 200 and not resp.challenged:
            try:
                title, items = parse_feed(resp.body, self.cfg.author_salt)
            except ET.ParseError as e:
                error = f"not a feed: {e}"
        else:
            error = "challenged" if resp.challenged else f"HTTP {resp.status}"

        if error:
            failures = feed.failures + 1
            delay = min(MAX_FEED_BACKOFF, 60 * 2 ** failures)
            self.store.feed_polled(feed, resp.status, interval_s=feed.interval_s, next_due=now + delay,
                                   etag=feed.etag, last_modified=feed.last_modified, failures=failures)
            self.store.commit()
            event("feed_failed", url=feed.url, error=error, failures=failures, retry_in=delay)
            if resp.challenged or resp.status is None or resp.status >= 500:
                self.block_streak += 1
                cooldown = min(MAX_COOLDOWN, 300 * 2 ** (self.block_streak - 1))
                event("cooldown", seconds=cooldown, streak=self.block_streak)
                self.sleep(cooldown)
            return 0

        self.block_streak = 0
        new_items = [it for it in items if self.store.upsert_item(it)]
        now_iso = utc_now()
        append_day(self.cfg.items_dir, [rss_record(it, now_iso) for it in new_items], now_iso)
        interval = self._next_interval(feed, len(new_items))
        self.store.feed_polled(feed, 200, interval_s=interval, next_due=now + interval, title=title,
                               etag=resp.etag, last_modified=resp.last_modified, new_items=len(new_items))
        self.store.commit()
        event("feed", url=feed.url, items=len(items), new=len(new_items), next_in=round(interval))
        return len(new_items)

    # ---- Hugging Face

    def push_due(self) -> bool:
        if not self.cfg.hf_repo_id:
            return False
        last = float(self.store.get_meta("hf_pushed_at") or 0)
        return self.clock() - last >= self.cfg.hf_push_hours * 3600

    def push(self) -> int:
        """Upload finished days (UTC) of crawl posts and RSS items. Today's files are still growing, so they wait."""
        from .hub import push_files  # needs the [hf] extra

        self.store.set_meta("hf_pushed_at", str(self.clock()))
        self.store.commit()
        today = datetime.now(timezone.utc).date().isoformat()
        pairs = []
        for directory, prefix in self.cfg.hf_targets:
            files = sorted(str(p) for p in Path(directory).glob("*.jsonl") if p.stem < today)
            pairs += [(f, f"{prefix}/{Path(f).name}") for f in self.store.unpushed(files)]
        if not pairs:
            return 0
        try:
            push_files(self.cfg.hf_repo_id, pairs, message=f"Tunisia-Sat collector: {len(pairs)} day file(s)")
        except Exception as e:  # network or auth: retry at the next push slot
            event("hf_push_failed", error=repr(e)[:300])
            return 0
        self.store.mark_pushed([f for f, _ in pairs])
        self.store.commit()
        event("hf_pushed", files=len(pairs), repo=self.cfg.hf_repo_id)
        return len(pairs)

    # ---- loop

    def tick(self) -> float:
        """Do one unit of work. Returns seconds to wait before the next tick (0 = go on)."""
        if self.index_due():
            self.refresh_index()
            return 0
        if self.push_due():
            self.push()
        crawling = self.cfg.crawl and not self.crawler.blocked()
        if crawling and self.crawler.seed_due():
            self.crawler.seed()
        crawl_has_work = crawling and self.store.next_page() is not None
        now = self.clock()
        feed = self.store.next_feed(global_only=crawl_has_work)
        if feed and feed.next_due <= now:
            self.poll(feed)
            return 0
        if crawl_has_work:
            self.crawler.step()
            return 0
        waits = [60.0]
        if feed:
            waits.append(feed.next_due - now)
        if self.cfg.crawl and self.crawler.blocked():
            waits.append(self.crawler.blocked_until() - now)
        return max(1.0, min(waits))

    def run(self, stop: threading.Event) -> None:
        self.sleep = stop.wait  # cooldowns end early on shutdown
        event("start", crawl=self.cfg.crawl, min_post_date=self.cfg.min_post_date, **self.store.stats())
        while not stop.is_set():
            wait = self.tick()
            if wait:
                stop.wait(wait)
        event("stop")
