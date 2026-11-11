#!/usr/bin/env python3
"""Reference scorer for one round, and the constants being tuned.

A round is one clip and one pin. The pin is scored against the entry's weighted
footprint: inside, by how much of the language is spoken there; outside, by how
far it missed.

    inside   MAX_POINTS x (FLOOR + (1 - FLOOR) x weight)
    outside  MAX_POINTS x FLOOR x exp(-distance / DECAY_KM)

The two meet at the border without a step, which matters. A cliff at the edge
teaches players to aim at borders rather than at where a language is spoken.

`weight` is the cell's value from pipeline.weights, already scaled so the
densest cell in that entry is 1.0.

This is the reference implementation. The Worker will reimplement it, so the
shape lives here where it can be tuned against real pins rather than guessed.

Stdlib only. No pip install.
"""

import math

EARTH_KM = 6371.0088

# Tuned 2026-11-03 against data/test-pins.tsv. See tools/tune_scoring.py.
MAX_POINTS = 5000
FLOOR = 0.6          # share of the maximum a pin earns for landing inside at all
DECAY_KM = 1500.0    # distance at which an outside pin keeps 1/e of the floor


# A harder tier is worth more, so a small footprint is not a punishment.
# Measured 2026-11-03: the median footprint is 370,745 km2 at tier 1, 91,283 at
# tier 2 and 50,064 at tier 3, which makes a pin 4.1 and 7.4 times less likely
# to land inside by area alone. Maltese is 313 km2 against Spanish at 10 million.
#
# These do not compensate in full. A player does not pin at random, so paying
# seven times for a tier 3 round would make the other four pointless. They are
# the fourth root of the area ratio: a judgement, anchored on a measurement.
TIER_WEIGHT = {"1": 1.0, "2": 1.4, "3": 1.6}


def tier_weight(tier):
    """Relative worth of a round at this tier. Unknown tiers get no bonus."""
    return TIER_WEIGHT.get(str(tier), 1.0)


def session_multipliers(tiers):
    """Scale a session's tier weights so they average 1.

    Tier weighting alone would make a tier 3 heavy day worth more than a tier 1
    day, and for a daily game that breaks the one number players compare. Scaled
    within the session, a hard round is worth more than the others *that day*
    and every day still totals the same maximum.
    """
    raw = [tier_weight(t) for t in tiers]
    total = sum(raw)
    if total <= 0:
        return [1.0] * len(raw)
    return [value * len(raw) / total for value in raw]


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Wraps the antimeridian correctly."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def point_in_rings(lat, lon, rings):
    """Even-odd point in polygon, so a ring inside a ring is a hole."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat) and \
                    lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-18) + xi:
                inside = not inside
            j = i
    return inside


def _segment_km(lat, lon, ax, ay, bx, by):
    """Distance to a segment, measured in the local tangent plane."""
    k = math.cos(math.radians(lat))
    px, py = lon * k, lat
    x1, y1 = ax * k, ay
    x2, y2 = bx * k, by
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    nx, ny = x1 + t * dx, y1 + t * dy
    return haversine(lat, lon, ny, nx / k if k else nx)


def nearest_km(lat, lon, rings):
    """Distance from a pin to the footprint, zero when it is inside."""
    if point_in_rings(lat, lon, rings):
        return 0.0
    best = float("inf")
    for ring in rings:
        for i in range(len(ring) - 1):
            best = min(best, _segment_km(lat, lon, ring[i][0], ring[i][1],
                                         ring[i + 1][0], ring[i + 1][1]))
    return best


# How far to look for a weight when a pin's own cell is empty. The grid fills
# only cells whose centre is inside the polygon, so a coastal city can sit in an
# empty cell while the polygon plainly contains it.
BORROW_RADIUS = 3


def weight_at(grid, lat, lon):
    """Weight at a point, borrowing from the nearest filled cell if need be."""
    origin_lon, origin_lat = grid["origin"]
    cell = grid["cell"]
    if grid.get("wrapped") and lon < 0:
        # The grid was built in unwrapped space because the footprint crosses
        # the date line. Alaska at -150 is 210 in that space.
        lon += 360.0
    ix = int(math.floor((lon - origin_lon) / cell))
    iy = int(math.floor((lat - origin_lat) / cell))
    cells = grid["cells"]
    here = cells.get((ix, iy))
    if here is not None:
        return here
    for radius in range(1, BORROW_RADIUS + 1):
        best, best_d2 = None, None
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue
                value = cells.get((ix + dx, iy + dy))
                if value is None:
                    continue
                d2 = dx * dx + dy * dy
                if best_d2 is None or d2 < best_d2:
                    best, best_d2 = value, d2
        if best is not None:
            return best
    return 0.0


def is_inside(grid, lat, lon, in_polygon):
    """True when a pin is both within the footprint and on land.

    Asher draws smooth blobs, so a footprint encloses the water between islands.
    The density grid already knows the difference: a cell exists only where the
    footprint meets a country, so a zero weight there is open water.
    """
    return bool(in_polygon) and weight_at(grid, lat, lon) > 0.0


def score(inside, weight, distance_km, multiplier=1.0,
          max_points=MAX_POINTS, floor=FLOOR, decay_km=DECAY_KM):
    """Points for one pin.

    `multiplier` is the round's worth relative to the others in its session,
    from session_multipliers(). At 1.0 this is the unweighted score.
    """
    if inside:
        base = max_points * (floor + (1.0 - floor) * max(0.0, min(1.0, weight)))
    else:
        base = max_points * floor * math.exp(-max(0.0, distance_km) / decay_km)
    return base * multiplier
