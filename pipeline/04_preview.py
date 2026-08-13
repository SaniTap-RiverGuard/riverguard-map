#!/usr/bin/env python3
"""Step 4: Render a static PNG preview of the suitability layer for review,
with insets for the Efaho (expected HIGH) and Mandrare (expected LOW) sanity
sites.

Run after 03_score.py: .venv/bin/python pipeline/04_preview.py
"""
import json
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text())
DERIVED = ROOT / "data" / "derived"

segs = gpd.read_file(DERIVED / "segments_scored.gpkg")

COLORS = {"high": "#1a9850", "medium": "#fdae61", "low": "#bdbdbd", "excluded": "#f2f0f0"}
ORDER = ["excluded", "low", "medium", "high"]

fig = plt.figure(figsize=(16, 13), facecolor="white")
gs = fig.add_gridspec(2, 2, width_ratios=[1.35, 1], hspace=0.12, wspace=0.08)
ax = fig.add_subplot(gs[:, 0])

for cls in ORDER:
    sub = segs[segs["cls"] == cls]
    if len(sub):
        sub.plot(ax=ax, color=COLORS[cls], linewidth=0.5 if cls in ("excluded", "low") else 0.8)

ax.set_title("RiverGuard — East-coast riparian bamboo suitability (segments, ~500 m)", fontsize=13)
ax.set_aspect(1 / 0.92)
handles = [Line2D([0], [0], color=COLORS[c], lw=3,
                  label=f"{c} ({(segs['cls'] == c).sum():,})") for c in ORDER[::-1]]
handles.append(Line2D([], [], marker="*", color="red", lw=0, markersize=12, label="sanity sites"))
ax.legend(handles=handles, loc="lower left", fontsize=9)

for i, chk in enumerate(CFG["sanity_checks"]):
    ax.plot(chk["lon"], chk["lat"], marker="*", color="red", markersize=14, zorder=5)
    axi = fig.add_subplot(gs[i, 1])
    pad = 0.35
    x0, x1 = chk["lon"] - pad, chk["lon"] + pad
    y0, y1 = chk["lat"] - pad, chk["lat"] + pad
    local = segs.cx[x0:x1, y0:y1]
    for cls in ORDER:
        sub = local[local["cls"] == cls]
        if len(sub):
            sub.plot(ax=axi, color=COLORS[cls], linewidth=2.2)
    axi.plot(chk["lon"], chk["lat"], marker="*", color="red", markersize=16, zorder=5)
    axi.set_xlim(x0, x1); axi.set_ylim(y0, y1)
    med = local["score"].median() if len(local) else float("nan")
    axi.set_title(f"{chk['name']}\nexpected {chk['expect'].upper()} — "
                  f"{len(local)} segs, median score {med:.0f}", fontsize=10)
    axi.set_aspect(1 / 0.92)

for a in fig.axes:
    a.tick_params(labelsize=7)

out = DERIVED / "suitability_preview.png"
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"Wrote {out}")
