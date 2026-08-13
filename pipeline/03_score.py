#!/usr/bin/env python3
"""Step 3: Sample rasters per segment buffer, compute suitability scores and
species recommendations, export the app GeoJSON.

Run after 02_rasters.py: .venv/bin/python pipeline/03_score.py
"""
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from shapely.geometry import mapping

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"

print("Loading segments...")
lines = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="lines")
buffers = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="buffers")
assert (lines["seg_id"] == buffers["seg_id"]).all()
print(f"  {len(lines)} segments")


class TileSampler:
    """Zonal sampling over one or more (possibly tiled) rasters in EPSG:4326.

    all_touched=True is essential for rasters whose pixels are as large as or
    larger than the polygons (SoilGrids 250 m, WorldClim 4.6 km): with the
    default centre-in-polygon test a 200 m-wide buffer frequently contains no
    pixel centre and sampling silently returns nothing. A centroid nearest-
    pixel fallback covers the remaining edge cases.
    """

    def __init__(self, paths, all_touched=False):
        self.datasets = [rasterio.open(p) for p in paths]
        self.bounds = [d.bounds for d in self.datasets]
        self.all_touched = all_touched

    def stats(self, geom):
        gx0, gy0, gx1, gy1 = geom.bounds
        vals = []
        for ds, b in zip(self.datasets, self.bounds):
            if gx1 < b.left or gx0 > b.right or gy1 < b.bottom or gy0 > b.top:
                continue
            win = from_bounds(max(gx0, b.left), max(gy0, b.bottom),
                              min(gx1, b.right), min(gy1, b.top), transform=ds.transform)
            win = win.round_lengths().round_offsets()
            if win.width < 1 or win.height < 1:  # polygon smaller than one pixel
                win = rasterio.windows.Window(win.col_off, win.row_off,
                                              max(1, win.width), max(1, win.height))
            data = ds.read(1, window=win)
            if data.size == 0:
                continue
            t = ds.window_transform(win)
            mask = geometry_mask([mapping(geom)], out_shape=data.shape, transform=t,
                                 invert=True, all_touched=self.all_touched)
            v = data[mask]
            if ds.nodata is not None:
                v = v[v != ds.nodata]
            if len(v):
                vals.append(v)
        if vals:
            return np.concatenate(vals)
        # fallback: nearest pixel at the centroid
        cx, cy = geom.centroid.x, geom.centroid.y
        for ds, b in zip(self.datasets, self.bounds):
            if not (b.left <= cx <= b.right and b.bottom <= cy <= b.top):
                continue
            v = list(ds.sample([(cx, cy)]))[0][0]
            if ds.nodata is None or v != ds.nodata:
                return np.array([v])
        return None


print("Opening rasters...")
clay_s = TileSampler([DERIVED / "clay_030_pct.tif"], all_touched=True)
sand_s = TileSampler([DERIVED / "sand_030_pct.tif"], all_touched=True)
prec_s = TileSampler([DERIVED / "annual_precip_mm.tif"], all_touched=True)
dem_s = TileSampler(sorted((RAW / "glo30").glob("*.tif")))
slope_s = TileSampler(sorted((DERIVED / "slope").glob("*.tif")))
wc_s = TileSampler(sorted((RAW / "worldcover").glob("*.tif")))

SC = CFG["scoring"]
W = CFG["weights"]
PLANTABLE = set(SC["landcover"]["plantable_classes"])
UNPLANTABLE_HARD = {80}  # permanent water never counts toward denominator? No — water in buffer is real constraint; keep in denominator.


def texture_score(clay, sand):
    t = SC["texture"]
    base = 100 * np.clip((clay - t["clay_lo"]) / (t["clay_hi"] - t["clay_lo"]), 0, 1)
    pen = t["sand_penalty_max"] * np.clip(
        (sand - t["sand_penalty_start"]) / (t["sand_penalty_full"] - t["sand_penalty_start"]), 0, 1)
    return float(np.clip(base - pen, 0, 100))


