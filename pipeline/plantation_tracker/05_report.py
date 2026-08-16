#!/usr/bin/env python3
"""Plantation tracker — Step 2d: assemble the self-contained HTML report."""
import base64
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "output" / "cache"
FIGS = HERE / "output" / "figs"

stats = json.load(open(CACHE / "stats.json"))
bio = json.load(open(CACHE / "biomass.json"))
gedi = json.load(open(CACHE / "gedi_note.json"))
sep = json.load(open(CACHE / "separability.json"))


def img(name):
    b = base64.b64encode((FIGS / name).read_bytes()).decode()
    return f'<img src="data:image/png;base64,{b}" alt="{name}">'


g = bio["biomass"]["good"]; p = bio["biomass"]["poor"]; w = bio["biomass"]["whole"]

# Canonical areas (per AM 2026-08-16; informal "26 ha" figure retired):
LEASED_HA = 20.97          # géomètre survey Oct 2018, FLM lease
RENT_HA_YR = 200.0         # USD/ha/yr
RENT_YR = LEASED_HA * RENT_HA_YR
UNPLANTED_HA = round(LEASED_HA - bio["areas_ha"]["whole"], 2)

def rent_per_t(stratum_key, area_ha):
    lo, hi = bio["biomass"][stratum_key]["t_dry_total"]
    rent = area_ha * RENT_HA_YR
    return rent, (round(rent / hi, 1), round(rent / lo, 1))

rent_good, rpt_good = rent_per_t("good", bio["areas_ha"]["good"])
rent_poor, rpt_poor = rent_per_t("poor", bio["areas_ha"]["poor"])
rpt_whole = (round(RENT_YR / w["t_dry_total"][1], 1), round(RENT_YR / w["t_dry_total"][0], 1))
model = bio["model_reference"]
model_whole = [round(model["clump_kg_yr15"] * d / 1000 * bio["areas_ha"]["whole"]) for d in (400, 500)]
sep_recent = [r for r in sep if 2016 <= r["year"] <= 2025]
d_range = f"{min(r['cohens_d'] for r in sep_recent):.1f}–{max(r['cohens_d'] for r in sep_recent):.1f}"

