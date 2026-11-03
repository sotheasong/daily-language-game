#!/usr/bin/env python3
"""Assemble one footprint per entry from Glottography and Natural Earth.

Scoring puts a pin against a polygon, so every entry needs geometry. 117 of the
136 have a language area drawn by a Glottography dataset. The other 19 get an
admin boundary cut from Natural Earth, specified by hand in data/footprints.tsv.

Output is one GeoJSON FeatureCollection, one feature per entry.

Stdlib only. No pip install.
"""

import argparse
import collections
import json
import os
import re
import sys
import urllib.error

from pipeline import overlaps, sources

# A field runs until the next "word=" or the end, so a value may contain spaces.
_FIELD = re.compile(r"(\w+)=([^=]*?)(?=\s+\w+=|$)")

# What a dataset actually draws decides which one wins, and the registry records
# it per dataset. Reconstructed ranks last, below even national outlines: DiACL's
# Yiddish is a blob in eastern Poland, the pre-war heartland, and Yiddish is not
# spoken there now. For a game asking where a language is spoken today, a modern
# border is closer to the truth than an ancestral range inferred from phylogenetics.
#
# An unclassified dataset ranks below everything, so adding one to the registry
# without reviewing what it draws cannot quietly take over an entry's footprint.
KIND_RANK = {"language-area": 0, "historical-area": 1, "country-shape": 2, "reconstructed": 3}
UNCLASSIFIED = 4

# Within language-area, the global atlas wins, so neighbouring languages are drawn
# on one convention. Its contemporary layer is the one the game wants: measured
# 2026-11-02, the two layers are identical for 94 of the 99 entries it draws, and
# where they differ it is colonial spread. Traditional Portuguese is Portugal
# alone, which marks a Brazil pin wrong when 200 of 260 million speakers are
# Brazilian.
FIRST_CHOICE = "asher2007world/contemporary"

# Anything landing here has a footprint nobody should ship without looking at it.
NEEDS_REVIEW = ("reconstructed", "country-shape")


def pick_source(available, kinds):
    """Return the dataset an entry's polygon should come from."""
    if not available:
        return ""
    def rank(name):
        return (KIND_RANK.get(kinds.get(name, ""), UNCLASSIFIED),
                0 if name == FIRST_CHOICE else 1,
                name)
    return sorted(available, key=rank)[0]


def use_override(spec, coverage):
    """True when a hand-written selector should replace the automatic pick.

    The preference order over datasets cannot know that DiACL's Yiddish is the
    pre-war heartland, or that an entry resolved to North Azerbaijani should not
    carry Iranian Azerbaijan as well. Those are curation calls, so a row in
    data/footprints.tsv wins whether or not an atlas already draws the entry.

    A row with no selector carries territory for the overlap report only, and
    must not start redrawing anything.
    """
    return bool((spec or {}).get("selector"))


def needs_review(source_kind, spec):
    """True when a footprint rests on data nobody should ship unseen.

    Clears once a human writes a review date, or replaces the geometry outright.
    """
    spec = spec or {}
    if spec.get("selector") or spec.get("review"):
        return False
    return source_kind in NEEDS_REVIEW


def _perp_distance(point, start, end):
    """Distance from point to the segment start-end, in degrees."""
    (px, py), (x1, y1), (x2, y2) = point, start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    nx, ny = x1 + t * dx, y1 + t * dy
    return ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5


def simplify(points, tolerance):
    """Douglas-Peucker. Keeps the endpoints and anything further out than tolerance."""
    if len(points) < 3:
        return list(points)
    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        d = _perp_distance(points[i], points[0], points[-1])
        if d > worst:
            worst, index = d, i
    if worst <= tolerance:
        return [points[0], points[-1]]
    left = simplify(points[: index + 1], tolerance)
    right = simplify(points[index:], tolerance)
    return left[:-1] + right


def simplify_ring(ring, tolerance):
    """Simplify a closed ring, keeping it a closed ring.

    A small island inside the tolerance reduces to two points, which is a line.
    Point-in-polygon on a degenerate ring reports every pin as outside without
    erroring, so the footprint goes quietly wrong rather than loudly. Below a
    triangle, keep the original instead.
    """
    out = simplify(ring, tolerance)
    if out[0] != out[-1]:
        out.append(out[0])
    if len(out) < 4:
        return list(ring)
    return out


