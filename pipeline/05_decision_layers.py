#!/usr/bin/env python3
"""Step 5: Decision-support layers (ADDITIVE — never touches suitability
scores/classes). Enriches segments_scored.gpkg with:

  1. Population within 2/5 km of segment midpoint (WorldPop 2020 constrained)
  2. Distance to nearest protected area (WDPA, derived attrs only — licence)
     and to the nearest large natural-forest block (WorldCover tree cover)
  3. Access: road-adjacency, downstream pirogue reachability, access class
  4. Land-use composition of the buffer + likely-paddy + fire pressure (FIRMS)
  5. Cyclone exposure (IBTrACS passages within 100 km, 40 yr; max category)

Writes segments_enriched.gpkg and regenerates docs/data/segments.geojson.
All thresholds: pipeline/config.json -> decision_layers.

Run after 03_score.py: .venv/bin/python pipeline/05_decision_layers.py
"""
import json
import math
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import from_bounds
from scipy import ndimage
from shapely.geometry import Point, mapping
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
DL = CFG["decision_layers"]
RAW = ROOT / "data" / "raw"
DERIVED = ROOT / "data" / "derived"
UTM = "EPSG:32738"

print("Loading scored segments...")
segs = gpd.read_file(DERIVED / "segments_scored.gpkg")
segs_utm = segs.to_crs(UTM)
mid_utm = segs_utm.geometry.interpolate(0.5, normalized=True)
mid = mid_utm.to_crs("EPSG:4326")
mx = np.array([p.x for p in mid_utm])
my = np.array([p.y for p in mid_utm])
print(f"  {len(segs)} segments")


# ---------------------------------------------------------------- 1. population
def layer_population():
    cache = DERIVED / "layer_population.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    cfgp = DL["population"]
    agg = cfgp["aggregate_to_m"]
    print("Population: aggregating WorldPop to", agg, "m...")
    with rasterio.open(RAW / "mdg_ppp_2020_constrained.tif") as src:
        data = src.read(1)
        data = np.where(data == src.nodata, 0, data).astype("float64")
        t = src.transform
        px_m = abs(t.a) * 111320 * math.cos(math.radians(-19))  # ~100 m
        f = max(1, round(agg / px_m))
        H, W = data.shape
        Hc, Wc = H // f, W // f
        coarse = data[:Hc * f, :Wc * f].reshape(Hc, f, Wc, f).sum(axis=(1, 3))
        # coarse-grid geolocation
        lon0, lat0 = t.c, t.f
        dlon, dlat = t.a * f, t.e * f
    out = {}
    for r_km in cfgp["radii_km"]:
        r_px = int(round(r_km * 1000 / agg))
        yy, xx = np.ogrid[-r_px:r_px + 1, -r_px:r_px + 1]
        kernel = (xx * xx + yy * yy) <= r_px * r_px
        print(f"  disk convolution r={r_km} km ({kernel.sum()} px kernel)...")
        summed = ndimage.convolve(coarse, kernel.astype("float64"), mode="constant")
        cols = ((mid.x.values - lon0) / dlon).astype(int)
        rows = ((mid.y.values - lat0) / dlat).astype(int)
        ok = (rows >= 0) & (rows < Hc) & (cols >= 0) & (cols < Wc)
        vals = np.zeros(len(segs))
        vals[ok] = summed[rows[ok], cols[ok]]
        out[f"pop{r_km}k"] = np.round(vals).astype(int)
    df = pd.DataFrame(out, index=segs.index)
    df.to_parquet(cache)
    return df


