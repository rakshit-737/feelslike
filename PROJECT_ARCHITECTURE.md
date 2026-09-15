# FeelsLike — Project Architecture

**Status: describes the code as it exists today** (2026-08-17, branch `main`, commit `7e84321`
plus in-flight Phase A work). Every function name, constant and line reference below was read
out of the repo, not imagined. Proposed work is quarantined in §9 and clearly labelled as
*not built*.

> **Line-reference policy.** `file.py:NN` anchors are given only for files that are stable
> as of writing (`backend/app.py`, `backend/constraints.py`, `backend/parser.py`,
> `backend/memory.py`, `sim/controllers.py`, `sim/env.py`, `dashboard/index.html`).
> `sim/twin.py`, `sim/weather.py` and `sim/humidity.py` are actively being extended in
> Phase A, so they are referenced **by function name only** — names are stable, line numbers
> are not.

Companion documents: `DATA_CONTRACTS.md` (field-level types), `IMPLEMENTATION_PLAN.md`
(phasing and ownership), `IMPLEMENTATION.md` (the original hackathon build plan).

---

## 1. What the system is

A 5-zone office building is simulated by an RC thermal twin. Occupants complain in plain
language. A parser turns each complaint into a typed constraint with a decaying weight.
A constraint-aware controller folds those constraints into its setpoint schedule. A second,
identical twin runs the dumb static schedule on the same weather — that A/B pair is the
demo's entire evidentiary basis.

There is **no database, no frontend framework, no build step, no message queue**. One
FastAPI process holds all state in memory; one static HTML file polls it.

---

## 2. Module map

