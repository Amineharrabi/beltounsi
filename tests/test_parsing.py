from pathlib import Path

import pytest

from beltounsi.feeds import parse_feed
from beltounsi.forum import parse_listing, parse_thread
from beltounsi.hub import render_card
from beltounsi.http import Response
from beltounsi.pii import scrub
from beltounsi.sitemap import is_node, parse_sitemap, sitemap_files

FIX = Path(__file__).parent / "fixtures"


def test_feed_items():
    title, items = parse_feed((FIX / "feed_synthetic.xml").read_bytes())
    assert title == "المنتدى العام"
    first, second = items
    assert (first.thread_id, first.replies, first.forum_path) == (4528479, 22, "/forums/forums/91/")
    assert first.pub_date == "2026-10-29T10:25:55+00:00"
    assert first.truncated and not second.truncated
    assert second.thread_id == 4528400 and second.text == "3aslema ya jma3a"


def test_feed_text_has_no_usernames_quotes_or_read_more():
    _, items = parse_feed((FIX / "feed_synthetic.xml").read_bytes())
    text = items[0].text
    assert text.startswith("عسلامة @USER\n")
    assert "Fake" not in text and "quoted text" not in text and "said:" not in text
    assert "قراءة المزيد" not in text and not text.endswith("...")
    assert all(i.author_hash is None for i in items)  # no salt: authors dropped


def test_feed_author_hash_with_salt():
    _, a = parse_feed((FIX / "feed_synthetic.xml").read_bytes(), author_salt="s1")
    _, b = parse_feed((FIX / "feed_synthetic.xml").read_bytes(), author_salt="s2")
    assert a[0].author_hash and a[0].author_hash != b[0].author_hash and "Fake" not in a[0].author_hash


def test_sitemap_classification():
    xml = """<urlset>
      <url><loc>https://www.tunisia-sat.com/forums/</loc></url>
      <url><loc>https://www.tunisia-sat.com/forums/categories/3/</loc></url>
      <url><loc>https://www.tunisia-sat.com/forums/forums/91/</loc><lastmod>2026-10-29T00:00:00+00:00</lastmod></url>
      <url><loc>https://www.tunisia-sat.com/forums/sports/</loc></url>
      <url><loc>https://www.tunisia-sat.com/forums/threads/13/</loc><lastmod>2006-04-14T14:23:55+00:00</lastmod></url>
      <url><loc>https://www.tunisia-sat.com/forums/threads/some-title.14/</loc></url>
      <url><loc>https://www.tunisia-sat.com/forums/tags/masque/</loc></url>
    </urlset>"""
    page = parse_sitemap(xml)
    assert [p for p, _ in page.nodes] == ["/forums/forums/91/", "/forums/sports/"]
    assert page.threads == [
        (13, "https://www.tunisia-sat.com/forums/threads/13/", "2006-04-14T14:23:55+00:00"),
        (14, "https://www.tunisia-sat.com/forums/threads/some-title.14/", None),
    ]
    assert page.tags == [("https://www.tunisia-sat.com/forums/tags/masque/", None)]
    assert not is_node("/forums/members/x.1/")
    index = "<sitemapindex><sitemap><loc>https://x/forums/sitemap-1.xml</loc><lastmod>z</lastmod></sitemap></sitemapindex>"
    assert sitemap_files(index) == ["https://x/forums/sitemap-1.xml"]


def test_forum_pages():
    page = parse_thread((FIX / "thread_synthetic.html").read_text(encoding="utf-8"),
                        "https://www.tunisia-sat.com/forums/threads/777/")
    assert [p.post_id for p in page.posts] == [1001, 1002] and page.prev_url is None
    listing = parse_listing((FIX / "listing_synthetic.html").read_text(encoding="utf-8"),
                            "https://www.tunisia-sat.com/forums/forums/91/")
    sticky, normal = listing.threads
    assert (sticky.last_page, sticky.sticky, normal.sticky) == (7, True, False)


@pytest.mark.parametrize(
    "text,masked",
    [
        ("كلموني 98 123 456", "كلموني <PHONE>"),
        ("call +216 98123456 now", "call <PHONE> now"),
        ("mail me: a.b@example.tn", "mail me: <EMAIL>"),
        ("الهاتف 71.771.300", "الهاتف <PHONE>"),
        ("00216 22 33 99 77", "<PHONE>"),
        ("في 2025 ربحنا 3-1", "في 2025 ربحنا 3-1"),  # years and scores stay
        ("الثمن 1.250.000 مليم", "الثمن 1.250.000 مليم"),  # amounts stay
        # False positives seen on real posts before the pattern was tightened:
        ("ايامات 2005-2006", "ايامات 2005-2006"),
        ("2023\n2016", "2023\n2016"),
        ("2024-05-29", "2024-05-29"),
        ("28673.892 دينار", "28673.892 دينار"),
        ("8000 1992", "8000 1992"),
    ],
)
def test_scrub(text, masked):
    assert scrub(text)[0] == masked


def test_challenge_detection():
    assert Response(403, b"<html><title>Just a moment...</title></html>").challenged
    assert Response(200, b"x", {"cf-mitigated": "challenge"}).challenged
    assert Response(429).challenged
    assert not Response(200, b'<article class="message--post">\xd9\x84\xd8\xad\xd8\xb8\xd8\xa9</article>').challenged
    assert not Response(404, b"<title>Just a moment</title>").challenged
    assert not Response(200, b'<?xml version="1.0"?><rss version="2.0"></rss>').challenged


def test_card_lists_only_present_configs():
    card = render_card({"tunisia_sat", "tarc"})
    yaml = card.split("---")[1]
    assert "config_name: tunisia_sat" in yaml and "config_name: tarc" in yaml
    assert "config_name: tunizi" not in yaml
    assert "path: data/tunisia_sat/*.jsonl" in yaml and "path: data/tarc/train-*.parquet" in yaml
    assert "CC BY-NC-SA 4.0" in card