# ---------------------------------------------------------------- 2. WDPA + forest
def layer_protected():
    cache = DERIVED / "layer_protected.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    print("Protected areas (WDPA)...")
    # WDPA country zips contain up to 3 sub-zips of polygon shapefiles
    frames = []
    with zipfile.ZipFile(RAW / "wdpa_mdg.zip") as z:
        subs = [n for n in z.namelist() if n.endswith(".zip")]
        tmp = RAW / "wdpa_extract"
        tmp.mkdir(exist_ok=True)
        z.extractall(tmp)
    for sub in sorted(tmp.glob("*.zip")):
        with zipfile.ZipFile(sub) as sz:
            shp = [n for n in sz.namelist() if n.endswith("polygons.shp")]
        for s in shp:
            frames.append(gpd.read_file(f"zip://{sub}!{s}")[["NAME", "DESIG_ENG", "geometry"]])
    pa = pd.concat(frames, ignore_index=True)
    pa = gpd.GeoDataFrame(pa, crs="EPSG:4326").to_crs(UTM)
    pa = pa[pa.geometry.is_valid & ~pa.geometry.is_empty]
    print(f"  {len(pa)} PA polygons")
    tree = STRtree(pa.geometry.values)
    pts = mid_utm.geometry.values
    idx = tree.nearest(pts)
    d_pa = np.array([pts[i].distance(pa.geometry.values[j]) for i, j in enumerate(idx)]) / 1000
    names = pa["NAME"].values[idx]
    desig = pa["DESIG_ENG"].values[idx]

    print("Natural-forest blocks from WorldCover...")
    cfgf = DL["protected_areas"]
    grid = cfgf["forest_grid_m"]
    # build 100m tree-fraction grid over study bbox from the WorldCover tiles
    SA = CFG["study_area"]
    b = (SA["lon_min"] - 0.05, SA["lat_min"] - 0.05, SA["lon_max"] + 0.05, SA["lat_max"] + 0.05)
    deg = grid / 111320
    W = int((b[2] - b[0]) / deg); H = int((b[3] - b[1]) / deg)
    tree_cnt = np.zeros((H, W), dtype="int32")
    tot_cnt = np.zeros((H, W), dtype="int32")
    for tile in sorted((RAW / "worldcover").glob("*.tif")):
        with rasterio.open(tile) as src:
            tb = src.bounds
            ix0, ix1 = max(tb.left, b[0]), min(tb.right, b[2])
            iy0, iy1 = max(tb.bottom, b[1]), min(tb.top, b[3])
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            win = from_bounds(ix0, iy0, ix1, iy1, transform=src.transform)
            a = src.read(1, window=win.round_lengths().round_offsets())
            t = src.window_transform(win)
            f = max(1, round(deg / abs(t.a)))  # 10m px per 100m cell
            hh, ww = a.shape[0] // f, a.shape[1] // f
            if hh < 1 or ww < 1:
                continue
            blk = a[:hh * f, :ww * f].reshape(hh, f, ww, f)
            tr = (blk == 10).sum(axis=(1, 3))
            # place into global grid
            c0 = int((t.c - b[0]) / deg); r0 = int((b[3] - t.f) / deg)
            r1, c1 = min(r0 + hh, H), min(c0 + ww, W)
            if r0 < 0 or c0 < 0:
                continue
            tree_cnt[r0:r1, c0:c1] += tr[:r1 - r0, :c1 - c0]
            tot_cnt[r0:r1, c0:c1] += f * f
    frac = np.where(tot_cnt > 0, tree_cnt / np.maximum(tot_cnt, 1), 0)
    forest = frac >= cfgf["forest_tree_fraction_min"]
    lab, n = ndimage.label(forest)
    sizes = ndimage.sum(forest, lab, range(1, n + 1))
    cell_ha = (grid * grid) / 10000
    big = np.isin(lab, np.where(sizes * cell_ha >= cfgf["forest_block_min_ha"])[0] + 1)
    print(f"  {n} forest components, {big.sum()} cells in blocks >= {cfgf['forest_block_min_ha']} ha")
    dist_px = ndimage.distance_transform_edt(~big)
    cols = ((mid.x.values - b[0]) / deg).astype(int).clip(0, W - 1)
    rows = ((b[3] - mid.y.values) / deg).astype(int).clip(0, H - 1)
    d_forest = dist_px[rows, cols] * grid / 1000  # km (approx; grid in m)

    df = pd.DataFrame({
        "pa_km": np.round(d_pa, 1), "pa_name": names, "pa_desig": desig,
        "forest_km": np.round(d_forest, 1),
    }, index=segs.index)
    df.to_parquet(cache)
    return df