| Module | Lines | Responsibility | Imports (internal) |
|---|---|---|---|
| `sim/weather.py` | 69 | Deterministic synthetic outdoor temp (+ `offset`) + solar factor; optional Open-Meteo fetch | — |
| `sim/humidity.py` | 142 | Psychrometrics: Magnus `sat_pressure`/`dew_point`, `humidity_ratio`, `rh_from_ratio`, seeded `outdoor_rh` | — |
| `sim/twin.py` | 426 | 5-zone lumped RC thermal model, HVAC energy, comfort accounting, humidity state, `clone()` | `sim.weather`, `sim.humidity` |
| `sim/controllers.py` | 90 | `StaticSchedule`, `ReactiveComfort`, `ConstraintAware`, `RLPolicy` | `sim.twin` (+ `sim.env` lazily) |
| `sim/env.py` | 95 | Gymnasium env, `build_obs` (18-dim), `apply_action`, reward | `sim.twin`, `sim.controllers`, `backend.constraints` |
| `backend/contracts.py` | **new** | Canonical shared types; stdlib only, zero internal imports | — |
| `backend/prompts.py` | 42 | LLM system prompt template built from the zone list | — |
| `backend/parser.py` | 176 | Complaint → `ParsedComplaint`; LLM with deterministic rules fallback | `backend.prompts`, `sim.twin` |
| `backend/constraints.py` | 120 | `Constraint`, `ConstraintStore`: decay, arbitration, explanation | — |
| `backend/memory.py` | 84 | Comfort memory: mines recurring (zone, issue, hour) patterns, pre-applies | `backend.constraints` |
| `backend/app.py` | 215 | `LiveSim` thread + FastAPI routes + Slack/Teams webhook + static dashboard | all of the above |
| `backend/building.py` | **new (2026-09-15)** | Commercial building profiles (6 types), config validation, floors → zones topology, demand model, executive KPIs, zone "why" — pure projection, writes nothing | `backend.telemetry`, `sim.twin` |
| `backend/security/*` | **new (Phase 6)** | config (modes, validation, fail-closed start), passwords (scrypt), auth (roles, permissions, users, opaque sessions), audit (redacted), ratelimit, devices (identity registry, HMAC keys, rotation, quarantine), ingest (SecureIngest in front of LatestStore), protocols (simulated MQTT broker + ACL, MQTTS config, fieldbus gateway boundary), web (POLICY table, enforce dependency, security middleware) — `docs/SECURITY.md` | stdlib only; `backend.latest` via SecureIngest |
| `dashboard/security.js` | **new (Phase 6)** | Security tab: security metrics vs data quality, system health, protocols, device security, telemetry rejections, audit events | — |
| `backend/scenario.py`, `backend/loads_bridge.py` | **new (Phase 5)** | Seasonal / environmental scenarios for the live twin: Phase-2 WeatherModel + override layer through the twin's weather/RH/solar hooks, envelope scale, profile internal loads, causal explanation, season comparison on clones — `docs/SEASONAL_SIMULATION.md` | `backend.dataset.weather`, `backend.dataset.loads`, `backend.comfort`, `sim.twin` |
| `backend/simhw.py` | **new (Phase 5)** | SIMULATED hardware (not real): 20-device registry, simulated MQTT/Modbus/BACnet/HTTPS adapters, noise/bias/drift/faults, publishes via `LatestStore.ingest` — `docs/HARDWARE_SIMULATION.md` | `backend.latest` |
| `dashboard/scenario.js` | **new (Phase 5)** | Scenario tab: environment controls, live weather, building response + causal sentence, zones, season comparison, hardware simulation panel | — |
| `backend/latest.py`, `backend/telemetry_publish.py` | **new (Phase 4)** | Latest-value store (validate → store → freshness on read, 15-min recent buffer, sensor + system health) and its publishers (twin, comfort, hardware rig, ambient node) / readers (`ComfortReading`, `/api/latest` blocks). See §5a | `backend.comfort` |
| `backend/comfort.py`, `backend/comfort_events.py`, `backend/comfort_whatif.py` | **new (Phase 3)** | Comfort engine (pure, reads `ComfortReading`), event/duration tracker (ticked by `LiveSim` under the lock via `_safe`), comfort-vs-energy clone runs — `docs/COMFORT.md` | `backend.telemetry`, `backend.whatif`, `sim.twin` |
| `dashboard/comfort.js` | **new (Phase 3)** | Comfort tab (facility manager) | — |
| `backend/dataset/*` | **new (Phase 2)** | Historical dataset pipeline: config, calendar, weather, occupancy, loads, hvac, comfort, anomalies, quality, generator (drives `sim/twin.py`), SQLite store, analysis — `docs/DATASET.md` | `sim.twin`, `sim.humidity`, `backend.building`, `backend.telemetry` |
| `dashboard/history.js` | **new (Phase 2)** | History tab: historical charts, building/floor/zone + range filters, comparisons, anomalies, raw vs clean, export | — |
| `dashboard/building.js` | **new (2026-09-15)** | Building tab (landing). Binds to `window.FL`; charts via `window.FLChart` from `monitor.js` | — |
| `dashboard/index.html` | 424 | Single-file UI: floor plan SVG, energy chart, complaint console. Polls `/api/state` at 1 Hz | — |
| `scripts/demo_day.py` | 62 | 7-day controller comparison → `evals/results_energy.json` | `sim.*` |
| `evals/run_nlp_eval.py` | 80 | Scores the parser against `evals/benchmark.json` (dev / held-out splits) | `backend.parser` |
| `rl/train.py`, `rl/evaluate.py` | 43 / 41 | PPO training and the ablation table | `sim.env`, `sim.controllers`, `scripts.demo_day` |
| `scripts/build_report.py` | 744 | Generates the technical report PDF | reads eval JSON |

---

## 3. Data flow (as built)

