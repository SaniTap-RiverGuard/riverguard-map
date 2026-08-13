#!/usr/bin/env python3
"""Step 1: Build the east-coast river segment set from HydroRIVERS.

- Loads HydroRIVERS Africa (data/raw/HydroRIVERS_v10_af_shp.zip), bbox-filtered
  to the study area.
- Classifies each basin (MAIN_RIV group) as east/south-draining by testing its
  mouth (terminal reach, NEXT_DOWN==0) against the coastal separator polyline
  in config.json. West/north-coast basins are dropped.
- Drops headwater reaches below min_upland_km2.
- Splits reaches into ~segment_length_m segments (UTM 38S, EPSG:32738) and
  writes data/derived/segments_raw.gpkg with per-segment buffers for sampling.

Run: .venv/bin/python pipeline/01_rivers.py
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString
from shapely.ops import substring

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"
DERIVED.mkdir(parents=True, exist_ok=True)

SA = CFG["study_area"]
BBOX = (SA["lon_min"], SA["lat_min"], SA["lon_max"], SA["lat_max"])
SEP = np.array(CFG["study_area"]["coastal_separator"]["points"])  # [lat, lon]
UTM = "EPSG:32738"  # UTM 38S; <=0.5% scale error at the eastern edge, fine for 500 m segmentation


def separator_lon(lat):
    """Min longitude for an east/south-coast mouth at this latitude (linear interp)."""
    lats, lons = SEP[:, 0], SEP[:, 1]
    # np.interp needs increasing x; lats are decreasing
    return float(np.interp(lat, lats[::-1], lons[::-1]))


print("Reading HydroRIVERS (bbox-filtered)...")
riv = gpd.read_file(
    f"zip://{RAW}/HydroRIVERS_v10_af_shp.zip!HydroRIVERS_v10_af_shp/HydroRIVERS_v10_af.shp",
    bbox=BBOX,
)
print(f"  {len(riv)} reaches in bbox")

# Basin classification by mouth position
terminals = riv[riv["NEXT_DOWN"] == 0]
keep_basins = set()
for _, t in terminals.iterrows():
    geom = t.geometry
    line = geom.geoms[-1] if geom.geom_type == "MultiLineString" else geom
    mouth = line.coords[-1]  # HydroRIVERS digitises upstream->downstream
    lon, lat = mouth[0], mouth[1]
    if lon >= separator_lon(lat):
        keep_basins.add(t["MAIN_RIV"])
print(f"  {len(terminals)} terminal reaches in bbox, {len(keep_basins)} east/south-draining basins kept")

riv = riv[riv["MAIN_RIV"].isin(keep_basins)].copy()
print(f"  {len(riv)} reaches after basin filter")

min_upland = SA.get("min_upland_km2", 0)
riv = riv[riv["UPLAND_SKM"] >= min_upland].copy()
print(f"  {len(riv)} reaches after min upland area filter (>= {min_upland} km2)")

# Segment in metric CRS
seg_len = CFG["segmentation"]["segment_length_m"]
riv_m = riv.to_crs(UTM)

records = []
for _, r in riv_m.iterrows():
    geom = r.geometry
    lines = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
    for li, line in enumerate(lines):
        total = line.length
        if total < 30:  # degenerate slivers
            continue
        n = max(1, round(total / seg_len))
        step = total / n
        for i in range(n):
            piece = substring(line, i * step, (i + 1) * step)
            if piece.geom_type != "LineString" or piece.length < 1:
                continue
            records.append({
                "hyriv_id": int(r["HYRIV_ID"]),
                "part": li,
                "seg_idx": i,
                "ord_stra": int(r["ORD_STRA"]),
                "upland_km2": float(r["UPLAND_SKM"]),
                "dis_m3s": float(r["DIS_AV_CMS"]),
                "length_m": round(piece.length, 1),
                "geometry": piece,
            })

segs = gpd.GeoDataFrame(records, crs=UTM)
segs["seg_id"] = [f"s{i}" for i in range(len(segs))]
print(f"  {len(segs)} segments of ~{seg_len} m "
      f"({segs['length_m'].sum() / 1000:.0f} km total bank line)")

buffer_m = CFG["segmentation"]["buffer_m"]
segs["buffer_geom"] = segs.geometry.buffer(buffer_m, cap_style="flat")

# Save: lines and buffers as separate layers, both in EPSG:4326
lines_wgs = segs.drop(columns=["buffer_geom"]).to_crs("EPSG:4326")
buf_wgs = segs.set_geometry("buffer_geom").drop(columns=["geometry"]).rename_geometry("geometry").to_crs("EPSG:4326")

out = DERIVED / "segments_raw.gpkg"
lines_wgs.to_file(out, layer="lines", driver="GPKG")
buf_wgs.to_file(out, layer="buffers", driver="GPKG")
print(f"Wrote {out} (layers: lines, buffers)")

# Quick sanity: nearest segments to the two ground-truth points
for chk in CFG["sanity_checks"]:
    d = lines_wgs.distance(gpd.points_from_xy([chk["lon"]], [chk["lat"]])[0]).min()
    print(f"  sanity '{chk['name']}': nearest segment {d * 111:.1f} km away" +
          (" — OK, network covers it" if d * 111 < 5 else " — WARNING: no nearby segment!"))
