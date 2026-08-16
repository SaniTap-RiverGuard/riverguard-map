#!/usr/bin/env python3
"""Plantation tracker — Step 2a: harvest per-stratum time series.

Strata (from plantation_zones.geojson, corrected by Adriaan 2026-08-16):
  good  — the one mapped good-performance zone (0.6 ha)
  poor  — plantation minus good ("essentially everything else is poor";
          contains unmapped scattered good clumps -> expect upward
          contamination, handled in the analysis step)
  whole — full plantation

Sources:
  S2  : Sentinel-2 L2A 2015-2026, Element84 Earth Search (monthly best scene)
  LS  : Landsat 5/7/8 C2 L2 2009-2015, Planetary Computer (SAS-signed)
  S1  : Sentinel-1 RTC VV/VH 2015-2026, Planetary Computer
  GEDI: CMR metadata count only (footprint data needs Earthdata auth)

Caches results incrementally in output/cache/*.parquet + annual dry-season
NDVI composites in output/cache/annual_ndvi.npz. Safe to re-run.
"""
import json
import time
import urllib.parse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from pyproj import Transformer
from rasterio import features as rfeatures
from rasterio.windows import from_bounds
from rasterio.enums import Resampling

HERE = Path(__file__).resolve().parent
CACHE = HERE / "output" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

UTM = "EPSG:32738"
CENTRE = (46.9120, -24.9940)
HALF_M = 1200
to_utm = Transformer.from_crs("EPSG:4326", UTM, always_xy=True)
cx, cy = to_utm.transform(*CENTRE)
BOUNDS = (cx - HALF_M, cy - HALF_M, cx + HALF_M, cy + HALF_M)
GRID10 = rasterio.transform.from_origin(BOUNDS[0], BOUNDS[3], 10, 10)
SHAPE10 = (240, 240)

zones = gpd.read_file(HERE / "plantation_zones.geojson").to_crs(UTM)
plant = zones[zones.role == "plantation"].geometry.iloc[0]
good = zones[zones.role == "good"].geometry.iloc[0]
poor = plant.difference(good)
STRATA = {"good": good, "poor": poor, "whole": plant}
masks10 = {k: rfeatures.geometry_mask([g], out_shape=SHAPE10, transform=GRID10, invert=True)
           for k, g in STRATA.items()}
print("strata px @10m:", {k: int(m.sum()) for k, m in masks10.items()})


def retry(fn, n=4, wait=8):
    for a in range(n):
        try:
            return fn()
        except Exception as e:
            if a == n - 1:
                raise
            time.sleep(wait * (a + 1))


def read_window(href, bounds, out_shape=None, resampling=Resampling.nearest):
    """Windowed read; reprojects the query bounds into the raster's own CRS
    (USGS distributes southern-hemisphere Landsat in north-based EPSG:326xx
    with negative northings — a raw UTM-38S window lands off-raster)."""
    from rasterio.warp import transform_bounds
    with rasterio.open(href) as src:
        b = bounds
        if src.crs and src.crs.to_string() != UTM:
            b = transform_bounds(UTM, src.crs, *bounds)
        win = from_bounds(*b, transform=src.transform)
        kw = {}
        if out_shape:
            kw = dict(out_shape=out_shape, resampling=resampling)
        return src.read(1, window=win, **kw).astype("float32")


def stratum_stats(arr, valid, masks):
    out = {}
    for k, m in masks.items():
        v = arr[m & valid]
        out[f"{k}_med"] = float(np.median(v)) if len(v) else np.nan
        out[f"{k}_p25"] = float(np.percentile(v, 25)) if len(v) else np.nan
        out[f"{k}_p75"] = float(np.percentile(v, 75)) if len(v) else np.nan
        out[f"{k}_n"] = int(len(v))
    return out


def load_cache(name):
    p = CACHE / name
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def monthly_best(features, key="eo:cloud_cover"):
    """One scene per calendar month: lowest cloud."""
    best = {}
    for f in features:
        mo = f["properties"]["datetime"][:7]
        if mo not in best or f["properties"].get(key, 100) < best[mo]["properties"].get(key, 100):
            best[mo] = f
    return sorted(best.values(), key=lambda f: f["properties"]["datetime"])