```
 OCCUPANT                     BACKEND PROCESS (one python, all state in RAM)
 ─────────                    ───────────────────────────────────────────────────────

 dashboard chat ──POST /api/complaint──┐
 Slack slash cmd ─POST /api/slack──────┤
                                       ▼
                          app.handle_complaint(text, author)      app.py:130
                                       │
                    ┌──────────────────┴───────────────────┐
                    ▼                                      │  (no lock held:
        parser.parse(text)          parser.py:163          │   network call can
        ├─ ANTHROPIC_API_KEY? _call_anthropic  :122        │   take up to 15 s)
        ├─ OPENAI_API_KEY?    _call_openai     :139        │
        └─ any failure ──────► rules_parse     :92         │
                    │                                      │
                    ▼  (ParsedComplaint, source, latency_ms)
        ┌───────────┴────────────────────────────────┐
        │ retraction?  detect_retraction  parser.py:74│
        │   yes → store.clear_zone(zone)  constraints.py:67   ── "all-clear"
        │ not a comfort complaint?        → feed only ── "ignored"
        │ zone_id is None?                → feed only ── "clarify"
        │ else ▼                                       │
        └───────┬──────────────────────────────────────┘
                ▼   with sim.lock:
      Constraint.from_issue(...)          constraints.py:40
      store.add(c) ──► store.explain()    constraints.py:59 / :95
                │                                │
                │                                └──► explanation dict → feed entry
                ▼
      ConstraintStore.items  (in-memory list, never persisted)
                │
                │  read every simulated minute
                ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ LiveSim._loop   app.py:45   daemon thread, sleeps 0.2 s real             │
 │                                                                          │
 │   acc += speed * 0.2                 speed = sim-seconds per real-second │
 │   while acc >= 60:                                                       │
 │     sps, vents = ctrl_us.act(us, store)     controllers.py:53            │
 │        └─ store.zone_adjustments(t)         constraints.py:78            │
 │     us.step(sps, vents)                     twin.step()  ── FeelsLike    │
 │     bs, bv  = ctrl_base.act(base)           controllers.py:18            │
 │     base.step(bs, bv)                       twin.step()  ── baseline     │
 │     memory.tick(us, store) → feed notes     memory.py:56                 │
 │     every 900 sim-s: history.append({t, us.kwh, base.kwh})               │
 └──────────────────────────────────────────────────────────────────────────┘
                │
                ▼  GET /api/state (1 Hz poll)      app.py:75 LiveSim.state()
      {sim, zones[], meters{us, base, saved_*}, history[], feed[]}
                │
                ▼
      dashboard/index.html  tick()  :382 → drawFloor :243 · drawChart :273 · drawFeed :313
```

### The RC physics, one line

`DigitalTwin.step()` integrates, per zone, explicit Euler at `DT = 60 s`:

```
T_free = T + dt/C * ( UA·(T_out−T) + VENT_UA·vent·(T_out−T)
                      + solar_peak·solar_factor(orientation,h) + 100·occupants
                      + Σ_j G_ij·(T_j − T) )
q_cool = min( C·(T_free − setpoint)/dt , max_cool )       # perfect thermostat, finite capacity
T_new  = T_free − dt·q_cool/C
P_zone = q_cool/COP + FAN_W[vent]                          # COP 3.4, fan 0/150/420 W
```

Neighbour temperatures `T_j` are read from `self.T`, the **previous** step's dict, and all
zones are written into a fresh `newT` before the swap (`self.T = newT` at the end of `step`)
— so the update is simultaneous (Jacobi), not sequential (Gauss–Seidel). The order of
`ZONES` cannot change results.

Comfort accounting only runs when `occ > 0`: degree-minutes above
`BAND[1] = 26.5 °C` go to `hot_deg_min`, below `BAND[0] = 23.0 °C` to `cold_deg_min`, and
either increments `viol_min`. **An empty zone can never be uncomfortable** — that single
line is why `ConstraintAware` reaches 0 viol-min while the static schedule racks up 16,328.

---

## 4. The A/B lock-step twin design

This is the mechanism the pitch rests on, so it is worth stating precisely.

`LiveSim.__init__` (app.py:29) builds **two** `DigitalTwin` instances with the *same seed*
(default 7) and sets both clocks to the same `start_hour`:

```python
self.us   = DigitalTwin(seed=seed)      # driven by ConstraintAware
self.base = DigitalTwin(seed=seed)      # driven by StaticSchedule
self.us.t = self.base.t = start_hour * 3600.0
```

Both twins therefore call `weather_fn(t)` = `outdoor_temp(t, seed)` with identical
arguments at identical times. `sim/weather.py` is pure and seeded (`_day_offset` is a
deterministic `sin`-hash), so the two buildings experience *bit-identical* weather.
Occupancy is a pure function of `(profile, day, hour)` (`twin.occupancy`), so the two
buildings also hold identical people. **The only difference between them is the controller.**

They advance inside the same `while acc >= 60` loop body (app.py:51-59), so they can never
drift by even one step, no matter how the wall clock jitters. Energy comparison is then
just `base.kwh − us.kwh`, computed at app.py:92.

`scripts/demo_day.py` does the offline version of the same trick: a fresh
`DigitalTwin(seed=seed)` per controller, same seed, same step count (demo_day.py:17).

Consequence for anyone adding features: **any new stochastic input must be derived from
`twin.seed` via a `random.Random(seed)` instance**, never from module-level `random`, or
the two twins diverge and the headline number becomes a lie.

---

## 5. Where state lives

