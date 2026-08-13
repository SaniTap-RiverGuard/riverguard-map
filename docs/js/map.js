/* Map setup, suitability layer rendering, selection interactions. */

const CLASS_COLORS = { h: '#1a9850', m: '#fdae61', l: '#9e9e9e', e: '#d9d9d9' };
const SELECTED_COLOR = '#00e5ff';
const WC_NAMES = {
  10: 'Tree cover', 20: 'Shrubland', 30: 'Grassland', 40: 'Cropland', 50: 'Built-up',
  60: 'Bare/sparse', 70: 'Snow/ice', 80: 'Water', 90: 'Herb. wetland', 95: 'Mangroves', 100: 'Moss'
};

export class SuitabilityMap {
  /* callbacks: {onSelectionChange(), onPolygonsChange(), popupHtml(props)} */
  constructor(el, callbacks) {
    this.cb = callbacks;
    this.selected = new Set();
    this.drawnPolys = new Map(); // id -> {layer, areaHa, mix:null}
    this.features = [];          // raw GeoJSON features
    this.layerById = new Map();
    this.year = 0;
    this.canopyFraction = 0;
    this._drawnSeq = 0;

    this.map = L.map(el, { preferCanvas: true, zoomControl: true })
      .setView([-19.5, 48.3], 6);
    this.renderer = L.canvas({ padding: 0.3, tolerance: 6 });

    const esri = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      { attribution: 'Imagery © Esri & partners', maxZoom: 18 });
    const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      { attribution: '© OpenStreetMap contributors', maxZoom: 19 });
    esri.addTo(this.map);
    L.control.layers({ 'Satellite (Esri)': esri, 'OpenStreetMap': osm }, {},
      { position: 'bottomright' }).addTo(this.map);
    L.control.scale({ imperial: false }).addTo(this.map);

    this._initDraw();
    this._initBoxSelect();
  }

  loadSegments(geojson) {
    this.features = geojson.features;
    this.segLayer = L.geoJSON(geojson, {
      renderer: this.renderer,
      style: f => this._style(f),
      onEachFeature: (f, layer) => {
        this.layerById.set(f.properties.id, layer);
        layer.on('click', ev => {
          L.DomEvent.stop(ev);
          if (f.properties.c === 'e') {
            layer.bindPopup(this.cb.popupHtml(f.properties)).openPopup(ev.latlng);
            return;
          }
          this.toggle(f.properties.id);
          layer.bindPopup(this.cb.popupHtml(f.properties)).openPopup(ev.latlng);
        });
      },
    }).addTo(this.map);
  }

  _style(f) {
    const p = f.properties;
    const sel = this.selected.has(p.id);
    if (sel) {
      // Year visualisation: thicken and deepen green with canopy closure
      const t = this.canopyFraction; // 0..1
      const col = t === 0 ? SELECTED_COLOR : lerpColor('#7ddf9a', '#0a5c2e', t);
      return { color: col, weight: 3 + 5 * t, opacity: 0.95 };
    }
    return {
      color: CLASS_COLORS[p.c] || '#888',
      weight: p.c === 'h' ? 2.2 : p.c === 'm' ? 1.8 : 1.2,
      opacity: p.c === 'e' ? 0.5 : 0.85,
    };
  }

  restyle(ids) {
    const list = ids ? ids.map(i => this.layerById.get(i)).filter(Boolean)
      : this.segLayer ? Object.values(this.segLayer._layers) : [];
    for (const l of list) l.setStyle(this._style(l.feature));
  }

  setYearVisual(canopyFraction) {
    this.canopyFraction = canopyFraction;
    this.restyle([...this.selected]);
  }

  toggle(id) {
    if (this.selected.has(id)) this.selected.delete(id); else this.selected.add(id);
    this.restyle([id]);
    this.cb.onSelectionChange();
  }

  setSelection(ids) {
    const prev = [...this.selected];
    this.selected = new Set(ids);
    this.restyle([...new Set([...prev, ...ids])]);
    this.cb.onSelectionChange();
  }

  clearSelection() {
    const prev = [...this.selected];
    this.selected.clear();
    this.restyle(prev);
    for (const [, d] of this.drawnPolys) this.map.removeLayer(d.layer);
    this.drawnPolys.clear();
    this.cb.onPolygonsChange();
    this.cb.onSelectionChange();
  }

  selectedProps() {
    return this.features.filter(f => this.selected.has(f.properties.id)).map(f => f.properties);
  }

  selectVisibleHigh() {
    const b = this.map.getBounds();
    const add = [];
    for (const f of this.features) {
      const p = f.properties;
      if (p.c !== 'h') continue;
      const [x, y] = midpoint(f);
      if (b.contains([y, x])) add.push(p.id);
    }
    this.setSelection([...this.selected, ...add]);
  }

  /* Shift-drag box select (replaces Leaflet's shift-drag box zoom). */
  _initBoxSelect() {
    this.map.boxZoom.disable();
    const el = this.map.getContainer();
    let start = null, rect = null;
    el.addEventListener('mousedown', e => {
      if (!e.shiftKey || e.button !== 0) return;
      start = this.map.mouseEventToLatLng(e);
      this.map.dragging.disable();
      e.preventDefault();
    });
    el.addEventListener('mousemove', e => {
      if (!start) return;
      const cur = this.map.mouseEventToLatLng(e);
      const bounds = L.latLngBounds(start, cur);
      if (!rect) rect = L.rectangle(bounds, { color: SELECTED_COLOR, weight: 1, fillOpacity: 0.08 }).addTo(this.map);
      else rect.setBounds(bounds);
    });
    window.addEventListener('mouseup', e => {
      if (!start) return;
      const cur = this.map.mouseEventToLatLng(e);
      const bounds = L.latLngBounds(start, cur);
      if (rect) { this.map.removeLayer(rect); rect = null; }
      this.map.dragging.enable();
      const add = [];
      for (const f of this.features) {
        const p = f.properties;
        if (p.c === 'e') continue;
        const [x, y] = midpoint(f);
        if (bounds.contains([y, x])) add.push(p.id);
      }
      start = null;
      if (add.length) this.setSelection([...this.selected, ...add]);
    });
  }

  /* Free polygon drawing for areas the pipeline missed. */
  _initDraw() {
    this.map.pm.addControls({
      position: 'topleft',
      drawPolygon: true, drawMarker: false, drawCircleMarker: false, drawPolyline: false,
      drawRectangle: false, drawCircle: false, drawText: false,
      editMode: true, dragMode: false, cutPolygon: false, removalMode: true, rotateMode: false,
    });
    this.map.on('pm:create', e => {
      const layer = e.layer;
      const id = `d${++this._drawnSeq}`;
      layer.setStyle({ color: '#8e44ad', weight: 2, fillOpacity: 0.15 });
      const update = () => {
        const d = this.drawnPolys.get(id);
        if (d) { d.areaHa = polygonAreaHa(layer); this.cb.onPolygonsChange(); }
      };
      this.drawnPolys.set(id, { layer, areaHa: polygonAreaHa(layer), mix: null });
      layer.on('pm:edit', update);
      layer.on('pm:remove', () => { this.drawnPolys.delete(id); this.cb.onPolygonsChange(); });
      layer.bindPopup(() => {
        const d = this.drawnPolys.get(id);
        return `<b>Drawn area ${id}</b><br>${d ? d.areaHa.toFixed(1) : '?'} ha<br>` +
          `<span style="font-size:11px">Uses global species mix. Attributes not sampled — field data needed.</span>`;
      });
      this.cb.onPolygonsChange();
    });
  }

  removeDrawn(id) {
    const d = this.drawnPolys.get(id);
    if (d) { this.map.removeLayer(d.layer); this.drawnPolys.delete(id); this.cb.onPolygonsChange(); }
  }

  flyTo(lat, lon, zoom = 12) { this.map.flyTo([lat, lon], zoom, { duration: 1.2 }); }
}