def parse_selector(selector):
    """Parse "admin1 iso_a2=GB region=A|B C" into (kind, {field: {values}}).

    Natural Earth admin-1 is province level, so there is no GB-SCT or ES-GA
    feature to look up by code. Scotland is 32 council areas sharing gu_a3=SCT
    and Galicia is 4 provinces sharing region=Galicia, which is why selection is
    by field rather than by ISO subdivision code.

    Values may contain spaces, as "Greater London" does, so a field's value runs
    until the next field rather than to the next space.
    """
    kind, _, rest = selector.strip().partition(" ")
    filters = {}
    for field, values in _FIELD.findall(rest):
        filters[field] = set(values.strip().split("|"))
    return kind, filters


def match_selector(props, filters):
    """True when a feature satisfies every field in the selector."""
    return all(str(props.get(field, "")) in values for field, values in filters.items())


GLOTTOGRAPHY_GEO = (
    "https://raw.githubusercontent.com/Glottography/{repo}/main/cldf/{layer}{variant}.geojson"
)
# A dataset splits its polygons by languoid level, and its languages.csv says
# which file holds each entry in its Speaker_Area column. Serbian, Croatian and
# Irish English are all drawn as dialects, so loading languages.geojson alone
# silently leaves them without geometry. Families are deliberately skipped:
# Indo-European is not a footprint anyone can pin at.
GEO_VARIANTS = ("languages", "dialects")
NATURAL_EARTH = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{name}.geojson"
)
NE_FILES = {
    "admin0": "ne_50m_admin_0_countries",
    "admin1": "ne_10m_admin_1_states_provinces",
}


def load_geojson(url, path, min_bytes, refresh=False, label=None):
    sources.fetch(url, path, min_bytes=min_bytes, refresh=refresh, label=label)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def area_index(doc):
    """Map Glottocode -> list of geometries from a Glottography languages.geojson."""
    index = {}
    for feature in doc.get("features", []):
        props = feature.get("properties") or {}
        code = (props.get("cldf:languageReference") or props.get("Glottocode") or "").strip()
        if code and feature.get("geometry"):
            index.setdefault(code, []).append(feature["geometry"])
    return index