Everything is in-memory in a single process. There is no persistence layer of any kind.

| State | Owner | Lifetime | Bound |
|---|---|---|---|
| Module-global `sim = LiveSim()` | `backend/app.py:109` | Process | 1 instance, created at import |
| Zone temperatures `T`, clock `t` | `DigitalTwin` (×2) | Process | 5 floats each |
| Energy / comfort counters `kwh`, `kwh_by_zone`, `viol_min`, `hot_deg_min`, `cold_deg_min` | `DigitalTwin` | Process, monotonic | never reset |
| Complaint constraints | `ConstraintStore.items` | Process | **unbounded list** — see §8 |
| Comfort-memory dedup keys | `ComfortMemory._done` set | Process | grows with days × zones × issues |
| Energy history samples | `LiveSim.history` | Process | last 800 samples @ 15 sim-min ≈ 8.3 sim-days |
| Complaint feed | `LiveSim.feed` | Process | last 30 entries |
| Last setpoints / vents | `LiveSim.last_sps`, `.last_vents` | Process | overwritten each step |
| Trained PPO weights | `rl/models/ppo_feelslike.zip` | On disk | only file-backed state in the system |
| Benchmark + results | `evals/*.json` | On disk | regenerated by the eval scripts |

**Restarting the server resets the simulation to Monday 08:00 with zero energy.** That is
intentional for a demo, and it is also the reason no migration/versioning story exists.

### Concurrency model

One daemon thread (`LiveSim._loop`, started at app.py:43) mutates the twins. FastAPI route
handlers are plain `def`, so Starlette runs them in its threadpool — genuine parallelism
against the sim thread. `LiveSim.lock` (a `threading.Lock`) is held:

- around the entire step batch in `_loop` (app.py:49),
- around `store.clear_zone` and `store.add` in `handle_complaint` (app.py:140, 154),
- around the whole body of `state()` (app.py:76),
- around the speed write (app.py:213).

Deliberately **outside** the lock: `parser.parse()`. An LLM call has a 15 s timeout
(parser.py:134); holding the sim lock across it would freeze the building on stage. This is
the right call and must not be "tidied up".

---

## 5a. Latest-value telemetry (Phase 4)

```
 sim loop (60 s physics step, sim.lock held)
   twin step ─> telemetry.record ─> _tick_latest ──ingest(source=sim/derived/predicted)──┐
                                                                                          ▼
 POST /api/hw/reading ─> HardwareBridge (validates) ─> publish_hardware ─ingest(hardware)─> LatestStore
 POST /api/hw/sensor  ─> SensorNodeStore            ─> publish_ambient  ─ingest(hardware)─┘   (own lock)
                                                                                          │
   _tick_comfort: comfort_reading(store) ─> comfort.assess ─> ComfortTracker ─> publish_comfort (derived)
                                                                                          │
 GET /api/latest[/zone|/health|/sensors|/trend|/{metric}]  ◄── reads the store ──────────┘
   ▲
 dashboard/index.html: ONE setTimeout poll (server poll_interval_s, default 5 s; ?poll= or
 localStorage fl.pollS) -> FL.latest + 'fl:latest' event -> Building tab "Current conditions",
 header telemetry status. Historical endpoints (/api/history*) are untouched and not polled.
```

- **The digital twin is the telemetry source today** — every value it publishes is `sim`; this is
  not physical building telemetry. Hardware joins through the same `ingest()`.
- **Lock order:** `sim.lock → store lock` only. The store never calls out while locked and never
  takes `sim.lock`; readers (`/api/latest*`) need `sim.lock` only for the clock/config snapshot.
- **Comfort** (`/api/comfort*`, drill-down, occupant view) reads `ComfortReading`s from the store,
  so stale or invalid inputs show up as data-quality flags and stale comfort.
- **Reset** rebuilds the building and republishes its starting state immediately; CO₂ is
  "No current data available" until the first telemetry row exists.
- **Failure behaviour:** the shell marks the header OFFLINE after two failed polls and keeps the
  last values; missing values render "Unavailable" (never 0); invalid readings are stored as
  invalid with the rejected value; stale values stay visible as stale.
- **Security boundary (Phase 6 does the rest):** identifiers are pattern-checked, values
  validated, payload `source` labels ignored, no ingest endpoint accepts client-chosen sources.

## 5b. Security layer (Phase 6)

