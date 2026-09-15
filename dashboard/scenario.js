/* dashboard/scenario.js — the Scenario tab: seasonal / environmental simulation + SIMULATED hardware.
 *
 * Live cards (weather, building response, causal explanation, zones) render from the shell's ONE
 * /api/latest poll (FL.on('latest')). This panel calls the backend only when the operator acts:
 *   GET/POST /api/scenario, POST /api/scenario/reset, GET /api/scenario/compare   environment
 *   GET /api/simhw (every 2nd latest event while visible), POST /api/simhw[/devices/{id}/fault|config]
 *   POST /api/speed                                                                 simulation speed
 * No demo value is written here: every number is a backend value. SIMULATED HARDWARE IS NOT REAL HARDWARE.
 */
(function () {
  'use strict';
  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function num(v, d) { return isNum(v) ? v.toLocaleString('en-IN', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }) : '—'; }
  function ago(s) { return !isNum(s) ? '—' : s < 60 ? Math.round(s) + ' s ago' : Math.floor(s / 60) + ' min ago'; }
  var SRC = { sim: 'SIMULATED', derived: 'DERIVED', predicted: 'PREDICTED', hardware: 'HARDWARE', real: 'REAL', historical: 'HISTORICAL' };
  function badge(v) {
    if (!v || !v.source) return '';
    var label = v.origin === 'simulated_hardware' ? 'SIMULATED HARDWARE' : (SRC[v.source] || v.source);
    return '<span class="bd-src bd-src-' + esc(v.source) + '">' + esc(label) + '</span>';
  }
  function val(v, d, unit) {
    if (!v) return '<span class="bd-err">Unavailable</span>';
    if (v.value === null || v.value === undefined) return '<span class="' + (v.note ? 'bd-muted' : 'bd-err') + '">' + esc(v.note || v.display || 'Unavailable') + '</span>';
    return (typeof v.value === 'number' ? esc(num(v.value, d)) : esc(String(v.value).replace(/_/g, ' '))) + (unit === '' ? '' : ' <small>' + esc(unit || v.unit) + '</small>');
  }

  function scenarioPanel() {
    var FL = window.FL, root, sc = null, hw = null, ticks = 0, cmp = null;
    function el(s) { return root.querySelector(s); }
    function status(m, bad) { var n = el('.sc-status'); n.textContent = m; n.classList.toggle('bad', !!bad); }
    function err(e) { var d = e && e.body && e.body.detail; return d ? (d.errors ? d.errors.join('; ') : typeof d === 'string' ? d : JSON.stringify(d)) : (e && e.message) || String(e); }

    function mount(node) {
      root = node;
      root.innerHTML = '<div class="bd sc">' +
        '<div class="sc-banner" role="status" aria-live="polite"></div>' +
        '<section class="card"><h2>Environment controls <span class="right bd-status sc-status"></span></h2><form class="sc-form" novalidate></form></section>' +
        '<div class="bd-main">' +
          '<section class="card"><h2>Live weather <span class="right sc-wx-meta"></span></h2><div class="sc-weather"></div></section>' +
          '<section class="card"><h2>Building response</h2><div class="sc-chain"></div><p class="sc-why"></p></section>' +
        '</div>' +
        '<section class="card"><h2>Zones right now</h2><div class="sc-zones"></div></section>' +
        '<section class="card"><h2>Season comparison <span class="bd-src bd-src-predicted">SIMULATED / WHAT-IF</span> <span class="right"><select class="sc-h" aria-label="Horizon"><option value="3">3 h</option><option value="6" selected>6 h</option><option value="12">12 h</option><option value="24">24 h</option></select> <button type="button" class="bd-close sc-cmp-run">Compare seasons</button></span></h2><div class="sc-cmp"><p class="bd-muted">Runs four clone simulations of the live building (same start state and controller) under each season. Takes a few seconds.</p></div></section>' +
        '<section class="card"><h2>Hardware simulation <span class="bd-src bd-src-sim">SIMULATED HARDWARE — NOT REAL HARDWARE</span> <span class="right"><label class="sc-inline"><input type="checkbox" class="sc-hw-on"> enable simulated sensors</label></span></h2><div class="sc-hw"></div></section>' +
        '</div>';
      el('.sc-cmp-run').onclick = runCompare;
      el('.sc-hw-on').onchange = function () { FL.post('/api/simhw', { enabled: this.checked }).then(function (j) { hw = j; renderHw(); }, function (e) { status(err(e), true); }); };
      el('.sc-hw').addEventListener('click', onHwClick);
      FL.on('latest', renderLive);
      loadScenario(); loadHw();
      if (FL.latest) renderLive(FL.latest);
    }

    // ---------------------------------------------------------------- controls
    function loadScenario() { return FL.get('/api/scenario').then(function (j) { sc = j; renderForm(); }, function (e) { status(err(e), true); }); }
    function slider(name, label, v, lim, step, unit) {
      return '<label class="sc-f"><span>' + esc(label) + ' <output data-for="' + name + '">' + esc(v) + '</output> ' + esc(unit) + '</span><input type="range" name="' + name + '" min="' + lim[0] + '" max="' + lim[1] + '" step="' + step + '" value="' + esc(v) + '"></label>';
    }
    function renderForm() {
      var f = el('.sc-form'), o = sc.options, k = sc.knobs, L = sc.limits;
      if (f.contains(document.activeElement)) return;
      function sel(name, label, opts, cur) { return '<label class="sc-f"><span>' + esc(label) + '</span><select name="' + name + '">' + opts.map(function (x) { return '<option' + (x === cur ? ' selected' : '') + '>' + esc(x) + '</option>'; }).join('') + '</select></label>'; }
      f.innerHTML =
        '<fieldset><legend>Weather</legend>' + sel('mode', 'Weather model', o.modes, sc.mode) + sel('season', 'Season', o.seasons, sc.season) + sel('climate', 'Climate', o.climates, sc.climate) +
          '<label class="sc-f"><span>Cloud cover <output data-for="cloud_cover">' + (sc.cloud_cover === null ? 'model' : esc(sc.cloud_cover)) + '</output> %</span><input type="range" name="cloud_cover" min="0" max="100" step="5" value="' + (sc.cloud_cover === null ? 50 : sc.cloud_cover) + '"><label class="sc-inline"><input type="checkbox" name="cloud_auto"' + (sc.cloud_cover === null ? ' checked' : '') + '> from weather model</label></label>' +
          '<label class="sc-f"><span>Rain <output data-for="rain_mm_h">' + (sc.rain_mm_h === null ? 'model' : esc(sc.rain_mm_h)) + '</output> mm/h</span><input type="range" name="rain_mm_h" min="0" max="20" step="0.5" value="' + (sc.rain_mm_h === null ? 0 : sc.rain_mm_h) + '"><label class="sc-inline"><input type="checkbox" name="rain_auto"' + (sc.rain_mm_h === null ? ' checked' : '') + '> from weather model</label></label>' +
        '</fieldset>' +
        '<fieldset><legend>Outdoor offsets</legend>' + slider('outdoor_offset', 'Outdoor temperature offset', k.outdoor_offset, L.outdoor_offset, 0.5, '°C') + slider('humidity_offset', 'Humidity offset', k.humidity_offset, L.humidity_offset, 1, '%RH') + slider('solar_scale', 'Solar gain scale', k.solar_scale, L.solar_scale, 0.1, '×') +
          '<div class="sc-f"><span>Weather disturbance</span><div class="sc-row"><input type="number" name="disturbance_delta_c" min="-8" max="8" step="0.5" value="' + (sc.disturbance ? sc.disturbance.delta_c : 4) + '" aria-label="Disturbance °C"> °C for <input type="number" name="disturbance_hours" min="0.25" max="48" step="0.25" value="3" aria-label="Disturbance hours"> h <button type="button" class="sc-dist">Apply</button> <button type="button" class="sc-dist-clear">Clear</button></div>' + (sc.disturbance ? '<small class="bd-muted">active: ' + esc(sc.disturbance.delta_c) + ' °C</small>' : '') + '</div></fieldset>' +
        '<fieldset><legend>Building physics</legend>' + slider('envelope_scale', 'Envelope conductance (U·A) scale', sc.envelope_scale, L.envelope_scale, 0.1, '×') + sel('internal_load', 'Internal loads', o.internal_loads, sc.internal_load) + slider('occ_scale', 'Occupancy scale', k.occ_scale, L.occ_scale, 0.1, '×') + slider('capacity_scale', 'HVAC capacity scale', k.capacity_scale, L.capacity_scale, 0.05, '×') +
          '<p class="bd-small bd-muted">Humidity model: ' + esc(sc.humidity_model) + '<br>Heating: ' + esc(sc.heating) + '</p></fieldset>' +
        '<fieldset><legend>Simulation</legend><div class="bd-seg sc-speed" role="group" aria-label="Simulation speed">' + sc.speed_presets.map(function (p) { return '<button type="button" data-s="' + p.speed + '" class="' + (p.speed === sc.speed ? 'on' : '') + '">' + esc(p.label) + '</button>'; }).join('') + '</div><small class="bd-muted">1× = ' + esc(sc.speed_presets[2].speed) + ' sim-seconds per real second</small>' +
          '<div class="bd-form-err sc-err" role="alert" hidden></div><div class="bd-form-actions"><button type="submit" class="bd-primary">Apply scenario</button><button type="button" class="sc-reset">Reset scenario</button></div></fieldset>';
      f.oninput = function (e) { var o2 = f.querySelector('output[data-for="' + e.target.name + '"]'); if (o2) o2.textContent = e.target.value; };
      f.onsubmit = function (e) { e.preventDefault(); apply(collect()); };
      f.querySelector('.sc-reset').onclick = function () { FL.post('/api/scenario/reset', {}).then(function (j) { sc = j; renderForm(); status('Scenario reset to baseline'); }, function (e2) { status(err(e2), true); }); };
      f.querySelector('.sc-dist').onclick = function () { apply({ disturbance_delta_c: +f.disturbance_delta_c.value, disturbance_hours: +f.disturbance_hours.value }); };
      f.querySelector('.sc-dist-clear').onclick = function () { apply({ clear_disturbance: true }); };
      f.querySelector('.sc-speed').onclick = function (e) { var b = e.target.closest('button[data-s]'); if (!b) return; FL.post('/api/speed', { speed: +b.dataset.s }).then(function (r) { sc.speed = r.speed; renderForm(); status('Simulation speed ' + b.textContent); }); };
    }
    function collect() {
      var f = el('.sc-form');
      var b = { mode: f.mode.value, season: f.season.value, climate: f.climate.value, internal_load: f.internal_load.value };
      ['outdoor_offset', 'humidity_offset', 'solar_scale', 'envelope_scale', 'occ_scale', 'capacity_scale'].forEach(function (n) { b[n] = +f[n].value; });
      if (f.cloud_auto.checked) b.cloud_auto = true; else b.cloud_cover = +f.cloud_cover.value;
      if (f.rain_auto.checked) b.rain_auto = true; else b.rain_mm_h = +f.rain_mm_h.value;
      return b;
    }
    function apply(body) {
      status('applying…');
      FL.post('/api/scenario', body).then(function (j) { sc = j; el('.sc-err') && (el('.sc-err').hidden = true); renderForm(); status('Scenario applied · ' + (j.mode === 'seasonal' ? j.season + ' / ' + j.climate : 'classic weather')); },
        function (e) { status('not applied', true); var n = el('.sc-err'); if (n) { n.hidden = false; n.textContent = err(e); } });
    }

    // ---------------------------------------------------------------- live cards (central poll)
    function renderLive(j) {
      if (!root) return;
      if (!j) { el('.sc-banner').innerHTML = '<b class="bd-err">● OFFLINE</b> — backend unreachable; last values kept.'; return; }
      var s = j.scenario, w = j.weather, b = j.building.metrics;
      // keep the controls truthful when the scenario was changed elsewhere (API, another tab)
      if (sc && (sc.mode !== s.mode || (s.mode === 'seasonal' && (sc.season !== s.season || sc.climate !== s.climate)) ||
                 sc.internal_load !== s.internal_load || sc.envelope_scale !== s.envelope_scale) &&
          !el('.sc-form').contains(document.activeElement)) loadScenario();
      el('.sc-banner').innerHTML = '<b>' + esc(j.system_health.status) + '</b> · weather ' + esc(s.weather_model) + (s.season ? ' · season <b>' + esc(s.season.toUpperCase()) + '</b>' : '') +
        ' · internal loads ' + esc(s.internal_load) + ' · telemetry <b>' + esc(j.telemetry_mode) + '</b> · heating ' + esc(s.heating);
      var rows = [['Outdoor temperature', w.outdoor_temperature, 1], ['Outdoor humidity', w.outdoor_humidity, 0], ['Dew point', w.dew_point, 1], ['Heat index', w.heat_index, 1],
        ['Wind', w.wind_speed, 1], ['Wind direction', w.wind_direction, 0, ''], ['Solar irradiance', w.solar_irradiance, 0], ['Cloud cover', w.cloud_cover, 0], ['Rain', w.rainfall, 1],
        ['Condition', w.weather_condition, 0, ''], ['Season', w.season, 0, '']];
      el('.sc-weather').innerHTML = '<div class="bd-tablewrap"><table class="bd-table"><tbody>' + rows.map(function (r) { return '<tr><th scope="row">' + esc(r[0]) + '</th><td>' + val(r[1], r[2], r[3]) + '</td><td>' + badge(r[1]) + '</td><td class="bd-muted bd-small">' + esc(r[1] ? ago(r[1].age_s) : '') + '</td></tr>'; }).join('') + '</tbody></table></div>';
      el('.sc-wx-meta').textContent = 'sim ' + j.sim_clock;
      var c = j.causal;
      if (c && c.available) {
        el('.sc-chain').innerHTML = '<div class="sc-flow">' + c.chain.map(function (x, i) {
          var arr = x.direction === 'up' ? '↑' : x.direction === 'down' ? '↓' : '→';
          return (i ? '<span class="sc-arrow" aria-hidden="true">↓</span>' : '') + '<div class="sc-node"><span>' + esc(x.label) + '</span><b>' + esc(num(x.value, x.unit === '°C' ? 1 : 0)) + ' <small>' + esc(x.unit) + '</small></b><em class="sc-d-' + esc(x.direction || 'flat') + '">' + arr + (x.delta === null ? '' : ' ' + (x.delta > 0 ? '+' : '') + esc(num(x.delta, x.unit === '°C' ? 1 : 0))) + '</em></div>';
        }).join('') + '</div><div class="bd-tiles">' +
          [['Current power', b.power, 0], ["Today's energy", b.energy_today, 2], ['Peak cooling capacity used', b.cooling_capacity_pct, 0], ['Building comfort', b.comfort_score, 0], ['Internal loads', b.equipment_power, 0], ['Heating', b.heating_demand, 0]].map(function (t) {
            return '<div class="bd-tile"><div class="bd-kpi-h"><span>' + esc(t[0]) + '</span>' + badge(t[1]) + '</div><div class="bd-tile-v">' + val(t[1], t[2]) + '</div></div>';
          }).join('') + '</div>';
        el('.sc-why').innerHTML = '<b>Why:</b> ' + esc(c.sentence) + ' <span class="bd-src bd-src-derived">DERIVED</span> <span class="bd-muted bd-small">(changes over the last ' + Math.round(c.window_s / 60) + ' sim-min)</span>';
      } else {
        el('.sc-chain').innerHTML = '<p class="bd-muted">No current data available</p>';
        el('.sc-why').textContent = '';
      }
      el('.sc-zones').innerHTML = '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Zone</th><th>Indoor</th><th>RH</th><th>CO₂</th><th>Occupancy</th><th>Cooling capacity</th><th>HVAC power</th><th>Internal gain</th><th>Comfort</th></tr></thead><tbody>' +
        j.zones.map(function (z) {
          var atcap = z.hvac.at_capacity.value === true;
          return '<tr' + (atcap ? ' class="flagged"' : '') + '><th scope="row">' + esc(z.name) + '</th><td>' + val(z.temperature, 1) + '<div>' + badge(z.temperature) + '</div></td><td>' + val(z.humidity, 0) + '</td><td>' + val(z.co2, 0) + '</td><td>' + val(z.occupancy, 0) + '</td><td>' + val(z.hvac.cooling_pct, 0) + (atcap ? ' <b class="bd-err">AT CAPACITY</b>' : '') + '</td><td>' + val(z.hvac.hvac_power, 0) + '</td><td>' + val(z.internal_gain, 0) + '</td><td>' + val(z.comfort.score, 0, '/100') + '<div class="bd-muted bd-small">' + esc(z.comfort.status.value || '') + (z.comfort.missing_inputs.length ? ' · missing ' + esc(z.comfort.missing_inputs.join(', ')) : '') + (z.comfort.stale_inputs.length ? ' · stale ' + esc(z.comfort.stale_inputs.join(', ')) : '') + '</div></td></tr>';
        }).join('') + '</tbody></table></div>';
      if (FL.active === 'scenario' && ticks++ % 2 === 0) loadHw();
    }

    // ---------------------------------------------------------------- comparison
    function runCompare() {
      var h = el('.sc-h').value;
      el('.sc-cmp').innerHTML = '<p class="bd-foot">Running four clone simulations…</p>';
      FL.get('/api/scenario/compare?horizon_h=' + h).then(function (c) {
        cmp = c;
        var S = ['summer', 'monsoon', 'winter', 'transition'];
        var M = [['Outdoor temperature', 'outdoor_temperature_c', '°C', 1], ['Indoor temperature (occupied)', 'indoor_temperature_c', '°C', 1], ['Indoor humidity (occupied)', 'indoor_humidity_pct', '%', 0],
          ['Cooling demand (mean)', 'cooling_demand_w', 'W', 0], ['Max cooling capacity used', 'max_cooling_capacity_pct', '%', 0], ['Heating demand', 'heating_demand', '', 0], ['HVAC power (mean)', 'hvac_power_w', 'W', 0],
          ['Energy', 'energy_kwh', 'kWh', 2], ['Comfort (occupied)', 'comfort_score', '/100', 0], ['Peak demand', 'peak_demand_w', 'W', 0], ['Zone-minutes at capacity', 'at_capacity_zone_min', 'min', 0], ['Solar irradiance (mean)', 'solar_irradiance_w_m2', 'W/m²', 0], ['Rain', 'rain_hours', 'h', 1]];
        el('.sc-cmp').innerHTML = '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Metric</th>' + S.map(function (s) { return '<th>' + esc(s) + '</th>'; }).join('') + '</tr></thead><tbody>' +
          M.map(function (m) { return '<tr><th scope="row">' + esc(m[0]) + '</th>' + S.map(function (s) { var v = c.seasons[s][m[1]]; return '<td>' + (typeof v === 'string' ? '<span class="bd-muted">NOT MODELLED</span>' : esc(num(v, m[3])) + ' <small>' + esc(m[2]) + '</small>') + '</td>'; }).join('') + '</tr>'; }).join('') +
          '</tbody></table></div><p class="bd-foot">' + esc(c.note) + ' Horizon ' + esc(c.horizon_h) + ' h · climate ' + esc(c.climate) + '.</p>';
      }, function (e) { el('.sc-cmp').innerHTML = '<p class="bd-err">' + esc(err(e)) + '</p>'; });
    }

    // ---------------------------------------------------------------- simulated hardware
    function loadHw() { return FL.get('/api/simhw').then(function (j) { hw = j; renderHw(); }); }
    function renderHw() {
      if (!hw) return;
      el('.sc-hw-on').checked = hw.enabled;
      var box = el('.sc-hw');
      if (box.contains(document.activeElement)) return;
      box.innerHTML = '<p class="bd-small bd-muted">' + esc(hw.notice) + ' Devices sample the digital twin, add noise / bias / drift, pass through a simulated protocol and enter the same latest-value pipeline. ' + (hw.enabled ? '' : '<b>Disabled</b> — the twin publishes its values directly.') + '</p>' +
        '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Device</th><th>Zone</th><th>Metric</th><th>Protocol</th><th>Status</th><th>Reading</th><th>Last update</th><th>Source</th><th>Fault</th></tr></thead><tbody>' +
        hw.devices.map(function (d) {
          var bad = d.status !== 'ONLINE' && d.status !== 'DISABLED';
          return '<tr' + (bad ? ' class="flagged"' : '') + '><td><code>' + esc(d.device_id) + '</code><div class="bd-muted bd-small">' + esc(d.firmware_version) + ' · every ' + esc(d.sampling_interval_s) + ' s · ±' + esc(d.noise_sd) + ' ' + esc(d.unit) + '</div></td><td>' + esc(d.zone_id) + ' · ' + esc(d.floor_id) + '</td><td>' + esc(d.metric) + '</td><td>' + esc(d.protocol_label) + '</td>' +
            '<td><b class="' + (bad ? 'bd-err' : '') + '">' + esc(d.status) + '</b></td><td>' + (d.last_reading === null || d.last_reading === undefined ? '<span class="bd-err">Unavailable</span>' : esc(d.last_reading) + ' ' + esc(d.unit)) + (d.rejected_value !== null && d.rejected_value !== undefined ? '<div class="bd-err bd-small">rejected ' + esc(d.rejected_value) + '</div>' : '') + '</td>' +
            '<td>' + esc(ago(d.age_s)) + '</td><td><span class="bd-src bd-src-sim">SIMULATED HARDWARE</span></td>' +
            '<td><select data-fault="' + esc(d.device_id) + '" aria-label="Fault for ' + esc(d.device_id) + '"' + (hw.enabled ? '' : ' disabled') + '>' + hw.faults.map(function (f) { return '<option' + (f === d.fault ? ' selected' : '') + '>' + esc(f) + '</option>'; }).join('') + '</select></td></tr>';
        }).join('') + '</tbody></table></div>';
      box.querySelectorAll('select[data-fault]').forEach(function (s) {
        s.onchange = function () {
          var id = this.dataset.fault, mode = this.value;
          FL.post('/api/simhw/devices/' + encodeURIComponent(id) + '/fault', { mode: mode }).then(function () { this.blur && this.blur(); loadHw(); status('Fault ' + mode + ' on ' + id); }.bind(this), function (e) { status(err(e), true); });
        };
      });
    }
    function onHwClick() {}
    return { mount: mount, update: function () {} };
  }

  function injectCSS() {
    if (document.getElementById('sc-css')) return;
    var st = document.createElement('style'); st.id = 'sc-css';
    st.textContent = '.sc-banner{font-size:13px;background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--warn);border-radius:8px;padding:8px 11px;}' +
      '.sc-form{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px;}.sc-form fieldset{border:1px solid var(--grid);border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;gap:8px;min-width:0;}' +
      '.sc-form legend{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;padding:0 4px;}.sc-f{display:flex;flex-direction:column;gap:3px;font-size:12.5px;color:var(--ink2);}' +
      '.sc-f select,.sc-f input[type=number]{font:inherit;font-size:13px;border:1px solid var(--border);border-radius:6px;padding:3px 6px;background:var(--page);}.sc-f input[type=number]{width:70px;}' +
      '.sc-f output{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums;}.sc-inline{font-size:12px;display:inline-flex;gap:4px;align-items:center;}.sc-row{display:flex;flex-wrap:wrap;gap:4px;align-items:center;}' +
      '.sc-row button{font:inherit;font-size:12px;border:1px solid var(--border);background:var(--surface);border-radius:6px;padding:2px 8px;cursor:pointer;}' +
      '.sc-flow{display:flex;flex-direction:column;align-items:stretch;gap:2px;margin-bottom:8px;}.sc-node{display:grid;grid-template-columns:1fr auto 90px;gap:8px;align-items:baseline;border:1px solid var(--grid);border-radius:6px;padding:4px 9px;font-size:12.5px;}' +
      '.sc-node b{font-variant-numeric:tabular-nums;}.sc-node em{font-style:normal;text-align:right;font-variant-numeric:tabular-nums;color:var(--muted);}.sc-d-up{color:#b0381f !important;}.sc-d-down{color:#1d5fae !important;}' +
      '.sc-arrow{text-align:center;color:var(--muted);font-size:11px;line-height:1;}.sc-why{font-size:13px;margin-top:6px;}';
    document.head.appendChild(st);
  }
  var booted = false;
  function boot() {
    if (booted) return true;
    var FL = window.FL; if (!FL || typeof FL.registerPanel !== 'function') return false;
    booted = true; injectCSS(); FL.registerPanel('scenario', scenarioPanel()); return true;
  }
  if (!boot()) document.addEventListener('DOMContentLoaded', boot);
})();
