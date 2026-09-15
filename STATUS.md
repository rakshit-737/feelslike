# FeelsLike — Implementation Status

Live checklist for the multi-agent upgrade. Updated at every workflow checkpoint.

**Last updated:** 2026-08-17 · Workflow 1 (Phases A+B) still in flight; docs refreshed from measured results; parser claim independently re-verified.

| Marker | Meaning |
|---|---|
| `[x]` | Implemented **and** verified by a run I saw the output of |
| `[~]` | Code landed on disk, verification still pending |
| `[>]` | In flight right now (agent working) |
| `[ ]` | Not started |
| `[-]` | Deliberately out of scope — reason given |

---

## 0 · Phase board

| Phase | Scope | Status |
|---|---|---|
| **A — Understanding** | contracts, architecture docs, twin physics, parser | `[x]` **verified** — 49/49 contract tests pass |
| **B — Core intelligence** | constraints, explainability, controllers, what-if, maintenance, analytics, adapters, privacy | `[x]` **verified** — modules import, isolation holds, frozen numbers intact |
| **C — Integration** | `backend/app.py` — endpoints, live wiring, demo mode | `[x]` **verified 2026-08-30** — all endpoints pass the API suite + live smoke run |
| **D — Frontend** | dashboard tabs, new panels, occupant mobile page | `[~]` all 11 tabs built (panels.js 2.4k lines, syntax-checked, served); endpoints live-verified; **browser walk-through by the team still pending** |
| **E — Tests & QA** | unit suites, adversarial QA sweep | `[x]` **464 passed, 0 xfailed** (2026-08-30) — contract + nlp + constraints + thermal + controller + simulator + api + hardware + calibration; **every strict-xfail defect in the ledger is now fixed with a general rule and converted to a regression** |
| **F — Productization** | flip verified capability flags, regenerate docs, commit | `[ ]` flags flip only after the team sees each subsystem's output (§6 gate) |

**Finals-phase additions (2026-08-30, all verified by runs pasted in session):**
- `scripts/fit_rc.py` + `tests/test_calibration.py` (9 tests) — RC calibration fitter;
  self-test recovers synthetic truth within 0.5% (SHT31 noise) / 0.5% (DHT22 noise);
  labeled `selftest` vs `measured` — nothing presented as hardware data yet.
- Hardware live card in the Twin tab (`dashboard/panels.js`): connection/health badges,
  measured-vs-twin chart with heater shading, real heater toggle with duty meter.
  Live smoke: complaint on zone_b → fan command 0→1 in the node's next poll reply.
- `/api/hw/log` rows now carry `fan`/`heater` at reading time (calibration needs the
  timeline); reading POST also returns them unchanged.
- Q&A drill v2 in `IMPLEMENTATION.md` §8b (hardware / feasibility / AI questions).
- Proposed 3-minute finals cut in `docs/PRESENTATION_SCRIPT.md` Appendix D (team tunes);
  stale facts in the 8-min script corrected (retraction fix, test counts, panel status).
- **Gap-closers (team said yes, verified live 2026-08-30):** `backend/tariff.py` +
  `meters.tou` + Analytics ToD card + Overview sub-line (smoke: 5.2 h window billed
  ₹381 us / ₹488 base at ToD while flat figures stayed kWh×9); `GET /api/rl` + RL
  trajectory card in Experiments (164-point curve, final −86.8 @ 2.0M steps, measured
  table). `tests/test_tariff.py` (19 tests). Suite now **483 passed, 0 xfailed**;
  frozen numbers exact. Team browser walk-through of all tabs: done (item 1 confirmed).

**Finals-phase addition (2026-09-15, verified by runs pasted in session): Monitor tab.**
- `backend/telemetry.py` — step-cadence (60 sim-s) ring buffer, 7 sim-days, read-only observer of
  both twins (physics bit-identical with/without it: `tests/test_telemetry.py`). Derived signals
  with documented formulas: CO₂ single-zone mass balance (ASHRAE 62.1 form, uses the twin's own
  ventilation/infiltration couplings; flagged `co2_estimated`), comfort score 0–100 (heuristic,
  explicitly NOT PMV), per-zone power/capacity. Threshold table `THRESHOLDS` (ASHRAE 55/62.1,
  WHO 2021, EN 12464-1) served to the UI, never duplicated client-side. `forecast()` = the same
  ConstraintAware controller run ahead on clones (what-if isolation rules), labelled `predicted`.
- `backend/external.py` — REAL outdoor feed (Open-Meteo, CC BY 4.0, no key; site = VIT Chennai,
  `FL_LAT/FL_LON/FL_SITE`; `FL_EXTERNAL=0` disables) and the HISTORICAL UCI occupancy dataset
  (`data/uci_occupancy.csv`, 20 560 one-minute rows; `scripts/fetch_datasets.py` rebuilds it).
  Neither feeds the physics. `data/README.md` records provenance + datasets evaluated but not used.
- Endpoints (all additive): `GET /api/telemetry`, `/api/monitor`, `/api/forecast`,
  `/api/external` (+ `POST /api/external/refresh`), `/api/dataset`; `/api/state` gains `monitor`.
- `dashboard/monitor.js` (own file; `panels.js` untouched) — zone selector, Live/1h/6h/24h/7d/custom
  windows, 13 KPI cards (current / previous 15 sim-min / Δ% / Normal-Warning-Critical), 9 charts
  with crosshair tooltips, click-to-pin drill-down (per-zone table at that instant → click a zone
  to focus), alerts table, source badges on every card and chart. Sim time and wall time are never
  drawn on one axis. Noise = "not instrumented" (no source anywhere); light comes only from the
  historical dataset and says so. Auto-refresh keyed to sim clock movement.
- Preservation: frozen numbers re-run exact (722.4 / 493.4 / 530.3 kWh; 16 328 / 429 / 0 viol-min);
  `/api/state` legacy keys untouched; no new runtime dependency (httpx was already required).
  Suite: 501 passed in the cowork container (the RL-trajectory test needs `rl/models/progress.csv`,
  present only on the team PC). Headless-browser check of the tab: zero console errors across
  window/zone changes, drill-down, mock-node hardware card and live Open-Meteo feed.
- Known limitation surfaced by the tab (deliberately not hidden): simulated indoor RH sits at
  ~85–94 % because of the coil ADP approximation (twin docstring), so the Humidity KPI reads
  *critical* and its alert row carries an "ⓘ model limitation" note. Fixing it is one constant
  (`ADP_APPROACH`) and a team decision, since it changes humidity metrics in the report.
- Not done / proposed: validate the CO₂ estimator against the UCI sensor curve (needs an assumed
  room volume + ventilation rate for that office, `[DETAIL REQUIRED]`); a real CO₂ point (SCD40)
  on the rig would make `co2_estimated=false` for zone_b.

