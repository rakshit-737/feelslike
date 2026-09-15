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
