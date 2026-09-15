/* dashboard/security.js — the Security tab (Phase 6).
 *
 * Reads only role-restricted endpoints; the BACKEND decides who may see them (security:view /
 * devices:view). A 403 renders an explanation, never data. No key material, tokens or password
 * hashes are ever returned by these endpoints, so none can be rendered here.
 *   GET /api/security/status    mode, warnings, config (non-secret), telemetry + simulator health, metrics
 *   GET /api/security/devices   registry (SIMULATED vs HARDWARE), unknown attempts, recent rejections
 *   GET /api/security/events    audit trail
 *   POST /api/security/devices/{id}/{action}   quarantine / reinstate (devices:manage only)
 * Refresh: on mount and every 3rd central /api/latest tick while the tab is visible.
 */
(function () {
  'use strict';
  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function ago(s) { return !isNum(s) ? '—' : s < 60 ? Math.round(s) + ' s ago' : s < 3600 ? Math.floor(s / 60) + ' min ago' : Math.floor(s / 3600) + ' h ago'; }
  function when(t) { return isNum(t) ? new Date(t * 1000).toLocaleTimeString() : '—'; }

  function securityPanel() {
    var FL = window.FL, root, ticks = 0, canManage = false;
    function el(s) { return root.querySelector(s); }
    function mount(node) {
      root = node;
      root.innerHTML = '<div class="bd sec">' +
        '<div class="sec-banner" role="status"></div>' +
        '<div class="bd-kpis sec-kpis" role="list" aria-label="Security metrics"></div>' +
        '<div class="bd-main"><section class="card"><h2>System health</h2><div class="sec-health"></div></section>' +
        '<section class="card"><h2>Protocols &amp; transport</h2><div class="sec-proto"></div></section></div>' +
        '<section class="card"><h2>Device security <span class="right bd-muted sec-dev-note"></span></h2><div class="sec-devices"></div></section>' +
        '<div class="bd-main"><section class="card"><h2>Telemetry security · recent rejections</h2><div class="sec-rej"></div></section>' +
        '<section class="card"><h2>Security events</h2><div class="sec-events"></div></section></div>' +
        '</div>';
      root.addEventListener('click', onClick);
      FL.on('latest', function () { if (FL.active === 'security' && ticks++ % 3 === 0) load(); });
      document.addEventListener('fl:auth', function () { load(); });
      load();
    }
    function denied(e) {
      return e && (e.status === 401 || e.status === 403);
    }
    function load() {
      var who = FL.auth && FL.auth.whoami;
      canManage = !!(who && who.principal.permissions.indexOf('devices:manage') >= 0);
      Promise.all([FL.get('/api/security/status'), FL.get('/api/security/devices'), FL.get('/api/security/events?limit=60')])
        .then(function (r) { render(r[0], r[1], r[2]); }, function (e) {
          el('.sec-banner').innerHTML = denied(e)
            ? '<b>Security information is restricted.</b> Your role cannot view device security, telemetry security or audit events. Sign in as an administrator, facility manager or auditor.'
            : '<span class="bd-err">Security status unavailable: ' + esc(e && e.message) + '</span>';
          ['.sec-kpis', '.sec-health', '.sec-proto', '.sec-devices', '.sec-rej', '.sec-events'].forEach(function (s) { el(s).innerHTML = ''; });
        });
    }
    function render(st, dv, ev) {
      var c = st.config, m = st.metrics, s = m.security_events, t = m.telemetry_security;
      el('.sec-banner').innerHTML = '<b>' + esc(c.mode.toUpperCase()) + '</b> · auth ' + (c.auth_enabled ? 'enforced' : '<b class="bd-err">not enforced</b>') +
        ' · device auth ' + (c.device_auth_required ? 'required' : 'optional') + ' · ' + esc(c.transport_note) +
        ' · signed in as <b>' + esc(st.principal.username || 'development principal') + '</b> (' + esc(st.principal.role) + ')' +
        (st.warnings.length ? '<ul class="sec-warn">' + st.warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '');
      var K = [['Failed authentication', s.failed_authentication, s.failed_auth_last_5min + ' in last 5 min'], ['Unauthorized requests', s.unauthorized_requests, (s.unauthenticated_requests || 0) + ' without credentials'],
        ['Rejected telemetry', t.rejected, t.malformed + ' malformed'], ['Unknown devices', s.unknown_device_attempts, ''], ['Replay attempts', s.replay_attempts, ''],
        ['Duplicate messages', s.duplicate_messages, ''], ['Spoof attempts', s.spoof_attempts, ''], ['Quarantined devices', s.quarantined_devices, ''],
        ['Offline devices', m.devices.offline, m.devices.total + ' registered'], ['Control commands', s.control_commands_last_5min, 'last 5 min'],
        ['Rate-limit violations', s.rate_limit_violations, ''], ['Accepted telemetry', t.accepted, t.stale + ' stale (data quality)']];
      el('.sec-kpis').innerHTML = K.map(function (k) {
        var warn = k[0] !== 'Accepted telemetry' && k[0] !== 'Control commands' && k[1] > 0;
        return '<div class="bd-kpi' + (warn ? ' bd-k-warning' : '') + '" role="listitem"><div class="bd-kpi-h"><span>' + esc(k[0]) + '</span></div><div class="bd-kpi-v">' + esc(k[1]) + '</div><div class="bd-kpi-s">' + esc(k[2]) + '</div></div>';
      }).join('');
      var dq = m.data_quality, th = st.telemetry_health;
      el('.sec-health').innerHTML = '<table class="bd-table"><tbody>' +
        '<tr><th>API</th><td>' + esc(st.api.status) + '</td></tr>' +
        '<tr><th>Telemetry</th><td>' + esc(th.status) + ' · ' + esc(dq.healthy) + ' healthy / ' + esc(dq.stale) + ' stale / ' + esc(dq.invalid) + ' invalid <span class="bd-muted">(data quality, not security)</span></td></tr>' +
        '<tr><th>Devices</th><td>' + esc(m.devices.total) + ' registered · ' + esc(m.devices.simulated) + ' SIMULATED · ' + esc(m.devices.hardware) + ' hardware · ' + Object.keys(m.devices.by_status).map(function (k) { return esc(k) + ' ' + esc(m.devices.by_status[k]); }).join(', ') + '</td></tr>' +
        '<tr><th>Simulator</th><td>' + esc(st.simulator.steps) + ' steps · speed ' + esc(st.simulator.speed) + '× · ' + (st.simulator.subsystem_errors.length ? '<span class="bd-err">' + st.simulator.subsystem_errors.length + ' subsystem error(s)</span>' : 'no subsystem errors') + ' · simulated hardware ' + (st.simulator.simulated_hardware ? 'on' : 'off') + '</td></tr>' +
        '<tr><th>Sessions</th><td>' + esc(st.active_sessions) + ' active · token TTL ' + esc(c.token_ttl_s) + ' s</td></tr>' +
        '</tbody></table>';
      var p = st.protocols;
      el('.sec-proto').innerHTML = '<table class="bd-table"><tbody>' +
        '<tr><th>HTTPS device ingest</th><td>' + esc(p.https_device_ingest) + '</td></tr><tr><th>MQTT</th><td>' + esc(p.mqtt) + ' · ' +
        Object.keys(p.mqtt_broker_stats).map(function (k) { return esc(k) + ' ' + esc(p.mqtt_broker_stats[k]); }).join(', ') + '</td></tr>' +
        '<tr><th>MQTTS client</th><td>' + esc(p.mqtts_client) + '</td></tr><tr><th>BACnet / Modbus</th><td>' + esc(p.bacnet_modbus) + '</td></tr>' +
        '<tr><th>CORS origins</th><td><code>' + esc(c.allowed_origins.join(', ')) + '</code></td></tr><tr><th>TLS</th><td>' + (c.tls_configured ? 'configured' : 'not configured') + '</td></tr></tbody></table>';
      el('.sec-dev-note').textContent = dv.note;
      el('.sec-devices').innerHTML = '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>Device</th><th>Type</th><th>Zone</th><th>Status</th><th>Protocol</th><th>Source</th><th>Authenticated</th><th>Last seen</th><th>Firmware</th><th>Credential</th><th>Counts</th>' + (canManage ? '<th>Action</th>' : '') + '</tr></thead><tbody>' +
        dv.devices.map(function (d) {
          var bad = ['QUARANTINED', 'INACTIVE', 'OFFLINE'].indexOf(d.effective_status) >= 0;
          return '<tr' + (bad ? ' class="flagged"' : '') + '><td><code>' + esc(d.device_id) + '</code></td><td>' + esc(d.device_type) + (d.sensor_type ? ' · ' + esc(d.sensor_type) : '') + '</td><td>' + esc(d.zone_id) + ' · ' + esc(d.floor_id) + '</td>' +
            '<td><b class="' + (bad ? 'bd-err' : '') + '">' + esc(d.effective_status) + '</b>' + (d.quarantine_reason ? '<div class="bd-small bd-muted">' + esc(d.quarantine_reason) + '</div>' : '') + '</td><td>' + esc(d.protocol) + '</td>' +
            '<td><span class="bd-src bd-src-' + (d.simulated ? 'sim">SIMULATED' : 'hardware">HARDWARE') + '</span></td><td>' + esc(d.authenticated_state) + '</td><td>' + esc(ago(d.last_seen_age_s)) + '</td><td>' + esc(d.firmware_version || '—') + '</td>' +
            '<td>' + esc(d.credential_state) + ' <span class="bd-muted bd-small">' + esc(d.key_id || '') + '</span></td><td>' + esc(d.accepted) + ' ok / ' + esc(d.rejected) + ' rejected</td>' +
            (canManage ? '<td>' + (d.status === 'QUARANTINED' ? '<button type="button" data-act="reinstate" data-dev="' + esc(d.device_id) + '">Reinstate</button>' : '<button type="button" data-act="quarantine" data-dev="' + esc(d.device_id) + '">Quarantine</button>') + '</td>' : '') + '</tr>';
        }).join('') + '</tbody></table></div>' +
        (dv.unknown_attempts.length ? '<p class="bd-small"><b>Unknown device attempts:</b> ' + dv.unknown_attempts.slice(-10).map(function (u) { return esc(u.device_id) + ' (' + esc(u.transport) + ', ' + esc(when(u.received_at)) + ')'; }).join(' · ') + '</p>' : '');
      el('.sec-rej').innerHTML = dv.recent_rejections.length ? '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>When</th><th>Device</th><th>Metric</th><th>Status</th><th>Reason</th><th>Transport</th></tr></thead><tbody>' +
        dv.recent_rejections.slice().reverse().slice(0, 30).map(function (r) { return '<tr><td>' + esc(when(r.received_at)) + '</td><td><code>' + esc(r.device_id) + '</code></td><td>' + esc(r.metric || '—') + '</td><td>' + esc(r.status) + '</td><td>' + esc(r.reason) + '</td><td>' + esc(r.transport) + '</td></tr>'; }).join('') + '</tbody></table></div>' : '<p class="bd-muted">No rejected telemetry.</p>';
      el('.sec-events').innerHTML = ev.events.length ? '<div class="bd-tablewrap"><table class="bd-table"><thead><tr><th>When</th><th>Event</th><th>Actor</th><th>Action</th><th>Target</th><th>Result</th><th>Reason</th></tr></thead><tbody>' +
        ev.events.slice(0, 40).map(function (e) { return '<tr' + (e.severity === 'high' ? ' class="flagged"' : '') + '><td>' + esc(when(e.timestamp)) + '</td><td>' + esc(e.event_type) + '</td><td>' + esc(e.actor || '—') + '</td><td>' + esc(e.action) + '</td><td>' + esc(e.target || e.zone || '—') + '</td><td>' + esc(e.result) + '</td><td class="bd-small">' + esc(e.reason || '') + '</td></tr>'; }).join('') + '</tbody></table></div>' : '<p class="bd-muted">No events yet.</p>';
    }
    function onClick(e) {
      var b = e.target.closest('button[data-act]');
      if (!b) return;
      FL.post('/api/security/devices/' + encodeURIComponent(b.dataset.dev) + '/' + b.dataset.act, {}).then(load, function (err) { alert(denied(err) ? 'Not permitted.' : 'Action failed.'); });
    }
    return { mount: mount, update: function () {} };
  }

  function injectCSS() {
    if (document.getElementById('sec-css')) return;
    var st = document.createElement('style'); st.id = 'sec-css';
    st.textContent = '.sec-banner{font-size:13px;background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--us);border-radius:8px;padding:8px 11px;}' +
      '.sec-warn{margin:6px 0 0 18px;color:#8a5a00;font-size:12.5px;}.sec td button{font:inherit;font-size:11.5px;border:1px solid var(--border);background:var(--surface);border-radius:6px;padding:1px 8px;cursor:pointer;}';
    document.head.appendChild(st);
  }
  var booted = false;
  function boot() {
    if (booted) return true;
    var FL = window.FL; if (!FL || typeof FL.registerPanel !== 'function') return false;
    booted = true; injectCSS(); FL.registerPanel('security', securityPanel()); return true;
  }
  if (!boot()) document.addEventListener('DOMContentLoaded', boot);
})();
