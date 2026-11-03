#!/usr/bin/env python3
"""Build a speaker-density grid per entry, for scoring and for the reveal.

The scoring design wants a density-weighted footprint, so pinning Mexico City
for Spanish beats pinning rural Patagonia, and the reveal shades the same way:
dark where the language is actually spoken, pale where the footprint merely
reaches. One grid serves both.

The unit has to be a cell, not a polygon part. Khmer's main part spans Cambodia
and Thailand as one piece, so a per-part weight cannot shade them differently.

Weight of a cell is the population living in it times the share of this
language's speakers who live in that country:

    weight = population x share

**Population alone is the wrong answer, and that is the point.** Khmer's
footprint reaches into northeastern Thailand and the Mekong Delta, both far more
populous than rural Cambodia, so weighting by people alone shades Vietnam
darkest for Khmer. The share is what separates where a language is spoken from
where people happen to live, and it is hand-authored in data/shares.tsv because
no free dataset carries speakers per country.

Population comes from Natural Earth, which is public domain: POP_EST per country
for the total, and 7,342 populated places for where the urban share of it sits.
The rest is spread evenly over the country's land, so rural Cambodia is not
empty just because Natural Earth lists only 19 Cambodian towns.

Stdlib only. No pip install.
"""

import argparse
import collections
import csv
import json
import math
import os
import sys

from pipeline import sources

Grid = collections.namedtuple("Grid", "lon0 lat0 cell cols rows")

NE_ADMIN0 = "ne_50m_admin_0_countries"
NE_PLACES = "ne_10m_populated_places_simple"
NATURAL_EARTH = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{name}.geojson"
)
DEFAULT_SHARES = os.path.join(sources.DATA_DIR, "shares.tsv")

# Enough cells to shade a footprint convincingly without shipping a raster.
# Spanish spans a third of the planet; 2,400 cells put it at roughly 60 km,
# which is finer than the scoring formula can use anyway, since the weight only
# modulates an inside-score by plus or minus 40 percent.
TARGET_CELLS = 2400
MIN_CELL = 0.02
# Caps the grid for a footprint scattered across the planet.
MAX_DIM = 420


def grid_for(bbox, area_km2, target=TARGET_CELLS, min_cell=MIN_CELL):
    """Pick a cell size that puts about `target` cells inside the footprint.

    Sized by the ground the footprint actually covers, not by its bounding box.
    Spanish's bbox runs California to Patagonia and is mostly ocean, so sizing
    to the bbox gave the whole language 181 cells of roughly 250 km each.

    MAX_DIM caps the grid for a footprint scattered across the planet. Yiddish
    is small patches on three continents, and a cell sized to its area would
    need thousands of columns to reach across the bbox.
    """
    lon0, lat0, lon1, lat1 = bbox
    width = max(lon1 - lon0, 1e-3)
    height = max(lat1 - lat0, 1e-3)
    mid_lat = (lat0 + lat1) / 2
    km_per_deg2 = (111.32 ** 2) * max(math.cos(math.radians(mid_lat)), 0.05)
    cell = math.sqrt(max(area_km2, 1.0) / target / km_per_deg2)
    cell = max(cell, min_cell, width / MAX_DIM, height / MAX_DIM)
    cols = max(1, int(math.ceil(width / cell)))
    rows = max(1, int(math.ceil(height / cell)))
    return Grid(lon0=lon0, lat0=lat0, cell=cell, cols=cols, rows=rows)


def rasterise(rings, grid):
    """Return the set of (ix, iy) cells whose centre falls inside the rings.

    Scanline with the even-odd rule, so a ring inside a ring cuts a hole. Doing
    it per row rather than per cell means each edge is touched once for the rows
    it spans, instead of once for every cell in the grid.
    """
    edges = []
    for ring in rings:
        for i in range(len(ring) - 1):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))
        if ring and (ring[0][0], ring[0][1]) != (ring[-1][0], ring[-1][1]):
            x1, y1 = ring[-1][0], ring[-1][1]
            x2, y2 = ring[0][0], ring[0][1]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))

    filled = set()
    for iy in range(grid.rows):
        y = grid.lat0 + (iy + 0.5) * grid.cell
        crossings = []
        for x1, y1, x2, y2 in edges:
            if (y1 > y) == (y2 > y):
                continue
            crossings.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        if not crossings:
            continue
        crossings.sort()
        for i in range(0, len(crossings) - 1, 2):
            left, right = crossings[i], crossings[i + 1]
            ix0 = int(math.floor((left - grid.lon0) / grid.cell - 0.5)) + 1
            ix1 = int(math.floor((right - grid.lon0) / grid.cell - 0.5))
            for ix in range(max(ix0, 0), min(ix1, grid.cols - 1) + 1):
                filled.add((ix, iy))
    return filled


