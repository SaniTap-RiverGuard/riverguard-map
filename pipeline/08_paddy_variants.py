#!/usr/bin/env python3
"""Paddy-detector variants analysis (2026-08-16, after terraced-rice field FAIL).

Computes, per segment with any cropland, three candidate paddy measures:
  up2  — shipped heuristic: cropland px, slope < 2°, buffer touches water/wetland
  up8  — option (a): same but slope < 8° (captures terraces near water)
  upw  — option (b-lite): cropland px with JRC GSW occurrence > 5% (wetness signal)
Option (c) needs no detection: cropland counted at a discount (analytic).

Writes data/derived/paddy_variants.parquet and prints flagged-share + preset-
hectare impact numbers. DOES NOT touch shipped outputs.
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry import mapping

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"

e = gpd.read_file(DERIVED / "segments_enriched.gpkg")
buffers = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="buffers").set_index("seg_id")
buffers = buffers.loc[e["seg_id"].values]

wcs = [rasterio.open(p) for p in sorted((RAW / "worldcover").glob("*.tif"))]
wbounds = [d.bounds for d in wcs]
slopes = [rasterio.open(p) for p in sorted((DERIVED / "slope").glob("*.tif"))]
sbounds = [d.bounds for d in slopes]
gsws = [rasterio.open(p) for p in sorted((RAW / "gsw").glob("*.tif"))]
gbounds = [d.bounds for d in gsws]


def sample_at(datasets, dbounds, xs, ys):
    out = np.full(len(xs), np.nan)
    for ds, bb in zip(datasets, dbounds):
        m = (xs >= bb.left) & (xs <= bb.right) & (ys >= bb.bottom) & (ys <= bb.top) & ~np.isfinite(out)
        if not m.any():
            continue
        win = from_bounds(xs[m].min(), ys[m].min(), xs[m].max(), ys[m].max(),
                          transform=ds.transform).round_lengths().round_offsets()
        win = rasterio.windows.Window(win.col_off, win.row_off, max(1, win.width) + 1, max(1, win.height) + 1)
        a = ds.read(1, window=win)
        t = ds.window_transform(win)
        cc = ((xs[m] - t.c) / t.a).astype(int).clip(0, a.shape[1] - 1)
        rr = ((ys[m] - t.f) / t.e).astype(int).clip(0, a.shape[0] - 1)
        out[m] = a[rr, cc]
    return out


idx_crop = np.where(e["pct_crop"].values > 0)[0]
print(f"{len(idx_crop)} segments with cropland...")
up2 = np.zeros(len(e)); up8 = np.zeros(len(e)); upw = np.zeros(len(e))
for k, i in enumerate(idx_crop):
    if k % 3000 == 0:
        print(f"  {k}/{len(idx_crop)}")
    geom = buffers.geometry.iloc[i]
    gx0, gy0, gx1, gy1 = geom.bounds
    for ds, bb in zip(wcs, wbounds):
        if gx1 < bb.left or gx0 > bb.right or gy1 < bb.bottom or gy0 > bb.top:
            continue
        win = from_bounds(max(gx0, bb.left), max(gy0, bb.bottom), min(gx1, bb.right),
                          min(gy1, bb.top), transform=ds.transform).round_lengths().round_offsets()
        if win.width < 1 or win.height < 1:
            continue
        a = ds.read(1, window=win)
        t = ds.window_transform(win)
        m = geometry_mask([mapping(geom)], out_shape=a.shape, transform=t, invert=True)
        v = a[m]
        if not len(v):
            continue
        tot = len(v)
        has_wet = (v == 80).any() or (v == 90).any()
        rr, cc = np.where(m)
        crop_sel = a[m] == 40
        if crop_sel.any():
            xs = t.c + (cc[crop_sel] + 0.5) * t.a
            ys = t.f + (rr[crop_sel] + 0.5) * t.e
            sl = sample_at(slopes, sbounds, xs, ys)
            gw = sample_at(gsws, gbounds, xs, ys)
            gw = np.where(gw > 100, 0, gw)  # 255 nodata -> never water
            if has_wet:
                up2[i] = ((sl >= 0) & (sl < 2)).sum() / tot
                up8[i] = ((sl >= 0) & (sl < 8)).sum() / tot
            upw[i] = (gw > 5).sum() / tot
        break

df = pd.DataFrame({"seg_id": e.seg_id.values, "up2": np.round(up2, 3), "up8": np.round(up8, 3),
                   "upw": np.round(upw, 3)})
df.to_parquet(DERIVED / "paddy_variants.parquet")

# ---- report
ne = e[e.cls != "excluded"].reset_index(drop=True)
mask_ne = (e.cls != "excluded").values
L = e.length_m.values / 1000
lf = e.lc_frac.values
uc = e.pct_crop.values


def ha_with(paddy, m):
    return (L[m] * 2 * np.clip(lf[m] - paddy[m], 0, None)).sum()


presets = {
    "op": ((e.tb == 1) & (e.access.isin(["road", "boat"])) & (e.pop5k > 1000)).values & mask_ne,
    "bio": ((e.tb == 1) & (e.pa_km <= 10)).values & mask_ne,
}
variants = {
    "no deduction (paddy toggle off)": np.zeros(len(e)),
    "current up2 (slope<2)": up2,
    "(a) up8 (slope<8)": up8,
    "(b) GSW-wet cropland": upw,
    "(c) all cropland @50%": 0.5 * uc,
    "(c) all cropland @100%": uc,
}
print("\nflagged share of segments (non-excluded, any paddy>0) and area deducted:")
for name, v in variants.items():
    n = (v[mask_ne] > 0.005).sum()
    ded = (L[mask_ne] * 2 * np.minimum(v[mask_ne], lf[mask_ne])).sum()
    print(f"  {name:32s}: {n:6d} segs flagged, {ded:8,.0f} ha deducted coast-wide")
print("\npreset effective hectares under each variant:")
for pname, pm in presets.items():
    row = "  " + pname + ": " + " | ".join(f"{name}: {ha_with(v, pm):,.0f}" for name, v in variants.items())
    print(row)
# false-positive probe for (a): how much of up8-flagged area has NO wetness signal at all?
both = (up8 > 0.005) & mask_ne
dry = both & (upw < 0.001)
print(f"\n(a) false-positive probe: of {both.sum()} up8-flagged segments, "
      f"{dry.sum()} ({dry.sum()/max(1,both.sum())*100:.0f}%) show zero GSW wetness on their cropland "
      f"(rain-fed sloped cropland at risk of false flagging)")
for d in wcs + slopes + gsws:
    d.close()