# ---------------------------------------------------------------- 3. access
def layer_access():
    cache = DERIVED / "layer_access.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    cfga = DL["access"]
    print("Access: roads from Geofabrik...")
    roads = gpd.read_file(f"zip://{RAW}/madagascar-latest-free.shp.zip!gis_osm_roads_free_1.shp",
                          bbox=tuple(segs.total_bounds))
    roads = roads[roads["fclass"].isin(cfga["road_classes"])].to_crs(UTM)
    print(f"  {len(roads)} road features")
    rtree = STRtree(roads.geometry.values)
    snap = cfga["road_snap_m"]
    seg_geoms = segs_utm.geometry.values
    road_adj = np.zeros(len(segs), bool)
    for i, g in enumerate(seg_geoms):
        cand = rtree.query(g.buffer(snap))
        road_adj[i] = any(g.distance(roads.geometry.values[j]) <= snap for j in cand)
    print(f"  {road_adj.sum()} road-adjacent segments")

    print("  river line gradient from DEM...")
    dem_paths = sorted((RAW / "glo30").glob("*.tif"))
    dems = [rasterio.open(p) for p in dem_paths]
    bounds = [d.bounds for d in dems]

    def elev_at(lon, lat):
        for ds, bb in zip(dems, bounds):
            if bb.left <= lon <= bb.right and bb.bottom <= lat <= bb.top:
                v = list(ds.sample([(lon, lat)]))[0][0]
                return float(v)
        return np.nan

    lines_wgs = segs.geometry.values
    grad = np.zeros(len(segs))
    for i, line in enumerate(lines_wgs):
        p0, p1 = line.coords[0], line.coords[-1]
        e0, e1 = elev_at(*p0[:2]), elev_at(*p1[:2])
        L = segs["length_m"].iloc[i]
        grad[i] = abs(e0 - e1) / L * 100 if (L > 0 and np.isfinite(e0) and np.isfinite(e1)) else 0.0
    for d in dems:
        d.close()

    print("  downstream traversal...")
    # order segments within each reach; reach-to-reach via NEXT_DOWN from HydroRIVERS
    riv = gpd.read_file(
        f"zip://{RAW}/HydroRIVERS_v10_af_shp.zip!HydroRIVERS_v10_af_shp/HydroRIVERS_v10_af.shp",
        bbox=tuple(segs.total_bounds))[["HYRIV_ID", "NEXT_DOWN"]]
    nxt = dict(zip(riv.HYRIV_ID.astype(int), riv.NEXT_DOWN.astype(int)))

    raw = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="lines",
                        columns=["seg_id", "part", "seg_idx"])
    raw = raw.set_index("seg_id").loc[segs["seg_id"].values]
    df = pd.DataFrame({"riv": segs["riv"].astype(int).values, "part": raw["part"].values,
                       "idx": raw["seg_idx"].values, "len": segs["length_m"].values})
    df.index = segs.index
    order = df.sort_values(["riv", "part", "idx"]).index.to_numpy()
    pos_in_reach = {}
    reach_first, reach_last = {}, {}
    for i in order:
        r = df.at[i, "riv"]
        pos_in_reach.setdefault(r, []).append(i)
    for r, lst in pos_in_reach.items():
        reach_first[r] = lst[0]
        reach_last[r] = lst[-1]
    succ = np.full(len(segs), -1)
    for r, lst in pos_in_reach.items():
        for a, bnx in zip(lst, lst[1:]):
            succ[a] = bnx
        nd = nxt.get(r, 0)
        if nd and nd in reach_first:
            succ[reach_last[r]] = reach_first[nd]

    barrier = (grad > cfga["rapids_gradient_pct"]) | (segs["excl"].values == "semi-arid")
    boat = np.zeros(len(segs), bool)
    dist_km = np.full(len(segs), np.inf)
    from collections import deque
    q = deque()
    for i in np.where(road_adj)[0]:
        dist_km[i] = 0.0
        boat[i] = False  # road-adjacent counted separately
        q.append(i)
    while q:
        i = q.popleft()
        j = succ[i]
        if j < 0 or barrier[j]:
            continue
        nd = dist_km[i] + (df.at[i, "len"] + df.at[j, "len"]) / 2000
        if nd < dist_km[j]:
            dist_km[j] = nd
            boat[j] = True
            q.append(j)
    # remote: straight-line distance to nearest access point
    acc_idx = np.where(road_adj)[0]
    if len(acc_idx):
        acc_tree = STRtree([Point(mx[i], my[i]) for i in acc_idx])
        for i in np.where(~road_adj & ~boat)[0]:
            j = acc_tree.nearest(Point(mx[i], my[i]))
            dist_km[i] = Point(mx[i], my[i]).distance(Point(mx[acc_idx[j]], my[acc_idx[j]])) / 1000
    cls = np.where(road_adj, "road", np.where(boat, "boat", "remote"))
    print(f"  access classes: road {road_adj.sum()}, boat {boat.sum()}, remote {(cls=='remote').sum()}")
    out = pd.DataFrame({"access": cls, "access_km": np.round(np.where(np.isfinite(dist_km), dist_km, -1), 1),
                        "riv_grad": np.round(grad, 2)}, index=segs.index)
    out.to_parquet(cache)
    return out