def slope_score(slp):
    s = SC["slope"]
    if slp >= s["exclude_deg"]:
        return None  # excluded
    if slp < s["optimal_min"]:
        return s["flat_score"] + (100 - s["flat_score"]) * slp / s["optimal_min"]
    if slp <= s["optimal_max"]:
        return 100.0
    return 100 + (s["steep_score"] - 100) * (slp - s["optimal_max"]) / (s["exclude_deg"] - s["optimal_max"])


def rainfall_score(mm):
    r = SC["rainfall"]
    return float(100 * np.clip((mm - r["mm_lo"]) / (r["mm_hi"] - r["mm_lo"]), 0, 1))


def species_mix(env):
    for rule in CFG["species_recommendation"]["rules"]:
        if eval(rule["if"], {"__builtins__": {}}, env):
            return rule["mix"]
    return {"balcooa": 100, "vulgaris": 0, "asper": 0}


print("Sampling and scoring...")
rows = []
n = len(buffers)
for i, (line_row, buf_row) in enumerate(zip(lines.itertuples(), buffers.itertuples())):
    if i % 2000 == 0:
        print(f"  {i}/{n}")
    g = buf_row.geometry

    clay_v = clay_s.stats(g)
    sand_v = sand_s.stats(g)
    prec_v = prec_s.stats(g)
    dem_v = dem_s.stats(g)
    slp_v = slope_s.stats(g)
    wc_v = wc_s.stats(g)

    if clay_v is None or wc_v is None or slp_v is None:
        continue  # no data (offshore sliver etc.)

    clay = float(np.nanmean(clay_v))
    sand = float(np.nanmean(sand_v)) if sand_v is not None else 50.0
    rain = float(np.nanmean(prec_v)) if prec_v is not None else 1500.0
    elev = float(np.nanmean(dem_v)) if dem_v is not None else 100.0
    slp = float(np.nanmean(slp_v))

    total_px = len(wc_v)
    plantable_frac = float(np.isin(wc_v, list(PLANTABLE)).sum() / total_px)
    dominant_class = int(np.bincount(wc_v.astype(int)).argmax())

    ts = texture_score(clay, sand)
    ss = slope_score(slp)
    rs = rainfall_score(rain)
    ls = 100 * plantable_frac

    ar = SC["aridity"]
    excluded = None
    if ss is None:
        excluded = "slope"
    elif plantable_frac < SC["landcover"]["min_plantable_fraction"]:
        excluded = "landcover"
    elif rain < ar["exclude_below_mm"]:
        excluded = "semi-arid"  # policy: failed field trials on semi-arid braided rivers

    if excluded:
        score, cls = 0.0, "excluded"
    else:
        score = W["texture"] * ts + W["landcover"] * ls + W["slope"] * ss + W["rainfall"] * rs
        fp = SC["flood_penalty"]
        if slp < fp["slope_deg"] and elev < fp["elev_m"]:
            score -= fp["penalty"]
        if rain < ar["penalty_to_mm"]:  # graded caution in the 700-1000 mm transitional band
            score -= ar["penalty"] * (ar["penalty_to_mm"] - rain) / (ar["penalty_to_mm"] - ar["exclude_below_mm"])
        score = float(np.clip(score, 0, 100))
        cls = None  # assigned by percentile after the loop

    mix = species_mix({"rain_mm": rain, "texture_score": ts, "clay": clay,
                       "slope": slp, "score": score, "true": True})

    rows.append({
        "seg_id": line_row.seg_id,
        "geometry": line_row.geometry,
        "score": round(score, 1),
        "cls": cls,
        "excl": excluded or "",
        "length_m": line_row.length_m,
        "clay": round(clay, 1),
        "sand": round(sand, 1),
        "slope": round(slp, 1),
        "elev": round(elev),
        "rain": round(rain),
        "lc_frac": round(plantable_frac, 2),
        "lc_dom": dominant_class,
        "mix_b": mix["balcooa"],
        "mix_v": mix["vulgaris"],
        "mix_a": mix["asper"],
        "ord": line_row.ord_stra,
        "riv": line_row.hyriv_id,
    })