**Hardware bring-up, stage 1 (2026-09-15, verified on the bench — first real node):**
- Board: classic ESP32 (ESP32-D0WD-V3, CP2102 USB-UART; Windows needed the Silicon Labs CP210x
  Universal driver, Code 28 before). Sensor DHT22 on GPIO 4. Actuator channels on indicator LEDs
  (GPIO 16 fan, GPIO 17 heater) pending the power stage — firmware and pins unchanged for the swap.
- Network lesson: phone hotspot must broadcast 2.4 GHz (ESP32 cannot see 5 GHz); server must run
  with `--host 0.0.0.0`; laptop IP is DHCP and goes into `secrets.h`.
- Verified with server logs + node echo + eyes on the LEDs:
  - sensing: `shoebox-1` POSTs every ~2 s, all 200; 30.5-30.9 degC / 77-79 % RH; sensor health ok.
  - control: "it is really stuffy in conference room b" -> LLM parse (zone_b, stuffy, sev 2) ->
    ConstraintAware vent +1 -> node echoed `fan=1` in 2 s; fan LED lit.
  - safety layer 2: heater commanded on, server duty cap forced it off at duty 0.50
    (`duty_limited: true`); heater LED went dark.
  - safety layer 1: server killed 16:24:47 -> fan LED off within the 10 s watchdog (seen);
    server back 16:25:08 -> node echoed `fan=1` at 16:25:22 -> LED back on (seen). Run twice.
- Bench note: at 240x a complaint expires in ~30 real s, so actuator tests run the sim at 10x.
- Not yet verified: safety layer 0 (physical switch needs the actuator supply), real fan and
  heater through the L9110, calibration run. Firmware credentials moved to git-ignored `secrets.h`
  (template `secrets.h.example`); the password was never committed.
- Also verified on the team PC: the Monitor tab from the separate session - 502 passed, frozen
  numbers exact, 13 KPI cards (11 server + light/noise client-side), UTF-8 clean, zone filter and
  the hardware card wired to the real node.

**Wired ambient node, server side (2026-09-15, verified by runs pasted in session):**
- `backend/hardware.py` `SensorNodeStore` — sensor-only nodes kept apart from the actuator bridge:
  the reply is an acknowledgement, never a command, and a sensor post cannot overwrite the rig's
  reading (regression `test_a_sensor_node_can_never_touch_the_rig`). Temp OR fault per reading
  (faults recorded, never turned into numbers); inferred health stale / sensor_fault / stuck;
  at most 8 nodes, least recently seen evicted; a `calibrated` flag only a real boolean true sets.
- Endpoints (additive): `POST /api/hw/sensor`, `GET /api/hw/sensors`,
  `GET /api/hw/sensors/{node_id}/log`; `ambient` added to `/api/hw/status`, `/api/state.hardware`
  and `/api/monitor.hardware`. Monitor's Physical-zone card shows the room temperature, labelled
  *(uncalibrated)* until the node reports `calibrated: true`.
- `hardware/firmware/ambient_node_uno/ambient_node_uno.ino` — LM35 on A0, internal 1.1 V reference,
  64x oversampling, one JSON line per 2 s, rail-fault reporting, `CROSS_CALIBRATED` flag.
- `scripts/serial_bridge.py` (USB serial -> `/api/hw/sensor`; auto-detects Arduino VIDs and
  excludes the ESP32's CP210x) and `scripts/crosscal_ambient.py` (time-pairs Uno vs DHT22 readings,
  gain-only VREF correction with residual check and DHT22-accuracy uncertainty).
  `requirements-hardware.txt` holds pyserial — tooling only, never imported by `backend/`.
- `tests/test_ambient.py` (32 tests). Suite **534 passed**; frozen numbers exact; `monitor.js`
  syntax clean. README flashing step corrected for `secrets.h`.
- **Not yet verified on hardware:** the Uno has not been flashed or bridged; the LM35 is not
  cross-calibrated, so no ambient number is claimed. The L9110 stage (decision 15) is not wired yet: there is
  no fan on hand and the LEDs work off the ESP32 pins directly, so it is optional until a
  fan arrives - its only immediate value is testing safety layer 0. The fuse's rating is
  unreadable, so no protection claim is made for it.

**Commercial dashboard, Phase 6 (2026-09-16, verified by runs in session): security and secure connectivity.** Full detail: `docs/SECURITY.md`.
- `backend/security/` — config (development / enforced / production; production refuses to start without https origins,
  TLS files or upstream termination, users file), scrypt passwords (stdlib; no crypto packages installed), opaque revocable
  bearer sessions (no cookies, so no CSRF surface), 6 roles + zone scoping, fail-closed route POLICY table (test asserts
  every route listed), redacting audit log (memory + optional JSONL), token-bucket rate limits, device registry
  (HMAC-SHA256 keys, rotation, revoke/expire, quarantine), SecureIngest (signature, assignment claims, source label
  ignored, freshness, replay window, auto-quarantine) in front of the EXISTING LatestStore, SIMULATED MQTT broker +
  adapter, MQTTS / BACnet-Modbus gateway config validators (config only), security headers + safe 500s.
- API: `/api/security/{login,logout,whoami,status,metrics,events,devices,users}`, `POST /api/telemetry/ingest`,
  `POST /api/control/zone/{zone_id}`, `GET /api/occupant/rooms`. Existing endpoints gained backend role/zone checks + audit.
  The 20 simulated devices now publish through the same signed pipeline; security faults exercise it.
- Dashboard: sign-in dialog, whoami pill, Security tab (restricted message on 403); occupant page sends its token.
- Verified: **736 passed** (24 security tests); frozen demo_day 722.4 / 493.4 / 530.3 kWh exact. Headless-Chrome walk
  in ENFORCED mode: 401 before login, login/logout, all tabs render, Security tab (20 devices all SIMULATED, no key material),
  occupant 403 on control and security, operator 403 on zone_b and 200 on zone_a (audited), facility manager 403 on users,
  unknown device 401, occupant page shows only its room and no admin info, 0 JS exceptions, 0 secrets in console.
  Audit + server logs scanned: 0 passwords, tokens, hashes or keys. Login ~350 ms (scrypt); security GETs 5–13 ms median.
- Fixed during validation: MQTT adapter mutated the signed envelope (now passes topic claims separately); reinstating a
  device now clears its failure window; requests with no credentials are counted as `unauthenticated_request`,
  not `auth_failure`.
- **Not provided (production requirements):** TLS termination, real MQTT broker / client, BACnet/SC, shared rate-limit
  and session store across workers, tamper-evident audit storage, SSO/MFA, HSM/KMS key storage. The local demo is plain HTTP.

