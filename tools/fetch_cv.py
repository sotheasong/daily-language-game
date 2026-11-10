#!/usr/bin/env python3
"""Resolve and pull Common Voice locales from Mozilla Data Collective.

Common Voice moved to MDC in October 2025 and is no longer on Hugging Face, so
this is the only route. The catalog is public; downloading needs an API key.

    # public, no key: what each locale would cost to pull
    .venv/bin/python3 tools/fetch_cv.py --tier 1 --list

    # needs MDC_API_KEY, and the dataset terms accepted in the browser first
    .venv/bin/python3 tools/fetch_cv.py --tier 1 --pull --limit 60

Writes .cache/cv/<locale>/ with audio and a manifest.tsv that
`python3 -m pipeline.clips` reads directly.

Needs the datacollective SDK, so it runs from .venv rather than the system
Python, which Homebrew marks externally managed. The pipeline itself stays
stdlib; this is a tool.
"""

import argparse
import csv
import os
import re
import shutil
import sys
import tarfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, ".cache", "cv")
# The canonical per-locale release. Other things in the catalog carry the same
# words: single-word segments, regional subsets, third-party bundles.
CANON = re.compile(r"^Common Voice Scripted Speech ([\d.]+) - ", re.I)


def read_locales(tier=None, only=None):
    path = os.path.join(REPO, "data", "cv-locales.tsv")
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(
            (l for l in fh if l.strip() and not l.startswith("#")), delimiter="\t"))
    if tier:
        rows = [r for r in rows if r["tier"] == str(tier)]
    if only:
        rows = [r for r in rows if r["locale"] in only]
    return rows


def resolve(locale):
    """Find the canonical Common Voice release for a locale. Public, no key."""
    import datacollective as dc
    found = dc.list_datasets(locale=locale, query="Common Voice Scripted Speech",
                             results_per_page=30)
    best = None
    for item in (found.datasets if hasattr(found, "datasets") else found):
        name = getattr(item, "name", "") or ""
        match = CANON.match(name)
        if not match or getattr(item, "locale", None) != locale:
            continue
        version = tuple(int(p) for p in match.group(1).split(".") if p.isdigit())
        if best is None or version > best[0]:
            best = (version, item)
    return best[1] if best else None


def describe(item):
    import datacollective as dc
    details = dc.get_dataset_details(getattr(item, "id"))
    return {
        "id": details.id,
        "name": details.name,
        "size": int(getattr(details, "sizeBytes", 0) or 0),
        "licence": getattr(details, "licenseAbbreviation", "") or "",
        "paid": bool(getattr(details, "isPaid", False)),
        "format": getattr(details, "format", "") or "",
    }


def write_manifest(root, locale, limit):
    """Turn an extracted Common Voice locale into the manifest clips.py reads.

    Common Voice ships validated.tsv with client_id, path and sentence. Only
    validated rows are used: the rest have not been checked by two listeners.
    """
    validated = None
    for base, _dirs, names in os.walk(root):
        if "validated.tsv" in names:
            validated = os.path.join(base, "validated.tsv")
            break
    if not validated:
        return 0, "no validated.tsv in the archive"

    clips_dir = os.path.join(os.path.dirname(validated), "clips")
    with open(validated, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    # Spread across speakers rather than taking the first N rows, which would
    # be one prolific contributor. Different voices is the whole point.
    by_speaker = {}
    for row in rows:
        by_speaker.setdefault(row.get("client_id", ""), []).append(row)
    picked, speakers = [], sorted(by_speaker, key=lambda k: -len(by_speaker[k]))
    while len(picked) < limit and speakers:
        for speaker in list(speakers):
            if by_speaker[speaker]:
                picked.append(by_speaker[speaker].pop())
            else:
                speakers.remove(speaker)
            if len(picked) >= limit:
                break

    out_dir = os.path.join(OUT, locale)
    os.makedirs(out_dir, exist_ok=True)
    kept = []
    for row in picked:
        name = os.path.basename(row.get("path", ""))
        src = os.path.join(clips_dir, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(out_dir, name)
        if not os.path.exists(dst):
            # Hard link where the filesystem allows it, so keeping 60 clips out
            # of a 50 GB extraction costs nothing. Copy if it does not.
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
        kept.append({"file": name, "locale": locale,
                     "speaker": row.get("client_id", ""),
                     "text": (row.get("sentence", "") or "").replace("\t", " ")[:300]})

    path = os.path.join(out_dir, "manifest.tsv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["file", "locale", "speaker", "text"],
                                delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(kept)
    return len(kept), path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", type=int, help="pull every locale at this tier")
    ap.add_argument("--locale", nargs="*", help="specific locale codes")
    ap.add_argument("--list", action="store_true", help="resolve only, no download")
    ap.add_argument("--pull", action="store_true", help="download and extract")
    ap.add_argument("--limit", type=int, default=60,
                    help="utterances to keep per locale")
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "cv-archives"))
    args = ap.parse_args()

    rows = read_locales(args.tier, set(args.locale) if args.locale else None)
    if not rows:
        sys.stderr.write("no locales matched\n")
        return 1

    resolved, total = [], 0
    sys.stderr.write(f"  {'entry':<20}{'locale':<10}{'size':>10}  dataset\n")
    for row in rows:
        item = resolve(row["locale"])
        if item is None:
            sys.stderr.write(f"  {row['input_name']:<20}{row['locale']:<10}"
                             f"{'-':>10}  no canonical release\n")
            continue
        info = describe(item)
        total += info["size"]
        resolved.append((row, info))
        sys.stderr.write(f"  {row['input_name']:<20}{row['locale']:<10}"
                         f"{info['size'] / 1e6:>9.0f}M  {info['name'][:44]}\n")

    paid = [r for r in resolved if r[1]["paid"]]
    sys.stderr.write(f"\n  {len(resolved)} locales, {total / 1e9:.1f} GB total\n")
    if paid:
        sys.stderr.write(f"  {len(paid)} are paid datasets and were not fetched\n")
    if args.list or not args.pull:
        sys.stderr.write("  (--list only; pass --pull to download)\n")
        return 0

    import datacollective as dc
    if not os.environ.get("MDC_API_KEY"):
        raise SystemExit(
            "MDC_API_KEY is not set in this shell.\n"
            "Put it in a .env file at the repo root, which is gitignored:\n"
            "    MDC_API_KEY=your-key-here\n"
            "and accept each dataset's terms once in the browser first."
        )

    os.makedirs(args.cache, exist_ok=True)
    for row, info in resolved:
        if info["paid"]:
            continue
        locale = row["locale"]
        sys.stderr.write(f"\n  {locale}: downloading {info['size'] / 1e6:.0f} MB\n")
        archive = dc.download_dataset(info["id"], download_directory=args.cache)
        extracted = os.path.join(args.cache, "extracted", locale)
        if not os.path.isdir(extracted):
            os.makedirs(extracted, exist_ok=True)
            with tarfile.open(archive) as tar:
                tar.extractall(extracted, filter="data")
        kept, where = write_manifest(extracted, locale, args.limit)
        sys.stderr.write(f"  {locale}: {kept} utterances -> {where}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