# ---------------------------------------------------------------- 4. land use + fire
def layer_landuse_fire():
    cache = DERIVED / "layer_landuse.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    cfgl = DL["landuse_fire"]
    print("Land-use composition + likely-paddy...")
    wc_paths = sorted((RAW / "worldcover").glob("*.tif"))
    wcs = [rasterio.open(p) for p in wc_paths]
    wbounds = [d.bounds for d in wcs]
    slope_paths = sorted((DERIVED / "slope").glob("*.tif"))
    slopes = [rasterio.open(p) for p in slope_paths]
    sbounds = [d.bounds for d in slopes]
    buffers = gpd.read_file(DERIVED / "segments_raw.gpkg", layer="buffers").set_index("seg_id")
    buffers = buffers.loc[segs["seg_id"].values]

    from rasterio.features import geometry_mask
    pct = {k: np.zeros(len(segs)) for k in ("crop", "grass", "shrub", "bare", "wetland", "paddy")}
    for i, geom in enumerate(buffers.geometry.values):
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
            pct["crop"][i] = (v == 40).sum() / tot
            pct["grass"][i] = (v == 30).sum() / tot
            pct["shrub"][i] = (v == 20).sum() / tot
            pct["bare"][i] = (v == 60).sum() / tot
            pct["wetland"][i] = (v == 90).sum() / tot
            # Likely-paddy, PER-PIXEL (agreed 2026-08-14): cropland pixels whose
            # own 30 m slope is < paddy_slope_max_deg, in buffers touching
            # water/wetland. (The earlier buffer-mean slope test was mis-specced:
            # banks dominate the mean; the paddy signal is in the flat pixels.)
            if pct["crop"][i] > 0 and ((v == 80).any() or (v == 90).any()):
                rr, cc = np.where(m)
                crop_sel = a[m] == 40
                if crop_sel.any():
                    xs = t.c + (cc[crop_sel] + 0.5) * t.a
                    ys = t.f + (rr[crop_sel] + 0.5) * t.e
                    for sds, sbb in zip(slopes, sbounds):
                        if not (sbb.left <= xs.mean() <= sbb.right and sbb.bottom <= ys.mean() <= sbb.top):
                            continue
                        swin = from_bounds(max(gx0, sbb.left), max(gy0, sbb.bottom),
                                           min(gx1, sbb.right), min(gy1, sbb.top),
                                           transform=sds.transform).round_lengths().round_offsets()
                        if swin.width < 1 or swin.height < 1:
                            break
                        sa = sds.read(1, window=swin)
                        st = sds.window_transform(swin)
                        sc = ((xs - st.c) / st.a).astype(int).clip(0, sa.shape[1] - 1)
                        sr = ((ys - st.f) / st.e).astype(int).clip(0, sa.shape[0] - 1)
                        sl_px = sa[sr, sc]
                        flat = (sl_px >= 0) & (sl_px < cfgl["paddy_slope_max_deg"])
                        pct["paddy"][i] = flat.sum() / tot
                        break
            break
    for d in wcs + slopes:
        d.close()

    print("Fire pressure (FIRMS active fire 2001-2025)...")
    pts = []
    for f in sorted((RAW / "firms").glob("modis_*_Madagascar.csv")):
        df = pd.read_csv(f, usecols=["latitude", "longitude"])
        pts.append(df)
    fires = pd.concat(pts, ignore_index=True)
    print(f"  {len(fires)} fire detections")
    # count within radius via 500m grid histogram + disk convolution (fast, adequate)
    SA = CFG["study_area"]
    deg = 500 / 111320
    b = (SA["lon_min"] - 0.1, SA["lat_min"] - 0.1, SA["lon_max"] + 0.1, SA["lat_max"] + 0.1)
    W = int((b[2] - b[0]) / deg); H = int((b[3] - b[1]) / deg)
    cols = ((fires.longitude - b[0]) / deg).astype(int)
    rows = ((b[3] - fires.latitude) / deg).astype(int)
    ok = (rows >= 0) & (rows < H) & (cols >= 0) & (cols < W)
    grid = np.zeros((H, W))
    np.add.at(grid, (rows[ok], cols[ok]), 1)
    r_px = max(1, int(round(cfgl["fire_radius_m"] / 500)))
    yy, xx = np.ogrid[-r_px:r_px + 1, -r_px:r_px + 1]
    kernel = (xx * xx + yy * yy) <= r_px * r_px
    summed = ndimage.convolve(grid, kernel.astype(float), mode="constant")
    ccols = ((mid.x.values - b[0]) / deg).astype(int).clip(0, W - 1)
    crows = ((b[3] - mid.y.values) / deg).astype(int).clip(0, H - 1)
    yrs = len(list((RAW / "firms").glob("modis_*_Madagascar.csv")))
    fire_dec = summed[crows, ccols] / yrs * 10

    out = pd.DataFrame({
        "pct_crop": np.round(pct["crop"], 2), "pct_grass": np.round(pct["grass"], 2),
        "pct_shrub": np.round(pct["shrub"], 2), "pct_bare": np.round(pct["bare"], 2),
        "pct_wetland": np.round(pct["wetland"], 2), "pct_paddy": np.round(pct["paddy"], 2),
        "fire_dec": np.round(fire_dec, 1),
        "fire_flag": (fire_dec >= cfgl["fire_high_per_decade"]).astype(int),
    }, index=segs.index)
    out.to_parquet(cache)
    return out


