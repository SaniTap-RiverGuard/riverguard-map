/* RiverGuard app main. */
import { seedlingsPerHa, computeScenario, buildSelection, netFactor, canopyCover } from './model.js';
import { SuitabilityMap, popupHtml } from './map.js';

const $ = id => document.getElementById(id);
const fmt = (n, d = 0) => n.toLocaleString('en-US', { maximumFractionDigits: d });
const fmtM = n => n >= 1e6 ? '$' + (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? '$' + (n / 1e3).toFixed(0) + 'k' : '$' + n.toFixed(0);

let MC = null;          // model_config.json (xlsx-derived defaults)
let benchmarkMeta = null; // {score, total_km} from segments.geojson _benchmark
let params = null;      // live parameters (MC + UI overrides)
let uiCfg = null;       // planting/mix UI state
let map = null;
let chart = null;
let scenario = null;
let playTimer = null;
let activePreset = null;

async function init() {
  const [mcRes, segRes] = await Promise.all([
    fetch('data/model_config.json'), fetch('data/segments.geojson'),
  ]);
  MC = await mcRes.json();
  const segs = await segRes.json();

  params = liveParams();
  uiCfg = {
    stripWidthM: MC.planting.strip_width_m.value,
    rows: MC.planting.default_rows.value,
    spacingM: MC.planting.default_spacing_m.value,
    survival: MC.planting.survival_rate.value,
    mixMode: 'recommended',
    globalMix: { balcooa: 0.6, vulgaris: 0.25, asper: 0.15 },
    subtractPaddy: false,
  };

  map = new SuitabilityMap('map', {
    onSelectionChange: () => { recompute(); writeHash(); },
    onPolygonsChange: () => { renderDrawnList(); recompute(); },
    popupHtml,
  });
  map.loadSegments(segs);
  benchmarkMeta = segs._benchmark || { score: 79, total_km: 0 };
  $('present-benchmark').textContent =
    `${fmt(benchmarkMeta.total_km)} km of river bank scores at or above our proven Efaho trial benchmark`;

  initInputs();
  initSearch();
  initExports();
  initPresentation();
  initAssumptions();
  initChart();
  readHash();
  recompute();
  $('loading').classList.add('hidden');
  window.__rg = { map, get params() { return params; }, get scenario() { return scenario; } }; // debug/test handle
}

function liveParams() {
  // deep-ish copy of defaults with .value unwrapped, mutated by UI
  return {
    planting: {
      strip_width_m: MC.planting.strip_width_m.value,
      mature_canopy_diameter_m: MC.planting.mature_canopy_diameter_m.value,
      rows: MC.planting.default_rows.value,
      spacing_m: MC.planting.default_spacing_m.value,
      survival_rate: MC.planting.survival_rate.value,
    },
    carbon: {
      carbon_fraction: MC.carbon.carbon_fraction.value,
      root_shoot_ratio: MC.carbon.root_shoot_ratio.value,
      soc_tc_ha_yr: MC.carbon.soc_tc_ha_yr.value,
      include_minor_pools: true,
      minor_pools: MC.carbon.minor_pools_tc_ha ? {
        herb: MC.carbon.minor_pools_tc_ha.herb.values,
        dead_wood: MC.carbon.minor_pools_tc_ha.dead_wood.values,
        litter: MC.carbon.minor_pools_tc_ha.litter.values,
      } : null,
      deductions: {
        performance_benchmark: MC.carbon.deductions.performance_benchmark.value,
        uncertainty: MC.carbon.deductions.uncertainty.value,
        leakage: MC.carbon.deductions.leakage.value,
        buffer: MC.carbon.deductions.buffer.value,
      },
    },
    finance: {
      carbon_price_usd: MC.finance.carbon_price_usd.value,
      price_escalation: MC.finance.price_escalation.value,
      project_years: MC.finance.project_years.value,
      discount_rate: MC.finance.discount_rate.value,
      revenue_start_year: MC.finance.revenue_start_year.value,
    },
    species: {
      balcooa: { scale: MC.species.balcooa.scale.value, growth_curve: MC.species.balcooa.growth_curve },
      vulgaris: { scale: MC.species.vulgaris.scale.value },
      asper: { scale: MC.species.asper.scale.value },
    },
  };
}

/* ------------------------------------------------ inputs */
const inputGetters = {};  // id -> getter, for re-syncing values after scenario import

function syncInputs() {
  for (const [id, get] of Object.entries(inputGetters)) $(id).value = get();
  $('mix-rec').checked = uiCfg.mixMode === 'recommended';
  $('mix-man').checked = uiCfg.mixMode === 'manual';
  $('mix-b').value = uiCfg.globalMix.balcooa * 100;
  $('mix-v').value = uiCfg.globalMix.vulgaris * 100;
  $('mix-a').value = uiCfg.globalMix.asper * 100;
  updateMixLabels();
}

function initInputs() {
  const bind = (id, get, set) => {
    inputGetters[id] = get;
    const el = $(id);
    el.value = get();
    el.addEventListener('input', () => { set(parseFloat(el.value)); recompute(); });
  };
  bind('cfg-width', () => uiCfg.stripWidthM, v => { uiCfg.stripWidthM = v || 20; updatePresetStats(); });
  bind('cfg-rows', () => uiCfg.rows, v => { uiCfg.rows = Math.max(1, Math.min(5, v || 5)); params.planting.rows = uiCfg.rows; });
  bind('cfg-spacing', () => uiCfg.spacingM, v => { uiCfg.spacingM = v || 4; params.planting.spacing_m = uiCfg.spacingM; });
  bind('cfg-survival', () => uiCfg.survival, v => { uiCfg.survival = v || .7; params.planting.survival_rate = uiCfg.survival; });
  bind('cfg-price', () => params.finance.carbon_price_usd, v => params.finance.carbon_price_usd = v || 10);
  bind('cfg-esc', () => params.finance.price_escalation * 100, v => params.finance.price_escalation = (v || 0) / 100);
  bind('cfg-discount', () => params.finance.discount_rate * 100, v => params.finance.discount_rate = (v || 0) / 100);
  bind('cfg-revstart', () => params.finance.revenue_start_year, v => params.finance.revenue_start_year = v || 3);
  bind('cfg-years', () => params.finance.project_years, v => {
    params.finance.project_years = Math.max(5, Math.min(40, v || 20));
    $('year-slider').max = params.finance.project_years;
  });
  bind('cfg-pb', () => params.carbon.deductions.performance_benchmark, v => params.carbon.deductions.performance_benchmark = v || 0);
  bind('cfg-unc', () => params.carbon.deductions.uncertainty, v => params.carbon.deductions.uncertainty = v || 0);
  bind('cfg-leak', () => params.carbon.deductions.leakage, v => params.carbon.deductions.leakage = v || 0);
  bind('cfg-buffer', () => params.carbon.deductions.buffer, v => params.carbon.deductions.buffer = v || 0);
  $('cfg-minor-pools').checked = params.carbon.include_minor_pools;
  $('cfg-minor-pools').addEventListener('change', () => {
    params.carbon.include_minor_pools = $('cfg-minor-pools').checked; recompute();
  });
  bind('cfg-scale-v', () => params.species.vulgaris.scale, v => params.species.vulgaris.scale = v || 0.85);
  bind('cfg-scale-a', () => params.species.asper.scale, v => params.species.asper.scale = v || 1);

  $('mix-rec').checked = true;
  $('mix-rec').addEventListener('change', () => { uiCfg.mixMode = 'recommended'; recompute(); });
  $('mix-man').addEventListener('change', () => { uiCfg.mixMode = 'manual'; recompute(); });
  const sliders = { b: 'balcooa', v: 'vulgaris', a: 'asper' };
  for (const k of Object.keys(sliders)) {
    $(`mix-${k}`).value = uiCfg.globalMix[sliders[k]] * 100;
    $(`mix-${k}`).addEventListener('input', () => {
      uiCfg.mixMode = 'manual'; $('mix-man').checked = true;
      normaliseMix(k, sliders); recompute();
    });
  }
  updateMixLabels();

  $('btn-clear').addEventListener('click', () => map.clearSelection());
  $('btn-select-visible-high').addEventListener('click', () => map.selectVisibleHigh());
  // Decision filters -> one predicate over segment properties
  const LEGENDS = {
    suit: 'green high · amber medium · grey low · pale excluded',
    access: 'green road-adjacent · blue boat-reachable · grey remote',
    pop: 'darker blue = more people within 5 km (breaks: 200 / 1k / 3k / 10k)',
    cyclone: 'darker red = more cyclone passages within 100 km since 1986 (1/3/5/8)',
    cold: 'purple = cold-marginal (BIO6 < 10 °C) · green = not flagged',
  };
  $('overlay-mode').addEventListener('change', () => {
    map.setOverlay($('overlay-mode').value);
    $('overlay-legend').textContent = LEGENDS[$('overlay-mode').value];
  });
  $('overlay-legend').textContent = LEGENDS.suit;

  initFilterControls();

  /* Filter presets (agreed 2026-08-14): each just SETS the visible filter
   * controls — users see the values and tweak from there. Headline next to
   * each button = full-dataset result of that preset's criteria. */
  initTooltips();

  $('cfg-paddy').addEventListener('change', () => {
    uiCfg.subtractPaddy = $('cfg-paddy').checked; recompute();
  });

  $('year-slider').addEventListener('input', () => setYear(parseInt($('year-slider').value)));
  $('btn-play').addEventListener('click', playPause);
}

function normaliseMix(changed, sliders) {
  const vals = {};
  for (const k of Object.keys(sliders)) vals[k] = parseFloat($(`mix-${k}`).value);
  const others = Object.keys(sliders).filter(k => k !== changed);
  const rest = 100 - vals[changed];
  const otherSum = others.reduce((s, k) => s + vals[k], 0);
  for (const k of others) {
    vals[k] = otherSum > 0 ? vals[k] / otherSum * rest : rest / others.length;
    $(`mix-${k}`).value = vals[k];
  }
  for (const k of Object.keys(sliders)) uiCfg.globalMix[sliders[k]] = vals[k] / 100;
  updateMixLabels();
}

function updateMixLabels() {
  $('mix-b-val').textContent = ` ${Math.round(uiCfg.globalMix.balcooa * 100)}%`;
  $('mix-v-val').textContent = ` ${Math.round(uiCfg.globalMix.vulgaris * 100)}%`;
  $('mix-a-val').textContent = ` ${Math.round(uiCfg.globalMix.asper * 100)}%`;
}

/* ------------------------------------------------ decision filters
 * One principle: the user switches criteria on/off and sets their DIRECTION —
 * the tool never decides whether an attribute is good or bad. Every criterion
 * is [enable checkbox] + [direction/threshold control]; disabled = zero effect.
 */
const BUILTIN_PRESETS = {
  op: {
    name: 'Easy to plant + community nearby',
    state: { suit: { on: true, dir: 'tb' }, acc: { on: true, r: true, b: true, x: false }, pop: { on: true, min: 1000 } },
  },
  bio: {
    name: 'Best biodiversity case',
    state: { suit: { on: true, dir: 'tb' }, pa: { on: true, max: 10 } },
    /* Cyclone deliberately NOT part of this preset (2026-08-15): conflating the
     * adaptation narrative with biodiversity proximity wrongly wiped out Anosy
     * sites near Tsitongambarika. Cyclone stays an independent filter. */
  },
};

function defaultFilterState() {
  return {
    suit: { on: false, dir: 'tb' },
    acc: { on: false, r: true, b: true, x: false },
    pop: { on: false, min: 1000 },
    pa: { on: false, max: 10 },
    cyc: { on: false, dir: 'only' },
    cold: { on: false, dir: 'excl' },
    paddy: { on: false, max: 30 },
    fire: { on: false, dir: 'excl' },
    sal: { on: false },
  };
}

function readFilterState() {
  return {
    suit: { on: $('fe-suit').checked, dir: $('fd-suit').value },
    acc: { on: $('fe-acc').checked, r: $('fd-acc-r').checked, b: $('fd-acc-b').checked, x: $('fd-acc-x').checked },
    pop: { on: $('fe-pop').checked, min: +$('fd-pop').value },
    pa: { on: $('fe-pa').checked, max: +$('fd-pa').value },
    cyc: { on: $('fe-cyc').checked, dir: $('fd-cyc').value },
    cold: { on: $('fe-cold').checked, dir: $('fd-cold').value },
    paddy: { on: $('fe-paddy').checked, max: +$('fd-paddy').value },
    fire: { on: $('fe-fire').checked, dir: $('fd-fire').value },
    sal: { on: $('fe-sal').checked },
  };
}

function writeFilterState(s) {
  $('fe-suit').checked = s.suit.on; $('fd-suit').value = s.suit.dir;
  $('fe-acc').checked = s.acc.on;
  $('fd-acc-r').checked = s.acc.r; $('fd-acc-b').checked = s.acc.b; $('fd-acc-x').checked = s.acc.x;
  $('fe-pop').checked = s.pop.on; $('fd-pop').value = s.pop.min;
  $('fe-pa').checked = s.pa.on; $('fd-pa').value = s.pa.max;
  $('fe-cyc').checked = s.cyc.on; $('fd-cyc').value = s.cyc.dir;
  $('fe-cold').checked = s.cold.on; $('fd-cold').value = s.cold.dir;
  $('fe-paddy').checked = s.paddy.on; $('fd-paddy').value = s.paddy.max;
  $('fe-fire').checked = s.fire.on; $('fd-fire').value = s.fire.dir;
  $('fe-sal').checked = s.sal.on;
  updateFilterValueLabels();
}

function updateFilterValueLabels() {
  $('fd-pop-val').textContent = (+$('fd-pop').value).toLocaleString();
  $('fd-pa-val').textContent = $('fd-pa').value;
  $('fd-paddy-val').textContent = $('fd-paddy').value;
}

function predicateFrom(s) {
  return p => {
    if (p.p5 === undefined) return true;   // excluded segments carry no decision attrs; class filter below
    if (s.suit.on) {
      if (s.suit.dir === 'tb' && !p.tb) return false;
      if (s.suit.dir === 'm' && !(p.c === 'm' || p.c === 'h')) return false;
      if (s.suit.dir === 'h' && p.c !== 'h') return false;
    }
    if (s.acc.on && !{ r: s.acc.r, b: s.acc.b, x: s.acc.x }[p.ac]) return false;
    if (s.pop.on && p.p5 < s.pop.min) return false;
    if (s.pa.on && p.pk > s.pa.max) return false;
    if (s.cyc.on && (s.cyc.dir === 'only' ? !p.cf : p.cf)) return false;
    if (s.cold.on && (s.cold.dir === 'only' ? !p.cm : p.cm)) return false;
    if (s.paddy.on && p.up * 100 > s.paddy.max) return false;
    if (s.fire.on && (s.fire.dir === 'only' ? !p.ff : p.ff)) return false;
    if (s.sal.on && p.sx) return false;
    return true;
  };
}

function countActive(s) {
  return Object.values(s).filter(c => c.on).length;
}

function matchStats(pred) {
  let n = 0, km = 0;
  for (const f of map.features) {
    const p = f.properties;
    if (p.c !== 'e' && pred(p)) { n++; km += p.L / 1000; }
  }
  return { n, km, ha: km * uiCfg.stripWidthM / 10 };
}

function applyFilters() {
  updateFilterValueLabels();
  const s = readFilterState();
  const nActive = countActive(s);
  const pred = predicateFrom(s);
  map.setFilter(nActive === 0 ? null : pred);
  const m = matchStats(pred);
  $('filter-match').innerHTML = nActive === 0
    ? 'No filters active — all segments match.'
    : `<b>${fmt(m.n)}</b> segments · <b>${fmt(m.km)}</b> km · <b>${fmt(m.ha)}</b> ha match`;
  if (nActive === 0) {
    $('filter-summary').classList.add('hidden');
  } else {
    $('filter-summary-text').textContent =
      `${nActive} filter${nActive > 1 ? 's' : ''} active — ${fmt(m.km)} km shown`;
    $('filter-summary').classList.remove('hidden');
  }
  updatePresentationLines();
}

function clearPresetHighlight() {
  activePreset = null;
  for (const b of document.querySelectorAll('.preset-btn, .saved-preset-btn')) b.classList.remove('active');
}

function presetName(key) {
  if (!key) return null;
  if (BUILTIN_PRESETS[key]) return BUILTIN_PRESETS[key].name;
  return key.startsWith('saved:') ? key.slice(6) : key;
}

function loadSavedPresets() {
  try { return JSON.parse(localStorage.getItem('rg_presets') || '{}'); } catch { return {}; }
}

function activatePreset(key, state, btn) {
  if (activePreset === key) {   // toggle off
    writeFilterState(defaultFilterState());
    clearPresetHighlight();
    applyFilters();
    return;
  }
  clearPresetHighlight();
  const full = defaultFilterState();
  for (const [k, v] of Object.entries(state)) full[k] = { ...full[k], ...v, on: true };
  writeFilterState(full);
  activePreset = key;
  if (btn) btn.classList.add('active');
  applyFilters();
}

function renderSavedPresets() {
  const box = $('saved-presets');
  const saved = loadSavedPresets();
  box.innerHTML = '';
  for (const [name, state] of Object.entries(saved)) {
    const row = document.createElement('div');
    row.className = 'saved-preset-row';
    const btn = document.createElement('button');
    btn.className = 'saved-preset-btn';
    btn.textContent = name;
    btn.addEventListener('click', () => activatePreset(`saved:${name}`, state, btn));
    const del = document.createElement('button');
    del.className = 'saved-preset-del';
    del.textContent = '✕';
    del.title = 'Delete preset';
    del.addEventListener('click', () => {
      const cur = loadSavedPresets();
      delete cur[name];
      localStorage.setItem('rg_presets', JSON.stringify(cur));
      renderSavedPresets();
    });
    row.append(btn, del);
    const stat = document.createElement('span');
    stat.className = 'preset-stat';
    const m = matchStats(predicateFrom({ ...defaultFilterState(), ...state }));
    stat.textContent = `${fmt(m.n)} segs · ${fmt(m.km)} km · ${fmt(m.ha)} ha`;
    box.append(row, stat);
  }
}

function initFilterControls() {
  writeFilterState(defaultFilterState());
  const ids = ['fe-suit', 'fd-suit', 'fe-acc', 'fd-acc-r', 'fd-acc-b', 'fd-acc-x', 'fe-pop', 'fd-pop',
    'fe-pa', 'fd-pa', 'fe-cyc', 'fd-cyc', 'fe-cold', 'fd-cold', 'fe-paddy', 'fd-paddy',
    'fe-fire', 'fd-fire', 'fe-sal'];
  for (const id of ids)
    $(id).addEventListener('input', () => { clearPresetHighlight(); applyFilters(); });
  for (const btn of document.querySelectorAll('.preset-btn')) {
    const key = btn.dataset.preset;
    btn.addEventListener('click', () => activatePreset(key, BUILTIN_PRESETS[key].state, btn));
  }
  $('preset-clear').addEventListener('click', () => {
    writeFilterState(defaultFilterState());
    clearPresetHighlight();
    applyFilters();
  });
  $('filter-summary-clear').addEventListener('click', () => {
    writeFilterState(defaultFilterState());
    clearPresetHighlight();
    applyFilters();
  });
  $('preset-save').addEventListener('click', () => {
    const s = readFilterState();
    if (countActive(s) === 0) { alert('Activate at least one filter first.'); return; }
    const name = prompt('Preset name:');
    if (!name) return;
    const saved = loadSavedPresets();
    saved[name.trim()] = s;
    localStorage.setItem('rg_presets', JSON.stringify(saved));
    renderSavedPresets();
  });
  renderSavedPresets();
  updatePresetStats();
  applyFilters();
}

/* Tap/hover tooltips for the ⓘ icons — one shared bubble, click-to-toggle so
 * it works on touch screens, hover as a convenience on desktop. */
function initTooltips() {
  const tip = document.createElement('div');
  tip.id = 'tooltip';
  tip.setAttribute('role', 'tooltip');
  document.body.appendChild(tip);
  let pinned = null;
  const show = btn => {
    tip.textContent = btn.dataset.tip;
    tip.classList.add('show');
    const r = btn.getBoundingClientRect();
    tip.style.top = `${Math.max(6, r.bottom + 6)}px`;
    const left = Math.min(Math.max(6, r.left - 40), window.innerWidth - tip.offsetWidth - 6);
    tip.style.left = `${left}px`;
  };
  const hide = () => { tip.classList.remove('show'); pinned = null; };
  for (const btn of document.querySelectorAll('.info')) {
    btn.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation();
      if (pinned === btn) { hide(); return; }
      pinned = btn; show(btn);
    });
    btn.addEventListener('mouseenter', () => { if (!pinned) show(btn); });
    btn.addEventListener('mouseleave', () => { if (!pinned) hide(); });
  }
  document.addEventListener('click', e => { if (!e.target.closest('.info')) hide(); });
}

