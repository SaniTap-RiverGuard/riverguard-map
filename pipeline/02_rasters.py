#!/usr/bin/env python3
"""Step 2: Download / prepare all raster layers for the study area.

Idempotent: each product is skipped if its output already exists. Outputs land
in data/raw/ (tiles) and data/derived/ (analysis-ready layers).

Layers:
  1. SoilGrids 250m clay & sand, 0-30cm depth-weighted mean (%), EPSG:4326
     — windowed remote reads from ISRIC VRTs (no full-continent download).
  2. Copernicus GLO-30 DEM tiles intersecting the segments + derived slope
     tiles (degrees).
  3. ESA WorldCover 2021 v200 3-degree tiles intersecting the segments.
  4. WorldClim 2.1 annual precipitation (sum of 2.5' monthly), cropped.

Run after 01_rivers.py: .venv/bin/python pipeline/02_rasters.py
"""
import json
import math
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"

SA = CFG["study_area"]
# pad bbox so 100 m buffers at the edge are covered
PAD = 0.02
BOUNDS = (SA["lon_min"] - PAD, SA["lat_min"] - PAD, SA["lon_max"] + PAD, SA["lat_max"] + PAD)

buffers = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="buffers")
print(f"{len(buffers)} segment buffers loaded; bounds {buffers.total_bounds.round(2)}")


def http_download(url, dest, ok404=False):
    if dest.exists() and dest.stat().st_size > 0:
        return True
    for attempt in range(5):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                if r.status_code == 404 and ok404:
                    return False
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                tmp.rename(dest)
                return True
        except Exception as e:
            print(f"    retry {attempt + 1} for {url}: {e}")
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"failed to download {url}")


# ---------------------------------------------------------------- SoilGrids
SG_RES = 0.0025  # ~250 m in degrees


