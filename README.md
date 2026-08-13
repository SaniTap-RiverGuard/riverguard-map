# Project RiverGuard — East Coast Riparian Bamboo Site Selection & Planning Tool

Desk-based site-selection and carbon/finance planning tool for SaniTap's VM0047
(Verra VCS-CCB) clumping-bamboo riparian restoration project on the east coast of
Madagascar (Sambava/Antalaha south to Tolagnaro).

**Live app:** https://sanitap-riverguard.github.io/riverguard-map/

Two parts:

1. **`pipeline/`** — a reproducible Python pipeline that scores ~500 m river
   segments along all east/south-draining rivers for bamboo planting suitability
   and emits `docs/data/segments.geojson`.
2. **`docs/`** — a fully static Leaflet web app (GitHub Pages) for selecting
   segments, configuring the planting/carbon/finance model, and visualising
   20-year impact. Model defaults come from the VM0047 calculator xlsx.

> **Disclaimer:** suitability scores are desk-based estimates from global
> datasets and REQUIRE field verification before operational planting decisions.

## Data sources & licences

| Layer | Source | Resolution | Licence |
|---|---|---|---|
| River network | [HydroSHEDS HydroRIVERS v1.0](https://www.hydrosheds.org/products/hydrorivers) (Africa) | vector | HydroSHEDS licence v1 (free, attribution) |
| Soil clay/sand 0–30 cm | [ISRIC SoilGrids 2.0](https://soilgrids.org) | 250 m | CC-BY 4.0 |
| Elevation / slope | [Copernicus GLO-30 DEM](https://registry.opendata.aws/copernicus-dem/) (AWS Open Data) | 30 m | free with attribution (ESA/Airbus) |
| Land cover | [ESA WorldCover 2021 v200](https://esa-worldcover.org) (AWS) | 10 m | CC-BY 4.0 |
| Annual rainfall | [WorldClim 2.1](https://worldclim.org) monthly precipitation, summed | 2.5′ (~4.6 km) | CC-BY-SA 4.0 |
| Basemap (app) | Esri World Imagery / OpenStreetMap | tiles | Esri & OSM terms |

Raw downloads (~3 GB) land in `data/raw/` and are **gitignored** — never commit them.

## Pipeline

### Method summary

1. **Network** (`01_rivers.py`): HydroRIVERS Africa, bbox lon 46.3–51°E /
   lat 12.5–25.2°S. Basins are kept when their river **mouth** (terminal reach)
   falls east of a piecewise coastal separator line (config), i.e. east/south-
   draining to the Indian Ocean. Headwater reaches with upstream catchment
   < 25 km² are dropped. Reaches are split into ~500 m segments (UTM 38S).
2. **Rasters** (`02_rasters.py`): downloads/prepares all layers (idempotent).
   Slope is computed from GLO-30 in-script; SoilGrids is fetched by windowed
   remote reads (no continental download); clay/sand are depth-weighted
   (5/10/15 cm) over 0–30 cm.
3. **Scoring** (`03_score.py`): each segment's 100 m buffer (each side,
   configurable) is sampled. Sub-scores 0–100:
   - **Texture (weight 0.40)** — clay % ramps 0→100 over 10–30 %; sand > 60 %
     penalised up to −50 pts at 85 %. **Known limitation:** SoilGrids 250 m maps
     the *regional soil profile* and cannot distinguish sandy braided channel
     beds from surrounding soils — it actually reports higher clay at the failed
     Mandrare trial area than at the thriving Efaho site. Texture is therefore
     useful for within-east-coast variation only, and **field soil verification
     is mandatory before any planting commitment**.
   - **Land cover (0.30)** — plantable fraction of buffer (WorldCover shrub/
     grass/crop/bare). Tree cover, built-up, water, wetland, mangrove are
     unplantable (VM0047 additionality: only degraded/cleared land is restored).
   - **Slope (0.15)** — optimal 1–12°; > 30° excluded (cliff); flat scored 70.
   - **Rainfall (0.15)** — ramps 0→100 over 700–1400 mm/yr.
   - **Semi-arid policy (from field trials):** annual rainfall < 700 mm ⇒
     segment **excluded** ("semi-arid braided system — failed field trials").
     In the 700–1000 mm transitional band a graded penalty of up to −30 pts
     applies. Climate is the discriminating signal for the sandy-braided washout
     failure mode precisely because SoilGrids cannot see it (above).
   - Composite minus 15 pts if slope < 0.5° **and** elevation < 10 m (deep-
     inundation floodplain risk). Segment also **excluded** if slope > 30° or
     plantable fraction < 20 %.
   - **Classes are relative, scores are absolute:** high/medium/low are the
     top 25 % / middle 50 % / bottom 25 % of the non-excluded population — a
     prioritisation aid, recomputed on every pipeline run. The underlying 0–100
     score is absolute and carried in every popup and export.
   - **Trial benchmark (absolute anchor):** segments scoring ≥ 79 — the median
     score of the Efaho reach where the 2026 field trials succeeded — carry a
     `tb=1` flag ("meets proven trial benchmark") in popups, exports, and a map
     filter. This preserves the field-truth anchor alongside the relative
     classes: the proven site sits near the ~30th percentile of the coastline,
     i.e. thousands of km of bank score *better* than a site where bamboo
     already demonstrably thrives.
4. **Species recommendation** (provisional, desk-based, config `rules`):
   D. asper share raised on wettest clay-rich sites (≥ 2200 mm), B. balcooa the
   mid-bank workhorse, B. vulgaris share raised on drier/lower-grade sites.
5. **Export**: simplified polylines (tolerance 0.0002°, 5-decimal coords) with
   compact per-segment attributes → `docs/data/segments.geojson` (schema is
   embedded in the file under `_schema`). `04_preview.py` renders a static PNG
   review map with Efaho/Mandrare sanity insets.

Ground-truth anchors: the Efaho river (Anosy, thriving 3–4 month trial) must
score HIGH; the Mandrare at Amboasary (sandy, semi-arid) must score LOW.

### Re-running

```bash
python3 -m venv .venv
.venv/bin/pip install openpyxl geopandas rasterio shapely pyproj fiona requests matplotlib numpy pandas rasterstats
.venv/bin/python pipeline/extract_defaults.py   # xlsx -> docs/data/model_config.json
.venv/bin/python pipeline/01_rivers.py          # needs data/raw/HydroRIVERS zip (auto-hint below)
.venv/bin/python pipeline/02_rasters.py         # downloads all rasters (idempotent, ~3 GB)
.venv/bin/python pipeline/03_score.py           # sampling + scoring + GeoJSON export
.venv/bin/python pipeline/04_preview.py         # static PNG review map
```

All weights/thresholds live in `pipeline/config.json`; edit and re-run steps
3–4 (fast — no re-download needed).

### Updating defaults from a new xlsx version

Drop the new calculator in `data/`, then:

```bash
.venv/bin/python pipeline/extract_defaults.py "data/<new file>.xlsx"
```

The script asserts the sheet layout (Assumptions / Seedling Density / Biomass /
Eq30) and cross-checks the year-20 headline numbers; it fails loudly if the
layout moved. Commit the regenerated `docs/data/model_config.json`.

## Web app

Static, no build step — plain ES modules in `docs/`. Local preview:

```bash
python3 -m http.server -d docs 8000
```

## GitHub Pages deployment

```bash
gh repo create SaniTap-RiverGuard/riverguard-map --public --source . --push
gh api -X POST repos/SaniTap-RiverGuard/riverguard-map/pages \
  -f "source[branch]=main" -f "source[path]=/docs"
```

Site: https://sanitap-riverguard.github.io/riverguard-map/ (allow a few minutes
for the first deploy). Keep every file < 100 MB (GitHub hard limit); the
shipped GeoJSON is validated < 30 MB by the pipeline.