html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cascade Plantation Growth History</title>
<style>
 body {{ margin:0; background:#f7f9f5; color:#1e2b22; font:15.5px/1.6 system-ui,sans-serif; padding:36px 18px 80px; }}
 main {{ max-width:880px; margin:0 auto; }}
 h1 {{ font:700 30px/1.2 Georgia,serif; margin:0 0 4px; }}
 h2 {{ font:700 20px/1.3 Georgia,serif; margin:38px 0 10px; }}
 .eyebrow {{ font:600 11.5px/1 ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; color:#157a43; margin-bottom:8px; }}
 .lede {{ color:#52635a; max-width:64ch; }}
 img {{ max-width:100%; border:1px solid #dde4dc; border-radius:8px; margin:10px 0; background:#fff; }}
 table {{ width:100%; border-collapse:collapse; font-size:14px; margin:10px 0; }}
 th {{ text-align:left; font:600 11.5px/1.3 ui-monospace,monospace; text-transform:uppercase; color:#7d8c83; padding:7px 9px; border-bottom:2px solid #dde4dc; }}
 td {{ padding:6px 9px; border-bottom:1px solid #e5eae4; font-variant-numeric:tabular-nums; vertical-align:top; }}
 .warn {{ background:#fff6e8; border:1px solid #eeddbb; border-left:4px solid #a3670f; border-radius:8px; padding:12px 16px; margin:14px 0; font-size:14px; }}
 .find {{ background:#eaf5ee; border:1px solid #cfe5d6; border-left:4px solid #157a43; border-radius:8px; padding:12px 16px; margin:14px 0; font-size:14.5px; }}
 .muted {{ color:#7d8c83; font-size:12.5px; }}
 code {{ font:.9em ui-monospace,monospace; background:#eef3ec; padding:1px 5px; border-radius:4px; }}
</style></head><body><main>
<p class="eyebrow">SaniTap · plantation tracker · desk analysis · Aug 2026</p>
<h1>Cascade Plantation Growth History</h1>
<p class="lede">Domaine de la Cascade, Anosy (−24.994, 46.912). 176 usable satellite scenes
2009–2026 (65 Landsat, 111 Sentinel-2) plus 134 Sentinel-1 radar passes, analysed per performance
stratum. Three area figures are used consistently throughout: <b>leased parcel 20.97 ha</b>
(legal — géomètre survey Oct 2018, FLM lease at $200/ha/yr), <b>mapped bamboo canopy 18.13 ha</b>
(the corrected polygon = analysis AOI), <b>good zone 0.56 ha</b>. The informal "26 ha" figure is
retired.</p>

<div class="find"><b>The three questions, answered:</b><br>
<b>1. Can satellites reconstruct the growth history?</b> Yes — a continuous 17-year record
(fig 1) shows establishment-era NDVI (~0.60) rising through 2014–2017 to a stable canopy
plateau, with phased greening visible in the chronology map (fig 6). The record is censored at
2009: 12.3 ha was already vegetated then (earlier planting or prior cover).<br>
<b>2. Can they distinguish good from poor?</b> Yes, decisively — the strata separate from 2012
(Cohen's d 0.83) and in every Sentinel-2 year 2016–2025 (d {d_range}, p &lt; 10⁻¹⁶).
The discrimination is <b>seasonal</b>: the gap triples in Nov–Jan (fig 2b) when the poor stratum
browns on exhausted soil moisture while the streamside good zone stays green — direct field
validation of the suitability tool's wetter-is-better premise, and the MRV recipe: monitor in
<b>Nov–Dec</b>.<br>
<b>3. Is the xlsx curve consistent with this plantation?</b> No — and that is the point. The
poor stratum stands at an estimated <b>{p['pct_of_model_yr15'][0]}–{p['pct_of_model_yr15'][1]}%
of the modelled year-15 biomass</b>; the model curve describes a good site (like the good zone,
{g['pct_of_model_yr15'][0]}–{g['pct_of_model_yr15'][1]}% of model), not an average one. Site
selection is not a refinement of the project — it is the difference between the model being
right and wrong.</div>

<div class="warn"><b>Area reconciliation.</b> Mapped bamboo canopy (18.13 ha) sits
<b>{UNPLANTED_HA} ha short of the leased parcel (20.97 ha)</b> — most plausibly unplanted or
failed ground inside the lease (tracks, edges, wet patches), which still carries
<b>${UNPLANTED_HA * RENT_HA_YR:,.0f}/yr of rent with zero biomass</b>. Fig 7 shows spectrally
similar blocks outside the mapped boundary (orange); NDVI similarity alone cannot tell
plantation from natural woodland, and most of it clearly is natural vegetation — but the
blocks hugging the boundary (NE corner ~1.5–2.4 ha, southern lobes ~0.5–1 ha) are worth an
eye against high-res imagery, not least to check where the surveyed parcel lines actually run.
If any are planted ground, re-run the editor and I'll update every figure.</div>

<h2>1 · NDVI history by stratum</h2>{img("fig1_ndvi_series.png")}
<p>Both strata start near 0.60–0.65 under Landsat (30 m), rise through 2014–2017, and plateau
at ~0.84 (good) vs ~0.78 (poor). Two anomalies are honest features of the record: 2015 is the
Landsat→Sentinel-2 hand-off (few scenes, mixed sensors), and <b>2026 shows the good zone
dropping to 0.72, below the poor stratum</b> — either recent cutting in the good zone or an
artifact of the thin partial-year composite. <b>Do you know of 2026 harvesting there?</b></p>

<h2>2 · Seasonal behaviour — the discrimination mechanism</h2>
{img("fig2b_monthly_gap.png")}
<p>The classic wet-minus-dry amplitude metric (fig 2, in the repo) is muddied by cyclone-season
cloud sampling; the month-by-month gap climatology above is the clean view. Sep–Jan is when
desk monitoring can grade site quality; Mar–Jul it cannot (gap 0.02–0.04, within noise of a
single scene).</p>

<h2>3 · Radar structure check</h2>{img("fig3_s1.png")}
<p>Sentinel-1 VH backscatter runs ~{stats.get('s1_vh_gap_recent_db', 0.6)} dB higher over the
good zone in recent years — consistent with denser woody structure — but C-band saturates at
roughly 50–100 t/ha of biomass, so radar corroborates rather than quantifies here.</p>

<h2>4 · The plantation developing, year by year</h2>{img("fig4_smallmultiples.png")}
{img("fig6_chronology.png")}
<p>Recovered chronology: 12.3 ha already green at the 2009 series start (censored — planted
earlier or pre-existing cover), then phased greening of ~1.5 ha (2011), ~0.8 ha (2012),
~1.9 ha (2013), ~1.5 ha (2015). The good zone's own signal is instructive: in 2011 it read
<i>lower</i> than the poor stratum (d = −0.67) — freshly planted into cleared ground — and
overtook everything by 2012. Treat years as ±1: Landsat pixels are 30 m and the onset
threshold is a desk heuristic.</p>

<h2>5 · Pixel-level mixture (your contamination caveat, confirmed)</h2>
{img("fig5_distributions.png")}
<p>{int(stats['recent_pixel_overlap']*100)}% of poor-stratum pixels exceed the good zone's
median in the 2024–26 composite — the right tail you predicted from unmapped scattered good
clumps, plus optical saturation once even a thin-culm canopy closes. Stratum medians remain
strongly separable; single pixels are not a reliable per-clump verdict.</p>

<h2>6 · Standing biomass — ranges, not points</h2>
<table>
<tr><th>Stratum</th><th>Area</th><th>t dry/ha</th><th>t dry total</th><th>t wet total (46% moisture)</th><th>% of modelled yr-15</th></tr>
<tr><td>Good zone</td><td>{bio['areas_ha']['good']} ha</td><td>{g['t_dry_per_ha'][0]}–{g['t_dry_per_ha'][1]}</td>
<td>{g['t_dry_total'][0]}–{g['t_dry_total'][1]}</td><td>{g['t_wet_total'][0]}–{g['t_wet_total'][1]}</td>
<td>{g['pct_of_model_yr15'][0]}–{g['pct_of_model_yr15'][1]}%</td></tr>
<tr><td>Poor stratum</td><td>{bio['areas_ha']['poor']} ha</td><td>{p['t_dry_per_ha'][0]}–{p['t_dry_per_ha'][1]}</td>
<td>{p['t_dry_total'][0]}–{p['t_dry_total'][1]}</td><td>{p['t_wet_total'][0]}–{p['t_wet_total'][1]}</td>
<td>{p['pct_of_model_yr15'][0]}–{p['pct_of_model_yr15'][1]}%</td></tr>
<tr><td><b>Whole site</b></td><td>{bio['areas_ha']['whole']} ha</td><td>—</td>
<td><b>{w['t_dry_total'][0]}–{w['t_dry_total'][1]}</b></td><td><b>{w['t_wet_total'][0]}–{w['t_wet_total'][1]}</b></td>
<td>vs <b>{model_whole[0]:,}–{model_whole[1]:,} t</b> if fully as modelled</td></tr>
</table>
{img("fig8_biomass.png")}
<p><b>Rent economics</b> (for the cost-coverage discussion with Lucas — lease
${RENT_HA_YR:.0f}/ha/yr on the surveyed 20.97 ha = <b>${RENT_YR:,.0f}/yr</b>):</p>
<table>
<tr><th>Stratum</th><th>Rent share/yr</th><th>Standing dry biomass</th><th>Rent per t dry standing</th></tr>
<tr><td>Good zone (0.56 ha)</td><td>${rent_good:,.0f}</td><td>{g['t_dry_total'][0]}–{g['t_dry_total'][1]} t</td>
<td><b>${rpt_good[0]}–{rpt_good[1]}/t/yr</b></td></tr>
<tr><td>Poor stratum (17.57 ha)</td><td>${rent_poor:,.0f}</td><td>{p['t_dry_total'][0]}–{p['t_dry_total'][1]} t</td>
<td><b>${rpt_poor[0]}–{rpt_poor[1]}/t/yr</b></td></tr>
<tr><td>Unplanted lease remainder ({UNPLANTED_HA} ha)</td><td>${UNPLANTED_HA * RENT_HA_YR:,.0f}</td><td>≈ 0</td><td>—</td></tr>
<tr><td><b>Whole lease (20.97 ha)</b></td><td><b>${RENT_YR:,.0f}</b></td><td>{w['t_dry_total'][0]}–{w['t_dry_total'][1]} t</td>
<td><b>${rpt_whole[0]}–{rpt_whole[1]}/t/yr</b></td></tr>
</table>
<p class="muted">Reading: the good zone's rent burden per standing tonne is negligible; the poor
stratum's is one to two orders of magnitude higher. These are STANDING-stock figures, not annual
increment — sustainable-harvest economics need the growth rate, which the field plots would pin
down. Wet-tonne figures: divide by {1/(1-0.46):.2f}.</p>
<p><b>How these numbers are built</b> (satellite indices cannot weigh stems — anchors are
structural): (a) culm-level allometry — bamboo culm mass scales roughly with diameter² (per
village-bamboo allometries for <i>B. balcooa</i> and congeners, Nath et&nbsp;al. 2009,
<i>Biomass &amp; Bioenergy</i> 33:1188–96; Kaushal et&nbsp;al. bamboo biomass studies), so a
1–4 cm "fishing rod" carries ~{bio['assumption_ranges']['poor_culm_kg_dry'][0]}–{bio['assumption_ranges']['poor_culm_kg_dry'][1]} kg
dry against {bio['assumption_ranges']['good_culm_kg_dry'][0]}–{bio['assumption_ranges']['good_culm_kg_dry'][1]} kg for a proper culm
(xlsx: 7 kg avg, 10.3 kg Tana sample); (b) the xlsx per-clump curve
({model['clump_kg_yr15']:.0f} kg dry at year 15) as the "performing as modelled" ceiling.
Clump density assumed {bio['assumption_ranges']['clumps_per_ha'][0]}–{bio['assumption_ranges']['clumps_per_ha'][1]}/ha.
<b>Uncertainty is ±40–50%</b> and skewed: NDVI saturation makes thin-culm stands look better
than they are; Sentinel-1 keeps the structural floor honest.</p>

<div class="find"><b>One-day field calibration protocol (MadAvance / Cascade)</b> — collapses
the ranges to ~±15–20%:<br>
· 16 plots of 10 × 10 m: 12 in the poor stratum (4 per NDVI tercile — I can supply the
stratified plot coordinates), 3 in the good zone, 1 straddling the boundary.<br>
· Per plot: count clumps; on 3 clumps per plot count all culms and tape/calliper the DBH of 5
culms; measure 2 culm heights (pole or clinometer).<br>
· If cutting is permitted: weigh 2–3 freshly cut culms across diameter classes (spring scale)
for a local wet-mass check (moisture 0.46 assumed otherwise).<br>
· Phone-GPS each plot corner; photos along one diagonal.<br>
That is ~20 min per plot for a crew of three — one field day.</div>

<h2>7 · Verdicts</h2>
<p><b>DIY satellite MRV: credible, with stated limits.</b> Free imagery reliably tracks canopy
establishment, detects phase timing, grades site quality seasonally (Nov–Dec window), and
flags disturbance (the 2026 good-zone dip). It cannot measure culm diameter — the fishing-rod
problem is structural, and satellites see leaves, not stems: without the field anchor, optical
data would have called this plantation a success.</p>
<p><b>xlsx curve:</b> keep it as the good-site trajectory it demonstrably is (the good zone
plausibly sits at {g['pct_of_model_yr15'][0]}–{g['pct_of_model_yr15'][1]}% of it), but a
site-quality multiplier belongs in any whole-portfolio projection: this 15-year site holds
roughly <b>{round(w['t_dry_total'][0]/model_whole[1]*100)}–{round(w['t_dry_total'][1]/model_whole[0]*100)}%</b>
of its fully-modelled standing stock. RiverGuard's benchmark-anchored site selection exists
precisely to keep new plantings out of this graph's red band.</p>
<p><b>Next instrument:</b> if structure monitoring matters, a drone photogrammetry flight is
the right buy — canopy height + gap fraction at clump scale for the whole 18 ha in one
afternoon (the planting rows already resolve in Bing's z18 basemap). It still will not weigh
culms; pair it with the one-day plot protocol above and both questions close.</p>

<h2>Caveats & sources</h2>
<p class="muted">Strata as corrected by AM (16 Aug 2026); poor stratum contains unmapped good
clumps (right-tail contamination, fig 5) and its quality varies bad-to-worse. Good zone is
0.56 ha as drawn (~2–3 ha was described — extend in the editor if under-drawn, stats will
sharpen). Boundary precision beyond ~10 m is immaterial to these results (analysis pixels are
10 m S2 / 30 m Landsat; good-zone Landsat medians rest on ~6 pixels, hence pre-2016 noise).
GEDI: {gedi.get('granules_intersecting_bbox', 'n/a')} L2A granules intersect the area but
footprint data needs NASA Earthdata auth — {'skipped (time-boxed)' if 'error' not in gedi else 'query failed'}.
Chronology censored at 2009. 2026 composites are partial-year.<br>
Sources: Sentinel-2 L2A (ESA/Copernicus, Element84 Earth Search STAC), Landsat 5/7/8 C2 L2
(USGS via Microsoft Planetary Computer), Sentinel-1 RTC (Planetary Computer), NASA CMR.
Pipeline: <code>pipeline/plantation_tracker/</code>, caches under <code>output/cache/</code>,
re-run <code>02→03→04→05</code>.</p>
</main></body></html>"""

out = HERE / "output" / "report.html"
out.write_text(html)
print(f"Wrote {out} ({out.stat().st_size/1e6:.1f} MB)")