```
 Browser ─(HTTPS in production / HTTP in dev)─► CORS allowlist ─► SecurityMiddleware (request id, headers,
   HTTPS redirect, safe 500, generic POST audit) ─► route ─► enforce() [global dependency]: bearer session →
   Principal → POLICY[(method, route)] (fail closed) → permission → rate limit ─► endpoint: field-level +
   zone checks (require) ─► existing services ─► AuditLog

 Device ─► /api/telemetry/ingest | SimulatedMqttBroker (ACL) | simulated Modbus/BACnet ─► SecureIngest
   (registry → status → credential → HMAC → claims → timestamp → replay → metric) ─► LatestStore (trusted source)
 Simulated hardware (Phase 5) uses the same SecureIngest via SimHardware.sink — no parallel pipeline.
```

Default `FL_SECURITY_MODE=development` keeps every earlier behaviour (explicit development principal, plain
HTTP); `enforced` / `production` turn on authentication, device signatures and restrictive CORS, and
production refuses to start without TLS, explicit https origins and a users file.

## 6. External interfaces

| Interface | Direction | Contract | Failure behaviour |
|---|---|---|---|
| `GET /` | in | Serves `dashboard/index.html` via `FileResponse` | 500 if the file is missing |
| `GET /api/state` | in | The frozen state payload (§7) | — |
| `POST /api/complaint` | in | `{text, author}` JSON → action dict | Pydantic 422 on a missing `text` |
| `POST /api/slack` | in | Slack form-encoded *or* Teams JSON; replies `{response_type, text}` in < 3 s | Empty text → ephemeral help message |
| `POST /api/speed` | in | `{speed}` clamped to 1–3600 sim-s per real-s | Non-numeric → 500 (unguarded `float()`) |
| `GET /api/building` | in | Building tab snapshot: config, operating mode, topology, zone cards, demand, KPIs (`DATA_CONTRACTS.md` §10) | — |
| `GET/POST /api/building/profile` | in | Active profile + catalog / switch type, edit fields, operating mode (writes controller objective + safety mode) | 400 `{errors:[…]}` all-or-nothing; 422 unknown field |
| `GET /api/building/zones/{id}` | in | Zone drill-down: trends, decision + why, constraint, alerts, events | 404 zone, 400 window |
| `GET/POST /api/scenario`, `POST /api/scenario/reset`, `GET /api/scenario/compare` | in | Environment for the live twin (both twins), bounded; season comparison on clones (`DATA_CONTRACTS.md` §14) | 400 with every invalid field, nothing applied; 422 unknown field |
| `GET/POST /api/simhw`, `POST /api/simhw/devices/{id}/fault`, `POST /api/simhw/devices/{id}/config` | in | SIMULATED hardware registry, enable, faults, characteristics — not real devices | 404 id not in registry, 400 bounds/mode |
| `GET /api/latest`, `/api/latest/zone/{id}`, `/api/latest/health`, `/api/latest/sensors`, `/api/latest/trend`, `/api/latest/{metric}` | in | Current values with unit, source, quality, timestamp, age (`DATA_CONTRACTS.md` §13); polled once centrally | 400 bad zone/source/window, 404 unknown metric/zone; "No current data available" instead of zeros |
| `GET /api/history/catalog`, `/api/history`, `/api/history/compare`, `/api/history/anomalies`, `/api/history/quality`, `/api/history/export` | in | Generated historical dataset (SQLite) — filters building/floor/zone/time/interval; ≤ 1000 points; CSV/JSON export ≤ 50 000 rows (`DATA_CONTRACTS.md` §11) | 503 when no dataset file; 400 bad filters |
| `GET /api/building/demand` | in | Hourly actual (telemetry) vs expected (profile) | 400 bad range/zone |
| Anthropic Messages API | out | `claude-haiku-4-5` by default, 15 s timeout, strict-JSON system prompt | Any exception → rules parser, `source: "rules"` badge |
| OpenAI-compatible chat API | out | `LLM_BASE_URL` + `OPENAI_API_KEY`; Groq / Gemini / Ollama all work | same fallback |
| Open-Meteo | out | `fetch_openmeteo()` — **optional, never called by the running demo** | n/a |
| Slack / Teams | out | The webhook's own JSON reply body | n/a |

Environment (loaded from `.env` at parser import, parser.py:16): `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`.

---

## 7. The frozen `/api/state` payload