function midpoint(f) {
  const c = f.geometry.coordinates;
  return c[Math.floor(c.length / 2)];
}

/* Spherical polygon area (ha) — shoelace on an equirectangular projection at
 * the polygon's mean latitude; fine for planting-scale polygons. */
function polygonAreaHa(layer) {
  const ring = layer.getLatLngs()[0];
  if (!ring || ring.length < 3) return 0;
  const lat0 = ring.reduce((s, p) => s + p.lat, 0) / ring.length;
  const mPerDegX = 111320 * Math.cos(lat0 * Math.PI / 180), mPerDegY = 111132;
  let area = 0;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i], b = ring[(i + 1) % ring.length];
    area += (a.lng * mPerDegX) * (b.lat * mPerDegY) - (b.lng * mPerDegX) * (a.lat * mPerDegY);
  }
  return Math.abs(area) / 2 / 10000;
}

function lerpColor(a, b, t) {
  const pa = [1, 3, 5].map(i => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map(i => parseInt(b.slice(i, i + 2), 16));
  return '#' + pa.map((v, i) => Math.round(v + (pb[i] - v) * t).toString(16).padStart(2, '0')).join('');
}

const EXCL_REASONS = {
  'slope': 'cliff slope > 30°',
  'landcover': '< 20 % plantable land (forest/built/water)',
  'semi-arid': 'semi-arid braided system — failed field trials',
};

export function popupHtml(p) {
  const cls = { h: 'High', m: 'Medium', l: 'Low', e: 'Excluded' }[p.c];
  const rows = [
    ['Suitability', `${p.s} / 100 (${cls}${p.x ? ' — ' + (EXCL_REASONS[p.x] || p.x) : ''})`],
    ['Bank length', `${p.L} m`],
    ['Clay 0–30 cm', `${p.cy} %`],
    ['Sand 0–30 cm', `${p.sd} %`],
    ['Slope', `${p.sl}°`],
    ['Elevation', `${p.el} m`],
    ['Rainfall', `${p.rn} mm/yr`],
    ['Plantable land', `${Math.round(p.lf * 100)} % (${WC_NAMES[p.ld] || p.ld})`],
  ];
  return `<b>Segment ${p.id}</b><table class="popup-table">` +
    rows.map(([k, v]) => `<tr><td>${k}</td><td><b>${v}</b></td></tr>`).join('') +
    `</table><div class="popup-mix">Suggested mix: balcooa ${p.mb} / vulgaris ${p.mv} / asper ${p.ma} %</div>`;
}
