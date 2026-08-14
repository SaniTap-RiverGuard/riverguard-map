/* RiverGuard carbon & finance model.
 * Pure functions; all parameters passed in via a `params` object whose defaults
 * come from docs/data/model_config.json (extracted from the VM0047 xlsx).
 * Every formula mirrors the xlsx; sheet references in comments.
 */

/* Seedling Density sheet logic (verified against every table row):
 * effective strip width = canopy + (rows-1)*spacing; length for 1 ha of
 * canopy-covered strip = 10000/width; seedlings/ha = rows * length/spacing. */
export function seedlingsPerHa(rows, spacingM, canopyD) {
  const width = canopyD + (rows - 1) * spacingM;
  return rows * 10000 / (width * spacingM);
}

/* Interpolate the per-clump growth curve (Biomass sheet, years 1-20) at year y
 * (fractional ok). Year 0 => 0. Returns {culms, totalKg, agbKg}. */
export function clumpAtYear(curve, y) {
  if (y <= 0) return { culms: 0, totalKg: 0, agbKg: 0 };
  const last = curve[curve.length - 1];
  if (y >= last.year) return { culms: last.culms_per_clump, totalKg: last.total_biomass_kg, agbKg: last.agb_kg };
  const i = curve.findIndex(p => p.year >= y);
  if (curve[i].year === y || i === 0) {
    const p = curve[i];
    if (p.year === y) return { culms: p.culms_per_clump, totalKg: p.total_biomass_kg, agbKg: p.agb_kg };
    // between year 0 and first point
    const f = y / curve[0].year;
    return { culms: curve[0].culms_per_clump * f, totalKg: curve[0].total_biomass_kg * f, agbKg: curve[0].agb_kg * f };
  }
  const a = curve[i - 1], b = curve[i];
  const f = (y - a.year) / (b.year - a.year);
  return {
    culms: a.culms_per_clump + f * (b.culms_per_clump - a.culms_per_clump),
    totalKg: a.total_biomass_kg + f * (b.total_biomass_kg - a.total_biomass_kg),
    agbKg: a.agb_kg + f * (b.agb_kg - a.agb_kg),
  };
}

/* Canopy cover fraction (0..1) of the planted strip at year y.
 * ASSUMPTION (documented in Assumptions modal): canopy area per clump scales
 * linearly with culm count, reaching the mature canopy diameter (Assumptions!B27,
 * 5 m) at the year-20 culm count. Cover = clump canopy area / ground area per
 * seedling, capped at 1. */
export function canopyCover(params, y) {
  const curve = params.species.balcooa.growth_curve;
  const matureCulms = curve[curve.length - 1].culms_per_clump;
  const { culms } = clumpAtYear(curve, y);
  const dia = params.planting.mature_canopy_diameter_m * Math.sqrt(culms / matureCulms);
  const areaPerClump = Math.PI * (dia / 2) ** 2;
  const perHa = seedlingsPerHa(params.planting.rows, params.planting.spacing_m,
    params.planting.mature_canopy_diameter_m) * params.planting.survival_rate;
  return Math.min(1, areaPerClump * perHa / 10000);
}

export function netFactor(d) {
  // Eq30: CR = ΔC * (1-PB) * (1-UNC) * (1-LK) * (1-buffer) = 0.5472 at defaults
  return (1 - d.performance_benchmark) * (1 - d.uncertainty) * (1 - d.leakage) * (1 - d.buffer);
}

/* Core scenario computation.
 * selection: { bankKm, speciesClumps: {balcooa: N, vulgaris: N, asper: N} }
 *   speciesClumps are SURVIVING clumps (survival already applied by caller).
 * Returns per-year arrays (index 0 = year 0) and totals.
 */
