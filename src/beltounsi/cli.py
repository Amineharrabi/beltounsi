"""beltounsi-collect: run | discover | crawl | poll | push | import-seen | stats"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from .collector import Collector
from .config import Config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="beltounsi-collect", description="Tunisia-Sat collector: crawl + RSS")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="collect forever (the systemd service runs this)")
    sub.add_parser("discover", help="read the sitemaps once: forum feeds, thread and tag index")
    p = sub.add_parser("crawl", help="crawl N pages now, then exit (a quick check from a new server)")
    p.add_argument("-n", type=int, default=10)
    p = sub.add_parser("poll", help="poll the N most overdue RSS feeds now, then exit")
    p.add_argument("-n", type=int, default=5)
    sub.add_parser("push", help="upload finished day files to Hugging Face now")
    p = sub.add_parser("import-seen", help="mark posts already collected elsewhere as seen (no duplicates)")
    p.add_argument("files", nargs="*", type=Path, help="JSONL files with a post_id field")
    p.add_argument("--hf", action="store_true", help="also read data/tunisia_sat/*.jsonl from hf_repo_id")
    sub.add_parser("stats", help="print counts")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    cfg = Config.load(args.config)
    c = Collector(cfg)
    try:
        if args.cmd == "run":
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            c.run(stop)
        elif args.cmd == "discover":
            c.refresh_index()
        elif args.cmd == "crawl":
            if c.crawler.seed_due():
                c.crawler.seed()
            for _ in range(args.n):
                if c.crawler.step() in ("idle", "blocked"):
                    break
        elif args.cmd == "poll":
            for _ in range(args.n):
                feed = c.store.next_feed()
                if feed is None:
                    break
                c.poll(feed)
        elif args.cmd == "push":
            if not cfg.hf_repo_id:
                ap.error("set hf_repo_id in the config first")
            print(f"pushed {c.push()} file(s)")
        elif args.cmd == "import-seen":
            files = list(args.files)
            if args.hf:
                from .hub import download_prefix

                files += download_prefix(cfg.hf_repo_id, "data/tunisia_sat/")
            print(f"marked {import_seen(c, files)} post ids as seen")
        if args.cmd != "run":
            print(json.dumps(c.store.stats(), ensure_ascii=False, indent=2))
    finally:
        c.close()
    return 0


def import_seen(c: Collector, files: list[Path]) -> int:
    n = 0
    for f in files:
        by_thread: dict[int, list[int]] = {}
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if "post_id" in r:
                    by_thread.setdefault(r["thread_id"], []).append(r["post_id"])
        for thread_id, ids in by_thread.items():
            c.store.add_posts(ids, thread_id)
            n += len(ids)
        c.store.commit()
    return n


if __name__ == "__main__":
    sys.exit(main())
