#!/usr/bin/env python3
"""Distribution report for the decision-support layers: histogram grid PNG +
stats for the check-in. Run after 05_decision_layers.py."""
import json
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DERIVED = ROOT / "data" / "derived"
e = gpd.read_file(DERIVED / "segments_enriched.gpkg")
ok = e[e.cls != "excluded"]

PANELS = [
    ("pop5k", "Population within 5 km", dict(bins=np.logspace(0, 5.5, 40)), "log"),
    ("pa_km", "km to nearest protected area", dict(bins=40, range=(0, 120)), None),
    ("forest_km", "km to forest block ≥100 ha", dict(bins=40, range=(0, 30)), None),
    ("access_km", "km to road access", dict(bins=40, range=(0, 60)), None),
    ("riv_grad", "river-line gradient %", dict(bins=40, range=(0, 6)), None),
    ("pct_crop", "cropland fraction of buffer", dict(bins=30, range=(0, 1)), None),
    ("pct_paddy", "likely-paddy fraction", dict(bins=30, range=(0, 0.8)), None),
    ("fire_dec", "fires/decade within 1 km", dict(bins=40, range=(0, 40)), None),
    ("cyc_n", "cyclone passages (100 km, 40 yr)", dict(bins=np.arange(-0.5, 28.5)), None),
    ("elev", "elevation m", dict(bins=40, range=(0, 1600)), None),
    ("bio6", "BIO6 coldest-month min °C", dict(bins=40, range=(4, 22)), None),
]

fig, axes = plt.subplots(4, 3, figsize=(15, 14), facecolor="white")
axes = axes.ravel()
for ax, (col, title, kw, xscale) in zip(axes, PANELS):
    v = ok[col].dropna()
    ax.hist(v, color="#1a9850", alpha=0.85, **kw)
    if xscale:
        ax.set_xscale(xscale)
    ax.set_title(title, fontsize=10)
    ax.tick_params(labelsize=8)
    med = v.median()
    ax.axvline(med, color="#a3670f", lw=1.4, ls="--")
    ax.text(0.97, 0.92, f"med {med:,.1f}", transform=ax.transAxes, ha="right", fontsize=8, color="#a3670f")

# access class + flags summary panel
ax = axes[len(PANELS)]
counts = {
    "road": (ok.access == "road").sum(), "boat": (ok.access == "boat").sum(),
    "remote": (ok.access == "remote").sum(),
    "≥benchmark": (ok.tb == 1).sum(), "high fire": (ok.fire_flag == 1).sum(),
    "high cyclone": (ok.cyc_flag == 1).sum(), "cold-marginal": (ok.cold_flag == 1).sum(),
}
ax.barh(list(counts.keys())[::-1], list(counts.values())[::-1], color="#1a9850", alpha=0.85)
ax.set_title("segment counts (non-excluded)", fontsize=10)
ax.tick_params(labelsize=8)

fig.suptitle("RiverGuard decision-support layers — distributions (non-excluded segments)", fontsize=13)
fig.tight_layout(rect=[0, 0, 1, 0.97])
out = DERIVED / "layer_distributions.png"
fig.savefig(out, dpi=140)
print(f"Wrote {out}")

# high-class cold check + cyclone gradient (stdout for the check-in)
hi = e[e.cls == "high"]
print(f"\n'high' class cold check ({len(hi)} segs): elev p50 {hi.elev.median():.0f} m / p90 {hi.elev.quantile(.9):.0f} m; "
      f"BIO6 p10 {hi.bio6.quantile(.1):.1f} / p50 {hi.bio6.median():.1f} °C; "
      f"cold-marginal {hi.cold_flag.sum()} ({hi.cold_flag.mean()*100:.0f}%)")
mid_y = e.geometry.interpolate(0.5, normalized=True).y
print("cyclone N-S gradient (mean passages):")
for lo, hi_ in [(-15, -12.5), (-18, -15), (-21, -18), (-24, -21), (-25.2, -24)]:
    m = (mid_y >= lo) & (mid_y < hi_)
    print(f"  lat {lo}..{hi_}: {e.loc[m, 'cyc_n'].mean():.1f}")
