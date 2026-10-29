"""Crawl order, recrawl, Cloudflare fallback to RSS, and the daemon's choice of work (no network)."""

import json
from pathlib import Path

import pytest

from beltounsi.collector import Collector
from beltounsi.config import Config
from beltounsi.crawler import recent
from beltounsi.http import Response
from beltounsi.store import Store

BASE = "https://www.tunisia-sat.com"
FORUM = f"{BASE}/forums/forums/91/"
FEED_XML = (Path(__file__).parent / "fixtures" / "feed_synthetic.xml").read_bytes()
CHALLENGE = Response(403, b"<html><title>Just a moment...</title></html>", {"cf-mitigated": "challenge"})


def listing_html(rows, next_page=None):
    """rows: (thread_id, last_page, latest_date, sticky)."""

    def row(tid, last, date):
        jump = "".join(f'<a href="/forums/threads/{tid}/page-{n}">{n}</a>' for n in range(2, last + 1))
        return (
            f'<div class="structItem structItem--thread js-threadListItem-{tid}">'
            f'<div class="structItem-title"><a href="/forums/threads/{tid}/">t</a></div>'
            f'<span class="structItem-pageJump">{jump}</span>'
            f'<time class="structItem-latestDate u-dt" datetime="{date}T12:00:00+0100">d</time></div>'
        )

    sticky = "".join(row(t, n, d) for t, n, d, s in rows if s)
    normal = "".join(row(t, n, d) for t, n, d, s in rows if not s)
    nxt = f'<a href="{next_page}" class="pageNav-jump pageNav-jump--next">n</a>' if next_page else ""
    return (
        f'<html><body><div class="structItemContainer-group structItemContainer-group--sticky">{sticky}</div>'
        f'<div class="structItemContainer-group js-threadList">{normal}</div>{nxt}</body></html>'
    )


def thread_html(tid, page, dates):
    posts = "".join(
        f'<article class="message message--post" data-content="post-{tid * 100 + page * 10 + i}">'
        f'<time class="u-dt" datetime="{d}T10:00:00+0100">d</time>'
        f'<div class="message-body"><div class="bbWrapper">post {i} 98 123 456</div></div></article>'
        for i, d in enumerate(dates)
    )
    prev = ""
    if page > 1:
        href = f"/forums/threads/{tid}/" + (f"page-{page - 1}" if page > 2 else "")
        prev = f'<a href="{href}" class="pageNav-jump pageNav-jump--prev">p</a>'
    crumbs = '<ul class="p-breadcrumbs"><li><a href="/forums/forums/91/">general</a></li></ul>'
    return f'<html><body>{crumbs}<h1 class="p-title-value">t{tid}</h1>{posts}{prev}</body></html>'


def site():
    return {
        FORUM: listing_html(
            [(1, 1, "2020-01-01", True), (2, 3, "2026-10-28", False), (3, 1, "2025-05-01", False)],
            next_page=f"{FORUM}page-2",
        ),
        f"{FORUM}page-2": listing_html(
            [(4, 1, "2025-06-01", False), (5, 1, "2023-01-01", False)], next_page=f"{FORUM}page-3"
        ),
        f"{FORUM}page-3": listing_html([(6, 1, "2022-01-01", False)]),
        # Thread 2: page 3 is new, page 2 crosses the cutoff, page 1 is old and must not be fetched.
        f"{BASE}/forums/threads/2/page-3": thread_html(2, 3, ["2026-10-01", "2026-10-28"]),
        f"{BASE}/forums/threads/2/page-2": thread_html(2, 2, ["2024-12-30", "2025-02-01"]),
        f"{BASE}/forums/threads/2/": thread_html(2, 1, ["2015-01-01"]),
        f"{BASE}/forums/threads/3/": thread_html(3, 1, ["2025-05-01"]),
        f"{BASE}/forums/threads/4/": thread_html(4, 1, ["2025-06-01"]),
        f"{BASE}/forums/forums/-/index.rss": FEED_XML,
        f"{BASE}/forums/forums/91/index.rss": FEED_XML,
    }


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []
        self.challenge = False

    def get(self, url, etag=None, last_modified=None):
        self.urls.append(url)
        if self.challenge and not url.endswith(".rss"):
            return CHALLENGE
        body = self.pages.get(url)
        if body is None:
            return Response(404)
        return Response(200, body.encode() if isinstance(body, str) else body)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    http = FakeHttp(site())
    cfg = Config(sections=["/forums/forums/91/"], crawl_all_nodes=False, exclude_paths=[],
                 min_post_date="2025-01-01", data_dir=str(tmp_path))
    store = Store(cfg.db_path, clock=clock)
    store.set_meta("sitemap_attempted_at", str(clock.t))  # no sitemap in these tests
    store.set_meta("sitemap_refreshed_at", "x")
    c = Collector(cfg, http=http, store=store, clock=clock, sleep=lambda s: None)
    store.add_feed(f"{FORUM}index.rss", "node", 3600)
    store.commit()
    return c, http, clock, cfg


