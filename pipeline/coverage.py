#!/usr/bin/env python3
"""Report which entries have a language-area polygon and which need one drawn.

Scoring puts a pin inside a weighted multipolygon, so every entry needs a
footprint. Glottography publishes those as CC BY 4.0 CLDF datasets keyed by
Glottocode, one dataset per source atlas, with known gaps. This answers, entry by
entry, whether a polygon exists and where, so the gaps can be filled with admin
boundaries before anyone draws anything.

It reads only the small languages.csv index from each dataset, about 2.4 MB in
total, not the roughly 400 MB of geojson.

Stdlib only. No pip install.
"""

import argparse
import csv
import os
import sys

from pipeline import glottocodes, sources

DEFAULT_REGISTRY = os.path.join(sources.DATA_DIR, "sources", "glottography.tsv")

COVERAGE = ("direct", "via_dialects", "via_parent", "none")

OUT_FIELDS = [
    "input_name",
    "tier",
    "glottocode",
    "matched_name",
    "level",
    "macroarea",
    "coverage",
    "n_sources",
    "sources",
    "country_shape_only",
    "inherited_point",
    "fallback_countries",
]


def classify(code, parent, children, index, kind="language"):
    """Return (coverage, sources) for one Glottocode.

    index maps a Glottocode to the list of "dataset/layer" names that draw it.
    kind is "language" or "accent", and an accent is never allowed to fall back
    to its base language's area, because the two differing is the whole reason
    the accent is a separate entry.
    """
    hits = index.get(code, [])
    if hits:
        return "direct", "; ".join(hits)
    child_hits = []
    for child in children:
        for src in index.get(child, []):
            child_hits.append(f"{src} [{child}]")
    if child_hits:
        # A union of the dialects is tighter than whatever sits above them, and
        # what sits above is often a whole family.
        return "via_dialects", "; ".join(child_hits)
    parent_hits = index.get(parent, []) if parent and kind != "accent" else []
    if parent_hits:
        return "via_parent", "; ".join(f"{src} [{parent}]" for src in parent_hits)
    return "none", ""


def is_country_shape_only(srcs, country_shape):
    """True when every dataset behind a footprint draws national outlines.

    Such a footprint is a border, not a speech community. It is defensible for a
    standard variety defined politically and misleading for everything else, so
    the report flags it rather than counting it as covered.
    """
    labels = [s.split(" [")[0] for s in srcs.split("; ") if s]
    return bool(labels) and all(label in country_shape for label in labels)


def has_inherited_coordinate(lat, lon, parent_lat, parent_lon):
    """True when an entry's point is just its parent's rather than its own.

    Glottolog gives a dialect node its parent language's coordinate when it has
    no separate one. That is harmless as classification and poison as a fallback
    centre, since it silently relocates every English accent to Derbyshire.
    """
    if not (lat and lon and parent_lat and parent_lon):
        return False
    return (lat, lon) == (parent_lat, parent_lon)


def build_area_index(registry, cache_dir, refresh=False):
    """Map Glottocode -> ["dataset/layer", ...] across every registered dataset.

    Adding a dataset means adding a registry row. Nothing here names one.
    """
    index, country_shape = {}, set()
    for row in registry:
        label = f"{row['dataset']}/{row['layer']}" if row["layer"] else row["dataset"]
        if (row.get("kind") or "language-area") == "country-shape":
            country_shape.add(label)
        stem = f"{row['dataset']}-{row['layer']}" if row["layer"] else row["dataset"]
        path = os.path.join(cache_dir, "glottography", f"{stem}.csv")
        sources.fetch(row["url"], path, min_bytes=200, refresh=refresh, label=label)
        for area in sources.load_csv(path):
            code = (area.get("Glottocode") or "").strip()
            if code:
                index.setdefault(code, []).append(label)
    return index, country_shape


