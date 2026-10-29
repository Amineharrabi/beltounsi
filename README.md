# BelTounsi tooling

Collectors for **BelTounsi**, a Tunisian Arabic (Derja) dataset on Hugging Face. There are two parts:

- **`beltounsi-collect`**: a 24/7 Tunisia-Sat collector for a VPS. It runs a full crawl over plain HTTP and falls back to RSS when Cloudflare blocks it.
- **`beltounsi-mirror`**: copies the open Tunisian datasets from their original sources into the HF repo. It runs on GitHub Actions, so downloads and uploads use GitHub's bandwidth, not yours.

Tunisia-Sat content is collected with the written permission of the forum's administration.

## How the collector works

All requests go through one client:
- Python's standard `urllib`
- an honest User-Agent
- at least 5 s between requests
- robots.txt respected
- gzip and conditional GET

It never pretends to be a browser and never tries to pass a bot check.

| Part | What it does |
|---|---|
| **Crawl** | **Listings:** each forum listing is read from page 1, newest activity first. It stops at the first unpinned thread last active before `min_post_date` (default 2025-01-01). **Threads:** each thread is read from its **last page backwards** to the same date, giving full post text. **Recrawl:** every `recrawl_hours` the listings are re-read, and only threads with new replies are fetched again. Posts are deduplicated by id. **Order:** `sections` go first, then every other forum from the sitemap (`crawl_all_nodes`). |
| **Cloudflare fallback** | If a page comes back as a challenge (`cf-mitigated: challenge`, a "Just a moment" page, or HTTP 429), the crawl pauses: 30 min at first, doubling up to 12 h. Meanwhile **every forum's RSS feed** is polled, so new threads are still captured. When the pause ends, the crawl tries again. |
| **RSS** | **Site-wide feed:** `/forums/forums/-/index.rss` is polled every 5 min. **Per-forum feeds** (`<forum>/index.rss`): polled on an adaptive 15 min – 4 h schedule when the crawl is blocked or idle. Busy feeds speed up and quiet ones slow down. **Content:** feeds carry the 20 newest threads, each with a snippet of the first post. Category, thread, tag and "what's new" feeds exist on the site but are always empty, and `?page=` is ignored, so these two feeds are every source the RSS side can use. |
| **Index** | Every week the sitemap (`/forums/sitemap.php` and about 38 files) is read. The full lists of forums (which become feeds), threads (with `lastmod`) and tags go into `state.sqlite`. |
| **Output** | Posts go to `data/posts/YYYY-MM-DD.jsonl` (config `tunisia_sat`). RSS snippets go to `data/rss/YYYY-MM-DD.jsonl` (config `tunisia_sat_rss`). There is one file per UTC day; finished days are pushed to HF once a day. Usernames are never stored (mentions become `@USER`, quoted replies are dropped). E-mails and phone numbers are masked. |

## Deploy on a VPS (Debian/Ubuntu)

```bash
git clone <this repo> beltounsi && cd beltounsi
sudo bash deploy/install.sh
sudo nano /opt/beltounsi/config.toml     # set hf_repo_id
sudo nano /opt/beltounsi/.env            # HF_TOKEN=hf_... (write token)
sudo -u beltounsi bash -c 'set -a; . /opt/beltounsi/.env; /opt/beltounsi/.venv/bin/beltounsi-collect --config /opt/beltounsi/config.toml import-seen --hf'
sudo -u beltounsi /opt/beltounsi/.venv/bin/beltounsi-collect --config /opt/beltounsi/config.toml crawl -n 3
sudo systemctl enable --now beltounsi-collect
journalctl -u beltounsi-collect -f
```

- `import-seen --hf` marks the posts already on HF as collected, so they aren't duplicated.
- `crawl -n 3` is a quick check that the site answers this server. Datacenter IPs are challenged more often than home connections; if it reports `crawl_blocked`, the service still works in RSS mode.

Other commands: `stats`, `discover` (re-read sitemaps now), `poll -n N`, `push`.

## Mirror the open datasets (no local bandwidth)

1. In this GitHub repo, go to **Settings → Secrets and variables → Actions**.
2. Add the secret `HF_TOKEN` (an HF write token).
3. Add the variable `HF_REPO_ID` (e.g. `you/BelTounsi`).
4. Under **Actions → Mirror open datasets to Hugging Face → Run workflow**, leave "only" empty to mirror everything.

| Subset | Original source | License |
|---|---|---|
| `tarc` | github.com/eligugliotta/tarc (rebuilt into parallel sentences) | CC BY-NC-SA 4.0 |
| `tunizi` | zenodo.org/record/4275240 | CC BY 4.0 |
| `tsac` | github.com/fbougares/TSAC | LGPL-3.0 |
| `tunswitch` | zenodo.org/records/8370566 (transcript CSVs read out of the zips by HTTP range; the 18 GB of audio is never downloaded) | CC BY 4.0 |
| `fineweb2_aeb` | HuggingFaceFW/fineweb-2 `aeb_Arab` | ODC-By 1.0 |
| `linto_text` | linagora/linto-dataset-text-ar-tn, minus subsets mirrored from their originals and generated/list-like ones | Apache-2.0 (as declared) |

The HF dataset card is generated from `src/beltounsi/hub.py`. It only lists configs that have data.

## Development

```bash
pip install -e ".[hf,dev]"
pytest
```
