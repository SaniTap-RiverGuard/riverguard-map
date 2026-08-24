#!/usr/bin/env python3
"""Step 10 (VARIANT, approved AM 2026-08-24): re-run WorldCover 100 m buffer
sampling on OSM-snapped geometry (from 09_osm_snap.py) and rescore.

Scope (approved): land-cover-derived attributes ONLY — plantable fraction,
dominant class, wetland fraction, land-use composition. JRC inundation and all
non-land-cover attributes keep their stored values (noted as a limitation in
the comparison report). Scoring LOGIC identical to 03_score.py; scores are
recomputed for the whole population from stored components so old and new are
computed by the same code path (old_recomp validates against the stored score).

Output: data/derived/segments_variant_v2.gpkg — NOT promoted to the app.
Run after 09: .venv/bin/python pipeline/10_variant_landcover.py
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
UTM = "EPSG:32738"
SC, W = CFG["scoring"], CFG["weights"]
PLANTABLE = set(SC["landcover"]["plantable_classes"])
BUF_M = CFG["segmentation"]["buffer_m"]

print("Loading inputs...")
snap = gpd.read_file(DERIVED / "segments_snap.gpkg").set_index("seg_id")
scored = gpd.read_file(DERIVED / "segments_scored.gpkg").set_index("seg_id")
enr = gpd.read_file(DERIVED / "segments_enriched.gpkg",
                    columns=["seg_id", "pct_crop", "pct_grass", "pct_shrub", "pct_tree",
                             "pct_water", "pct_wetland"]).set_index("seg_id")
enr = enr.drop(columns=[c for c in ("geometry",) if c in enr.columns])
df = scored.join(pd.DataFrame(snap[["geom_src", "off_med_m"]])).join(enr, how="left")
df["geom_snap"] = snap["geometry"]
print(f"  {len(df)} segments; {(df['geom_src'] == 'snapped').sum()} snapped")

# ---------------- resample WorldCover over snapped buffers
print("Resampling WorldCover on snapped buffers...")
wcs = [rasterio.open(p) for p in sorted((RAW / "worldcover").glob("*.tif"))]
wbounds = [d.bounds for d in wcs]

def wc_pixels(geom):
    gx0, gy0, gx1, gy1 = geom.bounds
    out = []
    for ds, bb in zip(wcs, wbounds):
        if gx1 < bb.left or gx0 > bb.right or gy1 < bb.bottom or gy0 > bb.top:
            continue
        win = from_bounds(max(gx0, bb.left), max(gy0, bb.bottom), min(gx1, bb.right),
                          min(gy1, bb.top), transform=ds.transform).round_lengths().round_offsets()
        if win.width < 1 or win.height < 1:
            continue
        a = ds.read(1, window=win)
        m = geometry_mask([mapping(geom)], out_shape=a.shape,
                          transform=ds.window_transform(win), invert=True)
        v = a[m]
        if len(v):
            out.append(v)
    return np.concatenate(out) if out else None

snapped_ids = df.index[df["geom_src"] == "snapped"]
snap_utm = gpd.GeoSeries(df.loc[snapped_ids, "geom_snap"], crs="EPSG:4326").to_crs(UTM)
snap_buf = snap_utm.buffer(BUF_M, cap_style="flat").to_crs("EPSG:4326")

new = {k: {} for k in ("lf", "ld", "wet", "crop", "grass", "shrub", "tree", "water")}
miss = 0
for j, (sid, geom) in enumerate(snap_buf.items()):
    if j % 2000 == 0:
        print(f"  {j}/{len(snap_buf)}")
    v = wc_pixels(geom)
    if v is None:
        miss += 1
        continue
    tot = len(v)
    new["lf"][sid] = np.isin(v, list(PLANTABLE)).sum() / tot
    new["ld"][sid] = int(np.bincount(v.astype(int)).argmax())
    new["wet"][sid] = (v == 90).sum() / tot
    new["crop"][sid] = (v == 40).sum() / tot
    new["grass"][sid] = (v == 30).sum() / tot
    new["shrub"][sid] = (v == 20).sum() / tot
    new["tree"][sid] = (v == 10).sum() / tot
    new["water"][sid] = (v == 80).sum() / tot
print(f"  resampled {len(new['lf'])} snapped buffers ({miss} no-data, keep old values)")

for col, src, old in [("lf_new", "lf", "lc_frac"), ("ld_new", "ld", "lc_dom"),
                      ("wet_new", "wet", "wet_frac"), ("uc_new", "crop", "pct_crop"),
                      ("ug_new", "grass", "pct_grass"), ("us_new", "shrub", "pct_shrub"),
                      ("ut_new", "tree", "pct_tree"), ("uo_new", "water", "pct_water")]:
    df[col] = df.index.map(new[src]).astype(float)
    df[col] = df[col].fillna(df[old])

# ---------------- rescore (same formulas as 03_score.py)
def texture_score(clay, sand):
    t = SC["texture"]
    base = 100 * np.clip((clay - t["clay_lo"]) / (t["clay_hi"] - t["clay_lo"]), 0, 1)
    pen = t["sand_penalty_max"] * np.clip(
        (sand - t["sand_penalty_start"]) / (t["sand_penalty_full"] - t["sand_penalty_start"]), 0, 1)
    return np.clip(base - pen, 0, 100)

def slope_score(slp):
    s = SC["slope"]
    return np.where(slp < s["optimal_min"],
                    s["flat_score"] + (100 - s["flat_score"]) * slp / s["optimal_min"],
                    np.where(slp <= s["optimal_max"], 100.0,
                             100 + (s["steep_score"] - 100) * (slp - s["optimal_max"])
                             / (s["exclude_deg"] - s["optimal_max"])))

def rainfall_score(mm):
    r = SC["rainfall"]
    return 100 * np.clip((mm - r["mm_lo"]) / (r["mm_hi"] - r["mm_lo"]), 0, 1)

def compute(lf, wet):
    """excl + score under 03_score.py's exact rules, with stored non-LC attrs."""
    ar, inu, fp = SC["aridity"], SC["inundation"], SC["flood_penalty"]
    slope_excl = df["slope"] >= SC["slope"]["exclude_deg"]
    # stored 'slope' excl can also come from missing data; honour the stored flag
    slope_excl |= df["excl"] == "slope"
    excl = np.select(
        [slope_excl,
         lf < SC["landcover"]["min_plantable_fraction"],
         df["rain"] < ar["exclude_below_mm"],
         df["water_frac"] > inu["buffer_share_max"],
         wet > SC["wetland_exclusion"]["share_max"]],
        ["slope", "landcover", "semi-arid", "inundated", "swamp/wetland"], default="")
    score = (W["texture"] * texture_score(df["clay"], df["sand"])
             + W["landcover"] * 100 * lf
             + W["slope"] * slope_score(df["slope"])
             + W["rainfall"] * rainfall_score(df["rain"]))
    score = score - np.where((df["slope"] < fp["slope_deg"]) & (df["elev"] < fp["elev_m"]),
                             fp["penalty"], 0)
    score = score - np.where(df["rain"] < ar["penalty_to_mm"],
                             ar["penalty"] * (ar["penalty_to_mm"] - df["rain"])
                             / (ar["penalty_to_mm"] - ar["exclude_below_mm"]), 0)
    score = np.clip(score, 0, 100)
    return excl, np.where(excl == "", np.round(score, 1), 0.0)

