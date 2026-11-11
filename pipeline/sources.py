#!/usr/bin/env python3
"""Shared plumbing for external datasets.

Every dataset this project pulls in has the same shape. It is a CSV fetched over
HTTP, it joins on a Glottocode, and it is worth caching because it does not change
between runs. Keeping the fetch and cache in one place means adding a dataset is
adding a row to a registry file, not another copy of this code.

Datasets expected to arrive this way: Glottolog CLDF (done), Glottography area
indexes (done), then WALS, Grambank, and the Common Voice clip manifests.

Registry files live in data/sources/ as TSV with # comments. Caches live in
.cache/ and are gitignored, so a clone re-downloads rather than carrying 2.4 MB
of someone else's data in the history.

Stdlib only. No pip install.
"""

import csv
import os
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, ".cache")
DATA_DIR = os.path.join(REPO_ROOT, "data")


def cache_path(*parts):
    """Path inside .cache/, directories created on demand."""
    path = os.path.join(CACHE_DIR, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def fetch(url, path, min_bytes=0, refresh=False, label=None):
    """Download url to path once, then reuse it.

    min_bytes guards against caching an error page or a truncated download. A
    silently empty cache file is worse than a crash, because every downstream
    join then reports a gap that is not really there.
    """
    if not refresh and os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sys.stderr.write(f"downloading {label or url}\n")
    req = urllib.request.Request(url, headers={"User-Agent": "daily-language-game/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if len(data) < min_bytes:
        raise SystemExit(
            f"refusing to cache a suspiciously small file from {url} "
            f"({len(data)} bytes, expected at least {min_bytes}); "
            "check the URL or your connection"
        )
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def load_csv(path, delimiter=","):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=delimiter))


def read_registry(path):
    """Read a data/sources/*.tsv registry, skipping # comments and blank lines."""
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))
