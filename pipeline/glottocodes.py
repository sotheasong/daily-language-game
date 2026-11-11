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

Resolution is pinned by an approval lock. Once an entry is checked, its Glottocode
is the join key under every clip, polygon and approved puzzle, so an upstream
re-code is reported as drift rather than adopted:

    python3 -m pipeline.glottocodes data/entries.tsv -o data/resolved.tsv \
        --lock data/glottocodes.lock.tsv
    # after reviewing the change, sign it off with --write-lock

Input is one entry per line: "Name" or "Name<TAB>tier". Blank lines and lines
starting with # are ignored.

Stdlib only. No pip install.
"""

import argparse
import csv
import difflib
import os
import sys

from pipeline import sources

CLDF_URL = (
    "https://raw.githubusercontent.com/glottolog/glottolog-cldf/master/cldf/languages.csv"
)
DEFAULT_CACHE = os.path.join(sources.CACHE_DIR, "languages.csv")

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

# Keep this list and the statuses returned by resolve() in step, or the exit-code
# gate goes quiet. Anything outside APPROVED means a human has to look.
STATUSES = ("ok", "local", "ambiguous", "alias_broken", "unresolved", "drifted")

# "local" is approved by the act of writing the row and its reason into
# data/local-ids.tsv. It will never resolve, so gating on it would mean a build
# that can never go green.
APPROVED = ("ok", "local")

LOCK_FIELDS = ["input_name", "glottocode", "matched_name"]

OUT_FIELDS = [
    "input_name",
    "tier",
    "kind",
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
    sources.fetch(
        CLDF_URL, cache_path, min_bytes=500_000, refresh=refresh,
        label="Glottolog CLDF language table",
    )
    return sources.load_csv(cache_path)


def build_index(rows, levels):
    """Map lowercased name -> list of rows, restricted to the requested levels."""
    index = {}
    for row in rows:
        if row["Level"] not in levels:
            continue
        index.setdefault(row["Name"].strip().lower(), []).append(row)
    return index


def read_entries(path):
    """Read "Name", "Name<TAB>tier" or "Name<TAB>tier<TAB>kind".

    kind is "language" or "accent". They resolve identically, since Glottolog
    models most English accents as dialect nodes with their own Glottocodes, but
    the scheduler has to tell them apart to keep an accent and its base language
    out of the same session.
    """
    entries = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            name = parts[0].strip()
            tier = parts[1].strip() if len(parts) > 1 else ""
            kind = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "language"
            entries.append((name, tier, kind))
    return entries


def read_local_ids(path):
    """Read data/local-ids.tsv as input_name -> {local_id, reason}."""
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            out[row["input_name"]] = row
    return out


def read_lock(path):
    """Read the approval lock as input_name -> {glottocode, matched_name}."""
    lock = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            lock[row["input_name"]] = row
    return lock


def write_lock(path, records):
    """Rewrite the lock from a run a human has just signed off on."""
    approved = [r for r in records if r["glottocode"]]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=LOCK_FIELDS, delimiter="\t",
            extrasaction="ignore", lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(approved)
    return len(approved)


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


def apply_local_ids(records, local_ids):
    """Give entries Glottolog does not model their recorded local identifier.

    Glottolog is the join key for everything, and it has no node for a handful of
    varieties the pool wants. Scottish English is the first. Those entries get an
    id of the form "<base glottocode>-<place>", written down with a reason in
    data/local-ids.tsv, and a footprint drawn by hand.

    A real Glottolog match always wins, so a row left here after Glottolog adds
    the node stops being used rather than quietly holding a stale footprint.
    """
    for rec in records:
        if rec["status"] == "ok":
            continue
        entry = local_ids.get(rec["input_name"])
        if entry:
            rec["status"] = "local"
            rec["glottocode"] = entry["local_id"]
            rec["candidates"] = entry["reason"]
    return records


def apply_lock(records, lock):
    """Flag approved entries whose Glottocode has moved upstream.

    The lock file is the human approval record for this step. Once an entry has
    been checked, its Glottocode is the join key under every clip, polygon and
    approved puzzle, so an upstream re-code is a breaking change and has to be
    re-approved rather than silently adopted.
    """
    for rec in records:
        locked = lock.get(rec["input_name"])
        if locked and rec["glottocode"] != locked["glottocode"]:
            rec["status"] = "drifted"
            rec["candidates"] = (
                f"approved as {locked['glottocode']} ({locked['matched_name']}), "
                f"now resolves to {rec['glottocode'] or 'nothing'} "
                f"({rec['matched_name'] or rec['status']})"
            )
    return records


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
    ap.add_argument(
        "--local-ids",
        help="TSV of entries Glottolog does not model, with an id and a written reason",
    )
    ap.add_argument("--lock", help="approved input_name/glottocode TSV; drift from it needs a human")
    ap.add_argument(
        "--write-lock",
        action="store_true",
        help="rewrite --lock from this run, recording a human sign-off",
    )
    args = ap.parse_args()

    levels = {lvl.strip() for lvl in args.levels.split(",") if lvl.strip()}
    rows = fetch_table(args.cache, args.refresh)
    index = build_index(rows, levels)
    names_list = list(index.keys())
    entries = read_entries(args.entries)

    out_rows = []
    for name, tier, kind in entries:
        status, row, cands = resolve(name, index, names_list, args.cutoff)
        rec = {f: "" for f in OUT_FIELDS}
        rec.update({
            "input_name": name, "tier": tier, "kind": kind,
            "status": status, "candidates": cands,
        })
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

    if args.local_ids and os.path.exists(args.local_ids):
        apply_local_ids(out_rows, read_local_ids(args.local_ids))

    if args.write_lock:
        if not args.lock:
            raise SystemExit("--write-lock needs --lock PATH to write to")
        n = write_lock(args.lock, out_rows)
        sys.stderr.write(f"wrote {n} approved entries to {args.lock}\n")
    elif args.lock and os.path.exists(args.lock):
        apply_lock(out_rows, read_lock(args.lock))

    # Counted after the lock check, so drift shows up in the summary and the gate.
    counts = dict.fromkeys(STATUSES, 0)
    for rec in out_rows:
        counts[rec["status"]] += 1

    stream = sys.stdout if args.out == "-" else open(args.out, "w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(stream, fieldnames=OUT_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(out_rows)
    finally:
        if stream is not sys.stdout:
            stream.close()

    total = len(entries)
    needs_human = sum(n for status, n in counts.items() if status not in APPROVED)
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