# Within a country, population decides where the shading is darker. Compressed,
# because Phnom Penh against a rural cell is 70 to 1 drawn linearly and the whole
# country goes pale around one dot, and capped for the same reason.
URBAN_EXPONENT = 0.35
URBAN_FLOOR = 0.6
URBAN_CEILING = 2.5


def population_shares(cells):
    """Derive shares from population when no row was authored.

    Right whenever one country holds nearly all the speakers, which is most
    entries. Hindi is 100% India by population too.
    """
    by_country = collections.defaultdict(float)
    for iso, population in cells.values():
        by_country[iso] += population
    total = sum(by_country.values())
    if total <= 0:
        return {}
    return {iso: pop / total for iso, pop in by_country.items()}


def apply_shares(cells, shares):
    """Turn {cell: (iso, population)} into {cell: weight}.

    A country's share sets the level, so Cambodia reads about 15 times Thailand
    for Khmer even though Isan is populous. Within a country, population only
    modulates, so a capital is a hotspot rather than the only lit cell.

    Deliberately not a density. Dividing by a country's cell count crowns small
    dense ones, and El Salvador outshining Mexico is the wrong answer for a game
    that scores a pin by how many speakers are near it.
    """
    counts = collections.Counter(iso for iso, _ in cells.values())
    totals = collections.defaultdict(float)
    for iso, population in cells.values():
        totals[iso] += population

    out = {}
    for cell, (iso, population) in cells.items():
        share = shares.get(iso, 0.0)
        if share <= 0 or not counts[iso]:
            continue
        mean = totals[iso] / counts[iso]
        if mean > 0 and population > 0:
            boost = (population / mean) ** URBAN_EXPONENT
        else:
            boost = URBAN_FLOOR
        out[cell] = share * min(max(boost, URBAN_FLOOR), URBAN_CEILING)
    return out


def normalise(raw):
    """Scale so the densest cell is 1.0, dropping cells with no weight."""
    top = max(raw.values()) if raw else 0.0
    if top <= 0:
        return {}
    return {k: v / top for k, v in raw.items() if v > 0}


def ring_area_km2(ring):
    """Shoelace on an equirectangular projection, corrected by mean latitude."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = math.cos(math.radians(lat0))
    total = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i][0] * k, ring[i][1]
        x2, y2 = ring[(i + 1) % len(ring)][0] * k, ring[(i + 1) % len(ring)][1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0 * (111.32 ** 2)


def rings_of(geometry):
    polys = (geometry["coordinates"] if geometry["type"] == "MultiPolygon"
             else [geometry["coordinates"]])
    return [[tuple(p[:2]) for p in poly[0]] for poly in polys if poly and len(poly[0]) >= 4]


def read_shares(path):
    """entry -> {ISO: share}. Shares are relative and get normalised per entry."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.startswith("#")]
    for row in csv.DictReader(lines, delimiter="\t"):
        share = {}
        for pair in row["shares"].split():
            iso, _, value = pair.partition(":")
            share[iso] = float(value)
        total = sum(share.values()) or 1.0
        out[row["input_name"]] = {k: v / total for k, v in share.items()}
    return out