**Commercial dashboard, Phase 5 (2026-09-16, verified by runs in session): seasonal physical twin + SIMULATED hardware.**
- `backend/scenario.py` + `backend/loads_bridge.py` — the Phase-2 WeatherModel (no second weather engine) drives the LIVE
  twin through its existing weather / RH / solar hooks: season (summer / monsoon / winter / transition / auto), climate
  (delhi / chennai), bounded override layer (cloud, rain, disturbance) over the unmodified model, existing knobs
  (outdoor / humidity offsets, solar, occupancy, capacity), new `envelope_scale` twin hook (1.0 bit-identical), profile
  internal loads (lighting + plugs + equipment/IT as sensible gains, HVAC sized for them). Applied to BOTH twins. Classic
  weather stays the default and is bit-identical to the original twin (tested). Seasonal mode uses the dataset's 12 °C
  coil ADP; classic keeps the documented RH approximation (shown in the UI). **Heating is NOT MODELLED in the live twin**
  (controller is cooling-only) — reported everywhere, never invented. Causal explanation from telemetry deltas; season
  comparison on clones (SIMULATED / WHAT-IF). Reset scenario restores the baseline without rebuilding or touching history.
- `backend/simhw.py` — **SIMULATED HARDWARE, NOT REAL HARDWARE**: 20 devices (TEMP/HUM/CO2/OCC × 5 zones) mapped device →
  zone → floor → building, simulated MQTT / Modbus / BACnet / HTTPS loopback adapters, sampling interval, noise, precision,
  range, bias, drift, dropouts, comm delay, faults (offline / stuck / drift / invalid / delay), all through
  `LatestStore.ingest` (source `sim`, origin `simulated_hardware`). While on, the twin stops publishing those metrics, so a
  failed sensor really is Unavailable / stale. Controller keeps reading the twin (no sensor-failure fallback — documented).
- Endpoints (additive): `/api/scenario` (GET/POST), `/api/scenario/reset`, `/api/scenario/compare`, `/api/simhw` (GET/POST),
  `/api/simhw/devices/{id}/fault`, `/api/simhw/devices/{id}/config`; `/api/latest` gains `weather`, `scenario`, `causal`,
  `telemetry_mode`, zone `internal_gain` / `hvac.heating_demand`, building `cooling_capacity_pct` / `heating_demand` /
  `equipment_power`; readings gain `origin`. New Scenario tab (`dashboard/scenario.js`).
- Verified: `tests/test_scenario.py` (19) + `tests/test_simhw.py` (12); full suite **712 passed**; frozen 7-day numbers exact.
  Controlled API run, same building and clock (Mon 11:30 after 90 sim-min, office, Delhi): summer 34.6 °C out → cooling 80 %
  of capacity, 8.5 kW, 14.4 kWh, comfort 96; monsoon 30.9 °C / 66 % RH / cloud 62 % → indoor RH 73 %, 6.4 kW; transition
  25.3 °C → 35 %, 3.2 kW, 7.0 kWh, comfort 96; winter 16.0 °C → cooling 0, 0.87 kW (fans), indoor 21.9 °C, comfort 75,
  heating NOT MODELLED. Summer + capacity 0.2 + 6 K at 13:30: all 5 zones AT CAPACITY, indoor 39.9 °C, comfort 38.
  Data-centre profile loads at 03:00 with nobody in: 88.7 kW IT gain in the white space, 24 kW HVAC. Headless Chrome:
  Scenario tab, season switches, causal chain, simulated hardware on (20 devices ONLINE, origin simulated_hardware),
  invalid fault → "Unavailable" + DEGRADED + comfort missing input (no zeros), offline → OFFLINE, delay → DEGRADED, season
  comparison, 4 `/api/latest` requests in 20 s, all older tabs / history / occupant page OK, no console errors beyond favicon.
  Found and fixed in the walk-through: the environment form did not refresh when the scenario changed elsewhere.
- Not done (Phase 6): real protocols, device identity, authentication, encryption, secure ingestion.

**Commercial dashboard, Phase 4 (2026-09-15, verified by runs in session): latest-value telemetry.**
- `backend/latest.py` — `LatestStore` (own lock; `sim.lock → store lock` only): validated `ingest()` with
  server-side source tags (payload labels ignored, ids pattern-checked), invalid values stored as invalid with the
  rejected value, missing as missing, freshness on read (good ≤ 60 s, aging ≤ 300 s, stale > 300 s, offline > 30 min),
  source preference, 15-min recent buffer, counts, sensor health, `system_status()` (SIMULATION MODE / LIVE TELEMETRY /
  DEGRADED / STALE DATA / OFFLINE from freshness only).
- `backend/telemetry_publish.py` — the twin publishes every step through the SAME ingest (sim / derived / predicted:
  temperature, RH, occupancy, CO₂ estimate, HVAC mode/cooling %/fan/setpoint/power, controller action, demand, energy
  today, expected demand, outdoor); comfort is computed FROM the store (Phase-3 `ComfortReading` with sources + ages) and
  published as derived with its oldest input's timestamp; hardware seam: `/api/hw/reading` and `/api/hw/sensor` publish
  accepted readings as `hardware` (shown as alternatives; `FL_HW_AUTHORITATIVE=1` makes them primary).
- Endpoints (additive): `/api/latest`, `/api/latest/zone/{id}`, `/api/latest/health`, `/api/latest/sensors`,
  `/api/latest/trend`, `/api/latest/{metric}`; `/api/comfort/occupant` gains `updated_age_s`.
- Dashboard: ONE central `/api/latest` poll in the shell (default 5 s, `?poll=` / `fl.pollS`), header telemetry status,
  Building-tab "Current conditions" (building tiles, 5 zone rows with source/quality/age per value, telemetry health,
  source summary, 15-min live trend, sensor health), occupant "updated N s ago".
- Verified: `tests/test_latest.py` 32 tests; full suite **681 passed**; headless Chrome: values auto-refresh (sim clock
  Mon 09:11 → 09:50 between polls), labels SIMULATED/DERIVED/PREDICTED/ESTIMATED, ages, hardware post → LIVE TELEMETRY
  with the rig value as an alternative, invalid rig post 422, CO₂ "Unavailable" after reset, forced backend failure →
  OFFLINE with last values kept, 4 `/api/latest` requests in 20 s (no storm), all older tabs + history charts +
  occupant page working, no console errors beyond favicon and the intentional 422.
- The digital twin is the telemetry source; this is **not** physical building telemetry. Not done (later phases):
  hardware simulation / seasonal scenarios (Phase 5), security / device identity / protocols (Phase 6), push
  transport (SSE/WebSocket), persistence of latest values across restarts.

**Commercial dashboard, Phase 3 (2026-09-15, verified by runs in session): occupant comfort.**
- `backend/comfort.py` — the one comfort engine (engineering index, not a PMV/PPD certification): thermal /
  humidity / CO₂-ventilation-indicator sub-scores against the profile ranges, profile `comfort_weights`
  (validated, editable), occupancy relevance (Unoccupied / Partially Occupied / Occupied), per-dimension statuses,
  severity, root cause ranked severity-then-impact, HVAC-aware recommendation with expected effect + energy
  consideration, data quality (missing / invalid / stale; nothing invented), `controller_preference()` interface
  for comfort vs energy priority. Input is a `ComfortReading` — the Phase-4 latest-value seam.
