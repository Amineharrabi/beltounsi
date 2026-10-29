"""Hugging Face side: the BelTounsi dataset card and upload/download helpers (needs the [hf] extra).

The card only declares configs that have data in the repo, so the dataset viewer never points at
missing files. Every uploader calls `sync_card` after adding data.
"""

from __future__ import annotations

from dataclasses import dataclass

CARD_PATH = "README.md"


@dataclass
class Subset:
    name: str
    title: str
    script: str
    splits: tuple[str, ...]
    fmt: str  # "parquet" or "jsonl"
    source: str
    license: str
    note: str = ""

    def data_files(self) -> list[dict]:
        if self.fmt == "jsonl":  # day files appended by the collector; one split
            return [{"split": "train", "path": f"data/{self.name}/*.jsonl"}]
        return [{"split": s, "path": f"data/{self.name}/{s}-*.parquet"} for s in self.splits]


SUBSETS = [
    Subset("tunisia_sat", "Tunisia-Sat forum posts (full text)", "Arabic, some Latin", ("train",), "jsonl",
           "https://www.tunisia-sat.com/forums/",
           "Used with the written permission of the Tunisia-Sat administration (2026-10-28); posts remain their authors' copyright",
           "Collected newest first from discussion forums. Usernames and mentions removed; e-mails and phone numbers masked."),
    Subset("tunisia_sat_rss", "Tunisia-Sat new threads via RSS (first-post snippets)", "Arabic, some Latin", ("train",),
           "jsonl", "https://www.tunisia-sat.com/forums/forums/-/index.rss",
           "Same permission as tunisia_sat", "Snippets are cut by the forum (`truncated` = true)."),
    Subset("tarc", "TArC: Tunisian Arabish Corpus, sentence-level parallel Arabizi / Arabic script", "parallel",
           ("train",), "parquet", "https://github.com/eligugliotta/tarc", "CC BY-NC-SA 4.0",
           "Rebuilt into sentences from the token-level release (`arabish` and `CODA` columns)."),
    Subset("tunizi", "TUNIZI: Arabizi social media comments with sentiment", "Latin (Arabizi)", ("train",),
           "parquet", "https://zenodo.org/record/4275240", "CC BY 4.0"),
    Subset("tsac", "TSAC: Tunisian sentiment analysis corpus (Facebook comments)", "mixed", ("train", "test"),
           "parquet", "https://github.com/fbougares/TSAC", "LGPL-3.0"),
    Subset("tunswitch", "TunSwitch: transcripts of Tunisian (and code-switched) speech", "Arabic, some Latin",
           ("train", "dev", "test"), "parquet", "https://zenodo.org/records/8370566", "CC BY 4.0",
           "Transcripts only (TunSwitchTO and TunSwitchCS); the audio stays on Zenodo."),
    Subset("fineweb2_aeb", "FineWeb-2 aeb_Arab: web text classified as Tunisian Arabic", "Arabic", ("train", "test"),
           "parquet", "https://huggingface.co/datasets/HuggingFaceFW/fineweb-2", "ODC-By 1.0 (and Common Crawl terms of use)",
           "Language ID is noisy: some documents are MSA or other dialects."),
    Subset("linto_text", "LinTO Tunisian text collection (selected subsets)", "Arabic", ("train",), "parquet",
           "https://huggingface.co/datasets/linagora/linto-dataset-text-ar-tn",
           "Apache-2.0 as declared by LinTO; upstream terms of each subset not verified",
           "Excludes subsets mirrored here from their originals (TSAC, TunSwitch) and generated or list-like ones."),
]
BY_NAME = {s.name: s for s in SUBSETS}


def render_card(present: set[str]) -> str:
    configs = [s for s in SUBSETS if s.name in present]
    yaml = ["---", "pretty_name: BelTounsi", "language:", "- aeb", "- ar", "license: other",
            "license_name: per-config", "task_categories:", "- text-generation", "- translation",
            "- text-classification", "configs:"]
    for s in configs:
        yaml.append(f"- config_name: {s.name}")
        yaml.append("  data_files:")
        for df in s.data_files():
            yaml += [f"  - split: {df['split']}", f"    path: {df['path']}"]
    yaml.append("---")

    rows = "\n".join(
        f"| `{s.name}` | {s.title} | {s.script} | {s.license} | [link]({s.source}) |"
        + (" available |" if s.name in present else " coming |")
        for s in SUBSETS
    )
    notes = "\n".join(f"- **`{s.name}`**: {s.note}" for s in SUBSETS if s.note)
    body = f"""
# BelTounsi

Tunisian Arabic (Derja / *Tounsi*) text in both scripts: Arabic script and Latin-script Arabizi,
plus a parallel Arabizi / Arabic-script subset. Each config keeps its source and its own license.

| config | content | script | license | source | status |
|---|---|---|---|---|---|
{rows}

```python
from datasets import load_dataset
ds = load_dataset("<this repo>", "tunisia_sat", split="train")
```

## Notes

{notes}

## Licensing

There is no single license: **each config carries the license of its source** (table above). Check
it before reuse. `tarc` is non-commercial (NC-SA). The Tunisia-Sat configs were collected and are
published with the written permission of the forum's administration; members keep the copyright of
their posts. To have a post removed, open a discussion on this dataset's page with its URL.

## Privacy

Tunisia-Sat usernames are not stored (mentions become `@USER`, quoted replies are dropped) and
e-mail addresses and phone numbers are masked as `<EMAIL>` / `<PHONE>` (`pii_masked` counts them).
"""
    return "\n".join(yaml) + "\n" + body


def _api():
    from huggingface_hub import HfApi

    return HfApi()


def ensure_repo(repo_id: str, private: bool = True) -> None:
    _api().create_repo(repo_id, repo_type="dataset", private=private, exist_ok=True)


def present_subsets(repo_id: str) -> set[str]:
    files = _api().list_repo_files(repo_id, repo_type="dataset")
    return {f.split("/")[1] for f in files if f.startswith("data/") and f.count("/") >= 2} & set(BY_NAME)


def sync_card(repo_id: str) -> None:
    api = _api()
    card = render_card(present_subsets(repo_id))
    try:
        current = open(api.hf_hub_download(repo_id, CARD_PATH, repo_type="dataset"), encoding="utf-8").read()
    except Exception:
        current = None
    if card != current:
        api.upload_file(path_or_fileobj=card.encode("utf-8"), path_in_repo=CARD_PATH, repo_id=repo_id,
                        repo_type="dataset", commit_message="Update dataset card")


def push_files(repo_id: str, pairs: list[tuple[str, str]], message: str) -> None:
    """Upload (local path, path in repo) pairs in one commit, then refresh the card."""
    from huggingface_hub import CommitOperationAdd

    ensure_repo(repo_id)
    ops = [CommitOperationAdd(path_in_repo=dst, path_or_fileobj=src) for src, dst in pairs]
    _api().create_commit(repo_id, operations=ops, commit_message=message, repo_type="dataset")
    sync_card(repo_id)


def download_prefix(repo_id: str, prefix: str) -> list:
    from pathlib import Path

    api = _api()
    files = [f for f in api.list_repo_files(repo_id, repo_type="dataset") if f.startswith(prefix)]
    return [Path(api.hf_hub_download(repo_id, f, repo_type="dataset")) for f in files]
