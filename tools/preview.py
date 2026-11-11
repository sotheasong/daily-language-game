#!/usr/bin/env python3
"""Render footprints and their density over a coastline basemap, to check by eye.

Bounding boxes catch a polygon on the wrong continent. They do not catch one
that is the wrong shape, inside out, or the right country's neighbour.

Three things make the plot read as a map rather than as a grid of bins:

  - the density is drawn at grid resolution and upscaled bicubically, so what
    shows is the kernel rather than the cells
  - it is then masked by the footprint polygon, so the field is smooth inside
    and the border stays a hard edge
  - everything is drawn at SUPERSAMPLE times the final size and downsampled with
    Lanczos, so coastlines are not staircases

Borders come from Natural Earth 10m, the finest free set.

Dev tool, not pipeline. Needs Pillow, which the pipeline itself does not.

    python3 tools/preview.py Khmer Spanish -o /tmp/check.png
    python3 tools/preview.py Khmer --shape          # outline only, no density
"""

import argparse
import json
import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import weights

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASEMAP = os.path.join(REPO, ".cache", "geo", "ne_10m_admin_0_countries.geojson")
FALLBACK_BASEMAP = os.path.join(REPO, ".cache", "geo", "ne_50m_admin_0_countries.geojson")
FOOTPRINTS = os.path.join(REPO, "data", "footprints.geojson")
DENSITY = os.path.join(REPO, "data", "density.json")

SEA, LAND, BORDER = (222, 233, 242), (238, 236, 230), (188, 190, 186)
EDGE, TEXT = (120, 26, 16), (34, 34, 34)
RAMP = [(253, 231, 201), (232, 137, 63), (140, 29, 16)]
PANEL = (520, 400)
SUPERSAMPLE = 3


def font(size):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def polygons(geometry):
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"]]
    if kind == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def shift_east(rings):
    """Move rings into unwrapped space, deciding per ring.

    Mirrors pipeline.weights.shift_east. A ring crossing the date line moves
    only its western half, a ring wholly in the west moves whole, and a ring
    straddling the prime meridian stays put.
    """
    out = []
    for ring in rings:
        xs = [p[0] for p in ring]
        if max(xs) - min(xs) > 180:
            out.append([[p[0] + 360.0 if p[0] < 0 else p[0], p[1]] for p in ring])
        elif max(xs) < 0:
            out.append([[p[0] + 360.0, p[1]] for p in ring])
        else:
            out.append([list(p[:2]) for p in ring])
    return out


def unwrap(geometry):
    """Return (polygons, wrapped) with the date line taken out of the way."""
    polys = polygons(geometry)
    xs = [p[0] for poly in polys for ring in poly for p in ring]
    if not xs or max(xs) - min(xs) <= 180:
        return polys, False
    return [shift_east(poly) for poly in polys], True


def bounds_of(polys):
    xs, ys = [], []
    for poly in polys:
        for ring in poly:
            for pt in ring:
                xs.append(pt[0]); ys.append(pt[1])
    return min(xs), min(ys), max(xs), max(ys)


def bounds(geometry):
    xs, ys = [], []
    for poly in polygons(geometry):
        for ring in poly:
            for pt in ring:
                xs.append(pt[0]); ys.append(pt[1])
    return min(xs), min(ys), max(xs), max(ys)


def make_view(bbox, size, pad=0.3):
    minlon, minlat, maxlon, maxlat = bbox
    clat = (minlat + maxlat) / 2
    k = max(math.cos(math.radians(clat)), 0.1)
    lon_span = max(maxlon - minlon, 0.05) * (1 + pad)
    lat_span = max(maxlat - minlat, 0.05) * (1 + pad)
    aspect = size[0] / size[1]
    if (lon_span * k) / lat_span < aspect:
        lon_span = lat_span * aspect / k
    else:
        lat_span = (lon_span * k) / aspect
    clon = (minlon + maxlon) / 2
    return (clon - lon_span / 2, clat - lat_span / 2,
            clon + lon_span / 2, clat + lat_span / 2)


def projector(view, size):
    minlon, minlat, maxlon, maxlat = view
    w, h = size

    def project(pt):
        return ((pt[0] - minlon) / (maxlon - minlon) * w,
                (maxlat - pt[1]) / (maxlat - minlat) * h)
    return project


def draw_polys(draw, polys, project, fill, outline, width=1):
    for poly in polys:
        for i, ring in enumerate(poly):
            pts = [project(p) for p in ring]
            if len(pts) >= 3:
                draw.polygon(pts, fill=(None if i else fill), outline=outline, width=width)


def draw_geometry(draw, geometry, project, fill, outline, width=1):
    draw_polys(draw, polygons(geometry), project, fill, outline, width)


def ramp(t):
    t = max(0.0, min(1.0, t))
    i = 0 if t < 0.5 else 1
    f = t * 2 if t < 0.5 else (t - 0.5) * 2
    a, b = RAMP[i], RAMP[i + 1]
    return tuple(int(a[c] + (b[c] - a[c]) * f) for c in range(3))


def density_layer(entry_density, view, size):
    """The density field, drawn at grid resolution and upscaled smoothly."""
    cols, rows = entry_density["cols"], entry_density["rows"]
    ox, oy = entry_density["origin"]
    cell = entry_density["cell"]
    img = Image.new("RGB", (cols, rows), ramp(0.0))
    px = img.load()
    # Extended outward first, so interpolation at the coast samples real values
    # instead of dragging the border into a false gradient. The clip removes it.
    field = weights.extend_edges(
        {(c[0], c[1]): c[2] for c in entry_density["cells"]}, cols, rows, radius=2)
    for (ix, iy), w in field.items():
        if 0 <= ix < cols and 0 <= iy < rows:
            px[ix, rows - 1 - iy] = ramp(math.sqrt(w))
    # Place the grid in the view, then upscale. Bicubic is what turns cells
    # into a field; without it the plot reads as bins however fine the grid.
    full = Image.new("RGB", size, ramp(0.0))
    project = projector(view, size)
    left, top = project((ox, oy + rows * cell))
    right, bottom = project((ox + cols * cell, oy))
    w_px, h_px = max(int(round(right - left)), 1), max(int(round(bottom - top)), 1)
    full.paste(img.resize((w_px, h_px), Image.BICUBIC), (int(round(left)), int(round(top))))
    return full


