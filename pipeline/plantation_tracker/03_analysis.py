#!/usr/bin/env python3
"""Plantation tracker — Step 2b: analysis + figures from the harvested caches.

Produces PNGs in output/figs/ and output/stats.json (consumed by the report).
Wet season = Dec-Mar, dry season = Jun-Sep (southeast Madagascar).
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pyproj import Transformer
from rasterio import features as rfeatures
from scipy import stats as sstats

HERE = Path(__file__).resolve().parent
CACHE = HERE / "output" / "cache"
FIGS = HERE / "output" / "figs"
FIGS.mkdir(exist_ok=True)

UTM = "EPSG:32738"
CENTRE = (46.9120, -24.9940)
to_utm = Transformer.from_crs("EPSG:4326", UTM, always_xy=True)
cx, cy = to_utm.transform(*CENTRE)
BOUNDS = (cx - 1200, cy - 1200, cx + 1200, cy + 1200)
GRID10 = rasterio.transform.from_origin(BOUNDS[0], BOUNDS[3], 10, 10)
SHAPE10 = (240, 240)

zones = gpd.read_file(HERE / "plantation_zones.geojson").to_crs(UTM)
plant = zones[zones.role == "plantation"].geometry.iloc[0]
good = zones[zones.role == "good"].geometry.iloc[0]
poor = plant.difference(good)
masks = {k: rfeatures.geometry_mask([g], out_shape=SHAPE10, transform=GRID10, invert=True)
         for k, g in {"good": good, "poor": poor, "whole": plant}.items()}

s2 = pd.read_parquet(CACHE / "s2_stats.parquet")
ls = pd.read_parquet(CACHE / "landsat_stats.parquet")
s1 = pd.read_parquet(CACHE / "s1_stats.parquet") if (CACHE / "s1_stats.parquet").exists() else pd.DataFrame()
for df in (s2, ls, s1):
    if len(df):
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values("date", inplace=True)
COLORS = {"good": "#1a9850", "poor": "#c0392b", "whole": "#555555"}
stats_out = {}

# ---------------------------------------------------------------- fig 1: main NDVI series
fig, ax = plt.subplots(figsize=(13, 5.5), facecolor="white")
for k in ("good", "poor"):
    ax.plot(ls["date"], ls[f"ndvi_{k}_med"], "o", ms=3, alpha=0.35, color=COLORS[k])
    ax.plot(s2["date"], s2[f"ndvi_{k}_med"], "o", ms=3, alpha=0.35, color=COLORS[k])
    both = pd.concat([ls[["date", f"ndvi_{k}_med"]], s2[["date", f"ndvi_{k}_med"]]]).set_index("date").sort_index()
    roll = both.rolling("365D", center=True, min_periods=4).median()
    ax.plot(roll.index, roll[f"ndvi_{k}_med"], "-", lw=2.4, color=COLORS[k],
            label=f"{k} stratum (12-mo rolling median)")
ax.axvline(pd.Timestamp("2015-07-01"), color="#888", lw=0.8, ls=":")
ax.text(pd.Timestamp("2015-08-01"), 0.15, "Landsat 30 m ← | → Sentinel-2 10 m", fontsize=8, color="#666")
ax.set_ylabel("NDVI (stratum median)")
ax.set_ylim(0, 1)
ax.legend(loc="lower right", fontsize=9)
ax.set_title("Domaine de la Cascade — NDVI history by performance stratum, 2009–2026")
fig.tight_layout()
fig.savefig(FIGS / "fig1_ndvi_series.png", dpi=140)

# ---------------------------------------------------------------- fig 2: seasonal amplitude
def year_amplitude(df, col):
    out = {}
    for yr, g in df.groupby(df.date.dt.year):
        wet = g[g.date.dt.month.isin([12, 1, 2, 3])][col].median()
        dry = g[g.date.dt.month.isin([6, 7, 8, 9])][col].median()
        if np.isfinite(wet) and np.isfinite(dry):
            out[yr] = wet - dry
    return pd.Series(out)

fig, ax = plt.subplots(figsize=(9, 4.2), facecolor="white")
for k in ("good", "poor"):
    both = pd.concat([ls[["date", f"ndvi_{k}_med"]], s2[["date", f"ndvi_{k}_med"]]])
    amp = year_amplitude(both, f"ndvi_{k}_med")
    ax.plot(amp.index, amp.values, "o-", color=COLORS[k], label=f"{k} stratum")
    stats_out[f"amplitude_{k}_recent"] = round(float(amp[amp.index >= 2022].mean()), 3)
ax.axhline(0, color="#999", lw=0.7)
ax.set_ylabel("wet − dry season NDVI")
ax.set_title("Seasonal NDVI amplitude by year (hypothesis: poor browns harder in dry season)")
ax.legend()
fig.tight_layout()
fig.savefig(FIGS / "fig2_amplitude.png", dpi=140)

# ---------------------------------------------------------------- fig 3: S1 backscatter
if len(s1):
    fig, ax = plt.subplots(figsize=(13, 4), facecolor="white")
    for k in ("good", "poor"):
        ax.plot(s1["date"], s1[f"vh_{k}_med"], "o", ms=3, alpha=0.35, color=COLORS[k])
        roll = s1.set_index("date")[f"vh_{k}_med"].rolling("365D", center=True, min_periods=4).median()
        ax.plot(roll.index, roll.values, "-", lw=2.2, color=COLORS[k], label=f"{k} VH (12-mo median)")
    ax.set_ylabel("Sentinel-1 RTC VH γ0 (dB)")
    ax.set_title("Radar backscatter by stratum — VH is sensitive to canopy structure/biomass")
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(FIGS / "fig3_s1.png", dpi=140)
    stats_out["s1_vh_gap_recent_db"] = round(float(
        s1[s1.date >= "2023"][f"vh_good_med"].median() - s1[s1.date >= "2023"][f"vh_poor_med"].median()), 2)

# ---------------------------------------------------------------- annual dry-season composites
s2_npz = np.load(CACHE / "s2_ndvi_scenes.npz")
s2_dates = {k: k.split("_")[2] for k in s2_npz.files}   # S2X_38JPT_YYYYMMDD_...
ls_npz = np.load(CACHE / "landsat_ndvi_scenes.npz")
ls_dates = {k: k.split("_")[3] for k in ls_npz.files}   # LXSS_L2SP_PPPRRR_YYYYMMDD_...

annual = {}
for yr in range(2009, 2027):
    arrs = []
    for k, d in ls_dates.items():
        if d[:4] == str(yr) and 6 <= int(d[4:6]) <= 9:
            a = ls_npz[k]                             # 80x80 @30m
            arrs.append(np.kron(a, np.ones((3, 3)))[:240, :240])
    for k, d in s2_dates.items():
        if d[:4] == str(yr) and 6 <= int(d[4:6]) <= 9:
            arrs.append(s2_npz[k])
    if arrs:
        annual[yr] = np.nanmedian(np.stack(arrs), axis=0)
np.savez_compressed(CACHE / "annual_dry_ndvi.npz", **{str(y): a for y, a in annual.items()})
print("annual dry composites:", sorted(annual.keys()))

# fig 4: small multiples
years = sorted(annual.keys())
cols = 6
rows_n = int(np.ceil(len(years) / cols))
fig, axes = plt.subplots(rows_n, cols, figsize=(16, 2.9 * rows_n), facecolor="white")
from shapely.ops import transform as shtransform
def to_px(geom):
    return shtransform(lambda x, y: ((x - BOUNDS[0]) / 10, (BOUNDS[3] - y) / 10), geom)
plant_px, good_px = to_px(plant), to_px(good)
for ax, yr in zip(axes.ravel(), years):
    ax.imshow(annual[yr], cmap="RdYlGn", vmin=0.1, vmax=0.9)
    for g, c in ((plant_px, "#2166ac"), (good_px, "#000")):
        xs, ys = g.exterior.xy
        ax.plot(xs, ys, color=c, lw=1.1)
    ax.set_title(str(yr), fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_xlim(40, 200); ax.set_ylim(200, 40)
for ax in axes.ravel()[len(years):]:
    ax.axis("off")
fig.suptitle("Dry-season (Jun–Sep) median NDVI — plantation (blue) and good zone (black)", fontsize=12)
fig.tight_layout(rect=[0, 0, 1, 0.97])
fig.savefig(FIGS / "fig4_smallmultiples.png", dpi=130)

# ---------------------------------------------------------------- separability by year (pixel level)
sep_rows = []
for yr in years:
    a = annual[yr]
    gvals = a[masks["good"]]; pvals = a[masks["poor"]]
    gvals = gvals[np.isfinite(gvals)]; pvals = pvals[np.isfinite(pvals)]
    if len(gvals) < 15 or len(pvals) < 50:
        continue
    u, p = sstats.mannwhitneyu(gvals, pvals, alternative="greater")
    d = (np.mean(gvals) - np.mean(pvals)) / np.sqrt((np.var(gvals) + np.var(pvals)) / 2)
    sep_rows.append({"year": yr, "p": float(p), "cohens_d": float(d),
                     "good_med": float(np.median(gvals)), "poor_med": float(np.median(pvals))})
sep = pd.DataFrame(sep_rows)
sep.to_json(CACHE / "separability.json", orient="records")
sig = sep[(sep.p < 0.01) & (sep.cohens_d > 0.8)]
stats_out["first_separable_year"] = int(sig.year.min()) if len(sig) else None
stats_out["separability"] = sep_rows
print(sep.to_string(index=False))

# fig 5: pixel distributions (mixture check) for recent epoch
recent = np.nanmean(np.stack([annual[y] for y in years if y >= 2024]), axis=0)
fig, ax = plt.subplots(figsize=(9, 4.4), facecolor="white")
g = recent[masks["good"]]; p = recent[masks["poor"]]
bins = np.linspace(0.3, 0.95, 40)
ax.hist(p[np.isfinite(p)], bins=bins, density=True, alpha=0.6, color=COLORS["poor"],
        label=f"poor stratum ({np.isfinite(p).sum()} px)")
ax.hist(g[np.isfinite(g)], bins=bins, density=True, alpha=0.65, color=COLORS["good"],
        label=f"good zone ({np.isfinite(g).sum()} px)")
ax.set_xlabel("dry-season NDVI (2024–2026 mean, per 10 m pixel)")
ax.set_ylabel("density")
ax.set_title("Pixel-level distributions — poor stratum as a mixture (unmapped good clumps pull the right tail)")
ax.legend()
fig.tight_layout()
fig.savefig(FIGS / "fig5_distributions.png", dpi=140)
stats_out["recent_pixel_overlap"] = round(float(np.mean(
    p[np.isfinite(p)] > np.median(g[np.isfinite(g)]))), 3)

# ---------------------------------------------------------------- fig 6: chronology (greening onset)
onset = np.full(SHAPE10, np.nan)
yrs_arr = sorted(annual.keys())
stack = np.stack([annual[y] for y in yrs_arr])
green = stack > 0.55
for i in range(len(yrs_arr) - 1):
    newly = green[i] & green[i + 1] & ~np.isfinite(onset)
    onset[newly] = yrs_arr[i]
onset_in = np.where(masks["whole"], onset, np.nan)
fig, ax = plt.subplots(figsize=(7.5, 6.5), facecolor="white")
im = ax.imshow(onset_in, cmap="viridis", vmin=2009, vmax=2024)
for g_, c in ((plant_px, "#2166ac"), (good_px, "#e31a1c")):
    xs, ys = g_.exterior.xy
    ax.plot(xs, ys, color=c, lw=1.4)
ax.set_xlim(60, 190); ax.set_ylim(190, 60)
ax.set_xticks([]); ax.set_yticks([])
plt.colorbar(im, ax=ax, label="greening onset year (NDVI > 0.55 sustained 2 yrs)")
ax.set_title("Recovered planting chronology (desk estimate)\nLandsat 30 m before 2016 — blocky early phases expected")
fig.tight_layout()
fig.savefig(FIGS / "fig6_chronology.png", dpi=140)
vals, cnt = np.unique(onset_in[np.isfinite(onset_in)], return_counts=True)
stats_out["onset_ha_by_year"] = {int(v): round(float(c) * 0.01, 1) for v, c in zip(vals, cnt)}

json.dump(stats_out, open(CACHE / "stats.json", "w"), indent=1)
print(json.dumps(stats_out, indent=1)[:1500])
print("figures written to", FIGS)
