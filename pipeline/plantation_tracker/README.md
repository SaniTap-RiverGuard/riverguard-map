# Plantation growth tracker — Domaine de la Cascade (Anosy)

Historical growth analysis of SaniTap's existing Bambusa balcooa plantation
(~-24.9940, 46.9120, leased parcel 20.97 ha, phased establishment from ~2010). Separate from
the public suitability map.

Three questions:
1. Can open satellite data reconstruct the plantation's growth history 2010–2026?
2. Can it distinguish the well-performing (~2–3 ha, streamside) zones from the
   poor thin-culm ("fishing rod") majority? If yes → free MRV capability and a
   site-quality signal validating the suitability tool's wetter-is-better premise.
3. Is the xlsx balcooa biomass curve consistent with what a mostly-poor 15-year
   plantation actually shows?

## Step 1 — AOI capture (current)

`01_aoi_capture.py` auto-delineates the plantation from the most recent
cloud-free Sentinel-2 L2A scene (Element84 Earth Search STAC, windowed COG
reads) by seeded region-growing on NDVI + red-edge (NDRE) + NDVI texture,
then emits `output/aoi_editor.html` — an interactive page for correcting the
boundary and marking good/poor performance zones. Export produces
`plantation_zones_corrected.geojson` (roles: plantation | good | poor).

Auto-result: 16.4 ha candidate vs 20.97 ha leased (18.13 ha mapped after correction) — the sparse-canopy poor
zones likely fall outside the spectral component; correct by hand.

**Waiting on the corrected GeoJSON before Step 2** (time series: S2 2015–2026,
Landsat 2009–2015, optional S1 backscatter, seasonal-amplitude analysis, GEDI
check, planting-chronology recovery, and the written interpretation).

Sources: Sentinel-2 L2A COGs (ESA/Copernicus via AWS Open Data, Element84
Earth Search STAC). Raw downloads stay in gitignored data/raw/.
