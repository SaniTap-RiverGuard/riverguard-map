#!/usr/bin/env python3
"""Step 9 (VARIANT, approved AM 2026-08-24): geometry-only snap of reach lines
to OSM waterways. Staged: Strahler order >= 3 only. Segmentation, scoring
logic, segment IDs and length_m are untouched — only display/sampling geometry
moves, and only where the match is unambiguous.

Conservative per SEGMENT: the reach is densified and point-snapped as a whole
(for continuity), but each 500 m segment is accepted only if every one of its
own points found an OSM waterway within MAX_SNAP_M, no consecutive snapped
points jump more than JUMP_MAX_M (branch/tributary hopping), and the snapped
piece's length stays within LEN_RATIO of the original. Any failure -> that
segment falls back to HydroRIVERS geometry (flag 'hydrorivers'); OSM coverage
gaps therefore cost only the segments that touch them, not the whole reach.

Output: data/derived/segments_snap.gpkg
  seg_id, geom_src ('snapped'|'hydrorivers'), off_med_m (midpoint offset the
  snap corrected), geometry (EPSG:4326; original geometry where not snapped).

Does NOT touch docs/data/segments.geojson (the live dataset).
Run: .venv/bin/python pipeline/09_osm_snap.py
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString
from shapely.ops import substring

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"
UTM = "EPSG:32738"

MIN_ORDER = 3          # staged rollout: main stems + orders 3-4
DENSIFY_M = 30.0
MAX_SNAP_M = 1100.0    # covers the verified ~1 km worst offsets (Ambatondrazaka);
                       # wrong-river matches are guarded by the jump/length checks
JUMP_MAX_M = 200.0     # consecutive snapped points further apart than this = hop
LEN_RATIO = (0.6, 2.5)
SMOOTH_M = 5.0         # clean point-projection zigzag; << any sampling scale

print("Loading segments...")
lines = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="lines")
lines_m = lines.to_crs(UTM)
print(f"  {len(lines_m)} segments")

print("Loading OSM waterways...")
sa = CFG["study_area"]
osm = gpd.read_file(
    f"zip://{RAW}/madagascar-latest-free.shp.zip!gis_osm_waterways_free_1.shp",
    bbox=(sa["lon_min"] - 0.1, sa["lat_min"] - 0.1, sa["lon_max"] + 0.1, sa["lat_max"] + 0.1))
osm = osm[osm["fclass"].isin(["river", "stream", "canal"])]
osm_m = osm.to_crs(UTM)
ways = osm_m.geometry.values
tree = shapely.STRtree(ways)
print(f"  {len(ways)} waterway lines")

print("Snapping reaches (order >= %d), per-segment acceptance..." % MIN_ORDER)
res_geom, res_src, res_off = {}, {}, {}
groups = lines_m.sort_values("seg_idx").groupby(["hyriv_id", "part"])
n_reach = 0
for (rid, part), g in groups:
    segs = list(g.itertuples())
    if segs[0].ord_stra < MIN_ORDER:
        for s in segs:
            res_geom[s.seg_id], res_src[s.seg_id], res_off[s.seg_id] = s.geometry, "hydrorivers", 0.0
        continue
    n_reach += 1
    coords = [np.asarray(s.geometry.coords) for s in segs]
    reach = LineString(np.vstack([coords[0]] + [c[1:] for c in coords[1:]]))
    # densify the WHOLE reach so continuity checks see across segment joins
    n_pts = max(2, int(np.ceil(reach.length / DENSIFY_M)) + 1)
    chain = np.linspace(0, reach.length, n_pts)
    pts = shapely.line_interpolate_point(reach, chain)
    idx = tree.nearest(pts)
    near_ways = ways[idx]
    dists = shapely.distance(pts, near_ways)
    snapped_pts = shapely.line_interpolate_point(
        near_ways, shapely.line_locate_point(near_ways, pts))
    xy = shapely.get_coordinates(snapped_pts)
    valid = dists <= MAX_SNAP_M
    jumps = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    pair_ok = (jumps <= JUMP_MAX_M) & valid[:-1] & valid[1:]

    # per-segment acceptance over the segment's own chainage span
    orig_lens = np.array([s.geometry.length for s in segs])
    bounds = np.concatenate([[0.0], np.cumsum(orig_lens)])
    for k, s in enumerate(segs):
        lo_i = int(np.searchsorted(chain, bounds[k], side="left"))
        hi_i = int(np.searchsorted(chain, bounds[k + 1], side="right")) - 1
        span = slice(max(lo_i - 1, 0), min(hi_i + 1, n_pts - 1) + 1)  # 1-pt overlap w/ neighbours
        seg_valid = valid[span].all() and pair_ok[span.start:span.stop - 1].all() \
            and (span.stop - span.start) >= 2
        piece = None
        if seg_valid:
            piece = LineString(xy[span]).simplify(SMOOTH_M)
            ratio = piece.length / max(s.geometry.length, 1)
            if not (LEN_RATIO[0] <= ratio <= LEN_RATIO[1]):
                piece = None
        if piece is None:
            res_geom[s.seg_id], res_src[s.seg_id], res_off[s.seg_id] = s.geometry, "hydrorivers", 0.0
        else:
            res_geom[s.seg_id] = piece
            res_src[s.seg_id] = "snapped"
            res_off[s.seg_id] = float(shapely.distance(
                s.geometry.interpolate(0.5, normalized=True), piece))
print(f"  {n_reach} eligible reaches processed")

out = lines_m[["seg_id", "hyriv_id", "part", "seg_idx", "ord_stra", "length_m"]].copy()
out["geom_src"] = out["seg_id"].map(res_src)
out["off_med_m"] = out["seg_id"].map(res_off).round(1)
out = gpd.GeoDataFrame(out, geometry=out["seg_id"].map(res_geom), crs=UTM).to_crs("EPSG:4326")

n_s = (out["geom_src"] == "snapped").sum()
elig = out["ord_stra"] >= MIN_ORDER
print(f"  segments snapped: {n_s}/{elig.sum()} eligible ({n_s/elig.sum()*100:.0f} %); "
      f"median corrected offset {out.loc[out['geom_src']=='snapped','off_med_m'].median():.0f} m, "
      f"p90 {out.loc[out['geom_src']=='snapped','off_med_m'].quantile(.9):.0f} m")

dst = DERIVED / "segments_snap.gpkg"
out.to_file(dst, driver="GPKG")
print(f"Wrote {dst}")
