/* dashboard/building.js — the Building tab: commercial building digital-twin console.
 *
 * Binds to the window.FL shell contract (index.html) exactly like monitor.js and
 * panels.js, and lives in its own file so neither of those is rewritten. Zero
 * dependencies, no build step. Charts reuse the Monitor tab's SVG primitive
 * (window.FLChart, exported by monitor.js); without it the charts say so.
 *
 * WHERE EVERY NUMBER COMES FROM — the page renders, it never computes a KPI:
 *   GET  /api/building              config, operating mode, floors -> zones,
 *                                   zone cards, building demand, executive KPIs
 *   GET  /api/building/demand       hourly actual (telemetry) vs expected (profile)
 *   GET  /api/building/zones/{id}   drill-down: trends, decision + WHY, events
 *   GET  /api/building/profile      config + catalog (types, limits, modes)
 *   POST /api/building/profile      switch type / edit profile / operating mode
 *   POST /api/speed                 simulation speed (the shell's own endpoint)
 * Each value carries a source tag from the server; the badge shows it as
 * SIMULATED / DERIVED / PREDICTED / HARDWARE / REAL / HISTORICAL / CONFIGURED /
 * NOT MODELLED. The page never upgrades a simulated value to a real one.
 *
 * REFRESH. The shell polls /api/state at 1 Hz; this panel refetches the building
 * snapshot when the sim clock moved (at most ~1/s), the demand series every 15 s
 * or on a filter change, and an open zone drill-down every 3 s.
 */