`dashboard/index.html` reads these keys by name. They may be **added to, never renamed or
removed**:

```
sim     { clock, hour, t_out, speed }
zones[] { id, name, temp, base_temp, setpoint, vent, occ, offset, active_constraints }
meters  { us{kwh,cost_rs,co2_kg,viol_min,hot_deg_min,cold_deg_min}, base{…},
          saved_kwh, saved_pct, saved_rs, saved_co2 }
history[] { t, us, base }
feed[]  { author, text, source, latency_ms, parsed{…}, action, sim_clock, explanation? }
```

Exact reader sites in the dashboard: `s.sim.clock` / `s.sim.t_out` / `s.sim.speed` at
index.html:386-388; `s.meters.*` at 391-404; `f.explanation.conflict` at 407 (drives the
pulsing conflict outline); `drawFloor` reads `temp, occ, setpoint, vent, offset,
active_constraints, name` at 243-265; `drawFeed` reads `author, sim_clock, latency_ms,
source, text, parsed.*, action, explanation.summary` at 313-339.

`backend/contracts.py:ZoneRuntime` deliberately keeps those nine zone field **names**
verbatim so it can be dropped into the payload as a strict superset.

---

## 8. Dependency graph and known sharp edges

```
                       backend/contracts.py   (leaf: stdlib only, imports nothing internal)
                                 ·
   sim/weather.py                                 backend/prompts.py
        │                                                │
        ▼                                                ▼
   sim/twin.py ───────────────────────────────────► backend/parser.py
        │  ▲                                             │
        ▼  │                                             │
 sim/controllers.py                backend/constraints.py│
        │  ▲  (lazy, inside RLPolicy.act)      │         │
        │  └───────────┐                       ▼         │
        ▼              │                 backend/memory.py
   sim/env.py ─────────┘                       │         │
        │                                      ▼         ▼
        │                              ┌──────────────────────┐
        └─────────────────────────────►│    backend/app.py    │
                                       └──────────────────────┘
 scripts/demo_day.py → sim.*        rl/train.py → sim.env
 evals/run_nlp_eval.py → backend.parser        rl/evaluate.py → sim.controllers, scripts.demo_day
```

**The one latent cycle:** `sim/env.py:17` imports `ConstraintAware` from
`sim.controllers` at module level, and `RLPolicy.act` imports `sim.env` at
controllers.py:86 — *inside the method*. Keep it inside the method. Hoisting that import
to module scope creates a hard circular import and breaks `sim.controllers` for everyone.

`backend.parser` importing `sim.twin` just to read `ZONES` is the only backend→sim
dependency in the complaint path; it makes the parser un-importable without the sim. Not
worth changing now, but it is why `backend/contracts.py` imports nothing at all.

### Real defects worth knowing before extending

1. **FIXED — feed / history race.** `state()` now builds the whole payload inside the
   lock and hands out copies only (`list(self.history)`, `[dict(e) for e in self.feed]`).
   Regression: `test_api.py::test_polling_state_while_the_sim_steps_never_tears_a_response`.
2. **`ConstraintStore.items` never shrinks.** `active()` filters by decay (constraints.py:63)
   but nothing prunes; `zone_adjustments` and `ComfortMemory.patterns` both walk the full
   list every simulated minute. At 960× over a long session this becomes O(n) work per
   step with n growing all session.
3. **FIXED (2026-08-30) — `clear_zone` back-dating.** Expiry is now the
   `Constraint.cleared_t` flag, honoured by `decay()`; `created_t` is never moved. The
   same fix landed in the second site, `privacy.redact_record()`. Regressions:
   `test_constraints.py` §9.
4. **`POST /api/speed` does not validate types.** `float(body.get("speed", 240))` raises on
   a non-numeric body → 500.
5. **`parse()` breaks out of the provider loop on the first failure** (parser.py:174), so if
   `ANTHROPIC_API_KEY` is set but invalid, a valid `OPENAI_API_KEY` is never tried. Silent
   downgrade to rules. Intentional-looking, but surprising.
6. **`DigitalTwin.metrics()` rounds** every value. Anything that differences two metrics dicts
   inherits ±0.005 kWh of noise. Read the raw attributes for maths, `metrics()` for display.

---

## 9. Proposed extensions and their risks

*Nothing in this section is built.* Each item names the pinned API from the shared contract
and the concrete risk it introduces.

