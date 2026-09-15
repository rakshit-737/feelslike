/* dashboard/monitor.js — the Monitor tab: a building-monitoring console.
 *
 * Binds to the same window.FL contract as panels.js and lives in its own file
 * so the pinned, single-owner panels.js is untouched. Zero dependencies, no
 * build step, no CDN — hand-rolled SVG, tokens from the shell.
 *
 * WHAT IT SHOWS AND WHERE EACH NUMBER COMES FROM (rule: no value without a
 * server-side source; the source badge on every card/chart is not decoration):
 *   SIM        /api/telemetry, /api/monitor   the digital twin, every 60-s step
 *   DERIVED    same endpoints                 CO2 mass balance, comfort score,
 *                                             power/capacity (backend/telemetry.py)
 *   HARDWARE   /api/monitor -> hardware       the ESP32 rig (zone_b) — real air
 *   REAL       /api/external                  Open-Meteo outdoor weather + PM/AQI
 *   HISTORICAL /api/dataset                   UCI occupancy dataset (Feb 2015)
 *   PREDICTED  /api/forecast                  same controller run ahead on clones
 * Sim and real data sit on DIFFERENT clocks (sim time vs wall time). They are
 * never drawn on one x-axis; real outdoor gets its own charts.
 *
 * Metrics with NO source anywhere in the application (noise) are shown as
 * "not instrumented", never as a number.
 *
 * REFRESH. The shell polls /api/state at 1 Hz; this panel watches sim.t and
 * refetches telemetry + KPIs whenever the clock moved (so a paused sim stops
 * refetching), forecast every 30 s or on a filter change, the real feed every
 * 60 s (server refreshes it every 10 min), the dataset once per window change.
 */
