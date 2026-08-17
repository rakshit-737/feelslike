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
| **C — Integration** | `backend/app.py` — endpoints, live wiring, demo mode | `[>]` Workflow 2 running |
| **D — Frontend** | dashboard tabs, new panels, occupant mobile page | `[>]` Workflow 2 running |
| **E — Tests & QA** | unit suites, adversarial QA sweep | `[>]` contract suite green; unit suites + QA in flight |
| **F — Productization** | flip verified capability flags, regenerate docs, commit | `[ ]` after E |

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

**Verified NLP numbers (blind probe, 20 unseen cases):**

| Metric | Rules | LLM (Groq) |
|---|---|---|
| Zone extraction (exact set) | **95%** | **95%** |
| Complaint detection | 60% | 70% |
| Issue extraction | 65% | 75% |
| **Exact triple** | **50%** | **60%** |

Tuned-split scores read 100% / 98% — see the integrity finding in §7 for why those are not the number to quote.

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

### Agent 14 — Testing
- `[~]` `tests/test_contract_conformance.py` — 45 cross-boundary contract tests (written by the lead, not yet run against the finished tree)
- `[ ]` NLP unit suite
- `[ ]` Constraint lifecycle suite
- `[ ]` Thermal / humidity suite
- `[ ]` Controller safety + objective suite
- `[ ]` Simulator determinism / reset / isolation suite
- `[ ]` API contract suite against a live server

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

### Functional
- `[ ]` complaints work · `[ ]` multi-zone complaints work · `[ ]` parser confidence works
- `[ ]` constraints work · `[ ]` occupancy affects control · `[ ]` humidity affects simulation
- `[ ]` controller works · `[ ]` explainability works · `[ ]` what-if does not modify live state
- `[ ]` maintenance detection works · `[ ]` analytics work · `[ ]` demo mode works

### Technical
- `[ ]` tests pass · `[ ]` deterministic simulation · `[ ]` API contracts compatible
- `[ ]` no console errors · `[ ]` no broken routes · `[ ]` no duplicated logic · `[ ]` no fake functionality

### UX
- `[ ]` responsive · `[ ]` accessible · `[ ]` readable · `[ ]` professional
- `[ ]` branding preserved · `[ ]` loading/error states · `[ ]` simulator clearly marked

---

## 5 · Final demo scenario (§26) — end-to-end acceptance

- `[ ]` Extreme heat 36 °C / high occupancy / 65% humidity settable from the UI
- `[ ]` "The lobby and cafeteria are too hot" → **2 zones detected** with intent, severity, confidence
- `[ ]` Validated → 2 constraints → arbitration → controller decision
- `[ ]` Both zone setpoints visibly move; thermal response simulates
- `[ ]` Comfort / energy / carbon deltas shown
- `[ ]` "Why did the system change this?" opens the real decision record
- `[ ]` "What if occupancy increases 20%?" runs and compares baseline vs scenario
- `[ ]` Recurring-issue alert for the repeatedly-complaining zone

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
2. **Real parser wins, confirmed on unseen input:** zone extraction 95% (multi-zone and
   Hinglish included), typo tolerance, and the appliance/outdoor negatives.
3. **Real remaining gaps, found by the probe:** novel metaphor ("legit swamp" → humid),
   inverted sarcasm ("hates warm-blooded people" → too_cold), Hinglish implied heat
   ("AC chal hi nahi raha"), and two-clause contrast ("X is fine but Y is unbearable").
4. **Open defect — retraction phrasing.** "no longer stuffy in reception" files a *new*
   complaint instead of clearing the zone. `detect_retraction` misses the "no longer X"
   form. Cheap, general fix; deferred only because `backend/parser.py` is owned by an
   in-flight agent and I will not edit a file another agent is writing.
5. **Two latent bugs found by the architecture agent reading the existing code** — both predate this work:
   - **Race in `LiveSim.state()`**: `history` and `feed` are returned *by reference* while the sim thread appends to
     one and rebinds the other. FastAPI serialises them outside the lock, so a live demo can hit
     `RuntimeError: list changed size during iteration`. Assigned to the integration agent; QA will hammer
     `/api/state` 200× to prove the fix.
   - **Corruption in `clear_zone()`**: it expires constraints by back-dating `created_t`, and `ComfortMemory.patterns`
     reads that same field — so every retracted complaint is later clustered at the wrong hour of the wrong day.
     Needs a separate cleared flag. Captured as an `xfail` test so it stays visible instead of being quietly patched.
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
