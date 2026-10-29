"""Mirror the open Tunisian datasets into the BelTounsi HF repo, from their original sources.

Meant to run on a GitHub Actions runner (.github/workflows/mirror-open-datasets.yml), so the
downloads and uploads use GitHub's and Hugging Face's bandwidth, not yours. Each subset is
rewritten as parquet with a `text` column and replaces the previous copy in the repo.

Sources and file names were checked on 2026-10-29 (see docs/sources.md in the main project).
"""

from __future__ import annotations

import argparse
import csv
import http.client
import io
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

UA = {"User-Agent": "BelTounsi-mirror/0.1 (dataset build)"}
TARC_URL = "https://raw.githubusercontent.com/eligugliotta/tarc/HEAD/tarc.tsv"
TUNIZI_URL = "https://zenodo.org/api/records/4275240/files/tunizi_train/content"
TSAC_URL = "https://raw.githubusercontent.com/fbougares/TSAC/HEAD/{}.txt"
TUNSWITCH_ZIPS = {
    "TO": "https://zenodo.org/api/records/8370566/files/TunSwitchTO.zip/content",
    "CS": "https://zenodo.org/api/records/8370566/files/TunSwitchCS.zip/content",
}
FINEWEB_REPO = "HuggingFaceFW/fineweb-2"
FINEWEB_FILES = {"train": "data/aeb_Arab/train/000_00000.parquet", "test": "data/aeb_Arab/test/000_00000.parquet"}
FINEWEB_COLUMNS = ["text", "id", "url", "date", "dump", "language_score"]
LINTO_REPO = "linagora/linto-dataset-text-ar-tn"
# Mirrored from their originals instead (tsac, tunswitch), or not natural Derja text (name lists, LLM output).
LINTO_EXCLUDE = {"TSAC", "TunSwitchCodeSwitching", "TunSwitchTunisiaOnly", "CombinedNames",
                 "Tunisien_Dialect_Summary-llama2"}


def _read(req: urllib.request.Request, timeout: int, tries: int = 5) -> bytes:
    """Read a response, retrying dropped connections (Zenodo sometimes cuts transfers short)."""
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == tries - 1:
                raise
            print(f"  retrying {req.full_url} after {e!r}", flush=True)
            time.sleep(5 * 2**attempt)
    raise AssertionError("unreachable")


def fetch(url: str) -> bytes:
    return _read(urllib.request.Request(url, headers=UA), timeout=300)


