#!/usr/bin/env python3
"""Render footprints over a coastline basemap, to check them by eye.

Bounding boxes catch a polygon on the wrong continent. They do not catch one that
is the wrong shape, inside out, or the right country's neighbour. This draws them.

Dev tool, not pipeline. Needs Pillow, which the pipeline itself does not.

    python3 tools/preview.py Kabyle Spanish "Jamaican English" -o /tmp/check.png
"""

import argparse
import json
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASEMAP = os.path.join(REPO, ".cache", "geo", "ne_50m_admin_0_countries.geojson")
FOOTPRINTS = os.path.join(REPO, "data", "footprints.geojson")

SEA, LAND, BORDER = (222, 233, 242), (238, 236, 230), (196, 196, 190)
FILL, EDGE, TEXT = (214, 69, 48), (150, 30, 14), (34, 34, 34)
PANEL = (460, 360)


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


def bounds(geometry):
    xs, ys = [], []
    for poly in polygons(geometry):
        for ring in poly:
            for pt in ring:
                xs.append(pt[0]); ys.append(pt[1])
    return min(xs), min(ys), max(xs), max(ys)


def make_view(bbox, size, pad=0.35):
    """Expand a bbox to the panel's aspect, correcting longitude for latitude."""
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
    return (clon - lon_span / 2, clat - lat_span / 2, clon + lon_span / 2, clat + lat_span / 2)


def projector(view, size):
    minlon, minlat, maxlon, maxlat = view
    w, h = size

    def project(pt):
        x = (pt[0] - minlon) / (maxlon - minlon) * w
        y = (maxlat - pt[1]) / (maxlat - minlat) * h
        return x, y
    return project


def draw_geometry(draw, geometry, project, fill, outline, width=1):
    for poly in polygons(geometry):
        for i, ring in enumerate(poly):
            pts = [project(p) for p in ring]
            if len(pts) < 3:
                continue
            draw.polygon(pts, fill=(None if i else fill), outline=outline, width=width)


def panel(entry, feature, basemap, size=PANEL):
    view = make_view(bounds(feature["geometry"]), size)
    project = projector(view, size)
    img = Image.new("RGB", size, SEA)
    draw = ImageDraw.Draw(img)
    for f in basemap["features"]:
        b = bounds(f["geometry"])
        if b[2] < view[0] or b[0] > view[2] or b[3] < view[1] or b[1] > view[3]:
            continue
        draw_geometry(draw, f["geometry"], project, LAND, BORDER)
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw_geometry(ImageDraw.Draw(overlay), feature["geometry"], project,
                  FILL + (120,), EDGE + (255,), 2)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    p = feature["properties"]
    draw.rectangle([0, 0, size[0], 38], fill=(255, 255, 255))
    draw.text((8, 4), entry, font=font(17), fill=TEXT)
    draw.text((8, 22), f"{p['source']}  ·  {p['parts']} parts  ·  tier {p['tier']}",
              font=font(11), fill=(110, 110, 110))
    draw.text((8, size[1] - 14),
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
    args = ap.parse_args()

    basemap = json.load(open(BASEMAP, encoding="utf-8"))
    feats = {f["properties"]["entry"]: f
             for f in json.load(open(FOOTPRINTS, encoding="utf-8"))["features"]}

    panels = []
    for name in args.entries:
        if name not in feats:
            sys.stderr.write(f"no footprint for {name!r}\n")
            continue
        panels.append(panel(name, feats[name], basemap))

    cols = min(args.cols, len(panels))
    rows = math.ceil(len(panels) / cols)
    sheet = Image.new("RGB", (cols * PANEL[0], rows * PANEL[1]), (255, 255, 255))
    for i, p in enumerate(panels):
        sheet.paste(p, ((i % cols) * PANEL[0], (i // cols) * PANEL[1]))
    sheet.save(args.out)
    sys.stderr.write(f"wrote {args.out}  {sheet.size[0]}x{sheet.size[1]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