def load_natural_earth(name, cache_dir, refresh=False):
    path = os.path.join(cache_dir, "geo", f"{name}.geojson")
    sources.fetch(NATURAL_EARTH.format(name=name), path, min_bytes=100_000,
                  refresh=refresh, label=f"Natural Earth {name}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def country_table(admin0):
    """ISO -> {rings, bbox, rural_per_km2 (filled in later), pop, area}."""
    table = {}
    for feature in admin0["features"]:
        props = feature["properties"]
        iso = props.get("ISO_A2_EH") or props.get("ISO_A2") or ""
        if not iso or iso == "-99":
            continue
        rings = rings_of(feature["geometry"])
        if not rings:
            continue
        entry = table.setdefault(iso, {"rings": [], "pop": 0.0, "area": 0.0, "urban": 0.0})
        entry["rings"].extend(rings)
        entry["pop"] += props.get("POP_EST") or 0.0
        entry["area"] += sum(ring_area_km2(r) for r in rings)
    for entry in table.values():
        xs = [p[0] for r in entry["rings"] for p in r]
        ys = [p[1] for r in entry["rings"] for p in r]
        entry["bbox"] = (min(xs), min(ys), max(xs), max(ys))
    return table


def add_cities(table, places):
    cities = []
    for feature in places["features"]:
        props = feature["properties"]
        iso = props.get("iso_a2") or ""
        pop = props.get("pop_max") or 0
        coords = feature.get("geometry", {}).get("coordinates")
        if not coords or not pop:
            continue
        cities.append((coords[0], coords[1], float(pop), iso))
        if iso in table:
            table[iso]["urban"] += float(pop)
    for entry in table.values():
        rural = max(entry["pop"] - entry["urban"], 0.0)
        entry["rural_per_km2"] = rural / entry["area"] if entry["area"] > 0 else 0.0
    return cities


def cell_area_km2(grid, iy):
    lat = grid.lat0 + (iy + 0.5) * grid.cell
    return (grid.cell * 111.32) ** 2 * max(math.cos(math.radians(lat)), 0.02)


def build_entry(feature, grid, table, cities, share_map):
    """Return (cells, by_country, unauthored) for one entry."""
    rings = rings_of(feature["geometry"])
    inside = rasterise(rings, grid)
    if not inside:
        return {}, {}, False

    lon1 = grid.lon0 + grid.cols * grid.cell
    lat1 = grid.lat0 + grid.rows * grid.cell
    owner = {}
    for iso, country in table.items():
        box = country["bbox"]
        if box[2] < grid.lon0 or box[0] > lon1 or box[3] < grid.lat0 or box[1] > lat1:
            continue
        for cell in rasterise(country["rings"], grid):
            if cell in inside and cell not in owner:
                owner[cell] = iso

    urban = collections.defaultdict(float)
    for lon, lat, pop, _iso in cities:
        if not (grid.lon0 <= lon < lon1 and grid.lat0 <= lat < lat1):
            continue
        cell = (int((lon - grid.lon0) / grid.cell), int((lat - grid.lat0) / grid.cell))
        if cell in inside:
            urban[cell] += pop

    populations = {}
    for cell in inside:
        iso = owner.get(cell)
        if iso is None:
            continue
        populations[cell] = (
            iso,
            table[iso]["rural_per_km2"] * cell_area_km2(grid, cell[1]) + urban[cell],
        )

    shares = share_map if share_map is not None else population_shares(populations)
    raw = apply_shares(populations, shares)
    present = {iso: v for iso, v in shares.items()
               if any(p[0] == iso for p in populations.values()) and v > 0}
    total = sum(present.values()) or 1.0
    ranked = {k: round(v / total, 4)
              for k, v in sorted(present.items(), key=lambda kv: -kv[1])}
    runner_up = list(ranked.values())[1] if len(ranked) > 1 else 0.0
    # Only worth a human's time when the split is real. Hindi is 100 percent
    # India by population alone and needs no row.
    return normalise(raw), ranked, share_map is None and runner_up >= 0.10



def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("footprints", nargs="?",
                    default=os.path.join(sources.DATA_DIR, "footprints.geojson"))
    ap.add_argument("-o", "--out", default=os.path.join(sources.DATA_DIR, "density.json"))
    ap.add_argument("--shares", default=DEFAULT_SHARES)
    ap.add_argument("--cache-dir", default=sources.CACHE_DIR)
    ap.add_argument("--target-cells", type=int, default=TARGET_CELLS)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    with open(args.footprints, encoding="utf-8") as fh:
        footprints = json.load(fh)
    table = country_table(load_natural_earth(NE_ADMIN0, args.cache_dir, args.refresh))
    cities = add_cities(table, load_natural_earth(NE_PLACES, args.cache_dir, args.refresh))
    shares = read_shares(args.shares)

    out, unauthored, empty = [], [], []
    for feature in footprints["features"]:
        name = feature["properties"]["entry"]
        rings = rings_of(feature["geometry"])
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        area = sum(ring_area_km2(r) for r in rings)
        grid = grid_for((min(xs), min(ys), max(xs), max(ys)), area, args.target_cells)
        cells, by_country, needs = build_entry(
            feature, grid, table, cities, shares.get(name))
        if not cells:
            empty.append(name)
            continue
        if needs:
            unauthored.append((name, feature["properties"]["tier"], by_country))
        out.append({
            "entry": name,
            "tier": feature["properties"]["tier"],
            "kind": feature["properties"]["kind"],
            "origin": [round(grid.lon0, 4), round(grid.lat0, 4)],
            "cell": round(grid.cell, 5),
            "cols": grid.cols, "rows": grid.rows,
            "by_country": by_country,
            "authored": name in shares,
            "cells": [[ix, iy, round(w, 3)] for (ix, iy), w in sorted(cells.items())],
        })

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))

    size = os.path.getsize(args.out)
    total_cells = sum(len(e["cells"]) for e in out)
    sys.stderr.write(
        f"\n{len(out)} entries, {total_cells:,} cells, {size / 1e6:.1f} MB\n"
    )
    if empty:
        sys.stderr.write(f"No cells for {len(empty)}: {', '.join(empty)}\n")
    if unauthored:
        sys.stderr.write(
            f"\n{len(unauthored)} entries span several countries with no authored share, "
            "so they are weighted by population alone:\n"
        )
        for name, tier, by_country in sorted(unauthored, key=lambda r: (r[1], r[0])):
            top = "  ".join(f"{k} {v:.0%}" for k, v in list(by_country.items())[:4])
            sys.stderr.write(f"  T{tier} {name:<24} {top}\n")
    # Every entry needs a weighted footprint, or its rounds cannot be scored.
    return 1 if empty else 0


if __name__ == "__main__":
    sys.exit(main())
