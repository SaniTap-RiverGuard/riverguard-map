#!/usr/bin/env python3
"""Plantation tracker — Step 2c: biomass estimation module + boundary check.

Standing-biomass RANGES per stratum (good 0.56 ha / poor 17.6 ha / whole
18.1 ha), dry and wet (moisture 0.46, Assumptions!B25), anchored two ways:
  (a) culm-level allometry (mass ~ DBH^2): thin 1-4 cm "fishing-rod" culms
      carry roughly (D_thin/D_good)^2 of a proper culm's mass;
  (b) the xlsx per-clump curve as the "performing as modelled" upper reference.
Satellite roles: optical NDVI saturates over any closed leafy canopy (it sees
leaves, not stems) -> upper-bound bias on thin stands; Sentinel-1 VH adds a
structure-sensitive consistency check (C-band saturates ~50-100 t/ha AGB).

Also renders the boundary-discrepancy figure: spectrally similar blocks near
but OUTSIDE the corrected 18.1 ha boundary (candidate missed plantation area
vs the leased parcel of 20.97 ha).
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
from scipy import ndimage

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
AREAS = {"good": good.area / 1e4, "poor": poor.area / 1e4, "whole": plant.area / 1e4}
masks = {k: rfeatures.geometry_mask([g], out_shape=SHAPE10, transform=GRID10, invert=True)
         for k, g in {"good": good, "poor": poor, "whole": plant}.items()}

out = {"areas_ha": {k: round(v, 2) for k, v in AREAS.items()}}

# ================================================== boundary discrepancy check
# Re-run the step-1 spectral similarity on the recent dry-season composite and
# find similar blocks OUTSIDE the corrected boundary.
annual = np.load(CACHE / "annual_dry_ndvi.npz") if (CACHE / "annual_dry_ndvi.npz").exists() else None
if annual is not None:
    recent_years = [y for y in annual.files if int(y) >= 2024]
    comp = np.nanmean(np.stack([annual[y] for y in recent_years]), axis=0)
    seed = comp[masks["whole"]]
    lo, hi = np.nanpercentile(seed, 15), np.nanpercentile(seed, 99)
    sim = (comp >= lo) & (comp <= hi + 0.05)
    sim = ndimage.binary_opening(ndimage.binary_closing(sim, iterations=1), iterations=2)
    lab, n = ndimage.label(sim & ~masks["whole"])
    cand = np.zeros_like(sim)
    cand_ha = 0.0
    plant_px_mask = masks["whole"]
    dist_to_plant = ndimage.distance_transform_edt(~plant_px_mask) * 10
    blocks = []
    for c in range(1, n + 1):
        m = lab == c
        ha = m.sum() * 0.01
        d = dist_to_plant[m].min()
        if ha >= 0.5 and d <= 400:
            cand |= m
            cand_ha += ha
            blocks.append({"ha": round(ha, 1), "min_dist_m": round(float(d))})
    out["missed_block_candidates"] = {"total_ha": round(cand_ha, 1), "blocks": blocks,
        "criteria": "NDVI within plantation's spectral envelope, >=0.5 ha, within 400 m of the boundary"}
    fig, ax = plt.subplots(figsize=(8, 7), facecolor="white")
    ax.imshow(comp, cmap="Greys_r", vmin=0.2, vmax=0.95)
    overlay = np.zeros((*SHAPE10, 4))
    overlay[cand] = [0.95, 0.55, 0.1, 0.55]
    ax.imshow(overlay)
    from shapely.ops import transform as shtransform
    def to_px(g): return shtransform(lambda x, y: ((x - BOUNDS[0]) / 10, (BOUNDS[3] - y) / 10), g)
    for g_, c_, lw in ((to_px(plant), "#2166ac", 2), (to_px(good), "#1a9850", 1.5)):
        xs, ys = g_.exterior.xy
        ax.plot(xs, ys, color=c_, lw=lw)
    ax.set_xlim(20, 220); ax.set_ylim(220, 20)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(f"Boundary check: mapped bamboo canopy 18.13 ha (blue) within the leased parcel of 20.97 ha\n"
                 f"orange = spectrally similar blocks outside the mapped boundary "
                 f"({cand_ha:.1f} ha within 400 m — mostly natural vegetation; NDVI alone cannot tell)")
    fig.tight_layout()
    fig.savefig(FIGS / "fig7_boundary_check.png", dpi=140)
    print(f"boundary check: {cand_ha:.1f} ha of candidate blocks outside boundary; {len(blocks)} blocks")

# ================================================== biomass ranges
MOISTURE = 0.46                       # Assumptions!B25 (fresh culm moisture)
mc = json.load(open(HERE.parent.parent / "docs" / "data" / "model_config.json"))
curve = {r["year"]: r["total_biomass_kg"] for r in mc["species"]["balcooa"]["growth_curve"]}

# Plantation ages from the recovered chronology (fallback: planted ~2010-2013)
stats = json.load(open(CACHE / "stats.json")) if (CACHE / "stats.json").exists() else {}
onset = stats.get("onset_ha_by_year", {})

# Assumption ranges (explicit; field calibration collapses these):
CLUMPS_HA = (350, 500)                 # plantation spacing unknown; 4.5-5.5 m grids
GOOD_CULMS = (40, 70)                  # mature balcooa clump, 15 yr
GOOD_CULM_KG = (5, 10.3)               # xlsx 7 kg avg; Tana sample 10.3 kg dry
POOR_CULMS = (15, 45)                  # fishing-rod clumps: many thin culms
POOR_CULM_KG = (0.3, 1.5)              # allometric mass ~ D^2: (1-4 cm / 7-9 cm)^2 x proper culm

def rng(a, b):
    return np.array([a[0] * b[0], a[1] * b[1]])

good_clump_kg = rng(GOOD_CULMS, GOOD_CULM_KG)          # per clump, dry
poor_clump_kg = rng(POOR_CULMS, POOR_CULM_KG)
model_15yr = curve[15]                                 # 607.6 kg dry/clump at year 15

biomass = {}
for k, clump_kg in (("good", good_clump_kg), ("poor", poor_clump_kg)):
    t_ha = clump_kg * np.array(CLUMPS_HA) / 1000       # t dry / ha
    tot = t_ha * AREAS[k]
    biomass[k] = {"t_dry_per_ha": [round(float(x), 1) for x in t_ha],
                  "t_dry_total": [round(float(x), 1) for x in tot],
                  "t_wet_total": [round(float(x / (1 - MOISTURE)), 1) for x in tot],
                  "pct_of_model_yr15": [round(float(c / model_15yr * 100)) for c in clump_kg]}
whole_tot = np.array(biomass["good"]["t_dry_total"]) + np.array(biomass["poor"]["t_dry_total"])
biomass["whole"] = {"t_dry_total": [round(float(x), 1) for x in whole_tot],
                    "t_wet_total": [round(float(x / (1 - MOISTURE)), 1) for x in whole_tot]}
out["biomass"] = biomass
out["model_reference"] = {
    "clump_kg_yr15": model_15yr,
    "modelled_t_dry_per_ha_yr15_at_400_500": [round(model_15yr * 400 / 1000, 1), round(model_15yr * 500 / 1000, 1)],
    "note": "xlsx Biomass sheet, 'performing as modelled' upper reference"}
out["assumption_ranges"] = {"clumps_per_ha": CLUMPS_HA, "good_culms_per_clump": GOOD_CULMS,
    "good_culm_kg_dry": GOOD_CULM_KG, "poor_culms_per_clump": POOR_CULMS,
    "poor_culm_kg_dry": POOR_CULM_KG, "moisture_fresh": MOISTURE}

# biomass evolution: endpoint ranges back-cast along the model curve SHAPE,
# per-stratum onset (good zone anchored to model shape; poor scaled to its endpoint)
years = list(range(2009, 2027))
onset_year = {"good": 2011, "poor": 2012}
if onset:
    ha_sorted = sorted(((int(y), h) for y, h in onset.items()), key=lambda t: -t[1])
    if ha_sorted:
        onset_year["poor"] = ha_sorted[0][0]
evol = {}
for k in ("good", "poor"):
    lo, hi = biomass[k]["t_dry_total"]
    shape = np.array([curve.get(min(max(y - onset_year[k], 0), 20), 0) if y >= onset_year[k] else 0
                      for y in years], dtype=float)
    shape = shape / shape[-1] if shape[-1] > 0 else shape
    evol[k] = {"years": years, "lo": [round(float(x), 1) for x in lo * shape],
               "hi": [round(float(x), 1) for x in hi * shape]}
out["evolution"] = evol

fig, ax = plt.subplots(figsize=(10, 4.6), facecolor="white")
for k, c in (("good", "#1a9850"), ("poor", "#c0392b")):
    ax.fill_between(years, evol[k]["lo"], evol[k]["hi"], color=c, alpha=0.25)
    ax.plot(years, np.mean([evol[k]["lo"], evol[k]["hi"]], axis=0), color=c, lw=2,
            label=f"{k} stratum ({AREAS[k]:.1f} ha), range mid")
model_tot = [curve.get(min(max(y - onset_year['poor'], 0), 20), 0) * 450 / 1000 * AREAS['whole'] for y in years]
ax.plot(years, model_tot, "--", color="#555", lw=1.8,
        label="xlsx curve if WHOLE site performed as modelled (450 clumps/ha)")
ax.set_ylabel("standing dry biomass (t)")
ax.set_title("Estimated standing biomass evolution — ranges reflect ±40-50% desk uncertainty")
ax.legend(fontsize=9)
fig.tight_layout()
fig.savefig(FIGS / "fig8_biomass.png", dpi=140)

json.dump(out, open(CACHE / "biomass.json", "w"), indent=1)
print(json.dumps(out, indent=1)[:2000])
