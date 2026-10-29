"""Settings, read from a flat TOML file (see config.example.toml)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

# Chat/discussion forums first; satellite/tech sections are mostly MSA or technical text.
DEFAULT_SECTIONS = [
    "/forums/forums/91/",  # General
    "/forums/forums/89/",  # Family life
    "/forums/forums/409/",  # Advice
    "/forums/forums/85/",  # Hobbies
    "/forums/sports/",  # Sports
    "/forums/forums/313/",  # ISPs / telecom
]


@dataclass
class Config:
    base_url: str = "https://www.tunisia-sat.com"
    user_agent: str = "BelTounsi/0.1 (Tunisian Arabic dataset research; collected with the site admin's permission)"
    data_dir: str = "data"
    min_delay_seconds: float = 5.0  # between any two requests to the site (crawl and RSS share it)

    # Full crawl (plain HTTP). When the site answers with a Cloudflare challenge, the crawl pauses
    # and every RSS feed is polled instead until the next retry.
    crawl: bool = True
    sections: list[str] = field(default_factory=lambda: list(DEFAULT_SECTIONS))
    crawl_all_nodes: bool = True  # after `sections`, every other forum from the sitemap
    exclude_paths: list[str] = field(default_factory=lambda: ["/forums/forums/66/"])  # English & French
    min_post_date: str = "2025-01-01"  # newest first, back to this day; older posts are not kept
    recrawl_hours: float = 6.0  # re-read listings from page 1 to pick up new activity
    blocked_cooldown_minutes: float = 30.0  # first pause after a challenge; doubles up to 12 h

    # RSS
    global_feed_minutes: float = 5.0  # the site-wide feed of the 20 newest threads
    node_min_minutes: float = 15.0  # busiest forum feeds are polled this often...
    node_max_minutes: float = 240.0  # ...and the quietest at least this often
    sitemap_refresh_hours: float = 168.0  # rebuild the node/thread/tag index weekly

    author_salt: str = ""  # empty: authors are dropped; set: a salted hash is kept

    # Hugging Face (token from the HF_TOKEN environment variable)
    hf_repo_id: str = ""  # e.g. "you/BelTounsi"; empty: never push
    hf_push_hours: float = 24.0

    @classmethod
    def load(cls, path: str | Path) -> Config:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        cfg = cls(**data)
        if cfg.min_delay_seconds < 4.0:
            raise ValueError("min_delay_seconds must be >= 4 (politeness floor)")
        return cfg

    def url(self, path: str) -> str:
        return path if path.startswith("http") else self.base_url.rstrip("/") + path

    def excluded(self, path: str) -> bool:
        return any(path.startswith(x) for x in self.exclude_paths)

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "state.sqlite"

    @property
    def posts_dir(self) -> Path:
        return Path(self.data_dir) / "posts"  # full posts from the crawl, one file per UTC day

    @property
    def items_dir(self) -> Path:
        return Path(self.data_dir) / "rss"  # RSS first-post snippets, one file per UTC day

    @property
    def hf_targets(self) -> list[tuple[Path, str]]:
        return [(self.posts_dir, "data/tunisia_sat"), (self.items_dir, "data/tunisia_sat_rss")]