# ---------------------------------------------------------------- 5. cyclone
def layer_cyclone():
    cache = DERIVED / "layer_cyclone.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    cfgc = DL["cyclone"]
    print("Cyclone exposure (IBTrACS)...")
    ib = pd.read_csv(RAW / "ibtracs.SI.list.v04r01.csv", skiprows=[1], low_memory=False,
                     usecols=["SID", "SEASON", "LAT", "LON", "WMO_WIND", "USA_WIND"])
    ib["SEASON"] = pd.to_numeric(ib["SEASON"], errors="coerce")
    y0 = 2026 - cfgc["window_years"]
    ib = ib[ib.SEASON >= y0].copy()
    ib["LAT"] = pd.to_numeric(ib.LAT, errors="coerce")
    ib["LON"] = pd.to_numeric(ib.LON, errors="coerce")
    wind = pd.to_numeric(ib.WMO_WIND, errors="coerce").fillna(pd.to_numeric(ib.USA_WIND, errors="coerce"))
    ib["wind"] = wind
    # crop to Madagascar neighbourhood
    ib = ib[(ib.LON > 40) & (ib.LON < 58) & (ib.LAT > -30) & (ib.LAT < -10)].dropna(subset=["LAT", "LON"])
    print(f"  {ib.SID.nunique()} storms near Madagascar since {y0}")

    R = cfgc["radius_km"] * 1000
    counts = np.zeros(len(segs), int)
    maxw = np.zeros(len(segs))
    mid_pts = [Point(x, y) for x, y in zip(mx, my)]
    mtree = STRtree(mid_pts)
    mxy = np.c_[mx, my]
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", UTM, always_xy=True)
    from shapely.geometry import LineString
    for sid, g in ib.groupby("SID"):
        xs, ys = tr.transform(g.LON.values, g.LAT.values)
        track = LineString(zip(xs, ys)) if len(xs) > 1 else Point(xs[0], ys[0])
        zone = track.buffer(R)
        hit = mtree.query(zone)
        if len(hit) == 0:
            continue
        hit = np.array([h for h in hit if zone.covers(mid_pts[h])], dtype=int)
        if len(hit) == 0:
            continue
        counts[hit] += 1
        # max wind among the storm's IN-RADIUS points only (config semantics):
        # a storm peaking cat-5 far offshore does not make a segment cat-5.
        winds = np.where(np.isfinite(g["wind"].values), g["wind"].values, -1.0)
        pxy = np.c_[xs, ys]
        for c0 in range(0, len(hit), 5000):
            chunk = hit[c0:c0 + 5000]
            d2 = ((mxy[chunk, None, :] - pxy[None, :, :]) ** 2).sum(axis=2)
            wmask = np.where(d2 <= R * R, winds[None, :], -1.0).max(axis=1)
            maxw[chunk] = np.maximum(maxw[chunk], wmask)
    cat = np.select([maxw >= 137, maxw >= 113, maxw >= 96, maxw >= 83, maxw >= 64],
                    [5, 4, 3, 2, 1], default=0)
    out = pd.DataFrame({"cyc_n": counts, "cyc_cat": cat.astype(int),
                        "cyc_flag": (counts >= cfgc["high_exposure_min_passages"]).astype(int)},
                       index=segs.index)
    out.to_parquet(cache)
    return out