(function () {
  'use strict';

  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return ESCAPES[c]; }); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function num(v, d) {
    if (!isNum(v)) return '—';
    d = d === undefined ? 1 : d;
    return v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  function simClock(t) {
    if (!isNum(t)) return '—';
    var s = Math.floor(t);
    return DAYS[Math.floor(s / 86400) % 7] + ' ' + String(Math.floor(s % 86400 / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0');
  }
  function hh(h) { return String(Math.floor(h)).padStart(2, '0') + ':' + String(Math.round((h % 1) * 60)).padStart(2, '0'); }

  var SRC = { sim: 'SIMULATED', derived: 'DERIVED', predicted: 'PREDICTED', historical: 'HISTORICAL', hardware: 'HARDWARE', real: 'REAL', config: 'CONFIGURED', none: 'NOT MODELLED' };
  var FLAG = { comfortable: 'Comfortable', warm: 'Warm', cold: 'Cold', high_co2: 'High CO₂', high_occupancy: 'High occupancy', hvac_active: 'HVAC active', warning: 'Warning', critical: 'Critical', unoccupied: 'Unoccupied' };
  var LEVEL = { low: 'Low', medium: 'Medium', high: 'High', very_high: 'Very high' };
  var MODE_LABEL = { normal: 'Normal', energy_saving: 'Energy saving', comfort_priority: 'Comfort priority', peak_demand_reduction: 'Peak demand reduction', emergency: 'Emergency', simulation: 'Simulation (dry run)' };
  var RANGES = [[6, '6 h'], [12, '12 h'], [24, '24 h'], [72, '3 d'], [168, '7 d']];
  var METRICS = [['power', 'HVAC power'], ['occ', 'Occupancy'], ['cool', 'Cooling load']];
  var ZWINDOWS = [['1h', '1 h'], ['6h', '6 h'], ['24h', '24 h'], ['7d', '7 d']];
  var SPEEDS = [1, 60, 240, 960, 3600];
  var C = { actual: '#2a78d6', expected: '#7c5cd6', alt: '#0d9488', warn: '#c2410c' };

  function srcBadge(k) { return '<span class="bd-src bd-src-' + esc(k) + '" title="Data state">' + esc(SRC[k] || String(k).toUpperCase()) + '</span>'; }
  function pill(st, text) {
    var icon = { normal: '●', warning: '▲', critical: '■' }[st] || '○';
    return '<span class="bd-st bd-st-' + esc(st) + '">' + icon + ' ' + esc(text || st) + '</span>';
  }
  function flagChip(f) { return '<span class="bd-flag bd-flag-' + esc(f) + '">' + esc(FLAG[f] || f) + '</span>'; }

  function buildingPanel() {
    var FL = window.FL, root = null, tip = null;
    var snap = null, profile = null, demand = null, detail = null;
    var floor = 'all', zone = 'all', range = 24, metric = 'power', zwin = '6h';
    var lastT = null, lastFetch = 0, lastDemand = 0, lastDetail = 0, busy = false, floorsKey = '';

    function el(s) { return root.querySelector(s); }
    function status(msg, bad) { var n = el('.bd-status'); if (n) { n.textContent = msg; n.classList.toggle('bad', !!bad); } }
    function errText(e) {
      var d = e && e.body && e.body.detail;
      if (d && d.errors) return d.errors.join('; ');
      return (e && e.message) || String(e);
    }
    function focused(n) { return n && document.activeElement === n; }

    // ---------------------------------------------------------------- mount
    function mount(node) {
      root = node;
      root.innerHTML =
        '<div class="bd">' +
        '<div class="bd-bar" role="group" aria-label="Building controls">' +
          '<label>Building type <select class="bd-type"></select></label>' +
          '<label>Floor <select class="bd-floorsel"><option value="all">All floors</option></select></label>' +
          '<label>Zone <select class="bd-zone"><option value="all">All zones</option></select></label>' +
          '<label>Operating mode <select class="bd-mode">' + Object.keys(MODE_LABEL).map(function (k) { return '<option value="' + k + '">' + esc(MODE_LABEL[k]) + '</option>'; }).join('') + '</select></label>' +
          '<label>Comfort priority <input type="range" class="bd-cp" min="0" max="100" step="5"> <output class="bd-cp-v">—</output></label>' +
          '<label>Energy priority <input type="range" class="bd-ep" min="0" max="100" step="5"> <output class="bd-ep-v">—</output></label>' +
          '<label>Simulation speed <select class="bd-speed">' + SPEEDS.map(function (s) { return '<option value="' + s + '">' + s + '×</option>'; }).join('') + '</select></label>' +
          '<span class="bd-status" role="status" aria-live="polite">loading building…</span>' +
        '</div>' +
        '<section class="card bd-live" aria-label="Current conditions"><h2>Current conditions <span class="bd-live-state"></span><span class="right bd-live-meta"></span></h2>' +
          '<div class="bd-live-notice bd-muted bd-small"></div><div class="bd-live-bldg"></div><div class="bd-live-zones"></div>' +
          '<div class="bd-main bd-live-lower"><div class="bd-live-health"></div><div class="bd-live-trend"></div></div>' +
          '<details class="bd-live-sensors"><summary>Sensor health</summary><div class="bd-live-sensorlist"><p class="bd-muted">Open to load.</p></div></details>' +
        '</section>' +
        '<div class="bd-head"></div>' +
        '<div class="bd-kpis" role="list" aria-label="Executive KPIs"></div>' +
        '<section class="card bd-detail" hidden aria-label="Zone detail"></section>' +
        '<div class="bd-main">' +
          '<section class="card bd-overview"><h2>Building overview <span class="right bd-ov-note">click a zone for detail</span></h2><div class="bd-tree"></div></section>' +
          '<section class="card bd-demand"><h2>Demand <span class="right bd-d-scope"></span></h2>' +
            '<div class="bd-dnow"></div>' +
            '<div class="bd-seg-row"><div class="bd-seg bd-ranges" role="group" aria-label="Time range">' + RANGES.map(function (r) { return '<button type="button" data-r="' + r[0] + '" class="' + (r[0] === range ? 'on' : '') + '">' + r[1] + '</button>'; }).join('') + '</div>' +
            '<div class="bd-seg bd-metrics" role="group" aria-label="Demand metric">' + METRICS.map(function (m) { return '<button type="button" data-m="' + m[0] + '" class="' + (m[0] === metric ? 'on' : '') + '">' + m[1] + '</button>'; }).join('') + '</div></div>' +
            '<div class="bd-dchart"></div><div class="bd-levels"></div><p class="bd-foot bd-dnote"></p>' +
          '</section>' +
        '</div>' +
        '<details class="card bd-config"><summary>Building configuration</summary><form class="bd-form" novalidate></form></details>' +
        '<p class="bd-foot bd-legend"></p>' +
        '</div>';
      if (window.FLChart) { tip = window.FLChart.tipFor(root); root.classList.add('mn-rel'); }

      el('.bd-type').onchange = function () { post({ building_type: this.value }, 'Switched building type'); };
      el('.bd-mode').onchange = function () { post({ operating_mode: this.value }, 'Operating mode applied'); };
      ['cp', 'ep'].forEach(function (k) {
        var inp = el('.bd-' + k);
        inp.oninput = function () { el('.bd-' + k + '-v').textContent = this.value; };
        inp.onchange = function () { post({ comfort_priority: +el('.bd-cp').value, energy_priority: +el('.bd-ep').value }, 'Priorities applied'); };
      });
      el('.bd-speed').onchange = function () {
        FL.post('/api/speed', { speed: +this.value }).then(function (r) { status('Simulation speed ' + r.speed + '×'); }, function (e) { status('speed change failed: ' + errText(e), true); });
      };
      el('.bd-floorsel').onchange = function () {
        floor = this.value;
        if (zone !== 'all' && snap && floor !== 'all' && !zoneOnFloor(zone, +floor)) selectZone('all');
        fillZoneOptions(); renderTree();
      };
      el('.bd-zone').onchange = function () { selectZone(this.value); };
      el('.bd-tree').addEventListener('click', function (e) { var z = e.target.closest('[data-zone]'); if (z) selectZone(z.dataset.zone); });
      el('.bd-tree').addEventListener('keydown', function (e) {
        var z = e.target.closest('[data-zone]');
        if (z && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); selectZone(z.dataset.zone); }
      });
      el('.bd-ranges').onclick = function (e) { var b = e.target.closest('button[data-r]'); if (!b) return; range = +b.dataset.r; segOn(this, b); fetchDemand(); };
      el('.bd-metrics').onclick = function (e) { var b = e.target.closest('button[data-m]'); if (!b) return; metric = b.dataset.m; segOn(this, b); renderDemandChart(); };
      el('.bd-detail').addEventListener('click', function (e) {
        if (e.target.closest('.bd-close')) { selectZone('all'); return; }
        var b = e.target.closest('button[data-w]'); if (b) { zwin = b.dataset.w; fetchDetail(); }
      });
      el('.bd-form').addEventListener('submit', function (e) { e.preventDefault(); saveForm(); });
      el('.bd-form').addEventListener('click', function (e) { if (e.target.closest('.bd-defaults')) resetDefaults(); });
      el('.bd-legend').innerHTML = 'Data states: ' + ['sim', 'derived', 'predicted', 'hardware', 'config', 'none'].map(srcBadge).join(' ') +
        ' — the twin models 5 zones of an office-like floor plate; a building profile changes how that state is evaluated and what is expected, never the physics.';

      // Phase 4: current conditions come from the shell's ONE /api/latest poll
      FL.on('latest', renderLive);
      if (FL.latest) renderLive(FL.latest);
      el('.bd-live-sensors').addEventListener('toggle', function () { if (this.open) loadSensors(); });

      // Comfort tab heatmap -> this tab's zone drill-down
      document.addEventListener('fl:select-zone', function (e) {
        if (e.detail && e.detail.zone) selectZone(e.detail.zone);
      });

      loadProfile();
      refresh(true);
    }

    function segOn(group, btn) { group.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === btn); }); }

    // ---------------------------------------------------------------- data
    function update(state) {
      if (!root) return;
      var sp = el('.bd-speed');
      if (state && state.sim && !focused(sp)) {
        var s = state.sim.speed;
        if (!sp.querySelector('option[value="' + s + '"]')) sp.insertAdjacentHTML('beforeend', '<option value="' + s + '">' + s + '×</option>');
        sp.value = String(s);
      }
      var t = state && state.sim ? state.sim.t : null;
      if ((t !== lastT || !snap) && Date.now() - lastFetch > 900) refresh(false);
      lastT = t;
    }

    function loadProfile() {
      return FL.get('/api/building/profile').then(function (j) {
        profile = j;
        var sel = el('.bd-type');
        sel.innerHTML = j.catalog.types.map(function (x) { return '<option value="' + esc(x.key) + '">' + esc(x.label) + '</option>'; }).join('');
        syncBar(); renderForm();
      }, function (e) { status('profile load failed: ' + errText(e), true); });
    }

    function refresh(force) {
      if (busy && !force) return;
      busy = true; lastFetch = Date.now();
      var now = Date.now(), jobs = [fetchSnap()];
      if (force || !demand || now - lastDemand > 15000) jobs.push(fetchDemand());
      if (zone !== 'all' && (force || now - lastDetail > 3000)) jobs.push(fetchDetail());
      Promise.all(jobs).then(function () {
        status('live · sim ' + (snap ? snap.sim_clock : '—') + ' · updated ' + new Date().toLocaleTimeString());
      }, function (e) { status('refresh failed: ' + errText(e), true); }).then(function () { busy = false; });
    }
    function fetchSnap() { return FL.get('/api/building').then(function (j) { snap = j; renderSnap(); }); }
    function fetchDemand() {
      lastDemand = Date.now();
      var z = zone, r = range;
      return FL.get('/api/building/demand?zone=' + encodeURIComponent(z) + '&hours=' + r + '&ahead_h=6').then(function (j) {
        if (z === zone && r === range) { demand = j; renderDemandChart(); }
      });
    }
    function fetchDetail() {
      if (zone === 'all') return Promise.resolve();
      lastDetail = Date.now();
      var z = zone, w = zwin;
      return FL.get('/api/building/zones/' + encodeURIComponent(z) + '?window=' + w).then(function (j) {
        if (z === zone && w === zwin) { detail = j; renderDetail(); }
      });
    }

    function post(body, okMsg) {
      status('applying…');
      return FL.post('/api/building/profile', body).then(function (r) {
        if (profile) { profile.config = r.config; profile.operating_mode = r.operating_mode; }
        var c = r.controller || {};
        status(okMsg + (r.levers_applied ? ' · controller objective ' + c.objective + ', safety mode ' + c.safety_mode : ''));
        formErrors([]);
        syncBar(); renderForm(); refresh(true);
        return r;
      }, function (e) {
        status('not applied: ' + errText(e), true);
        var d = e && e.body && e.body.detail;
        formErrors(d && d.errors ? d.errors : [errText(e)]);
        syncBar();
      });
    }

    function selectZone(z) {
      zone = z;
      el('.bd-zone').value = z;
      var panel = el('.bd-detail');
      if (z === 'all') { detail = null; panel.hidden = true; panel.innerHTML = ''; }
      else { panel.hidden = false; panel.innerHTML = '<p class="bd-foot">Loading zone detail…</p>'; fetchDetail().catch(function (e) { panel.innerHTML = '<p class="bd-err">' + esc(errText(e)) + '</p>'; }); }
      renderTree(); renderDemandNow(); fetchDemand().catch(function () {});
      if (z !== 'all') panel.scrollIntoView({ block: 'nearest' });
    }
    function zoneOnFloor(z, f) { return snap.zones.some(function (c) { return c.id === z && c.floor === f; }); }

    // ---------------------------------------------------------------- render: bar + header
    function syncBar() {
      var cfg = (snap && snap.config) || (profile && profile.config);
      if (!cfg) return;
      var t = el('.bd-type'); if (!focused(t) && t.options.length) t.value = cfg.building_type;
      var m = el('.bd-mode'); if (!focused(m)) m.value = cfg.operating_mode;
      [['cp', 'comfort_priority'], ['ep', 'energy_priority']].forEach(function (p) {
        var inp = el('.bd-' + p[0]);
        if (!focused(inp)) { inp.value = cfg[p[1]]; el('.bd-' + p[0] + '-v').textContent = Math.round(cfg[p[1]]); }
      });
    }

    function fillFloorOptions() {
      var floors = snap.topology.floors, key = floors.length + ':' + snap.config.building_type;
      if (key === floorsKey) return;
      floorsKey = key;
      var sel = el('.bd-floorsel');
      sel.innerHTML = '<option value="all">All floors</option>' + floors.filter(function (f) { return f.modelled; }).map(function (f) { return '<option value="' + f.floor + '">' + esc(f.label) + '</option>'; }).join('');
      if (floor !== 'all' && !floors.some(function (f) { return String(f.floor) === floor && f.modelled; })) floor = 'all';
      sel.value = floor;
      fillZoneOptions();
    }
    function fillZoneOptions() {
      var sel = el('.bd-zone');
      var zs = snap.zones.filter(function (c) { return floor === 'all' || c.floor === +floor; });
      sel.innerHTML = '<option value="all">All zones</option>' + zs.map(function (c) { return '<option value="' + esc(c.id) + '">' + esc(c.role) + ' (' + esc(c.name) + ')</option>'; }).join('');
      sel.value = zs.some(function (c) { return c.id === zone; }) ? zone : 'all';
    }

    function renderSnap() {
      if (!snap) return;
      fillFloorOptions();
      var zsel = el('.bd-zone');
      if (zsel.options.length - 1 !== snap.zones.filter(function (c) { return floor === 'all' || c.floor === +floor; }).length) fillZoneOptions();
      syncBar(); renderHead(); renderKpis(); renderTree(); renderDemandNow();
    }

    function renderHead() {
      var cfg = snap.config, om = snap.operating_mode, topo = snap.topology, d = snap.demand;
      var days = cfg.open_days.length === 7 ? 'every day' : cfg.open_days.map(function (x) { return DAYS[x]; }).join(', ');
      var hours = (cfg.open_hour === 0 && cfg.close_hour === 24) ? '24 h' : hh(cfg.open_hour) + '–' + hh(cfg.close_hour);
      var sync = om.in_sync ? '' :
        '<div class="bd-warn" role="note">The controller no longer matches this mode (objective ' + esc(om.controller_objective) + ', safety ' + esc(om.controller_safety_mode) + ' — changed on the Control tab). Re-select the mode to re-apply it.</div>';
      el('.bd-head').innerHTML =
        '<div class="bd-title"><h2>' + esc(cfg.name) + '</h2><span class="bd-typelbl">' + esc(cfg.type_label) + '</span>' + srcBadge('config') +
          '<span class="bd-level">Demand now <b class="bd-lv bd-lv-' + esc(d.level) + '">' + esc(LEVEL[d.level]) + '</b> · expected at this hour <b class="bd-lv bd-lv-' + esc(d.expected_level) + '">' + esc(LEVEL[d.expected_level]) + '</b> · ' + (d.is_open ? 'open' : 'outside operating hours') + '</span></div>' +
        '<div class="bd-facts">' +
          '<span><b>' + cfg.floors + '</b> floors</span><span><b>' + cfg.zones + '</b> zones configured · <b>' + topo.modelled_zones + '</b> modelled</span>' +
          '<span>capacity <b>' + num(cfg.occupancy_capacity, 0) + '</b> people</span><span>HVAC design <b>' + num(cfg.hvac_capacity_w, 0) + '</b> W</span>' +
          '<span>open <b>' + esc(hours) + '</b>, ' + esc(days) + '</span>' +
          '<span>comfort <b>' + num(cfg.comfort_min_c) + '–' + num(cfg.comfort_max_c) + ' °C</b></span>' +
          '<span>RH <b>' + num(cfg.humidity_min_pct, 0) + '–' + num(cfg.humidity_max_pct, 0) + ' %</b></span>' +
          '<span>CO₂ ≤ <b>' + num(cfg.co2_max_ppm, 0) + ' ppm</b></span>' +
        '</div>' +
        '<div class="bd-modeline">Mode <b>' + esc(MODE_LABEL[om.key]) + '</b> → objective <b>' + esc(om.objective) + '</b>, safety mode <b>' + esc(om.safety_mode) + '</b><span class="bd-muted"> · ' + esc(om.description) + '</span></div>' +
        sync +
        (cfg.model_fit_note ? '<div class="bd-muted bd-fit">Model fit: ' + esc(cfg.model_fit_note) + '</div>' : '');
    }

    // ---------------------------------------------------------------- render: KPIs
    function kpiValue(key, it) {
      var v = it.value;
      switch (key) {
        case 'current_energy': case 'peak_demand': return [num(v, 0), 'W'];
        case 'today_energy': return [num(v, 2), 'kWh'];
        case 'hvac_load': case 'energy_saving': return [num(v, 1), '%'];
        case 'occupancy': return [num(v, 0), 'people'];
        case 'avg_temperature': return [num(v, 1), '°C'];
        case 'avg_humidity': return [num(v, 1), '%'];
        case 'avg_co2': return [num(v, 0), 'ppm'];
        case 'comfort_score': return [num(v, 0), '/100'];
        case 'active_alerts': return [num(v, 0), ''];
        case 'system_health': return [String(v || '—').toUpperCase(), ''];
        default: return [isNum(v) ? num(v) : esc(v), it.unit || ''];
      }
    }
    function kpiSub(key, it) {
      if (key === 'occupancy') return num(it.pct, 1) + '% of modelled design headcount';
      if (key === 'peak_demand') return it.t !== null && it.t !== undefined ? 'at ' + simClock(it.t) : 'no samples today';
      if (key === 'active_alerts') { var b = it.breakdown || {}; return b.maintenance + ' maintenance · ' + b.zone_warning + ' warning · ' + b.zone_critical + ' critical zones'; }
      if (key === 'system_health') { var bad = (it.checks || []).filter(function (c) { return !c.ok; }); return bad.length ? bad.map(function (c) { return c.name + ': ' + c.detail; }).join('; ') : 'all checks passing'; }
      return it.note || '';
    }
    function renderKpis() {
      var k = snap.kpis;
      el('.bd-kpis').innerHTML = k.order.map(function (key) {
        var it = k.items[key], v = kpiValue(key, it);
        return '<div class="bd-kpi bd-k-' + esc(it.status) + '" role="listitem">' +
          '<div class="bd-kpi-h"><span>' + esc(it.label) + '</span>' + srcBadge(it.source) + '</div>' +
          '<div class="bd-kpi-v">' + esc(v[0]) + (v[1] ? '<span class="bd-unit">' + esc(v[1]) + '</span>' : '') + '</div>' +
          '<div class="bd-kpi-s">' + esc(kpiSub(key, it)) + '</div>' +
          '<div class="bd-kpi-f">' + pill(it.status === 'unknown' ? 'unknown' : it.status, it.status) + '</div></div>';
      }).join('');
    }

    // ---------------------------------------------------------------- render: tree
    function renderTree() {
      if (!snap) return;
      var floors = snap.topology.floors.filter(function (f) { return floor === 'all' || String(f.floor) === floor; });
      var out = [], gap = null;
      function flushGap() {
        if (!gap) return;
        out.push('<div class="bd-floor bd-floor-empty"><div class="bd-floor-h"><b>' + esc(gap.a === gap.b ? gap.la : gap.la + ' – ' + gap.lb) + '</b>' + srcBadge('none') + '</div>' +
          '<p class="bd-muted">Not modelled by the digital twin — no values are shown for these floors.</p></div>');
        gap = null;
      }
      floors.forEach(function (f) {
        if (!f.modelled) { if (gap) { gap.b = f.floor; gap.lb = f.label; } else gap = { a: f.floor, b: f.floor, la: f.label, lb: f.label }; return; }
        flushGap();
        var cards = snap.zones.filter(function (c) { return c.floor === f.floor; });
        out.push('<div class="bd-floor"><div class="bd-floor-h"><b>' + esc(f.label) + '</b><span class="bd-muted">' + cards.length + ' modelled zone' + (cards.length === 1 ? '' : 's') + '</span>' + pill(f.status, f.status) + '</div>' +
          '<div class="bd-zones">' + cards.map(zoneCard).join('') + '</div></div>');
      });
      flushGap();
      el('.bd-tree').innerHTML =
        '<div class="bd-muted bd-tree-note">' + esc(snap.topology.name) + ' · ' + esc(snap.topology.note) + ' Temperature, humidity and occupancy are ' + srcBadge('sim') + '; CO₂ (estimated), comfort score and power are ' + srcBadge('derived') + '.</div>' + out.join('');
    }
    function zoneCard(c) {
      var hv = c.hvac, dm = c.demand, sel = c.id === zone;
      var flags = c.flags.filter(function (f) { return f !== 'warning' && f !== 'critical'; });
      return '<div class="bd-zonecard bd-z-' + esc(c.severity) + (sel ? ' sel' : '') + '" data-zone="' + esc(c.id) + '" role="button" tabindex="0" aria-pressed="' + sel + '" aria-label="' + esc(c.role + ', ' + c.status) + '">' +
        '<div class="bd-zc-h"><b>' + esc(c.role) + '</b>' + pill(c.severity, FLAG[c.status] || c.status) + '</div>' +
        '<div class="bd-zc-sub">' + esc(c.name) + ' · twin zone ' + esc(c.id) + '</div>' +
        '<div class="bd-zc-temp">' + num(c.temp.value) + '<span class="bd-unit">°C</span><span class="bd-zc-rh">' + num(c.humidity.value, 0) + '% RH</span></div>' +
        '<div class="bd-zc-grid">' +
          '<span>Occupancy</span><b>' + num(c.occupancy.value, 0) + ' <small>(' + num(c.occupancy.pct, 0) + '%)</small></b>' +
          '<span>CO₂ est.</span><b>' + num(c.co2.value, 0) + ' <small>ppm</small></b>' +
          '<span>HVAC</span><b>' + (hv.setpoint === null || hv.setpoint === undefined ? 'off' : num(hv.setpoint) + ' °C') + ' · fan ' + esc(hv.vent) + '</b>' +
          '<span>Power</span><b>' + num(c.energy.power_w.value, 0) + ' <small>W</small></b>' +
          '<span>Demand</span><b>' + esc(LEVEL[dm.level]) + ' <small>(exp. ' + esc(LEVEL[dm.expected_level]) + ')</small></b>' +
          '<span>Comfort</span><b>' + num(c.comfort_score.value, 0) + ' <small>/100</small></b>' +
        '</div>' +
        '<div class="bd-flags">' + flags.map(flagChip).join('') + (c.active_constraints ? '<span class="bd-flag">' + c.active_constraints + ' complaint' + (c.active_constraints > 1 ? 's' : '') + '</span>' : '') +
          (c.hardware ? '<span class="bd-flag bd-flag-hw">rig ' + num(c.hardware.temp_c) + ' °C ' + srcBadge('hardware') + '</span>' : '') + '</div>' +
        '</div>';
    }

    // ---------------------------------------------------------------- render: demand
    function renderDemandNow() {
      if (!snap) return;
      var card = zone === 'all' ? null : snap.zones.filter(function (c) { return c.id === zone; })[0];
      var d = card ? card.demand : snap.demand;
      el('.bd-d-scope').textContent = card ? card.role + ' (' + card.name + ')' : 'whole building · modelled zones';
      function row(label, cur, exp, src, note) {
        return '<tr><th scope="row">' + esc(label) + '</th><td>' + cur + '</td><td>' + (exp || '<span class="bd-muted">—</span>') + '</td><td>' + srcBadge(src) + (note ? '<div class="bd-muted bd-small">' + esc(note) + '</div>' : '') + '</td></tr>';
      }
      var v = d.ventilation_demand, cd = d.comfort_demand;
      var rows = [
        row('Occupancy', num(d.current_occupancy.value, 0) + ' people <small>(' + num(d.current_occupancy.pct, 0) + '%)</small>',
            (card ? num(d.expected_occupancy.value, 0) + ' people ' : num(d.expected_occupancy.value, 0) + ' of capacity ') + '<small>(' + num(d.expected_occupancy.pct, 0) + '%)</small> ' + srcBadge('predicted'), 'sim'),
        row('HVAC demand', num(d.hvac_demand.value, 0) + ' W <small>(' + num(d.hvac_demand.pct, 0) + '% of design)</small>', num(d.expected_hvac_demand.value, 0) + ' W ' + srcBadge('predicted'), 'derived'),
        row('Cooling demand', num(d.cooling_demand.value, 0) + ' W thermal', '', 'derived'),
        row('Heating demand', '<span class="bd-muted">not modelled</span>', '', 'none', d.heating_demand.note),
        row('Ventilation demand', num(v.value, 0) + ' L/s required', num(v.supplied, 0) + ' L/s supplied ' + (v.met ? pill('normal', 'met') : pill('warning', 'short')), 'derived'),
        row('Lighting demand', num(d.lighting_demand.value, 0) + ' W', '', 'derived', 'estimate; not in the twin\'s kWh'),
        row('Estimated energy demand', num(d.energy_demand.value, 0) + ' W', '', 'derived', 'HVAC + lighting estimate'),
        row('Comfort demand', cd.value > 0 ? num(cd.value, 1) + ' °C outside range' : 'within range', card ? '' : num(cd.zones_outside, 0) + ' of ' + num(cd.occupied_zones, 0) + ' occupied zones outside', 'derived')
      ];
      if (!card) rows.push(row('Peak demand today', num(d.peak_demand.value, 0) + ' W', d.peak_demand.t !== null && d.peak_demand.t !== undefined ? 'at ' + simClock(d.peak_demand.t) : '', 'derived'));
      el('.bd-dnow').innerHTML = '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th scope="col">Demand</th><th scope="col">Current</th><th scope="col">Expected / context</th><th scope="col">Data state</th></tr></thead><tbody>' + rows.join('') + '</tbody></table></div>';
    }

    function renderDemandChart() {
      var box = el('.bd-dchart');
      if (!demand) { box.innerHTML = '<p class="bd-foot">Loading demand…</p>'; return; }
      var lib = window.FLChart;
      if (!lib) { box.innerHTML = '<p class="bd-foot">Charts need /static/monitor.js, which did not load. The demand table above is unaffected.</p>'; return; }
      var P = demand.points;
      function pts(k) { return P.map(function (p) { return { x: p.t + 1800, y: p[k] }; }); }
      var series, unit, dec = 0;
      if (metric === 'occ') {
        unit = '%';
        series = [{ name: 'Actual occupancy (simulated)', color: C.actual, pts: pts('actual_occ_pct'), unit: '%', dec: 0 },
                  { name: 'Expected (profile)', color: C.expected, pts: pts('expected_occ_pct'), unit: '%', dec: 0, dash: true }];
      } else if (metric === 'cool') {
        unit = 'W';
        series = [{ name: 'Cooling load (derived)', color: C.alt, pts: pts('actual_cool_w'), unit: 'W', dec: 0 }];
      } else {
        unit = 'W';
        series = [{ name: 'HVAC power, hourly mean (derived)', color: C.actual, pts: pts('actual_power_w'), unit: 'W', dec: 0 },
                  { name: 'Hourly peak (derived)', color: C.warn, pts: pts('peak_power_w'), unit: 'W', dec: 0, opacity: 0.55 },
                  { name: 'Expected HVAC (profile)', color: C.expected, pts: pts('expected_hvac_w'), unit: 'W', dec: 0, dash: true }];
      }
      var ch = lib.lineChart({ series: series, h: 210, yMin: 0, xTicks: lib.simTicks, nowX: demand.now_t, empty: 'No telemetry in this range yet.' });
      box.innerHTML = '<div class="bd-legend-row">' + series.map(function (s) { return '<span><span class="bd-sw' + (s.dash ? ' dash' : '') + '" style="border-color:' + s.color + '"></span>' + esc(s.name) + '</span>'; }).join('') + '<span class="bd-muted">unit ' + esc(unit) + ' · dashed after “now” = expected only</span></div>' + ch.svg;
      var svg = box.querySelector('svg');
      if (svg && tip) lib.bindHover(svg, ch, tip, function (x) { return simClock(x - 1800) + ' hour'; });
      el('.bd-levels').innerHTML = '<div class="bd-muted bd-small">Expected demand level by hour (profile schedule) ' + srcBadge('predicted') + '</div><div class="bd-lvstrip" role="img" aria-label="Expected demand level per hour">' +
        P.map(function (p) { return '<span class="bd-lvcell bd-lv-' + esc(p.expected_level) + (p.future ? ' fut' : '') + '" title="' + esc(DAYS[p.day] + ' ' + hh(p.hour) + ' · expected ' + LEVEL[p.expected_level] + ' · ' + (p.is_open ? 'open' : 'closed') + (p.samples ? ' · ' + p.samples + ' samples' : ' · no samples')) + '"></span>'; }).join('') + '</div>';
      el('.bd-dnote').textContent = demand.note;
    }

    // ---------------------------------------------------------------- render: zone detail
    function renderDetail() {
      var panel = el('.bd-detail');
      if (!detail) return;
      var c = detail.zone, x = detail.explanation, hv = c.hvac, cfg = detail.config;
      function tile(label, val, src, sub) { return '<div class="bd-tile"><div class="bd-kpi-h"><span>' + esc(label) + '</span>' + srcBadge(src) + '</div><div class="bd-tile-v">' + val + '</div>' + (sub ? '<div class="bd-kpi-s">' + sub + '</div>' : '') + '</div>'; }
      var tiles = [
        tile('Temperature', num(c.temp.value) + ' °C', 'sim', 'target ' + num(cfg.comfort_c[0]) + '–' + num(cfg.comfort_c[1]) + ' °C'),
        tile('Humidity', num(c.humidity.value, 0) + ' %', 'sim', 'range ' + num(cfg.humidity_pct[0], 0) + '–' + num(cfg.humidity_pct[1], 0) + ' %'),
        tile('Occupancy', num(c.occupancy.value, 0) + ' <small>(' + num(c.occupancy.pct, 0) + '%)</small>', 'sim', 'expected ' + num(c.demand.expected_occupancy.pct, 0) + '% (profile)'),
        tile('CO₂ (estimated)', num(c.co2.value, 0) + ' ppm', 'derived', 'threshold ' + num(cfg.co2_ppm, 0) + ' ppm'),
        tile('Comfort score', num(c.comfort_score.value, 0) + ' /100', 'derived', 'against the profile range'),
        tile('HVAC demand', num(c.demand.hvac_demand.value, 0) + ' W', 'derived', num(hv.capacity_pct, 0) + '% of cooling capacity' + (hv.at_capacity ? ' · at capacity' : '')),
        tile('Energy', num(c.energy.kwh.value, 2) + ' kWh', 'sim', 'since simulation start'),
        tile('Setpoint / fan', (hv.setpoint === null || hv.setpoint === undefined ? 'off' : num(hv.setpoint) + ' °C') + ' · ' + esc(hv.vent), 'sim', hv.locked_out ? 'maintenance lockout' : '')
      ];
      var factors = x.factors.map(function (f) {
        return '<tr' + (f.flag ? ' class="flagged"' : '') + '><th scope="row">' + esc(f.label) + '</th><td>' + esc(isNum(f.value) ? num(f.value, f.unit === 'ppm' || f.unit === '%' ? 0 : 1) : f.value) + ' ' + esc(f.unit) + '</td><td>' + srcBadge(f.source) + '</td><td>' + (f.flag ? pill('warning', f.flag) : '') + '</td></tr>';
      }).join('');
      var cons = x.constraint && x.constraint.adjustment ? esc(x.constraint.summary) : 'No active occupant constraint in this zone.';
      var alerts = detail.alerts.maintenance.map(function (a) { return '<li>' + pill(a.severity === 'high' ? 'critical' : 'warning', a.severity) + ' <b>' + esc(a.kind) + '</b> — ' + esc((a.evidence || []).join('; ')) + '<div class="bd-muted bd-small">' + esc(a.recommendation) + '</div></li>'; })
        .concat(detail.alerts.thresholds.map(function (a) { return '<li>' + pill(a.status, a.status) + ' ' + esc(a.metric) + ' ' + esc(isNum(a.value) ? num(a.value, 1) : a.value) + ' ' + esc(a.unit) + ' ' + srcBadge(a.source) + (a.note ? '<div class="bd-muted bd-small">' + esc(a.note) + '</div>' : '') + '</li>'; }));
      var events = detail.events.decisions.map(function (d) { return '<li><b>' + esc(d.sim_clock) + '</b> decision <code>' + esc(d.reason_code) + '</code> — ' + esc(d.summary) + '</li>'; })
        .concat(detail.events.feed.map(function (e) { return '<li><b>' + esc(e.sim_clock) + '</b> occupant “' + esc(e.text) + '” → ' + esc(String(e.action || '').split(' ')[0]) + '</li>'; }));

      panel.innerHTML =
        '<div class="bd-dt-h"><h2>' + esc(c.role) + ' <span class="bd-muted">' + esc(c.name) + ' · ' + esc(detail.floor.label) + ' · sim ' + esc(detail.sim_clock) + '</span></h2>' + pill(c.severity, FLAG[c.status] || c.status) +
          '<button type="button" class="bd-close" aria-label="Close zone detail">Close</button></div>' +
        '<div class="bd-flags">' + c.flags.map(flagChip).join('') + '</div>' +
        '<div class="bd-tiles">' + tiles.join('') + '</div>' +
        comfortBlock(detail.comfort) +
        '<div class="bd-dt-grid">' +
          '<div class="bd-why"><h3>Why the HVAC is doing this</h3>' +
            '<p class="bd-why-s">' + esc(x.sentence) + '</p>' +
            '<p><b>Current action:</b> ' + esc(x.action) + ' · reason <code>' + esc(x.reason_code) + '</code>' + (x.objective ? ' · ' + esc(x.objective) + ' / ' + esc(x.safety_mode) : '') +
              (isNum(x.est_energy_delta_pct) ? ' · est. energy ' + (x.est_energy_delta_pct > 0 ? '+' : '') + num(x.est_energy_delta_pct) + '%, comfort ' + (x.est_comfort_delta_pct > 0 ? '+' : '') + num(x.est_comfort_delta_pct) + '% vs base schedule' : '') + '</p>' +
            '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th scope="col">Factor</th><th scope="col">Value</th><th scope="col">Data state</th><th scope="col">Flag</th></tr></thead><tbody>' + factors + '</tbody></table></div>' +
            '<p class="bd-small"><b>Controller record:</b> ' + esc(x.decision_summary) + '</p>' +
            '<p class="bd-small"><b>Current constraint:</b> ' + cons + '</p>' +
          '</div>' +
          '<div class="bd-lists"><h3>Active alerts</h3>' + (alerts.length ? '<ul>' + alerts.join('') + '</ul>' : '<p class="bd-muted">No active alerts for this zone.</p>') +
            '<h3>Recent events</h3>' + (events.length ? '<ul>' + events.join('') + '</ul>' : '<p class="bd-muted">No controller events or occupant messages for this zone yet.</p>') + '</div>' +
        '</div>' +
        '<div class="bd-seg-row"><span class="bd-muted">Trends</span><div class="bd-seg" role="group" aria-label="Trend window">' + ZWINDOWS.map(function (w) { return '<button type="button" data-w="' + w[0] + '" class="' + (w[0] === zwin ? 'on' : '') + '">' + w[1] + '</button>'; }).join('') + '</div>' +
          '<span class="bd-muted bd-small">' + detail.trends.count_raw + ' step samples</span></div>' +
        '<div class="bd-dt-charts"></div>';
      renderTrends(panel.querySelector('.bd-dt-charts'), cfg);
    }

    /* ---------------------------------------------------------------- Phase 4: current conditions */
    var liveTicks = 0, liveTrendZone = 'zone_a';
    function ago(s) { if (!isNum(s)) return '—'; return s < 60 ? Math.round(s) + ' s ago' : s < 3600 ? Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s ago' : Math.floor(s / 3600) + ' h ago'; }
    function liveCell(v, d, unit) {
      if (!v) return '<span class="bd-err">Unavailable</span>';
      var qual = v.quality || 'missing';
      var val = v.value === null || v.value === undefined
        ? '<span class="' + (v.note ? 'bd-muted' : 'bd-err') + '">' + esc(v.note || v.display || v.message || 'Unavailable') + '</span>'
        : (typeof v.value === 'number' ? esc(num(v.value, d)) : esc(v.value)) + (unit !== '' ? ' <small>' + esc(unit === undefined ? v.unit : unit) + '</small>' : '');
      var qcls = qual === 'good' || qual === 'estimated' ? '' : ' bd-q-' + qual;
      return '<div class="bd-lv-cell' + qcls + '">' + val + '<div class="bd-lv-meta">' + (v.source ? srcBadge(v.source) : '') +
        (qual !== 'good' ? ' <span class="bd-lv-q' + (qual === 'estimated' ? ' bd-lv-q-est' : '') + '">' + esc(qual.toUpperCase()) + '</span>' : '') +
        (isNum(v.age_s) ? ' ' + esc(ago(v.age_s)) : '') + '</div></div>';
    }
    function renderLive(j) {
      if (!root) return;
      var state = el('.bd-live-state');
      if (!j) {
        state.innerHTML = '<span class="bd-st bd-st-critical">● OFFLINE — backend unreachable; values below are the last received</span>';
        return;
      }
      var h = j.system_health, dq = j.data_quality;
      var cls = { 'LIVE TELEMETRY': 'normal', 'SIMULATION MODE': 'warning', 'DEGRADED': 'critical', 'STALE DATA': 'critical', 'OFFLINE': 'critical' }[h.status] || 'unknown';
      state.innerHTML = ' ' + pill(cls, h.status) + (h.status === 'SIMULATION MODE' ? ' <span class="bd-src bd-src-sim">LIVE SIMULATION</span>' : '');
      el('.bd-live-meta').textContent = 'sim ' + j.sim_clock + ' · polled every ' + j.poll_interval_s + ' s · ' + j.timestamp.replace('T', ' ').slice(0, 19) + ' UTC';
      el('.bd-live-notice').textContent = j.notice;
      var b = j.building.metrics;
      function bm(label, v, d, extra) { return '<div class="bd-tile"><div class="bd-kpi-h"><span>' + esc(label) + '</span></div><div class="bd-tile-v">' + liveCell(v, d) + '</div>' + (extra ? '<div class="bd-kpi-s">' + extra + '</div>' : '') + '</div>'; }
      el('.bd-live-bldg').innerHTML = '<div class="bd-tiles">' +
        bm('Current power', b.power, 0, 'instantaneous') + bm("Today's energy", b.energy_today, 2, esc(b.energy_today.note || 'accumulated')) +
        bm('Demand now', b.demand, 0) + bm('Expected demand', b.expected_demand, 0, 'profile schedule') +
        bm('Occupancy', b.occupancy, 0) + bm('Building comfort', b.comfort_score, 0, 'occupied zones') + bm('Outdoor', b.outdoor_temperature, 1) + '</div>';
      el('.bd-live-zones').innerHTML = '<div class="bd-tablewrap"><table class="bd-table bd-lv-table"><thead><tr><th>Zone</th><th>Temperature</th><th>Humidity</th><th>CO₂</th><th>Occupancy</th><th>HVAC</th><th>Power</th><th>Comfort</th></tr></thead><tbody>' +
        j.zones.map(function (z) {
          var hv = z.hvac, c = z.comfort;
          var hvac = liveCell(hv.hvac_mode, 0, '') + '<div class="bd-muted bd-small">cool ' + (hv.cooling_pct.value === null ? '—' : num(hv.cooling_pct.value, 0) + ' %') + ' · fan ' + (hv.fan_level.value === null ? '—' : hv.fan_level.value) +
            ' · set ' + (hv.setpoint.value === null ? (hv.setpoint.note ? 'off' : '—') : num(hv.setpoint.value, 1) + ' °C') + '<br>' + esc(hv.controller_action.value || '') + '</div>';
          var cm = liveCell(c.score, 0, '/100') + '<div class="bd-muted bd-small">' + esc(c.status.value || '') +
            (c.stale_inputs.length ? '<br><span class="bd-err">stale input: ' + esc(c.stale_inputs.join(', ')) + '</span>' : '') +
            (c.missing_inputs.length ? '<br><span class="bd-err">missing: ' + esc(c.missing_inputs.join(', ')) + '</span>' : '') + '</div>';
          var alt = z.alternatives && z.alternatives.temperature ? '<div class="bd-muted bd-small">also: ' + z.alternatives.temperature.filter(function (a) { return a.sensor_id !== z.temperature.sensor_id; }).map(function (a) { return esc((a.value === null ? 'n/a' : num(a.value, 1) + ' °C') + ' ' + (a.source || '').toUpperCase() + ' ' + ago(a.age_s)); }).join('; ') + '</div>' : '';
          return '<tr><th scope="row">' + esc(z.name) + '<div class="bd-muted bd-small">' + esc(z.zone_id + ' · ' + z.floor_id) + '</div></th><td>' + liveCell(z.temperature, 1) + alt + '</td><td>' + liveCell(z.humidity, 0) + '</td><td>' + liveCell(z.co2, 0) + '</td><td>' + liveCell(z.occupancy, 0) +
            '<div class="bd-muted bd-small">' + (z.occupancy_pct.value === null ? '' : num(z.occupancy_pct.value, 0) + ' % of design') + '</div></td><td>' + hvac + '</td><td>' + liveCell(z.energy.power, 0) + '</td><td>' + cm + '</td></tr>';
        }).join('') + '</tbody></table></div>';
      var S = j.sources;
      el('.bd-live-health').innerHTML = '<h3 class="bd-h3">Telemetry health</h3><div class="bd-lv-health">' +
        [['Healthy', dq.healthy], ['Aging', dq.aging], ['Stale', dq.stale], ['Invalid', dq.invalid], ['Missing', dq.missing]].map(function (x) { return '<span><b>' + x[1] + '</b> ' + x[0] + '</span>'; }).join('') +
        '<span>Last update <b>' + esc(dq.last_update_age_s === null ? '—' : dq.last_update_age_s + ' s ago') + '</b></span></div>' +
        '<p class="bd-small">' + esc(h.reasons.join('; ')) + '</p>' +
        '<h3 class="bd-h3">Data sources</h3><div class="bd-lv-src">' + Object.keys(S).map(function (k) { return '<span>' + esc(k) + ': ' + (S[k] ? S[k].split('+').map(srcBadge).join(' ') : '<span class="bd-err">none</span>') + '</span>'; }).join('') + '</div>' +
        '<p class="bd-foot">Rules: good ≤ 60 s, aging ≤ 300 s, stale after 300 s (wall clock). Stale values keep their value and say so.</p>';
      if (liveTicks++ % 3 === 0) loadTrend();
    }
    function loadTrend() {
      var z = zone !== 'all' ? zone : liveTrendZone;
      FL.get('/api/latest/trend?zone=' + encodeURIComponent(z) + '&minutes=15').then(function (t) {
        var lib = window.FLChart, box = el('.bd-live-trend');
        if (!lib) { box.innerHTML = ''; return; }
        var defs = [['temperature', 'Temperature', '°C', 1, '#c2410c'], ['hvac_power', 'HVAC power', 'W', 0, '#2a78d6'], ['occupancy_pct', 'Occupancy', '%', 0, '#898781'], ['comfort_score', 'Comfort', '/100', 0, '#0d9488']];
        var charts = defs.map(function (d) {
          var series = (t.series[d[0]] || []).map(function (s) { return { name: d[1] + ' (' + s.source + ')', color: s.source === 'hardware' ? '#7c5cd6' : d[4], unit: d[2], dec: d[3], pts: s.points.map(function (p) { return { x: p.t_wall, y: p.value }; }) }; });
          return { d: d, ch: lib.lineChart({ series: series, h: 110, empty: 'No current data available', xTicks: function (a, b) { return [{ x: a, label: '−' + Math.round((b - a) / 60) + ' min' }, { x: b, label: 'now' }]; } }) };
        });
        box.innerHTML = '<h3 class="bd-h3">Live trend · ' + esc(z) + ' <span class="bd-src bd-src-sim">' + esc(t.label) + '</span></h3><div class="bd-live-charts">' +
          charts.map(function (c, i) { return '<div class="bd-mini" data-i="' + i + '"><div class="bd-kpi-h"><span>' + esc(c.d[1]) + ' <small>' + esc(c.d[2]) + '</small></span></div>' + c.ch.svg + '</div>'; }).join('') + '</div><p class="bd-foot">In-memory buffer of the last 15 minutes (wall clock) — not history.</p>';
        if (tip) charts.forEach(function (c, i) { var s = box.querySelector('.bd-mini[data-i="' + i + '"] svg'); if (s) lib.bindHover(s, c.ch, tip, function (x) { return new Date(x * 1000).toLocaleTimeString(); }); });
      }, function () { el('.bd-live-trend').innerHTML = '<p class="bd-err">Live trend unavailable.</p>'; });
    }
    function loadSensors() {
      FL.get('/api/latest/sensors').then(function (s) {
        el('.bd-live-sensorlist').innerHTML = '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Sensor</th><th>Metric</th><th>Zone</th><th>Source</th><th>Last seen</th><th>Age</th><th>Quality</th><th>Status</th></tr></thead><tbody>' +
          s.sensors.map(function (x) { return '<tr' + (x.status !== 'Healthy' ? ' class="flagged"' : '') + '><td><code>' + esc(x.sensor_id) + '</code>' + (x.simulated ? ' <span class="bd-muted bd-small">simulated</span>' : '') + '</td><td>' + esc(x.metric) + '</td><td>' + esc(x.zone_id) + '</td><td>' + srcBadge(x.source) + '</td><td>' + esc(x.last_seen.slice(11, 19)) + '</td><td>' + esc(ago(x.age_s)) + '</td><td>' + esc(x.quality) + '</td><td>' + esc(x.status) + '</td></tr>'; }).join('') + '</tbody></table></div>';
      }, function (e) { el('.bd-live-sensorlist').innerHTML = '<p class="bd-err">' + esc(errText(e)) + '</p>'; });
    }

    /* Phase 3: the comfort engine's root cause for this zone (backend/comfort.py). */
    function comfortBlock(c) {
      if (!c) return '';
      function dimRow(label, d) {
        var tgt = d.target[0] === null ? '≤ ' + d.target[1] : d.target[0] + '–' + d.target[1];
        return '<tr' + (d.severity && d.severity !== 'none' ? ' class="flagged"' : '') + '><th scope="row">' + esc(label) + '</th><td>' +
          (d.value === null ? '<span class="bd-err">Unavailable (' + esc(d.quality) + ')</span>' : esc(num(d.value, d.unit === '°C' ? 1 : 0) + ' ' + d.unit)) +
          '</td><td>' + esc(tgt + ' ' + d.unit) + '</td><td>' + esc(d.status) + '</td><td>' + esc(d.score === null ? '—' : num(d.score, 0)) + '</td><td>' + srcBadge(d.source) + '</td></tr>';
      }
      var pc = c.primary_cause, r = c.recommendation, d = c.durations;
      var head = pc ? 'Primary cause: <b>' + esc(pc.dimension === 'air_quality' ? 'CO₂ (ventilation indicator)' : pc.dimension) + '</b> · ' + esc(pc.status) + ' ' + esc(num(pc.value, pc.unit === '°C' ? 1 : 0)) + ' ' + esc(pc.unit) +
        (c.secondary_causes.length ? ' · secondary: ' + c.secondary_causes.map(function (s) { return esc(s.status + ' ' + num(s.value, s.unit === '°C' ? 1 : 0) + ' ' + s.unit); }).join(', ') : '') : 'No comfort issue against the profile ranges.';
      return '<div class="bd-why bd-comfort"><h3>Occupant comfort · ' + esc(c.status) + ' · ' + esc(c.score === null ? '—' : num(c.score, 0) + '/100') + ' ' + srcBadge('derived') + '</h3>' +
        '<p>' + head + ' · ' + esc(c.occupancy_state || '—') + (c.comfort_relevant ? '' : ' (conditions not currently affecting anyone)') + '</p>' +
        '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Dimension</th><th>Value</th><th>Target</th><th>Status</th><th>Score</th><th>Data state</th></tr></thead><tbody>' +
        dimRow('Thermal', c.thermal) + dimRow('Humidity', c.humidity) + dimRow('CO₂', c.air_quality) + '</tbody></table></div>' +
        '<p><b>Recommended:</b> ' + esc(r.text) + (r.expected_effect ? ' <b>Expected:</b> ' + esc(r.expected_effect) + '.' : '') + (r.energy_consideration ? ' <b>Energy:</b> ' + esc(r.energy_consideration) + '.' : '') + '</p>' +
        '<p class="bd-small">Discomfort today: ' + Math.round(d.today.uncomfortable_s / 60) + ' of ' + Math.round(d.today.occupied_s / 60) + ' occupied min' + (d.today.uncomfortable_pct !== null ? ' (' + num(d.today.uncomfortable_pct, 1) + ' %)' : '') +
        ' · current episode ' + Math.round(d.current_discomfort_s / 60) + ' min · ' + c.events.length + ' event(s) · weights ' + Math.round(c.weights.thermal * 100) + '/' + Math.round(c.weights.humidity * 100) + '/' + Math.round(c.weights.air_quality * 100) + '</p></div>';
    }

    function renderTrends(box, cfg) {
      var lib = window.FLChart;
      if (!lib) { box.innerHTML = '<p class="bd-foot">Charts need /static/monitor.js, which did not load.</p>'; return; }
      var P = detail.trends.points, F = detail.trends.fields;
      function pts(k) { return P.map(function (p) { return { x: p.t, y: p[k] }; }); }
      var defs = [
        ['Temperature', 'temp', '°C', 1, C.actual, { band: cfg.comfort_c }],
        ['Humidity', 'rh', '%', 0, C.actual, { band: cfg.humidity_pct }],
        ['Occupancy', 'occ_pct', '%', 0, C.actual, { yMin: 0 }],
        ['CO₂ (estimated)', 'co2', 'ppm', 0, C.alt, { band: [400, cfg.co2_ppm] }],
        ['HVAC power', 'power_w', 'W', 0, C.warn, { yMin: 0 }],
        ['Comfort score', 'comfort', '/100', 0, C.expected, { band: [70, 100], yMin: 0 }]
      ];
      var charts = defs.map(function (d) {
        var o = { series: [{ name: d[0], color: d[4], pts: pts(d[1]), unit: d[2], dec: d[3] }], h: 150, xTicks: lib.simTicks, empty: 'No samples in this window yet.' };
        Object.keys(d[5]).forEach(function (k) { o[k] = d[5][k]; });
        return { def: d, ch: lib.lineChart(o) };
      });
      box.innerHTML = charts.map(function (c, i) {
        return '<div class="bd-mini" data-i="' + i + '"><div class="bd-kpi-h"><span>' + esc(c.def[0]) + ' <small class="bd-muted">' + esc(c.def[2]) + '</small></span>' + srcBadge(F[c.def[1]] || 'sim') + '</div>' + c.ch.svg + '</div>';
      }).join('');
      if (!tip) return;
      charts.forEach(function (c, i) {
        var svg = box.querySelector('.bd-mini[data-i="' + i + '"] svg');
        if (svg) lib.bindHover(svg, c.ch, tip, simClock);
      });
    }

    // ---------------------------------------------------------------- configuration form
    var GROUPS = [
      ['Identity & size', [['name', 'Building name', 'text'], ['floors', 'Floors', 'int'], ['zones', 'Zones', 'int'], ['occupancy_capacity', 'Occupancy capacity (people)', 'int'], ['hvac_capacity_w', 'HVAC design capacity (W)', 'num']]],
      ['Operating hours & occupancy', [['open_hour', 'Opens (hour)', 'num'], ['close_hour', 'Closes (hour)', 'num'], ['open_days', 'Open days', 'days'], ['expected_occupancy_pct', 'Expected occupancy (% of schedule)', 'num'], ['occupancy_sensitivity', 'Occupancy sensitivity (0–1)', 'num'], ['base_load_fraction', 'Base HVAC load (0–1)', 'num']]],
      ['Comfort constraints', [['comfort_min_c', 'Comfort min (°C)', 'num'], ['comfort_max_c', 'Comfort max (°C)', 'num'], ['humidity_min_pct', 'Humidity min (%)', 'num'], ['humidity_max_pct', 'Humidity max (%)', 'num'], ['co2_max_ppm', 'CO₂ threshold (ppm)', 'num']]],
      ['Priorities', [['comfort_priority', 'Comfort priority (0–100)', 'num'], ['energy_priority', 'Energy priority (0–100)', 'num']]],
      ['Outside air & lighting', [['oa_per_person_ls', 'Outdoor air per person (L/s)', 'num'], ['oa_per_area_ls_m2', 'Outdoor air per area (L/s·m²)', 'num'], ['lighting_w_m2', 'Lighting power density (W/m²)', 'num']]]
    ];
    function renderForm() {
      if (!profile) return;
      var form = el('.bd-form');
      if (form.contains(document.activeElement)) return;      // never clobber a user mid-edit
      var cfg = profile.config, lim = profile.catalog.limits;
      form.innerHTML =
        '<p class="bd-muted bd-small">Building type is switched from the control bar (it loads that profile\'s defaults). Values are validated by the server; nothing is applied unless every field passes. ' + srcBadge('config') + '</p>' +
        GROUPS.map(function (g) {
          return '<fieldset><legend>' + esc(g[0]) + '</legend>' + g[1].map(function (f) {
            var id = 'bd-f-' + f[0], v = cfg[f[0]];
            if (f[2] === 'days') {
              return '<div class="bd-fld bd-days" role="group" aria-label="Open days"><span>' + esc(f[1]) + '</span>' + DAYS.map(function (dn, i) { return '<label><input type="checkbox" name="open_days" value="' + i + '"' + (v.indexOf(i) >= 0 ? ' checked' : '') + '> ' + dn + '</label>'; }).join('') + '</div>';
            }
            var l = lim[f[0]] || [], attrs = f[2] === 'text' ? ' type="text" maxlength="80"' : ' type="number" step="' + (f[2] === 'int' ? '1' : 'any') + '"' + (l.length ? ' min="' + l[0] + '" max="' + l[1] + '"' : '');
            return '<label class="bd-fld" for="' + id + '"><span>' + esc(f[1]) + '</span><input id="' + id + '" name="' + f[0] + '"' + attrs + ' value="' + esc(v) + '"></label>';
          }).join('') + '</fieldset>';
        }).join('') +
        '<div class="bd-form-err" role="alert" hidden></div>' +
        '<div class="bd-form-actions"><button type="submit" class="bd-primary">Save configuration</button><button type="button" class="bd-defaults">Reset to ' + esc(cfg.type_label) + ' defaults</button></div>';
    }
    function formErrors(list) {
      var n = el('.bd-form-err'); if (!n) return;
      n.hidden = !list.length;
      n.innerHTML = list.length ? '<b>Not applied:</b><ul>' + list.map(function (m) { return '<li>' + esc(m) + '</li>'; }).join('') + '</ul>' : '';
    }
    function saveForm() {
      var form = el('.bd-form'), body = {}, errs = [];
      GROUPS.forEach(function (g) {
        g[1].forEach(function (f) {
          if (f[2] === 'days') {
            body.open_days = [].slice.call(form.querySelectorAll('input[name="open_days"]:checked')).map(function (x) { return +x.value; });
            return;
          }
          var raw = form.querySelector('[name="' + f[0] + '"]').value;
          if (f[2] === 'text') { body[f[0]] = raw; return; }
          var v = Number(raw);
          if (raw.trim() === '' || !isFinite(v)) errs.push(f[1] + ' must be a number');
          else if (f[2] === 'int' && Math.floor(v) !== v) errs.push(f[1] + ' must be a whole number');
          else body[f[0]] = v;
        });
      });
      if (errs.length) { formErrors(errs); status('not applied: fix the highlighted form errors', true); return; }
      if (document.activeElement) document.activeElement.blur();
      post(body, 'Configuration saved');
    }
    function resetDefaults() {
      if (!profile) return;
      var t = profile.config.building_type, def = profile.catalog.types.filter(function (x) { return x.key === t; })[0];
      if (!def) return;
      var body = {};
      profile.catalog.editable.forEach(function (k) { if (k !== 'building_type' && k !== 'operating_mode' && def.defaults[k] !== undefined) body[k] = def.defaults[k]; });
      if (document.activeElement) document.activeElement.blur();
      post(body, 'Reset to ' + def.label + ' defaults');
    }

    return { mount: mount, update: update };
  }

  // ---------------------------------------------------------------- styles
  function injectCSS() {
    if (document.getElementById('bd-css')) return;
    var css = [
      '.bd{display:flex;flex-direction:column;gap:12px;}',
      '.bd-bar{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px;}',
      '.bd-bar label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}',
      '.bd-bar select,.bd-bar input[type=range]{font:inherit;font-size:13px;text-transform:none;letter-spacing:0;color:var(--ink);}',
      '.bd-bar select{border:1px solid var(--border);background:var(--page);border-radius:6px;padding:4px 6px;min-width:120px;}',
      '.bd-bar output{font-size:12px;color:var(--ink);font-variant-numeric:tabular-nums;text-transform:none;}',
      '.bd-bar label:has(input[type=range]){flex-direction:column;}',
      '.bd-status{margin-left:auto;font-size:12px;color:var(--ink2);align-self:center;}.bd-status.bad{color:var(--crit);}',
      '.bd-bar select:focus-visible,.bd-bar input:focus-visible,.bd button:focus-visible,.bd-zonecard:focus-visible,.bd input:focus-visible,.bd summary:focus-visible{outline:2px solid var(--us);outline-offset:2px;}',
      '.bd-head{display:flex;flex-direction:column;gap:5px;}',
      '.bd-title{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px 10px;}.bd-title h2{font-size:18px;letter-spacing:-.01em;}',
      '.bd-typelbl{font-size:13px;color:var(--ink2);}.bd-level{margin-left:auto;font-size:12.5px;color:var(--ink2);}',
      '.bd-facts{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--ink2);font-variant-numeric:tabular-nums;}.bd-facts b{color:var(--ink);font-weight:600;}',
      '.bd-modeline{font-size:12.5px;color:var(--ink2);}.bd-modeline b{color:var(--ink);}',
      '.bd-muted{color:var(--muted);}.bd-small{font-size:11.5px;}.bd-fit{font-size:12px;}',
      '.bd-warn{font-size:12.5px;color:#7a5c00;background:rgba(250,178,25,.12);border:1px solid rgba(250,178,25,.6);border-radius:6px;padding:6px 9px;}',
      '.bd-err{color:var(--crit);font-size:12.5px;}',
      '.bd-src{display:inline-block;font-size:9.5px;font-weight:700;letter-spacing:.05em;padding:0 5px;border-radius:4px;border:1px solid var(--border);color:var(--ink2);background:var(--page);vertical-align:1px;white-space:nowrap;}',
      '.bd-src-sim{color:#1d5fae;border-color:rgba(42,120,214,.35);}.bd-src-derived{color:#52514e;}.bd-src-predicted{color:#5b3fb8;border-color:rgba(124,92,214,.4);}',
      '.bd-src-hardware{color:#0b7a70;border-color:rgba(13,148,136,.45);}.bd-src-real{color:#a3360a;border-color:rgba(194,65,12,.4);}.bd-src-historical{color:#5b3fb8;}',
      '.bd-src-config{color:var(--ink);}.bd-src-none{color:var(--muted);border-style:dashed;}',
      '.bd-st{display:inline-block;font-size:11px;font-weight:600;white-space:nowrap;color:var(--ink2);}',
      '.bd-st-normal{color:var(--good);}.bd-st-warning{color:#8a5a00;}.bd-st-critical{color:var(--crit);}.bd-st-not_modelled,.bd-st-unknown{color:var(--muted);}',
      '.bd-kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;}',
      '.bd-kpi,.bd-tile{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:9px 11px;display:flex;flex-direction:column;gap:2px;min-width:0;}',
      '.bd-kpi.bd-k-warning{box-shadow:inset 3px 0 0 var(--warn);}.bd-kpi.bd-k-critical{box-shadow:inset 3px 0 0 var(--crit);}',
      '.bd-kpi-h{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}',
      '.bd-kpi-v{font-size:22px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
      '.bd-tile-v{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums;}',
      '.bd-unit{font-size:12px;font-weight:500;color:var(--ink2);margin-left:4px;}',
      '.bd-kpi-s{font-size:11.5px;color:var(--ink2);min-height:1.3em;}.bd-kpi-f{margin-top:2px;}',
      '.bd-main{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:12px;align-items:start;}',
      '@media (max-width:1100px){.bd-main{grid-template-columns:1fr;}}',
      '.bd-tree-note{font-size:12px;margin-bottom:8px;}',
      '.bd-floor{border-top:1px solid var(--grid);padding:8px 0 4px;}.bd-floor-h{display:flex;align-items:center;gap:10px;font-size:13px;margin-bottom:6px;}',
      '.bd-floor-h .bd-st{margin-left:auto;}.bd-floor-empty p{font-size:12px;}',
      '.bd-zones{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;}',
      '.bd-zonecard{border:1px solid var(--border);border-radius:8px;padding:8px 10px;background:var(--page);cursor:pointer;display:flex;flex-direction:column;gap:3px;text-align:left;}',
      '.bd-zonecard:hover{border-color:var(--ink2);}.bd-zonecard.sel{border-color:var(--us);box-shadow:0 0 0 1px var(--us);}',
      '.bd-zonecard.bd-z-warning{border-left:3px solid var(--warn);}.bd-zonecard.bd-z-critical{border-left:3px solid var(--crit);}',
      '.bd-zc-h{display:flex;justify-content:space-between;gap:6px;font-size:13px;}.bd-zc-sub{font-size:11px;color:var(--muted);}',
      '.bd-zc-temp{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums;}.bd-zc-rh{font-size:12px;font-weight:500;color:var(--ink2);margin-left:10px;}',
      '.bd-zc-grid{display:grid;grid-template-columns:auto 1fr;gap:1px 10px;font-size:12px;font-variant-numeric:tabular-nums;}.bd-zc-grid span{color:var(--muted);}.bd-zc-grid b{font-weight:600;}',
      '.bd-zc-grid small,.bd-table small,.bd-tile small{color:var(--ink2);font-weight:400;}',
      '.bd-flags{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px;}',
      '.bd-flag{font-size:10.5px;border:1px solid var(--border);border-radius:4px;padding:0 5px;color:var(--ink2);background:var(--surface);}',
      '.bd-flag-comfortable{color:var(--good);}.bd-flag-warm{color:#b0381f;}.bd-flag-cold{color:#1d5fae;}.bd-flag-high_co2{color:#8a5a00;}',
      '.bd-flag-high_occupancy{color:#5b3fb8;}.bd-flag-hvac_active{color:#0b7a70;}.bd-flag-warning{color:#8a5a00;border-color:rgba(250,178,25,.7);}.bd-flag-critical{color:var(--crit);border-color:var(--crit);}',
      '.bd-tablewrap{overflow-x:auto;}.bd-table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums;}',
      '.bd-table th,.bd-table td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--grid);vertical-align:top;}',
      '.bd-table thead th{font-size:11px;color:var(--muted);font-weight:600;}.bd-table tbody th{font-weight:600;color:var(--ink2);white-space:nowrap;}',
      '.bd-table tr.flagged td,.bd-table tr.flagged th{background:rgba(250,178,25,.07);}',
      '.bd-seg-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;margin:10px 0 4px;}',
      '.bd-seg{display:inline-flex;border:1px solid var(--border);border-radius:6px;overflow:hidden;}',
      '.bd-seg button{font:inherit;font-size:12px;border:0;border-right:1px solid var(--border);background:var(--surface);color:var(--ink2);padding:3px 9px;cursor:pointer;}',
      '.bd-seg button:last-child{border-right:0;}.bd-seg button.on{background:var(--ink);color:var(--surface);}',
      '.bd-legend-row{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11.5px;color:var(--ink2);margin-bottom:2px;}',
      '.bd-sw{display:inline-block;width:14px;height:0;border-top:2px solid;vertical-align:3px;margin-right:5px;}.bd-sw.dash{border-top-style:dashed;}',
      '.bd-lvstrip{display:flex;gap:1px;margin-top:3px;}.bd-lvcell{flex:1;height:10px;min-width:2px;}.bd-lvcell.fut{opacity:.55;}',
      '.bd-lv-low{background:#dbe8f8;}.bd-lv-medium{background:#9cc0ec;}.bd-lv-high{background:#4f8fdc;}.bd-lv-very_high{background:#1f5fae;}',
      'b.bd-lv{background:none;padding:0;color:var(--ink);}',
      '.bd-foot{font-size:11.5px;color:var(--muted);margin-top:6px;}',
      '.bd-detail{border-color:var(--us);scroll-margin-top:150px;}',
      '.bd-live h2{flex-wrap:wrap;}.bd-live-bldg .bd-tiles{margin:6px 0 8px;}.bd-lv-cell{font-variant-numeric:tabular-nums;}',
      '.bd-lv-meta{font-size:10.5px;color:var(--muted);white-space:nowrap;}.bd-lv-q{font-weight:700;color:#b0381f;}.bd-lv-q-est{color:var(--ink2);font-weight:600;}',
      '.bd-q-stale,.bd-q-invalid,.bd-q-missing{background:rgba(208,59,59,.07);}.bd-q-aging{background:rgba(250,178,25,.10);}',
      '.bd-lv-table td,.bd-lv-table th{font-size:12.5px;}.bd-h3{font-size:12.5px;color:var(--ink2);margin:8px 0 4px;}',
      '.bd-lv-health,.bd-lv-src{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12.5px;}',
      '.bd-live-charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px;}.bd-live-sensors summary{cursor:pointer;font-size:12.5px;color:var(--ink2);margin-top:6px;}',
      '.bd-dt-h{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}.bd-dt-h h2{font-size:16px;color:var(--ink);margin:0;}.bd-dt-h h2 span{font-size:12.5px;font-weight:400;}',
      '.bd-close{margin-left:auto;font:inherit;font-size:12px;border:1px solid var(--border);background:var(--surface);border-radius:6px;padding:3px 10px;cursor:pointer;}',
      '.bd-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:10px 0;}',
      '.bd-dt-grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:14px;}@media (max-width:1000px){.bd-dt-grid{grid-template-columns:1fr;}}',
      '.bd-why h3,.bd-lists h3{font-size:12.5px;color:var(--ink2);margin:0 0 6px;}.bd-lists h3+ul,.bd-lists h3+p{margin-bottom:10px;}',
      '.bd-why p{font-size:12.5px;margin:6px 0;}.bd-why-s{font-size:13.5px !important;background:var(--page);border:1px solid var(--grid);border-radius:6px;padding:7px 9px;}',
      '.bd-lists ul{padding-left:16px;font-size:12px;display:flex;flex-direction:column;gap:5px;}',
      '.bd code{font-size:11.5px;background:var(--page);border:1px solid var(--grid);border-radius:4px;padding:0 4px;}',
      '.bd-dt-charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;}',
      '.bd-mini{border:1px solid var(--grid);border-radius:8px;padding:6px 8px;}',
      '.bd-config summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--ink2);}',
      '.bd-form{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;margin-top:10px;}.bd-form>p,.bd-form-err,.bd-form-actions{grid-column:1/-1;}',
      '.bd-form fieldset{border:1px solid var(--grid);border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;gap:6px;min-width:0;}',
      '.bd-form legend{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;padding:0 4px;}',
      '.bd-fld{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12.5px;color:var(--ink2);}',
      '.bd-fld input[type=number],.bd-fld input[type=text]{font:inherit;font-size:13px;width:120px;border:1px solid var(--border);border-radius:6px;padding:3px 6px;background:var(--page);color:var(--ink);font-variant-numeric:tabular-nums;}',
      '.bd-fld input[type=text]{width:170px;}.bd-fld input:invalid{border-color:var(--crit);}',
      '.bd-days{flex-wrap:wrap;justify-content:flex-start;}.bd-days span{width:100%;}.bd-days label{font-size:12px;}',
      '.bd-form-err{color:var(--crit);font-size:12.5px;border:1px solid var(--crit);border-radius:6px;padding:6px 9px;}.bd-form-err ul{padding-left:16px;}',
      '.bd-form-actions{display:flex;gap:8px;}.bd-form-actions button{font:inherit;font-size:13px;border-radius:6px;padding:6px 14px;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--ink);}',
      '.bd-primary{background:var(--us) !important;color:#fff !important;border-color:var(--us) !important;font-weight:600;}'
    ].join('');
    var st = document.createElement('style'); st.id = 'bd-css'; st.textContent = css; document.head.appendChild(st);
  }

  var booted = false;
  function boot() {
    if (booted) return true;
    var FL = window.FL; if (!FL || typeof FL.registerPanel !== 'function') return false;
    booted = true; injectCSS();
    FL.registerPanel('building', buildingPanel());
    return true;
  }
  if (!boot()) document.addEventListener('DOMContentLoaded', boot);
})();