- `backend/comfort_events.py` (debounced events + resolution + per-day occupied/uncomfortable seconds),
  `backend/comfort_whatif.py` (comfort vs energy on clones, SIMULATED / WHAT-IF, isolation verified).
  `backend/dataset/comfort.py` now uses the engine's formulas (dataset values unchanged).
- Endpoints (additive): `/api/comfort`, `/api/comfort/zone/{id}`, `/api/comfort/events`, `/api/comfort/history`
  (live buffer or historical dataset; partial / "No data available"), `/api/comfort/tradeoff`,
  `/api/comfort/occupant`; `/api/building/zones/{id}` gains `comfort`; profile POST accepts `comfort_weights`.
- Dashboards: new Comfort tab (KPIs, heatmap → drill-down, worst zones + why, trends, events, durations,
  trade-off); Building drill-down comfort block; occupant page "How this room feels right now" card.
- Verified: `tests/test_comfort.py` 32 tests; full suite **649 passed**; headless-Chrome walk-through (Comfort tab
  filters/ranges/historical, trade-off, heatmap → drill-down, six profiles change thresholds/weights, occupancy
  relevance, events, all older tabs, occupant card with no infrastructure leak), no console errors beyond favicon.
- Surfaced finding: in the live twin every occupied zone reads Humid/Severe because of the documented RH
  approximation (ADP_APPROACH) — shown with a model note, not hidden or re-scored.

**Commercial dashboard, Phase 2 (2026-09-15, verified by runs in session): historical dataset + History tab.**
- `backend/dataset/` pipeline (config → calendar → seasonal weather → occupancy → loads → the EXISTING
  `sim/twin.py` per floor under a profile BMS schedule → CO2/comfort/PMV → energy/PV/grid/demand/forecast →
  RAW sensors → validate → CLEAN), SQLite store (stdlib), analysis, `scripts/generate_dataset.py`.
  `sim/twin.py` gained optional hooks (RH, solar, occupancy, internal gains, per-zone capacity, heating,
  coil ADP) — defaults bit-identical: frozen 722.4 / 493.4 / 530.3 kWh, 16 328 / 429 / 0 viol-min re-verified.
- Default dataset generated (seed 42, 293 s): 42 days 2026-05-18 → 06-28, 5-min, 6 buildings, 35 zones,
  12 096 timestamps, 423 360 zone rows, 72 576 building rows, 423 360 raw + 423 360 clean rows, 17 anomalies,
  324 MB (git-ignored). Hourly correlations seen (office / mall / hospital): occupancy→CO2 0.94 / 0.95 / 0.96,
  occupancy→energy 0.96 / 0.98 / 0.93, outdoor T→HVAC 0.85 / 0.90 / 0.95, HVAC→energy 0.98 / 0.97 / 0.98,
  cloud→solar (10–15 h) −0.67, solar→PV 0.997. Data centre: 126 MWh with ~3 people (IT load).
- Endpoints (additive): `/api/history/catalog`, `/api/history`, `/api/history/compare`, `/api/history/anomalies`,
  `/api/history/quality`, `/api/history/export`. New `History` tab (`dashboard/history.js`).
- Verified: `tests/test_dataset.py` 28 tests; headless-Chrome walk-through of History (9 charts, 1h–30d, building /
  floor / zone, 6 comparisons, anomalies, raw vs clean, CSV export) + Phase-1 tabs, no console errors beyond favicon.
  Docs: `docs/DATASET.md`, `DATA_CONTRACTS.md` §11, `data/README.md` §4.
- Not done (by instruction, later phases): advanced occupant comfort (Phase 3), real-time latest-value pipeline,
  hardware simulation/scenario UI, security.

**Commercial dashboard, Phase 1 (2026-09-15, verified by runs in session): Building tab.**
- `backend/building.py` — six commercial profiles (office, mall, hospital, hotel, college,
  data center) as a validated `BuildingConfig`; floors → zones topology (unmodelled floors carry
  no values); zone status flags against the PROFILE comfort/CO₂ limits; demand model (occupancy,
  HVAC, cooling, heating = not modelled, ventilation per ASHRAE 62.1, lighting estimate, energy,
  comfort, peak) with expected values from the profile schedule; 12 executive KPIs; factual
  zone "why" from the controller's decision record. Pure projection: the physics, the 5 zones,
  the parser and the A/B race are untouched. Operating modes map onto the EXISTING controller
  levers (objective + safety mode).
- Endpoints (additive): `GET /api/building`, `GET|POST /api/building/profile`,
  `GET /api/building/zones/{id}`, `GET /api/building/demand`; `/api/state` gains `building`;
  `TelemetryStore.rows_between()` (read-only) added.
- `dashboard/building.js` — new landing tab (Overview and every other tab unchanged and
  deep-linkable); control bar, KPI strip, building tree, demand table + interactive chart,
  zone drill-down, validated configuration form. `monitor.js` exports its chart primitive as
  `window.FLChart` (additive).
- Verified: `tests/test_building.py` (55 tests) + full suite **589 passed**; frozen numbers
  exact (722.4 / 493.4 / 530.3 kWh; 16 328 / 429 / 0 viol-min); headless-Chrome walk-through
  (landing, 12 KPIs, type switch to hospital, floor filter, zone drill-down with 6 trend charts,
  mode change moved the controller objective, invalid form rejected with nothing applied, valid
  form saved, all 12 existing tabs still render) with no console errors beyond the missing
  favicon and the intentional 400. Docs: `docs/BUILDING_PROFILES.md`, `DATA_CONTRACTS.md` §10.
- Not done (later phases, by instruction): dataset expansion, real-time telemetry architecture,
  seasonal weather, hardware simulation, security architecture; scaling the 5 modelled zones
  to the configured building; a dedicated peak-shaving control law; heating.

**Workflow 1 outcome (verified 2026-08-17):** 10 agents, 0 errors. Every Phase A+B `[~]` above is now `[x]`:

```
49 passed in 2.28s                      contract conformance, zero cross-agent drift
Static   722.4 kWh  16328 viol-min      frozen headline numbers EXACT
Reactive 493.4 kWh    429 viol-min
FeelsLike 530.3 kWh     0 viol-min
PPO      512.7 kWh     22 viol-min
app imports OK · what-if isolation True · 11 scenarios · 5 adapters
multi-zone complaint drove zone_d 25.0 -> 23.7 C, 2 decisions emitted
```

---

## 1 · Preservation guarantees (the "do not break it" contract)

These are regression tripwires, re-checked at every checkpoint.

