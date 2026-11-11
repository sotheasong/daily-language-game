#!/usr/bin/env python3
"""Score the worked pins in data/test-pins.tsv and check the bands come out in order.

The scoring constants were guesses written before there was any geometry to test
them against. This runs real pins through the real footprints and weights.

A band violation is a finding about the footprint or the weights, not about the
player: if Bangkok outscores Surin for Khmer, the footprint reaches too far west.

    python3 tools/tune_scoring.py              # the table
    python3 tools/tune_scoring.py --sweep      # what other constants would do
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import scoring

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANDS = ["bullseye", "core", "fringe", "near", "region", "wrong"]


def load():
    with open(os.path.join(REPO, "data", "footprints.geojson"), encoding="utf-8") as fh:
        feats = {f["properties"]["entry"]: f for f in json.load(fh)["features"]}
    with open(os.path.join(REPO, "data", "density.json"), encoding="utf-8") as fh:
        dens = {}
        for e in json.load(fh):
            dens[e["entry"]] = {
                "origin": e["origin"], "cell": e["cell"],
                "cells": {(c[0], c[1]): c[2] for c in e["cells"]},
            }
    with open(os.path.join(REPO, "data", "test-pins.tsv"), encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.startswith("#")]
    return feats, dens, list(csv.DictReader(lines, delimiter="\t"))


def tier_of(feats, entry):
    return feats[entry]["properties"].get("tier", "")


def rings_of(feature):
    geom = feature["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [[(p[0], p[1]) for p in poly[0]] for poly in polys if len(poly[0]) >= 4]


def evaluate(feats, dens, pins, floor, decay):
    out = []
    for row in pins:
        entry = row["entry"]
        lat, lon = float(row["lat"]), float(row["lon"])
        rings = rings_of(feats[entry])
        in_polygon = scoring.point_in_rings(lat, lon, rings)
        inside = scoring.is_inside(dens[entry], lat, lon, in_polygon)
        distance = 0.0 if inside else scoring.nearest_km(lat, lon, rings)
        weight = scoring.weight_at(dens[entry], lat, lon) if inside else 0.0
        points = scoring.score(inside, weight, distance, floor=floor, decay_km=decay)
        out.append({**row, "inside": inside, "km": distance,
                    "weight": weight, "points": points})
    return out


def violations(rows):
    bad = []
    by_entry = {}
    for row in rows:
        by_entry.setdefault(row["entry"], []).append(row)
    for entry, group in by_entry.items():
        group = sorted(group, key=lambda r: BANDS.index(r["band"]))
        for i in range(len(group) - 1):
            a, b = group[i], group[i + 1]
            if a["band"] != b["band"] and a["points"] < b["points"] - 1:
                bad.append((entry, a, b))
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--floor", type=float, default=scoring.FLOOR)
    ap.add_argument("--decay", type=float, default=scoring.DECAY_KM)
    args = ap.parse_args()

    feats, dens, pins = load()
    rows = evaluate(feats, dens, pins, args.floor, args.decay)

    print(f"floor {args.floor}  decay {args.decay:.0f} km  max {scoring.MAX_POINTS}\n")
    current = None
    for row in rows:
        if row["entry"] != current:
            current = row["entry"]
            tier = tier_of(feats, current)
            print(f"  {current}   tier {tier}, worth "
                  f"x{scoring.tier_weight(tier):.1f} against a tier 1 round")
        where = "inside" if row["inside"] else f"{row['km']:>6.0f} km out"
        print(f"    {row['band']:<9}{row['pin']:<24}{where:<15}"
              f"w {row['weight']:.2f}   {row['points']:>6.0f}")

    bad = violations(rows)
    print()
    if bad:
        print(f"{len(bad)} band violation(s):")
        for entry, a, b in bad:
            print(f"  {entry}: {a['band']} {a['pin']} scored {a['points']:.0f}, "
                  f"below {b['band']} {b['pin']} at {b['points']:.0f}")
    else:
        print("All bands in order.")

    if args.sweep:
        print("\nwhat the constants do to a wrong-but-reasonable pin:\n")
        print(f"  {'decay':>7}  " + "  ".join(f"{b:>9}" for b in BANDS))
        for decay in (600, 1000, 1500, 2200, 3000):
            swept = evaluate(feats, dens, pins, args.floor, decay)
            means = []
            for band in BANDS:
                vals = [r["points"] for r in swept if r["band"] == band]
                means.append(sum(vals) / len(vals) if vals else 0.0)
            print(f"  {decay:>7}  " + "  ".join(f"{m:>9.0f}" for m in means))
    return 0


if __name__ == "__main__":
    sys.exit(main())