df["excl_recomp"], df["score_recomp"] = compute(df["lc_frac"], df["wet_frac"])
df["excl_new"], df["score_new"] = compute(df["lf_new"], df["wet_new"])

ok_old = df["excl"] == ""
dd = (df.loc[ok_old, "score_recomp"] - df.loc[ok_old, "score"]).abs()
print(f"  recomputation fidelity vs stored scores: mean |Δ| {dd.mean():.3f}, "
      f"p99 {dd.quantile(.99):.2f}, max {dd.max():.2f} "
      f"(rounding of stored components); excl match "
      f"{(df['excl_recomp'] == df['excl']).mean()*100:.2f} %")

# classes: percentiles of the variant non-excluded population
cl = SC["classes"]
okn = df["excl_new"] == ""
hi = float(np.quantile(df.loc[okn, "score_new"], cl["high_quantile"]))
lo = float(np.quantile(df.loc[okn, "score_new"], cl["low_quantile"]))
df["cls_new"] = np.where(~okn, "excluded",
                         np.select([df["score_new"] >= hi, df["score_new"] < lo],
                                   ["high", "low"], default="medium"))
tbs = SC["trial_benchmark"]["score"]
df["tb_new"] = (okn & (df["score_new"] >= tbs)).astype(int)
print(f"  variant class thresholds: high >= {hi:.1f}, low < {lo:.1f} "
      f"(live: from 03 log); tb_new: {df['tb_new'].sum()} segments")

out = df.reset_index()[[
    "seg_id", "length_m", "ord", "riv", "geom_src", "off_med_m",
    "score", "score_recomp", "score_new", "cls", "cls_new", "excl", "excl_new",
    "tb", "tb_new", "lc_frac", "lf_new", "lc_dom", "ld_new", "wet_frac", "wet_new",
    "pct_crop", "uc_new", "pct_grass", "ug_new", "pct_shrub", "us_new",
    "pct_tree", "ut_new", "pct_water", "uo_new", "geom_snap"]]
out = gpd.GeoDataFrame(out, geometry="geom_snap", crs="EPSG:4326")
meta = {"variant": "v2_osm_snap", "class_hi_thr": hi, "class_lo_thr": lo, "tb_score": tbs}
dst = DERIVED / "segments_variant_v2.gpkg"
out.to_file(dst, driver="GPKG")
(DERIVED / "segments_variant_v2.meta.json").write_text(json.dumps(meta, indent=1))
print(f"Wrote {dst}")
