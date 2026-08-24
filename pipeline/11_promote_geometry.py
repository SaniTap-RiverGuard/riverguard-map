#!/usr/bin/env python3
"""Step 11 (PROMOTION, approved AM 2026-08-24): make the OSM-snapped geometry
(from 09_osm_snap.py) the canonical segment geometry, then re-run 03 and 05 so
every sampled attribute describes the corrected buffer.

What this does:
  1. One-time backups (never overwritten if already present):
       docs/data/segments.geojson  -> docs/data/segments_v1_hydrorivers.geojson
       data/derived/segments_raw.gpkg -> segments_raw_v1_hydrorivers.gpkg
  2. Rewrites segments_raw.gpkg: snapped segments get OSM-snapped line geometry
     and a rebuilt 100 m flat-cap buffer; a geom_src column ('snapped' /
     'hydrorivers') is added to the lines layer. seg_id and length_m are
     UNTOUCHED (share-links and bank-km accounting stay stable).
  3. Deletes the stale layer_*.parquet caches so 05 recomputes every
     geometry-dependent decision layer.

Then run: 03_score.py -> 05_decision_layers.py -> 07_roads_overlay.py
"""
import json
import shutil
from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
DERIVED = ROOT / "data" / "derived"
UTM = "EPSG:32738"
BUF_M = CFG["segmentation"]["buffer_m"]

# ---- 1. backups
live = ROOT / CFG["output"]["geojson_path"]
v1 = live.parent / "segments_v1_hydrorivers.geojson"
if not v1.exists():
    shutil.copy2(live, v1)
    print(f"Backed up live dataset -> {v1.name}")
raw = DERIVED / "segments_raw.gpkg"
raw_v1 = DERIVED / "segments_raw_v1_hydrorivers.gpkg"
if not raw_v1.exists():
    shutil.copy2(raw, raw_v1)
    print(f"Backed up raw segments -> {raw_v1.name}")

# ---- 2. promote snapped geometry into segments_raw.gpkg
print("Promoting snapped geometry...")
snap = gpd.read_file(DERIVED / "segments_snap.gpkg").set_index("seg_id")
lines = gpd.read_file(raw_v1, layer="lines").set_index("seg_id")
buffers = gpd.read_file(raw_v1, layer="buffers").set_index("seg_id")

common = lines.index.intersection(snap.index)
lines["geom_src"] = "hydrorivers"
lines.loc[common, "geom_src"] = snap.loc[common, "geom_src"]
snapped_ids = lines.index[lines["geom_src"] == "snapped"]
lines.loc[snapped_ids, "geometry"] = snap.loc[snapped_ids, "geometry"]
print(f"  {len(snapped_ids)}/{len(lines)} segments now on snapped geometry; "
      f"length_m untouched (bank-km accounting stable)")

new_buf = gpd.GeoSeries(lines.loc[snapped_ids, "geometry"], crs="EPSG:4326") \
    .to_crs(UTM).buffer(BUF_M, cap_style="flat").to_crs("EPSG:4326")
buffers["geom_src"] = lines["geom_src"]
buffers.loc[snapped_ids, "geometry"] = new_buf
print(f"  rebuilt {len(snapped_ids)} sampling buffers ({BUF_M} m flat-cap)")

lines.reset_index().to_file(raw, layer="lines", driver="GPKG")
buffers.reset_index().to_file(raw, layer="buffers", driver="GPKG")
print(f"Wrote {raw}")

# ---- 3. invalidate geometry-dependent decision-layer caches
for p in sorted(DERIVED.glob("layer_*.parquet")):
    p.unlink()
    print(f"  cleared cache {p.name}")
print("Done. Now run 03_score.py, 05_decision_layers.py, 07_roads_overlay.py.")