function updatePresentationLines() {
  if (!map) return;
  const props = map.selectedProps();
  const cycKm = props.reduce((a, p) => a + (p.cf ? p.L : 0), 0) / 1000;
  $('present-cyclone').textContent = cycKm > 0
    ? `${fmt(cycKm, 1)} km of highly cyclone-exposed bank protected in this scenario` : '';
  const el = $('present-preset');
  if (el) el.textContent = activePreset ? `Showing: ${presetName(activePreset)}` : '';
}

function updatePresetStats() {
  for (const [key, def] of Object.entries(BUILTIN_PRESETS)) {
    const full = defaultFilterState();
    for (const [k, v] of Object.entries(def.state)) full[k] = { ...full[k], ...v, on: true };
    const m = matchStats(predicateFrom(full));
    $(`preset-${key}-stat`).textContent = `${fmt(m.n)} segs · ${fmt(m.km)} km · ${fmt(m.ha)} ha`;
  }
}

/* ------------------------------------------------ compute + render */
function recompute() {
  const segProps = map.selectedProps();
  const polys = [...map.drawnPolys.entries()].map(([id, d]) => ({ id, areaHa: d.areaHa, mix: d.mix }));
  const sel = buildSelection(params, segProps, polys, uiCfg);
  scenario = computeScenario(params, sel);
  scenario.selection = sel;

  const perHa = sel.seedlingsPerHa;
  $('density-readout').innerHTML =
    `<b>${fmt(perHa)}</b> seedlings/ha · <b>${fmt(perHa * uiCfg.stripWidthM / 10)}</b>/km of bank` +
    `<br>strip ${uiCfg.stripWidthM} m × ${uiCfg.rows} rows @ ${uiCfg.spacingM} m`;
  $('netfactor-readout').innerHTML = `Net factor: <b>×${netFactor(params.carbon.deductions).toFixed(4)}</b> of gross`;

  renderResults();
  renderChart();
  renderSpeciesBreakdown();
  setYear(parseInt($('year-slider').value), true);
}