| Extension | New surface | Principal risk | Mitigation |
|---|---|---|---|
| **Humidity** (`sim/humidity.py`, `twin.rh_now`, `dew_point_now`) | `outdoor_rh`, `humidity_ratio`, `rh_from_ratio`, `dew_point`, `sat_pressure` | Adding a latent load to `step()` changes `kwh` for every existing controller and **invalidates the published −26.6% headline**. Also risks breaking A/B determinism if RH is drawn from anything but the seed. | Keep latent load out of the energy path until the number is re-measured and the README/PRODUCT numbers are updated in the same commit. Derive RH from `outdoor_rh(t, seed)` only. |
| **`DigitalTwin.clone()`** | deep copy incl. counters + humidity | A shallow copy of `T`, `kwh_by_zone`, or `weather_fn` closure silently couples the clone to the live twin — what-if results would then corrupt the demo. | `clone()` must copy every mutable attribute and be covered by a test that steps the clone 60× and asserts the original's `t`, `kwh` and `T` are unchanged. |
| **What-if engine** (`backend/whatif.py`) | `SCENARIOS`, `run_scenario`, `compare` | Mutating the live twin/store (hard rule 6). Second risk: multi-seed runs are CPU-bound in the same process as the sim thread and will stutter the live demo. | Clone first, always. Cap `horizon_h × len(seeds)`; run scenarios on request only, never on a timer. |
| **Decision log** (`backend/decisions.py`) | `build_decision`, `DecisionLog(maxlen=500)` | `build_decision` called for 5 zones every simulated minute at 960× = 80 objects/real-second. Unbounded logging would eat memory and lock time. | `maxlen` deque; build decisions only when something changed, or sample them. |
| **Multi-objective + safety modes** (`ConstraintAware(objective=…, safety_mode=…)`) | `set_objective`, `set_safety_mode`, `lock_zone`, `pending_recommendations` | Changing `ConstraintAware.__init__` defaults would silently change `demo_day` numbers and the RL baseline (`sim/env.py:23` instantiates it at import). | New kwargs must default to `objective="balanced"`, `safety_mode="automatic"` and reproduce today's behaviour bit-for-bit. Regression: `demo_day --days 7 --seed 7` must still print 530 kWh / 0 viol-min. |
| **Maintenance monitor** (`backend/maintenance.py`) | `MaintenanceMonitor.tick` | False positives on stage. `at_capacity` is genuinely common in the cafeteria at lunch and is not a fault. | Require sustained evidence (N consecutive minutes) plus a confidence, and show the evidence strings verbatim. |
| **Analytics** (`backend/analytics.py`) | heatmap, energy series, complaint/controller stats | Another unbounded in-memory series; and heatmaps invite fabricated smoothing. | Fixed-size ring buffers sized in sim-days; only aggregate data that was actually sampled. |
| **Adapters** (`backend/adapters.py`) | `HVACAdapter`, `OccupancyAdapter`, … Protocols | Protocols with only a Sim implementation are architecture theatre unless the controller actually calls through them (hard rule 7). | Either route `ConstraintAware` through the adapter registry, or don't ship it. |
| **Privacy** (`backend/privacy.py`) | `scrub_pii`, `anonymize_author`, `RetentionPolicy`, `export_records` | Scrubbing the complaint text *before* parsing would destroy zone cues ("Rahul's cabin"). Retention deleting `store.items` breaks `ComfortMemory.patterns`. | Scrub for display/export only, after parse. Retention must expire feed entries, not constraint history the memory depends on. |
| **Multi-zone complaints** (`ParsedComplaint.zone_ids`, `store.add_many`) | list-valued zones | `zone_id` is read in five places (app.py:136/141/149/155/194, dashboard:334/408). Any change that stops populating it breaks the Slack reply and the conflict outline. | Keep `zone_id == zone_ids[0] or None` as an invariant, asserted in a test. |

---

## 10. How to run it (unchanged)

```bash
cd /d/Cygnix/feelslike
.venv/Scripts/python -m scripts.demo_day            # evidence table
.venv/Scripts/python -m evals.run_nlp_eval --rules  # NLP benchmark, offline
.venv/Scripts/python -m uvicorn backend.app:app     # live demo on :8000
```

Prefix `PYTHONIOENCODING=utf-8` for anything printing benchmark text — the console is cp1252.
