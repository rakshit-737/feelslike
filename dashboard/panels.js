/* dashboard/panels.js — the ten operator panels behind the FeelsLike shell.
 *
 * WHY THIS FILE EXISTS. index.html owns the shell: the header, the tab nav, the
 * polished overview (race strip, floor plan, energy chart, occupant channel) and
 * the one poll of /api/state that feeds everything. This file owns everything
 * BEHIND the other ten tabs, as one zero-dependency classic script — no module
 * system, no bundler, no CDN, so it works from a file server with the network
 * unplugged, which is the only guarantee that matters on demo day.
 *
 * THE CONTRACT WITH THE SHELL (pinned; this file may not widen it)
 *   window.FL.registerPanel(key, {mount(el), update(state)})
 *   Panel containers are <section class="panel" id="panel-<key>">, exactly one
 *   carries class "on". A panel NEVER touches a node outside the element handed
 *   to its mount(). If window.FL is absent this file logs once and does nothing,
 *   so a half-built shell degrades to the overview instead of throwing on load.
 *
 * FIVE RULES THIS FILE LIVES BY
 * 1. NO FAKE CONTROLS. Every control here posts to a real endpoint and changes
 *    real state. Where a lever does not exist server-side (a direct fan-level
 *    write, a runtime privacy-policy change) the panel shows the real value
 *    read-only and says which endpoint would be needed, rather than shipping a
 *    switch that lies.
 * 2. THE SERVER IS THE TRUTH. A slider shows what /api/conditions echoed back,
 *    never what the knob guessed; a mode picker binds to requested_safety_mode,
 *    never to the effective mode, because the maintenance auto-lockout promotes
 *    the effective one and an operator must not have to fight it.
 * 3. ESCAPE EVERYTHING. esc() wraps every value that reaches innerHTML —
 *    occupant text, zone names, alert evidence, error bodies, demo payloads.
 * 4. ONE PANEL'S BUG IS ONE PANEL'S PROBLEM. Every mount/update runs inside
 *    guard(), which shows the failure inside that panel and stops calling it,
 *    mirroring the backend's _safe() firewall. The shell's loop cannot die here.
 * 5. TOKENS ONLY. Colour comes from the shell's CSS custom properties
 *    (--page --surface --ink --ink2 --muted --grid --border --us --base --good
 *    --warn --crit); intensity comes from fill-opacity and color-mix(), so no
 *    new colour literal is introduced and both charts and chrome restyle with
 *    the shell. Charts are hand-rolled inline SVG with a hover readout each.
 *
 * CADENCES. update(state) runs on every shell poll (~1 s). Panels that own an
 * endpoint fetch it only while their tab is visible, throttled (constraints and
 * decisions 2 s, demo 2 s, maintenance 4 s, analytics 5 s, experiments on
 * demand), and force one immediate fetch on the hidden -> visible transition.
 */