function renderResults() {
  const y = parseInt($('year-slider').value);
  const s = scenario, sel = s.selection;
  $('r-bank').textContent = fmt(sel.bankKm, 1);
  const props = map.selectedProps();
  const tbKm = props.reduce((a, p) => a + (p.tb ? p.L : 0), 0) / 1000;
  $('r-tbkm').textContent = fmt(tbKm, 1);
  updatePresentationLines();
  $('r-area').textContent = fmt(sel.areaHa, 1);
  $('r-seedlings').textContent = fmt(sel.totalSeedlings);
  $('r-net').textContent = fmt(s.netCum[Math.min(y, s.netCum.length - 1)]);
  const revCum = s.revenue.slice(0, y + 1).reduce((a, b) => a + b, 0);
  $('r-revenue').textContent = fmtM(revCum);
  $('r-npv').textContent = fmtM(s.totals.npv);
}

function setYear(y, keep = false) {
  $('year-label').textContent = `Year ${y}`;
  if (!keep) $('year-slider').value = y;
  map.setYearVisual(scenario ? scenario.canopy[Math.min(y, scenario.canopy.length - 1)] : 0);
  renderResults();
  if (chart) {
    chart.options.plugins.annotationYear = y;
    chart.update('none');
  }
}

function playPause() {
  if (playTimer) { clearInterval(playTimer); playTimer = null; $('btn-play').textContent = '▶'; return; }
  $('btn-play').textContent = '⏸';
  let y = parseInt($('year-slider').value);
  if (y >= params.finance.project_years) y = 0;
  playTimer = setInterval(() => {
    y++;
    setYear(y);
    if (y >= params.finance.project_years) { clearInterval(playTimer); playTimer = null; $('btn-play').textContent = '▶'; }
  }, 600);
}

