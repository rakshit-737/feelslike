/* dashboard/history.js — the History tab: the generated historical dataset.
 *
 * Every value here is SIMULATED HISTORY (backend/dataset, stored in SQLite) — never
 * live, real or hardware; the banner and every chart badge say so. Binds to the
 * window.FL shell; charts reuse window.FLChart from monitor.js (a scatter plot is
 * drawn here, the one chart type the primitive lacks).
 *
 *   GET /api/history/catalog      buildings -> floors -> zones, span, pairs
 *   GET /api/history              series (building | floor | zone), downsampled server-side
 *   GET /api/history/compare      named relationships with Pearson r; current vs historical demand
 *   GET /api/history/anomalies    anomaly metadata in range
 *   GET /api/history/quality      RAW vs CLEAN layers for one zone
 *   GET /api/history/export       CSV / JSON download
 * Axis times are SITE wall-clock times of the dataset, not the live sim clock.
 */
(function () {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function num(v, d) { return isNum(v) ? v.toLocaleString('en-IN', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }) : '—'; }
  function iso(t) { return new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' '); }
  function ticks(xmin, xmax) {
    var span = xmax - xmin, step = span <= 7200 ? 900 : span <= 6 * 3600 + 60 ? 3600 : span <= 86400 + 60 ? 4 * 3600 : span <= 8 * 86400 ? 86400 : 5 * 86400, out = [];
    for (var t = Math.ceil(xmin / step) * step; t <= xmax; t += step) {
      var d = new Date(t * 1000);
      out.push({ x: t, label: step >= 86400 ? (d.getUTCDate() + '/' + (d.getUTCMonth() + 1)) : String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0') });
    }
    return out;
  }

  var RANGES = ['1h', '6h', '24h', '7d', '30d'];
  var PAIR_LABEL = {
    indoor_vs_outdoor_temperature: 'Indoor vs outdoor temperature', occupancy_vs_energy: 'Occupancy vs energy',
    occupancy_vs_co2: 'Occupancy vs CO₂', outdoor_temperature_vs_hvac_load: 'Outdoor temperature vs HVAC load',
    hvac_load_vs_energy: 'HVAC load vs energy', comfort_vs_energy: 'Comfort vs energy',
    cloud_cover_vs_solar: 'Cloud cover vs solar (10–15 h)', solar_vs_renewable: 'Solar vs PV generation (10–15 h)',
    current_vs_historical_demand: 'Current vs historical demand'
  };
  var C = { a: '#2a78d6', b: '#c2410c', c: '#0d9488', d: '#7c5cd6', e: '#898781' };
  // title, unit, series [key, label, color, dashed], opts, data state
  var CHARTS = [
    ['Temperature', '°C', [['indoor_temperature_c', 'Indoor', C.a], ['outdoor_temperature_c', 'Outdoor', C.b, true]], {}, 'SIMULATED'],
    ['Humidity', '%', [['indoor_humidity_percent', 'Indoor', C.a], ['outdoor_humidity_percent', 'Outdoor', C.b, true]], { yMin: 0 }, 'SIMULATED'],
    ['CO₂', 'ppm', [['indoor_co2_ppm', 'Indoor (mass balance)', C.c]], {}, 'SIMULATED'],
    ['Occupancy', 'people', [['occupancy_count', 'Actual', C.a], ['expected_occupancy', 'Expected (profile)', C.d, true]], { yMin: 0 }, 'SIMULATED'],
    ['HVAC load', 'kW', [['hvac_power_kw', 'Compressor / heat pump', C.a], ['ventilation_power_kw', 'Fans', C.c], ['cooling_demand_kw', 'Cooling (thermal)', C.b, true]], { yMin: 0 }, 'SIMULATED'],
    ['Energy per interval', 'kWh', [['energy_consumption_kwh', 'Total', C.a], ['hvac_energy_kwh', 'HVAC', C.b], ['lighting_energy_kwh', 'Lighting', C.d]], { yMin: 0 }, 'SIMULATED'],
    ['Demand', 'kW', [['total_power_kw', 'Total (mean)', C.a], ['total_power_kw_max', 'Peak in interval', C.b], ['forecast_demand_kw', 'Forecast (seasonal naive)', C.d, true], ['renewable_power_kw', 'PV', C.c]], { yMin: 0 }, 'DERIVED · PREDICTED'],
    ['Comfort', '/100', [['comfort_score', 'Comfort score', C.a], ['temperature_comfort_score', 'Temperature', C.b, true], ['co2_comfort_score', 'CO₂', C.c, true]], { yMin: 0, band: [70, 100] }, 'DERIVED'],
    ['Outdoor conditions', '°C · W/m²÷100', [['outdoor_temperature_c', 'Outdoor °C', C.b], ['solar_hundreds', 'Solar W/m² ÷100', C.d], ['cloud_tens', 'Cloud % ÷10', C.e, true]], { yMin: 0 }, 'SIMULATED']
  ];

  function historyPanel() {
    var FL = window.FL, root, tip, cat = null;
    var sel = { building_id: null, level: 'building', floor_id: '', zone_id: '', range: '24h', pair: 'occupancy_vs_co2' };
    var data = null, cmp = null, qual = null, busy = 0;

    function el(s) { return root.querySelector(s); }
    function status(m, bad) { var n = el('.hs-status'); n.textContent = m; n.classList.toggle('bad', !!bad); }
    function err(e) { var d = e && e.body && e.body.detail; return d ? (typeof d === 'string' ? d : (d.hint || JSON.stringify(d))) : (e && e.message) || String(e); }
    function qs(extra) {
      var p = ['building_id=' + encodeURIComponent(sel.building_id), 'range=' + sel.range];
      if (sel.level === 'floor') p.push('level=floor', 'floor_id=' + encodeURIComponent(sel.floor_id));
      if (sel.level === 'zone') p.push('level=zone', 'zone_id=' + encodeURIComponent(sel.zone_id));
      return p.concat(extra || []).join('&');
    }
    function bld() { return cat.buildings.filter(function (b) { return b.building_id === sel.building_id; })[0]; }

    function mount(node) {
      root = node;
      root.innerHTML =
        '<div class="hs">' +
        '<div class="hs-banner" role="note"><b>SIMULATED HISTORY</b> — a generated dataset (backend/dataset: seasonal weather → occupancy → the sim/twin.py thermal model → HVAC → comfort → energy). Not live, not real, not hardware. Times are the dataset\'s site clock.</div>' +
        '<div class="bd-bar hs-bar" role="group" aria-label="History filters">' +
          '<label>Building <select class="hs-b"></select></label>' +
          '<label>Level <select class="hs-l"><option value="building">Building</option><option value="floor">Floor</option><option value="zone">Zone</option></select></label>' +
          '<label>Floor <select class="hs-f" disabled></select></label>' +
          '<label>Zone <select class="hs-z" disabled></select></label>' +
          '<div class="bd-seg hs-r" role="group" aria-label="Time range">' + RANGES.map(function (r) { return '<button type="button" data-r="' + r + '" class="' + (r === sel.range ? 'on' : '') + '">' + r + '</button>'; }).join('') + '</div>' +
          '<span class="bd-status hs-status" role="status" aria-live="polite">loading catalog…</span>' +
        '</div>' +
        '<div class="hs-meta bd-muted bd-small"></div>' +
        '<div class="hs-charts"></div>' +
        '<div class="bd-main">' +
          '<section class="card"><h2>Comparison <span class="right"><select class="hs-pair" aria-label="Comparison"></select></span></h2><div class="hs-cmp"></div></section>' +
          '<section class="card"><h2>Anomalies in range <span class="right hs-an-n"></span></h2><div class="hs-an"></div></section>' +
        '</div>' +
        '<section class="card"><h2>Data quality · raw vs clean <span class="right bd-muted hs-q-zone"></span></h2><div class="hs-q"></div></section>' +
        '<p class="bd-foot hs-export"></p>' +
        '</div>';
      if (window.FLChart) { tip = window.FLChart.tipFor(root); root.classList.add('mn-rel'); }
      el('.hs-b').onchange = function () { sel.building_id = this.value; fillFloorsZones(); load(); };
      el('.hs-l').onchange = function () { sel.level = this.value; fillFloorsZones(); load(); };
      el('.hs-f').onchange = function () { sel.floor_id = this.value; load(); };
      el('.hs-z').onchange = function () { sel.zone_id = this.value; load(); };
      el('.hs-r').onclick = function (e) {
        var b = e.target.closest('button[data-r]'); if (!b) return;
        sel.range = b.dataset.r; this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); load();
      };
      el('.hs-pair').onchange = function () { sel.pair = this.value; loadCompare(); };
      FL.get('/api/history/catalog').then(function (c) {
        cat = c;
        if (!c.available) { status('No dataset yet — run: python -m scripts.generate_dataset', true); return; }
        el('.hs-b').innerHTML = c.buildings.map(function (b) { return '<option value="' + esc(b.building_id) + '">' + esc(b.building_name) + ' (' + esc(b.building_type) + ')</option>'; }).join('');
        el('.hs-pair').innerHTML = c.pairs.map(function (p) { return '<option value="' + esc(p) + '"' + (p === sel.pair ? ' selected' : '') + '>' + esc(PAIR_LABEL[p] || p) + '</option>'; }).join('');
        sel.building_id = c.buildings[0].building_id;
        el('.hs-meta').textContent = 'Dataset ' + c.start + ' → ' + c.end + ' · ' + num(c.timestamps) + ' timestamps · ' + c.buildings.length + ' buildings · ' +
          num(c.row_counts.zone_obs) + ' zone rows · ' + num(c.row_counts.raw_obs) + ' raw sensor rows · ' + c.row_counts.anomalies + ' anomalies · seed ' + esc(c.meta.config && c.meta.config.seed);
        fillFloorsZones(); load();
      }, function (e) { status('catalog failed: ' + err(e), true); });
    }

    function fillFloorsZones() {
      var b = bld(); if (!b) return;
      var f = el('.hs-f'), z = el('.hs-z');
      f.innerHTML = b.floor_ids.map(function (x) { return '<option>' + esc(x) + '</option>'; }).join('');
      if (b.floor_ids.indexOf(sel.floor_id) < 0) sel.floor_id = b.floor_ids[0];
      f.value = sel.floor_id;
      z.innerHTML = b.zone_list.map(function (x) { return '<option value="' + esc(x.zone_id) + '">' + esc(x.zone_id + ' · ' + x.zone_role) + '</option>'; }).join('');
      if (!b.zone_list.some(function (x) { return x.zone_id === sel.zone_id; })) sel.zone_id = b.zone_list[0].zone_id;
      z.value = sel.zone_id;
      f.disabled = sel.level !== 'floor'; z.disabled = sel.level !== 'zone';
    }

    function load() {
      if (!cat || !cat.available) return;
      var my = ++busy;
      status('loading…');
      var zoneForQ = sel.level === 'zone' ? sel.zone_id : bld().zone_list[0].zone_id;
      Promise.all([
        FL.get('/api/history?' + qs()),
        FL.get('/api/history/anomalies?building_id=' + encodeURIComponent(sel.building_id)),
        FL.get('/api/history/quality?building_id=' + encodeURIComponent(sel.building_id) + '&zone_id=' + encodeURIComponent(zoneForQ) + '&range=' + (sel.range === '30d' || sel.range === '7d' ? '24h' : sel.range))
      ]).then(function (r) {
        if (my !== busy) return;
        data = r[0]; qual = r[2];
        renderCharts(); renderAnomalies(r[1].anomalies); renderQuality(zoneForQ); renderExport();
        status(data.points.length + ' points · ' + (data.interval_s >= 3600 ? data.interval_s / 3600 + ' h' : data.interval_s / 60 + ' min') + ' buckets' + (data.interval_adjusted ? ' (coarsened to stay under 1000 points)' : '') + ' · ' + data.start + ' → ' + data.end);
      }, function (e) { if (my === busy) status('load failed: ' + err(e), true); });
      loadCompare();
    }
    function loadCompare() {
      if (!cat || !cat.available) return;
      FL.get('/api/history/compare?pair=' + sel.pair + '&' + qs().replace('range=' + sel.range, 'range=' + (sel.range === '1h' || sel.range === '6h' ? '24h' : sel.range)))
        .then(function (c) { cmp = c; renderCompare(); }, function (e) { el('.hs-cmp').innerHTML = '<p class="bd-err">' + esc(err(e)) + '</p>'; });
    }

    function renderCharts() {
      var lib = window.FLChart, box = el('.hs-charts'), P = data.points;
      if (!lib) { box.innerHTML = '<p class="bd-foot">Charts need /static/monitor.js.</p>'; return; }
      P.forEach(function (p) { p.solar_hundreds = isNum(p.solar_irradiance_w_m2) ? p.solar_irradiance_w_m2 / 100 : null; p.cloud_tens = isNum(p.cloud_cover_percent) ? p.cloud_cover_percent / 10 : null; });
      var built = CHARTS.map(function (d) {
        var series = d[2].filter(function (s) { return P.some(function (p) { return isNum(p[s[0]]); }); })
          .map(function (s) { return { name: s[1], color: s[2], dash: !!s[3], unit: d[1], dec: d[1] === 'kWh' ? 3 : 1, pts: P.map(function (p) { return { x: p.t, y: p[s[0]] }; }) }; });
        var o = { series: series, h: 170, xTicks: ticks, empty: 'No data for this filter.' };
        Object.keys(d[3]).forEach(function (k) { o[k] = d[3][k]; });
        return { d: d, series: series, ch: lib.lineChart(o) };
      });
      box.innerHTML = built.map(function (b, i) {
        return '<div class="bd-mini" data-i="' + i + '"><div class="bd-kpi-h"><span>' + esc(b.d[0]) + ' <small class="bd-muted">' + esc(b.d[1]) + '</small></span><span class="bd-src bd-src-sim">' + esc(b.d[4]) + '</span></div>' +
          '<div class="bd-legend-row">' + b.series.map(function (s) { return '<span><span class="bd-sw' + (s.dash ? ' dash' : '') + '" style="border-color:' + s.color + '"></span>' + esc(s.name) + '</span>'; }).join('') + '</div>' + b.ch.svg + '</div>';
      }).join('');
      if (tip) built.forEach(function (b, i) { var s = box.querySelector('.bd-mini[data-i="' + i + '"] svg'); if (s) lib.bindHover(s, b.ch, tip, iso); });
    }

    function renderCompare() {
      var box = el('.hs-cmp');
      if (cmp.pair === 'current_vs_historical_demand') {
        var lib = window.FLChart, H = cmp.hours;
        if (!lib) return;
        var series = [
          { name: 'Current day ' + (cmp.current_date || ''), color: C.a, unit: 'kW', dec: 1, pts: H.map(function (h) { return { x: h.hour, y: h.current_kw }; }) },
          { name: 'Historical mean', color: C.d, unit: 'kW', dec: 1, dash: true, pts: H.map(function (h) { return { x: h.hour, y: h.historical_mean_kw }; }) },
          { name: 'Historical p90', color: C.e, unit: 'kW', dec: 1, dash: true, opacity: 0.6, pts: H.map(function (h) { return { x: h.hour, y: h.historical_p90_kw }; }) },
          { name: 'Historical p10', color: C.e, unit: 'kW', dec: 1, dash: true, opacity: 0.6, pts: H.map(function (h) { return { x: h.hour, y: h.historical_p10_kw }; }) }
        ];
        var ch = lib.lineChart({ series: series, h: 220, yMin: 0, xTicks: function () { return [0, 6, 12, 18, 23].map(function (h) { return { x: h, label: h + ':00' }; }); } });
        box.innerHTML = '<div class="bd-legend-row">' + series.map(function (s) { return '<span><span class="bd-sw' + (s.dash ? ' dash' : '') + '" style="border-color:' + s.color + '"></span>' + esc(s.name) + '</span>'; }).join('') + '</div>' + ch.svg +
          '<p class="bd-foot">Grid demand by hour of day: the latest dataset day against every earlier day in the range. “Current” is the most recent generated day — not live.</p>';
        var svg = box.querySelector('svg'); if (svg && tip) lib.bindHover(svg, ch, tip, function (x) { return x + ':00'; });
        return;
      }
      var P = cmp.points.filter(function (p) { return isNum(p.x) && isNum(p.y); });
      if (!P.length) { box.innerHTML = '<p class="bd-foot">No data.</p>'; return; }
      var W = 520, Hh = 240, L = 48, R = 10, T = 10, B = 30, pw = W - L - R, ph = Hh - T - B;
      var xs = P.map(function (p) { return p.x; }), ys = P.map(function (p) { return p.y; });
      var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
      if (x1 - x0 < 1e-9) x1 = x0 + 1; if (y1 - y0 < 1e-9) y1 = y0 + 1;
      var X = function (v) { return L + pw * (v - x0) / (x1 - x0); }, Y = function (v) { return T + ph * (1 - (v - y0) / (y1 - y0)); };
      var s = '';
      for (var g = 0; g <= 4; g++) {
        var yy = T + ph * g / 4, xx = L + pw * g / 4;
        s += '<line x1="' + L + '" y1="' + yy + '" x2="' + (L + pw) + '" y2="' + yy + '" stroke="var(--grid)"/><text x="' + (L - 5) + '" y="' + (yy + 4) + '" font-size="10" text-anchor="end" fill="var(--muted)">' + num(y1 - (y1 - y0) * g / 4, 1) + '</text>' +
          '<text x="' + xx + '" y="' + (Hh - 12) + '" font-size="10" text-anchor="middle" fill="var(--muted)">' + num(x0 + (x1 - x0) * g / 4, 1) + '</text>';
      }
      s += P.map(function (p) { return '<circle cx="' + X(p.x).toFixed(1) + '" cy="' + Y(p.y).toFixed(1) + '" r="2.6" fill="' + C.a + '" fill-opacity="0.5"><title>' + esc(iso(p.t) + ' · ' + num(p.x, 2) + ', ' + num(p.y, 2)) + '</title></circle>'; }).join('');
      var r = cmp.r, strength = !isNum(r) ? 'not enough data' : Math.abs(r) >= 0.7 ? 'strong' : Math.abs(r) >= 0.4 ? 'moderate' : 'weak';
      box.innerHTML = '<div class="hs-r-line"><b>Pearson r = ' + (isNum(r) ? r.toFixed(2) : '—') + '</b> (' + strength + (isNum(r) ? (r >= 0 ? ', positive' : ', negative') : '') + ') · n = ' + cmp.n + ' buckets · ' + esc(cmp.x) + ' → ' + esc(cmp.y) + '</div>' +
        '<svg viewBox="0 0 ' + W + ' ' + Hh + '" width="100%" role="img" aria-label="' + esc(PAIR_LABEL[cmp.pair] || cmp.pair) + ' scatter">' + s +
        '<text x="' + (L + pw / 2) + '" y="' + (Hh - 1) + '" font-size="10.5" text-anchor="middle" fill="var(--ink2)">' + esc(cmp.x) + '</text></svg>' +
        '<p class="bd-foot">Computed from the generated dataset at the selected level (' + esc(cmp.level) + '). Relationships emerge from the simulation; nothing is drawn to order.</p>';
    }

    function renderAnomalies(list) {
      var inRange = list.filter(function (a) { return a.end_time >= data.start.replace(' ', 'T') && a.start_time < data.end.replace(' ', 'T'); });
      el('.hs-an-n').textContent = inRange.length + ' in range · ' + list.length + ' for this building';
      el('.hs-an').innerHTML = list.length ? '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>ID</th><th>Type</th><th>Severity</th><th>Window</th><th>Zone</th><th>Description</th></tr></thead><tbody>' +
        list.slice(0, 40).map(function (a) {
          var on = inRange.indexOf(a) >= 0;
          return '<tr' + (on ? ' class="flagged"' : '') + '><td>' + esc(a.anomaly_id) + '</td><td>' + esc(a.anomaly_type) + '<div class="bd-muted bd-small">' + esc(a.layer) + '</div></td><td>' + esc(a.severity) + '</td><td>' + esc(a.start_time.replace('T', ' ')) + ' → ' + esc(a.end_time.slice(11)) + '</td><td>' + esc(a.affected_zone) + '</td><td>' + esc(a.description) + '</td></tr>';
        }).join('') + '</tbody></table></div>' : '<p class="bd-muted">No anomalies were injected for this building.</p>';
    }

    function renderQuality(zone) {
      el('.hs-q-zone').textContent = zone + ' · ' + (qual.start || '') + ' → ' + (qual.end || '');
      var lib = window.FLChart, P = qual.points;
      if (!lib) return;
      var ch = lib.lineChart({ h: 170, xTicks: ticks, series: [
        { name: 'Raw sensor', color: C.b, unit: '°C', dec: 2, pts: P.map(function (p) { return { x: p.t, y: p.temperature_raw_c }; }) },
        { name: 'Clean', color: C.a, unit: '°C', dec: 2, pts: P.map(function (p) { return { x: p.t, y: p.temperature_clean_c }; }) }] });
      var counts = Object.keys(qual.flag_counts).sort().map(function (k) { return '<span class="bd-flag">' + esc(k.replace('_quality', '')) + ' ' + qual.flag_counts[k] + '</span>'; }).join('');
      el('.hs-q').innerHTML = '<div class="bd-legend-row"><span><span class="bd-sw" style="border-color:' + C.b + '"></span>Raw temperature (noise, drift, gaps, outliers, sensor faults)</span><span><span class="bd-sw" style="border-color:' + C.a + '"></span>Clean (validated, short gaps interpolated)</span></div>' + ch.svg + '<div class="bd-flags">' + counts + '</div>' +
        '<p class="bd-foot">Raw is never overwritten. Drift cannot be removed without a reference sensor and is left in (documented).</p>';
      var svg = el('.hs-q svg'); if (svg && tip) lib.bindHover(svg, ch, tip, iso);
    }

    function renderExport() {
      var z = sel.level === 'zone' ? '&zone_id=' + encodeURIComponent(sel.zone_id) : '';
      var base = '/api/history/export?building_id=' + encodeURIComponent(sel.building_id) + '&range=' + sel.range + z;
      el('.hs-export').innerHTML = 'Export this range (max 50 000 rows): ' +
        [['zone', 'zone rows'], ['building', 'building rows'], ['raw', 'raw sensors'], ['clean', 'clean sensors']].map(function (l) {
          return '<a href="' + base + '&layer=' + l[0] + '&format=csv">' + l[1] + ' CSV</a> · <a href="' + base + '&layer=' + l[0] + '&format=json" target="_blank" rel="noopener">JSON</a>';
        }).join(' | ');
    }

    return { mount: mount, update: function () {} };
  }

  function injectCSS() {
    if (document.getElementById('hs-css')) return;
    var st = document.createElement('style'); st.id = 'hs-css';
    st.textContent = '.hs{display:flex;flex-direction:column;gap:12px;}' +
      '.hs-banner{font-size:12.5px;color:var(--ink2);background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--warn);border-radius:8px;padding:8px 11px;}' +
      '.hs-bar .bd-seg{align-self:flex-end;}.hs-bar select:disabled{opacity:.5;}' +
      '.hs-charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:8px;}' +
      '.hs-r-line{font-size:13px;margin-bottom:4px;}.hs-export a{color:var(--us);}' +
      '.hs .card h2 select{font:inherit;font-size:12px;border:1px solid var(--border);border-radius:6px;padding:2px 5px;background:var(--page);}';
    document.head.appendChild(st);
  }

  var booted = false;
  function boot() {
    if (booted) return true;
    var FL = window.FL; if (!FL || typeof FL.registerPanel !== 'function') return false;
    booted = true; injectCSS();
    FL.registerPanel('history', historyPanel());
    return true;
  }
  if (!boot()) document.addEventListener('DOMContentLoaded', boot);
})();
