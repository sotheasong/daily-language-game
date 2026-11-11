#!/usr/bin/env python3
"""Build the data files the footprint reviewer loads.

One job, and one rule: **everything comes from the canonical files**. The
outline, the clip and the density grid must all be the same geometry. They were
not, once. The reviewer's outline was rebuilt at a 0.06 degree tolerance while
the density grid came from the 0.01 degree canonical file, so the two disagreed
by up to 6.4 km and the outline traced a visibly different shape from the
shading it was supposed to enclose. Re-simplifying here is how that happened,
so this script does not re-simplify anything.

    python3 tools/export_reviewer.py -o <dir>

Writes entries.json and world-fine.json. Dev tool, needs nothing but stdlib.
"""

import argparse
import base64
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import footprints as FP
from pipeline import weights as W

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")

# The basemap is the only thing simplified here, because it is scenery rather
# than the subject. 0.012 degrees is about 1.3 km, sub-pixel at the zoom a
# fitted footprint uses.
BASEMAP_TOLERANCE = 0.012


def load_tsv(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


def pack_grid(entry):
    """One byte per cell, 0 meaning outside, edges extended for interpolation."""
    cols, rows = entry["cols"], entry["rows"]
    field = W.extend_edges(
        {(c[0], c[1]): c[2] for c in entry["cells"]}, cols, rows, radius=2)
    buf = bytearray(cols * rows)
    for (ix, iy), weight in field.items():
        if 0 <= ix < cols and 0 <= iy < rows:
            buf[iy * cols + ix] = 1 + int(round(max(0.0, min(1.0, weight)) * 254))
    return {
        "o": entry["origin"], "cs": entry["cell"], "cw": cols, "ch": rows,
        "wr": entry["wrapped"], "bc": entry["by_country"], "au": entry["authored"],
        "g": base64.b64encode(bytes(buf)).decode("ascii"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", required=True, help="directory to write into")
    ap.add_argument("--precision", type=int, default=4,
                    help="decimal places for outline coordinates")
    ap.add_argument("--density", default=os.path.join(DATA, "density.json"),
                    help="density grid to pack; pass a coarser build to shrink the page")
    args = ap.parse_args()

    with open(os.path.join(DATA, "footprints.geojson"), encoding="utf-8") as fh:
        shapes = json.load(fh)["features"]
    with open(args.density, encoding="utf-8") as fh:
        density = {e["entry"]: e for e in json.load(fh)}
    resolved = {r["input_name"]: r for r in load_tsv("resolved.tsv")}

    collide = {}
    for row in load_tsv("overlaps.tsv"):
        collide.setdefault(row["entry_a"], []).append((row["entry_b"], row["score"]))
        collide.setdefault(row["entry_b"], []).append((row["entry_a"], row["score"]))

    rounding = args.precision
    entries = []
    for shape in shapes:
        props = shape["properties"]
        name = props["entry"]
        row = resolved[name]
        record = {
            "n": name, "g": props["glottocode"], "t": props["tier"],
            "k": props["kind"], "s": props["source"],
            "sk": props.get("source_kind", ""), "rv": props.get("review", ""),
            "nr": props.get("needs_review", "no"), "ma": row["matched_name"],
            "lv": row["level"], "st": row["status"], "cn": row["countries"],
            "cl": sorted(collide.get(name, []), key=lambda p: -float(p[1]))[:6],
            # Straight from the canonical file. Not re-simplified, ever.
            "p": [[[round(x, rounding), round(y, rounding)] for x, y in ring]
                  for poly in shape["geometry"]["coordinates"] for ring in poly[:1]],
        }
        if name in density:
            record.update(pack_grid(density[name]))
        entries.append(record)

    world = []
    with open(os.path.join(REPO, ".cache", "geo",
                           "ne_10m_admin_0_countries.geojson"), encoding="utf-8") as fh:
        for feature in json.load(fh)["features"]:
            geom = feature["geometry"]
            polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                     else [geom["coordinates"]])
            keep = []
            for poly in polys:
                ring = FP.simplify_ring([tuple(p[:2]) for p in poly[0]], BASEMAP_TOLERANCE)
                if len(ring) >= 4:
                    keep.append([[round(x, 4), round(y, 4)] for x, y in ring])
            if keep:
                world.append(keep)

    os.makedirs(args.out, exist_ok=True)
    for name, payload in (("entries.json", entries), ("world-fine.json", world)):
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        sys.stderr.write(f"{path}  {os.path.getsize(path) / 1e6:.2f} MB\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