(function () {
  'use strict';

  // =========================================================================
  // 0. boot
  // =========================================================================

  var BOOT_TRIES = 30;          // ~3 s: the shell may publish FL after a fetch
  var booted = false;

  function boot() {
    if (booted) return true;
    var FL = window.FL;
    if (!FL || typeof FL.registerPanel !== 'function') return false;
    booted = true;
    injectCSS();
    for (var i = 0; i < PANELS.length; i++) {
      FL.registerPanel(PANELS[i][0], guard(PANELS[i][0], PANELS[i][1]));
    }
    return true;
  }

  function bootLater() {
    var tries = 0;
    var timer = setInterval(function () {
      if (boot() || ++tries >= BOOT_TRIES) {
        clearInterval(timer);
        if (!booted) {
          console.warn('[FeelsLike panels] window.FL.registerPanel never appeared — ' +
                       'the ten operator panels are not mounted. The overview is unaffected.');
        }
      }
    }, 100);
  }

  // =========================================================================
  // 1. helpers
  // =========================================================================

  var ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  /** The only way a value is allowed to reach innerHTML in this file. */
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/[&<>"']/g, function (c) { return ESCAPES[c]; });
  }

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function f(v, d) { return isNum(v) ? v.toFixed(d === undefined ? 1 : d) : '—'; }
  function sgn(v, d) { return isNum(v) ? (v > 0 ? '+' : '') + v.toFixed(d === undefined ? 1 : d) : '—'; }
  function pctS(v, d) { return isNum(v) ? (v > 0 ? '+' : '') + v.toFixed(d === undefined ? 1 : d) + '%' : '—'; }
  function pctU(v, d) { return isNum(v) ? v.toFixed(d === undefined ? 1 : d) + '%' : '—'; }
  function iN(v) { return isNum(v) ? Math.round(v).toLocaleString('en-IN') : '—'; }
  function sp(v) { return v === null || v === undefined ? 'off' : f(v, 1) + ' °C'; }
  function words(s) { return String(s === null || s === undefined ? '' : s).replace(/_/g, ' '); }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  /** sim-seconds since Monday 00:00 -> "Wed 14:35", the clock /api/state emits. */
  function simClock(t) {
    if (!isNum(t)) return '—';
    var d = Math.floor(t / 86400), h = (t % 86400) / 3600;
    return DAYS[((d % 7) + 7) % 7] + ' ' +
      String(Math.floor(h)).padStart(2, '0') + ':' +
      String(Math.floor((h % 1) * 60)).padStart(2, '0');
  }

  /* Mirrors of two backend tables. They are DISPLAY COPIES: the server never
     sends the weights, and the reason gloss must survive a decision row that
     was logged before a redeploy. Both are pinned in backend/contracts.py. */
  var OBJECTIVE_WEIGHTS = {
    balanced: { comfort: 0.70, energy: 0.30 },
    comfort:  { comfort: 0.95, energy: 0.05 },
    energy:   { comfort: 0.25, energy: 0.75 },
    cost:     { comfort: 0.35, energy: 0.65 },
    carbon:   { comfort: 0.35, energy: 0.65 }
  };
  var OBJECTIVE_NOTE = {
    balanced: 'Comfort leads, energy is a real second voice. The shipped default.',
    comfort:  'Almost any kilowatt-hour is worth spending to hold the band.',
    energy:   'Kilowatt-hours win unless comfort is badly broken.',
    cost:     'Same trade as energy, reported in rupees at the flat tariff.',
    carbon:   'Same trade as energy, reported in kg CO₂ at the grid factor.'
  };
  /* Both reason vocabularies (backend/decisions.py emits the left column,
     backend/contracts.py canonicalises it) so any logged code renders. */
  var REASONS = {
    no_change: 'Base schedule in force; nothing moved the setpoint this step.',
    occupied_base: 'Zone occupied, no active constraint: base occupied setpoint.',
    occupancy_setback: 'Zone unoccupied and none expected soon: setback / HVAC off.',
    setback: 'Zone unoccupied and none expected soon: setback / HVAC off.',
    precool: 'Occupancy expected within 30 min: pre-cool setpoint.',
    complaint_offset: 'An active complaint constraint shifted the setpoint.',
    constraint_applied: 'An active complaint constraint shifted the setpoint.',
    conflict_compromise: 'Opposing complaints arbitrated to a weighted compromise.',
    safety_clamp: 'The requested offset hit the 21.5–29.0 °C safety clamp.',
    clamped: 'The requested offset hit the 21.5–29.0 °C safety clamp.',
    at_capacity: 'Cooling demand exceeds the zone maximum; the setpoint is unreachable.',
    recommend_only: 'Safety mode blocked the write; logged as a recommendation.',
    awaiting_approval: 'Queued for human approval; not applied.',
    locked_out: 'Zone under maintenance lockout; constraint recorded, not applied.',
    locked: 'Zone under maintenance lockout; constraint recorded, not applied.',
    emergency_override: 'Emergency override: complaints ignored, safe band enforced.',
    emergency: 'Emergency override: complaints ignored, safe band enforced.'
  };
  var ISSUES = ['too_hot', 'too_cold', 'stuffy', 'humid', 'drafty', 'other'];
  var BAND = [23.0, 26.5];      // sim/twin.py BAND — the occupied comfort band, °C
  var METRIC_ORDER = ['kwh', 'cost_rs', 'co2_kg', 'viol_min', 'hot_deg_min',
                      'cold_deg_min', 'humid_viol_min', 'at_capacity_min',
                      'mean_temp', 'mean_rh', 'interventions'];

  /* Preferences shared between panels: the anonymous switch the settings panel
     owns governs the complaints the twin panel injects, because they are the
     same POST /api/complaint body. */
  var prefs = { anonymous: false, author: 'operator' };

  // ---- HTTP ---------------------------------------------------------------
  // Deliberately fetch() rather than FL.get/FL.post: the pinned contract does
  // not fix whether those resolve to a Response or to parsed JSON, and a panel
  // that guesses wrong fails silently. Same origin, so no CORS surface.
  function req(method, path, body) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      return r.text().then(function (txt) {
        var j = null;
        try { j = txt ? JSON.parse(txt) : null; } catch (e) { j = null; }
        if (!r.ok) {
          var d = j && j.detail !== undefined ? j.detail : null;
          var msg = typeof d === 'string' ? d : (d ? JSON.stringify(d) : (txt || '').slice(0, 200));
          var err = new Error(msg || ('HTTP ' + r.status));
          err.status = r.status;
          throw err;
        }
        return j;
      });
    });
  }
  function GET(p) { return req('GET', p); }
  function POST(p, b) { return req('POST', p, b === undefined ? {} : b); }

  /** Throttle a promise-returning fetch: at most one in flight, at most 1/ms. */
  function poller(fn, ms) {
    var last = 0, busy = false;
    return function tick(force) {
      var now = Date.now();
      if (busy || (!force && now - last < ms)) return;
      busy = true;
      Promise.resolve()
        .then(fn)
        .catch(function () { /* the panel renders its own error strip */ })
        .then(function () { busy = false; last = Date.now(); });
    };
  }

  // ---- DOM ----------------------------------------------------------------
  /** True while this panel's <section class="panel"> carries "on". */
  function visible(root) {
    var p = root && root.closest ? (root.closest('.panel') || root) : root;
    if (!p || !p.classList) return true;
    return p.classList.contains('panel') ? p.classList.contains('on') : true;
  }

  function tipFor(root) {
    root.classList.add('fl-rel');
    var tip = document.createElement('div');
    tip.className = 'fl-tip';
    tip.hidden = true;
    root.appendChild(tip);                 // inside our own panel, never on body
    return {
      show: function (ev, html) {
        tip.innerHTML = html;
        tip.hidden = false;
        var r = root.getBoundingClientRect();
        var x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
        if (x + tip.offsetWidth > r.width) x = Math.max(4, r.width - tip.offsetWidth - 4);
        if (y + tip.offsetHeight > r.height) y = Math.max(4, y - tip.offsetHeight - 28);
        tip.style.left = x + 'px';
        tip.style.top = y + 'px';
      },
      hide: function () { tip.hidden = true; }
    };
  }

  /** Hover readout for any chart: rects/circles carry data-i into `recs`. */
  function hover(svg, tip, recs, render) {
    if (!svg) return;
    svg.addEventListener('mousemove', function (e) {
      var t = e.target.closest ? e.target.closest('[data-i]') : null;
      if (!t) return tip.hide();
      var rec = recs[+t.getAttribute('data-i')];
      if (!rec) return tip.hide();
      tip.show(e, render(rec));
    });
    svg.addEventListener('mouseleave', function () { tip.hide(); });
  }

  function card(title, right, body, cls) {
    return '<div class="fl-card' + (cls ? ' ' + cls : '') + '"><h3>' + esc(title) +
      (right ? '<span class="fl-right">' + esc(right) + '</span>' : '') + '</h3>' + body + '</div>';
  }
  function empty(title, lines) {
    return '<div class="fl-empty"><b>' + esc(title) + '</b>' +
      lines.map(function (l) { return '<div>' + esc(l) + '</div>'; }).join('') + '</div>';
  }
  function tile(label, value, sub) {
    return '<div class="fl-tile"><div class="fl-lbl">' + esc(label) + '</div>' +
      '<div class="v">' + esc(value) + '</div>' +
      (sub ? '<div class="fl-sub fl-num">' + esc(sub) + '</div>' : '') + '</div>';
  }
  function tiles(rows) {
    return '<div class="fl-grid">' + rows.map(function (r) {
      return tile(r[0], r[1], r[2]);
    }).join('') + '</div>';
  }
  function badge(text, kind) {
    return '<span class="fl-badge' + (kind ? ' fl-b-' + kind : '') + '">' + esc(text) + '</span>';
  }
  function chip(text, strong) {
    return '<span class="fl-chip">' + esc(text) +
      (strong ? ' <b class="fl-num">' + esc(strong) + '</b>' : '') + '</span>';
  }
  function kv(rows) {
    return '<dl class="fl-kv">' + rows.map(function (r) {
      return '<dt>' + esc(r[0]) + '</dt><dd>' + (r[2] ? r[1] : esc(r[1])) + '</dd>';
    }).join('') + '</dl>';
  }
  /** Write a container only when its markup really changed, keeping the scroll
   *  position. update() runs every second; rewriting an unchanged feed would
   *  yank a reader back to the top and re-attach chart listeners on every poll.
   *  Returns true when it wrote, which is the signal to (re)bind hover. */
  function paint(node, html) {
    if (!node || node._flLast === html) return false;
    var top = node.scrollTop || 0;
    node._flLast = html;
    node.innerHTML = html;
    if (top) { try { node.scrollTop = top; } catch (e) { /* not scrollable */ } }
    return true;
  }

  function showErr(node, e) {
    if (!node) return;
    node.hidden = false;
    node.textContent = (e && e.message) ? e.message : String(e);
  }
  function clearErr(node) { if (node) { node.hidden = true; node.textContent = ''; } }

  function guard(key, factory) {
    var inst = null, root = null, dead = false;
    function fatal(e) {
      dead = true;
      console.error('[FeelsLike panels] ' + key + ' panel stopped:', e);
      if (!root) return;
      var d = document.createElement('div');
      d.className = 'fl-err';
      d.textContent = 'This panel stopped: ' + ((e && e.message) || e) +
        ' — every other panel keeps running.';
      root.appendChild(d);
    }
    return {
      mount: function (el) {
        root = el;
        try { inst = factory(); inst.mount(el); } catch (e) { fatal(e); }
      },
      update: function (state) {
        if (dead || !inst) return;
        try { inst.update(state); } catch (e) { fatal(e); }
      }
    };
  }

  /** Panels track visibility so an endpoint is only polled on its own tab. */
  function visibilityGate(root) {
    var was = false;
    return function () {
      var now = visible(root);
      var entered = now && !was;
      was = now;
      return { on: now, entered: entered };
    };
  }

  // ---- charts -------------------------------------------------------------
  var AXIS = 'font-size="10" fill="var(--muted)"';

  /** 24 hour-of-day bars. rows: [{hour, count}] */
  function hourBars(rows, opts) {
    opts = opts || {};
    var W = 560, H = opts.height || 116, L = 26, R = 6, T = 8, B = 18;
    var pw = W - L - R, ph = H - T - B, bw = pw / 24;
    var max = 1;
    rows.forEach(function (r) { max = Math.max(max, r.count || 0); });
    var s = '';
    for (var g = 0; g <= 2; g++) {
      var y = T + ph * g / 2;
      s += '<line x1="' + L + '" y1="' + y.toFixed(1) + '" x2="' + (L + pw) + '" y2="' + y.toFixed(1) +
        '" stroke="var(--grid)"/><text x="' + (L - 4) + '" y="' + (y + 3.5).toFixed(1) +
        '" text-anchor="end" ' + AXIS + '>' + Math.round(max * (2 - g) / 2) + '</text>';
    }
    rows.forEach(function (r, i) {
      var h = ph * (r.count || 0) / max;
      s += '<rect data-i="' + i + '" x="' + (L + i * bw + 1).toFixed(1) + '" y="' + (T + ph - h).toFixed(1) +
        '" width="' + (bw - 2).toFixed(1) + '" height="' + Math.max(0, h).toFixed(1) +
        '" fill="var(--us)" fill-opacity="' + (r.count ? 0.85 : 0.12) + '" rx="2"/>';
      if (i % 3 === 0) {
        s += '<text x="' + (L + i * bw + bw / 2).toFixed(1) + '" y="' + (H - 5) +
          '" text-anchor="middle" ' + AXIS + '>' + String(r.hour).padStart(2, '0') + '</text>';
      }
    });
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" class="fl-chart">' + s + '</svg>';
  }

  /** Horizontal category bars. rows: [{label, value, note}] */
  function hBars(rows, unit) {
    if (!rows.length) return '<div class="fl-note">nothing recorded yet</div>';
    var max = 1;
    rows.forEach(function (r) { max = Math.max(max, Math.abs(r.value) || 0); });
    return '<div class="fl-hbars">' + rows.map(function (r) {
      var w = clamp(100 * Math.abs(r.value || 0) / max, 0, 100);
      var val = (isNum(r.value) ? f(r.value, r.dp === undefined ? 0 : r.dp) : r.value) +
        (unit ? ' ' + unit : '');
      // Direct value labels, so these bars need no hover to be readable; the
      // title carries the secondary number (share of total) when there is one.
      return '<div class="fl-hbar" title="' + esc(r.label + ' — ' + val +
        (isNum(r.note) ? ' (' + f(r.note, 1) + '% of total)' : '')) + '">' +
        '<span class="fl-hbar-l">' + esc(r.label) + '</span>' +
        '<span class="fl-hbar-t"><i style="width:' + w.toFixed(1) + '%"></i></span>' +
        '<span class="fl-hbar-v fl-num">' + esc(val) + '</span></div>';
    }).join('') + '</div>';
  }

  // =========================================================================
  // 2. twin — live building conditions
  // =========================================================================

  var KNOBS = [
    { k: 'outdoor_offset', label: 'Outdoor temperature', min: -10, max: 10, step: 0.5,
      fmt: function (v) { return sgn(v, 1) + ' °C'; },
      help: 'added to the weather trace both twins run on' },
    { k: 'solar_scale', label: 'Solar gain', min: 0, max: 2, step: 0.05,
      fmt: function (v) { return f(v, 2) + '×'; },
      help: 'window gain multiplier — 0 is a fully overcast day' },
    { k: 'humidity_offset', label: 'Outdoor humidity', min: -30, max: 30, step: 1,
      fmt: function (v) { return sgn(v, 0) + ' %RH'; },
      help: 'points of relative humidity added outdoors' },
    { k: 'occ_scale', label: 'Occupancy', min: 0, max: 3, step: 0.05,
      fmt: function (v) { return f(v, 2) + '×'; },
      help: 'people vs the weekday schedule — body heat and moisture scale with it' },
    { k: 'capacity_scale', label: 'HVAC capacity', min: 0.1, max: 1.5, step: 0.05,
      fmt: function (v) { return f(v, 2) + '×'; },
      help: 'rated cooling actually available — drop it to fake a fouled coil' }
  ];

  function twinPanel() {
    var root, errBox, sendErr, sendOut, zoneBox, resetBox, confirmTimer = null;
    var pending = {}, timers = {}, clamped = {}, confirming = false;

    function sliderRow(kn) {
      return '<div class="fl-slider" data-k="' + kn.k + '">' +
        '<div><div class="fl-slider-l">' + esc(kn.label) + '</div>' +
        '<div class="fl-note">' + esc(kn.help) + '</div></div>' +
        '<input class="fl-range" type="range" min="' + kn.min + '" max="' + kn.max +
        '" step="' + kn.step + '" value="' + kn.min + '" aria-label="' + esc(kn.label) + '">' +
        '<div class="fl-slider-v"><b class="fl-num" data-role="val">—</b>' +
        '<span class="fl-note" data-role="pend"></span></div></div>';
    }

    function flush() {
      var body = {};
      var any = false;
      for (var k in pending) if (pending.hasOwnProperty(k)) { body[k] = pending[k]; any = true; }
      if (!any) return;
      POST('/api/conditions', body).then(function (r) {
        clearErr(errBox);
        clamped = {};
        (r.clamped || []).forEach(function (k) { clamped[k] = true; });
        for (var k in body) if (body.hasOwnProperty(k)) delete pending[k];
        paintKnobs(r.conditions || {});
      }).catch(function (e) {
        showErr(errBox, e);
        for (var k in body) if (body.hasOwnProperty(k)) delete pending[k];
      });
    }

    function paintKnobs(cond) {
      KNOBS.forEach(function (kn) {
        var row = root.querySelector('.fl-slider[data-k="' + kn.k + '"]');
        if (!row) return;
        var rng = row.querySelector('input');
        var val = row.querySelector('[data-role="val"]');
        var pen = row.querySelector('[data-role="pend"]');
        var server = cond[kn.k];
        if (isNum(server)) {
          val.textContent = kn.fmt(server);
          if (!(kn.k in pending) && document.activeElement !== rng) rng.value = String(server);
        }
        if (kn.k in pending) {
          pen.textContent = 'requested ' + kn.fmt(pending[kn.k]) + ' …';
          pen.className = 'fl-note fl-pend';
        } else if (clamped[kn.k]) {
          pen.textContent = 'clamped to the twin’s legal range';
          pen.className = 'fl-note fl-warn';
        } else {
          pen.textContent = 'confirmed by the twin';
          pen.className = 'fl-note';
        }
      });
    }

    function paintZones(state) {
      var zs = (state && state.zones) || [];
      if (!zs.length) {
        paint(zoneBox, empty('Waiting for the first poll',
          ['Zone readings appear as soon as /api/state answers.']));
        return;
      }
      paint(zoneBox, '<div class="fl-scroll"><table class="fl-table"><tr>' +
        ['Zone', 'Temp', 'Baseline', 'Setpoint', 'Fan', 'People', 'RH', 'Dew', 'Cooling', 'Constraints']
          .map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') + '</tr>' +
        zs.map(function (z) {
          var flags = [];
          if (z.at_capacity) flags.push(badge('AT CAPACITY', 'crit'));
          if (z.locked_out) flags.push(badge('LOCKED', 'warn'));
          if (z.conflict) flags.push(badge('CONFLICT', 'warn'));
          if (z.pending_constraints) flags.push(badge(z.pending_constraints + ' PENDING', 'muted'));
          return '<tr><td>' + esc(z.name) + ' ' + flags.join(' ') + '</td>' +
            '<td class="fl-num">' + f(z.temp, 1) + '</td>' +
            '<td class="fl-num fl-dim">' + f(z.base_temp, 1) + '</td>' +
            '<td class="fl-num">' + esc(sp(z.setpoint)) +
            (z.offset ? ' <span class="fl-dim">(' + esc(sgn(z.offset, 1)) + ')</span>' : '') + '</td>' +
            '<td class="fl-num">' + esc(String(z.vent)) + '</td>' +
            '<td class="fl-num">' + iN(z.occ) + (isNum(z.occ_pct) ? ' <span class="fl-dim">' + f(z.occ_pct, 0) + '%</span>' : '') + '</td>' +
            '<td class="fl-num">' + f(z.rh, 0) + '%</td>' +
            '<td class="fl-num">' + f(z.dew_point_c, 1) + '</td>' +
            '<td class="fl-num">' + f(z.capacity_pct, 0) + '%</td>' +
            '<td class="fl-num">' + iN(z.active_constraints) + '</td></tr>';
        }).join('') + '</table></div>');
    }

    function inject(text) {
      var out = sendOut;
      clearErr(sendErr);
      out.textContent = 'sending…';
      POST('/api/complaint', {
        text: text,
        author: (root.querySelector('#fl-twin-author').value || 'operator').slice(0, 64),
        anonymous: prefs.anonymous
      }).then(function (r) {
        var zones = (r.zones || []).join(', ');
        out.innerHTML = badge(String(r.action || '').split(' ')[0].toUpperCase(),
          r.action === 'applied' ? 'good' : (r.action === 'cleared' ? 'us' : 'muted')) +
          ' <span class="fl-num">' + esc(r.source) + ' · ' + esc(String(r.latency_ms)) + ' ms</span> ' +
          esc(r.action || '') + (zones ? ' <span class="fl-dim">[' + esc(zones) + ']</span>' : '') +
          ((r.redacted && r.redacted.length) ? ' ' + badge('PII SCRUBBED: ' + r.redacted.join(', '), 'warn') : '');
        var box = root.querySelector('#fl-twin-msg');
        if (box) box.value = '';
      }).catch(function (e) { out.textContent = ''; showErr(sendErr, e); });
    }

    function paintReset() {
      resetBox.innerHTML = confirming
        ? '<div class="fl-confirm"><div class="fl-sub">This rebuilds both twins, the constraint store, ' +
          'comfort memory, the decision log, the maintenance monitor, the analytics sampler and the feed ' +
          'at the configured start hour. Speed, objective and the requested safety mode survive — they are ' +
          'configuration, not state.</div><div class="fl-row" style="margin-top:6px">' +
          '<button class="fl-btn fl-btn-danger" data-act="do">CONFIRM RESET</button>' +
          '<button class="fl-btn" data-act="cancel">Cancel</button>' +
          '<span class="fl-note">cancels itself in a few seconds</span></div></div>'
        : '<button class="fl-btn fl-btn-danger" data-act="ask">RESET SIMULATION</button>' +
          '<span class="fl-note"> — two-step, because it throws the demo away</span>';
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML =
          card('Live conditions', 'applied to BOTH twins — the A/B race stays fair',
            '<div class="fl-sliders">' + KNOBS.map(sliderRow).join('') + '</div>' +
            '<div class="fl-err" hidden id="fl-twin-err"></div>' +
            '<div class="fl-note" style="margin-top:8px">Values are what /api/conditions echoed back, ' +
            'not what the slider guessed. Out-of-range requests are clamped by the twin and said so.</div>') +
          card('Zones right now', 'FeelsLike twin · baseline column is the static-22 partner',
            '<div id="fl-twin-zones"></div>') +
          card('Speak to the building', 'the real parser, the real constraint store',
            '<div class="fl-row"><input class="fl-in" id="fl-twin-msg" style="flex:1;min-width:220px" ' +
            'placeholder="e.g. the cafeteria is roasting" autocomplete="off">' +
            '<input class="fl-in" id="fl-twin-author" style="width:120px" value="operator" aria-label="author">' +
            '<button class="fl-btn fl-btn-p" data-act="send">Send</button></div>' +
            '<div class="fl-err" hidden id="fl-twin-senderr"></div>' +
            '<div class="fl-sub" id="fl-twin-sendout" style="margin-top:6px"></div>' +
            '<div class="fl-hr"></div>' +
            '<div class="fl-lbl">Airflow</div>' +
            '<div class="fl-note">There is no direct fan write in this API — the controller owns the fan. ' +
            'Airflow moves the way it does in the real product: through the constraint store. ' +
            '“stuffy” adds +1 fan level, “drafty” takes one away.</div>' +
            '<div class="fl-row" style="margin-top:6px">' +
            '<select class="fl-in" id="fl-twin-zone" aria-label="zone"></select>' +
            '<button class="fl-btn" data-act="air-up">Ask for more air (stuffy)</button>' +
            '<button class="fl-btn" data-act="air-down">Too much draft</button></div>') +
          card('Reset', 'POST /api/reset', '<div id="fl-twin-reset"></div>');

        errBox = root.querySelector('#fl-twin-err');
        sendErr = root.querySelector('#fl-twin-senderr');
        sendOut = root.querySelector('#fl-twin-sendout');
        zoneBox = root.querySelector('#fl-twin-zones');
        resetBox = root.querySelector('#fl-twin-reset');
        paintReset();

        root.addEventListener('input', function (e) {
          var row = e.target.closest ? e.target.closest('.fl-slider') : null;
          if (!row) return;
          var k = row.getAttribute('data-k');
          pending[k] = parseFloat(e.target.value);
          delete clamped[k];
          paintKnobs({});
          clearTimeout(timers[k]);
          timers[k] = setTimeout(flush, 250);          // debounce: one POST per rest
        });

        root.addEventListener('click', function (e) {
          var b = e.target.closest ? e.target.closest('[data-act]') : null;
          if (!b) return;
          var act = b.getAttribute('data-act');
          var zoneSel = root.querySelector('#fl-twin-zone');
          var zname = zoneSel && zoneSel.selectedIndex >= 0 ? zoneSel.options[zoneSel.selectedIndex].text : '';
          if (act === 'send') {
            var v = (root.querySelector('#fl-twin-msg').value || '').trim();
            if (v) inject(v);
          } else if (act === 'air-up' && zname) {
            inject('it feels really stuffy in ' + zname);
          } else if (act === 'air-down' && zname) {
            inject('it is too drafty in ' + zname);
          } else if (act === 'ask') {
            confirming = true; paintReset();
            confirmTimer = setTimeout(function () { confirming = false; paintReset(); }, 6000);
          } else if (act === 'cancel') {
            clearTimeout(confirmTimer); confirming = false; paintReset();
          } else if (act === 'do') {
            clearTimeout(confirmTimer);
            confirming = false; paintReset();
            POST('/api/reset').then(function (s) {
              clearErr(errBox);
              pending = {}; clamped = {};
              paintKnobs((s.sim && s.sim.conditions) || {});
              paintZones(s);
            }).catch(function (err) { showErr(errBox, err); });
          }
        });

        root.querySelector('#fl-twin-msg').addEventListener('keydown', function (e) {
          if (e.key === 'Enter') {
            var v = (e.target.value || '').trim();
            if (v) inject(v);
          }
        });
      },

      update: function (state) {
        if (!state) return;
        if (!visible(root)) return;
        paintKnobs((state.sim && state.sim.conditions) || {});
        paintZones(state);
        var sel = root.querySelector('#fl-twin-zone');
        var ids = (state.zones || []).map(function (z) { return z.id; }).join(',');
        if (sel && sel.getAttribute('data-ids') !== ids) {
          sel.setAttribute('data-ids', ids);
          sel.innerHTML = (state.zones || []).map(function (z) {
            return '<option value="' + esc(z.id) + '">' + esc(z.name) + '</option>';
          }).join('');
        }
      }
    };
  }

  // =========================================================================
  // 3. complaints — the full occupant channel with parse detail
  // =========================================================================

  function complaintsPanel() {
    var root, listBox, countBox, lastState = null;
    var filters = { zone: '', issue: '', source: '' };

    function actionKind(entry) {
      var a = String(entry.action || '');
      if (entry.explanation && entry.explanation.conflict) return ['CONFLICT', 'warn'];
      if (a === 'applied') return ['APPLIED', 'good'];
      if (a.indexOf('all-clear') === 0) return ['ALL-CLEAR', 'us'];
      if (a.indexOf('clarify') === 0) return ['CLARIFY', 'warn'];
      if (a.indexOf('pre-applied') === 0) return ['PRE-APPLIED', 'us'];
      if (a.indexOf('noted') === 0) return ['NOTED', 'muted'];
      return [(a.split(' ')[0] || 'logged').toUpperCase(), 'muted'];
    }

    function keep(e) {
      var p = e.parsed || {};
      var zones = p.zone_ids && p.zone_ids.length ? p.zone_ids : (p.zone_id ? [p.zone_id] : []);
      if (filters.zone && zones.indexOf(filters.zone) < 0) return false;
      if (filters.issue && String(p.issue || '') !== filters.issue) return false;
      if (filters.source && String(e.source || '') !== filters.source) return false;
      return true;
    }

    function render(state) {
      var feed = (state && state.feed) || [];
      var zmap = {};
      (state.zones || []).forEach(function (z) { zmap[z.id] = z.name; });
      var rows = feed.filter(keep);
      countBox.textContent = 'showing ' + rows.length + ' of ' + feed.length +
        ' — newest first, capped at the server’s feed depth';
      if (!rows.length) {
        paint(listBox, feed.length
          ? empty('No message matches these filters', ['Clear a filter to see the rest of the channel.'])
          : empty('The building is listening.', [
              'No complaints yet. Send one from the Twin tab or the occupant channel on Overview.',
              'Every message is PII-scrubbed before it is parsed, so nothing sensitive reaches an external model.']));
        return;
      }
      paint(listBox, rows.map(function (e) {
        var p = e.parsed || {};
        var ak = actionKind(e);
        var conflict = !!(e.explanation && e.explanation.conflict);
        var zones = p.zone_ids && p.zone_ids.length ? p.zone_ids : (p.zone_id ? [p.zone_id] : []);
        var zchips = zones.map(function (z) {
          var c = (p.zone_confidence || {})[z];
          return chip(zmap[z] || z, isNum(c) ? Math.round(c * 100) + '%' : '');
        }).join(' ');
        var meta = [];
        if (p.is_comfort_complaint) {
          meta.push(esc(words(p.issue || 'other')));
          meta.push('severity <b>' + esc(String(p.severity)) + '</b>/3');
          meta.push('confidence <b>' + Math.round((p.confidence || 0) * 100) + '%</b>');
          if (p.language && p.language !== 'en') meta.push(esc(p.language));
          if (p.requires_clarification) meta.push('needs clarification');
        }
        return '<div class="fl-msg' + (conflict ? ' fl-msg-conflict' : '') + '">' +
          '<div class="fl-msg-who fl-num">' + esc(e.author) + ' · ' + esc(e.sim_clock) + ' · ' +
          esc(String(e.latency_ms)) + ' ms · ' + esc(e.source) +
          (e.external_ai ? ' ' + badge('EXTERNAL AI', 'warn') : ' ' + badge('LOCAL', 'good')) +
          (e.author_anonymous ? ' ' + badge('ANON', 'us') : '') + '</div>' +
          '<div class="fl-msg-txt">' + esc(e.text) + '</div>' +
          '<div class="fl-msg-parsed">' + badge(ak[0], ak[1]) + ' ' +
          (p.is_comfort_complaint ? meta.join(' · ') : 'not a comfort complaint') + '</div>' +
          (zchips ? '<div class="fl-msg-zones">' + zchips + '</div>' : '') +
          ((e.constraints && e.constraints.length)
            ? '<div class="fl-note fl-num">constraints ' + esc(e.constraints.join(', ')) +
              (e.complaint_id ? ' · complaint ' + esc(e.complaint_id) : '') + '</div>' : '') +
          ((e.redacted && e.redacted.length)
            ? '<div class="fl-note">' + badge('PII SCRUBBED', 'warn') + ' ' + esc(e.redacted.join(', ')) + '</div>' : '') +
          (e.explanation ? '<div class="fl-msg-expl">' + esc(e.explanation.summary) + '</div>' : '') +
          ((e.explanations && e.explanations.length > 1)
            ? e.explanations.slice(1).map(function (x) {
                return '<div class="fl-msg-expl">' + esc(x.zone_name || x.zone) + ': ' + esc(x.summary) + '</div>';
              }).join('') : '') +
          (String(e.action || '').indexOf('clarify') === 0
            ? '<div class="fl-msg-expl">' + esc(String(e.action).slice(10)) + '</div>' : '') +
          '</div>';
      }).join(''));
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML = card('Occupant channel', 'every message, how it was understood, and what it did',
          '<div class="fl-row" id="fl-cf">' +
          '<select class="fl-in" data-fk="zone" aria-label="zone filter"><option value="">every zone</option></select>' +
          '<select class="fl-in" data-fk="issue" aria-label="issue filter"><option value="">every issue</option>' +
          ISSUES.map(function (i) { return '<option value="' + esc(i) + '">' + esc(words(i)) + '</option>'; }).join('') +
          '</select>' +
          '<select class="fl-in" data-fk="source" aria-label="source filter"><option value="">every parser</option></select>' +
          '<button class="fl-btn" data-act="clear">clear filters</button>' +
          '<span class="fl-note" id="fl-cf-count"></span></div>' +
          '<div class="fl-hr"></div><div id="fl-cf-list" class="fl-feed"></div>');
        listBox = root.querySelector('#fl-cf-list');
        countBox = root.querySelector('#fl-cf-count');
        root.addEventListener('change', function (e) {
          var k = e.target.getAttribute && e.target.getAttribute('data-fk');
          if (!k) return;
          filters[k] = e.target.value;
          if (lastState) render(lastState);          // don't wait for the next poll
        });
        root.addEventListener('click', function (e) {
          var b = e.target.closest ? e.target.closest('[data-act="clear"]') : null;
          if (!b) return;
          filters = { zone: '', issue: '', source: '' };
          root.querySelectorAll('[data-fk]').forEach(function (s) { s.value = ''; });
          if (lastState) render(lastState);
        });
      },
      update: function (state) {
        if (!state || !visible(root)) return;
        lastState = state;
        var zsel = root.querySelector('[data-fk="zone"]');
        var ids = (state.zones || []).map(function (z) { return z.id; }).join(',');
        if (zsel.getAttribute('data-ids') !== ids) {
          zsel.setAttribute('data-ids', ids);
          zsel.innerHTML = '<option value="">every zone</option>' + (state.zones || []).map(function (z) {
            return '<option value="' + esc(z.id) + '">' + esc(z.name) + '</option>';
          }).join('');
          zsel.value = filters.zone;
        }
        var srcs = {};
        (state.feed || []).forEach(function (e) { if (e.source) srcs[e.source] = 1; });
        var skeys = Object.keys(srcs).sort().join(',');
        var ssel = root.querySelector('[data-fk="source"]');
        if (ssel.getAttribute('data-keys') !== skeys) {
          ssel.setAttribute('data-keys', skeys);
          ssel.innerHTML = '<option value="">every parser</option>' + Object.keys(srcs).sort().map(function (s) {
            return '<option value="' + esc(s) + '">' + esc(s) + '</option>';
          }).join('');
          ssel.value = filters.source;
        }
        render(state);
      }
    };
  }

  // =========================================================================
  // 4. control — objective, safety mode, lockouts, approvals
  // =========================================================================

  function controlPanel() {
    var root, objBox, modeBox, lockBox, apprBox, noteBox, errBox, tick, gate;
    var constraints = null, lastState = null;

    function weightBar(o) {
      var w = OBJECTIVE_WEIGHTS[o] || OBJECTIVE_WEIGHTS.balanced;
      return '<span class="fl-wbar" title="comfort ' + Math.round(w.comfort * 100) +
        '% / energy ' + Math.round(w.energy * 100) + '%">' +
        '<i class="c" style="width:' + (w.comfort * 100) + '%"></i>' +
        '<i class="e" style="width:' + (w.energy * 100) + '%"></i></span>';
    }

    function paintController(c) {
      if (!c) return;
      paint(objBox, (c.objectives || Object.keys(OBJECTIVE_WEIGHTS)).map(function (o) {
        var w = OBJECTIVE_WEIGHTS[o] || OBJECTIVE_WEIGHTS.balanced;
        return '<button class="fl-opt' + (o === c.objective ? ' on' : '') + '" data-obj="' + esc(o) + '">' +
          '<div class="fl-opt-h">' + esc(o) + (o === c.objective ? ' ' + badge('IN FORCE', 'good') : '') + '</div>' +
          weightBar(o) +
          '<div class="fl-note fl-num">comfort ' + Math.round(w.comfort * 100) + '% · energy ' +
          Math.round(w.energy * 100) + '%</div>' +
          '<div class="fl-note">' + esc(OBJECTIVE_NOTE[o] || '') + '</div></button>';
      }).join(''));

      var modes = c.safety_modes || {};
      paint(modeBox, Object.keys(modes).map(function (m) {
        var req = m === c.requested_safety_mode, eff = m === c.safety_mode;
        return '<button class="fl-opt' + (req ? ' on' : '') + (eff && !req ? ' eff' : '') +
          '" data-mode="' + esc(m) + '">' +
          '<div class="fl-opt-h">' + esc(words(m)) +
          (req ? ' ' + badge('REQUESTED', 'good') : '') +
          (eff ? ' ' + badge('EFFECTIVE', 'us') : '') + '</div>' +
          '<div class="fl-note">' + esc(modes[m]) + '</div></button>';
      }).join(''));

      paint(noteBox, c.note
        ? '<div class="fl-warnbox">' + esc(c.note) + '</div>'
        : '<div class="fl-note">Requested and effective mode agree. The picker binds to the ' +
          'REQUESTED mode — a capacity alert may promote the effective one to maintenance lockout ' +
          'and hand it back when the alert clears.</div>');

      var auto = c.auto_locked_zones || [], op = c.operator_locked_zones || [];
      var zones = (lastState && lastState.zones) || [];
      var locks = zones.length ? zones.map(function (z) {
        var isAuto = auto.indexOf(z.id) >= 0, isOp = op.indexOf(z.id) >= 0;
        return '<div class="fl-lock">' +
          '<span>' + esc(z.name) + (isAuto ? ' ' + badge('AUTO — CAPACITY ALERT', 'warn') : '') + '</span>' +
          '<button class="fl-btn' + (isOp ? ' on' : '') + '" data-lock="' + esc(z.id) +
          '" data-on="' + (isOp ? '1' : '0') + '">' + (isOp ? 'operator lock ON' : 'lock this zone') +
          '</button></div>';
      }).join('') : '<div class="fl-note">waiting for zones…</div>';

      var recs = c.pending_recommendations || [];
      paint(lockBox, locks + (recs.length
        ? '<div class="fl-hr"></div><div class="fl-lbl">Pending recommendations</div>' +
          '<div class="fl-note">' + recs.length + ' decision(s) computed but not written, because the ' +
          'safety mode withholds them.</div>'
        : ''));
    }

    function paintApprovals() {
      if (!constraints) { paint(apprBox, '<div class="fl-note">loading…</div>'); return; }
      var p = constraints.pending || [];
      var s = constraints.stats || {};
      var head = '<div class="fl-note fl-num">store: ' + iN(s.total) + ' filed · ' + iN(s.active) +
        ' active · ' + iN(s.pending) + ' awaiting approval · ' + iN(s.rejected) + ' rejected</div>';
      if (!p.length) {
        paint(apprBox, head + empty('Nothing is waiting on you', [
          'Complaints are applied as they arrive in this safety mode.',
          'Switch to “human approval” above and the queue below fills instead.']));
        return;
      }
      paint(apprBox, head + '<div class="fl-scroll"><table class="fl-table"><tr>' +
        ['Zone', 'Issue', 'Sev', 'Conf', 'Weight', 'Age', 'Expires in', 'Occupant said', ''].map(function (h) {
          return '<th>' + esc(h) + '</th>';
        }).join('') + '</tr>' + p.map(function (c) {
          return '<tr><td>' + esc(c.zone_name || c.zone) + '</td>' +
            '<td>' + esc(words(c.issue)) + '</td>' +
            '<td class="fl-num">' + esc(String(c.severity)) + '</td>' +
            '<td class="fl-num">' + pctU((c.confidence || 0) * 100, 0) + '</td>' +
            '<td class="fl-num">' + f(c.weight, 2) + '</td>' +
            '<td class="fl-num">' + f(c.age_min, 0) + ' min</td>' +
            '<td class="fl-num">' + f(c.expires_in_min, 0) + ' min</td>' +
            '<td>' + esc(c.text) + '<div class="fl-note">' + esc(c.author) + '</div></td>' +
            '<td class="fl-nowrap"><button class="fl-btn fl-btn-p" data-appr="' + esc(String(c.id)) +
            '">approve</button> <button class="fl-btn fl-btn-danger" data-rej="' + esc(String(c.id)) +
            '">reject</button></td></tr>';
        }).join('') + '</table></div>' +
        '<div class="fl-note">Approving late applies what is LEFT of the complaint — the decay clock ' +
        'is not reset. Rejecting keeps it in history so the pattern miner still learns it happened.</div>');
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML =
          '<div class="fl-err" hidden id="fl-ct-err"></div>' +
          card('Objective', 'how the controller trades comfort against energy',
            '<div class="fl-opts" id="fl-ct-obj"></div>' +
            '<div class="fl-note" style="margin-top:8px">Weights are the pinned table in ' +
            'backend/contracts.py. Changing the objective re-derives the whole schedule — occupied, ' +
            'pre-cool and setback setpoints and the pre-cool lead — from the next step onward. ' +
            'Nothing is retroactive.</div>') +
          card('Safety mode', 'what the controller is allowed to write',
            '<div class="fl-opts" id="fl-ct-mode"></div><div id="fl-ct-note" style="margin-top:8px"></div>') +
          card('Zone lockouts', 'a locked zone holds its base schedule setpoint',
            '<div id="fl-ct-lock"></div>' +
            '<div class="fl-note" style="margin-top:6px">A lockout only bites in maintenance-lockout ' +
            'mode, so locking a zone while the requested mode is “automatic” promotes the effective ' +
            'mode for as long as any lock exists.</div>') +
          card('Approval queue', 'POST /api/constraints/{id}/approve · /reject', '<div id="fl-ct-appr"></div>');
        objBox = root.querySelector('#fl-ct-obj');
        modeBox = root.querySelector('#fl-ct-mode');
        lockBox = root.querySelector('#fl-ct-lock');
        apprBox = root.querySelector('#fl-ct-appr');
        noteBox = root.querySelector('#fl-ct-note');
        errBox = root.querySelector('#fl-ct-err');
        gate = visibilityGate(root);
        tick = poller(function () {
          return GET('/api/constraints').then(function (c) { constraints = c; paintApprovals(); });
        }, 2000);

        root.addEventListener('click', function (e) {
          var t = e.target.closest ? e.target.closest('[data-obj],[data-mode],[data-lock],[data-appr],[data-rej]') : null;
          if (!t) return;
          var body = null, path = '/api/controller';
          if (t.hasAttribute('data-obj')) body = { objective: t.getAttribute('data-obj') };
          else if (t.hasAttribute('data-mode')) body = { safety_mode: t.getAttribute('data-mode') };
          else if (t.hasAttribute('data-lock')) {
            body = t.getAttribute('data-on') === '1'
              ? { unlock_zone: t.getAttribute('data-lock') }
              : { lock_zone: t.getAttribute('data-lock') };
          } else {
            var id = t.getAttribute('data-appr') || t.getAttribute('data-rej');
            path = '/api/constraints/' + encodeURIComponent(id) +
              (t.hasAttribute('data-appr') ? '/approve' : '/reject');
          }
          t.disabled = true;
          POST(path, body === null ? undefined : body).then(function (r) {
            clearErr(errBox);
            if (r && r.active !== undefined) { constraints = r; paintApprovals(); }
            else { paintController(r); tick(true); }
          }).catch(function (err) { showErr(errBox, err); })
            .then(function () { t.disabled = false; });
        });
      },
      update: function (state) {
        lastState = state;
        var g = gate();
        if (!g.on || !state) return;
        paintController(state.controller);
        tick(g.entered);
      }
    };
  }

  // =========================================================================
  // 5. explain — WHY DID THE SYSTEM CHANGE THIS?  (the centrepiece)
  // =========================================================================

  function explainPanel() {
    var root, listBox, detailBox, chartBox, errBox, tip, gate, tick;
    var rows = [], selected = null, zoneFilter = '', detail = null, chartRecs = [];

    function load() {
      var q = '/api/decisions?limit=200' + (zoneFilter ? '&zone=' + encodeURIComponent(zoneFilter) : '');
      return GET(q).then(function (r) {
        clearErr(errBox);
        rows = r.decisions || [];
        if (!selected && rows.length) select(rows[0].id);
        else paintList();
        paintChart();
      }).catch(function (e) { showErr(errBox, e); });
    }

    function select(id) {
      selected = id;
      paintList();
      var cached = rows.filter(function (r) { return r.id === id; })[0] || null;
      detail = cached;
      paintDetail();
      if (!id) return;
      // Re-read the record from its own endpoint: it proves the audit route is
      // real, and it survives a row that was evicted from the inlined 20.
      GET('/api/decisions/' + encodeURIComponent(id)).then(function (r) {
        if (selected !== id) return;
        detail = r.decision || cached;
        paintDetail();
      }).catch(function () { /* the cached row is already on screen */ });
    }

    function paintList() {
      if (!rows.length) {
        paint(listBox, empty('No decisions logged yet', [
          'The log is a CHANGE log, not a sampler: a decision is stored only when the setpoint,',
          'the fan level or the set of active constraints actually moved.',
          'File a complaint, or wait for the next occupancy transition.']));
        return;
      }
      paint(listBox, rows.map(function (d) {
        var moved = d.prev_setpoint !== d.new_setpoint || d.prev_vent !== d.new_vent;
        return '<div class="fl-item' + (d.id === selected ? ' on' : '') + '" data-dec="' + esc(d.id) + '">' +
          '<div class="fl-item-h"><b>' + esc(d.zone_name || d.zone) + '</b>' +
          '<span class="fl-num fl-dim">' + esc(d.sim_clock) + '</span></div>' +
          '<div class="fl-num fl-sub">' + esc(sp(d.prev_setpoint)) + ' → ' + esc(sp(d.new_setpoint)) +
          (d.offset_c ? ' <b>' + esc(sgn(d.offset_c, 1)) + '</b>' : '') +
          (d.prev_vent !== d.new_vent ? ' · fan ' + esc(String(d.prev_vent)) + '→' + esc(String(d.new_vent)) : '') +
          '</div>' +
          '<div>' + badge(String(d.reason_code || '').toUpperCase().replace(/_/g, ' '),
            d.conflict ? 'warn' : (moved ? 'us' : 'muted')) +
          (d.applied ? '' : ' ' + badge('NOT APPLIED', 'warn')) +
          (d.conflict ? ' ' + badge('CONFLICT', 'warn') : '') + '</div></div>';
      }).join(''));
    }

    function paintDetail() {
      var d = detail;
      if (!d) {
        paint(detailBox, empty('Pick a decision', [
          'Every row on the left is one zone, one controller step, and the complete reason it moved.']));
        return;
      }
      var cs = d.constraints || [];
      var w = OBJECTIVE_WEIGHTS[d.objective] || OBJECTIVE_WEIGHTS.balanced;
      var moved = d.prev_setpoint !== d.new_setpoint;
      paint(detailBox,
        '<div class="fl-why">' +
        '<div class="fl-lbl">Why did the system change this?</div>' +
        '<div class="fl-why-h">' + esc(d.zone_name || d.zone) + ' · <span class="fl-num">' +
        esc(d.sim_clock) + '</span> · <span class="fl-num fl-dim">' + esc(d.id) + '</span></div>' +
        '<div class="fl-why-move fl-num">' + esc(sp(d.prev_setpoint)) +
        '<span class="fl-arrow">→</span>' + esc(sp(d.new_setpoint)) +
        (moved ? '' : ' <span class="fl-note">(held)</span>') + '</div>' +
        '<div class="fl-sub">' + esc(d.summary) + '</div>' +
        '<div class="fl-row" style="margin-top:6px">' +
        badge(String(d.reason_code || '').toUpperCase().replace(/_/g, ' '), 'us') +
        badge(d.applied ? 'APPLIED TO THE TWIN' : 'RECOMMENDATION ONLY', d.applied ? 'good' : 'warn') +
        (d.conflict ? badge('ARBITRATED CONFLICT', 'warn') : '') + '</div>' +
        '<div class="fl-note">' + esc(REASONS[d.reason_code] || 'reason code not in the shipped vocabulary') +
        '</div></div>' +

        '<div class="fl-hr"></div>' +
        '<div class="fl-cols">' +
        kv([
          ['Setpoint written', esc(sp(d.new_setpoint)), true],
          ['Base schedule', esc(sp(d.base_setpoint)), true],
          ['Offset from schedule', esc(sgn(d.offset_c, 2)) + ' °C', true],
          ['Fan level', esc(String(d.prev_vent)) + ' → ' + esc(String(d.new_vent)), true],
          ['Objective', esc(d.objective) + ' <span class="fl-note">comfort ' +
            Math.round(w.comfort * 100) + '% / energy ' + Math.round(w.energy * 100) + '%</span>', true],
          ['Safety mode', esc(words(d.safety_mode)), true],
          ['Arbitration', esc(words(d.arbitration)), true]
        ]) +
        kv([
          ['Indoor', f(d.indoor_c, 1) + ' °C'],
          ['Humidity', f(d.rh_pct, 0) + ' %RH'],
          ['Outdoor', f(d.outdoor_c, 1) + ' °C'],
          ['Occupancy', iN(d.occupancy) + ' people (' + f(d.occupancy_pct, 0) + '% of design)'],
          ['Comfort band', BAND[0].toFixed(1) + '–' + BAND[1].toFixed(1) + ' °C'],
          ['Sim time', simClock(d.t)]
        ]) + '</div>' +

        '<div class="fl-hr"></div>' +
        '<div class="fl-lbl">Constraints in force</div>' +
        (cs.length
          ? '<div class="fl-scroll"><table class="fl-table"><tr>' +
            ['#', 'Issue', 'Sev', 'Conf', 'Weight', 'Asks for', 'Age', 'Expires in', 'Occupant said'].map(function (h) {
              return '<th>' + esc(h) + '</th>';
            }).join('') + '</tr>' + cs.map(function (c, i) {
              return '<tr' + (i === 0 ? ' class="fl-lead"' : '') + '><td class="fl-num">' + esc(String(c.id)) + '</td>' +
                '<td>' + esc(words(c.issue)) + '</td>' +
                '<td class="fl-num">' + esc(String(c.severity)) + '</td>' +
                '<td class="fl-num">' + pctU((c.confidence || 0) * 100, 0) + '</td>' +
                '<td class="fl-num"><b>' + f(c.weight, 3) + '</b></td>' +
                '<td class="fl-num">' + sgn(c.raw_offset, 1) + ' °C</td>' +
                '<td class="fl-num">' + f(c.age_min, 0) + ' min</td>' +
                '<td class="fl-num">' + f(c.expires_in_min, 0) + ' min</td>' +
                '<td>' + esc(c.text) + '<div class="fl-note">' + esc(c.author) + '</div></td></tr>';
            }).join('') + '</table></div>' +
            '<div class="fl-note">weight = severity × confidence × decay (45-min half-life, 2 h expiry). ' +
            'The heaviest row is the one actually driving the number.</div>'
          : '<div class="fl-note">No constraint was active — this decision came from the schedule alone.</div>') +

        '<div class="fl-hr"></div>' +
        '<div class="fl-lbl">Estimated impact vs the base schedule</div>' +
        '<div class="fl-grid" style="margin-top:6px">' +
        tile('Energy', pctS(d.est_energy_delta_pct, 1), 'closed-form steady state') +
        tile('Comfort', pctS(d.est_comfort_delta_pct, 1), 'deviation from band midpoint') + '</div>' +
        '<div class="fl-note">Both are labelled <b>est_</b> because they are predictions made before the ' +
        'step from the twin’s own constants, not measurements taken after it. Measured outcomes live in ' +
        'the meters on Overview and in the what-if runs.</div>');
    }

    /** Stepped setpoint timeline for one zone, from the decision change-log. */
    function paintChart() {
      var zone = zoneFilter || (detail && detail.zone) || (rows[0] && rows[0].zone) || '';
      var pts = rows.filter(function (r) { return r.zone === zone; })
        .slice().sort(function (a, b) { return a.t - b.t; });
      if (pts.length < 2) {
        paint(chartBox, '<div class="fl-note">A timeline needs at least two logged changes in one ' +
          'zone. ' + (zone ? esc(zone) + ' has ' + pts.length + ' so far.' : '') + '</div>');
        chartRecs = [];
        return;
      }
      var W = 700, H = 210, L = 40, R = 58, T = 12, B = 24;
      var pw = W - L - R, ph = H - T - B;
      var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
      var vals = [];
      pts.forEach(function (p) {
        if (isNum(p.new_setpoint)) vals.push(p.new_setpoint);
        if (isNum(p.indoor_c)) vals.push(p.indoor_c);
      });
      vals.push(BAND[0], BAND[1]);
      var lo = Math.floor(Math.min.apply(null, vals) - 0.6);
      var hi = Math.ceil(Math.max.apply(null, vals) + 0.6);
      var X = function (t) { return L + pw * (t - t0) / Math.max(1, t1 - t0); };
      var Y = function (v) { return T + ph * (1 - (v - lo) / Math.max(0.1, hi - lo)); };

      var s = '<rect x="' + L + '" y="' + Y(BAND[1]).toFixed(1) + '" width="' + pw + '" height="' +
        Math.max(0, Y(BAND[0]) - Y(BAND[1])).toFixed(1) +
        '" fill="var(--good)" fill-opacity="0.07"/>';
      for (var v = Math.ceil(lo); v <= hi; v++) {
        s += '<line x1="' + L + '" y1="' + Y(v).toFixed(1) + '" x2="' + (L + pw) + '" y2="' + Y(v).toFixed(1) +
          '" stroke="var(--grid)"/><text x="' + (L - 5) + '" y="' + (Y(v) + 3.5).toFixed(1) +
          '" text-anchor="end" ' + AXIS + '>' + v + '</text>';
      }
      var stepPath = '', tempPath = '', prev = null;
      pts.forEach(function (p, i) {
        var x = X(p.t);
        if (isNum(p.new_setpoint)) {
          var y = Y(p.new_setpoint);
          if (prev === null) stepPath += 'M' + x.toFixed(1) + ' ' + y.toFixed(1);
          else stepPath += ' L' + x.toFixed(1) + ' ' + Y(prev).toFixed(1) + ' L' + x.toFixed(1) + ' ' + y.toFixed(1);
          prev = p.new_setpoint;
        } else { prev = null; }                       // HVAC off: break the line
        if (isNum(p.indoor_c)) tempPath += (i === 0 || !tempPath ? 'M' : ' L') + x.toFixed(1) + ' ' + Y(p.indoor_c).toFixed(1);
      });
      if (prev !== null) stepPath += ' L' + (L + pw).toFixed(1) + ' ' + Y(prev).toFixed(1);
      s += '<path d="' + tempPath + '" fill="none" stroke="var(--base)" stroke-width="1.5" stroke-dasharray="3 3"/>';
      s += '<path d="' + stepPath + '" fill="none" stroke="var(--us)" stroke-width="2"/>';
      chartRecs = [];
      pts.forEach(function (p, i) {
        chartRecs.push(p);
        var x = X(p.t), y = isNum(p.new_setpoint) ? Y(p.new_setpoint) : Y((lo + hi) / 2);
        s += '<circle data-i="' + i + '" cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) +
          '" r="4.5" fill="var(--us)" fill-opacity="' + (p.applied ? 0.95 : 0.35) + '"/>';
        s += '<rect data-i="' + i + '" x="' + (x - 9).toFixed(1) + '" y="' + T + '" width="18" height="' +
          ph + '" fill="var(--us)" fill-opacity="0"/>';
      });
      var lastP = pts[pts.length - 1];
      s += '<text x="' + (L + pw + 6) + '" y="' + (Y(isNum(lastP.new_setpoint) ? lastP.new_setpoint : (lo + hi) / 2) + 4).toFixed(1) +
        '" font-size="11" font-weight="600" fill="var(--us)">setpoint</text>';
      if (isNum(lastP.indoor_c)) {
        s += '<text x="' + (L + pw + 6) + '" y="' + (Y(lastP.indoor_c) + 4).toFixed(1) +
          '" font-size="11" fill="var(--base)">zone temp</text>';
      }
      s += '<text x="' + L + '" y="' + (H - 6) + '" ' + AXIS + '>' + esc(simClock(t0)) + '</text>' +
        '<text x="' + (L + pw) + '" y="' + (H - 6) + '" text-anchor="end" ' + AXIS + '>' + esc(simClock(t1)) + '</text>';

      var wrote = paint(chartBox, '<div class="fl-lbl">Setpoint timeline · ' +
        esc((pts[0].zone_name || zone)) + '</div>' +
        '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" class="fl-chart" id="fl-ex-svg">' + s + '</svg>' +
        '<div class="fl-note">Stepped, because a setpoint holds until it is changed. Shaded band is the ' +
        'occupied comfort band. Dashed line is the measured zone temperature at each logged change.</div>');
      // Bind only when the SVG was really replaced, or every poll would stack a
      // fresh listener on the same node.
      if (wrote) {
        hover(root.querySelector('#fl-ex-svg'), tip, chartRecs, function (p) {
          return '<b>' + esc(p.sim_clock) + '</b><br>setpoint ' + esc(sp(p.new_setpoint)) +
            '<br>zone ' + f(p.indoor_c, 1) + ' °C · fan ' + esc(String(p.new_vent)) +
            '<br>' + esc(words(p.reason_code)) + (p.applied ? '' : ' (not applied)');
        });
      }
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML =
          '<div class="fl-err" hidden id="fl-ex-err"></div>' +
          '<div class="fl-two">' +
          card('Decisions', 'newest first · change log, not a sampler',
            '<div class="fl-row"><select class="fl-in" id="fl-ex-zone" aria-label="zone filter">' +
            '<option value="">every zone</option></select>' +
            '<span class="fl-note">one row per material change</span></div>' +
            '<div class="fl-hr"></div><div class="fl-list" id="fl-ex-list"></div>') +
          card('The record', 'generated from controller state — no LLM writes this',
            '<div id="fl-ex-detail"></div>') +
          '</div>' +
          card('Timeline', 'setpoint vs the zone it is steering', '<div id="fl-ex-chart"></div>');
        listBox = root.querySelector('#fl-ex-list');
        detailBox = root.querySelector('#fl-ex-detail');
        chartBox = root.querySelector('#fl-ex-chart');
        errBox = root.querySelector('#fl-ex-err');
        tip = tipFor(root);
        gate = visibilityGate(root);
        tick = poller(load, 2000);
        paintDetail();

        root.addEventListener('click', function (e) {
          var it = e.target.closest ? e.target.closest('[data-dec]') : null;
          if (it) select(it.getAttribute('data-dec'));
        });
        root.querySelector('#fl-ex-zone').addEventListener('change', function (e) {
          zoneFilter = e.target.value;
          selected = null; detail = null;
          tick(true);
        });
      },
      update: function (state) {
        var g = gate();
        if (!g.on) return;
        if (state) {
          var sel = root.querySelector('#fl-ex-zone');
          var ids = (state.zones || []).map(function (z) { return z.id; }).join(',');
          if (sel.getAttribute('data-ids') !== ids) {
            sel.setAttribute('data-ids', ids);
            sel.innerHTML = '<option value="">every zone</option>' + (state.zones || []).map(function (z) {
              return '<option value="' + esc(z.id) + '">' + esc(z.name) + '</option>';
            }).join('');
            sel.value = zoneFilter;
          }
          if (!rows.length && state.decisions && state.decisions.length) {
            rows = state.decisions;                   // instant seed from the poll
            if (!selected) select(rows[0].id); else paintList();
            paintChart();
          }
        }
        tick(g.entered);
      }
    };
  }

  // =========================================================================
  // 6. whatif — counterfactuals on clones, with the isolation proof
  // =========================================================================

  function whatifPanel() {
    var root, selBox, helpBox, outBox, errBox, tip, gate;
    var registry = null, running = false, result = null, recs = [];

    function loadRegistry() {
      return GET('/api/scenarios').then(function (r) {
        registry = r;
        selBox.innerHTML = (r.scenarios || []).map(function (s) {
          return '<option value="' + esc(s.key) + '">' + esc(s.label) + ' — ' + esc(s.kind) + '</option>';
        }).join('');
        root.querySelector('#fl-wi-h').value = String(r.default_horizon_h);
        root.querySelector('#fl-wi-seeds').value = (r.default_seeds || []).join(', ');
        paintHelp();
      }).catch(function (e) { showErr(errBox, e); });
    }

    function paintHelp() {
      if (!registry) return;
      var key = selBox.value;
      var s = (registry.scenarios || []).filter(function (x) { return x.key === key; })[0];
      helpBox.innerHTML = s
        ? esc(s.help) + ' <span class="fl-note fl-num">params ' + esc(JSON.stringify(s.params)) + '</span>'
        : '';
    }

    function run() {
      if (running || !registry) return;
      var h = parseFloat(root.querySelector('#fl-wi-h').value);
      var seedTxt = root.querySelector('#fl-wi-seeds').value || '';
      var seeds = seedTxt.split(',').map(function (x) { return parseInt(x.trim(), 10); })
        .filter(function (x) { return !isNaN(x); });
      if (!(h > 0 && h <= 48)) return showErr(errBox, new Error('horizon must be > 0 and ≤ 48 hours'));
      if (seeds.length < 1 || seeds.length > 5) return showErr(errBox, new Error('give between 1 and 5 seeds'));
      if (seeds.some(function (s) { return s < 0; })) return showErr(errBox, new Error('seeds must be non-negative'));
      clearErr(errBox);
      running = true;
      root.querySelector('#fl-wi-run').disabled = true;
      root.querySelector('#fl-wi-run').textContent = 'running the physics…';
      POST('/api/whatif', { scenario: selBox.value, horizon_h: h, seeds: seeds })
        .then(function (r) { result = r; paintResult(); })
        .catch(function (e) { showErr(errBox, e); })
        .then(function () {
          running = false;
          root.querySelector('#fl-wi-run').disabled = false;
          root.querySelector('#fl-wi-run').textContent = 'RUN';
        });
    }

    function paintResult() {
      if (!result) {
        outBox.innerHTML = empty('Nothing run yet', [
          'Pick a question, choose a horizon and seeds, and press RUN.',
          'Every scenario is stepped on throwaway clones — the live building is never touched.']);
        return;
      }
      var meta = (registry && registry.metrics) || {};
      var d = result.delta || {};
      var scen = result.scenario || {}, base = result.baseline || {};
      var nSeeds = ((scen.per_seed || []).length) || 1;
      var iso = result.isolation_verified;
      var snap = result.snapshot || {};

      var maxPct = 1;
      METRIC_ORDER.forEach(function (k) {
        if (d[k] && isNum(d[k].pct)) maxPct = Math.max(maxPct, Math.abs(d[k].pct));
      });
      recs = [];
      var rowsHtml = METRIC_ORDER.filter(function (k) { return d[k]; }).map(function (k) {
        var m = d[k], mm = meta[k] || {};
        var i = recs.push({ key: k, m: m, unit: mm.unit || '', dir: mm.direction || 'neutral' }) - 1;
        var w = isNum(m.pct) ? clamp(50 * Math.abs(m.pct) / maxPct, 0, 50) : 0;
        var good = mm.direction === 'lower' ? (m.abs < 0) : null;
        var barCol = good === null ? 'var(--base)' : (good ? 'var(--good)' : 'var(--crit)');
        var bar = '<span class="fl-dbar" data-i="' + i + '"><i style="left:' +
          (m.abs < 0 ? 50 - w : 50) + '%;width:' + w + '%;background:' + barCol + '"></i></span>';
        var sd = (scen.sd || {})[k], ci = (scen.ci95 || {})[k];
        return '<tr><td>' + esc(k.replace(/_/g, ' ')) + '<div class="fl-note">' + esc(mm.unit || '') + '</div></td>' +
          '<td class="fl-num">' + f(m.baseline, 2) + '</td>' +
          '<td class="fl-num">' + f(m.scenario, 2) +
          (nSeeds > 1 ? '<div class="fl-note fl-num">sd ' + f(sd, 2) +
            (isNum(ci) && ci > 0 ? ' · ±' + f(ci, 2) + ' CI95' : '') + '</div>' : '') + '</td>' +
          '<td class="fl-num">' + sgn(m.abs, 2) + '</td>' +
          '<td class="fl-num">' + (isNum(m.pct) ? pctS(m.pct, 1) : '<span class="fl-dim">n/a</span>') + '</td>' +
          '<td>' + bar + '</td>' +
          '<td class="fl-sub">' + esc(m.verdict) + '</td></tr>';
      }).join('');

      outBox.innerHTML =
        '<div class="fl-headline">' + esc(result.headline) + '</div>' +
        '<div class="fl-row" style="margin:6px 0">' +
        badge(iso ? 'LIVE STATE UNTOUCHED — FINGERPRINT VERIFIED' : 'ISOLATION CHECK FAILED — TREAT AS SUSPECT',
          iso ? 'good' : 'crit') +
        badge((scen.kind === 'measured' ? 'MEASURED' : String(scen.kind || '').toUpperCase()) +
          ' — the horizon really was stepped', scen.kind === 'measured' ? 'us' : 'warn') +
        badge(nSeeds + ' seed' + (nSeeds === 1 ? '' : 's'), 'muted') + '</div>' +
        '<div class="fl-note">Snapshot taken at <b class="fl-num">' + esc(snap.clock || '—') +
        '</b> · fingerprint <span class="fl-mono">' + esc(String(snap.fingerprint || '').slice(0, 16)) +
        '…</span>. ' + esc(snap.note || '') + '</div>' +
        '<div class="fl-scroll" style="margin-top:8px"><table class="fl-table" id="fl-wi-tbl"><tr>' +
        ['Metric', 'Baseline', 'Scenario', 'Δ abs', 'Δ %', '', 'What that means'].map(function (h) {
          return '<th>' + esc(h) + '</th>';
        }).join('') + '</tr>' + rowsHtml + '</table></div>' +
        '<div class="fl-note">' + esc(result.ci95_note || '') + '</div>' +
        ((scen.per_seed || []).length > 1
          ? '<div class="fl-hr"></div><div class="fl-lbl">Per seed</div><div class="fl-scroll">' +
            '<table class="fl-table"><tr><th>Seed</th><th>kWh</th><th>viol min</th><th>at capacity</th>' +
            '<th>mean °C</th><th>what was perturbed</th></tr>' +
            scen.per_seed.map(function (p) {
              return '<tr><td class="fl-num">' + esc(String(p.seed)) + '</td>' +
                '<td class="fl-num">' + f(p.kwh, 2) + '</td>' +
                '<td class="fl-num">' + f(p.viol_min, 1) + '</td>' +
                '<td class="fl-num">' + f(p.at_capacity_min, 1) + '</td>' +
                '<td class="fl-num">' + f(p.mean_temp, 2) + '</td>' +
                '<td class="fl-sub">' + esc(p.note) + '</td></tr>';
            }).join('') + '</table></div>'
          : '') +
        '<div class="fl-note">Baseline column is the identical clone with an empty perturbation, over the ' +
        'same horizon, seeds and starting state — so the only difference between the columns is the ' +
        'question being asked.</div>';

      var tbl = root.querySelector('#fl-wi-tbl');
      if (tbl) {
        tbl.addEventListener('mousemove', function (e) {
          var t = e.target.closest ? e.target.closest('[data-i]') : null;
          if (!t) return tip.hide();
          var r = recs[+t.getAttribute('data-i')];
          tip.show(e, '<b>' + esc(r.key.replace(/_/g, ' ')) + '</b><br>baseline ' + f(r.m.baseline, 3) +
            ' ' + esc(r.unit) + '<br>scenario ' + f(r.m.scenario, 3) + ' ' + esc(r.unit) +
            '<br>Δ ' + sgn(r.m.abs, 3) + (isNum(r.m.pct) ? ' (' + pctS(r.m.pct, 1) + ')' : '') +
            '<br><span class="fl-note">lower is ' + (r.dir === 'lower' ? 'better' : 'not a score') + '</span>');
        });
        tbl.addEventListener('mouseleave', function () { tip.hide(); });
      }
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML =
          '<div class="fl-err" hidden id="fl-wi-err"></div>' +
          card('Ask a counterfactual', 'runs on clones — POST /api/whatif',
            '<div class="fl-row"><select class="fl-in" id="fl-wi-sel" style="min-width:260px" aria-label="scenario"></select>' +
            '<label class="fl-note">horizon <input class="fl-in fl-num" id="fl-wi-h" type="number" ' +
            'min="0.25" max="48" step="0.25" style="width:80px"> h</label>' +
            '<label class="fl-note">seeds <input class="fl-in fl-num" id="fl-wi-seeds" style="width:110px" ' +
            'placeholder="7, 8, 9"></label>' +
            '<button class="fl-btn fl-btn-p" id="fl-wi-run">RUN</button></div>' +
            '<div class="fl-sub" id="fl-wi-help" style="margin-top:6px"></div>') +
          card('Result', 'baseline vs scenario, measured over the same horizon', '<div id="fl-wi-out"></div>');
        selBox = root.querySelector('#fl-wi-sel');
        helpBox = root.querySelector('#fl-wi-help');
        outBox = root.querySelector('#fl-wi-out');
        errBox = root.querySelector('#fl-wi-err');
        tip = tipFor(root);
        gate = visibilityGate(root);
        paintResult();
        selBox.addEventListener('change', paintHelp);
        root.querySelector('#fl-wi-run').addEventListener('click', run);
      },
      update: function () {
        var g = gate();
        if (!g.on) return;
        if (!registry) loadRegistry();
      }
    };
  }

  // =========================================================================
  // 7. analytics — heatmap, energy, complaints, controller
  // =========================================================================

  function analyticsPanel() {
    var root, box, errBox, tip, gate, tick, data = null;
    var heatRecs = [], energyRecs = [], hourRecs = [], ctrlRecs = [];

    function heatmap(hm) {
      var zones = hm.zones || [], hours = hm.hours || [];
      if (!zones.length) return '<div class="fl-note">no grid yet</div>';
      var L = 116, T = 20, cw = 25, chh = 26, W = L + cw * 24 + 10, H = T + zones.length * chh + 34;
      var max = Math.max(0.25, hm.max_abs_deviation || 0);
      var byKey = {};
      (hm.cells || []).forEach(function (c) { byKey[c.zone + '|' + c.hour] = c; });
      heatRecs = [];
      var s = '';
      hours.forEach(function (h) {
        if (h % 3 === 0) {
          s += '<text x="' + (L + h * cw + cw / 2) + '" y="' + (T - 7) + '" text-anchor="middle" ' + AXIS + '>' +
            String(h).padStart(2, '0') + '</text>';
        }
      });
      zones.forEach(function (z, zi) {
        var y = T + zi * chh;
        s += '<text x="' + (L - 8) + '" y="' + (y + chh / 2 + 4) + '" text-anchor="end" font-size="11" ' +
          'fill="var(--ink2)">' + esc(z.name) + '</text>';
        hours.forEach(function (h) {
          var c = byKey[z.id + '|' + h] || { deviation: 0, occ_samples: 0, samples: 0, viol_min: 0 };
          var i = heatRecs.push({ zone: z.name, hour: h, c: c }) - 1;
          var x = L + h * cw;
          if (!c.occ_samples) {
            s += '<rect data-i="' + i + '" x="' + x + '" y="' + y + '" width="' + (cw - 2) + '" height="' +
              (chh - 2) + '" rx="3" fill="var(--grid)" fill-opacity="0.35"/>';
          } else {
            var op = 0.07 + 0.88 * clamp(Math.abs(c.deviation) / max, 0, 1);
            s += '<rect data-i="' + i + '" x="' + x + '" y="' + y + '" width="' + (cw - 2) + '" height="' +
              (chh - 2) + '" rx="3" fill="var(--crit)" fill-opacity="' + op.toFixed(3) + '"/>';
            if (c.deviation < -0.05) {
              s += '<circle data-i="' + i + '" cx="' + (x + (cw - 2) / 2) + '" cy="' + (y + (chh - 2) / 2) +
                '" r="2.6" fill="var(--us)"/>';
            }
          }
        });
      });
      var legend = [0, 0.25, 0.5, 0.75, 1].map(function (t) {
        return '<rect x="' + (L + t * 120) + '" y="' + (H - 20) + '" width="22" height="10" rx="2" ' +
          'fill="var(--crit)" fill-opacity="' + (0.07 + 0.88 * t).toFixed(2) + '"/>';
      }).join('');
      s += legend +
        '<text x="' + L + '" y="' + (H - 24) + '" ' + AXIS + '>0 °C off midpoint</text>' +
        '<text x="' + (L + 142) + '" y="' + (H - 11) + '" ' + AXIS + '>' + f(max, 1) + ' °C</text>' +
        '<rect x="' + (L + 210) + '" y="' + (H - 20) + '" width="22" height="10" rx="2" fill="var(--grid)" fill-opacity="0.35"/>' +
        '<text x="' + (L + 238) + '" y="' + (H - 11) + '" ' + AXIS + '>nobody in the room</text>' +
        '<circle cx="' + (L + 372) + '" cy="' + (H - 15) + '" r="2.6" fill="var(--us)"/>' +
        '<text x="' + (L + 382) + '" y="' + (H - 11) + '" ' + AXIS + '>cell ran cool, not warm</text>';
      return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" class="fl-chart" id="fl-an-heat">' + s + '</svg>';
    }

    function energyChart(en) {
      var rows = en.hourly || [];
      if (rows.length < 2) return '<div class="fl-note">the hourly series needs two samples — ' +
        'the sampler runs every 15 simulated minutes</div>';
      var W = 700, H = 200, L = 40, R = 62, T = 10, B = 22;
      var pw = W - L - R, ph = H - T - B;
      var max = 0.001;
      rows.forEach(function (r) { max = Math.max(max, r.us || 0, r.base || 0); });
      var X = function (i) { return L + pw * i / Math.max(1, rows.length - 1); };
      var Y = function (v) { return T + ph * (1 - v / max); };
      var s = '';
      for (var g = 0; g <= 3; g++) {
        var y = T + ph * g / 3;
        s += '<line x1="' + L + '" y1="' + y.toFixed(1) + '" x2="' + (L + pw) + '" y2="' + y.toFixed(1) +
          '" stroke="var(--grid)"/><text x="' + (L - 5) + '" y="' + (y + 3.5).toFixed(1) +
          '" text-anchor="end" ' + AXIS + '>' + (max * (3 - g) / 3).toFixed(1) + '</text>';
      }
      var p1 = '', p2 = '';
      energyRecs = rows;
      rows.forEach(function (r, i) {
        p1 += (i ? ' L' : 'M') + X(i).toFixed(1) + ' ' + Y(r.us || 0).toFixed(1);
        p2 += (i ? ' L' : 'M') + X(i).toFixed(1) + ' ' + Y(r.base || 0).toFixed(1);
        s += '<rect data-i="' + i + '" x="' + (X(i) - pw / rows.length / 2).toFixed(1) + '" y="' + T +
          '" width="' + Math.max(2, pw / rows.length).toFixed(1) + '" height="' + ph + '" fill="var(--us)" fill-opacity="0"/>';
      });
      s += '<path d="' + p2 + '" fill="none" stroke="var(--base)" stroke-width="2"/>' +
        '<path d="' + p1 + '" fill="none" stroke="var(--us)" stroke-width="2"/>' +
        '<text x="' + (L + pw + 6) + '" y="' + (Y(rows[rows.length - 1].base || 0) + 4).toFixed(1) +
        '" font-size="11" fill="var(--ink2)">baseline</text>' +
        '<text x="' + (L + pw + 6) + '" y="' + (Y(rows[rows.length - 1].us || 0) + 4).toFixed(1) +
        '" font-size="11" font-weight="600" fill="var(--us)">FeelsLike</text>' +
        '<text x="' + L + '" y="' + (H - 5) + '" ' + AXIS + '>' + esc(rows[0].label) + '</text>' +
        '<text x="' + (L + pw) + '" y="' + (H - 5) + '" text-anchor="end" ' + AXIS + '>' +
        esc(rows[rows.length - 1].label) + '</text>';
      return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" class="fl-chart" id="fl-an-energy">' + s + '</svg>';
    }

    function render() {
      if (!data) { paint(box, '<div class="fl-note">loading analytics…</div>'); return; }
      if ((data.samples || 0) < 2) {
        paint(box, empty('Analytics is still filling up', [
          'The sampler takes one snapshot every 15 simulated minutes; ' + (data.samples || 0) + ' so far.',
          'Raise the sim speed on the header, or run the guided demo, and the grid fills in.']));
        return;
      }
      var hm = data.heatmap || {}, en = data.energy || {}, cs = data.complaints || {}, ct = data.controller || {};
      var tot = en.totals || {};
      hourRecs = cs.by_hour || [];
      ctrlRecs = ct.interventions_by_hour || [];

      var wrote = paint(box,
        card('Window', 'rolling sampler, bounded ring buffer',
          tiles([
            ['Window', f(data.window_h, 1) + ' h', (data.samples || 0) + ' samples'],
            ['Saved', f(tot.saved_kwh, 1) + ' kWh', pctU(tot.saved_pct, 1) + ' of baseline'],
            ['Cost avoided', '₹' + iN(tot.saved_rs), 'flat tariff'],
            ['CO₂ avoided', f(tot.saved_co2, 1) + ' kg', 'grid factor'],
            ['Complaints', iN(cs.total), iN(cs.entries) + ' feed entries'],
            ['Interventions', iN(ct.interventions), pctU(ct.intervention_rate_pct, 1) + ' of decisions']
          ])) +
        card('Comfort heatmap', 'zone × hour of day · mean deviation from the band midpoint',
          heatmap(hm) +
          '<div class="fl-note">Deviation is averaged over OCCUPIED samples only — an empty room at 3 a.m. ' +
          'is not a comfort failure, so it is greyed rather than painted red.</div>') +
        card('Energy', 'per hour of the window, both twins', energyChart(en) +
          (en.by_zone && en.by_zone.length
            ? '<div class="fl-hr"></div><div class="fl-lbl">Where it went</div>' +
              hBars(en.by_zone.map(function (z) {
                return { label: z.name, value: z.kwh, note: z.pct, dp: 2 };
              }), 'kWh')
            : '')) +
        card('Complaints', 'what the building was told, and when',
          tiles([
            ['Mean latency', isNum(cs.mean_latency_ms) ? f(cs.mean_latency_ms, 0) + ' ms' : '—', 'complaint → action'],
            ['p95 latency', isNum(cs.p95_latency_ms) ? f(cs.p95_latency_ms, 0) + ' ms' : '—', ''],
            ['Mean confidence', isNum(cs.mean_confidence) ? pctU(cs.mean_confidence * 100, 0) : '—', 'parser'],
            ['Clarification rate', pctU(cs.clarification_rate, 1), 'zone not named']
          ]) +
          '<div class="fl-hr"></div><div class="fl-lbl">By hour of day</div>' +
          '<div id="fl-an-hours">' + hourBars(cs.by_hour || []) + '</div>' +
          '<div class="fl-cols" style="margin-top:8px">' +
          '<div><div class="fl-lbl">By issue</div>' + hBars((cs.by_issue || []).map(function (r) {
            return { label: words(r.key), value: r.count };
          })) + '</div>' +
          '<div><div class="fl-lbl">By zone</div>' + hBars((cs.by_zone || []).map(function (r) {
            return { label: r.name, value: r.count };
          })) + '</div></div>' +
          ((cs.recurring || []).length
            ? '<div class="fl-hr"></div><div class="fl-lbl">Recurring (2+ of the same thing)</div>' +
              (cs.recurring).map(function (r) {
                return '<div class="fl-sub">' + esc(r.name) + ' · ' + esc(words(r.issue)) +
                  ' <b class="fl-num">×' + esc(String(r.count)) + '</b></div>';
              }).join('')
            : '')) +
        card('Controller activity', 'over the decision log',
          tiles([
            ['Decisions', iN(ct.total), pctU(ct.applied_pct, 1) + ' applied'],
            ['Interventions / sim h', f(ct.interventions_per_sim_hour, 2), 'setpoint actually moved'],
            ['Conflicts', iN(ct.conflicts), pctU(ct.conflict_pct, 1) + ' of decisions'],
            ['Clamped', iN(ct.clamped), 'hit the 21.5–29.0 °C rail'],
            ['Mean offset', f(ct.mean_offset_c, 2) + ' °C', 'max ' + f(ct.max_offset_c, 2) + ' °C']
          ]) +
          '<div class="fl-hr"></div><div class="fl-lbl">Interventions by hour</div>' +
          '<div id="fl-an-ctrl">' + hourBars(ct.interventions_by_hour || []) + '</div>' +
          '<div class="fl-cols" style="margin-top:8px">' +
          '<div><div class="fl-lbl">By reason</div>' + hBars((ct.by_reason || []).map(function (r) {
            return { label: words(r.key), value: r.count };
          })) + '</div>' +
          '<div><div class="fl-lbl">Setpoint changes by zone</div>' +
          hBars((ct.setpoint_changes_by_zone || []).map(function (r) {
            return { label: r.name, value: r.changes };
          })) + '</div></div>'));

      if (!wrote) return;          // same markup: the existing hovers still hold
      hover(root.querySelector('#fl-an-heat'), tip, heatRecs, function (r) {
        var c = r.c;
        return '<b>' + esc(r.zone) + ' · ' + String(r.hour).padStart(2, '0') + ':00</b><br>' +
          (c.occ_samples
            ? 'deviation ' + sgn(c.deviation, 2) + ' °C from ' + ((BAND[0] + BAND[1]) / 2).toFixed(2) + ' °C<br>' +
              'out of band ' + f(c.viol_min, 1) + ' occupied-min<br>' +
              c.occ_samples + ' occupied of ' + c.samples + ' samples'
            : 'nobody in this room at this hour<br>' + c.samples + ' samples');
      });
      hover(root.querySelector('#fl-an-energy'), tip, energyRecs, function (r) {
        return '<b>' + esc(r.label) + '</b><br>FeelsLike ' + f(r.us, 3) + ' kWh<br>baseline ' +
          f(r.base, 3) + ' kWh<br>saved ' + f(r.saved, 3) + ' kWh';
      });
      hover(root.querySelector('#fl-an-hours svg'), tip, hourRecs, function (r) {
        return '<b>' + String(r.hour).padStart(2, '0') + ':00</b><br>' + r.count + ' complaint(s)';
      });
      hover(root.querySelector('#fl-an-ctrl svg'), tip, ctrlRecs, function (r) {
        return '<b>' + String(r.hour).padStart(2, '0') + ':00</b><br>' + r.count + ' intervention(s)';
      });
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML = '<div class="fl-err" hidden id="fl-an-err"></div><div id="fl-an-box"></div>';
        box = root.querySelector('#fl-an-box');
        errBox = root.querySelector('#fl-an-err');
        tip = tipFor(root);
        gate = visibilityGate(root);
        tick = poller(function () {
          return GET('/api/analytics').then(function (r) { clearErr(errBox); data = r; render(); })
            .catch(function (e) { showErr(errBox, e); });
        }, 5000);
        render();
      },
      update: function () {
        var g = gate();
        if (!g.on) return;
        tick(g.entered);
      }
    };
  }

  // =========================================================================
  // 8. maintenance — alert cards with their evidence
  // =========================================================================

  function maintenancePanel() {
    var root, box, errBox, gate, tick, data = null;

    function alertCard(a, resolved) {
      var sev = a.severity === 'high' ? 'crit' : (a.severity === 'medium' ? 'warn' : 'muted');
      return '<div class="fl-alert' + (resolved ? ' resolved' : '') + '">' +
        '<div class="fl-alert-h">' + badge(String(a.kind || '').toUpperCase(), 'us') + ' ' +
        badge(String(a.severity || '').toUpperCase(), sev) +
        '<b>' + esc(a.zone_name || a.zone) + '</b>' +
        '<span class="fl-right fl-num fl-dim">' + esc(a.id) + ' · confidence ' +
        pctU((a.confidence || 0) * 100, 0) + '</span></div>' +
        '<div class="fl-note fl-num">first seen ' + esc(simClock(a.first_seen_t)) +
        ' · last seen ' + esc(simClock(a.last_seen_t)) +
        (resolved ? ' · resolved ' + esc(simClock(a.resolved_t)) : '') + '</div>' +
        '<ul class="fl-ev">' + (a.evidence || []).map(function (e) {
          return '<li>' + esc(e) + '</li>';
        }).join('') + '</ul>' +
        '<div class="fl-rec"><span class="fl-lbl">Recommended</span> ' + esc(a.recommendation) + '</div></div>';
    }

    function render() {
      if (!data) { paint(box, '<div class="fl-note">loading…</div>'); return; }
      var alerts = data.alerts || [], hist = data.history || [], sup = data.suppressed_zones || [];
      paint(box,
        (sup.length
          ? '<div class="fl-warnbox">Setpoint chasing is suppressed in ' + esc(sup.join(', ')) +
            ': the coil there is already saturated, so another degree of demand buys no cooling and only ' +
            'hides the fault. Those zones hold their base schedule setpoint until the alert clears.</div>'
          : '') +
        card('Active alerts', 'four detectors · sampled every ' + f((data.interval_s || 0) / 60, 0) + ' sim-min',
          alerts.length
            ? alerts.map(function (a) { return alertCard(a, false); }).join('')
            : empty('No faults detected', [
                'Capacity, sensor, actuator and recurring-complaint detectors are all watching and quiet.',
                'Nothing fires on a single bad reading — each one needs sustained evidence (30–45 sim-min)',
                'before it will put a technician in a van.'])) +
        (hist.length
          ? card('Resolved', 'symptom absent long enough to close',
              hist.slice().reverse().map(function (a) { return alertCard(a, true); }).join(''))
          : ''));
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML = '<div class="fl-err" hidden id="fl-mt-err"></div><div id="fl-mt-box"></div>';
        box = root.querySelector('#fl-mt-box');
        errBox = root.querySelector('#fl-mt-err');
        gate = visibilityGate(root);
        tick = poller(function () {
          return GET('/api/maintenance').then(function (r) { clearErr(errBox); data = r; render(); })
            .catch(function (e) { showErr(errBox, e); });
        }, 4000);
        render();
      },
      update: function () {
        var g = gate();
        if (!g.on) return;
        tick(g.entered);
      }
    };
  }

  // =========================================================================
  // 9. experiments — the committed multi-seed sweep
  // =========================================================================

  function experimentsPanel() {
    var root, box, errBox, tip, gate, tick, data = null, recs = [];

    function whiskers(rows) {
      if (!rows.length) return '';
      var W = 700, L = 168, R = 60, T = 12, rowH = 22, H = T + rows.length * rowH + 26;
      var pw = W - L - R;
      var max = 1;
      rows.forEach(function (r) { max = Math.max(max, Math.abs(r.pct) + Math.abs(r.ciPct)); });
      var X = function (v) { return L + pw * (0.5 + 0.5 * clamp(v / max, -1, 1)); };
      var s = '<line x1="' + X(0) + '" y1="' + T + '" x2="' + X(0) + '" y2="' + (T + rows.length * rowH) +
        '" stroke="var(--grid)"/>';
      rows.forEach(function (r, i) {
        var y = T + i * rowH + rowH / 2;
        var x = X(r.pct), lo = X(r.pct - r.ciPct), hi = X(r.pct + r.ciPct);
        s += '<text x="' + (L - 8) + '" y="' + (y + 3.5) + '" text-anchor="end" font-size="11" fill="var(--ink2)">' +
          esc(r.label) + '</text>' +
          '<line data-i="' + i + '" x1="' + lo.toFixed(1) + '" y1="' + y + '" x2="' + hi.toFixed(1) + '" y2="' + y +
          '" stroke="var(--base)" stroke-width="2"/>' +
          '<circle data-i="' + i + '" cx="' + x.toFixed(1) + '" cy="' + y + '" r="4.5" fill="' +
          (r.pct < 0 ? 'var(--good)' : 'var(--crit)') + '"/>' +
          '<rect data-i="' + i + '" x="' + L + '" y="' + (y - rowH / 2) + '" width="' + pw + '" height="' + rowH +
          '" fill="var(--us)" fill-opacity="0"/>';
      });
      s += '<text x="' + X(0) + '" y="' + (H - 8) + '" text-anchor="middle" ' + AXIS + '>0%</text>' +
        '<text x="' + L + '" y="' + (H - 8) + '" ' + AXIS + '>−' + f(max, 0) + '% energy</text>' +
        '<text x="' + (L + pw) + '" y="' + (H - 8) + '" text-anchor="end" ' + AXIS + '>+' + f(max, 0) + '%</text>';
      return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" class="fl-chart" id="fl-xp-svg">' + s + '</svg>';
    }

    function render() {
      if (!data) { paint(box, '<div class="fl-note">loading…</div>'); return; }
      if (!data.available) {
        paint(box, empty('No saved sweep on disk', [
          data.note || '',
          'The API never regenerates this file, on purpose: what a judge sees here is exactly what was committed.']));
        return;
      }
      var sc = data.scenarios || {};
      var keys = Object.keys(sc);
      recs = [];
      var rows = keys.map(function (k) {
        var e = sc[k];
        var d = (e.delta || {}).kwh || {};
        var ci = ((e.scenario || {}).ci95 || {}).kwh || 0;
        var basemean = ((e.baseline || {}).mean || {}).kwh || 0;
        var rec = {
          key: k, label: e.label || k, family: e.family || '',
          pct: isNum(d.pct) ? d.pct : 0,
          ciPct: basemean ? 100 * ci / Math.abs(basemean) : 0,
          abs: d.abs, base: d.baseline, scen: d.scenario,
          viol: ((e.delta || {}).viol_min || {}),
          headline: e.headline || ''
        };
        recs.push(rec);
        return rec;
      });

      var wrote = paint(box,
        card('Saved experiment sweep', esc(data.path || ''),
          tiles([
            ['Scenarios', String(keys.length), 'from the registry'],
            ['Seeds', (data.seeds || []).join(', ') || '—', 'per scenario'],
            ['Horizon', f(data.horizon_h, 1) + ' h', 'simulated per run'],
            ['Isolation', data.isolation_held === true ? 'HELD' : (data.isolation_held === false ? 'FAILED' : '—'),
              'live state fingerprint'],
            ['Wall clock', f(data.elapsed_s, 2) + ' s', 'to produce the file']
          ]) +
          '<div class="fl-note" style="margin-top:8px">' + esc(data.ci95_note || '') + '</div>') +
        card('Energy effect, with confidence', 'Δ kWh as a percentage of each scenario’s own baseline',
          whiskers(rows) +
          '<div class="fl-note">The whisker is the SCENARIO’s 95% CI across seeds, expressed against the ' +
          'baseline mean. Seeds are paired between the two columns, so the baseline’s own spread is not ' +
          'drawn here — read the table for both.</div>') +
        card('Every scenario', 'measured, not predicted',
          '<div class="fl-scroll"><table class="fl-table"><tr>' +
          ['Scenario', 'Family', 'Baseline kWh', 'Scenario kWh', 'Δ kWh', 'Δ %', 'Δ out-of-band min', 'Headline']
            .map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') + '</tr>' +
          rows.map(function (r) {
            return '<tr><td>' + esc(r.label) + '</td><td class="fl-sub">' + esc(r.family) + '</td>' +
              '<td class="fl-num">' + f(r.base, 2) + '</td>' +
              '<td class="fl-num">' + f(r.scen, 2) + '</td>' +
              '<td class="fl-num">' + sgn(r.abs, 2) + '</td>' +
              '<td class="fl-num">' + (isNum(r.pct) ? pctS(r.pct, 1) : '—') + '</td>' +
              '<td class="fl-num">' + sgn(r.viol.abs, 1) + '</td>' +
              '<td class="fl-sub">' + esc(r.headline) + '</td></tr>';
          }).join('') + '</table></div>'));

      if (!wrote) return;          // same markup: the existing hover still holds
      hover(root.querySelector('#fl-xp-svg'), tip, recs, function (r) {
        return '<b>' + esc(r.label) + '</b><br>baseline ' + f(r.base, 2) + ' kWh<br>scenario ' +
          f(r.scen, 2) + ' kWh<br>Δ ' + sgn(r.abs, 2) + ' kWh (' + pctS(r.pct, 1) + ')<br>' +
          '<span class="fl-note">±' + f(r.ciPct, 1) + '% CI95 across seeds</span>';
      });
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML = '<div class="fl-err" hidden id="fl-xp-err"></div>' +
          '<div class="fl-row"><button class="fl-btn" id="fl-xp-reload">re-read the file</button>' +
          '<span class="fl-note">GET /api/experiments — read straight from evals/results_whatif.json</span></div>' +
          '<div id="fl-xp-box"></div>';
        box = root.querySelector('#fl-xp-box');
        errBox = root.querySelector('#fl-xp-err');
        tip = tipFor(root);
        gate = visibilityGate(root);
        tick = poller(function () {
          return GET('/api/experiments').then(function (r) { clearErr(errBox); data = r; render(); })
            .catch(function (e) { showErr(errBox, e); });
        }, 30000);
        root.querySelector('#fl-xp-reload').addEventListener('click', function () { tick(true); });
        render();
      },
      update: function () {
        var g = gate();
        if (!g.on) return;
        tick(g.entered);
      }
    };
  }

  // =========================================================================
  // 10. settings — disclosure, retention, export, redaction
  // =========================================================================

  function settingsPanel() {
    var root, box, errBox, exportBox, gate, exported = null;
    var lastPrivacy = null, lastController = null, pKey = '', cKey = '';

    function render() {
      var p = lastPrivacy || {}, c = lastController || {};
      var ai = p.ai_disclosure || {}, audit = p.retention_audit || {};
      var modes = c.safety_modes || {};
      box.innerHTML =
        card('Is a model reading my messages?', 'the disclosure an occupant is owed',
          '<div class="' + (ai.external ? 'fl-warnbox' : 'fl-goodbox') + '">' +
          badge(ai.external ? 'EXTERNAL AI IN USE' : 'LOCAL ONLY', ai.external ? 'warn' : 'good') + ' ' +
          esc(ai.text || '') + '</div>' +
          '<div class="fl-note fl-num">last parse ran on: ' + esc(ai.source || '—') +
          (ai.provider ? ' · provider ' + esc(ai.provider) : '') + '. Personally identifying text is ' +
          'scrubbed BEFORE parsing, so a phone number never reaches a third party even when the LLM path ' +
          'is live.</div>') +
        card('Safety mode', 'the same lever as the Control tab, bound to the REQUESTED mode',
          '<div class="fl-row"><select class="fl-in" id="fl-st-mode" aria-label="safety mode">' +
          Object.keys(modes).map(function (m) {
            return '<option value="' + esc(m) + '"' + (m === c.requested_safety_mode ? ' selected' : '') + '>' +
              esc(words(m)) + '</option>';
          }).join('') + '</select>' +
          '<button class="fl-btn fl-btn-p" data-act="mode">apply</button>' +
          '<span class="fl-note">effective right now: <b>' + esc(words(c.safety_mode || '—')) + '</b></span></div>') +
        card('Privacy defaults', 'deployment configuration, read from the running process',
          kv([
            ['Anonymous by default', (p.anonymous_default ? 'on' : 'off') +
              ' — replaces every handle with a salted per-session pseudonym'],
            ['PII scrubbing', (p.scrub_default ? 'on' : 'off') + ' — runs before the parser, always'],
            ['Retention window', f(p.retention_hours, 1) + ' sim-hours'],
            ['Records held', iN(audit.total) + ' (' + iN(audit.kept) + ' inside the window, ' +
              iN(audit.dropped) + ' due to be swept, ' + iN(audit.undated) + ' undated)'],
            ['Oldest record', isNum(audit.oldest_age_min) ? f(audit.oldest_age_min, 0) + ' sim-min old' : 'none dated']
          ]) +
          '<div class="fl-hr"></div>' +
          '<label class="fl-check"><input type="checkbox" id="fl-st-anon"' + (prefs.anonymous ? ' checked' : '') +
          '> send the complaints I inject from this dashboard anonymously</label>' +
          '<div class="fl-note">That switch is real: it sets <span class="fl-mono">anonymous: true</span> on ' +
          'POST /api/complaint, which scrubs the handle before anything is stored. The two deployment ' +
          'defaults above are process configuration and this API exposes no endpoint to change them at ' +
          'runtime — they are shown, not faked.</div>') +
        card('Download my data', 'GET /api/export — the messages AND the constraints they caused',
          '<div class="fl-row"><input class="fl-in" id="fl-st-author" style="width:180px" ' +
          'placeholder="handle (blank = everyone)"><label class="fl-check"><input type="checkbox" ' +
          'id="fl-st-scrub"> re-scrub on the way out</label>' +
          '<button class="fl-btn fl-btn-p" data-act="export">export</button>' +
          '<button class="fl-btn" data-act="download" id="fl-st-dl" disabled>download .json</button></div>' +
          '<div id="fl-st-export"></div>');
      exportBox = root.querySelector('#fl-st-export');
      paintExport();
    }

    function paintExport() {
      if (!exportBox) return;
      if (!exported) {
        exportBox.innerHTML = '<div class="fl-note">Nothing exported yet. “We deleted your message but ' +
          'kept the setpoint it caused” is not deletion, so the export carries both halves — and the ' +
          'forget button below expires the constraints too.</div>';
        return;
      }
      var recs = exported.records || [];
      exportBox.innerHTML =
        '<div class="fl-sub fl-num" style="margin-top:6px">' + esc(exported.schema) + ' · subject ' +
        esc(exported.subject) + ' · ' + iN(exported.counts.records) + ' record(s), ' +
        iN(exported.counts.constraints) + ' constraint(s)</div>' +
        (recs.length
          ? '<div class="fl-scroll"><table class="fl-table"><tr><th>Record</th><th>Author</th><th>Said</th>' +
            '<th>Parser</th><th></th></tr>' + recs.map(function (r) {
              return '<tr><td class="fl-mono">' + esc(r.id) + '</td><td>' + esc(r.author) + '</td>' +
                '<td>' + esc(r.text) + '</td>' +
                '<td class="fl-sub">' + esc(r.source) + (r.external_ai ? ' ' + badge('EXTERNAL', 'warn') : '') + '</td>' +
                '<td><button class="fl-btn fl-btn-danger" data-forget="' + esc(r.id) + '">forget this</button></td></tr>';
            }).join('') + '</table></div>'
          : '<div class="fl-note">No records for that subject.</div>') +
        '<ul class="fl-ev">' + (exported.notes || []).map(function (n) {
          return '<li>' + esc(n) + '</li>';
        }).join('') + '</ul>';
    }

    function doExport() {
      var a = (root.querySelector('#fl-st-author').value || '').trim();
      var scrub = root.querySelector('#fl-st-scrub').checked;
      var q = '/api/export?scrub=' + (scrub ? 'true' : 'false') + (a ? '&author=' + encodeURIComponent(a) : '');
      GET(q).then(function (r) {
        clearErr(errBox);
        exported = r;
        root.querySelector('#fl-st-dl').disabled = false;
        paintExport();
      }).catch(function (e) { showErr(errBox, e); });
    }

    function download() {
      if (!exported) return;
      var blob = new Blob([JSON.stringify(exported, null, 2)], { type: 'application/json' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'feelslike-export.json';
      root.appendChild(a);                    // inside our own panel, then removed
      a.click();
      root.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML = '<div class="fl-err" hidden id="fl-st-err"></div><div id="fl-st-box"></div>';
        box = root.querySelector('#fl-st-box');
        errBox = root.querySelector('#fl-st-err');
        gate = visibilityGate(root);
        render();

        root.addEventListener('change', function (e) {
          if (e.target.id === 'fl-st-anon') prefs.anonymous = !!e.target.checked;
        });
        root.addEventListener('click', function (e) {
          var b = e.target.closest ? e.target.closest('[data-act],[data-forget]') : null;
          if (!b) return;
          var act = b.getAttribute('data-act');
          if (act === 'mode') {
            POST('/api/controller', { safety_mode: root.querySelector('#fl-st-mode').value })
              .then(function (c) { clearErr(errBox); lastController = c; render(); })
              .catch(function (err) { showErr(errBox, err); });
          } else if (act === 'export') {
            doExport();
          } else if (act === 'download') {
            download();
          } else if (b.hasAttribute('data-forget')) {
            b.disabled = true;
            POST('/api/redact', { entry_id: b.getAttribute('data-forget') })
              .then(function () { clearErr(errBox); doExport(); })
              .catch(function (err) { showErr(errBox, err); b.disabled = false; });
          }
        });
      },
      update: function (state) {
        var g = gate();
        if (!g.on || !state) return;
        // Repaint only when something the panel shows actually changed: this
        // card owns text inputs and checkboxes, and rewriting it every second
        // would fight the operator's cursor.
        var pv = JSON.stringify(state.privacy || {});
        var cv = JSON.stringify({ m: (state.controller || {}).requested_safety_mode,
                                  e: (state.controller || {}).safety_mode,
                                  n: Object.keys((state.controller || {}).safety_modes || {}) });
        if (pv !== pKey || cv !== cKey) {
          pKey = pv; cKey = cv;
          lastPrivacy = state.privacy;
          lastController = state.controller;
          render();
        }
      }
    };
  }

  // =========================================================================
  // 11. demo — the presenter's remote control
  // =========================================================================

  function demoPanel() {
    var root, box, errBox, gate, tick, pos = null, busy = false;

    /** Escaped, depth-capped renderer for a step's real payload. */
    function value(v, depth) {
      if (v === null || v === undefined) return '<span class="fl-dim">—</span>';
      if (typeof v === 'number') return '<span class="fl-num">' + esc(f(v, 2)) + '</span>';
      if (typeof v === 'boolean') return esc(v ? 'yes' : 'no');
      if (typeof v === 'string') return esc(v);
      if (depth >= 3) return '<span class="fl-dim">…</span>';
      if (Array.isArray(v)) {
        if (!v.length) return '<span class="fl-dim">none</span>';
        return '<ul class="fl-ev">' + v.slice(0, 12).map(function (x) {
          return '<li>' + value(x, depth + 1) + '</li>';
        }).join('') + (v.length > 12 ? '<li class="fl-dim">…' + (v.length - 12) + ' more</li>' : '') + '</ul>';
      }
      var ks = Object.keys(v).slice(0, 14);
      return '<dl class="fl-kv">' + ks.map(function (k) {
        return '<dt>' + esc(words(k)) + '</dt><dd>' + value(v[k], depth + 1) + '</dd>';
      }).join('') + '</dl>';
    }

    function render() {
      if (!pos) { paint(box, '<div class="fl-note">loading the script…</div>'); return; }
      var step = pos.step || {};
      var hint = pos.state_hint || {};
      var res = pos.result || {};
      var done = pos.done || [];
      paint(box,
        card('Guided demo', 'nine steps, every one a real action on the live building',
          '<div class="fl-row">' +
          '<button class="fl-btn fl-btn-p" data-demo="start">START</button>' +
          '<button class="fl-btn" data-demo="prev">◀ PREVIOUS</button>' +
          '<button class="fl-btn" data-demo="next">NEXT ▶</button>' +
          '<button class="fl-btn fl-btn-danger" data-demo="reset">RESET SCRIPT</button>' +
          '<span class="fl-note fl-num">step ' + (pos.index + 1) + ' of ' + pos.total +
          ' · clock ' + esc(res.clock || '—') + '</span></div>' +
          '<div class="fl-note">PREVIOUS performs nothing — walking back can never undo or repeat a real ' +
          'action. RESET SCRIPT forgets the cursor and puts the condition knobs back to nominal; it does ' +
          'not rewind the clock (the Twin tab’s RESET SIMULATION is that button).</div>') +
        card('Step ' + (pos.index + 1) + ' · ' + (step.title || ''),
          step.mutates ? 'this step changes the building' : 'read-only step',
          '<div class="fl-narr">' + esc(pos.narration || '') + '</div>' +
          '<div class="fl-row" style="margin-top:8px">' +
          badge(pos.applied ? 'ACTION PERFORMED' : (step.mutates ? 'NOT YET PERFORMED' : 'NOTHING TO PERFORM'),
            pos.applied ? 'good' : 'muted') +
          (hint.panel ? chip('watch the ' + hint.panel + ' tab') : '') +
          ((hint.zones || []).length ? chip(hint.zones.join(', ')) : '') + '</div>' +
          (hint.watch ? '<div class="fl-sub" style="margin-top:6px"><span class="fl-lbl">Watch</span> ' +
            esc(hint.watch) + '</div>' : '')) +
        '<div class="fl-two">' +
        card('What this step did', 'returned by the real action, cached so it is not re-run',
          res.applied ? value(res.applied, 0) :
            '<div class="fl-note">Nothing performed yet for this step.</div>') +
        card('What the building says now', 're-read from live state on every poll',
          res.live ? value(res.live, 0) : '<div class="fl-note">—</div>') +
        '</div>' +
        card('The script', 'progress',
          '<ol class="fl-steps">' + (pos.steps || []).map(function (s, i) {
            return '<li class="' + (i === pos.index ? 'now' : '') + (done.indexOf(s.id) >= 0 ? ' done' : '') + '">' +
              '<b>' + esc(s.title) + '</b>' +
              (s.mutates ? ' ' + badge('ACTS', 'us') : ' ' + badge('READS', 'muted')) + '</li>';
          }).join('') + '</ol>'));
    }

    function send(action) {
      if (busy) return;
      busy = true;
      root.querySelectorAll('[data-demo]').forEach(function (b) { b.disabled = true; });
      POST('/api/demo', { action: action }).then(function (r) {
        clearErr(errBox);
        pos = r;
        render();
      }).catch(function (e) { showErr(errBox, e); })
        .then(function () {
          busy = false;
          root.querySelectorAll('[data-demo]').forEach(function (b) { b.disabled = false; });
        });
    }

    return {
      mount: function (el) {
        root = el;
        root.classList.add('fl-panel', 'fl-rel');
        root.innerHTML = '<div class="fl-err" hidden id="fl-dm-err"></div><div id="fl-dm-box"></div>';
        box = root.querySelector('#fl-dm-box');
        errBox = root.querySelector('#fl-dm-err');
        gate = visibilityGate(root);
        tick = poller(function () {
          if (busy) return null;
          return GET('/api/demo').then(function (r) { clearErr(errBox); pos = r; render(); })
            .catch(function (e) { showErr(errBox, e); });
        }, 2000);
        render();
        root.addEventListener('click', function (e) {
          var b = e.target.closest ? e.target.closest('[data-demo]') : null;
          if (b) send(b.getAttribute('data-demo'));
        });
      },
      update: function () {
        var g = gate();
        if (!g.on) return;
        tick(g.entered);
      }
    };
  }

  // =========================================================================
  // 12. styles — one block, every class fl-prefixed, tokens only
  // =========================================================================

  function injectCSS() {
    if (document.getElementById('fl-panels-css')) return;
    var css = [
      '.fl-panel{display:flex;flex-direction:column;gap:12px;}',
      '.fl-rel{position:relative;}',
      '.fl-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px;}',
      '.fl-card h3{font-size:13px;color:var(--ink2);font-weight:600;margin-bottom:10px;display:flex;',
      'align-items:center;gap:8px;}',
      '.fl-card h3 .fl-right{margin-left:auto;font-weight:400;color:var(--muted);font-size:12px;}',
      '.fl-lbl{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}',
      '.fl-num{font-variant-numeric:tabular-nums;}',
      '.fl-sub{font-size:12.5px;color:var(--ink2);}',
      '.fl-note{font-size:11.5px;color:var(--muted);}',
      '.fl-dim{color:var(--muted);}',
      '.fl-warn{color:color-mix(in srgb,var(--warn) 62%,var(--ink));}',
      '.fl-pend{color:var(--us);}',
      '.fl-nowrap{white-space:nowrap;}',
      '.fl-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px;}',
      '.fl-hr{height:1px;background:var(--grid);margin:9px 0;}',
      '.fl-scroll{overflow-x:auto;}',
      '.fl-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}',
      '.fl-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;}',
      '.fl-two{display:grid;grid-template-columns:minmax(280px,0.85fr) minmax(320px,1.15fr);gap:12px;',
      'align-items:start;}',
      '@media (max-width:980px){.fl-two{grid-template-columns:1fr;}}',
      '.fl-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:9px;}',
      '.fl-tile{border:1px solid var(--border);border-radius:10px;padding:9px 11px;background:var(--surface);}',
      '.fl-tile .v{font-size:20px;font-weight:650;letter-spacing:-0.02em;font-variant-numeric:tabular-nums;',
      'margin-top:2px;}',
      '.fl-err{font-size:12.5px;color:var(--crit);border:1px solid var(--crit);border-radius:9px;',
      'padding:7px 10px;background:var(--surface);}',
      '.fl-warnbox{font-size:12.5px;color:color-mix(in srgb,var(--warn) 62%,var(--ink));',
      'border:1px solid color-mix(in srgb,var(--warn) 70%,transparent);border-radius:9px;padding:8px 10px;',
      'background:color-mix(in srgb,var(--warn) 10%,transparent);}',
      '.fl-goodbox{font-size:12.5px;color:var(--good);border:1px solid color-mix(in srgb,var(--good) 45%,transparent);',
      'border-radius:9px;padding:8px 10px;background:color-mix(in srgb,var(--good) 7%,transparent);}',
      '.fl-btn{border:1px solid var(--border);background:var(--surface);color:var(--ink2);border-radius:8px;',
      'padding:5px 11px;font:inherit;font-size:12.5px;cursor:pointer;}',
      '.fl-btn:hover:not(:disabled){border-color:var(--ink2);color:var(--ink);}',
      '.fl-btn.on{color:var(--ink);border-color:var(--ink2);font-weight:600;}',
      '.fl-btn:disabled{opacity:0.5;cursor:default;}',
      '.fl-btn-p{background:var(--us);border-color:var(--us);color:var(--surface);font-weight:600;}',
      '.fl-btn-p:hover:not(:disabled){color:var(--surface);}',
      '.fl-btn-danger{color:var(--crit);border-color:color-mix(in srgb,var(--crit) 55%,transparent);}',
      '.fl-in{border:1px solid var(--border);border-radius:8px;padding:6px 9px;background:var(--page);',
      'color:var(--ink);font:inherit;font-size:13px;}',
      '.fl-check{font-size:12px;color:var(--ink2);display:inline-flex;gap:5px;align-items:center;}',
      '.fl-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;}',
      '.fl-table th{text-align:left;color:var(--muted);font-size:11px;font-weight:600;',
      'border-bottom:1px solid var(--grid);padding:4px 6px;white-space:nowrap;}',
      '.fl-table td{padding:5px 6px;border-bottom:1px solid var(--grid);font-size:12.5px;vertical-align:top;}',
      '.fl-table tr.fl-lead td{background:color-mix(in srgb,var(--us) 6%,transparent);}',
      '.fl-badge{display:inline-block;border:1px solid var(--border);border-radius:6px;padding:1px 7px;',
      'font-size:10.5px;font-weight:600;color:var(--ink2);margin-right:4px;}',
      '.fl-b-good{color:var(--good);border-color:color-mix(in srgb,var(--good) 40%,transparent);}',
      '.fl-b-us{color:var(--us);border-color:color-mix(in srgb,var(--us) 40%,transparent);}',
      '.fl-b-warn{color:color-mix(in srgb,var(--warn) 62%,var(--ink));',
      'background:color-mix(in srgb,var(--warn) 16%,transparent);',
      'border-color:color-mix(in srgb,var(--warn) 60%,transparent);}',
      '.fl-b-crit{color:var(--crit);border-color:color-mix(in srgb,var(--crit) 45%,transparent);}',
      '.fl-b-muted{color:var(--muted);}',
      '.fl-chip{display:inline-block;border:1px solid var(--border);border-radius:99px;padding:1px 9px;',
      'font-size:11.5px;color:var(--ink2);background:var(--page);margin-right:4px;}',
      '.fl-kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:12.5px;align-items:baseline;}',
      '.fl-kv dt{color:var(--muted);}',
      '.fl-kv dd{font-variant-numeric:tabular-nums;min-width:0;}',
      '.fl-tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);',
      'border-radius:8px;padding:7px 10px;font-size:12px;font-variant-numeric:tabular-nums;z-index:6;',
      'max-width:280px;box-shadow:0 4px 14px color-mix(in srgb,var(--ink) 14%,transparent);}',
      '.fl-empty{text-align:center;color:var(--muted);font-size:12.5px;padding:24px 16px;',
      'border:1px dashed var(--grid);border-radius:12px;}',
      '.fl-empty b{display:block;color:var(--ink2);font-size:14px;margin-bottom:5px;}',
      '.fl-chart{display:block;overflow:visible;}',
      '.fl-chart text{font-family:inherit;}',
      /* sliders */
      '.fl-sliders{display:flex;flex-direction:column;gap:9px;}',
      '.fl-slider{display:grid;grid-template-columns:minmax(150px,1.1fr) minmax(120px,1.4fr) 150px;',
      'gap:12px;align-items:center;}',
      '@media (max-width:720px){.fl-slider{grid-template-columns:1fr;}}',
      '.fl-slider-l{font-size:13px;font-weight:600;}',
      '.fl-slider-v{text-align:right;}',
      '.fl-slider-v b{font-size:15px;}',
      '.fl-range{width:100%;accent-color:var(--us);}',
      '.fl-confirm{border:1px solid color-mix(in srgb,var(--crit) 45%,transparent);border-radius:9px;',
      'padding:9px 11px;}',
      /* feed */
      '.fl-feed{display:flex;flex-direction:column;gap:8px;max-height:560px;overflow-y:auto;}',
      '.fl-msg{border:1px solid var(--border);border-radius:10px;padding:8px 10px;}',
      '.fl-msg-conflict{border-color:color-mix(in srgb,var(--warn) 70%,transparent);',
      'background:color-mix(in srgb,var(--warn) 8%,transparent);}',
      '.fl-msg-who{font-size:11px;color:var(--muted);margin-bottom:2px;}',
      '.fl-msg-txt{font-size:13.5px;}',
      '.fl-msg-parsed{margin-top:5px;font-size:11.5px;color:var(--ink2);font-variant-numeric:tabular-nums;}',
      '.fl-msg-zones{margin-top:4px;}',
      '.fl-msg-expl{margin-top:4px;font-size:12px;color:var(--ink2);font-style:italic;}',
      /* options */
      '.fl-opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px;}',
      '.fl-opt{text-align:left;border:1px solid var(--border);background:var(--surface);border-radius:10px;',
      'padding:9px 11px;cursor:pointer;font:inherit;color:var(--ink2);}',
      '.fl-opt:hover{border-color:var(--ink2);}',
      '.fl-opt.on{border-color:var(--us);box-shadow:inset 0 0 0 1px var(--us);color:var(--ink);}',
      '.fl-opt.eff{border-color:color-mix(in srgb,var(--us) 55%,transparent);}',
      '.fl-opt-h{font-size:13px;font-weight:600;color:var(--ink);margin-bottom:4px;text-transform:capitalize;}',
      '.fl-wbar{display:flex;height:7px;border-radius:99px;overflow:hidden;background:var(--page);',
      'border:1px solid var(--grid);margin:3px 0;}',
      '.fl-wbar i.c{background:var(--us);}',
      '.fl-wbar i.e{background:var(--base);}',
      '.fl-lock{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:4px 0;',
      'border-bottom:1px solid var(--grid);font-size:13px;}',
      /* explain */
      '.fl-list{display:flex;flex-direction:column;gap:6px;max-height:520px;overflow-y:auto;}',
      '.fl-item{border:1px solid var(--border);border-radius:9px;padding:7px 9px;cursor:pointer;',
      'background:var(--surface);}',
      '.fl-item:hover{border-color:var(--ink2);}',
      '.fl-item.on{border-color:var(--us);box-shadow:inset 3px 0 0 var(--us);}',
      '.fl-item-h{display:flex;justify-content:space-between;gap:8px;font-size:13px;}',
      '.fl-why-h{font-size:15px;font-weight:650;letter-spacing:-0.01em;margin:2px 0 4px;}',
      '.fl-why-move{font-size:22px;font-weight:650;letter-spacing:-0.02em;margin-bottom:6px;}',
      '.fl-arrow{color:var(--us);padding:0 8px;}',
      '.fl-headline{font-size:15px;font-weight:600;letter-spacing:-0.01em;}',
      '.fl-dbar{display:inline-block;position:relative;width:96px;height:9px;background:var(--page);',
      'border:1px solid var(--grid);border-radius:3px;vertical-align:middle;}',
      '.fl-dbar i{position:absolute;top:0;bottom:0;border-radius:2px;}',
      /* bars */
      '.fl-hbars{display:flex;flex-direction:column;gap:3px;margin-top:4px;}',
      '.fl-hbar{display:grid;grid-template-columns:minmax(90px,1fr) minmax(60px,2fr) auto;gap:8px;',
      'align-items:center;font-size:12px;}',
      '.fl-hbar-t{height:9px;background:var(--page);border:1px solid var(--grid);border-radius:99px;',
      'overflow:hidden;}',
      '.fl-hbar-t i{display:block;height:100%;background:var(--us);opacity:0.85;}',
      '.fl-hbar-v{font-variant-numeric:tabular-nums;color:var(--ink2);text-align:right;}',
      /* maintenance */
      '.fl-alert{border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin-bottom:8px;}',
      '.fl-alert.resolved{opacity:0.72;}',
      '.fl-alert-h{display:flex;align-items:center;gap:6px;font-size:13.5px;}',
      '.fl-ev{margin:6px 0 0 16px;font-size:12.5px;color:var(--ink2);}',
      '.fl-ev li{margin-bottom:2px;}',
      '.fl-rec{margin-top:6px;font-size:12.5px;}',
      /* demo */
      '.fl-narr{font-size:14px;line-height:1.5;}',
      '.fl-steps{margin:0 0 0 18px;font-size:12.5px;color:var(--ink2);}',
      '.fl-steps li{padding:2px 0;}',
      '.fl-steps li.now{color:var(--ink);font-weight:600;}',
      '.fl-steps li.done{color:var(--good);}'
    ].join('');
    var st = document.createElement('style');
    st.id = 'fl-panels-css';
    st.textContent = css;
    document.head.appendChild(st);
  }

  // =========================================================================
  // 13. registration
  // =========================================================================

  var PANELS = [
    ['twin', twinPanel],
    ['complaints', complaintsPanel],
    ['control', controlPanel],
    ['explain', explainPanel],
    ['whatif', whatifPanel],
    ['analytics', analyticsPanel],
    ['maintenance', maintenancePanel],
    ['experiments', experimentsPanel],
    ['settings', settingsPanel],
    ['demo', demoPanel]
  ];

  if (!boot()) {
    // The shell may publish window.FL after its own first fetch, so try again
    // on DOMContentLoaded and then for ~3 s before giving up loudly and quietly.
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { if (!boot()) bootLater(); });
    } else {
      bootLater();
    }
  }
})();
