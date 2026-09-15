/* dashboard/comfort.js — the Comfort tab: facility-manager occupant-comfort console.
 *
 * Renders only what the backend computes (backend/comfort.py is the source of truth;
 * no score, status or threshold is calculated here):
 *   GET /api/comfort                KPIs, zone assessments, floors, open events, weights, priority
 *   GET /api/comfort/events         events table (zone / issue / status filters)
 *   GET /api/comfort/history        trends — live twin telemetry or the historical dataset
 *   GET /api/comfort/tradeoff       comfort vs energy, SIMULATED / WHAT-IF on clones
 *   GET /api/history/catalog        historical building list
 * Heatmap click -> the Building tab's zone drill-down (event fl:select-zone).
 */
(function () {
  'use strict';
  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function num(v, d) { return isNum(v) ? v.toFixed(d === undefined ? 1 : d) : '—'; }
  function dur(s) { if (!isNum(s)) return '—'; s = Math.round(s / 60); return s >= 60 ? Math.floor(s / 60) + ' h ' + (s % 60) + ' min' : s + ' min'; }
  var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  function simClock(t) { if (!isNum(t)) return '—'; var s = Math.floor(t); return DAYS[Math.floor(s / 86400) % 7] + ' ' + String(Math.floor(s % 86400 / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0'); }
  function isoClock(t) { return new Date(t * 1000).toISOString().slice(5, 16).replace('T', ' '); }
  var ISSUES = [['', 'All issues'], ['warm', 'Warm'], ['cold', 'Cold'], ['humid', 'Humid'], ['dry', 'Dry'], ['high_co2', 'High CO₂']];
  var RANGES = ['1h', '6h', '12h', '24h', '7d', '30d'];
  var SRC = { sim: 'SIM', derived: 'DERIVED', historical: 'HISTORICAL', predicted: 'PREDICTED', hardware: 'HARDWARE', real: 'REAL' };
  var SEVCLS = { none: 'ok', info: 'info', low: 'low', medium: 'med', high: 'high', severe: 'sev' };
  function badge(k) { return '<span class="bd-src bd-src-' + esc(k) + '">' + esc(SRC[k] || k) + '</span>'; }

  function comfortPanel() {
    var FL = window.FL, root, tip, snap = null, events = [], hist = null, trade = null, cat = null;
    var f = { floor: '', zone: '', issue: '', range: '6h', source: 'live', building_id: '', estatus: 'all' };
    var lastT = null, busy = false, lastFetch = 0;
    function el(s) { return root.querySelector(s); }
    function status(m, bad) { var n = el('.cf-status'); n.textContent = m; n.classList.toggle('bad', !!bad); }
    function err(e) { var d = e && e.body && e.body.detail; return d ? (typeof d === 'string' ? d : JSON.stringify(d)) : (e && e.message) || String(e); }

    function mount(node) {
      root = node;
      root.innerHTML = '<div class="bd cf">' +
        '<div class="bd-bar" role="group" aria-label="Comfort filters">' +
          '<label>Floor <select class="cf-floor"><option value="">All floors</option></select></label>' +
          '<label>Zone <select class="cf-zone"><option value="">All zones</option></select></label>' +
          '<label>Issue <select class="cf-issue">' + ISSUES.map(function (i) { return '<option value="' + i[0] + '">' + i[1] + '</option>'; }).join('') + '</select></label>' +
          '<label>Trend source <select class="cf-source"><option value="live">Live twin (SIM)</option><option value="historical">Historical dataset</option></select></label>' +
          '<label class="cf-bl" hidden>Dataset building <select class="cf-bld"></select></label>' +
          '<div class="bd-seg cf-ranges" role="group" aria-label="Date range">' + RANGES.map(function (r) { return '<button type="button" data-r="' + r + '" class="' + (r === f.range ? 'on' : '') + '">' + r + '</button>'; }).join('') + '</div>' +
          '<span class="bd-status cf-status" role="status" aria-live="polite">loading comfort…</span>' +
        '</div>' +
        '<div class="cf-head bd-muted bd-small"></div>' +
        '<div class="bd-kpis cf-kpis" role="list" aria-label="Comfort KPIs"></div>' +
        '<div class="bd-main">' +
          '<section class="card"><h2>Comfort heatmap <span class="right">click a zone → drill-down</span></h2><div class="cf-heat"></div></section>' +
          '<section class="card"><h2>Worst zones · why <span class="right">occupied first</span></h2><div class="cf-worst"></div></section>' +
        '</div>' +
        '<section class="card"><h2>Comfort trend <span class="right cf-tnote"></span></h2><div class="cf-trend"></div></section>' +
        '<div class="bd-main">' +
          '<section class="card"><h2>Comfort events <span class="right"><select class="cf-estatus" aria-label="Event status"><option value="all">All</option><option value="open">Open</option><option value="resolved">Resolved</option></select></span></h2><div class="cf-events"></div></section>' +
          '<section class="card"><h2>Discomfort duration <span class="right">occupied time only</span></h2><div class="cf-dur"></div></section>' +
        '</div>' +
        '<section class="card"><h2>Comfort vs energy <span class="bd-src bd-src-predicted">SIMULATED / WHAT-IF</span> <span class="right">' +
          '<select class="cf-tzone" aria-label="Zone"></select> <select class="cf-taction" aria-label="Action"><option value="auto">Auto (from root cause)</option><option value="cool">Setpoint −1 K</option><option value="raise">Setpoint +1 K</option><option value="vent">Fan +1</option></select> ' +
          '<select class="cf-th" aria-label="Horizon"><option value="0.5">30 min</option><option value="1" selected>1 h</option><option value="2">2 h</option></select> <button type="button" class="cf-run bd-close">Run</button></span></h2><div class="cf-trade"><p class="bd-muted">Choose a zone and run: two clone runs of the live twin (current vs proposed action). Not a guaranteed result.</p></div></section>' +
        '<p class="bd-foot cf-legend"></p></div>';
      if (window.FLChart) { tip = window.FLChart.tipFor(root); root.classList.add('mn-rel'); }
      el('.cf-floor').onchange = function () { f.floor = this.value; refresh(true); };
      el('.cf-zone').onchange = function () { f.zone = this.value; refresh(true); loadHist(); };
      el('.cf-issue').onchange = function () { f.issue = this.value; refresh(true); };
      el('.cf-estatus').onchange = function () { f.estatus = this.value; loadEvents(); };
      el('.cf-source').onchange = function () { f.source = this.value; el('.cf-bl').hidden = f.source !== 'historical'; if (f.source === 'historical') loadCatalog(); loadHist(); };
      el('.cf-bld').onchange = function () { f.building_id = this.value; loadHist(); };
      el('.cf-ranges').onclick = function (e) { var b = e.target.closest('button[data-r]'); if (!b) return; f.range = b.dataset.r; this.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); }); loadHist(); };
      el('.cf-run').onclick = runTrade;
      el('.cf-heat').addEventListener('click', function (e) { var z = e.target.closest('[data-zone]'); if (z) openZone(z.dataset.zone); });
      el('.cf-heat').addEventListener('keydown', function (e) { var z = e.target.closest('[data-zone]'); if (z && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openZone(z.dataset.zone); } });
      el('.cf-legend').innerHTML = 'Data states: ' + ['sim', 'derived', 'historical', 'predicted'].map(badge).join(' ') +
        ' — comfort is an engineering index computed in backend/comfort.py (not a PMV/PPD certification). CO₂ is a ventilation indicator, not a full air-quality measurement.';
      refresh(true); loadHist();
    }
    function openZone(z) {
      FL.go('building');
      setTimeout(function () { document.dispatchEvent(new CustomEvent('fl:select-zone', { detail: { zone: z } })); }, 50);
    }
    function update(state) {
      var t = state && state.sim ? state.sim.t : null;
      if (t !== lastT && Date.now() - lastFetch > 1500) refresh(false);
      lastT = t;
    }
    function qs() { return ['floor=' + f.floor, 'zone=' + f.zone, 'issue=' + f.issue].filter(function (p) { return !/=$/.test(p); }).join('&'); }
    function refresh(force) {
      if (busy && !force) return;
      busy = true; lastFetch = Date.now();
      Promise.all([FL.get('/api/comfort' + (qs() ? '?' + qs() : '')), FL.get('/api/comfort')]).then(function (r) {
        snap = r[0]; snap.all = r[1];
        render(); status('live · sim ' + snap.sim_clock);
        return loadEvents();
      }, function (e) { status('comfort failed: ' + err(e), true); }).then(function () { busy = false; });
    }
    function loadEvents() {
      var p = ['status=' + f.estatus, 'limit=60'];
      if (f.zone) p.push('zone=' + f.zone);
      if (f.issue) p.push('issue=' + f.issue);
      return FL.get('/api/comfort/events?' + p.join('&')).then(function (j) { events = j.events; renderEvents(); });
    }
    function loadCatalog() {
      if (cat) return;
      FL.get('/api/history/catalog').then(function (c) {
        cat = c;
        var s = el('.cf-bld');
        if (!c.available) { s.innerHTML = '<option value="">no dataset</option>'; return; }
        s.innerHTML = c.buildings.map(function (b) { return '<option value="' + esc(b.building_id) + '">' + esc(b.building_name) + '</option>'; }).join('');
        f.building_id = c.buildings[0].building_id; loadHist();
      });
    }
    function loadHist() {
      var p = ['range=' + f.range, 'source=' + f.source];
      if (f.source === 'live') p.push('zone=' + (f.zone || 'all'));
      else if (f.building_id) p.push('building_id=' + encodeURIComponent(f.building_id));
      el('.cf-trend').innerHTML = '<p class="bd-foot">Loading trend…</p>';
      FL.get('/api/comfort/history?' + p.join('&')).then(function (h) { hist = h; renderTrend(); }, function (e) { el('.cf-trend').innerHTML = '<p class="bd-err">' + esc(err(e)) + '</p>'; });
    }

    function render() {
      var all = snap.all, s = snap.summary;
      var fsel = el('.cf-floor'), zsel = el('.cf-zone'), tz = el('.cf-tzone');
      if (fsel.options.length === 1) fsel.innerHTML += all.floors.map(function (x) { return '<option value="' + x.floor + '">Floor ' + x.floor + '</option>'; }).join('');
      if (zsel.options.length === 1) {
        zsel.innerHTML += all.zones.map(function (z) { return '<option value="' + esc(z.zone_id) + '">' + esc(z.role + ' (' + z.zone_name + ')') + '</option>'; }).join('');
        tz.innerHTML = all.zones.map(function (z) { return '<option value="' + esc(z.zone_id) + '">' + esc(z.role) + '</option>'; }).join('');
      }
      var w = snap.weights, pr = snap.priority, th = snap.thresholds;
      el('.cf-head').innerHTML = esc(snap.building.name) + ' · comfort ' + num(th.comfort_c[0]) + '–' + num(th.comfort_c[1]) + ' °C · RH ' + num(th.humidity_pct[0], 0) + '–' + num(th.humidity_pct[1], 0) + ' % · CO₂ ≤ ' + num(th.co2_ppm, 0) + ' ppm (' + esc(th.basis) + ') · weights thermal ' + Math.round(w.thermal * 100) + ' % / humidity ' + Math.round(w.humidity * 100) + ' % / CO₂ ' + Math.round(w.air_quality * 100) + ' % · priority comfort ' + Math.round(pr.comfort_weight * 100) + ' / energy ' + Math.round(pr.energy_weight * 100) + ' → act from ' + esc(pr.act_from_severity) + ' severity' +
        (snap.model_notes && snap.model_notes.length ? '<div class="bd-warn" role="note">ⓘ ' + esc(snap.model_notes.join(' ')) + '</div>' : '');
      var K = [
        ['Overall comfort', num(s.overall_score, 0), '/100', 'derived', s.overall_score],
        ['Occupied comfort', num(s.occupied_score, 0), '/100', 'derived', s.occupied_score],
        ['Comfortable zones', s.comfortable_zones + ' / ' + s.occupied_zones, 'occupied', 'derived'],
        ['Uncomfortable zones', s.uncomfortable_zones, '', 'derived', s.uncomfortable_zones ? 60 : 100],
        ['Comfort compliance', num(s.compliance_pct, 0), '%', 'derived', s.compliance_pct],
        ['Avg discomfort event', dur(s.average_event_duration_s), '', 'derived'],
        ['Worst zone', s.worst_zone ? esc(zoneName(s.worst_zone.zone_id)) : '—', s.worst_zone ? num(s.worst_zone.score, 0) + ' · ' + s.worst_zone.status : '', 'derived'],
        ['Most frequent issue', s.most_frequent_issue ? s.most_frequent_issue.replace('_', ' ') : 'none', '', 'derived'],
        ['High CO₂ zones', s.high_co2_zones, '', 'derived', s.high_co2_zones ? 60 : 100],
        ['Warm zones', s.warm_zones, '', 'derived', s.warm_zones ? 60 : 100],
        ['Cold zones', s.cold_zones, '', 'derived', s.cold_zones ? 60 : 100],
        ['Humid zones', s.humid_zones, '', 'derived', s.humid_zones ? 60 : 100]
      ];
      el('.cf-kpis').innerHTML = K.map(function (k) {
        var st = !isNum(k[4]) ? '' : k[4] < 60 ? ' bd-k-critical' : k[4] < 75 ? ' bd-k-warning' : '';
        return '<div class="bd-kpi' + st + '" role="listitem"><div class="bd-kpi-h"><span>' + esc(k[0]) + '</span>' + badge(k[3]) + '</div><div class="bd-kpi-v">' + k[1] + (k[2] ? '<span class="bd-unit">' + esc(k[2]) + '</span>' : '') + '</div></div>';
      }).join('');
      var shown = {}; snap.zones.forEach(function (z) { shown[z.zone_id] = z; });
      el('.cf-heat').innerHTML = all.floors.filter(function (fl) { return !f.floor || String(fl.floor) === f.floor; }).map(function (fl) {
        return '<div class="cf-floorrow"><div class="cf-floorlbl">Floor ' + fl.floor + '</div><div class="cf-cells">' + fl.zones.map(function (zid) {
          var z = shown[zid];
          if (!z) return '<div class="cf-cell cf-dim" aria-hidden="true">' + esc(zoneName(zid)) + '<small>filtered out</small></div>';
          var sev = SEVCLS[z.severity] || 'ok';
          var aq = z.air_quality.status !== 'Good' && z.air_quality.status !== 'Moderate' ? ' · Air: ' + z.air_quality.status : '';
          return '<div class="cf-cell cf-' + sev + (z.comfort_relevant ? '' : ' cf-empty') + '" data-zone="' + esc(zid) + '" role="button" tabindex="0" aria-label="' + esc(z.role + ': ' + z.status + ', score ' + num(z.score, 0)) + '">' +
            '<b>' + esc(z.role) + '</b><span class="cf-st">' + esc(z.status) + '</span>' +
            '<span class="cf-score">' + num(z.score, 0) + '<small>/100</small></span>' +
            '<small>Thermal: ' + esc(z.thermal.status) + ' · Humidity: ' + esc(z.humidity.status) + esc(aq) + '</small>' +
            '<small>' + esc(z.occupancy_state || '—') + ' · ' + num(z.thermal.value) + ' °C · ' + num(z.humidity.value, 0) + ' % · ' + num(z.air_quality.value, 0) + ' ppm</small></div>';
        }).join('') + '</div></div>';
      }).join('');
      var worst = snap.zones.slice().sort(function (a, b) { return (b.comfort_relevant - a.comfort_relevant) || ((a.score === null ? 999 : a.score) - (b.score === null ? 999 : b.score)); });
      el('.cf-worst').innerHTML = worst.length ? worst.map(function (z) {
        var pc = z.primary_cause, r = z.recommendation;
        var why = pc ? '<b>Primary cause: ' + esc(pc.dimension === 'air_quality' ? 'CO₂' : pc.dimension) + '</b> — ' + num(pc.value, pc.unit === 'ppm' || pc.unit === '%' ? 0 : 1) + ' ' + esc(pc.unit) + ' vs ' + (pc.target[0] === null ? '≤ ' + pc.target[1] : pc.target[0] + '–' + pc.target[1]) + ' ' + esc(pc.unit) + ' (' + esc(pc.severity) + ')' +
          (z.secondary_causes.length ? '<br>Secondary: ' + z.secondary_causes.map(function (c) { return esc(c.status) + ' ' + num(c.value, c.unit === 'ppm' || c.unit === '%' ? 0 : 1) + ' ' + esc(c.unit); }).join(', ') : '') :
          'No issue in the configured ranges.';
        var hv = z.hvac || {};
        return '<div class="cf-why"><div class="cf-why-h"><b>' + esc(z.role) + '</b> <span class="bd-muted">' + esc(z.zone_name) + '</span> <span class="cf-pill cf-' + (SEVCLS[z.severity] || 'ok') + '">' + esc(z.status) + ' · ' + num(z.score, 0) + '</span> ' + badge('derived') + '</div>' +
          '<div class="bd-small">' + why + '<br>Occupancy: ' + esc(z.occupancy_state) + ' (' + num(z.occupancy_pct, 0) + ' %) ' + badge('sim') + ' · HVAC: ' + esc(hv.mode || '—') + ', cooling ' + num(hv.cooling_pct, 0) + ' %, fan ' + esc(hv.vent) + (hv.reason_code ? ', controller ' + esc(hv.reason_code) : '') +
          '<br><b>Recommended:</b> ' + esc(r.text) + (r.expected_effect ? '<br><b>Expected effect:</b> ' + esc(r.expected_effect) : '') + (r.energy_consideration ? '<br><b>Energy:</b> ' + esc(r.energy_consideration) : '') +
          (z.data_quality.missing.length ? '<br><span class="bd-err">Data quality: ' + esc(z.data_quality.missing.join(', ')) + ' unavailable</span>' : '') + '</div></div>';
      }).join('') : '<p class="bd-muted">No zone matches the filter.</p>';
      el('.cf-dur').innerHTML = '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Zone</th><th>Current</th><th>Today occupied</th><th>Today uncomfortable</th><th>%</th><th>Week %</th></tr></thead><tbody>' +
        snap.zones.map(function (z) { var d = z.durations; return '<tr><th scope="row">' + esc(z.role) + '</th><td>' + dur(d.current_discomfort_s) + '</td><td>' + dur(d.today.occupied_s) + '</td><td>' + dur(d.today.uncomfortable_s) + '</td><td>' + num(d.today.uncomfortable_pct, 1) + '</td><td>' + num(d.week.uncomfortable_pct, 1) + '</td></tr>'; }).join('') + '</tbody></table></div>' +
        '<p class="bd-foot">Accumulated from the live twin since the last reset (sim time). ' + badge('derived') + '</p>';
    }
    function zoneName(zid) { var z = (snap.all || snap).zones.filter(function (x) { return x.zone_id === zid; })[0]; return z ? z.role : zid; }

    function renderEvents() {
      el('.cf-events').innerHTML = events.length ? '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Event</th><th>Zone</th><th>Cause</th><th>Value / allowed</th><th>Occupancy · HVAC</th><th>Severity</th><th>Duration</th><th>Action</th></tr></thead><tbody>' +
        events.map(function (e) {
          var allowed = e.threshold[0] === null ? '≤ ' + e.threshold[1] : e.threshold[0] + '–' + e.threshold[1];
          return '<tr' + (e.status === 'open' ? ' class="flagged"' : '') + '><td>' + esc(e.event_id) + '<div class="bd-muted bd-small">' + esc(e.started_clock) + (e.resolved_clock ? ' → ' + esc(e.resolved_clock) : ' · open') + '</div></td>' +
            '<td>' + esc(e.zone_name) + '<div class="bd-muted bd-small">floor ' + esc(e.floor) + '</div></td><td>' + esc(e.event_type.replace('_', ' ')) + '</td>' +
            '<td>' + num(e.measured_value, e.unit === '°C' ? 1 : 0) + ' (peak ' + num(e.peak_value, e.unit === '°C' ? 1 : 0) + ') ' + esc(e.unit) + '<div class="bd-muted bd-small">allowed ' + esc(allowed) + '</div></td>' +
            '<td>' + esc(e.occupancy_state) + ' ' + num(e.occupancy_pct, 0) + ' %<div class="bd-muted bd-small">' + esc(e.hvac_state.mode) + ', cooling ' + num(e.hvac_state.cooling_pct, 0) + ' %, fan ' + esc(e.hvac_state.vent) + '</div></td>' +
            '<td>' + esc(e.severity) + '<div class="bd-muted bd-small">peak ' + esc(e.peak_severity) + '</div></td><td>' + dur(e.duration_s) + '</td>' +
            '<td class="bd-small">' + esc(e.recommended_action) + (e.resolution ? '<div class="bd-muted">' + esc(e.resolution) + '</div>' : '') + '</td></tr>';
        }).join('') + '</tbody></table></div>' : '<p class="bd-muted">No comfort events for this filter.</p>';
    }

    function renderTrend() {
      var box = el('.cf-trend'), lib = window.FLChart, P = hist.points || [];
      el('.cf-tnote').innerHTML = (hist.source === 'historical' ? badge('historical') : badge('sim') + ' score ' + badge('derived')) + (hist.partial ? ' · <b>partial</b>: only ' + dur(hist.covered_s) + ' of data in this range' : '');
      if (!P.length) { box.innerHTML = '<p class="cf-nodata">' + esc(hist.message || 'No data available') + (hist.hint ? ' — ' + esc(hist.hint) : '') + '</p>'; return; }
      if (!lib) { box.innerHTML = '<p class="bd-foot">Charts need /static/monitor.js.</p>'; return; }
      var fx = hist.source === 'historical' ? isoClock : simClock;
      var ticks = function (x0, x1) { var n = 5, out = []; for (var i = 0; i <= n; i++) { var x = x0 + (x1 - x0) * i / n; out.push({ x: x, label: fx(x).slice(hist.source === 'historical' ? 0 : 4) }); } return out; };
      var defs = [['Comfort score', 'comfort_score', '/100', { band: [75, 100], yMin: 0 }, '#2a78d6'], ['Temperature', 'temperature_c', '°C', {}, '#c2410c'],
        ['Humidity', 'humidity_pct', '%', {}, '#0d9488'], ['CO₂', 'co2_ppm', 'ppm', {}, '#7c5cd6'], ['Occupancy', 'occupancy', 'people', { yMin: 0 }, '#898781']];
      var built = defs.map(function (d) {
        var series = [{ name: d[0], color: d[4], unit: d[2], dec: d[2] === '°C' ? 1 : 0, pts: P.map(function (p) { return { x: p.t, y: p[d[1]] }; }) }];
        if (d[1] === 'comfort_score' && P.some(function (p) { return isNum(p.occupied_comfort_score); })) series.push({ name: 'Occupied zones only', color: '#2a78d6', dash: true, unit: '/100', dec: 0, pts: P.map(function (p) { return { x: p.t, y: p.occupied_comfort_score }; }) });
        var o = { series: series, h: 150, xTicks: ticks, empty: 'No data available' };
        Object.keys(d[3]).forEach(function (k) { o[k] = d[3][k]; });
        return { d: d, ch: lib.lineChart(o) };
      });
      box.innerHTML = '<div class="bd-dt-charts">' + built.map(function (b, i) { return '<div class="bd-mini" data-i="' + i + '"><div class="bd-kpi-h"><span>' + esc(b.d[0]) + ' <small class="bd-muted">' + esc(b.d[2]) + '</small></span></div>' + b.ch.svg + '</div>'; }).join('') + '</div><p class="bd-foot">' + esc(hist.note || '') + '</p>';
      if (tip) built.forEach(function (b, i) { var s = box.querySelector('.bd-mini[data-i="' + i + '"] svg'); if (s) lib.bindHover(s, b.ch, tip, fx); });
    }

    function runTrade() {
      var z = el('.cf-tzone').value, a = el('.cf-taction').value, h = el('.cf-th').value;
      el('.cf-trade').innerHTML = '<p class="bd-foot">Running two clone simulations…</p>';
      FL.get('/api/comfort/tradeoff?zone=' + encodeURIComponent(z) + '&action=' + a + '&horizon_h=' + h).then(function (t) {
        trade = t;
        var c = t.current, p = t.proposed, d = t.delta;
        function bar(v, max, cls) { return '<span class="cf-bar ' + cls + '" style="width:' + (isNum(v) && max > 0 ? Math.max(2, 100 * v / max) : 0) + '%"></span>'; }
        var emax = Math.max(c.energy_kwh || 0, p.energy_kwh || 0);
        el('.cf-trade').innerHTML = '<div class="cf-tgrid">' +
          '<div></div><b>Comfort (zone, /100)</b><b>Energy (building, kWh)</b>' +
          '<span>Current</span><div>' + bar(c.comfort_score, 100, 'cur') + ' ' + num(c.comfort_score, 1) + '</div><div>' + bar(c.energy_kwh, emax, 'cur') + ' ' + num(c.energy_kwh, 2) + '</div>' +
          '<span>Proposed: ' + esc(t.action_text) + '</span><div>' + bar(p.comfort_score, 100, 'prop') + ' ' + num(p.comfort_score, 1) + '</div><div>' + bar(p.energy_kwh, emax, 'prop') + ' ' + num(p.energy_kwh, 2) + '</div>' +
          '<span>Difference</span><b>' + (d.comfort_score > 0 ? '+' : '') + num(d.comfort_score, 1) + '</b><b>' + (d.energy_kwh > 0 ? '+' : '') + num(d.energy_kwh, 2) + ' kWh</b></div>' +
          '<p class="bd-foot">' + badge('predicted') + ' ' + esc(t.method) + ' Horizon ' + t.horizon_h + ' h · occupied steps ' + c.occupied_steps + ' · isolation verified: ' + t.isolation_verified + (t.note ? ' · ' + esc(t.note) : '') + '</p>';
      }, function (e) { el('.cf-trade').innerHTML = '<p class="bd-err">' + esc(err(e)) + '</p>'; });
    }
    return { mount: mount, update: update };
  }

  function injectCSS() {
    if (document.getElementById('cf-css')) return;
    var st = document.createElement('style'); st.id = 'cf-css';
    st.textContent = '.cf-floorrow{display:grid;grid-template-columns:62px 1fr;gap:8px;align-items:stretch;margin-bottom:8px;}' +
      '.cf-floorlbl{font-size:12px;color:var(--muted);padding-top:6px;}.cf-cells{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;}' +
      '.cf-cell{border:1px solid var(--border);border-radius:8px;padding:7px 9px;display:flex;flex-direction:column;gap:2px;cursor:pointer;font-size:12.5px;background:var(--page);}' +
      '.cf-cell small{color:var(--ink2);font-size:11px;}.cf-cell:hover{border-color:var(--ink2);}.cf-cell:focus-visible{outline:2px solid var(--us);outline-offset:2px;}' +
      '.cf-st{font-weight:600;}.cf-score{font-size:18px;font-weight:650;font-variant-numeric:tabular-nums;}' +
      '.cf-ok{background:rgba(0,99,0,.06);}.cf-low{background:rgba(250,178,25,.12);}.cf-med{background:rgba(250,178,25,.24);}' +
      '.cf-high{background:rgba(208,59,59,.14);}.cf-sev{background:rgba(208,59,59,.28);}.cf-info,.cf-empty{background:repeating-linear-gradient(45deg,var(--page),var(--page) 6px,var(--grid) 6px,var(--grid) 7px);}' +
      '.cf-dim{opacity:.35;cursor:default;}.cf-why{border-top:1px solid var(--grid);padding:7px 0;}.cf-why-h{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:3px;}' +
      '.cf-pill{font-size:11px;font-weight:600;border-radius:4px;padding:0 6px;border:1px solid var(--border);}' +
      '.cf-nodata{padding:18px;text-align:center;color:var(--ink2);border:1px dashed var(--border);border-radius:8px;}' +
      '.cf-tgrid{display:grid;grid-template-columns:minmax(120px,auto) 1fr 1fr;gap:6px 14px;align-items:center;font-size:13px;font-variant-numeric:tabular-nums;}' +
      '.cf-bar{display:inline-block;height:10px;border-radius:2px;vertical-align:middle;max-width:70%;}.cf-bar.cur{background:#898781;}.cf-bar.prop{background:#2a78d6;}' +
      '.cf .card h2 select{font:inherit;font-size:12px;border:1px solid var(--border);border-radius:6px;padding:2px 5px;background:var(--page);}';
    document.head.appendChild(st);
  }
  var booted = false;
  function boot() {
    if (booted) return true;
    var FL = window.FL; if (!FL || typeof FL.registerPanel !== 'function') return false;
    booted = true; injectCSS(); FL.registerPanel('comfort', comfortPanel()); return true;
  }
  if (!boot()) document.addEventListener('DOMContentLoaded', boot);
})();