# ---------------------------------------------------------------- 6. cold limit
def layer_cold():
    cache = DERIVED / "layer_cold.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    cfgt = DL["cold_limit"]
    print("Cold limit: BIO6 (CHELSA V2.1, pre-cropped to study area)...")
    with rasterio.open(RAW / "bio6_cold.tif") as src:
        acc = src.read(1).astype("float64")
        acc = np.where(acc == src.nodata, np.nan, acc)
        t = src.transform
        lon0, lat0, dlon, dlat = t.c, t.f, t.a, t.e
    cols = ((mid.x.values - lon0) / dlon).astype(int).clip(0, acc.shape[1] - 1)
    rows = ((mid.y.values - lat0) / dlat).astype(int).clip(0, acc.shape[0] - 1)
    bio6 = acc[rows, cols]
    # fill the few coastal NaNs with nearest valid via small search
    nanmask = ~np.isfinite(bio6)
    if nanmask.any():
        valid = np.argwhere(np.isfinite(acc))
        from scipy.spatial import cKDTree
        kd = cKDTree(valid)
        _, near = kd.query(np.c_[rows[nanmask], cols[nanmask]])
        bio6[nanmask] = acc[valid[near][:, 0], valid[near][:, 1]]
    out = pd.DataFrame({"bio6": np.round(bio6, 1),
                        "cold_flag": (bio6 < cfgt["bio6_max_c"]).astype(int)}, index=segs.index)
    out.to_parquet(cache)
    return out