export function computeScenario(params, selection) {
  const P = params;
  const years = P.finance.project_years;
  const curve = P.species.balcooa.growth_curve;
  const scale = { balcooa: P.species.balcooa.scale, vulgaris: P.species.vulgaris.scale, asper: P.species.asper.scale };
  const areaHa = selection.areaHa;
  const nf = netFactor(P.carbon.deductions);
  const socPerHaYr = P.carbon.soc_tc_ha_yr * (44 / 12); // tCO2e/ha/yr (Eq11)

  /* Minor VM0047 pools (Eq6 herb, Eq8 dead wood, Eq10 litter): per-ha carbon
   * stock curves from the xlsx, ~4-6% of gross. Toggleable; on by default to
   * reproduce Eq1/Eq30 totals. */
  const mp = P.carbon.minor_pools; // {herb:[...], dead_wood:[...], litter:[...]} tC/ha, years 1-20, or null
  const minorAt = y => {
    if (!mp || !P.carbon.include_minor_pools || y <= 0) return 0;
    const idx = Math.min(Math.round(y), mp.herb.length) - 1;
    return (mp.herb[idx] + mp.dead_wood[idx] + mp.litter[idx]) * (44 / 12); // tCO2e/ha
  };

  const out = { year: [], biomassT: [], grossCum: [], netCum: [], netAnnual: [], revenue: [], price: [], canopy: [], perSpecies: {} };
  for (const sp of Object.keys(scale)) out.perSpecies[sp] = { biomassT: [], netCum: [] };

  for (let y = 0; y <= years; y++) {
    const clump = clumpAtYear(curve, y);
    let biomassT = 0, grossBio = 0;
    for (const sp of Object.keys(scale)) {
      const n = selection.speciesClumps[sp] || 0;
      const bT = n * clump.totalKg * scale[sp] / 1000; // dry tonnes
      const gT = bT * P.carbon.carbon_fraction * (44 / 12); // Biomass!K logic
      biomassT += bT; grossBio += gT;
      out.perSpecies[sp].biomassT.push(bT);
      out.perSpecies[sp].netCum.push(gT * nf);
    }
    const gross = grossBio + (socPerHaYr * y + minorAt(y)) * areaHa; // Eq1 = Eq2 (woody+minor) + Eq11
    out.year.push(y);
    out.biomassT.push(biomassT);
    out.grossCum.push(gross);
    out.netCum.push(gross * nf);
    out.netAnnual.push(y === 0 ? 0 : gross * nf - out.grossCum[y - 1] * nf);
    out.canopy.push(canopyCover(P, y));
  }

  /* Revenue (Revenue sheet): sales start in revenue_start_year (default 3),
   * selling credits generated (start-1) years earlier — the validation lag.
   * price(t) = P0 * (1+esc)^(t - start). */
  const start = P.finance.revenue_start_year;
  const lag = start - 1;
  let npv = 0, revCum = 0;
  for (let y = 0; y <= years; y++) {
    let rev = 0, price = 0;
    if (y >= start) {
      price = P.finance.carbon_price_usd * Math.pow(1 + P.finance.price_escalation, y - start);
      const creditYear = y - lag;
      if (creditYear >= 1) rev = out.netAnnual[creditYear] * price;
    }
    out.price.push(price);
    out.revenue.push(rev);
    revCum += rev;
    npv += rev / Math.pow(1 + P.finance.discount_rate, y);
  }
  out.totals = {
    biomassT: out.biomassT[years],
    grossCum: out.grossCum[years],
    netCum: out.netCum[years],
    revenueCum: revCum,
    npv,
    netFactor: nf,
  };
  return out;
}

/* Build selection aggregates from selected segments + drawn polygons.
 * segs: array of {props} (GeoJSON properties, short keys). polys: [{areaHa, mix}]
 * cfg: ui config {stripWidthM, rows, spacingM, survival, mixMode, globalMix} */
export function buildSelection(params, segs, polys, cfg) {
  const perHa = seedlingsPerHa(cfg.rows, cfg.spacingM, params.planting.mature_canopy_diameter_m);
  let bankKm = 0;
  const clumps = { balcooa: 0, vulgaris: 0, asper: 0 };
  let segAreaHa = 0;
  for (const p of segs) {
    const km = p.L / 1000;
    bankKm += km;
    let ha = km * cfg.stripWidthM / 10; // 20 m -> 2 ha/km (Assumptions!B6), single side
    /* Optional heuristic: likely-paddy (p.up, fraction of buffer) is not
     * plantable; scale area by the paddy share of the plantable fraction. */
    if (cfg.subtractPaddy && p.up > 0 && p.lf > 0) {
      ha *= Math.max(0, 1 - p.up / p.lf);
    }
    segAreaHa += ha;
    const seedlings = ha * perHa * cfg.survival;
    const mix = cfg.mixMode === 'recommended'
      ? { balcooa: p.mb / 100, vulgaris: p.mv / 100, asper: p.ma / 100 }
      : cfg.globalMix;
    for (const sp of Object.keys(clumps)) clumps[sp] += seedlings * mix[sp];
  }
  let polyAreaHa = 0;
  for (const poly of polys) {
    polyAreaHa += poly.areaHa;
    const seedlings = poly.areaHa * perHa * cfg.survival;
    const mix = poly.mix || cfg.globalMix;
    for (const sp of Object.keys(clumps)) clumps[sp] += seedlings * mix[sp];
  }
  const areaHa = segAreaHa + polyAreaHa;
  const totalSeedlings = areaHa * perHa; // planted (pre-survival)
  return { bankKm, areaHa, segAreaHa, polyAreaHa, totalSeedlings, seedlingsPerHa: perHa, speciesClumps: clumps };
}