- `[x]` `.act(twin, store)` signature frozen across all 4 controllers (callers: `app.py`, `demo_day.py`, `rl/*`, `sim/env.py`)
- `[x]` `/api/state` legacy keys additive-only — no key removed or renamed
- `[x]` `ParsedComplaint.zone_id` retained as back-compat scalar alongside new `zone_ids[]`
- `[x]` Frozen headline numbers unchanged: **722.4 / 493.4 / 530.3 kWh**, **16,328 / 429 / 0** viol-min
- `[x]` NLP dev split stays **30/30**; blind probe is the number quoted publicly
- `[x]` Existing dashboard (race hero, floor plan, feed, conflict treatment) untouched by backend work
- `[x]` No new pip runtime dependencies (pytest / python-pptx / python-docx are dev-only)
- `[x]` Determinism: same seed → identical kWh **and** identical RH

---

## 2 · Subsystem checklist

### Agent 0 — Lead Architect
- `[~]` `PROJECT_ARCHITECTURE.md` — current architecture, data flow, dependency graph, risks
- `[~]` `DATA_CONTRACTS.md` — every canonical type + per-boundary INPUT/OUTPUT/SIDE EFFECTS/ERRORS
- `[~]` `IMPLEMENTATION_PLAN.md` — phases, file-ownership table, tier priorities
- `[x]` File-ownership map enforced (no two agents share a file; `app.py` + `index.html` single-owner in Phase C/D)
- `[x]` Shared contract pinned **before** parallel work started (the anti-drift mechanism)

### Agent 1 — Backend / domain models
- `[~]` `backend/contracts.py` — canonical types, `OBJECTIVE_WEIGHTS`, `SAFETY_MODES`, `to_dict`, deterministic `new_id`
- `[~]` `ControllerDecision`, `ConstraintView`, `MaintenanceAlert`, `ScenarioSpec`, `ScenarioResult`
- `[ ]` `Experiment` record persisted across runs (currently per-invocation JSON only)

### Agent 2 — AI / NLP
- `[~]` Staged pipeline: normalize → language → retraction → zones → intent → severity → context → confidence
- `[~]` **Multi-zone** extraction (`zone_ids[]` + per-zone confidence)
- `[~]` Typo tolerance (bounded edit-distance, stopword-guarded)
- `[~]` Hinglish **and** Tamil-English lexicons as extensible module data, not inline branches
- `[~]` Sarcasm / indirect cold ("gloves to type", "winter coat")
- `[~]` Appliance + outdoor-weather negative guards ("coffee machine is hot", "40 degrees outside")
- `[~]` Real confidence calculation + `requires_clarification`
- `[~]` LLM prompt teaches the same multi-zone schema; tolerant coercion of old/sloppy responses
- `[x]` Benchmark: 50 frozen cases + `zone_ids` gold + 20 new `heldout2` cases (70 total)
- `[x]` Runner reports multi-zone set match + clarification metric per split
- `[x]` LLM-path benchmark re-run with the Groq key — both parsers measured
- `[x]` **Blind probe** (`evals/blind_probe.json` + `run_blind_probe.py`) — 20 cases never used for development, the honest generalization number

**Verified NLP numbers (blind probe v2, 20 unseen cases, single-shot 2026-08-30):**

| Metric | Rules | LLM (Groq gpt-oss-120b, auto mode) |
|---|---|---|
| Zone set (exact, multi) | **90%** | **90%** |
| Complaint detection | 60% | 70% |
| Issue extraction | 65% | 75% |
| **Exact triple** | **45%** | **55%** |

Probe PROVENANCE (the honesty mechanism working, twice): probe v1 scored 50/60 blind;
its failure categories were then studied on 2026-08-17 and general fixes landed, which
BURNED it — its post-study re-score (80%) measures "were those sentences fixed", not
generalization, and is never quoted. v1 is archived as `blind_probe_v1_burned.json`;
v2 (same adversarial category mix, authored 2026-08-30 after all development, gold
committed before measurement, run once per mode) is the only number the docs quote.
`run_blind_probe.py` now REFUSES to score a probe marked burned. v2 is deliberately
harder than v1 — both zone-set misses are over-extraction of a real extra zone, never
a hallucinated one, and the clarify-don't-guess path holds. Tuned splits still read
~100% — quoting them would be the exact inflation §7.1 documents.

### Agent 3 — Thermal / digital twin
- `[~]` `sim/humidity.py` — Magnus psychrometrics, seeded outdoor RH
- `[~]` Per-zone moisture balance (ventilation + latent gain + SHR coil dehumidification + infiltration)
- `[~]` `rh_now`, `dew_point_now`, `zone_snapshot`, `metrics()` gains `humid_viol_min` / `mean_rh` / `at_capacity_min`
- `[~]` Live condition knobs: `occ_scale`, `capacity_scale`, `solar_scale`, `outdoor_offset`, `humidity_offset` + validated `set_conditions`
- `[~]` `clone()` — fully independent deep copy (the foundation of what-if isolation)
- `[~]` Determinism preserved under seeding
- `[-]` Humidity does **not** charge kWh in this version — documented modeling assumption so the frozen A/B numbers stay valid