# ---------------------------------------------------------------- assemble
layers = [layer_population(), layer_protected(), layer_access(), layer_landuse_fire(), layer_cyclone(), layer_cold()]
enriched = pd.concat([segs] + layers, axis=1)
enriched = gpd.GeoDataFrame(enriched, crs="EPSG:4326")

# Cyclone flag from config (recomputed here so cached cn/cc survive rule changes)
cfgc = DL["cyclone"]
freq = enriched["cyc_n"] >= cfgc["high_exposure_min_passages"]
sev = enriched["cyc_cat"] >= cfgc["high_exposure_min_category"]
enriched["cyc_flag"] = ((freq | sev) if cfgc["high_exposure_rule"] == "or" else (freq & sev)).astype(int)
ne = enriched[enriched.cls != "excluded"]
cov = ne.cyc_flag.mean()
print(f"cyclone flag ({cfgc['high_exposure_rule']}): {ne.cyc_flag.sum()}/{len(ne)} non-excluded segments = {cov*100:.0f}% "
      f"(freq-only {freq[ne.index].mean()*100:.0f}%, severity-only {sev[ne.index].mean()*100:.0f}%)")
if cfgc["high_exposure_rule"] == "or" and cov >= 0.45:
    print("NOTE: OR-rule coverage approaches half the coast — per agreement, consider 'and' and re-report before freezing.")
assert (enriched["score"] == segs["score"]).all() and (enriched["cls"] == segs["cls"]).all(), \
    "decision layers must not alter suitability!"
enriched.to_file(DERIVED / "segments_enriched.gpkg", driver="GPKG")
print("Wrote segments_enriched.gpkg")

# ---------------------------------------------------------------- export GeoJSON
print("Exporting app GeoJSON with decision attributes...")
tol = CFG["output"]["simplify_tolerance_deg"]
prec = CFG["output"]["coordinate_precision"]
exp = enriched.copy()
exp["geometry"] = exp.geometry.simplify(tol)
tb_score = CFG["scoring"]["trial_benchmark"]["score"]
tb_km = enriched.loc[enriched.tb == 1, "length_m"].sum() / 1000

features = []
for r in exp.itertuples():
    coords = [[round(x, prec), round(y, prec)] for x, y in r.geometry.coords]
    features.append({
        "type": "Feature",
        "properties": {
            "id": r.seg_id, "s": r.score, "c": r.cls[0], "x": r.excl,
            "L": int(r.length_m), "cy": r.clay, "sd": r.sand, "sl": r.slope,
            "el": r.elev, "rn": r.rain, "lf": r.lc_frac, "ld": r.lc_dom,
            "mb": r.mix_b, "mv": r.mix_v, "ma": r.mix_a, "o": r.ord, "rv": r.riv, "tb": r.tb,
            "p2": int(r.pop2k), "p5": int(r.pop5k),
            "pk": r.pa_km, "pn": (r.pa_name or "")[:40], "fk": r.forest_km,
            "ac": {"road": "r", "boat": "b", "remote": "x"}[r.access], "ak": r.access_km,
            "uc": r.pct_crop, "ug": r.pct_grass, "us": r.pct_shrub, "ub": r.pct_bare,
            "uw": r.pct_wetland, "up": r.pct_paddy,
            "fd": r.fire_dec, "ff": int(r.fire_flag),
            "cn": int(r.cyc_n), "cc": int(r.cyc_cat), "cf": int(r.cyc_flag),
            "b6": r.bio6, "cm": int(r.cold_flag),
        },
        "geometry": {"type": "LineString", "coordinates": coords},
    })

