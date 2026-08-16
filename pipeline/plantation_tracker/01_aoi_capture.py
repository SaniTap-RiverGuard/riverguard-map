#!/usr/bin/env python3
"""Plantation tracker — Step 1: auto-delineate the Domaine de la Cascade
Bambusa balcooa plantation (~-24.9940, 46.9120, ~26 ha) from Sentinel-2 and
emit an interactive correction page (output/aoi_editor.html).

Classification: seeded region-growing over NDVI + NDRE (red-edge) + NDVI
texture at 10 m — pixels spectrally similar to the plantation centre, connected
component only, morphologically cleaned. This is a FIRST GUESS for Adriaan to
correct — especially the streamside edge where plantation vs natural riparian
vegetation confusion is expected, and the poor thin-culm zones whose sparse
canopy may fall outside the auto-boundary.

Data: Sentinel-2 L2A COGs via Element84 Earth Search (no auth), windowed reads
only (~2.4 x 2.4 km).
"""
import base64
import io
import json
from pathlib import Path

import numpy as np
import rasterio
import requests
from rasterio.windows import from_bounds
from scipy import ndimage

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
OUT.mkdir(exist_ok=True)

CENTRE = (46.9120, -24.9940)  # lon, lat
HALF_M = 1200                  # window half-size, metres
UTM = "EPSG:32738"

# ---- pick the most recent near-cloudless scene
r = requests.post("https://earth-search.aws.element84.com/v1/search", json={
    "collections": ["sentinel-2-l2a"],
    "bbox": [CENTRE[0] - 0.02, CENTRE[1] - 0.02, CENTRE[0] + 0.02, CENTRE[1] + 0.02],
    "datetime": "2026-01-01T00:00:00Z/2026-08-16T00:00:00Z",
    "query": {"eo:cloud_cover": {"lt": 5}},
    "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    "limit": 4}, timeout=60)
scene = r.json()["features"][1] if len(r.json()["features"]) > 1 else r.json()["features"][0]
# prefer the truly cloudless one if present
for f in r.json()["features"]:
    if f["properties"]["eo:cloud_cover"] < 0.5:
        scene = f
        break
print("Scene:", scene["id"], scene["properties"]["datetime"][:10],
      "cloud", round(scene["properties"]["eo:cloud_cover"], 2))

from pyproj import Transformer
to_utm = Transformer.from_crs("EPSG:4326", UTM, always_xy=True)
to_wgs = Transformer.from_crs(UTM, "EPSG:4326", always_xy=True)
cx, cy = to_utm.transform(*CENTRE)
bounds_utm = (cx - HALF_M, cy - HALF_M, cx + HALF_M, cy + HALF_M)


def read_band(asset, scale=1):
    href = scene["assets"][asset]["href"]
    with rasterio.open(href) as src:
        win = from_bounds(*bounds_utm, transform=src.transform)
        a = src.read(1, window=win, out_shape=(int(win.height * scale), int(win.width * scale)),
                     resampling=rasterio.enums.Resampling.bilinear)
        t = src.window_transform(win)
    return a.astype("float32"), t


print("Reading bands...")
b04, t10 = read_band("red")
b03, _ = read_band("green")
b02, _ = read_band("blue")
b08, _ = read_band("nir")
b05, _ = read_band("rededge1", scale=2)   # 20 m -> 10 m
H, W = b04.shape
b05 = b05[:H, :W]
print(f"  window {H}x{W} px @10 m")

ndvi = (b08 - b04) / np.maximum(b08 + b04, 1)
ndre = (b08 - b05) / np.maximum(b08 + b05, 1)
mean = ndimage.uniform_filter(ndvi, 5)
mean2 = ndimage.uniform_filter(ndvi ** 2, 5)
tex = np.sqrt(np.maximum(mean2 - mean ** 2, 0))  # focal std of NDVI

print("Seeded region-growing from the plantation centre...")
ci, cj = H // 2, W // 2
F = np.dstack([ndvi, ndre, tex * 5])
seed = F[ci - 4:ci + 5, cj - 4:cj + 5].reshape(-1, 3)
mu, sd = seed.mean(0), np.maximum(seed.std(0), [0.03, 0.02, 0.01])
z = np.sqrt((((F - mu) / sd) ** 2).sum(axis=2))
mask = z < 4.0
mask = ndimage.binary_closing(mask, iterations=2)
mask = ndimage.binary_opening(mask, iterations=2)
lab2, n = ndimage.label(mask)
keep = lab2 == lab2[ci, cj]      # the connected component containing the centre
keep = ndimage.binary_fill_holes(keep)
ha = keep.sum() * 0.01
print(f"  candidate plantation mask: {ha:.1f} ha (target ~26; the editor exists to correct this)")

# polygonize
from rasterio import features as rfeatures
from shapely.geometry import shape
from shapely.ops import unary_union
polys = [shape(g) for g, v in rfeatures.shapes(keep.astype("uint8"), transform=t10) if v == 1]
poly = unary_union(polys).simplify(10)
geoms = list(poly.geoms) if poly.geom_type == "MultiPolygon" else [poly]
feats_gj = []
for g in geoms:
    coords = [[list(to_wgs.transform(x, y)) for x, y in g.exterior.coords]]
    coords[0] = [[round(x, 6), round(y, 6)] for x, y in coords[0]]
    feats_gj.append({"type": "Feature", "properties": {"role": "plantation", "auto": 1},
                     "geometry": {"type": "Polygon", "coordinates": coords}})
print(f"  {len(feats_gj)} polygon part(s)")

# ---- backdrops as data-URI PNGs
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def png_uri(img, cmap=None, vmin=None, vmax=None):
    buf = io.BytesIO()
    plt.imsave(buf, img, cmap=cmap, vmin=vmin, vmax=vmax, format="png")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


rgb = np.dstack([np.clip(b / 2200, 0, 1) for b in (b04, b03, b02)])
rgb = (rgb ** 0.85 * 255).astype("uint8")
tc_uri = png_uri(rgb)
ndvi_uri = png_uri(ndvi, cmap="RdYlGn", vmin=0.1, vmax=0.9)
x0, y0 = to_wgs.transform(bounds_utm[0], bounds_utm[1])
x1, y1 = to_wgs.transform(bounds_utm[2], bounds_utm[3])
img_bounds = [[y0, x0], [y1, x1]]

auto_gj = {"type": "FeatureCollection", "features": feats_gj,
           "_scene": scene["id"], "_auto_ha": round(ha, 1)}
(OUT / "aoi_auto.geojson").write_text(json.dumps(auto_gj))

html = (HERE / "editor_template.html").read_text()
html = (html.replace("__AUTO_GEOJSON__", json.dumps(auto_gj))
            .replace("__TC_URI__", tc_uri).replace("__NDVI_URI__", ndvi_uri)
            .replace("__IMG_BOUNDS__", json.dumps(img_bounds))
            .replace("__CENTRE__", json.dumps([CENTRE[1], CENTRE[0]]))
            .replace("__SCENE__", f"{scene['id']} ({scene['properties']['datetime'][:10]})")
            .replace("__AUTO_HA__", f"{ha:.1f}"))
(OUT / "aoi_editor.html").write_text(html)
print(f"Wrote {OUT/'aoi_editor.html'} and aoi_auto.geojson")
