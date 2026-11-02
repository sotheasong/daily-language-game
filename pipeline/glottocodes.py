#!/usr/bin/env python3
"""Resolve entry names to Glottocodes using the Glottolog CLDF language table.

Why a local table rather than the web API: the CLDF export is one 2.4 MB CSV that
carries everything downstream needs (Glottocode, ISO 639-3, macroarea, coordinates,
countries, family, level). Matching locally is reproducible, offline after the first
run, and lets us fuzzy-match without hammering glottolog.org.

Names are never guessed. Anything that does not match exactly and uniquely is
reported as ambiguous or unresolved, with candidates, for a human to settle.

Usage:
    python3 -m pipeline.glottocodes data/entries.tsv -o data/resolved.tsv
    python3 -m pipeline.glottocodes data/entries.tsv -o data/resolved.tsv --refresh

Input is one entry per line: "Name" or "Name<TAB>tier". Blank lines and lines
starting with # are ignored.

Stdlib only. No pip install.
"""

import argparse
import csv
import difflib
import os
import sys
import urllib.request

CLDF_URL = (
    "https://raw.githubusercontent.com/glottolog/glottolog-cldf/master/cldf/languages.csv"
)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CACHE = os.path.join(_REPO_ROOT, ".cache", "languages.csv")

# Names where Glottolog's label differs from the one a player would recognise.
# Keep this list short and obvious; a long alias table means the entry list is wrong.
ALIASES = {
    # Every entry here was verified against the cached CLDF table. Glottolog often
    # has no node for "the language", only for its varieties, so most of these pick
    # a specific variety on purpose. An unverified guess here is worse than no alias:
    # "Sakha" -> "Yakut" silently broke resolution until it was checked.
    "mandarin": "Mandarin Chinese",
    "cantonese": "Yue Chinese",
    "persian": "Western Farsi",
    "farsi": "Western Farsi",
    "punjabi": "Eastern Panjabi",
    "twi": "Akan",
    "luganda": "Ganda",
    "northern sami": "North Saami",
    "meadow mari": "Eastern Mari",
    "west frisian": "Western Frisian",
    "azerbaijani": "North Azerbaijani",
    "oromo": "West Central Oromo",
    "quechua": "Cusco Quechua",
    "malay": "Standard Malay",
    "arabic": "Standard Arabic",
    "greek": "Modern Greek",
    "hebrew": "Modern Hebrew",
    "albanian": "Northern Tosk Albanian",
    "yiddish": "Eastern Yiddish",
    "pashto": "Northern Pashto",
    "armenian": "Eastern Armenian",
    "mongolian": "Halh Mongolian",
    "khmer": "Central Khmer",
    "indonesian": "Standard Indonesian",
    "uzbek": "Northern Uzbek",
    "kyrgyz": "Kirghiz",
    "uyghur": "Uighur",
    "ossetian": "Iron Ossetian",
    "komi": "Komi-Zyrian",
    "luxembourgish": "Moselle Franconian",
    "sardinian": "Logudorese Sardinian",
    # Glottolog treats Serbian and Croatian as dialects of one language,
    # Serbian-Croatian-Bosnian [sout1528]. They need dialect-level matching to get
    # separate footprints. Same for Asturian under Asturian-Leonese-Cantabrian.
    "serbian": "Serbian Standard",
    "croatian": "Croatian Standard",
    "asturian": "Asturian",
}

# Any status other than "ok" means a human has to look. Keep this list and the
# statuses returned by resolve() in step, or the exit-code gate goes quiet.
STATUSES = ("ok", "ambiguous", "alias_broken", "unresolved")

OUT_FIELDS = [
    "input_name",
    "tier",
    "status",
    "glottocode",
    "matched_name",
    "level",
    "iso639_3",
    "macroarea",
    "latitude",
    "longitude",
    "countries",
    "family_id",
    "is_isolate",
    "candidates",
]