out = gpd.GeoDataFrame(rows, crs="EPSG:4326")

# Relative classes: percentiles of the non-excluded population (absolute score kept)
cl = SC["classes"]
ok = out["cls"] != "excluded"
hi_thr = float(np.quantile(out.loc[ok, "score"], cl["high_quantile"]))
lo_thr = float(np.quantile(out.loc[ok, "score"], cl["low_quantile"]))
out.loc[ok, "cls"] = np.select(
    [out.loc[ok, "score"] >= hi_thr, out.loc[ok, "score"] < lo_thr],
    ["high", "low"], default="medium")
print(f"  scored {len(out)} segments; percentile class thresholds: high >= {hi_thr:.1f}, low < {lo_thr:.1f}")
print(out["cls"].value_counts().to_string())

# Sanity checks
print("Sanity checks:")
sanity_results = []
for chk in CFG["sanity_checks"]:
    from shapely.geometry import Point
    pt = Point(chk["lon"], chk["lat"])
    d = out.geometry.distance(pt)
    near = out.loc[d[d < 0.05].index]  # ~5 km
    if near.empty:
        res = f"  '{chk['name']}': NO SEGMENTS within 5 km — check network filter!"
    else:
        med = near["score"].median()
        classes = near["cls"].value_counts().to_dict()
        res = (f"  '{chk['name']}': {len(near)} segments within ~5 km, median score {med:.0f}, "
               f"classes {classes} (expected {chk['expect']})")
    print(res)
    sanity_results.append(res)
(DERIVED / "sanity_report.txt").write_text("\n".join(sanity_results))

gpq = DERIVED / "segments_scored.gpkg"
out.to_file(gpq, driver="GPKG")
print(f"Wrote {gpq}")

# --- Export compact GeoJSON for the app
print("Exporting app GeoJSON...")
tol = CFG["output"]["simplify_tolerance_deg"]
prec = CFG["output"]["coordinate_precision"]
exp = out.copy()
exp["geometry"] = exp.geometry.simplify(tol)

features = []
for r in exp.itertuples():
    coords = [[round(x, prec), round(y, prec)] for x, y in r.geometry.coords]
    features.append({
        "type": "Feature",
        "properties": {
            "id": r.seg_id, "s": r.score, "c": r.cls[0], "x": r.excl,
            "L": int(r.length_m), "cy": r.clay, "sd": r.sand, "sl": r.slope,
            "el": r.elev, "rn": r.rain, "lf": r.lc_frac, "ld": r.lc_dom,
            "mb": r.mix_b, "mv": r.mix_v, "ma": r.mix_a, "o": r.ord, "rv": r.riv,
        },
        "geometry": {"type": "LineString", "coordinates": coords},
    })

gj = {"type": "FeatureCollection",
      "_schema": {"id": "segment id", "s": "suitability 0-100", "c": "class h/m/l/e",
                  "x": "exclusion reason", "L": "bank length m", "cy": "clay % 0-30cm",
                  "sd": "sand %", "sl": "mean slope deg", "el": "mean elev m",
                  "rn": "annual rain mm", "lf": "plantable fraction", "ld": "dominant WorldCover class",
                  "mb": "recommended % balcooa", "mv": "% vulgaris", "ma": "% asper",
                  "o": "Strahler order", "rv": "HydroRIVERS id"},
      "features": features}

out_path = ROOT / CFG["output"]["geojson_path"]
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(gj, separators=(",", ":")))
mb = out_path.stat().st_size / 1e6
print(f"Wrote {out_path} ({mb:.1f} MB)")
if mb > CFG["output"]["max_geojson_mb"]:
    print(f"WARNING: exceeds {CFG['output']['max_geojson_mb']} MB target — consider PMTiles or stronger filtering")
