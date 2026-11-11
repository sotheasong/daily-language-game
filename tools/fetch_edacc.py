#!/usr/bin/env python3
"""Fetch a sample of EdAcc utterances with their accent labels.

EdAcc is the Edinburgh International Accents of English Corpus, CC BY-SA 4.0,
40 hours of spontaneous dyadic conversation. The full release is 5.5 GB; this
pulls a spread across both splits, which is enough to audit how much of it
clears the voiced-seconds floor.

Audio comes back as mono 32 kHz WAV, so nothing needs installing.

    python3 tools/fetch_edacc.py --per-split 200

Writes .cache/edacc/*.wav and .cache/edacc/manifest.tsv.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, ".cache", "edacc")
ROWS = ("https://datasets-server.huggingface.co/rows"
        "?dataset=edinburghcstr%2Fedacc&config=default&split={split}"
        "&offset={offset}&length={length}")
PAGE = 100


def fetch(url, raw=False, tries=6, pace=0.4):
    """GET with backoff. The rows endpoint rate-limits, and 429 needs waiting
    out rather than retrying straight away."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "daily-language-game/0.1"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            time.sleep(pace)
            return data if raw else json.loads(data)
        except urllib.error.HTTPError as exc:
            # 429 is rate limiting, 5xx is the server having a moment. Both
            # are worth waiting out; a 404 is not.
            if (exc.code != 429 and exc.code < 500) or attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-split", type=int, default=200,
                    help="utterances to sample from each split")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    rows = []
    for split in ("validation", "test"):
        total = fetch(ROWS.format(split=split, offset=0, length=1))["num_rows_total"]
        # Rows are ordered by conversation, so a contiguous block is two
        # speakers talking to each other. Take a small page from many places
        # instead: the first attempt stepped in strides of 5,400 and stopped
        # after two pages, which reached 8 speakers and 5 accents out of 25.
        per_stop = 12
        stops = max(1, args.per_split // per_stop)
        taken = 0
        for n in range(stops):
            offset = min(total - per_stop, int(n * total / stops))
            if offset < 0:
                offset = 0
            try:
                page = fetch(ROWS.format(split=split, offset=offset, length=per_stop))
            except Exception as exc:
                sys.stderr.write(f"\n  skipped offset {offset}: {exc}\n")
                continue
            for item in page.get("rows", []):
                row = item["row"]
                audio = (row.get("audio") or [{}])[0].get("src")
                if not audio:
                    continue
                rows.append({
                    "split": split, "idx": item["row_idx"],
                    "speaker": row.get("speaker") or "",
                    "accent": row.get("accent") or "",
                    "l1": row.get("l1") or "",
                    "text": (row.get("text") or "").replace("\t", " ")[:200],
                    "src": audio,
                })
                taken += 1
            sys.stderr.write(f"\r  {split}: {taken} listed from {n + 1}/{stops} places")
            sys.stderr.flush()
        sys.stderr.write("\n")

    manifest = []
    for i, row in enumerate(rows, 1):
        name = f"{row['split']}-{row['idx']:05d}.wav"
        path = os.path.join(args.out, name)
        if not os.path.exists(path):
            try:
                blob = fetch(row["src"], raw=True)
            except Exception as exc:
                sys.stderr.write(f"\n  skipped {name}: {exc}\n")
                continue
            with open(path, "wb") as fh:
                fh.write(blob)
        manifest.append({k: row[k] for k in
                         ("split", "idx", "speaker", "accent", "l1", "text")} | {"file": name})
        if i % 25 == 0 or i == len(rows):
            sys.stderr.write(f"\r  downloaded {i}/{len(rows)}")
            sys.stderr.flush()
    sys.stderr.write("\n")

    path = os.path.join(args.out, "manifest.tsv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["file", "split", "idx", "speaker", "accent", "l1", "text"],
            delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest)
    sys.stderr.write(f"{len(manifest)} utterances in {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