def crawl_all(c):
    c.crawler.seed()
    while c.crawler.step() not in ("idle", "blocked"):
        pass


def posts(cfg):
    return [json.loads(line) for f in sorted(cfg.posts_dir.glob("*.jsonl")) for line in f.open(encoding="utf-8")]


def test_newest_first_walks_back_and_stops_at_cutoff(env):
    c, http, _, cfg = env
    crawl_all(c)
    urls = http.urls
    assert f"{BASE}/forums/threads/2/page-3" in urls and f"{BASE}/forums/threads/2/page-2" in urls
    assert f"{BASE}/forums/threads/2/" not in urls  # older than the cutoff
    assert f"{BASE}/forums/threads/1/" not in urls  # old sticky thread
    assert f"{BASE}/forums/threads/5/" not in urls  # last active before the cutoff
    assert f"{FORUM}page-3" not in urls  # page 2 already crossed the cutoff
    got = posts(cfg)
    assert len(got) == 5 and all(p["post_date"][:10] >= "2025-01-01" for p in got)
    assert all("<PHONE>" in p["text"] and p["pii_masked"] == 1 for p in got)
    assert {p["source"] for p in got} == {"tunisia-sat"}


def test_recrawl_requeues_only_threads_with_new_posts(env):
    c, http, clock, cfg = env
    crawl_all(c)
    http.urls.clear()
    http.pages[FORUM] = listing_html([(2, 3, "2026-10-29", False), (3, 1, "2025-05-01", False)])
    http.pages[f"{BASE}/forums/threads/2/page-3"] = thread_html(2, 3, ["2026-10-01", "2026-10-28", "2026-10-29"])
    clock.t += cfg.recrawl_hours * 3600
    assert c.crawler.seed_due()
    crawl_all(c)
    assert f"{BASE}/forums/threads/2/page-3" in http.urls  # new reply: re-read
    assert f"{BASE}/forums/threads/3/" not in http.urls  # unchanged: not fetched again
    assert len(posts(cfg)) == 6  # one new post, no duplicates


def test_challenge_pauses_crawl_and_falls_back_to_all_feeds(env):
    c, http, clock, cfg = env
    c.crawler.seed()
    # Crawl healthy: only the site-wide feed is polled, node feeds wait.
    for _ in range(6):
        c.tick()
    assert f"{FORUM}index.rss" not in http.urls
    assert f"{BASE}/forums/forums/-/index.rss" in http.urls

    http.challenge = True
    http.urls.clear()
    for _ in range(4):
        c.tick()
    assert c.crawler.blocked()
    assert f"{FORUM}index.rss" in http.urls  # fallback: every forum feed
    page_fetches = [u for u in http.urls if not u.endswith(".rss")]
    assert len(page_fetches) == 1  # one challenged request, then no more page requests while blocked
    assert any(json.loads(line)["source"] == "tunisia-sat-rss"
               for f in cfg.items_dir.glob("*.jsonl") for line in f.open(encoding="utf-8"))

    # Cooldown over and the site lets us through again: the crawl resumes.
    http.challenge = False
    clock.t = c.crawler.blocked_until() + 1
    http.urls.clear()
    for _ in range(3):
        c.tick()
    assert any(not u.endswith(".rss") for u in http.urls)


def test_block_cooldown_doubles(env):
    c, http, clock, cfg = env
    http.challenge = True
    c.crawler.seed()
    c.crawler.step()
    first = c.crawler.blocked_until() - clock.t
    clock.t = c.crawler.blocked_until() + 1
    c.crawler.step()
    assert c.crawler.blocked_until() - clock.t == 2 * first == 2 * cfg.blocked_cooldown_minutes * 60


def test_rss_items_are_deduplicated(env):
    c, http, clock, cfg = env
    feed = c.store.next_feed(global_only=True)
    assert c.poll(feed) == 2
    clock.t += 10_000
    assert c.poll(c.store.next_feed(global_only=True)) == 0
    lines = [line for f in cfg.items_dir.glob("*.jsonl") for line in f.open(encoding="utf-8")]
    assert len(lines) == 2 and "Fake" not in "".join(lines)


@pytest.mark.parametrize(
    "date,ok", [("2025-01-01T00:00:00+0100", True), ("2024-12-31T23:59:00+0100", False), (None, True)]
)
def test_recent(date, ok):
    assert recent(date, "2025-01-01") is ok