/* ------------------------------------------------ chart */
const yearLine = {
  id: 'yearLine',
  afterDraw(c) {
    const y = c.options.plugins.annotationYear ?? 0;
    const x = c.scales.x.getPixelForValue(y);
    if (!isFinite(x)) return;
    const ctx = c.ctx;
    ctx.save();
    ctx.strokeStyle = 'rgba(13,122,63,.8)'; ctx.lineWidth = 2; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(x, c.chartArea.top); ctx.lineTo(x, c.chartArea.bottom); ctx.stroke();
    ctx.restore();
  }
};

function initChart() {
  $('btn-chart-toggle').addEventListener('click', () => $('chart-panel').classList.toggle('hidden'));
  chart = new Chart($('chart-canvas'), {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { labels: { boxWidth: 12, font: { size: 10 } } }, annotationYear: 0 },
      scales: {
        x: { title: { display: true, text: 'Year' } },
        yC: { position: 'left', title: { display: true, text: 'net tCO₂e (cum.)' } },
        yR: { position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: 'revenue USD/yr' } },
      },
    },
    plugins: [yearLine],
  });
}

function renderChart() {
  if (!chart || !scenario) return;
  chart.data.labels = scenario.year;
  chart.data.datasets = [
    { label: 'Net tCO₂e (cumulative)', data: scenario.netCum, borderColor: '#0d7a3f', backgroundColor: 'rgba(13,122,63,.12)', fill: true, yAxisID: 'yC', pointRadius: 0, tension: .25 },
    { label: 'Revenue (USD/yr)', data: scenario.revenue, borderColor: '#c98a1b', yAxisID: 'yR', pointRadius: 0, tension: .25 },
  ];
  chart.update('none');
}