def fetch_soilgrids(prop, depth):
    out = RAW / f"soilgrids_{prop}_{depth}.tif"
    if out.exists():
        print(f"  {out.name} exists, skip")
        return out
    url = f"/vsicurl/https://files.isric.org/soilgrids/latest/data/{prop}/{prop}_{depth}_mean.vrt"
    print(f"  fetching {prop} {depth} (remote windowed read)...")
    for attempt in range(4):
        try:
            with rasterio.open(url) as src:
                with WarpedVRT(src, crs="EPSG:4326", resampling=rasterio.enums.Resampling.bilinear,
                               xRes=SG_RES, yRes=SG_RES) as vrt:
                    win = from_bounds(*BOUNDS, transform=vrt.transform)
                    data = vrt.read(1, window=win)
                    transform = vrt.window_transform(win)
                    profile = {
                        "driver": "GTiff", "height": data.shape[0], "width": data.shape[1],
                        "count": 1, "dtype": data.dtype, "crs": "EPSG:4326",
                        "transform": transform, "nodata": src.nodata,
                        "compress": "deflate",
                    }
                    with rasterio.open(out, "w", **profile) as dst:
                        dst.write(data, 1)
            return out
        except Exception as e:
            print(f"    retry {attempt + 1}: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"SoilGrids fetch failed: {prop} {depth}")


def depth_weighted(prop):
    """0-30cm mean: weights = layer thickness 5,10,15 cm. SoilGrids units g/kg -> %."""
    out = DERIVED / f"{prop}_030_pct.tif"
    if out.exists():
        print(f"  {out.name} exists, skip")
        return
    depths = [("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15)]
    acc, prof, nodata = None, None, None
    for d, w in depths:
        with rasterio.open(RAW / f"soilgrids_{prop}_{d}.tif") as src:
            a = src.read(1).astype("float64")
            nodata = src.nodata if src.nodata is not None else 0
            a = np.where(a == nodata, np.nan, a)
            acc = a * w if acc is None else acc + a * w
            prof = src.profile
    pct = acc / 30.0 / 10.0  # g/kg -> %
    prof.update(dtype="float32", nodata=-9999, compress="deflate")
    with rasterio.open(out, "w", **prof) as dst:
        dst.write(np.where(np.isnan(pct), -9999, pct).astype("float32"), 1)
    print(f"  wrote {out.name}")


print("SoilGrids clay & sand...")
for prop in ("clay", "sand"):
    for depth in ("0-5cm", "5-15cm", "15-30cm"):
        fetch_soilgrids(prop, depth)
    depth_weighted(prop)

# ---------------------------------------------------------------- GLO-30 DEM + slope
print("Copernicus GLO-30 DEM tiles...")
b = buffers.total_bounds
cells = set()
for _, row in buffers.iterrows():
    x0, y0, x1, y1 = row.geometry.bounds
    for lon in range(math.floor(x0), math.floor(x1) + 1):
        for lat in range(math.floor(y0), math.floor(y1) + 1):
            cells.add((lat, lon))
print(f"  {len(cells)} 1-degree cells intersect segments")

dem_dir = RAW / "glo30"
dem_dir.mkdir(exist_ok=True)
slope_dir = DERIVED / "slope"
slope_dir.mkdir(exist_ok=True)
dem_tiles = []
for lat, lon in sorted(cells):
    ns = f"S{abs(lat):02d}" if lat < 0 else f"N{lat:02d}"
    ew = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    name = f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"
    dest = dem_dir / f"{name}.tif"
    url = f"https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
    if http_download(url, dest, ok404=True):
        dem_tiles.append(dest)
    else:
        print(f"    {name}: no tile (ocean)")
print(f"  {len(dem_tiles)} DEM tiles present")

M_PER_DEG = 111320.0
for tile in dem_tiles:
    out = slope_dir / tile.name.replace("_DEM", "_SLOPE")
    if out.exists():
        continue
    with rasterio.open(tile) as src:
        z = src.read(1).astype("float64")
        t = src.transform
        lats = np.array([t.f + t.e * (i + 0.5) for i in range(z.shape[0])])
        dy = abs(t.e) * M_PER_DEG
        dx = abs(t.a) * M_PER_DEG * np.cos(np.radians(lats))[:, None]
        gy, gx = np.gradient(z)
        slope = np.degrees(np.arctan(np.hypot(gx / dx, gy / dy)))
        prof = src.profile
        prof.update(dtype="float32", compress="deflate", nodata=-9999)
        with rasterio.open(out, "w", **prof) as dst:
            dst.write(slope.astype("float32"), 1)
    print(f"  slope: {out.name}")

# ---------------------------------------------------------------- WorldCover
print("ESA WorldCover tiles...")
wc_dir = RAW / "worldcover"
wc_dir.mkdir(exist_ok=True)
wc_cells = set()
for lat, lon in cells:
    wc_cells.add((math.floor(lat / 3) * 3, math.floor(lon / 3) * 3))
for lat, lon in sorted(wc_cells):
    ns = f"S{abs(lat):02d}" if lat < 0 else f"N{lat:02d}"
    ew = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    name = f"ESA_WorldCover_10m_2021_v200_{ns}{ew}_Map.tif"
    url = f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{name}"
    if http_download(url, wc_dir / name, ok404=True):
        print(f"  {name} ok")
    else:
        print(f"  {name}: no tile (ocean)")

# ---------------------------------------------------------------- WorldClim precip
print("WorldClim annual precipitation...")
prec_out = DERIVED / "annual_precip_mm.tif"
if not prec_out.exists():
    zpath = RAW / "wc2.1_2.5m_prec.zip"
    acc, prof = None, None
    with zipfile.ZipFile(zpath) as zf:
        for m in range(1, 13):
            fname = f"wc2.1_2.5m_prec_{m:02d}.tif"
            with rasterio.open(f"zip://{zpath}!{fname}") as src:
                win = from_bounds(*BOUNDS, transform=src.transform)
                a = src.read(1, window=win).astype("float64")
                a = np.where(a == src.nodata, np.nan, a)
                acc = a if acc is None else acc + a
                if prof is None:
                    prof = src.profile
                    prof.update(height=a.shape[0], width=a.shape[1],
                                transform=src.window_transform(win))
    prof.update(dtype="float32", nodata=-9999, compress="deflate")
    with rasterio.open(prec_out, "w", **prof) as dst:
        dst.write(np.where(np.isnan(acc), -9999, acc).astype("float32"), 1)
    print(f"  wrote {prec_out.name}")
else:
    print(f"  {prec_out.name} exists, skip")

print("All rasters ready.")
