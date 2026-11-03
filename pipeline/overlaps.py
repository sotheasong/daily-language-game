#!/usr/bin/env python3
"""Report entry pairs that share too much territory to sit in one session.

A session is five rounds, each a different clip, each one pin. Two entries whose
footprints coincide ask the player to pin the same place twice, which wastes a
round and makes the session feel broken. That is a scheduling constraint, not a
reason to drop an entry: Hindi and Indian English both belong in the pool, they
just must not come up on the same day.

Territory comes from Glottolog's Countries column, overridden by the
hand-authored data/footprints.tsv where Glottolog is empty or too coarse.

This is a first pass at country granularity, which is all that is available
before polygons are drawn. It cannot tell Madrid from Barcelona. Once footprints
exist, replace the score with real polygon intersection over the weighted areas.

Stdlib only. No pip install.
"""

import argparse
import csv
import itertools
import os
import sys

from pipeline import sources

DEFAULT_FOOTPRINTS = os.path.join(sources.DATA_DIR, "footprints.tsv")
OUT_FIELDS = ["entry_a", "entry_b", "score", "shared", "territory_a", "territory_b"]


def territories_overlap(a, b):
    """True when two ISO codes cover any common ground.

    A bare country code contains its own subdivisions, so GB and GB-SCT overlap.
    Two different subdivisions of one country do not, which is the whole reason
    this is not a string comparison: Scotland and Wales are both GB.
    """
    if a == b:
        return True
    country_a, country_b = a.split("-")[0], b.split("-")[0]
    if country_a != country_b:
        return False
    return "-" not in a or "-" not in b


def collisions(territories, threshold=0.5):
    """Return pairs scoring at or above threshold, worst first.

    The score is the share of the two territory lists that coincides, so a pair
    of entries covering exactly the same ground scores 1.0 and a small language
    inside a much larger one's range scores low. Range size is deliberately part
    of the signal, because an entry spoken in one country and an entry spoken in
    thirty have different correct pins even where they overlap.
    """
    found = []
    for (name_a, set_a), (name_b, set_b) in itertools.combinations(sorted(territories.items()), 2):
        if not set_a or not set_b:
            continue
        shared = sorted(
            code for code in set_a | set_b
            if any(territories_overlap(code, other) for other in (set_b if code in set_a else set_a))
        )
        if not shared:
            continue
        score = round(len(shared) / len(set_a | set_b), 3)
        if score >= threshold:
            found.append({
                "a": name_a, "b": name_b, "score": score, "shared": shared,
                "territory_a": sorted(set_a), "territory_b": sorted(set_b),
            })
    return sorted(found, key=lambda p: (-p["score"], p["a"], p["b"]))


def read_footprints(path):
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.startswith("#")]
    return {r["input_name"]: r for r in csv.DictReader(lines, delimiter="\t")}


def build_territories(entries, footprints):
    """Territory set per entry, hand-authored rows winning over Glottolog."""
    out = {}
    for entry in entries:
        name = entry["input_name"]
        if name in footprints:
            codes = footprints[name]["territory"].split()
        else:
            codes = [c for c in entry.get("countries", "").split(";") if c]
        out[name] = {c.strip() for c in codes if c.strip()}
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("resolved", help="resolved.tsv from pipeline.glottocodes")
    ap.add_argument("-o", "--out", default="-", help="output TSV path, default stdout")
    ap.add_argument("--footprints", default=DEFAULT_FOOTPRINTS, help="hand-authored territory spec")
    ap.add_argument(
        "--threshold", type=float, default=0.5,
        help="report pairs at or above this overlap score, 0-1",
    )
    args = ap.parse_args()

    entries = sources.load_csv(args.resolved, delimiter="\t")
    footprints = read_footprints(args.footprints) if os.path.exists(args.footprints) else {}
    territories = build_territories(entries, footprints)
    pairs = collisions(territories, args.threshold)

    stream = sys.stdout if args.out == "-" else open(args.out, "w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(stream, fieldnames=OUT_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for p in pairs:
            writer.writerow({
                "entry_a": p["a"], "entry_b": p["b"], "score": p["score"],
                "shared": " ".join(p["shared"]),
                "territory_a": " ".join(p["territory_a"]),
                "territory_b": " ".join(p["territory_b"]),
            })
    finally:
        if stream is not sys.stdout:
            stream.close()

    blank = [n for n, t in territories.items() if not t]
    sys.stderr.write(
        f"\n{len(entries)} entries, {len(pairs)} pairs at or above {args.threshold}\n"
    )
    if blank:
        sys.stderr.write(
            f"{len(blank)} entries have no territory and were skipped: {', '.join(sorted(blank))}\n"
        )
    # A report, not a gate. Overlap is scheduled around, not fixed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