function renderSpeciesBreakdown() {
  const s = scenario;
  const yEnd = params.finance.project_years;
  const names = { balcooa: 'B. balcooa', vulgaris: 'B. vulgaris*', asper: 'D. asper*' };
  let html = '<table><tr><th>Species</th><th>Clumps</th><th>Biomass t (y20)</th><th>Net tCO₂e</th></tr>';
  for (const sp of Object.keys(names)) {
    html += `<tr><td>${names[sp]}</td><td>${fmt(s.selection.speciesClumps[sp])}</td>` +
      `<td>${fmt(s.perSpecies[sp].biomassT[yEnd])}</td><td>${fmt(s.perSpecies[sp].netCum[yEnd])}</td></tr>`;
  }
  html += '</table><span class="prov">* provisional scaled balcooa curve — no measured data yet</span>';
  $('species-breakdown').innerHTML = html;
}

function renderDrawnList() {
  const el = $('drawn-list');
  const items = [...map.drawnPolys.entries()];
  el.innerHTML = items.length ? '<h3 style="font-size:11px;margin:8px 0 2px">Drawn areas</h3>' : '';
  for (const [id, d] of items) {
    const row = document.createElement('div');
    row.className = 'drawn-item';
    row.innerHTML = `<span>${id}: ${d.areaHa.toFixed(1)} ha</span>`;
    const btn = document.createElement('button');
    btn.textContent = '✕';
    btn.addEventListener('click', () => map.removeDrawn(id));
    row.appendChild(btn);
    el.appendChild(row);
  }
}