def fetch_table(cache_path, refresh=False):
    """Download the CLDF language table once and cache it on disk."""
    if refresh or not os.path.exists(cache_path):
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        sys.stderr.write("downloading Glottolog CLDF table...\n")
        with urllib.request.urlopen(CLDF_URL, timeout=120) as resp:
            data = resp.read()
        if len(data) < 500_000:
            raise SystemExit(
                f"refusing to cache a suspiciously small table ({len(data)} bytes); "
                "check the URL or your connection"
            )
        with open(cache_path, "wb") as fh:
            fh.write(data)
    with open(cache_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_index(rows, levels):
    """Map lowercased name -> list of rows, restricted to the requested levels."""
    index = {}
    for row in rows:
        if row["Level"] not in levels:
            continue
        index.setdefault(row["Name"].strip().lower(), []).append(row)
    return index


def read_entries(path):
    entries = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            name = parts[0].strip()
            tier = parts[1].strip() if len(parts) > 1 else ""
            entries.append((name, tier))
    return entries


def resolve(name, index, names_list, cutoff, aliases=None):
    """Return (status, row_or_None, candidates_string).

    Resolution order: alias override, exact match, then fuzzy suggestions. A name
    matching several Glottolog languoids is ambiguous, never auto-picked, because a
    wrong Glottocode silently breaks every downstream join.

    Statuses:
      ok            unique exact match
      ambiguous     several exact matches, needs a human to choose
      alias_broken  our alias points at a name the table does not have
      unresolved    no match, with fuzzy candidates to look at
    """
    if aliases is None:
        aliases = ALIASES

    raw = name.strip().lower()
    aliased = raw in aliases
    key = (aliases[raw] if aliased else name).strip().lower()

    hits = index.get(key, [])
    if len(hits) == 1:
        return "ok", hits[0], ""
    if len(hits) > 1:
        cands = "; ".join(f"{r['Name']} [{r['Glottocode']}] {r['Macroarea']}" for r in hits)
        return "ambiguous", None, cands

    if aliased:
        # A broken alias is a bug in our own table, not a gap in upstream data.
        # Reporting it as "unresolved" hides that difference, which is how the
        # Sakha -> Yakut mistake survived a full run.
        return "alias_broken", None, f"alias points at {aliases[raw]!r}, which is not in the table"

    close = difflib.get_close_matches(key, names_list, n=5, cutoff=cutoff)
    cands = []
    for c in close:
        for r in index[c]:
            cands.append(f"{r['Name']} [{r['Glottocode']}] {r['Macroarea']}")
    return "unresolved", None, "; ".join(cands)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entries", help="input file, one entry per line, optional tab-separated tier")
    ap.add_argument("-o", "--out", default="-", help="output TSV path, default stdout")
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="where to cache the CLDF table")
    ap.add_argument("--refresh", action="store_true", help="re-download the CLDF table")
    ap.add_argument(
        "--levels",
        default="language,dialect",
        help="comma-separated Glottolog levels to match against (language,dialect,family)",
    )
    ap.add_argument("--cutoff", type=float, default=0.75, help="fuzzy match cutoff, 0-1")
    args = ap.parse_args()

    levels = {lvl.strip() for lvl in args.levels.split(",") if lvl.strip()}
    rows = fetch_table(args.cache, args.refresh)
    index = build_index(rows, levels)
    names_list = list(index.keys())
    entries = read_entries(args.entries)

    out_rows = []
    counts = dict.fromkeys(STATUSES, 0)
    for name, tier in entries:
        status, row, cands = resolve(name, index, names_list, args.cutoff)
        counts[status] += 1
        rec = {f: "" for f in OUT_FIELDS}
        rec.update({"input_name": name, "tier": tier, "status": status, "candidates": cands})
        if row is not None:
            rec.update(
                {
                    "glottocode": row["Glottocode"],
                    "matched_name": row["Name"],
                    "level": row["Level"],
                    "iso639_3": row["ISO639P3code"],
                    "macroarea": row["Macroarea"],
                    "latitude": row["Latitude"],
                    "longitude": row["Longitude"],
                    "countries": row["Countries"],
                    "family_id": row["Family_ID"],
                    "is_isolate": row["Is_Isolate"],
                }
            )
        out_rows.append(rec)

    stream = sys.stdout if args.out == "-" else open(args.out, "w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(stream, fieldnames=OUT_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(out_rows)
    finally:
        if stream is not sys.stdout:
            stream.close()

    total = len(entries)
    needs_human = sum(n for status, n in counts.items() if status != "ok")
    summary = ", ".join(f"{counts[s]} {s}" for s in STATUSES)
    sys.stderr.write(f"\n{total} entries: {summary}\n")
    if needs_human:
        sys.stderr.write("Settle these by hand before using the output:\n")
        for rec in out_rows:
            if rec["status"] != "ok":
                sys.stderr.write(f"  [{rec['status']:10}] {rec['input_name']}\n")
                if rec["candidates"]:
                    sys.stderr.write(f"               -> {rec['candidates']}\n")
    # Non-zero exit when anything needs a human, so this can gate a build step.
    return 1 if needs_human else 0


if __name__ == "__main__":
    sys.exit(main())
