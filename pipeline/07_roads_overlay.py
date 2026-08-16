#!/usr/bin/env python3
"""Step 7: Roads overlay for the app — the OSM roads the access analysis used,
plus derived river-road access points (midpoints of road-adjacent segments).

Outputs docs/data/roads.geojson and docs/data/access_points.geojson (compact,
lazy-loaded by the app when the overlay is toggled on).

Run after 05: .venv/bin/python pipeline/07_roads_overlay.py
"""
import json
from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"
SA = CFG["study_area"]
CLASSES = CFG["decision_layers"]["access"]["road_classes"]

print("Reading roads...")
roads = gpd.read_file(f"zip://{RAW}/madagascar-latest-free.shp.zip!gis_osm_roads_free_1.shp",
                      bbox=(SA["lon_min"], SA["lat_min"], SA["lon_max"], SA["lat_max"]))
roads = roads[roads["fclass"].isin(CLASSES)][["fclass", "geometry"]]
print(f"  {len(roads)} road features in study bbox")

CODE = {"trunk": "t", "primary": "p", "secondary": "s", "tertiary": "y", "unclassified": "u", "track": "k"}
MAIN = {"trunk", "primary", "secondary", "tertiary"}


def export(subset, tol, dec, path):
    subset = subset.copy()
    subset["geometry"] = subset.geometry.simplify(tol)
    feats = []
    for r in subset.itertuples():
        geoms = r.geometry.geoms if r.geometry.geom_type == "MultiLineString" else [r.geometry]
        for g in geoms:
            coords = [[round(x, dec), round(y, dec)] for x, y in g.coords]
            if len(coords) < 2:
                continue
            feats.append({"type": "Feature", "properties": {"c": CODE[r.fclass]},
                          "geometry": {"type": "LineString", "coordinates": coords}})
    gj = {"type": "FeatureCollection",
          "_schema": {"c": "road class: t trunk, p primary, s secondary, y tertiary, u unclassified, k track (OSM, classes used by the access analysis)"},
          "features": feats}
    path.write_text(json.dumps(gj, separators=(",", ":")))
    print(f"Wrote {path} ({path.stat().st_size/1e6:.1f} MB, {len(feats)} lines)")


# Main roads: always available to the overlay. Minor roads (tracks/unclassified)
# are the bulk of the network; the overlay exists to verify RIVER-road access,
# so only minor roads within 1 km of a scored segment are shipped, simplified
# harder (~100 m tolerance, 3-decimal coords ≈ 110 m precision).
export(roads[roads.fclass.isin(MAIN)], 0.0004, 4, ROOT / "docs" / "data" / "roads.geojson")
segs = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="lines")
seg_buf = gpd.GeoDataFrame(geometry=segs.to_crs("EPSG:32738").buffer(1000), crs="EPSG:32738").to_crs("EPSG:4326")
minor = roads[~roads.fclass.isin(MAIN)]
near = gpd.sjoin(minor, seg_buf[["geometry"]], predicate="intersects", how="inner")
near = near[~near.index.duplicated()]
print(f"  minor roads within 1 km of rivers: {len(near)} of {len(minor)}")
export(near.drop(columns=["index_right"]), 0.001, 3, ROOT / "docs" / "data" / "roads_minor.geojson")

print("Access points (road-adjacent segment midpoints)...")
e = gpd.read_file(DERIVED / "segments_enriched.gpkg")
adj = e[e.access == "road"]
mids = adj.geometry.interpolate(0.5, normalized=True)
pts = [{"type": "Feature", "properties": {"id": sid},
        "geometry": {"type": "Point", "coordinates": [round(p.x, 4), round(p.y, 4)]}}
       for sid, p in zip(adj.seg_id.values, mids.values)]
gj2 = {"type": "FeatureCollection",
       "_note": "river-road access points = midpoints of segments with a road within 250 m",
       "features": pts}
out2 = ROOT / "docs" / "data" / "access_points.geojson"
out2.write_text(json.dumps(gj2, separators=(",", ":")))
print(f"Wrote {out2} ({out2.stat().st_size/1e6:.1f} MB, {len(pts)} points)")