def as_polygons(geometry):
    """Flatten a geometry to a list of polygons, each a list of rings."""
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"]]
    if kind == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def build_geometry(geometries, tolerance):
    """Merge geometries into one simplified MultiPolygon."""
    polys = []
    for geometry in geometries:
        for poly in as_polygons(geometry):
            rings = [simplify_ring([tuple(p[:2]) for p in ring], tolerance) for ring in poly]
            rings = [r for r in rings if len(r) >= 4]
            if rings:
                polys.append([[list(p) for p in ring] for ring in rings])
    return {"type": "MultiPolygon", "coordinates": polys} if polys else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("resolved", help="resolved.tsv from pipeline.glottocodes")
    ap.add_argument("--coverage", default=os.path.join(sources.DATA_DIR, "coverage.tsv"))
    ap.add_argument("--footprints", default=os.path.join(sources.DATA_DIR, "footprints.tsv"))
    ap.add_argument("-o", "--out", default=os.path.join(sources.DATA_DIR, "footprints.geojson"))
    ap.add_argument("--cache-dir", default=sources.CACHE_DIR)
    ap.add_argument(
        "--tolerance", type=float, default=0.01,
        help="simplification tolerance in degrees, about 1 km at the equator",
    )
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    registry = sources.read_registry(
        os.path.join(sources.DATA_DIR, "sources", "glottography.tsv"))
    kinds = {(f"{r['dataset']}/{r['layer']}" if r["layer"] else r["dataset"]):
             (r.get("kind") or "") for r in registry}

    entries = sources.load_csv(args.resolved, delimiter="\t")
    coverage = {r["input_name"]: r for r in sources.load_csv(args.coverage, delimiter="\t")}
    spec = overlaps.read_footprints(args.footprints)

    # Only the datasets the pool actually needs get downloaded.
    wanted = set()
    for entry in entries:
        row = coverage.get(entry["input_name"], {})
        if row.get("coverage") != "none":
            srcs = [s.split(" [")[0] for s in (row.get("sources") or "").split("; ") if s]
            if srcs:
                wanted.add(pick_source(srcs, kinds))
    atlases = {}
    for name in sorted(wanted):
        repo, _, layer = name.partition("/")
        stem = f"{repo}{'-' + layer if layer else ''}"
        index = {}
        for variant in GEO_VARIANTS:
            path = os.path.join(args.cache_dir, "geo", f"{stem}-{variant}.geojson")
            url = GLOTTOGRAPHY_GEO.format(
                repo=repo, layer=f"{layer}/" if layer else "", variant=variant)
            try:
                doc = load_geojson(url, path, min_bytes=200, refresh=args.refresh,
                                   label=f"{name} {variant}")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue          # not every dataset draws every level
                raise
            for code, geoms in area_index(doc).items():
                index.setdefault(code, []).extend(geoms)
        atlases[name] = index

    ne = {}
    for kind in sorted({parse_selector(r["selector"])[0] for r in spec.values() if r.get("selector")}):
        name = NE_FILES[kind]
        path = os.path.join(args.cache_dir, "geo", f"{name}.geojson")
        ne[kind] = load_geojson(
            NATURAL_EARTH.format(name=name), path, min_bytes=100_000,
            refresh=args.refresh, label=f"Natural Earth {kind}")

    features, missing = [], []
    for entry in entries:
        name = entry["input_name"]
        row = coverage.get(name, {})
        geometries, source = [], ""
        entry_spec = spec.get(name) or {}
        selector = entry_spec.get("selector") or ""
        if use_override(entry_spec, row.get("coverage", "")) or (
                row.get("coverage") == "none" and selector):
            kind, filters = parse_selector(selector)
            source = f"naturalearth/{kind}"
            for feature in ne[kind]["features"]:
                if match_selector(feature.get("properties") or {}, filters) and feature.get("geometry"):
                    geometries.append(feature["geometry"])
        elif row.get("coverage") != "none":
            srcs = [s.split(" [")[0] for s in (row.get("sources") or "").split("; ") if s]
            codes = [c.split(" [")[1].rstrip("]") if " [" in c else entry["glottocode"]
                     for c in (row.get("sources") or "").split("; ") if c]
            source = pick_source(srcs, kinds)
            index = atlases.get(source, {})
            for code in dict.fromkeys(codes + [entry["glottocode"]]):
                if code in index:
                    geometries = index[code]
                    break

        geometry = build_geometry(geometries, args.tolerance) if geometries else None
        if geometry is None:
            missing.append(name)
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "entry": name, "glottocode": entry["glottocode"], "tier": entry["tier"],
                "kind": entry["kind"], "source": source,
                "source_kind": kinds.get(source, "admin" if source.startswith("naturalearth") else ""),
                "needs_review": "yes" if needs_review(kinds.get(source, ""), entry_spec) else "no",
                "review": entry_spec.get("review", ""),
                "parts": len(geometry["coordinates"]),
            },
            "geometry": geometry,
        })

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)

    size = os.path.getsize(args.out)
    by_source = collections.Counter(f["properties"]["source"] for f in features)
    sys.stderr.write(
        f"\n{len(features)} of {len(entries)} entries have geometry, "
        f"{size / 1e6:.1f} MB at tolerance {args.tolerance}\n"
    )
    for src, n in by_source.most_common():
        sys.stderr.write(f"  {n:>4}  {src}\n")
    review = [f["properties"] for f in features if f["properties"]["needs_review"] == "yes"]
    if review:
        sys.stderr.write(
            f"\n{len(review)} footprints are a reconstruction or a national outline "
            "and need a human to look before they ship:\n"
        )
        for p in sorted(review, key=lambda x: (x["source_kind"], x["entry"])):
            sys.stderr.write(f"  {p['entry']:<22} {p['source']}  ({p['source_kind']})\n")
    if missing:
        sys.stderr.write(f"No geometry for {len(missing)}: {', '.join(missing)}\n")
    # Every entry needs a footprint or it cannot be scored.
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
