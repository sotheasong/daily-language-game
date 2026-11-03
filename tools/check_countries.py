#!/usr/bin/env python3
"""Check each footprint against the countries Glottolog says the language is in.

Finds the gap class Asher's atlas creates by design: it maps vernacular speech
communities, so a colonial language loses every country where it is a lingua
franca rather than a mother tongue. Portuguese comes out as Portugal plus Brazil
with nothing in Angola or Mozambique.

Two polygons overlap if either one has a vertex inside the other, which catches
both a small footprint inside a large country and a large footprint spanning
several. Dev tool, not pipeline.
"""

import csv
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [p[0] for p in polys if p and len(p[0]) >= 4]


def inside(pt, ring):
    x, y = pt
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-18) + xi:
            hit = not hit
        j = i
    return hit


def bbox(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def overlaps(rings_a, rings_b):
    boxes_b = [(r, bbox(r)) for r in rings_b]
    boxes_a = [(r, bbox(r)) for r in rings_a]
    for ra, ba in boxes_a:
        for rb, bb in boxes_b:
            if ba[2] < bb[0] or ba[0] > bb[2] or ba[3] < bb[1] or ba[1] > bb[3]:
                continue
            step_a = max(1, len(ra) // 60)
            step_b = max(1, len(rb) // 60)
            if any(inside(p, rb) for p in ra[::step_a]):
                return True
            if any(inside(p, ra) for p in rb[::step_b]):
                return True
    return False


def main():
    feats = {f["properties"]["entry"]: f for f in
             json.load(open(os.path.join(REPO, "data", "footprints.geojson")))["features"]}
    res = {r["input_name"]: r for r in
           csv.DictReader(open(os.path.join(REPO, "data", "resolved.tsv")), delimiter="\t")}
    ne = json.load(open(os.path.join(REPO, ".cache", "geo",
                                     "ne_50m_admin_0_countries.geojson")))
    countries = {}
    for f in ne["features"]:
        p = f["properties"]
        iso = p.get("ISO_A2_EH") or p.get("ISO_A2") or ""
        if iso and iso != "-99":
            countries.setdefault(iso, []).extend(rings(f["geometry"]))

    out = []
    for name, feat in sorted(feats.items()):
        listed = [c for c in (res[name]["countries"] or "").split(";") if c]
        if not listed:
            continue
        fr = rings(feat["geometry"])
        missing = [iso for iso in listed
                   if iso in countries and not overlaps(fr, countries[iso])]
        if missing:
            out.append((len(missing), len(listed), name, missing))

    out.sort(key=lambda r: (-r[0], r[2]))
    print(f"{'entry':<24}{'missed':>7}{'listed':>7}   countries with no footprint")
    for n_missing, n_listed, name, missing in out:
        print(f"{name:<24}{n_missing:>7}{n_listed:>7}   {' '.join(missing[:18])}")
    print(f"\n{len(out)} of {len(feats)} entries miss a country Glottolog lists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