gj = {"type": "FeatureCollection",
      "_schema": {"id": "segment id", "s": "suitability 0-100 (absolute)", "c": "class h/m/l/e (relative percentile)",
                  "x": "exclusion reason", "L": "bank length m", "cy": "clay % 0-30cm", "sd": "sand %",
                  "sl": "mean slope deg", "el": "mean elev m", "rn": "annual rain mm",
                  "lf": "plantable fraction", "ld": "dominant WorldCover class",
                  "mb": "% balcooa", "mv": "% vulgaris", "ma": "% asper", "o": "Strahler order",
                  "rv": "HydroRIVERS id", "tb": "1 = meets Efaho trial benchmark",
                  "p2": "population within 2 km", "p5": "population within 5 km",
                  "pk": "km to nearest protected area", "pn": "nearest PA name", "fk": "km to forest block >=100ha",
                  "ac": "access class r/b/x (road/boat/remote)", "ak": "km to road access (network for boat, straight-line for remote)",
                  "uc": "cropland frac", "ug": "grassland frac", "us": "shrub frac", "ub": "bare frac",
                  "uw": "wetland frac", "up": "likely-paddy frac (heuristic)",
                  "fd": "MODIS fire detections per decade within 1 km", "ff": "1 = high fire pressure",
                  "cn": "cyclone passages within 100 km, 40 yr", "cc": "max Saffir-Simpson category", "cf": "1 = high cyclone exposure",
                  "b6": "BIO6 min temp of coldest month degC", "cm": "1 = cold-marginal caution (BIO6 < 10C)"},
      "_benchmark": {"score": tb_score, "total_km": round(tb_km),
                     "note": "median suitability score of the Efaho reach where SaniTap 2026 field trials succeeded"},
      "features": features}

out_path = ROOT / CFG["output"]["geojson_path"]
out_path.write_text(json.dumps(gj, separators=(",", ":")))
mb = out_path.stat().st_size / 1e6
print(f"Wrote {out_path} ({mb:.1f} MB)")
if mb > CFG["output"]["max_geojson_mb"]:
    print(f"WARNING: exceeds {CFG['output']['max_geojson_mb']} MB target — consider PMTiles")

# quick distribution report for the check-in
print("\n--- attribute distributions ---")
for col in ["pop2k", "pop5k", "pa_km", "forest_km", "access_km", "riv_grad",
            "pct_crop", "pct_paddy", "fire_dec", "cyc_n", "cyc_cat"]:
    v = pd.to_numeric(enriched[col], errors="coerce")
    print(f"{col:>10}: p5 {v.quantile(.05):8.1f}  p50 {v.quantile(.5):8.1f}  p95 {v.quantile(.95):8.1f}  max {v.max():8.1f}")
print("access:", enriched["access"].value_counts().to_dict())
print("cyc by lat band (sanity: north should exceed south):")
for lo, hi in [(-15, -12.5), (-18, -15), (-21, -18), (-24, -21), (-25.2, -24)]:
    m = (mid.y >= lo) & (mid.y < hi)
    print(f"  lat {lo}..{hi}: mean passages {enriched.loc[m,'cyc_n'].mean():.1f}")
hi_cls = enriched[enriched.cls == "high"]
print(f"\ncold-limit check on 'high' class ({len(hi_cls)} segs):")
print(f"  elevation: p50 {hi_cls.elev.median():.0f} m, p90 {hi_cls.elev.quantile(.9):.0f} m")
print(f"  BIO6: p10 {hi_cls.bio6.quantile(.1):.1f}C, p50 {hi_cls.bio6.median():.1f}C")
print(f"  cold-marginal (BIO6 < {DL['cold_limit']['bio6_max_c']}C): {hi_cls.cold_flag.sum()} segs "
      f"= {hi_cls.cold_flag.mean()*100:.0f}% of high class, "
      f"{enriched.cold_flag.sum()} segs overall ({enriched.cold_flag.mean()*100:.0f}%)")