def panel(entry, feature, basemap, dens, show_density, size=PANEL):
    big = (size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE)
    fp_polys, wrapped = unwrap(feature["geometry"])
    view = make_view(bounds_of(fp_polys), big)
    project = projector(view, big)

    img = Image.new("RGB", big, SEA)
    draw = ImageDraw.Draw(img)
    # Land doubles as the clip: a footprint is a smooth blob and encloses the
    # water between islands, so the field has to stop at the coast.
    land_mask = Image.new("L", big, 0)
    land_draw = ImageDraw.Draw(land_mask)
    onscreen = []
    for f in basemap["features"]:
        polys = [shift_east(poly) for poly in polygons(f["geometry"])] if wrapped \
            else polygons(f["geometry"])
        b = bounds_of(polys)
        if b[2] < view[0] or b[0] > view[2] or b[3] < view[1] or b[1] > view[3]:
            continue
        onscreen.append(polys)
        draw_polys(draw, polys, project, LAND, BORDER, SUPERSAMPLE)
        draw_polys(land_draw, polys, project, 255, 255, 1)

    mask = Image.new("L", big, 0)
    draw_polys(ImageDraw.Draw(mask), fp_polys, project, 255, 255, 1)
    mask = ImageChops.multiply(mask, land_mask)

    if show_density and dens:
        img.paste(density_layer(dens, view, big), (0, 0), mask)
    else:
        flat = Image.new("RGB", big, (214, 69, 48))
        img = Image.blend(img, Image.composite(flat, img, mask), 0.42)

    # The outline of (footprint and land) is two things: the parts of the
    # footprint edge that are over land, and the parts of the coastline that are
    # inside the footprint. Stroke both and keep only what falls inside the
    # intersection, and the result follows the real border exactly without ever
    # computing a polygon intersection. The stroke straddles the boundary, so
    # half of it is masked away; draw it double width to compensate.
    stroke = Image.new("L", big, 0)
    draw_polys(ImageDraw.Draw(stroke), fp_polys, project, None, 255, 4 * SUPERSAMPLE)
    # The coastline is the edge of the land *union*, not each country's outline.
    # Stroking every country would draw Cambodia's border with Thailand straight
    # through the middle of the footprint, where the intersection has no
    # boundary at all. Eroding the land mask and subtracting leaves only the
    # coast, which is the edge the union actually has.
    coast = ImageChops.subtract(land_mask, land_mask.filter(ImageFilter.MinFilter(13)))
    stroke = ImageChops.lighter(stroke, coast)
    stroke = ImageChops.multiply(stroke, mask)

    edge = Image.new("RGB", big, EDGE)
    img = Image.composite(edge, img, stroke)
    img = img.resize(size, Image.LANCZOS)

    draw = ImageDraw.Draw(img)
    p = feature["properties"]
    draw.rectangle([0, 0, size[0], 40], fill=(255, 255, 255))
    draw.text((9, 5), entry, font=font(18), fill=TEXT)
    top = ""
    if dens and dens.get("by_country"):
        top = "   ".join(f"{k} {v:.0%}" for k, v in list(dens["by_country"].items())[:4])
    draw.text((9, 24), f"{p['source']}  ·  {p['parts']} parts  ·  {top}",
              font=font(11), fill=(110, 110, 110))
    draw.text((9, size[1] - 15),
              f"{view[0]:.1f}..{view[2]:.1f}E  {view[1]:.1f}..{view[3]:.1f}N",
              font=font(11), fill=(90, 90, 90))
    draw.rectangle([0, 0, size[0] - 1, size[1] - 1], outline=(170, 170, 170))
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entries", nargs="+")
    ap.add_argument("-o", "--out", default="/tmp/footprint-check.png")
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--shape", action="store_true", help="outline only, no density")
    ap.add_argument("--size", type=int, default=PANEL[0],
                    help="panel width in pixels, height follows the 13:10 ratio")
    args = ap.parse_args()

    path = BASEMAP if os.path.exists(BASEMAP) else FALLBACK_BASEMAP
    with open(path, encoding="utf-8") as fh:
        basemap = json.load(fh)
    with open(FOOTPRINTS, encoding="utf-8") as fh:
        feats = {f["properties"]["entry"]: f for f in json.load(fh)["features"]}
    dens = {}
    if os.path.exists(DENSITY):
        with open(DENSITY, encoding="utf-8") as fh:
            dens = {e["entry"]: e for e in json.load(fh)}

    size = (args.size, int(args.size * PANEL[1] / PANEL[0]))
    panels = []
    for name in args.entries:
        if name not in feats:
            sys.stderr.write(f"no footprint for {name!r}\n")
            continue
        panels.append(panel(name, feats[name], basemap, dens.get(name),
                            not args.shape, size))

    cols = min(args.cols, len(panels))
    rows = math.ceil(len(panels) / cols)
    sheet = Image.new("RGB", (cols * size[0], rows * size[1]), (255, 255, 255))
    for i, p in enumerate(panels):
        sheet.paste(p, ((i % cols) * size[0], (i // cols) * size[1]))
    sheet.save(args.out)
    sys.stderr.write(f"wrote {args.out}  {sheet.size[0]}x{sheet.size[1]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