/* ------------------------------------------------ search */
function initSearch() {
  const input = $('search-input'), results = $('search-results');
  let t = null;
  input.addEventListener('input', () => {
    clearTimeout(t);
    const q = input.value.trim();
    if (!q) { results.classList.add('hidden'); return; }
    const coord = q.match(/^(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)$/);
    if (coord) {
      results.innerHTML = `<div data-lat="${coord[1]}" data-lon="${coord[2]}">Go to ${coord[1]}, ${coord[2]}</div>`;
      results.classList.remove('hidden');
      return;
    }
    t = setTimeout(async () => {
      try {
        const r = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=6&countrycodes=mg&q=${encodeURIComponent(q)}`);
        const js = await r.json();
        results.innerHTML = js.map(p => `<div data-lat="${p.lat}" data-lon="${p.lon}">${p.display_name}</div>`).join('') || '<div>No results</div>';
        results.classList.remove('hidden');
      } catch { results.innerHTML = '<div>Search unavailable</div>'; results.classList.remove('hidden'); }
    }, 450);
  });
  results.addEventListener('click', e => {
    const d = e.target.closest('[data-lat]');
    if (!d) return;
    map.flyTo(parseFloat(d.dataset.lat), parseFloat(d.dataset.lon));
    results.classList.add('hidden');
  });
  document.addEventListener('click', e => { if (!e.target.closest('#searchbox')) results.classList.add('hidden'); });
}

/* ------------------------------------------------ exports / import / hash */
function download(name, text, type = 'text/plain') {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function scenarioObject() {
  return {
    version: 1,
    saved: new Date().toISOString(),
    uiCfg, params: { ...params, species: { ...params.species, balcooa: { scale: params.species.balcooa.scale } } },
    selectedSegments: [...map.selected],
    drawnPolygons: [...map.drawnPolys.entries()].map(([id, d]) => ({
      id, areaHa: d.areaHa, coords: d.layer.getLatLngs()[0].map(p => [p.lat, p.lng]),
    })),
  };
}

function initExports() {
  $('btn-export-csv').addEventListener('click', () => {
    const s = scenario;
    let csv = 'year,biomass_t,gross_tco2e_cum,net_tco2e_cum,net_tco2e_annual,price_usd,revenue_usd,canopy_cover\n';
    for (let y = 0; y < s.year.length; y++) {
      csv += [y, s.biomassT[y].toFixed(1), s.grossCum[y].toFixed(1), s.netCum[y].toFixed(1),
        s.netAnnual[y].toFixed(1), s.price[y].toFixed(2), s.revenue[y].toFixed(0), s.canopy[y].toFixed(3)].join(',') + '\n';
    }
    const props = map.selectedProps();
    const kmWhere = fn => (props.reduce((a, p) => a + (fn(p) ? p.L : 0), 0) / 1000).toFixed(2);
    csv += `\nTotals,,,,,,,\nbank_km,${s.selection.bankKm.toFixed(2)},,,,,,\n` +
      `bank_km_meets_trial_benchmark,${kmWhere(p => p.tb)},,,,,,\narea_ha,${s.selection.areaHa.toFixed(1)},,,,,,\n` +
      `seedlings,${Math.round(s.selection.totalSeedlings)},,,,,,\nnpv_usd,${s.totals.npv.toFixed(0)},,,,,,\n` +
      `bank_km_road_adjacent,${kmWhere(p => p.ac === 'r')},,,,,,\nbank_km_boat_reachable,${kmWhere(p => p.ac === 'b')},,,,,,\n` +
      `bank_km_remote,${kmWhere(p => p.ac === 'x')},,,,,,\nbank_km_high_cyclone,${kmWhere(p => p.cf)},,,,,,\n` +
      `bank_km_high_fire,${kmWhere(p => p.ff)},,,,,,\nbank_km_cold_marginal,${kmWhere(p => p.cm)},,,,,,\n` +
      `mean_pop_5km,${props.length ? Math.round(props.reduce((a, p) => a + (p.p5 || 0), 0) / props.length) : 0},,,,,,\n`;
    download('riverguard_scenario.csv', csv, 'text/csv');
  });
  $('btn-export-geojson').addEventListener('click', () => {
    const feats = map.features.filter(f => map.selected.has(f.properties.id));
    for (const [id, d] of map.drawnPolys) {
      feats.push({ type: 'Feature', properties: { id, drawn: true, areaHa: d.areaHa },
        geometry: { type: 'Polygon', coordinates: [d.layer.getLatLngs()[0].map(p => [p.lng, p.lat])] } });
    }
    download('riverguard_selection.geojson', JSON.stringify({ type: 'FeatureCollection', features: feats }), 'application/geo+json');
  });
  $('btn-export-scenario').addEventListener('click', () =>
    download('riverguard_scenario.json', JSON.stringify(scenarioObject(), null, 2), 'application/json'));
  $('btn-import-scenario').addEventListener('click', () => $('import-file').click());
  $('import-file').addEventListener('change', async e => {
    const f = e.target.files[0];
    if (!f) return;
    const js = JSON.parse(await f.text());
    applyScenario(js);
    e.target.value = '';
  });
  $('btn-share').addEventListener('click', () => {
    writeHash(true);
    navigator.clipboard.writeText(location.href).then(() => {
      $('btn-share').textContent = 'Link copied ✓';
      setTimeout(() => $('btn-share').textContent = 'Copy share link', 1500);
    });
  });
}

function applyScenario(js) {
  if (js.uiCfg) Object.assign(uiCfg, js.uiCfg);
  if (js.params) {
    params.finance = { ...params.finance, ...js.params.finance };
    params.carbon.deductions = { ...params.carbon.deductions, ...js.params.carbon?.deductions };
    if (js.params.planting) Object.assign(params.planting, js.params.planting);
    if (js.params.species) {
      params.species.vulgaris.scale = js.params.species.vulgaris?.scale ?? params.species.vulgaris.scale;
      params.species.asper.scale = js.params.species.asper?.scale ?? params.species.asper.scale;
    }
  }
  for (const p of js.drawnPolygons || []) {
    const layer = L.polygon(p.coords, { color: '#8e44ad', weight: 2, fillOpacity: 0.15 }).addTo(map.map);
    map.drawnPolys.set(p.id, { layer, areaHa: p.areaHa, mix: null });
  }
  syncInputs();
  map.setSelection(js.selectedSegments || []);
}

/* Selection encoded in the hash as compressed integer ranges: #sel=1-40,55,90-120 */
function writeHash(force = false) {
  const ids = [...map.selected].map(s => parseInt(s.slice(1))).sort((a, b) => a - b);
  if (!ids.length) { history.replaceState(null, '', location.pathname); return; }
  const ranges = [];
  let s = ids[0], e = ids[0];
  for (let i = 1; i <= ids.length; i++) {
    if (ids[i] === e + 1) { e = ids[i]; continue; }
    ranges.push(s === e ? `${s}` : `${s}-${e}`);
    s = e = ids[i];
  }
  const h = `#sel=${ranges.join(',')}`;
  if (h.length < 2000 || force) history.replaceState(null, '', h);
}

function readHash() {
  const m = location.hash.match(/#sel=([\d,-]+)/);
  if (!m) return;
  const ids = [];
  for (const part of m[1].split(',')) {
    const [a, b] = part.split('-').map(Number);
    if (b === undefined) ids.push(`s${a}`);
    else for (let i = a; i <= b; i++) ids.push(`s${i}`);
  }
  map.setSelection(ids);
}

/* ------------------------------------------------ presentation mode */
function initPresentation() {
  const exit = document.createElement('button');
  exit.id = 'btn-present-exit';
  exit.textContent = '✕ Exit presentation';
  document.getElementById('map-wrap').appendChild(exit);
  $('btn-present').addEventListener('click', () => {
    document.body.classList.add('presenting');
    map.map.invalidateSize();
  });
  exit.addEventListener('click', () => {
    document.body.classList.remove('presenting');
    map.map.invalidateSize();
  });
}

/* ------------------------------------------------ assumptions modal */
function initAssumptions() {
  $('btn-assumptions').addEventListener('click', () => {
    $('assumptions-body').innerHTML = assumptionsHtml();
    $('modal-backdrop').classList.remove('hidden');
  });
  $('btn-modal-close').addEventListener('click', () => $('modal-backdrop').classList.add('hidden'));
  $('modal-backdrop').addEventListener('click', e => {
    if (e.target === $('modal-backdrop')) $('modal-backdrop').classList.add('hidden');
  });
}

function assumptionsHtml() {
  const d = params.carbon.deductions;
  const rows = [
    ['Riparian strip width', `${uiCfg.stripWidthM} m`, MC.planting.strip_width_m.source],
    ['Rows × spacing', `${uiCfg.rows} × ${uiCfg.spacingM} m`, MC.planting.density_formula_source],
    ['Seedlings per ha (computed)', fmt(seedlingsPerHa(uiCfg.rows, uiCfg.spacingM, params.planting.mature_canopy_diameter_m)), 'Seedling Density sheet formula'],
    ['Mature canopy diameter', `${params.planting.mature_canopy_diameter_m} m`, MC.planting.mature_canopy_diameter_m.source],
    ['Survival rate', `${Math.round(uiCfg.survival * 100)} %`, MC.planting.survival_rate.source],
    ['Carbon fraction', params.carbon.carbon_fraction, MC.carbon.carbon_fraction.source],
    ['Root:shoot ratio', params.carbon.root_shoot_ratio, MC.carbon.root_shoot_ratio.source],
    ['CO₂e conversion', '44/12 = 3.667', 'Biomass!A29'],
    ['SOC accumulation', `${params.carbon.soc_tc_ha_yr} tC/ha/yr`, MC.carbon.soc_tc_ha_yr.source],
    ['Minor pools (herb/DW/litter)', params.carbon.include_minor_pools ? 'included (~4-6% of gross)' : 'excluded', 'Eq6/Eq8/Eq10 per-ha curves'],
    ['Performance benchmark', d.performance_benchmark, MC.carbon.deductions.performance_benchmark.source],
    ['Uncertainty deduction', d.uncertainty, MC.carbon.deductions.uncertainty.source],
    ['Leakage', d.leakage, MC.carbon.deductions.leakage.source],
    ['Verra buffer', d.buffer, MC.carbon.deductions.buffer.source],
    ['Net factor', `×${netFactor(d).toFixed(4)}`, 'product of the four deductions (Eq30)'],
    ['Carbon price', `$${params.finance.carbon_price_usd}/t, +${(params.finance.price_escalation * 100).toFixed(1)} %/yr`, `${MC.finance.carbon_price_usd.source} / ${MC.finance.price_escalation.source}`],
    ['Revenue start', `year ${params.finance.revenue_start_year} (validation lag)`, MC.finance.revenue_start_year.source],
    ['Discount rate', `${(params.finance.discount_rate * 100).toFixed(1)} %`, MC.finance.discount_rate.source],
    ['Crediting period', `${params.finance.project_years} years`, MC.finance.project_years.source],
    ['B. balcooa growth curve', 'years 1–20, 84 culms & 729 kg dry biomass/clump at yr 20', MC.species.balcooa.scale.source],
    ['B. vulgaris scaling', `×${params.species.vulgaris.scale}`, MC.species.vulgaris.scale.source],
    ['D. asper scaling', `×${params.species.asper.scale}`, MC.species.asper.scale.source],
    ['Canopy growth curve', 'canopy area ∝ culm count; mature 5 m diameter at yr-20 culm count', 'derived assumption (documented in README)'],
  ];
  const suit = [
    ['Weights', 'clay/texture 40 %, land cover 30 %, slope 15 %, rainfall 15 %', 'pipeline/config.json (agreed 2026-08-13)'],
    ['Soil texture', 'SoilGrids 250 m clay & sand 0–30 cm; clay ramps 10→30 %, sand penalised > 60 %. CAVEAT: SoilGrids maps the regional soil profile and CANNOT distinguish sandy braided channel beds from surrounding soils — field soil verification is mandatory before any planting commitment.', 'ISRIC SoilGrids 2.0'],
    ['Land cover', 'ESA WorldCover 2021: shrub/grass/crop/bare plantable; forest, built-up, water, wetland, mangrove excluded (VM0047 additionality)', 'ESA WorldCover v200'],
    ['Slope', 'Copernicus GLO-30; optimal 1–12°, >30° excluded', 'Copernicus DEM'],
    ['Rainfall', 'WorldClim 2.1 annual precipitation; score ramp 700–1400 mm. <700 mm = EXCLUDED as policy (semi-arid braided systems failed field trials); −30 pt graded penalty in the 700–1000 mm transitional band', 'WorldClim + SaniTap field trials'],
    ['Flood penalty', '−15 pts if slope <0.5° and elevation <10 m (kept from v1; the hard inundation exclusion below now handles the severe cases)', 'desk heuristic'],
    ['Inundation exclusion', 'EXCLUDED where >40 % of the buffer has JRC Global Surface Water occurrence >25 % (under water at least a quarter of observed time). Clumping bamboo does not establish in seasonally/permanently submerged ground. Wide open-water channels also trigger this — correctly, since the 100 m buffer holds no plantable bank there.', 'JRC GSW 2021 v1.4 (added 2026-08-14)'],
    ['Swamp/wetland exclusion', 'EXCLUDED where herbaceous wetland exceeds 50 % of the buffer. Also avoids VM0047 complications with organic wetland soils. Wetland share stays visible as an attribute on all other segments.', 'ESA WorldCover class 90 (added 2026-08-14)'],
    ['Brackish flag', 'FLAG (not exclusion): within 3 km of the coast and below 5 m elevation, or inside the Pangalanes canal corridor (18.1–22.9°S within 5 km of the coast) — field-check salinity before planting.', 'Natural Earth coastline + GLO-30'],
    ['Micro-drainage limitation', 'Fine-scale drainage — small waterlogged pockets within otherwise-good banks — is below the resolution of ALL desk data used here (10–30 m). Planting teams judge micro-siting in the field.', 'honest limitation'],
    ['Classes', 'RELATIVE percentiles of non-excluded segments: top 25 % high, middle 50 % medium, bottom 25 % low. The 0–100 score is absolute and shown in every popup/export.', 'agreed 2026-08-13'],
    ['Trial benchmark', `score ≥ ${benchmarkMeta?.score ?? 79} — the median suitability score of the Efaho reach where SaniTap's 2026 field trials succeeded. An absolute field-truth anchor, independent of the relative classes; ${fmt(benchmarkMeta?.total_km ?? 0)} km of bank meets it.`, 'SaniTap field trials + pipeline'],
    ['Species suggestion', 'asper ↑ on wettest clay-rich sites (≥2200 mm); vulgaris ↑ on drier/lower-grade sites', 'provisional desk rule'],
  ];
  const layers = [
    ['Population', 'WorldPop 2020 constrained 100 m, summed within 2/5 km of segment midpoint (500 m grid convolution). Community labour pool & CCB beneficiary indicator.', 'WorldPop (maxar_v1)'],
    ['Protected areas', 'Distance to nearest WDPA polygon + name/designation. LICENCE: only these derived values are shipped — WDPA geometries are not redistributed.', 'UNEP-WCMC/IUCN Protected Planet'],
    ['Forest blocks', 'Distance to nearest block of ≥100 ha with ≥50 % tree cover (100 m grid from WorldCover class 10). Fuelwood-substitution / pressure-relief indicator.', 'ESA WorldCover derived'],
    ['Access', 'Road-adjacent = OSM road (trunk→track) within 250 m. Boat-reachable = downstream of an access point until river-line gradient > 1.5 % (rapids proxy) or the semi-arid boundary. DESK HEURISTIC: DEM noise, dams and weirs not captured — needs local confirmation.', 'OSM/Geofabrik + GLO-30'],
    ['Land use & paddy', 'WorldCover composition of each buffer. Likely-paddy: cropland in near-flat (<2°) buffers that touch water/wetland — toggleable area deduction, heuristic only.', 'ESA WorldCover derived'],
    ['Fire pressure', 'MODIS active-fire detections (FIRMS) 2001–2024 within 1 km, scaled to per-decade; flag ≥5. SUBSTITUTION: MCD64A1 burned-area requires NASA Earthdata auth; active fire is the same tavy-pressure signal at coarser confidence.', 'NASA FIRMS'],
    ['Cyclone exposure', 'IBTrACS storms passing within 100 km since 1986; max Saffir-Simpson category among in-radius points. DUAL-USE: high exposure = highest erosion-protection value AND elevated establishment risk in years 1–3.', 'NOAA IBTrACS v04r01'],
    ['Cold-marginal', `BIO6 (coldest-month min temp) < 10 °C flagged as CAUTION only — the high-scoring inland cluster (upper Mangoro/Alaotra, ~900–1400 m) may be cold-marginal for tropical clumping bamboo. Species cold-tolerance data is thin; needs field/literature check before any exclusion.`, 'WorldClim 2.1 BIO6'],
  ];
  const tbl = rows => rows.map(r => `<tr><td>${r[0]}</td><td><b>${r[1]}</b></td><td>${r[2]}</td></tr>`).join('');
  return `
    <div class="disclaimer"><b>Disclaimer:</b> suitability scores are desk-based estimates from global datasets
    (250 m soil, 30 m terrain, 10 m land cover) and <b>require field verification</b> before operational planting
    decisions. Carbon figures are ex-ante estimates under VM0047 with placeholder performance benchmark and
    uncertainty values; they are not verified credits.</div>
    <h3>Model parameters (defaults from ${MC._generated_from})</h3>
    <table><tr><th>Parameter</th><th>Current value</th><th>Source</th></tr>${tbl(rows)}</table>
    <h3>Suitability scoring</h3>
    <table><tr><th>Component</th><th>Rule</th><th>Source</th></tr>${tbl(suit)}</table>
    <h3>Decision-support layers (context only — never change the suitability score)</h3>
    <table><tr><th>Layer</th><th>Method & caveats</th><th>Source</th></tr>${tbl(layers)}</table>`;
}

init();