class RangeFile(io.RawIOBase):
    """A remote file read with HTTP Range requests, so zipfile can pull single members out of a
    multi-GB archive (TunSwitch transcripts sit next to gigabytes of audio)."""

    def __init__(self, url: str):
        self.url, self.pos = url, 0
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=60) as r:
            self.size = int(r.headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def readinto(self, b):
        if self.pos >= self.size or not len(b):
            return 0
        end = min(self.pos + len(b), self.size) - 1
        req = urllib.request.Request(self.url, headers={**UA, "Range": f"bytes={self.pos}-{end}"})
        data = _read(req, timeout=120)
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


# ---------------------------------------------------------------- builders: name -> {split: rows}


def build_tarc() -> dict[str, list[dict]]:
    rows, toks = [], []
    reader = csv.reader(io.StringIO(fetch(TARC_URL).decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader)
    col = {name: i for i, name in enumerate(header)}

    def flush():
        if toks:
            first = toks[0]
            meta = lambda k: (first[col[k]] if col[k] < len(first) and first[col[k]] not in ("", "/") else None)  # noqa: E731
            rows.append({
                "arabizi": " ".join(t[col["arabish"]] for t in toks),
                "arabic": " ".join(t[col["CODA"]] for t in toks),
                "genre": first[col["TYPE"]],
                "date": meta("data"),
                "governorate": meta("governorate"),
                "age": meta("age"),
                "gender": meta("gender"),
                "n_tokens": len(toks),
            })
            toks.clear()

    for r in reader:
        if len(r) <= col["CODA"]:
            continue
        if r[col["data"]] == "<eos>":
            flush()
        else:
            toks.append(r)
    flush()
    for i, r in enumerate(rows):
        r["text"] = r["arabizi"]  # every config has `text`; here it is the Arabizi side
        r["id"] = f"tarc-{i}"
    return {"train": rows}


def build_tunizi() -> dict[str, list[dict]]:
    reader = csv.DictReader(io.StringIO(fetch(TUNIZI_URL).decode("utf-8")))
    return {"train": [{"text": r["text"], "label": int(r["label"]), "id": r["ID"]} for r in reader if r.get("text")]}


def build_tsac() -> dict[str, list[dict]]:
    out = {}
    for split in ("train", "test"):
        rows = []
        for label in ("pos", "neg"):
            for i, line in enumerate(fetch(TSAC_URL.format(f"{split}_{label}")).decode("utf-8").splitlines()):
                if line.strip():
                    rows.append({"text": line.strip(), "label": label, "id": f"{split}_{label}-{i}"})
        out[split] = rows
    return out


def build_tunswitch() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"train": [], "dev": [], "test": []}
    for part, url in TUNSWITCH_ZIPS.items():
        z = zipfile.ZipFile(io.BufferedReader(RangeFile(url), buffer_size=1 << 16))
        for name in z.namelist():
            split = Path(name).stem
            if not name.endswith(".csv") or split not in out:
                continue
            # SpeechBrain tables (ID, wav, wrd, duration); some files add a pandas index column, so read by name.
            for r in csv.DictReader(io.StringIO(z.read(name).decode("utf-8"))):
                text = (r.get("wrd") or "").strip()
                if not text:
                    continue
                try:
                    duration = float(r.get("duration") or "")
                except ValueError:
                    duration = None
                out[split].append({"text": text, "subset": f"TunSwitch{part}", "id": r.get("ID"), "duration": duration})
    return out


def build_fineweb2_aeb() -> dict[str, list[dict]]:
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    out = {}
    for split, path in FINEWEB_FILES.items():
        t = pq.read_table(hf_hub_download(FINEWEB_REPO, path, repo_type="dataset"))
        t = t.select([c for c in FINEWEB_COLUMNS if c in t.column_names])
        out[split] = t.to_pylist()
    return out


def build_linto_text() -> dict[str, list[dict]]:
    import pyarrow.parquet as pq
    from huggingface_hub import HfApi, hf_hub_download

    rows = []
    for f in sorted(HfApi().list_repo_files(LINTO_REPO, repo_type="dataset")):
        parts = f.split("/")
        if len(parts) != 3 or parts[0] != "data" or not f.endswith(".parquet") or parts[1] in LINTO_EXCLUDE:
            continue
        t = pq.read_table(hf_hub_download(LINTO_REPO, f, repo_type="dataset"), columns=["ids", "text"])
        rows += [{"text": r["text"], "subset": parts[1], "id": r["ids"]} for r in t.to_pylist() if r["text"]]
    return {"train": rows}


BUILDERS = {
    "tarc": build_tarc,
    "tunizi": build_tunizi,
    "tsac": build_tsac,
    "tunswitch": build_tunswitch,
    "fineweb2_aeb": build_fineweb2_aeb,
    "linto_text": build_linto_text,
}


def write_parquet(name: str, splits: dict[str, list[dict]], out_dir: Path) -> dict[str, int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    d = out_dir / "data" / name
    d.mkdir(parents=True, exist_ok=True)
    counts = {}
    for split, rows in splits.items():
        if rows:
            pq.write_table(pa.Table.from_pylist(rows), d / f"{split}-00000-of-00001.parquet", compression="zstd")
            counts[split] = len(rows)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="beltounsi-mirror", description=__doc__.splitlines()[0])
    ap.add_argument("--repo-id", default=os.environ.get("HF_REPO_ID", ""), help="e.g. you/BelTounsi")
    ap.add_argument("--only", default="", help="comma-separated subsets (default: all)")
    ap.add_argument("--out", type=Path, default=None, help="build here and keep the files")
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args(argv)
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(BUILDERS)
    unknown = set(names) - set(BUILDERS)
    if unknown:
        ap.error(f"unknown subsets: {sorted(unknown)}; choose from {list(BUILDERS)}")
    if not args.no_upload and not args.repo_id:
        ap.error("--repo-id (or HF_REPO_ID) is required to upload")

    out = args.out or Path(tempfile.mkdtemp(prefix="beltounsi-"))
    failed = []
    for name in names:
        try:
            counts = write_parquet(name, BUILDERS[name](), out)
        except Exception as e:  # one broken upstream must not stop the others
            print(f"[{name}] FAILED: {e!r}", flush=True)
            failed.append(name)
            continue
        print(f"[{name}] {counts}", flush=True)
        if not args.no_upload:
            from huggingface_hub import HfApi

            from .hub import ensure_repo

            ensure_repo(args.repo_id)
            HfApi().upload_folder(
                repo_id=args.repo_id, repo_type="dataset", folder_path=str(out),
                allow_patterns=[f"data/{name}/*"], delete_patterns=[f"data/{name}/*"],
                commit_message=f"Mirror {name} from its original source",
            )
    if not args.no_upload:
        from .hub import sync_card

        sync_card(args.repo_id)
    print(f"done; files in {out}" + (f"; FAILED: {failed}" if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