# ================================================================ Sentinel-2
def run_s2():
    cache_name = "s2_stats.parquet"
    done = set(load_cache(cache_name).get("scene", []))
    feats = []
    for yr in range(2015, 2027):
        r = retry(lambda: requests.post("https://earth-search.aws.element84.com/v1/search", json={
            "collections": ["sentinel-2-l2a"],
            "bbox": [CENTRE[0] - 0.01, CENTRE[1] - 0.01, CENTRE[0] + 0.01, CENTRE[1] + 0.01],
            "datetime": f"{yr}-01-01T00:00:00Z/{yr}-12-31T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": 60}}, "limit": 200}, timeout=60))
        feats += r.json()["features"]
    scenes = monthly_best(feats)
    print(f"S2: {len(scenes)} monthly scenes")
    rows = list(load_cache(cache_name).to_dict("records"))
    ndvi_stack = {}
    npz = CACHE / "s2_ndvi_scenes.npz"
    if npz.exists():
        old = np.load(npz)
        ndvi_stack = {k: old[k] for k in old.files}
    for i, sc in enumerate(scenes):
        sid = sc["id"]
        if sid in done and sid in ndvi_stack:
            continue
        try:
            a = sc["assets"]
            scl = retry(lambda: read_window(a["scl"]["href"], BOUNDS, SHAPE10))
            valid = np.isin(scl, [4, 5])
            if valid[masks10["whole"]].mean() < 0.35:
                continue
            b04 = retry(lambda: read_window(a["red"]["href"], BOUNDS))
            b08 = retry(lambda: read_window(a["nir"]["href"], BOUNDS))
            b02 = retry(lambda: read_window(a["blue"]["href"], BOUNDS))
            b05 = retry(lambda: read_window(a["rededge1"]["href"], BOUNDS, SHAPE10, Resampling.bilinear))
            if not sc["properties"].get("earthsearch:boa_offset_applied", False) and \
               sc["properties"]["datetime"] >= "2022-01-25":
                for b in (b04, b08, b02, b05):
                    b -= 1000
            ndvi = (b08 - b04) / np.maximum(b08 + b04, 1)
            evi = 2.5 * (b08 - b04) / np.maximum(b08 + 6 * b04 - 7.5 * b02 + 10000, 1)
            ndre = (b08 - b05) / np.maximum(b08 + b05, 1)
            row = {"scene": sid, "date": sc["properties"]["datetime"][:10],
                   "cloud": sc["properties"]["eo:cloud_cover"]}
            for name, arr in (("ndvi", ndvi), ("evi", evi), ("ndre", ndre)):
                for k, v in stratum_stats(arr, valid, masks10).items():
                    row[f"{name}_{k}"] = v
            rows.append(row)
            ndvi_stack[sid] = np.where(valid, ndvi, np.nan).astype("float32")
            if i % 10 == 0:
                pd.DataFrame(rows).to_parquet(CACHE / cache_name)
                np.savez_compressed(npz, **ndvi_stack)
                print(f"  S2 {i}/{len(scenes)} {row['date']} ndvi good={row['ndvi_good_med']:.2f} poor={row['ndvi_poor_med']:.2f}")
        except Exception as e:
            print(f"  S2 skip {sid}: {e}")
    pd.DataFrame(rows).to_parquet(CACHE / cache_name)
    np.savez_compressed(npz, **ndvi_stack)
    print(f"S2 done: {len(rows)} usable scenes")


# ================================================================ Landsat
def sign_pc(href, token):
    return href + "?" + token


def run_landsat():
    cache_name = "landsat_stats.parquet"
    done = set(load_cache(cache_name).get("scene", []))
    r = retry(lambda: requests.post("https://planetarycomputer.microsoft.com/api/stac/v1/search", json={
        "collections": ["landsat-c2-l2"],
        "bbox": [CENTRE[0] - 0.01, CENTRE[1] - 0.01, CENTRE[0] + 0.01, CENTRE[1] + 0.01],
        "datetime": "2009-01-01T00:00:00Z/2016-06-30T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": 70}}, "limit": 400}, timeout=60))
    scenes = monthly_best(r.json()["features"])
    print(f"Landsat: {len(scenes)} monthly scenes")
    SHAPE30 = (80, 80)
    GRID30 = rasterio.transform.from_origin(BOUNDS[0], BOUNDS[3], 30, 30)
    masks30 = {k: rfeatures.geometry_mask([g], out_shape=SHAPE30, transform=GRID30, invert=True)
               for k, g in STRATA.items()}
    rows = list(load_cache(cache_name).to_dict("records"))
    ndvi_stack = {}
    npz = CACHE / "landsat_ndvi_scenes.npz"
    if npz.exists():
        old = np.load(npz)
        ndvi_stack = {k: old[k] for k in old.files}
    token = None, 0
    for i, sc in enumerate(scenes):
        sid = sc["id"]
        if sid in done and sid in ndvi_stack:
            continue
        try:
            if token[0] is None or time.time() - token[1] > 1800:
                t = retry(lambda: requests.get(
                    "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2", timeout=30))
                token = (t.json()["token"], time.time())
            a = sc["assets"]
            plat = sc["properties"]["platform"]  # landsat-5/7/8
            red_key, nir_key = ("red", "nir08")
            qa = retry(lambda: read_window(sign_pc(a["qa_pixel"]["href"], token[0]), BOUNDS, SHAPE30))
            qai = qa.astype("uint16")
            # QA_PIXEL: bit3 cloud, bit4 shadow, bit1 dilated, bit0 fill
            valid = (qai & 0b11011) == 0
            if valid[masks30["whole"]].mean() < 0.35:
                continue
            red = retry(lambda: read_window(sign_pc(a[red_key]["href"], token[0]), BOUNDS, SHAPE30))
            nir = retry(lambda: read_window(sign_pc(a[nir_key]["href"], token[0]), BOUNDS, SHAPE30))
            red = red * 0.0000275 - 0.2
            nir = nir * 0.0000275 - 0.2
            valid &= (red > 0) & (nir > 0)
            ndvi = (nir - red) / np.maximum(nir + red, 1e-4)
            row = {"scene": sid, "date": sc["properties"]["datetime"][:10], "platform": plat,
                   "cloud": sc["properties"].get("eo:cloud_cover")}
            for k, v in stratum_stats(ndvi, valid, masks30).items():
                row[f"ndvi_{k}"] = v
            rows.append(row)
            ndvi_stack[sid] = np.where(valid, ndvi, np.nan).astype("float32")
            if i % 10 == 0:
                pd.DataFrame(rows).to_parquet(CACHE / cache_name)
                np.savez_compressed(npz, **ndvi_stack)
                print(f"  LS {i}/{len(scenes)} {row['date']} {plat} good={row['ndvi_good_med']:.2f} poor={row['ndvi_poor_med']:.2f}")
        except Exception as e:
            print(f"  LS skip {sid}: {e}")
    pd.DataFrame(rows).to_parquet(CACHE / cache_name)
    np.savez_compressed(npz, **ndvi_stack)
    print(f"Landsat done: {len(rows)} usable scenes")