def build_tree(rows):
    """Return (parent_of, children_of) from the Glottolog CLDF table.

    Language_ID is the parent language of a dialect, which is the only parent link
    this table carries. It reaches one level in each direction, covering the two
    mismatches that bite. An entry resolved to a dialect node whose area is drawn
    on the language, and a language drawn only through its dialects.

    Known limit: a language node has no Language_ID, so it has no parent here and
    via_parent can never fire for it. Climbing to a subfamily needs the Glottolog
    classification tree, which is a different file. Worth adding only if an entry
    turns out to be drawn at a level above its own, and none currently is.
    """
    parent_of, children_of = {}, {}
    for row in rows:
        code = row["Glottocode"]
        parent = (row.get("Language_ID") or "").strip()
        if parent and parent != code:
            parent_of[code] = parent
            children_of.setdefault(parent, []).append(code)
    return parent_of, children_of


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("resolved", help="resolved.tsv from pipeline.glottocodes")
    ap.add_argument("-o", "--out", default="-", help="output TSV path, default stdout")
    ap.add_argument("--sources", default=DEFAULT_REGISTRY, help="dataset registry TSV")
    ap.add_argument("--cache-dir", default=sources.CACHE_DIR, help="where downloads are cached")
    ap.add_argument("--refresh", action="store_true", help="re-download every dataset")
    args = ap.parse_args()

    registry = sources.read_registry(args.sources)
    index, country_shape = build_area_index(registry, args.cache_dir, args.refresh)

    table_path = os.path.join(args.cache_dir, "languages.csv")
    sources.fetch(
        glottocodes.CLDF_URL, table_path, min_bytes=500_000, refresh=args.refresh,
        label="Glottolog CLDF language table",
    )
    table = sources.load_csv(table_path)
    parent_of, children_of = build_tree(table)
    points = {r["Glottocode"]: (r["Latitude"], r["Longitude"]) for r in table}

    entries = sources.load_csv(args.resolved, delimiter="\t")
    out_rows = []
    counts = dict.fromkeys(COVERAGE, 0)
    for entry in entries:
        code = (entry.get("glottocode") or "").strip()
        if not code:
            status, srcs = "none", f"not resolved ({entry.get('status', '')})"
        else:
            status, srcs = classify(
                code, parent_of.get(code, ""), children_of.get(code, ()), index,
                kind=entry.get("kind") or "language",
            )
        counts[status] += 1
        out_rows.append({
            "input_name": entry.get("input_name", ""),
            "tier": entry.get("tier", ""),
            "glottocode": code,
            "matched_name": entry.get("matched_name", ""),
            "level": entry.get("level", ""),
            "macroarea": entry.get("macroarea", ""),
            "coverage": status,
            "n_sources": len(srcs.split("; ")) if srcs and status != "none" else 0,
            "sources": srcs,
            "inherited_point": "yes" if has_inherited_coordinate(
                entry.get("latitude", ""), entry.get("longitude", ""),
                *points.get(parent_of.get(code, ""), ("", "")),
            ) else "no",
            "country_shape_only": "yes" if is_country_shape_only(srcs, country_shape) else "no",
            "fallback_countries": entry.get("countries", ""),
        })

    stream = sys.stdout if args.out == "-" else open(args.out, "w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(
            stream, fieldnames=OUT_FIELDS, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(out_rows)
    finally:
        if stream is not sys.stdout:
            stream.close()

    summary = ", ".join(f"{counts[c]} {c}" for c in COVERAGE)
    sys.stderr.write(f"\n{len(out_rows)} entries: {summary}\n")
    inherited = [r for r in out_rows if r["inherited_point"] == "yes"]
    if inherited:
        sys.stderr.write(
            f"{len(inherited)} entries carry their parent's coordinate, not their own. "
            "Unusable as a fallback centre:\n"
        )
        for rec in inherited:
            sys.stderr.write(f"  {rec['input_name']} [{rec['glottocode']}]\n")
    borders = [r for r in out_rows if r["country_shape_only"] == "yes"]
    if borders:
        names = ", ".join(r["input_name"] for r in borders)
        sys.stderr.write(
            f"{len(borders)} footprint(s) rest on country outlines alone: {names}\n"
        )
    if counts["none"]:
        sys.stderr.write("No language area anywhere, needs an admin boundary or removal:\n")
        for rec in out_rows:
            if rec["coverage"] == "none":
                fallback = rec["fallback_countries"] or "no countries listed either"
                sys.stderr.write(
                    f"  {rec['input_name']} [{rec['glottocode']}] -> {fallback}\n"
                )
    # Non-zero while any entry is unscoreable, so this can gate a build.
    return 1 if counts["none"] else 0


if __name__ == "__main__":
    sys.exit(main())