(function () {
  'use strict';

  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return ESCAPES[c]; }); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function f(v, d) { return isNum(v) ? v.toFixed(d === undefined ? 1 : d) : '—'; }
  function sgn(v, d) { return isNum(v) ? (v > 0 ? '+' : '') + v.toFixed(d === undefined ? 1 : d) : '—'; }
  var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  function simClock(t) {
    if (!isNum(t)) return '—';
    var s = Math.floor(t);
    return DAYS[Math.floor(s / 86400) % 7] + ' ' + String(Math.floor(s % 86400 / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0');
  }
  function wallClock(iso) { return iso ? String(iso).replace('T', ' ').slice(5, 16) : '—'; }

  // Series colours: validated categorical set (dataviz palette check, light
  // surface) — sim/FeelsLike blue is the shell's --us, historical purple is the
  // shell's memory badge hue, real outdoor rust and hardware teal are new.
  var C = { sim: '#2a78d6', base: '#898781', real: '#c2410c', hist: '#7c5cd6', hw: '#0d9488', pred: '#2a78d6' };
  var SRC_LABEL = { sim: 'SIM', derived: 'DERIVED', hardware: 'HARDWARE', real: 'REAL', historical: 'HISTORICAL', predicted: 'PREDICTED', none: 'NOT INSTRUMENTED' };
  var WINDOWS = [['live', 'Live'], ['1h', '1 h'], ['6h', '6 h'], ['24h', '24 h'], ['7d', '7 d'], ['custom', 'Custom']];
  var ZONES = [['all', 'Whole building'], ['zone_a', 'Open Office A'], ['zone_b', 'Conference Room B'], ['zone_c', 'Cabin C'], ['zone_d', 'Lobby D'], ['zone_e', 'Cafeteria E']];

  // ======================================================================
  // chart primitive: one y-axis, ≤4 series, band shading, crosshair tooltip,
  // click-to-pin (drill-down). Points are {x, y} with x in the axis' own unit.
  // ======================================================================
  function lineChart(opts) {
    var W = 560, H = opts.h || 200, L = 44, R = 12, T = 12, B = 24, pw = W - L - R, ph = H - T - B;
    var series = opts.series.filter(function (s) { return s.pts && s.pts.length; });
    if (!series.length) return { svg: '<div class="mn-nodata">' + esc(opts.empty || 'No data in this window yet.') + '</div>', recs: [] };
    var xs = [], ys = [];
    series.forEach(function (s) { s.pts.forEach(function (p) { if (isNum(p.x)) xs.push(p.x); if (isNum(p.y)) ys.push(p.y); }); });
    if (opts.band) { ys.push(opts.band[0]); ys.push(opts.band[1]); }
    if (opts.yMin !== undefined) ys.push(opts.yMin);
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
    if (xmax - xmin < 1e-9) xmax = xmin + 1;
    var pad = (ymax - ymin) * 0.08 || 1; ymin -= pad; ymax += pad;
    if (opts.yMin !== undefined && ymin < opts.yMin) ymin = opts.yMin;
    var X = function (x) { return L + pw * (x - xmin) / (xmax - xmin); };
    var Y = function (y) { return T + ph * (1 - (y - ymin) / (ymax - ymin)); };
    var s = '';
    if (opts.band) s += '<rect x="' + L + '" y="' + Y(opts.band[1]).toFixed(1) + '" width="' + pw + '" height="' + Math.max(0, Y(opts.band[0]) - Y(opts.band[1])).toFixed(1) + '" fill="var(--good)" fill-opacity="0.07"/>';
    for (var g = 0; g <= 4; g++) {
      var y = T + ph * g / 4, v = ymax - (ymax - ymin) * g / 4;
      s += '<line x1="' + L + '" y1="' + y + '" x2="' + (L + pw) + '" y2="' + y + '" stroke="var(--grid)"/>' +
        '<text x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end" font-size="10" fill="var(--muted)" class="mn-num">' + esc(opts.fmtY ? opts.fmtY(v) : f(v, ymax - ymin > 20 ? 0 : 1)) + '</text>';
    }
    var ticks = opts.xTicks ? opts.xTicks(xmin, xmax) : [];
    ticks.forEach(function (tk) { s += '<text x="' + X(tk.x).toFixed(1) + '" y="' + (H - 7) + '" font-size="10" text-anchor="middle" fill="var(--muted)">' + esc(tk.label) + '</text>'; });
    if (opts.nowX !== undefined && opts.nowX >= xmin && opts.nowX <= xmax)
      s += '<line x1="' + X(opts.nowX).toFixed(1) + '" y1="' + T + '" x2="' + X(opts.nowX).toFixed(1) + '" y2="' + (T + ph) + '" stroke="var(--ink2)" stroke-dasharray="2 3"/><text x="' + (X(opts.nowX) + (X(opts.nowX) > L + pw - 30 ? -3 : 3)).toFixed(1) + '" y="' + (T + 9) + '" font-size="9" fill="var(--ink2)" text-anchor="' + (X(opts.nowX) > L + pw - 30 ? 'end' : 'start') + '">now</text>';
    series.forEach(function (sr) {
      var d = '', on = false;
      sr.pts.forEach(function (p) {
        if (!isNum(p.y) || !isNum(p.x)) { on = false; return; }
        d += (on ? 'L' : 'M') + X(p.x).toFixed(1) + ' ' + Y(p.y).toFixed(1) + ' '; on = true;
      });
      s += '<path d="' + d + '" fill="none" stroke="' + sr.color + '" stroke-width="2" stroke-linejoin="round"' + (sr.dash ? ' stroke-dasharray="5 4"' : '') + (sr.opacity ? ' stroke-opacity="' + sr.opacity + '"' : '') + '/>';
    });
    s += '<line class="mn-xh" x1="0" y1="' + T + '" x2="0" y2="' + (T + ph) + '" stroke="var(--ink)" stroke-opacity="0.5" visibility="hidden"/>';
    s += '<circle class="mn-dot" r="4" fill="var(--surface)" stroke="var(--ink)" stroke-width="2" visibility="hidden"/>';
    return { svg: '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" class="mn-chart" data-l="' + L + '" data-pw="' + pw + '" data-xmin="' + xmin + '" data-xmax="' + xmax + '">' + s + '</svg>', series: series, X: X, Y: Y, W: W };
  }

  /** Bind crosshair + tooltip + click-to-pin on an SVG built by lineChart. */
  function bindHover(svg, chart, tip, fmtX, onPin) {
    if (!svg || !chart.series) return;
    var L = +svg.dataset.l, pw = +svg.dataset.pw, xmin = +svg.dataset.xmin, xmax = +svg.dataset.xmax;
    function nearest(ev) {
      var r = svg.getBoundingClientRect(), mx = (ev.clientX - r.left) * (chart.W / r.width);
      if (mx < L || mx > L + pw) return null;
      var x = xmin + (mx - L) / pw * (xmax - xmin), best = null, bd = Infinity;
      chart.series[0].pts.forEach(function (p) { var d = Math.abs(p.x - x); if (d < bd) { bd = d; best = p; } });
      return best;
    }
    function show(ev, p) {
      var xh = svg.querySelector('.mn-xh'), dot = svg.querySelector('.mn-dot');
      xh.setAttribute('x1', chart.X(p.x)); xh.setAttribute('x2', chart.X(p.x)); xh.setAttribute('visibility', 'visible');
      if (isNum(p.y)) { dot.setAttribute('cx', chart.X(p.x)); dot.setAttribute('cy', chart.Y(p.y)); dot.setAttribute('visibility', 'visible'); }
      var rows = chart.series.map(function (sr) {
        var q = null, bd = Infinity; sr.pts.forEach(function (z) { var d = Math.abs(z.x - p.x); if (d < bd) { bd = d; q = z; } });
        return '<div><span class="mn-sw" style="background:' + sr.color + '"></span>' + esc(sr.name) + ' <b class="mn-num">' + (q && isNum(q.y) ? esc(f(q.y, sr.dec === undefined ? 1 : sr.dec)) + (sr.unit ? ' ' + esc(sr.unit) : '') : '—') + '</b></div>';
      }).join('');
      tip.show(ev, '<b>' + esc(fmtX(p.x)) + '</b>' + rows + (onPin ? '<div class="mn-tiphint">click to pin · drill down</div>' : ''));
    }
    svg.addEventListener('mousemove', function (ev) { var p = nearest(ev); if (p) show(ev, p); else tip.hide(); });
    svg.addEventListener('mouseleave', function () { tip.hide(); svg.querySelector('.mn-xh').setAttribute('visibility', 'hidden'); svg.querySelector('.mn-dot').setAttribute('visibility', 'hidden'); });
    if (onPin) svg.addEventListener('click', function (ev) { var p = nearest(ev); if (p) onPin(p); });
  }

  function tipFor(root) {
    var tip = document.createElement('div'); tip.className = 'mn-tip'; tip.hidden = true; root.appendChild(tip);
    return {
      show: function (ev, html) {
        tip.innerHTML = html; tip.hidden = false;
        var r = root.getBoundingClientRect(), x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 12;
        if (x + tip.offsetWidth > r.width) x = Math.max(4, x - tip.offsetWidth - 28);
        tip.style.left = x + 'px'; tip.style.top = y + 'px';
      },
      hide: function () { tip.hidden = true; }
    };
  }

  function simTicks(xmin, xmax) {
    var span = xmax - xmin, step = span <= 700 ? 120 : span <= 3700 ? 600 : span <= 6 * 3600 + 10 ? 3600 : span <= 86400 + 10 ? 4 * 3600 : 86400;
    var out = [], t0 = Math.ceil(xmin / step) * step;
    for (var t = t0; t <= xmax; t += step) out.push({ x: t, label: step >= 86400 ? DAYS[Math.floor(t / 86400) % 7] : simClock(t).slice(4) });
    return out;
  }
  function wallTicks(xmin, xmax) {
    var span = xmax - xmin, step = span <= 26 * 3600e3 ? 6 * 3600e3 : 24 * 3600e3, out = [];
    for (var t = Math.ceil(xmin / step) * step; t <= xmax; t += step) { var d = new Date(t); out.push({ x: t, label: step >= 86400e3 ? (d.getUTCDate() + '/' + (d.getUTCMonth() + 1)) : String(d.getUTCHours()).padStart(2, '0') + ':00' }); }
    return out;
  }
  function isoToMs(iso) { var d = Date.parse(String(iso).replace(' ', 'T') + 'Z'); return isNum(d) ? d : NaN; }
  function msToLabel(ms) { var d = new Date(ms); return d.toISOString().slice(5, 16).replace('T', ' '); }

  // ======================================================================
  // the panel
  // ======================================================================
  function monitorPanel() {
    var root, tip, FL = window.FL;
    var zone = 'all', win = '24h', custom = { from: 0, to: 0 };
    var showPred = true, tel = null, mon = null, fc = null, ext = null, ds = null, hw = null;
    var lastT = null, lastFetchT = -1, drillFor = null, lastFc = 0, lastExt = 0, dsWin = '', pinned = null, focus = null, busy = false;
    var dead = false;

    function el(sel) { return root.querySelector(sel); }
    function setStatus(msg, bad) { var n = el('.mn-status'); if (n) { n.textContent = msg; n.classList.toggle('bad', !!bad); } }

    function mount(node) {
      root = node;
      root.innerHTML =
        '<div class="mn">' +
        '<div class="mn-bar">' +
          '<label class="mn-lbl">Zone <select class="mn-zone">' + ZONES.map(function (z) { return '<option value="' + z[0] + '">' + esc(z[1]) + '</option>'; }).join('') + '</select></label>' +
          '<div class="mn-wins" role="group" aria-label="Time window">' + WINDOWS.map(function (w) { return '<button data-w="' + w[0] + '" class="' + (w[0] === win ? 'on' : '') + '">' + w[1] + '</button>'; }).join('') + '</div>' +
          '<span class="mn-custom" hidden>from <input class="mn-from" placeholder="Mon 08:00" size="9"> to <input class="mn-to" placeholder="Tue 08:00" size="9"> <button class="mn-apply">Apply</button></span>' +
          '<label class="mn-chk"><input type="checkbox" class="mn-pred" checked> predicted (next 3 h)</label>' +
          '<span class="mn-status">waiting for the first step…</span>' +
        '</div>' +
        '<div class="mn-legend">' +
          ['sim', 'derived', 'hardware', 'real', 'historical', 'predicted'].map(function (k) { return '<span class="mn-src mn-src-' + k + '" title="">' + SRC_LABEL[k] + '</span>'; }).join('') +
          '<span class="mn-legend-note">Every value carries its provenance. Sim and real data run on different clocks and are never drawn on one axis.</span>' +
        '</div>' +
        '<div class="mn-alerts"></div>' +
        '<div class="mn-kpis"></div>' +
        '<div class="mn-drill" hidden></div>' +
        '<div class="mn-charts"></div>' +
        '<div class="mn-hist"></div>' +
        '</div>';
      tip = tipFor(root); root.classList.add('mn-rel');
      el('.mn-zone').onchange = function () { zone = this.value; pinned = null; drillFor = null; refetch(true); };
      el('.mn-wins').onclick = function (e) {
        var b = e.target.closest('button[data-w]'); if (!b) return;
        win = b.dataset.w; el('.mn-wins').querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
        el('.mn-custom').hidden = win !== 'custom'; pinned = null;
        if (win !== 'custom') refetch(true);
      };
      el('.mn-apply').onclick = function () {
        var a = parseClock(el('.mn-from').value), b = parseClock(el('.mn-to').value);
        if (!isNum(a) || !isNum(b) || b <= a) { setStatus('custom range: use "Mon 08:00" style, and to > from', true); return; }
        custom = { from: a, to: b }; refetch(true);
      };
      el('.mn-pred').onchange = function () { showPred = this.checked; render(); };
      el('.mn-kpis').addEventListener('click', function (e) { var c = e.target.closest('[data-metric]'); if (c) { focus = focus === c.dataset.metric ? null : c.dataset.metric; render(); } });
      el('.mn-drill').addEventListener('click', function (e) {
        if (e.target.closest('.mn-close')) { pinned = null; drillFor = null; render(); return; }
        var z = e.target.closest('[data-zone]'); if (z) { zone = z.dataset.zone; el('.mn-zone').value = zone; pinned = null; refetch(true); }
      });
      if (FL.state) update(FL.state);
    }

    function parseClock(s) {
      var m = /^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}):(\d{2})\s*$/i.exec(s || ''); if (!m) return NaN;
      var d = DAYS.findIndex(function (x) { return x.toLowerCase() === m[1].toLowerCase(); });
      return d * 86400 + (+m[2]) * 3600 + (+m[3]) * 60;
    }

    // ---- data -----------------------------------------------------------
    function update(state) {
      if (dead || !root) return;
      var t = state && state.sim ? state.sim.t : null;
      var moved = t !== lastT; lastT = t;
      if (moved || tel === null) refetch(false);
      var now = Date.now();
      if (now - lastExt > 60000) { lastExt = now; fetchExt(); }
      if (dsWin !== win) { dsWin = win; fetchDs(); }
    }
    function q(path) { return FL.get(path); }
    function telPath() {
      var p = '/api/telemetry?zone=' + zone + '&window=' + win + '&max_points=360';
      if (win === 'custom') p += '&t_from=' + custom.from + '&t_to=' + custom.to;
      return p;
    }
    function refetch(force) {
      if (busy && !force) return; busy = true;
      var t0 = lastT;
      Promise.all([q(telPath()), q('/api/monitor?zone=' + zone)]).then(function (r) {
        tel = r[0]; mon = r[1]; hw = mon.hardware; lastFetchT = t0;
        setStatus('auto-refresh · sim ' + (mon.sim_clock || '') + ' · updated ' + new Date().toLocaleTimeString());
        var now = Date.now();
        // the forecast starts at the live clock; refetch once it is >10 sim-min stale
        // (or on a zone change) so the dashed curve always continues the solid one
        var stale = !fc || fc.zone !== zone || !isNum(fc.t_from) || (mon.sim_t - fc.t_from) > 600;
        if (showPred && (force || stale)) { return q('/api/forecast?zone=' + zone + '&horizon_h=3&max_points=60').then(function (x) { fc = x; }, function () { fc = null; }); }
      }).catch(function (e) { setStatus('refresh failed: ' + (e && e.message || e), true); })
        .then(function () { busy = false; render(); });
    }
    function fetchExt() { q('/api/external').then(function (x) { ext = x; render(); }, function () { ext = { available: false, error: 'fetch failed' }; }); }
    function fetchDs() { q('/api/dataset?window=' + (win === 'custom' ? '24h' : win) + '&max_points=360').then(function (x) { ds = x; render(); }, function () { ds = null; }); }

    // ---- render ---------------------------------------------------------
    function srcBadge(k) { return '<span class="mn-src mn-src-' + esc(k) + '">' + esc(SRC_LABEL[k] || k) + '</span>'; }
    function statusPill(st) {
      var icon = { normal: '●', warning: '▲', critical: '■', info: '○', unknown: '○' }[st] || '○';
      return '<span class="mn-st mn-st-' + esc(st) + '">' + icon + ' ' + esc(st === 'info' ? 'unoccupied' : st) + '</span>';
    }
    function kpiCard(o) {
      var d = o.delta, dp = o.delta_pct;
      var arrow = !isNum(d) ? '' : d > 0 ? '↑' : d < 0 ? '↓' : '→';
      return '<div class="mn-kpi' + (focus === o.key ? ' focus' : '') + (o.status === 'critical' ? ' crit' : o.status === 'warning' ? ' warn' : '') + '" data-metric="' + esc(o.key) + '" tabindex="0">' +
        '<div class="mn-kpi-h"><span class="mn-lbl">' + esc(o.label) + '</span>' + srcBadge(o.source) + '</div>' +
        '<div class="mn-kpi-v mn-num">' + esc(o.value) + (o.unit ? '<span class="mn-unit">' + esc(o.unit) + '</span>' : '') + '</div>' +
        '<div class="mn-kpi-s mn-num">' + (o.prevText !== undefined ? esc(o.prevText) : ('prev ' + esc(o.prev) + (isNum(d) ? ' · ' + arrow + ' ' + esc(sgn(d, o.dec)) + (isNum(dp) ? ' (' + esc(sgn(dp, 1)) + '%)' : '') : ''))) + '</div>' +
        '<div class="mn-kpi-f">' + statusPill(o.status) + (o.note ? '<span class="mn-note">' + esc(o.note) + '</span>' : '') + '</div></div>';
    }

    function renderKpis() {
      if (!mon || !mon.kpis || !mon.kpis.items || !Object.keys(mon.kpis.items).length) { el('.mn-kpis').innerHTML = '<div class="mn-nodata">Waiting for the first simulation step…</div>'; return; }
      var it = mon.kpis.items, cards = [], lag = Math.round(mon.kpis.lag_s / 60);
      function K(key, extra) { var x = it[key]; if (!x) return; cards.push(kpiCard(Object.assign({ key: key, label: x.label, unit: x.unit, source: x.source, value: f(x.value, extra && extra.dec !== undefined ? extra.dec : 1), prev: f(x.prev, extra && extra.dec !== undefined ? extra.dec : 1), delta: x.delta, delta_pct: x.delta_pct, status: x.status, dec: extra && extra.dec !== undefined ? extra.dec : 1 }, extra || {}))); }
      K('temp'); K('t_out', { label: 'Outdoor temperature (sim)' });
      // real outdoor sits beside the sim outdoor, clearly on the wall clock
      var ec = ext && ext.available && ext.current;
      cards.push(kpiCard({ key: 'real_t_out', label: 'Outdoor · real', unit: ec ? '°C' : '', source: 'real',
        value: ec ? f(ec.t_out, 1) : (ext && ext.error ? 'offline' : '…'),
        prevText: ec ? ('RH ' + f(ec.rh_out, 0) + ' % · feels ' + f(ec.apparent_c, 1) + ' °C · ' + wallClock(ec.time)) : (ext && ext.error ? String(ext.error).slice(0, 60) : 'fetching Open-Meteo'),
        status: ec ? statusOf('t_out', ec.t_out) : 'unknown', note: ec ? (ext.site && ext.site.name) : '' }));
      K('rh'); K('co2', { dec: 0, note: 'mass-balance estimate' });
      cards.push(kpiCard({ key: 'aq', label: 'Air quality · real (outdoor)', unit: ec ? 'µg/m³ PM2.5' : '', source: 'real',
        value: ec ? f(ec.pm2_5, 1) : (ext && ext.error ? 'offline' : '…'),
        prevText: ec ? ('PM10 ' + f(ec.pm10, 1) + ' µg/m³ · EAQI ' + f(ec.aqi, 0) + ' · CO₂ ' + f(ec.co2_ppm, 0) + ' ppm') : 'no indoor PM sensor in twin or rig',
        status: ec ? worst([statusOf('pm2_5', ec.pm2_5), statusOf('pm10', ec.pm10), statusOf('aqi', ec.aqi)]) : 'unknown', note: 'CAMS model, not a ground monitor' }));
      var dl = ds && ds.points && ds.points.length ? ds.points[ds.points.length - 1] : null;
      cards.push(kpiCard({ key: 'light', label: 'Light intensity', unit: dl ? 'lux' : '', source: dl ? 'historical' : 'none',
        value: dl ? f(dl.light_lux, 0) : 'not instrumented', prevText: dl ? ('dataset row ' + esc(dl.time) + ' — not this building') : 'no lux sensor in twin, rig or dataset',
        status: dl ? statusOf('light', dl.light_lux) : 'unknown', note: dl ? 'UCI office, Feb 2015' : '' }));
      cards.push(kpiCard({ key: 'noise', label: 'Noise', unit: '', source: 'none', value: 'not instrumented', prevText: 'no sound-level source anywhere in the application', status: 'unknown', note: 'a real deployment would add a dBA sensor on the node' }));
      K('occ', { dec: 0, prevText: 'prev ' + f(it.occ.prev, 0) + ' · ' + f(it.occ_pct.value, 0) + ' % of design headcount' });
      K('comfort', { dec: 0, note: 'heuristic score, not PMV' });
      K('kwh', { label: 'Energy · cumulative', dec: 1, prevText: 'now ' + f(it.power_w.value, 0) + ' W · Δ' + f(it.kwh.delta, 2) + ' kWh in last ' + lag + ' min' });
      var hv = it.hvac && it.hvac.value || {};
      cards.push(kpiCard({ key: 'hvac', label: 'HVAC status', unit: '', source: 'sim',
        value: hv.cooling ? 'cooling' : 'idle', prevText: 'setpoint ' + (zone === 'all' ? 'per zone' : (hv.setpoint === null || hv.setpoint === undefined ? 'off' : f(hv.setpoint, 1) + ' °C')) + ' · fan ' + esc(hv.vent) + ' · capacity ' + f(hv.capacity_pct, 0) + ' %',
        status: it.hvac.status, note: (mon.thresholds && it.capacity_pct.value >= 100) ? 'at capacity' : '' }));
      // hardware rig — real, wall clock, zone_b only
      var r = hw && hw.reading;
      cards.push(kpiCard({ key: 'hw', label: 'Physical zone (rig · Conference B)', unit: hw && hw.connected && r ? '°C' : '', source: 'hardware',
        value: hw && hw.connected && r ? f(r.temp_c, 2) : 'no node',
        prevText: hw && hw.connected && r ? ('RH ' + (r.rh_pct === null ? '—' : f(r.rh_pct, 1) + ' %') + ' · fan ' + hw.fan + ' · ' + f(hw.stale_s, 1) + ' s ago · ' + esc(hw.node_id) + (hw.ambient ? ' · room ' + f(hw.ambient.temp_c, 2) + ' °C' + (hw.ambient.calibrated ? '' : ' (uncalibrated)') : '')) : (hw && hw.polls ? 'last node stale ' + f(hw.stale_s, 0) + ' s' : 'ESP32 never posted — run scripts.mock_node to rehearse'),
        status: hw && hw.connected ? (hw.sensor_health && hw.sensor_health.ok ? 'normal' : 'warning') : 'unknown', note: hw && hw.sensor_health && hw.sensor_health.faults && hw.sensor_health.faults.length ? ('sensor: ' + hw.sensor_health.faults.join(', ')) : (hw && hw.connected ? 'sensor ok · wall clock' : '') }));
      el('.mn-kpis').innerHTML = cards.join('') + '<div class="mn-kpi-note">Previous = ' + lag + ' sim-min earlier. Status thresholds come from backend/telemetry.py THRESHOLDS (ASHRAE 55/62.1, WHO 2021, EN 12464-1); hover a card\'s status for the basis.</div>';
      root.querySelectorAll('.mn-kpi').forEach(function (c) { var k = c.dataset.metric, th = mon.thresholds && mon.thresholds[k]; if (th) c.querySelector('.mn-st').title = th.basis + ' · normal ' + th.normal.join('–') + ' ' + th.unit; });
    }
    function statusOf(m, v) {
      var th = mon && mon.thresholds && mon.thresholds[m]; if (!th || !isNum(v)) return 'unknown';
      if (v < th.critical[0] || v > th.critical[1]) return 'critical';
      if (v < th.normal[0] || v > th.normal[1]) return 'warning';
      return 'normal';
    }
    function worst(a) { return a.indexOf('critical') >= 0 ? 'critical' : a.indexOf('warning') >= 0 ? 'warning' : a.indexOf('normal') >= 0 ? 'normal' : 'unknown'; }

    function renderAlerts() {
      var n = el('.mn-alerts'); if (!mon) { n.innerHTML = ''; return; }
      var al = mon.alerts || [];
      if (!al.length) { n.innerHTML = '<div class="mn-alert-ok">● No threshold breaches in ' + esc(zone === 'all' ? 'any zone' : zoneName(zone)) + ' at ' + esc(mon.sim_clock) + '.</div>'; return; }
      var crit = al.filter(function (a) { return a.status === 'critical'; }).length;
      n.innerHTML = '<details class="mn-alertbox' + (crit ? ' crit' : '') + '" open><summary>' + (crit ? '■ ' : '▲ ') + al.length + ' active alert' + (al.length > 1 ? 's' : '') + (crit ? ' (' + crit + ' critical)' : '') + ' · ' + esc(mon.sim_clock) + '</summary>' +
        '<table class="mn-table"><tr><th>Status</th><th>Zone</th><th>Metric</th><th>Value</th><th>Normal range</th><th>Source</th></tr>' +
        al.map(function (a) { return '<tr data-zone="' + esc(a.zone === 'outdoor' ? 'all' : a.zone) + '"><td>' + statusPill(a.status) + '</td><td>' + esc(a.zone_name) + '</td><td>' + esc(mon.thresholds[a.metric] ? mon.thresholds[a.metric].label : a.metric) + (a.note ? ' <span class="mn-note" title="' + esc(a.note) + '">ⓘ model limitation</span>' : '') + '</td><td class="mn-num">' + esc(f(a.value, a.metric === 'co2' ? 0 : 1)) + ' ' + esc(a.unit) + '</td><td class="mn-num">' + esc(a.normal[0] <= -50 ? '≤ ' + a.normal[1] : a.normal.join(' – ')) + '</td><td>' + srcBadge(a.source) + '</td></tr>'; }).join('') +
        '</table></details>';
      n.querySelector('table').onclick = function (e) { var r = e.target.closest('tr[data-zone]'); if (r && r.dataset.zone !== zone) { zone = r.dataset.zone; el('.mn-zone').value = zone; refetch(true); } };
    }
    function zoneName(id) { var z = ZONES.find(function (x) { return x[0] === id; }); return z ? z[1] : id; }

    function pts(arr, k, xk) { return (arr || []).map(function (p) { return { x: p[xk || 't'], y: p[k] }; }); }
    function chartCard(key, title, sub, chart, sources, fmtX, drill) {
      var id = 'mn-c-' + key;
      return '<div class="mn-card' + (focus === key ? ' focus' : '') + '" data-chart="' + key + '"><h3>' + esc(title) + '<span class="mn-right">' + sources.map(srcBadge).join('') + '</span></h3>' +
        (sub ? '<div class="mn-sub">' + esc(sub) + '</div>' : '') +
        (chart.series && chart.series.length > 1 ? '<div class="mn-leg">' + chart.series.map(function (sr) { return '<span><span class="mn-sw' + (sr.dash ? ' dash' : '') + '" style="' + (sr.dash ? 'border-color:' : 'background:') + sr.color + '"></span>' + esc(sr.name) + '</span>'; }).join('') + '</div>' : '') +
        '<div class="mn-plot" id="' + id + '">' + chart.svg + '</div></div>';
    }
    function renderCharts() {
      var n = el('.mn-charts');
      if (!tel || !tel.points) { n.innerHTML = '<div class="mn-nodata">Waiting for telemetry…</div>'; return; }
      var P = tel.points, nowX = P.length ? P[P.length - 1].t : undefined, band = [23.0, 26.5];
      var fp = (showPred && fc && fc.points && fc.zone === zone) ? fc.points.filter(function (p) { return !isNum(nowX) || p.t > nowX; }) : [];
      // stitch prediction to the last live point so the dashed line continues the solid one
      var stitch = function (k) { var a = pts(fp, k); if (P.length && a.length) a.unshift({ x: P[P.length - 1].t, y: P[P.length - 1][k] }); return a; };
      var zlabel = zone === 'all' ? 'occupied-zone mean' : zoneName(zone);
      var charts = [];
      var c1 = lineChart({ series: [{ name: 'FeelsLike', color: C.sim, pts: pts(P, 'temp'), unit: '°C' }, { name: 'baseline (static 22 °C)', color: C.base, pts: pts(P, 'base_temp'), unit: '°C' }, { name: 'predicted', color: C.pred, dash: true, pts: stitch('temp'), unit: '°C' }], band: band, xTicks: simTicks, nowX: nowX });
      charts.push(['temp', 'Indoor temperature', zlabel + ' · green band = comfort band 23–26.5 °C', c1, ['sim', 'predicted']]);
      var c2 = lineChart({ series: [{ name: 'indoor', color: C.sim, pts: pts(P, 'temp'), unit: '°C' }, { name: 'outdoor (sim)', color: C.real, opacity: 0.75, pts: pts(P, 't_out'), unit: '°C' }], band: band, xTicks: simTicks });
      charts.push(['inout', 'Indoor vs outdoor (sim clock)', 'both from the twin — the real outdoor feed is charted separately below on the wall clock', c2, ['sim']]);
      var c3 = lineChart({ series: [{ name: 'indoor RH', color: C.sim, pts: pts(P, 'rh'), unit: '%' }, { name: 'outdoor RH', color: C.real, opacity: 0.75, pts: pts(P, 'rh_out'), unit: '%' }, { name: 'predicted', color: C.pred, dash: true, pts: stitch('rh'), unit: '%' }], yMin: 0, xTicks: simTicks, nowX: nowX });
      charts.push(['rh', 'Humidity', zlabel + ' · simulated indoor RH is pinned high by the coil-ADP approximation (known twin limitation)', c3, ['sim', 'predicted']]);
      var c4 = lineChart({ series: [{ name: 'CO₂ estimate', color: C.sim, pts: pts(P, 'co2'), unit: 'ppm', dec: 0 }, { name: 'predicted', color: C.pred, dash: true, pts: stitch('co2'), unit: 'ppm', dec: 0 }], band: [420, 1000], yMin: 300, xTicks: simTicks, nowX: nowX, fmtY: function (v) { return f(v, 0); } });
      charts.push(['co2', 'CO₂ (mass-balance estimate)', (zone === 'all' ? 'worst zone' : zlabel) + ' · ASHRAE 62.1 single-zone balance from occupancy + ventilation; not a sensor', c4, ['derived', 'predicted']]);
      var c5 = lineChart({ series: [{ name: 'FeelsLike', color: C.sim, pts: pts(P, 'power_w'), unit: 'W', dec: 0 }, { name: 'baseline', color: C.base, pts: pts(P, 'base_power_w'), unit: 'W', dec: 0 }, { name: 'predicted', color: C.pred, dash: true, pts: stitch('power_w'), unit: 'W', dec: 0 }], yMin: 0, xTicks: simTicks, nowX: nowX, fmtY: function (v) { return f(v / 1000, 1) + 'k'; } });
      charts.push(['energy', 'HVAC electrical power', (zone === 'all' ? 'building' : zlabel) + ' · W = cooling / COP + fan; cumulative kWh is the card above', c5, ['derived']]);
      var c6 = lineChart({ series: [{ name: 'comfort score', color: C.sim, pts: pts(P, 'comfort'), unit: '/100', dec: 0 }, { name: 'predicted', color: C.pred, dash: true, pts: stitch('comfort'), unit: '/100', dec: 0 }], band: [70, 100], yMin: 0, xTicks: simTicks, nowX: nowX });
      charts.push(['comfort', 'Thermal comfort score', zlabel + ' · 100 inside the band, −33/°C outside, −1/%RH above 65 %', c6, ['derived', 'predicted']]);
      var c7 = lineChart({ series: [{ name: 'people', color: C.sim, pts: pts(P, 'occ'), unit: '', dec: 0 }], yMin: 0, xTicks: simTicks, fmtY: function (v) { return f(v, 0); } });
      charts.push(['occ', 'Occupancy', zlabel + ' · schedule profile × occupancy scale (sim/twin.py)', c7, ['sim']]);
      // real outdoor (wall clock) — separate axis by design
      if (ext && ext.available) {
        var hr = ext.hourly || [], past = hr.filter(function (r) { return r.kind === 'past'; }), fut = hr.filter(function (r) { return r.kind === 'forecast'; });
        if (past.length && fut.length) fut.unshift(past[past.length - 1]);
        var nowMs = ext.current ? isoToMs(ext.current.time) : undefined;
        var c8 = lineChart({ series: [{ name: 'outdoor °C', color: C.real, pts: past.map(function (r) { return { x: isoToMs(r.time), y: r.t_out }; }), unit: '°C' }, { name: 'forecast', color: C.real, dash: true, pts: fut.map(function (r) { return { x: isoToMs(r.time), y: r.t_out }; }), unit: '°C' }], xTicks: wallTicks, nowX: nowMs });
        charts.push(['real_t', 'Outdoor temperature · real (' + (ext.site && ext.site.name) + ')', 'Open-Meteo, wall clock, last 7 days + 24 h forecast · ' + (ext.attribution || ''), c8, ['real', 'predicted'], msToLabel]);
        var ah = ext.air_hourly || [];
        var c9 = lineChart({ series: [{ name: 'PM2.5', color: C.real, pts: ah.map(function (r) { return { x: isoToMs(r.time), y: r.pm2_5 }; }), unit: 'µg/m³' }, { name: 'PM10', color: C.hist, pts: ah.map(function (r) { return { x: isoToMs(r.time), y: r.pm10 }; }), unit: 'µg/m³' }], band: [0, 35], yMin: 0, xTicks: wallTicks, nowX: nowMs });
        charts.push(['real_pm', 'Outdoor particulates · real', 'CAMS model output for the site · green band = WHO PM2.5 24-h interim target (35 µg/m³)', c9, ['real'], msToLabel]);
      } else {
        charts.push(['real_t', 'Outdoor · real', '', { svg: '<div class="mn-nodata">' + esc(ext && ext.error ? 'Open-Meteo unavailable: ' + ext.error : 'Fetching the real outdoor feed…') + '</div>' }, ['real']]);
      }
      n.innerHTML = charts.map(function (c) { return chartCard(c[0], c[1], c[2], c[3], c[4]); }).join('');
      charts.forEach(function (c) {
        var svg = n.querySelector('#mn-c-' + c[0] + ' svg'); if (!svg || !c[3].series) return;
        var isWall = !!c[5];
        bindHover(svg, c[3], tip, c[5] || simClock, isWall ? null : function (p) { pinned = p.x; render(); });
      });
    }

    function renderDrill() {
      var n = el('.mn-drill');
      if (pinned === null || !tel || !tel.points) { n.hidden = true; return; }
      n.hidden = false;
      // the pinned instant: fetch a per-zone snapshot from the whole-building series is not
      // enough (zones are folded) — ask the server for all zones around this t.
      var t = pinned;
      if (drillFor === t) return;              // already showing this instant
      drillFor = t;
      Promise.all(ZONES.slice(1).map(function (z) { return q('/api/telemetry?zone=' + z[0] + '&window=custom&t_from=' + (t - 30) + '&t_to=' + (t + 30) + '&max_points=2'); }))
        .then(function (rs) {
          if (pinned !== t) return;
          var rows = rs.map(function (r, i) { var p = r.points && r.points[r.points.length - 1]; return { z: ZONES[i + 1], p: p }; });
          n.innerHTML = '<div class="mn-card"><h3>Drill-down · ' + esc(simClock(t)) + '<span class="mn-right">' + srcBadge('sim') + srcBadge('derived') + '<button class="mn-close mn-btn">close</button></span></h3>' +
            '<table class="mn-table"><tr><th>Zone</th><th>Temp</th><th>Baseline</th><th>Setpoint</th><th>Fan</th><th>RH</th><th>CO₂ est.</th><th>Comfort</th><th>People</th><th>Power</th><th>Capacity</th><th>Constraints</th></tr>' +
            rows.map(function (r) { var p = r.p || {}; return '<tr data-zone="' + r.z[0] + '" class="' + (zone === r.z[0] ? 'on' : '') + '"><td>' + esc(r.z[1]) + '</td><td class="mn-num">' + f(p.temp, 1) + ' °C</td><td class="mn-num">' + f(p.base_temp, 1) + ' °C</td><td class="mn-num">' + (p.setpoint == null ? 'off' : f(p.setpoint, 1) + ' °C') + '</td><td>' + esc(p.vent) + '</td><td class="mn-num">' + f(p.rh, 0) + ' %</td><td class="mn-num">' + f(p.co2, 0) + ' ppm</td><td class="mn-num">' + f(p.comfort, 0) + '</td><td class="mn-num">' + f(p.occ, 0) + '</td><td class="mn-num">' + f(p.power_w, 0) + ' W</td><td class="mn-num">' + f(p.capacity_pct, 0) + ' %' + (p.at_capacity ? ' ■' : '') + '</td><td class="mn-num">' + f(p.constraints, 0) + '</td></tr>'; }).join('') +
            '</table><div class="mn-note">Click a zone row to focus it. Values are the telemetry row nearest the pinned instant (bucket-averaged when the window is coarse). Constraints = active occupant complaints driving that zone at the time.</div></div>';
        }).catch(function (e) { n.innerHTML = '<div class="mn-nodata">drill-down failed: ' + esc(e && e.message || e) + '</div>'; });
      n.innerHTML = '<div class="mn-nodata">loading ' + esc(simClock(t)) + '…</div>';
    }

    function renderHist() {
      var n = el('.mn-hist');
      if (!ds || !ds.points || !ds.points.length) { n.innerHTML = ds && ds.info && !ds.info.available ? '<div class="mn-nodata">Historical dataset unavailable: ' + esc(ds.info.error) + '</div>' : ''; return; }
      var P = ds.points.map(function (p) { return Object.assign({ x: isoToMs(p.time) }, p); });
      function ch(k, unit, dec, band) { return lineChart({ series: [{ name: unit, color: C.hist, pts: P.map(function (p) { return { x: p.x, y: p[k] }; }), unit: unit, dec: dec }], h: 150, yMin: 0, band: band, xTicks: wallTicks, fmtY: function (v) { return f(v, dec); } }); }
      var cs = [['temp_c', 'Temperature', '°C', 1, null], ['rh_pct', 'Humidity', '%', 0, null], ['co2_ppm', 'CO₂ (sensor)', 'ppm', 0, [400, 1000]], ['light_lux', 'Light', 'lux', 0, [300, 2000]], ['occupied', 'Occupancy (ground truth 0/1)', '', 2, null]];
      var info = ds.info || {};
      n.innerHTML = '<div class="mn-card"><h3>Historical reference · ' + esc(info.name || 'dataset') + '<span class="mn-right">' + srcBadge('historical') + '</span></h3>' +
        '<div class="mn-sub">' + esc(info.note || '') + ' Window: ' + esc(ds.t_from) + ' → ' + esc(ds.t_to) + ' (' + esc(ds.count_raw) + ' one-minute rows, ' + esc(P.length) + ' plotted). Licence ' + esc(info.license || '') + ' · <a href="' + esc(info.url || '#') + '" target="_blank" rel="noopener">source</a>. This is the validation set for the CO₂ estimator above — the sensor curve here vs the model curve there.</div>' +
        '<div class="mn-hist-grid">' + cs.map(function (c, i) { var chart = ch(c[0], c[2], c[3], c[4]); return '<div class="mn-mini" data-i="' + i + '"><div class="mn-lbl">' + esc(c[1]) + '</div>' + chart.svg + '</div>'; }).join('') + '</div></div>';
      n.querySelectorAll('.mn-mini').forEach(function (m, i) { var c = cs[i], chart = ch(c[0], c[2], c[3], c[4]); bindHover(m.querySelector('svg'), chart, tip, function (x) { return msToLabel(x) + ' (2015)'; }, null); });
    }

    function render() {
      if (dead || !root) return;
      try { renderAlerts(); renderKpis(); renderDrill(); renderCharts(); renderHist(); }
      catch (e) { dead = true; console.error('[FeelsLike monitor] stopped:', e); root.insertAdjacentHTML('afterbegin', '<div class="mn-nodata bad">Monitor panel stopped: ' + esc(e && e.message || e) + '. Other tabs are unaffected.</div>'); }
    }

    return { mount: mount, update: update };
  }

  // ======================================================================
  // CSS — tokens only, injected once
  // ======================================================================
  function injectCSS() {
    if (document.getElementById('mn-css')) return;
    var css = [
      '.mn{display:flex;flex-direction:column;gap:12px;}', '.mn-rel{position:relative;}',
      '.mn-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:9px 12px;font-size:12.5px;color:var(--ink2);}',
      '.mn-bar select,.mn-bar input{font:inherit;border:1px solid var(--border);border-radius:8px;padding:4px 8px;background:var(--page);color:var(--ink);}',
      '.mn-wins{display:flex;gap:3px;} .mn-wins button,.mn-btn,.mn-apply{font:inherit;font-size:12px;border:1px solid var(--border);background:transparent;color:var(--ink2);border-radius:99px;padding:3px 10px;cursor:pointer;}',
      '.mn-wins button.on{color:var(--ink);border-color:var(--ink2);font-weight:600;background:var(--page);}',
      '.mn-chk{display:inline-flex;gap:5px;align-items:center;} .mn-status{margin-left:auto;font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;} .mn-status.bad{color:var(--crit);}',
      '.mn-legend{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:11.5px;color:var(--muted);padding:0 4px;} .mn-legend-note{margin-left:6px;}',
      '.mn-src{display:inline-block;font-size:9.5px;font-weight:700;letter-spacing:0.06em;border-radius:5px;padding:1px 6px;border:1px solid var(--border);color:var(--ink2);background:var(--page);}',
      '.mn-src-sim{color:' + C.sim + ';border-color:' + C.sim + ';} .mn-src-derived{color:' + C.sim + ';border-style:dashed;}',
      '.mn-src-hardware{color:' + C.hw + ';border-color:' + C.hw + ';} .mn-src-real{color:' + C.real + ';border-color:' + C.real + ';}',
      '.mn-src-historical{color:' + C.hist + ';border-color:' + C.hist + ';} .mn-src-predicted{color:' + C.pred + ';border-style:dotted;}',
      '.mn-src-none{color:var(--muted);}',
      '.mn-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(196px,1fr));gap:9px;}',
      '.mn-kpi{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:10px 12px;cursor:pointer;transition:box-shadow .15s;}',
      '.mn-kpi:hover,.mn-kpi:focus-visible{box-shadow:0 0 0 2px var(--us);outline:0;} .mn-kpi.focus{box-shadow:inset 0 -3px 0 var(--us);}',
      '.mn-kpi.warn{border-color:var(--warn);} .mn-kpi.crit{border-color:var(--crit);}',
      '.mn-kpi-h{display:flex;justify-content:space-between;align-items:center;gap:6px;} .mn-lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}',
      '.mn-kpi-v{font-size:24px;font-weight:650;letter-spacing:-0.02em;margin-top:3px;} .mn-unit{font-size:12px;font-weight:500;color:var(--ink2);margin-left:4px;}',
      '.mn-kpi-s{font-size:11.5px;color:var(--ink2);margin-top:1px;} .mn-kpi-f{display:flex;gap:8px;align-items:center;margin-top:5px;flex-wrap:wrap;}',
      '.mn-kpi-note{grid-column:1/-1;font-size:11px;color:var(--muted);padding:0 4px;}',
      '.mn-num{font-variant-numeric:tabular-nums;} .mn-note{font-size:10.5px;color:var(--muted);}',
      '.mn-st{font-size:10.5px;font-weight:600;border-radius:99px;padding:1px 7px;border:1px solid var(--border);} .mn-st-normal{color:var(--good);} .mn-st-warning{color:#7a5c00;background:rgba(250,178,25,0.18);border-color:rgba(250,178,25,0.7);} .mn-st-critical{color:#fff;background:var(--crit);border-color:var(--crit);} .mn-st-info,.mn-st-unknown{color:var(--muted);}',
      '.mn-alert-ok{font-size:12.5px;color:var(--good);border:1px solid color-mix(in srgb,var(--good) 40%,transparent);border-radius:10px;padding:7px 12px;background:var(--surface);}',
      '.mn-alertbox{border:1px solid var(--warn);border-radius:10px;background:var(--surface);padding:6px 12px;font-size:12.5px;} .mn-alertbox.crit{border-color:var(--crit);} .mn-alertbox summary{cursor:pointer;font-weight:600;color:var(--ink);}',
      '.mn-table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:6px;} .mn-table th{text-align:left;color:var(--muted);font-size:11px;font-weight:600;border-bottom:1px solid var(--grid);padding:4px 6px;} .mn-table td{padding:4px 6px;border-bottom:1px solid var(--grid);} .mn-table tr[data-zone]{cursor:pointer;} .mn-table tr.on td{background:color-mix(in srgb,var(--us) 8%,transparent);}',
      '.mn-charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px;} @media(max-width:520px){.mn-charts{grid-template-columns:1fr;}}',
      '.mn-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px;} .mn-card.focus{box-shadow:0 0 0 2px var(--us);}',
      '.mn-card h3{font-size:13px;color:var(--ink2);font-weight:600;display:flex;align-items:center;gap:6px;margin-bottom:4px;} .mn-right{margin-left:auto;display:flex;gap:4px;align-items:center;}',
      '.mn-sub{font-size:11.5px;color:var(--muted);margin-bottom:6px;} .mn-chart{display:block;cursor:crosshair;} .mn-chart text{font-family:inherit;}',
      '.mn-nodata{font-size:12.5px;color:var(--muted);border:1px dashed var(--border);border-radius:10px;padding:14px;text-align:center;} .mn-nodata.bad{color:var(--crit);border-color:var(--crit);}',
      '.mn-tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,0.12);z-index:5;white-space:nowrap;} .mn-tip b{display:block;margin-bottom:2px;} .mn-sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px;} .mn-tiphint{color:var(--muted);font-size:10.5px;margin-top:3px;}',
      '.mn-leg{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:var(--ink2);margin-bottom:4px;} .mn-sw.dash{background:transparent;border:2px dashed;height:5px;width:12px;border-width:2px 0 0 0;border-radius:0;vertical-align:2px;}',
      '.mn-hist-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;} .mn-mini{border:1px solid var(--grid);border-radius:8px;padding:6px;}'
    ].join('');
    var st = document.createElement('style'); st.id = 'mn-css'; st.textContent = css; document.head.appendChild(st);
  }

  // ======================================================================
  // boot: same handshake as panels.js
  // ======================================================================
  var booted = false;
  function boot() {
    if (booted) return true;
    var FL = window.FL; if (!FL || typeof FL.registerPanel !== 'function') return false;
    booted = true; injectCSS();
    FL.registerPanel('monitor', monitorPanel());
    return true;
  }
  if (!boot()) {
    var tries = 0, timer = setInterval(function () { if (boot() || ++tries >= 30) { clearInterval(timer); if (!booted) console.warn('[FeelsLike monitor] window.FL never appeared; Monitor tab not mounted.'); } }, 100);
  }
})();