# ================================================================ Sentinel-1 RTC
def run_s1():
    cache_name = "s1_stats.parquet"
    done = set(load_cache(cache_name).get("scene", []))
    feats = []
    for yr in range(2015, 2027):
        r = retry(lambda: requests.post("https://planetarycomputer.microsoft.com/api/stac/v1/search", json={
            "collections": ["sentinel-1-rtc"],
            "bbox": [CENTRE[0] - 0.01, CENTRE[1] - 0.01, CENTRE[0] + 0.01, CENTRE[1] + 0.01],
            "datetime": f"{yr}-01-01T00:00:00Z/{yr}-12-31T23:59:59Z", "limit": 200}, timeout=60))
        feats += r.json()["features"]
    # one per month is plenty
    best = {}
    for f in feats:
        best.setdefault(f["properties"]["datetime"][:7], f)
    scenes = sorted(best.values(), key=lambda f: f["properties"]["datetime"])
    print(f"S1 RTC: {len(scenes)} monthly scenes")
    rows = list(load_cache(cache_name).to_dict("records"))
    token = None, 0
    for i, sc in enumerate(scenes):
        sid = sc["id"]
        if sid in done:
            continue
        try:
            if token[0] is None or time.time() - token[1] > 1800:
                t = retry(lambda: requests.get(
                    "https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-1-rtc", timeout=30))
                token = (t.json()["token"], time.time())
            a = sc["assets"]
            if "vh" not in a or "vv" not in a:
                continue
            vh = retry(lambda: read_window(sign_pc(a["vh"]["href"], token[0]), BOUNDS, SHAPE10))
            vv = retry(lambda: read_window(sign_pc(a["vv"]["href"], token[0]), BOUNDS, SHAPE10))
            valid = (vh > 0) & (vv > 0)
            vh_db = 10 * np.log10(np.maximum(vh, 1e-6))
            vv_db = 10 * np.log10(np.maximum(vv, 1e-6))
            row = {"scene": sid, "date": sc["properties"]["datetime"][:10]}
            for name, arr in (("vh", vh_db), ("vv", vv_db)):
                for k, v in stratum_stats(arr, valid, masks10).items():
                    row[f"{name}_{k}"] = v
            rows.append(row)
            if i % 15 == 0:
                pd.DataFrame(rows).to_parquet(CACHE / cache_name)
                print(f"  S1 {i}/{len(scenes)} {row['date']} vh good={row['vh_good_med']:.1f} poor={row['vh_poor_med']:.1f} dB")
        except Exception as e:
            print(f"  S1 skip {sid}: {e}")
    pd.DataFrame(rows).to_parquet(CACHE / cache_name)
    print(f"S1 done: {len(rows)} usable scenes")


# ================================================================ GEDI (metadata only)
def run_gedi():
    try:
        r = requests.get("https://cmr.earthdata.nasa.gov/search/granules.json", params={
            "short_name": "GEDI02_A", "version": "002",
            "bounding_box": f"{CENTRE[0]-0.01},{CENTRE[1]-0.01},{CENTRE[0]+0.01},{CENTRE[1]+0.01}",
            "page_size": 50}, timeout=60)
        n = len(r.json()["feed"]["entry"])
        (CACHE / "gedi_note.json").write_text(json.dumps({
            "granules_intersecting_bbox": n,
            "note": "Granule METADATA only — GEDI L2A footprint data requires NASA Earthdata "
                    "authentication; granules are whole orbits, so intersection does not "
                    "guarantee footprints on the 18 ha AOI. Time-boxed per instruction."}))
        print(f"GEDI: {n} L2A granules intersect the bbox (metadata only; download needs Earthdata auth)")
    except Exception as e:
        (CACHE / "gedi_note.json").write_text(json.dumps({"error": str(e)}))
        print("GEDI query failed:", e)


if __name__ == "__main__":
    run_gedi()
    run_s2()
    run_landsat()
    run_s1()
    print("ALL SOURCES DONE")
