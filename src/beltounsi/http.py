"""Polite HTTP: Python's standard client, an honest User-Agent, a fixed minimum delay, robots.txt,
gzip and conditional GET. Nothing here tries to look like a browser or to get past a bot check:
a Cloudflare challenge is reported as `challenged` and the caller backs off.
"""

from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field

from .config import Config

ROBOTS_TTL = 24 * 3600
CHALLENGE_MARKERS = (b"challenge-platform", b"cf-chl-", b"challenges.cloudflare.com", b"cf_chl_opt")
CHALLENGE_TITLES = (b"<title>just a moment", b"<title>attention required", b"<title>un instant")


@dataclass
class Response:
    status: int | None  # None: network error
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    @property
    def challenged(self) -> bool:
        """A Cloudflare bot check (or rate limit) instead of the page."""
        if self.status == 429 or self.headers.get("cf-mitigated") == "challenge":
            return True
        if self.status not in (200, 403, 503):
            return False
        head = self.body[:20000]
        if b"message--post" in head or b"structItem--thread" in head or b"<rss" in head[:500]:
            return False
        low = head.lower()
        return any(m in head for m in CHALLENGE_MARKERS) or any(t in low for t in CHALLENGE_TITLES)


class Http:
    def __init__(self, cfg: Config, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self.sleep = sleep
        self.clock = clock
        self.last = float("-inf")
        self.robots: urllib.robotparser.RobotFileParser | None = None
        self.robots_at = float("-inf")

    def _pace(self) -> None:
        wait = self.last + self.cfg.min_delay_seconds - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.last = self.clock()

    def _open(self, url: str, headers: dict[str, str]) -> Response:
        self._pace()
        req = urllib.request.Request(
            url, headers={"User-Agent": self.cfg.user_agent, "Accept-Encoding": "gzip", **headers}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return _response(r.status, r.read(), r.headers)
        except urllib.error.HTTPError as e:  # 304 and errors land here; keep the body to spot challenges
            try:
                body = e.read()
            except OSError:
                body = b""
            return _response(e.code, body, e.headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return Response(None)

    def allowed(self, url: str) -> bool:
        if self.robots is None or self.clock() - self.robots_at > ROBOTS_TTL:
            resp = self._open(self.cfg.url("/robots.txt"), {})
            if resp.status != 200:
                return self.robots is not None and self.robots.can_fetch(self.cfg.user_agent, url)
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(resp.body.decode("utf-8", "replace").splitlines())
            self.robots, self.robots_at = rp, self.clock()
        return self.robots.can_fetch(self.cfg.user_agent, url)

    def get(self, url: str, etag: str | None = None, last_modified: str | None = None) -> Response:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return self._open(url, headers)


def _response(status: int, body: bytes, headers) -> Response:
    h = {k.lower(): v for k, v in (headers.items() if headers else [])}
    if h.get("content-encoding") == "gzip" and body:
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    return Response(status, body, h)