### Agent 4 — Control / optimization
- `[~]` Objectives COMFORT / ENERGY / COST / CARBON / BALANCED driven by `OBJECTIVE_WEIGHTS` (balanced reproduces today's numbers exactly)
- `[~]` Humidity-aware control (extra depression above 65% RH, SHR-justified)
- `[~]` Crowd-scaled occupancy control (cafeteria lunch surge is the test case)
- `[~]` Safety modes: automatic / recommend_only / human_approval / emergency_override / maintenance_lockout
- `[~]` Hard safety envelope as a single un-bypassable choke point (21.5–29.0 °C, ±1.8 °C offset, vent 0–2)
- `[~]` `last_decisions` populated per `act()` without breaking the frozen signature

### Agent 5 — Explainability
- `[~]` `backend/decisions.py` — `build_decision()` from real controller state, **no free-form LLM text**
- `[~]` Physics-grounded `est_energy_delta_pct` + `est_comfort_delta_pct`
- `[~]` `reason_code` taxonomy (no_change / occupancy_setback / precool / complaint_offset / conflict_compromise / safety_clamp / recommend_only / locked_out / emergency_override)
- `[~]` `DecisionLog` — bounded, per-zone query, timeline for charting
- `[ ]` Explainability **UI panel** ("Why did the system change this?") — Phase D

### Agent 6 — What-if / experiments
- `[~]` `backend/whatif.py` — clone-based scenario engine
- `[~]` ≥8 scenarios: setpoint ±1, occupancy ±, outdoor +3, humidity +15, capacity loss, ignore complaint, complaint expiry, objective swap
- `[~]` Multi-seed with mean / sd / 95% CI; `measured` vs `predicted` labelled honestly
- `[~]` `verify_isolation()` proving the live twin/store are never mutated
- `[~]` `scripts/run_experiments.py` CLI → `evals/results_whatif.json`
- `[ ]` What-if **UI panel** with baseline-vs-scenario comparison — Phase D

### Agent 7 — Maintenance intelligence
- `[~]` Capacity / sensor / actuator / recurring detectors with quoted numeric evidence
- `[~]` Stateful alerts (update, never duplicate), auto-resolve, rising confidence
- `[~]` `suppress_setpoint_chasing(zone)` exposed
- `[ ]` Wire `suppress_setpoint_chasing` into the controller loop — Phase C
- `[ ]` Maintenance **UI panel** — Phase D

### Agent 8 — Analytics
- `[~]` `AnalyticsStore` — bounded ring buffers, comfort heatmap, energy series, complaint/controller stats, setpoint timeline, summary
- `[ ]` Charts rendered in the UI (heatmap, timeline, trends) — Phase D

### Agent 9 — Frontend / UX
- `[ ]` Tab navigation added to the existing dashboard (reusing current tokens, header, logo, card system)
- `[ ]` `dashboard/panels.js` — new panels isolated from the polished existing markup
- `[ ]` Overview / Live twin / Complaints / Control / Explainability / What-if / Analytics / Maintenance / Experiments / Settings / Demo
- `[x]` Design decision: extend the existing single-file dashboard, no framework, no build step, existing visual identity preserved

### Agent 10 — Digital twin UI controls
- `[ ]` Live sliders: outdoor temp, solar, humidity, occupancy, HVAC capacity, fan, seed
- `[ ]` Complaint injection + `RESET SIMULATION`
- `[ ]` Permanent "SIMULATION MODE — no real HVAC connected" banner

### Agent 11 — Mobile occupant interface
- `[ ]` `dashboard/occupant.html` — responsive quick-report form (issue / room / severity / anonymous)
- `[ ]` Post-submit pipeline trace (Received → Parsing → Validated → Constraint → Controller responded)

### Agent 12 — Safety / security / privacy
- `[~]` `backend/privacy.py` — PII scrub, stable pseudonymous authors, retention policy, export, redact
- `[~]` `AIDisclosure` — external-AI use never hidden
- `[ ]` Safety-mode + privacy **controls in the UI** — Phase D
- `[ ]` Anonymous toggle wired end to end — Phase C/D

### Agent 13 — Integration adapters
- `[~]` `HVACAdapter` / `OccupancyAdapter` / `WeatherAdapter` / `SensorAdapter` / `NotificationAdapter` protocols + Sim implementations + registry + conformance helper
- `[-]` Real BACnet / Modbus / MQTT clients — deliberately not implemented (no fake network code; the seam is the deliverable)
- `[x]` **(finals phase, verified 2026-08-30) REAL adapters over HTTP** — `backend/hardware.py`:
  `HardwareBridge` + `HttpSensorAdapter` + `HttpHVACAdapter` for the ESP32 shoebox rig
  (zone_b). Both pass the SAME `assert_conforms` gate as the Sim adapters
  (`tests/test_hardware.py`, 25 tests). Endpoints `/api/hw/reading|status|heater|log`;
  controller vent command for zone_b mirrors to the physical fan through the seam
  (`LiveSim._tick_hardware`); heater is calibration-only with a server-side 50%/10-min
  duty cap; `/api/state` gains additive `hardware` block. Twin state is NEVER overridden
  by measured air — the A/B race stays fair; real sensing is displayed and logged for
  calibration. Firmware: `hardware/firmware/feelslike_node/feelslike_node.ino`
  (boot-safe off, 10 s watchdog, single gated actuator path, WiFi-loss all-off) +
  `hardware/README.md` (wiring, 3-layer safety, bring-up). **Awaiting the physical
  node: server side fully exercised against a mock node; nothing hardware-side is
  claimed as working until the rig runs** (A5 policy).
- **Full run observed 2026-08-30 (end of day): `464 passed, 0 xfailed`; frozen numbers exact.**
- **Defect ledger cleared 2026-08-30.** The five remaining xfail families were fixed with
  GENERAL rules and their tests converted to plain regressions: (a) lexicon-claimed tokens
  can't double as another issue's prefix/typo + matched phrases claim their spans
  ("sweater"/"space heater" sarcasm now wins); (b) 'than'/'thank' joined FUZZ_STOPWORDS
  (kills the 'thand' fuzzy poison on every "hotter than X"); (c) acute distress in a named
  zone with no direction files a clarify-flagged comfort complaint instead of silence;
  (d) /api/complaint responses carry the documented short `action` code + new `action_text`
  (feed entry unchanged — dashboard contract intact; Slack handler updated to codes);
  (e) zones[].pending_constraints counted directly from the store, so an all-pending zone
  shows its queue. NLP benchmark after fixes: identical (dev 30/30, same single held-out
  failure) — no score was tuned, no benchmark case touched.

### Agent 14 — Testing
- `[x]` `tests/test_contract_conformance.py` — 49 cross-boundary contract tests, green (run seen 2026-08-30)
- `[x]` NLP unit suite (`tests/test_nlp.py`)
- `[x]` Constraint lifecycle suite (`tests/test_constraints.py`) — back-dating regressions now passing
- `[x]` Thermal / humidity suite (`tests/test_thermal.py`)
- `[x]` Controller safety + objective suite (`tests/test_controller.py`)
- `[x]` Simulator determinism / reset / isolation suite (`tests/test_simulator.py`)
- `[x]` API suite via TestClient incl. the /api/state concurrency hammer (`tests/test_api.py`)
- `[x]` Hardware bridge + real-adapter conformance suite (`tests/test_hardware.py`, 25 tests)
- `[x]` Calibration fitter suite vs synthetic ground truth (`tests/test_calibration.py`, 9 tests)
- **Full run observed 2026-08-30 (end of day): `464 passed, 0 xfailed` — defect ledger cleared, see §0**

### Agent 15 — Hackathon demo mode
- `[ ]` Scripted demo driving **real** state (extreme heat → occupancy → complaint → parse → multi-zone → constraints → arbitration → control → thermal response → explainability → what-if → analytics)
- `[ ]` START / NEXT / PREVIOUS / RESET controls, 2–4 min runtime

---

## 3 · Tier priority tracking (§24 of the brief)

| Tier | Item | Status |
|---|---|---|
| **1** | Explainable decisions | `[~]` engine done, UI pending |
| **1** | Multi-zone complaints | `[~]` parser + store done, UI pending |
| **1** | Occupancy-aware control | `[~]` |
| **1** | Better parser | `[~]` |
| **1** | What-if simulation | `[~]` engine done, UI pending |
| **2** | Digital twin controls | `[~]` backend knobs done, UI pending |
| **2** | Comfort timeline | `[~]` data done, chart pending |
| **2** | Maintenance detection | `[~]` |
| **2** | Humidity | `[~]` |
| **2** | Demo mode | `[ ]` |
| **3** | Multiple weather seeds | `[~]` |
| **3** | Confidence intervals | `[~]` |
| **3** | Reproducibility | `[~]` |
| **3** | Safety modes | `[~]` backend done, UI pending |
| **3** | Carbon optimization | `[~]` |
| **4** | Integration adapters | `[~]` |
| **4** | Privacy controls | `[~]` backend done, UI pending |
| **4** | RL comparison lab | `[ ]` (PPO trained + ablated already; lab UI not built) |
| **4** | Mobile interface | `[ ]` |
| **4** | Advanced experiment framework | `[~]` CLI + JSON done |

---

## 4 · Quality gate (§25) — nothing here is ticked without a run
*(All ticks 2026-08-30, from the observed `484 passed` suite run + live server smokes; the
named tests are the evidence.)*

### Functional
- `[x]` complaints work (`test_api` complaint round trips) · `[x]` multi-zone complaints work
  (`test_an_all_clear_clears_exactly_the_zones_it_names` et al.) · `[x]` parser confidence works
  (`test_nlp` §11 severity/confidence)
- `[x]` constraints work (`test_constraints`, exact decay/arbitration maths) · `[x]` occupancy
  affects control (`test_occupancy_setback_precool_and_occupied_fire_in_that_order`,
  crowd-term tests) · `[x]` humidity affects simulation (`test_thermal` + humidity-term
  controller tests)
- `[x]` controller works (envelope, objectives, five safety modes — `test_controller`, 40+ tests)
- `[x]` explainability works (decision log tests + `/api/decisions` suite) · `[x]` what-if does
  not modify live state (`test_no_scenario_in_the_registry_mutates_live_state`, fingerprint tests)
- `[x]` maintenance detection works (`test_api` alert paths + monitor suite) · `[x]` analytics
  work (`/api/analytics` suite) · `[x]` demo mode works (walk/again/prev-never-repeats tests)

### Technical
- `[x]` tests pass (**484 passed, 0 xfailed**) · `[x]` deterministic simulation
  (`test_the_same_seed_is_bit_identical_over_a_full_day`) · `[x]` API contracts compatible
  (49 contract tests + legacy-key assertions)
- `[x]` no broken routes (preflight --server 6/6 + API suite) · `[x]` no fake functionality
  (vent-only HVAC adapter rejects setpoints; ToD is display-only and labeled; selftest charts
  carry a baked-in banner)
- `[~]` no console errors / no duplicated logic — panels are guard()-isolated and
  `node --check` clean; a devtools-console pass on every tab is a team rehearsal item

### UX
- `[x]` team browser walk-through of all 11 tabs done (2026-08-30, team confirmed)
- `[x]` simulator clearly marked (permanent SIMBADGE header + per-payload honesty flags)
- `[x]` loading/error states (every panel ships empty-but-valid + panel-wait + fl-err paths)
- `[~]` responsive/accessible/readable/professional — built to the shell's token system with
  aria roles and roving tabindex; final judgment is the team's rehearsal call, not a checkbox
  I can tick from here

---

## 5 · Final demo scenario (§26) — end-to-end acceptance

- `[x]` Extreme heat / high occupancy / humidity settable from the UI (Twin tab sliders →
  `/api/conditions`, clamped-and-echoed; API suite covers it)
- `[x]` "The lobby and cafeteria are too hot" → **2 zones detected** with intent, severity,
  confidence (`test_api` multi-zone round trip asserts exactly this sentence's behaviour)
- `[x]` Validated → 2 constraints → arbitration → controller decision (same tests + conflict suite)
- `[x]` Both zone setpoints visibly move; thermal response simulates (zone-row offset asserts)
- `[x]` Comfort / energy / carbon deltas shown (race strip + tiles + ToD card, live-verified)
- `[x]` "Why did the system change this?" opens the real decision record (Explain panel →
  `/api/decisions/{id}`, tested)
- `[x]` "What if occupancy increases 20%?" runs and compares baseline vs scenario
  (`/api/whatif` + isolation proof, tested)
- `[x]` Recurring-issue alert for the repeatedly-complaining zone (maintenance recurring
  detector + comfort-memory pre-apply, tested)
- **The walk itself is scripted in the guided Demo tab (START/NEXT), whose runner is tested
  for never-repeat/never-invent semantics — rehearse it, then re-walk it live on stage.**

---

## 6 · Docs (deck + report)

Both are regenerated from the measured result files by `python -m scripts.update_docs`,
so a number can never be hand-typed into a slide and drift from the code.

- `[x]` `scripts/update_docs.py` — surgical pptx text/geometry editing (design preserved, 14 slides intact, backup written every run)
- `[x]` Deck slide 10 rewritten from the blind probe: honest sentence, 70-case count, `Severity` axis relabelled `Detection` (what the probe actually measures)
- `[x]` Chart **geometry** corrected — all 8 bars resized to match their printed values, common baseline verified, so no bar contradicts its own label
- `[x]` Report §8 gained a "blind probe" section explaining the tuned-vs-blind gap; 14 pages, PDF re-printed
- `[x]` **Capability gate**: `CAPABILITIES` in `update_docs.py` — multi-zone, explainability, what-if, maintenance, humidity, safety modes are all still `False`, so none of them is claimed in the deck or report yet. Flip a flag only after that subsystem is verified.
- `[ ]` Deck slides for the new subsystems (blocked on verification, not on effort)
- `[ ]` Report sections for explainability / what-if / maintenance (same gate)

## 7 · Verification findings (what checking actually turned up)

1. **The held-out NLP score was inflated.** After the parser rewrite the held-out split
   read 100% exact-triple, up from 55%. A blind probe of 20 fresh cases scored **50%**.
   The split had been developed against, so it measured "were these specific sentences
   fixed", not generalization. Response: the blind probe is now a committed artifact with
   a documented rotation rule, and it is the only NLP number the docs quote.
   **UPDATE 2026-08-30 — the rotation rule fired.** Probe v1 itself got burned on
   2026-08-17 (its failure categories were studied; its committed re-score of 80% was
   NOT blind), yet the prose kept quoting the older 50/60 — a live contradiction for
   any judge reading the repo. Resolved: v1 archived as burned with the story written
   into it, probe v2 authored fresh (same category mix, gold committed pre-measurement,
   single shot per mode), the runner now refuses burned probes, and every doc quotes
   v2: **rules 45% / LLM 55% exact triple, 90%/90% zone set** (Agent 2 table).
   Also found on the way: Groq had decommissioned `llama-3.3-70b-versatile` (404), so
   the live LLM path had been silently falling back to rules — .env now pins
   `openai/gpt-oss-120b` (verified live, ~1.9 s).
2. **Real parser wins, confirmed on unseen input:** zone extraction 95% (multi-zone and
   Hinglish included), typo tolerance, and the appliance/outdoor negatives.
3. **Real remaining gaps, found by the probe:** novel metaphor ("legit swamp" → humid),
   inverted sarcasm ("hates warm-blooded people" → too_cold), Hinglish implied heat
   ("AC chal hi nahi raha"), and two-clause contrast ("X is fine but Y is unbearable").
4. **FIXED (verified 2026-08-30) — retraction phrasing.** `detect_retraction` now handles
   the negated-discomfort family ("no longer X", "not X anymore", "stopped being X",
   "the heat is gone") via `_negated_state_re()` composed from the intent lexicon.
   Regression: `test_api.py::test_a_negated_all_clear_also_clears` passes.
5. **Two latent bugs found by the architecture agent — both now FIXED (verified 2026-08-30, 417 passed / 13 xfailed):**
   - **Race in `LiveSim.state()` — fixed.** `state()` builds the entire payload inside the
     lock and hands out copies only (`list(self.history)`, `[dict(e) for e in self.feed]`).
     Proof: `test_api.py::test_polling_state_while_the_sim_steps_never_tears_a_response`
     (120 concurrent polls vs 120 interleaved advances) passes.
   - **Corruption in `clear_zone()` — fixed.** Expiry is now the `Constraint.cleared_t`
     flag, honoured by `decay()`; `created_t` is never touched, so `ComfortMemory.patterns`
     keeps an honest timeline. Same fix applied to the second site,
     `privacy.redact_record()` (defect D2b), and `whatif.state_fingerprint` now includes
     `cleared_t`/`approved`/`rejected` so the isolation proof still catches store
     mutations. The two strict-xfail tests in `test_constraints.py` §9 are now plain
     passing regressions.
6. **Two bugs in my own docs updater, caught by inspecting output rather than trusting it:**
   a "nearest number" heuristic silently corrupted a chart bar (85 → 70), and a bar filter
   that admitted 0.01in divider rules broke the chart baseline. Both fixed; geometry is now
   asserted after every run.

## 8 · Decisions log (why things are the way they are)

1. **Extend, never rebuild.** Every existing signature is frozen; new capability arrives as additive fields and new modules.
2. **Contracts pinned before parallel work.** Nine agents ran concurrently against one written contract — the only reliable way to stop schema drift.
3. **Single-owner files for integration points.** `backend/app.py` and `dashboard/index.html` are touched by exactly one agent each, in a later phase, because they are where multi-agent collisions actually happen.
4. **No framework for the frontend.** Tabs + a second JS file keep the zero-dependency, zero-build, works-offline property that makes the demo survivable.
5. **Humidity is tracked, not billed.** Charging latent load to kWh would move the frozen headline numbers and invalidate the report; the limitation is documented instead of hidden.
6. **Held-out benchmark stays honest.** Fixes must be general rules, never memorized strings; failures are reported, not deleted.
7. **Adapters are seams, not fake clients.** Protocols + simulated implementations; no pretend BACnet code.
8. **(2026-08-30, finals phase) Hardware transport is HTTP POST, not MQTT.** The ESP32 posts
   readings to a FastAPI endpoint; the real adapters read the latest value with a staleness
   watchdog. Chosen because it adds zero pip runtime dependencies (guarantee §1 holds), has
   no broker process to die on stage, and works over a phone hotspot on hostile venue Wi-Fi.
   MQTT/BACnet are named as the production path in FEASIBILITY.md — the adapter seam makes
   transport swappable, which is the point.
9. **(2026-08-30, finals phase) Actuation is a 5V fan + small heat source via MOSFET.**
   Self-contained on stage, audible, and the heater doubles as the step input for the
   sim-to-real calibration. IR LED (real split-AC codes) is an honest stretch only —
   claimed solely if actually verified on a real AC.
10. **(2026-08-30) Constraint expiry is a flag, never a clock move.** `cleared_t` on the
    constraint; `created_t` is sacred — it is the pattern miner's only timestamp.
11. **(2026-08-30, team-approved) ToD tariff is DISPLAY ONLY.** `backend/tariff.py`
    reprices the SAME measured kWh series at the verified TANGEDCO LT-V ToD structure
    (peak 06–10/18–22 +25%, night 22–05 −5%, marginal ₹10.45) and surfaces it as the
    additive `meters.tou` block + an Analytics card + one Overview sub-line. Control
    still optimizes against the flat tariff — exploiting ToD would change kWh and
    invalidate the frozen numbers, so it stays a labeled display until re-measured.
12. **(2026-08-30) Blind-probe rotation is enforced, not aspirational.** A probe whose
    failures were studied is burned: flagged in the file, refused by the runner,
    archived, replaced. The quoted NLP number is always the newest probe's single-shot
    score — today that is v2: rules 45% / LLM 55% triple, 90%/90% zone set. A harder
    honest number beats a softer stale one.
13. **(2026-08-30, team-approved) RL is shown as a committed trajectory.** `GET /api/rl`
    reads `rl/models/progress.csv` + `evals/results_energy.json` from disk (never
    regenerates) and the Experiments tab charts the reward curve beside the measured
    four-controller table and the M4 decision sentence — the brief's RL deliverable,
    answered visually without shipping the weaker comfort number.
14. **(2026-09-15, team decision) Actuators stay on indicator LEDs for now.** The rig shows the
    fan and heater channels on LEDs (GPIO 16 / 17) — sensing, control, watchdog and duty cap are all
    verified on real hardware that way. No power stage is bought yet, so the demo presents these as
    "actuator channels verified on indicators; power stage pending", never as a working fan.
    Calibration (which needs a real heater of known wattage) waits for that purchase.
15. **(2026-09-15, team decision) L9110 power stage, fuse and toggle switch go in now, on parts in
    hand.** The L9110 is powered from the ESP32's 5 V pin through the fuse and the switch, and the
    indicator LEDs move onto its outputs — this verifies safety layer 0 (a physical kill no software
    can override) and the `DRIVER_L9110` firmware path. The 12 V fan may be tried at 5 V; it is claimed
    only if it visibly spins. The resistor heater is NOT built: only a mixed handful of resistors is
    available, so calibration still waits for a heater purchase (decision 14 stands on that point).
16. **(2026-09-15, team decision) The Arduino Uno becomes a wired, sensor-only ambient node.** An LM35
    read on the Uno's internal 1.1 V reference with 64x oversampling streams one JSON line per reading
    over USB serial, and a bridge script forwards it to the server. Two reasons: a true room-air
    reference beside the rig, and a second transport through the same seam (a wired bus, the shape of
    RS-485/Modbus sensor networks in real buildings) next to the ESP32's Wi-Fi. It never receives
    actuator commands, so it adds no actuation path. Its internal reference varies up to ±10 % chip to
    chip, so it is cross-calibrated against the DHT22 before any reading from it is used.
