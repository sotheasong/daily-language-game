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


def load_env(path=None):
    """Read MDC_API_KEY from a .env at the repo root if it is not already set.

    The key belongs in a gitignored file rather than a shell history or a
    message, and the documented setup puts it there, so read it from there.
    """
    if os.environ.get("MDC_API_KEY"):
        return
    path = path or os.path.join(REPO, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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
        "url": getattr(details, "datasetUrl", "") or "",
    }


def write_manifest(root, locale, speakers, per_speaker):
    """Turn an extracted Common Voice locale into the manifest clips.py reads.

    Common Voice ships validated.tsv with client_id, path and sentence. Only
    validated rows are used: the rest have not been checked by two listeners.

    **Several utterances from each of a few speakers, not one from many.** The
    first version spread the sample as widely as it could, which gave 60
    utterances from 60 speakers and built zero clips: a clip is spliced from
    one speaker's takes, and a Common Voice prompt is about 2.4 voiced seconds,
    so reaching 8 needs three or four from the same person. Variety comes from
    using different speakers for different clips, not within one.
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

    by_speaker = {}
    for row in rows:
        by_speaker.setdefault(row.get("client_id", ""), []).append(row)
    # Only speakers with enough takes to build a clip from are worth taking.
    ranked = sorted((k for k in by_speaker if len(by_speaker[k]) >= per_speaker),
                    key=lambda k: -len(by_speaker[k]))
    if not ranked:
        ranked = sorted(by_speaker, key=lambda k: -len(by_speaker[k]))
    picked = []
    for speaker in ranked[:speakers]:
        picked.extend(by_speaker[speaker][:per_speaker])

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


def terms_accepted(dataset_id):
    """Ask whether this dataset can be downloaded, without downloading it.

    Goes through the SDK's own request path rather than a hand-rolled one.
    A plain urllib POST gets a Cloudflare 403 on browser signature, code 1010,
    which reads like a refusal and is nothing of the kind. Asking costs one
    request; finding out by downloading costs gigabytes.
    """
    from datacollective import api_utils
    url = f"{api_utils._get_api_url()}/datasets/{dataset_id}/download"
    try:
        api_utils._send_api_request(method="POST", url=url,
                                    source_function="terms_accepted")
        return True, ""
    except PermissionError as exc:
        return False, str(exc).split(":")[-1].strip()[:70]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:90]


def download_with_backoff(dc, dataset_id, cache, pause, tries=5):
    """Download, waiting out MDC's rate limiter rather than giving up on it.

    Pulling six locales back to back got one through and rate-limited the other
    five. The limit is per account and clears on its own, so waiting is the
    whole fix.
    """
    import time
    for attempt in range(tries):
        try:
            return dc.download_dataset(dataset_id, download_directory=cache)
        except dc.RateLimitError:
            if attempt == tries - 1:
                raise
            wait = pause * (attempt + 1)
            sys.stderr.write(f"    rate limited, waiting {wait}s\n")
            time.sleep(wait)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", type=int, help="pull every locale at this tier")
    ap.add_argument("--locale", nargs="*", help="specific locale codes")
    ap.add_argument("--list", action="store_true", help="resolve only, no download")
    ap.add_argument("--pull", action="store_true", help="download and extract")
    ap.add_argument("--check", action="store_true",
                    help="ask whether each dataset is downloadable, without downloading")
    ap.add_argument("--speakers", type=int, default=8,
                    help="how many speakers to sample per locale")
    ap.add_argument("--per-speaker", type=int, default=10,
                    help="utterances per speaker; a clip needs three or four")
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "cv-archives"))
    ap.add_argument("--pause", type=int, default=90,
                    help="seconds to wait after a rate limit, doubling each retry")
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
    if args.check:
        load_env()
        if not os.environ.get("MDC_API_KEY"):
            raise SystemExit("MDC_API_KEY is not set and no .env was found")
        sys.stderr.write("\n  checking whether each dataset is unlocked:\n")
        blocked = []
        for row, info in resolved:
            ok, why = terms_accepted(info["id"])
            sys.stderr.write(f"    {row['input_name']:<18}{row['locale']:<8}"
                             f"{'ready' if ok else 'blocked':<9}{'' if ok else why}\n")
            if not ok:
                blocked.append((row["input_name"], row["locale"], info["url"]))
        if blocked:
            sys.stderr.write(f"\n  {len(blocked)} still need their terms accepted:\n")
            for name, locale, url in blocked:
                sys.stderr.write(f"    {name:<18}{locale:<8}{url}\n")
        return 1 if blocked else 0

    if args.list or not args.pull:
        sys.stderr.write("  (--list only; pass --pull to download)\n")
        return 0

    import datacollective as dc
    load_env()
    if not os.environ.get("MDC_API_KEY"):
        raise SystemExit(
            "MDC_API_KEY is not set in this shell.\n"
            "Put it in a .env file at the repo root, which is gitignored:\n"
            "    MDC_API_KEY=your-key-here\n"
            "and accept each dataset's terms once in the browser first."
        )

    os.makedirs(args.cache, exist_ok=True)
    needs_terms, failed = [], []
    for row, info in resolved:
        if info["paid"]:
            continue
        locale = row["locale"]
        sys.stderr.write(f"\n  {locale}: downloading {info['size'] / 1e6:.0f} MB\n")
        try:
            archive = download_with_backoff(dc, info["id"], args.cache, args.pause)
        except PermissionError:
            # Terms are accepted per dataset, in the browser. Collect every one
            # that needs it rather than stopping at the first, so the whole list
            # can be clicked through in one go.
            needs_terms.append((row["input_name"], locale, info["url"]))
            sys.stderr.write(f"  {locale}: terms not accepted yet\n")
            continue
        except Exception as exc:
            failed.append((locale, f"{type(exc).__name__}: {exc}"))
            sys.stderr.write(f"  {locale}: {type(exc).__name__}\n")
            continue

        extracted = os.path.join(args.cache, "extracted", locale)
        if not os.path.isdir(extracted):
            os.makedirs(extracted, exist_ok=True)
            with tarfile.open(archive) as tar:
                tar.extractall(extracted, filter="data")
        kept, where = write_manifest(extracted, locale, args.speakers, args.per_speaker)
        sys.stderr.write(f"  {locale}: {kept} utterances -> {where}\n")

    if needs_terms:
        sys.stderr.write(
            f"\n{len(needs_terms)} dataset(s) need their terms accepted once, in a\n"
            "browser signed in to the same account. Open each, accept, then re-run:\n\n")
        for name, locale, url in needs_terms:
            sys.stderr.write(f"  {name:<18} {locale:<8} {url}\n")
    if failed:
        sys.stderr.write(f"\n{len(failed)} failed for other reasons:\n")
        for locale, why in failed:
            sys.stderr.write(f"  {locale:<8} {why}\n")
    return 1 if (needs_terms or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
