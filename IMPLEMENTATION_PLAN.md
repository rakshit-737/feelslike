# FeelsLike — Implementation Plan (extension programme)

This plan covers the **extension** work pinned in the shared contract: humidity, decision
logging, multi-objective control, what-if scenarios, maintenance inference, analytics,
adapters and privacy. It does **not** replace `IMPLEMENTATION.md`, which is the original
hackathon build plan (M0–M6) and remains the record of how the shipped system was built.

Everything already working — the RC twin, the parser, the constraint store, the comfort
memory, the A/B race, the dashboard — is **frozen and extended, never rewritten**
(hard rule 1). See `PROJECT_ARCHITECTURE.md` for what exists and `DATA_CONTRACTS.md` for
the field-level types.

---

## 0. The five gates that must never go red

Any change, by anyone, at any phase, is wrong if it breaks one of these:

| # | Gate | Command | Expected |
|---|---|---|---|
| G1 | Controller regression | `python -m scripts.demo_day --days 7 --seed 7` | ConstraintAware **530 kWh / 0 viol-min**, static 722 / 16,328, reactive 493 / 429 |
| G2 | Parser regression | `python -m evals.run_nlp_eval --rules` | 30/30 dev, 11/20 held-out exact triple |
| G3 | Contracts round-trip | `python -c "import backend.contracts as c, json; …"` | every dataclass → `to_dict` → `json.dumps` clean |
| G4 | Frozen API surface | manual diff of `/api/state` keys | keys **added** only; never renamed or removed |
| G5 | Determinism | two twins, same seed, same inputs | bit-identical floats |

G1 is the one people break. `sim/env.py:23` instantiates `ConstraintAware()` at import time
as the RL action base, so a changed default in that constructor silently changes both the
demo table *and* the RL baseline.

---

## 1. Phases

### Phase A — Foundation (must land first; everything imports it)

| Deliverable | File | Depends on | Unlocks |
|---|---|---|---|
| Canonical types, weights, safety modes, `to_dict`, `new_id` | `backend/contracts.py` | — | every other phase |
| Architecture + contracts + plan docs | `PROJECT_ARCHITECTURE.md`, `DATA_CONTRACTS.md`, `IMPLEMENTATION_PLAN.md` | — | parallel agents can start without reading all the code |
| Psychrometrics (Magnus) — ✅ landed | `sim/humidity.py` | contracts | humid-complaint realism, dew-point UI, `humid_viol_min` |
| Twin additions: `clone()`, `rh_now`, `dew_point_now`, `zone_snapshot`, `set_conditions`, `*_scale` knobs, `last_setpoints/last_vents`, new metrics keys — ✅ landed | `sim/twin.py` | `sim/humidity.py` | **what-if, maintenance, analytics — all three were blocked on `clone()`; now unblocked** |
| Store additions: `clone()`, `add_many()`, `objective`/`set_objective`, `Constraint.complaint_id/approved/anonymous` | `backend/constraints.py` | contracts | multi-zone complaints, what-if, approval flow |
| Parser additions: `zone_ids`, `requires_clarification`, `language`, `normalized_text`, `zone_confidence` | `backend/parser.py` | contracts | multi-zone routing, clarification UX, Hinglish reporting |

**Phase A exit criteria**: G1–G3 green; `DigitalTwin.clone()` proven independent by test;
`ParsedComplaint.zone_id == zone_ids[0] or None` asserted.

Risk to watch: **do not put a latent (humidity) load into the energy path in Phase A.** The
moment `step()` spends kWh on dehumidification, the published −26.6 % headline is stale.
Model RH as a tracked state first; if a latent load is added later, it must land in the same
commit as re-measured numbers in `README.md`, `PRODUCT.md` and `IMPLEMENTATION.md`.

### Phase B — Core intelligence (parallelisable once A lands)

| Deliverable | File | Depends on | Unlocks |
|---|---|---|---|
| `build_decision`, `DecisionLog` | `backend/decisions.py` (new) | contracts, twin snapshot | the explainability panel, controller stats |
| `ConstraintAware(objective=…, safety_mode=…, locked_zones=…)`, `last_decisions`, `set_objective`, `set_safety_mode`, `lock_zone`/`unlock_zone`, `pending_recommendations` | `sim/controllers.py` | decisions, store objective | objective switcher, human-approval demo, lockout |
| `SCENARIOS`, `run_scenario`, `compare` | `backend/whatif.py` (new) | `twin.clone`, `store.clone` | "what if it's 4 °C hotter" — the strongest judge-facing feature after the race |
| `MaintenanceMonitor` | `backend/maintenance.py` (new) | `zone_snapshot`, decisions | ops-credibility story |
| `AnalyticsStore` | `backend/analytics.py` (new) | both twins, store, decisions | heatmap, complaint stats |
| `scrub_pii`, `anonymize_author`, `RetentionPolicy`, `export_records`, `redact_record` | `backend/privacy.py` (new) | contracts | the privacy answer in the Q&A drill, with code behind it |
| Adapter Protocols + Sim implementations + `get_adapters()` | `backend/adapters.py` (new) | twin | the "path to production" answer |

**Phase B exit criteria**: each new module byte-compiles, has its own regression command in
its docstring, and G1 is still green (the controller changes are the risky ones).

Hard rule 7 applies most sharply here: `adapters.py` is only worth shipping if something
actually calls through it. A Protocol with one Sim implementation that nobody invokes is
architecture theatre — either wire it into the controller path or cut it.

### Phase C — Integration (single writer, serialised after B)

Only **one** agent may write `backend/app.py`. Everyone else reports what they need.

1. `LiveSim` gains: `decisions = DecisionLog()`, `monitor = MaintenanceMonitor()`,
   `analytics = AnalyticsStore()` — constructed in `__init__`, ticked inside the existing
   `while acc >= 60` body (app.py:51-59) so they stay lock-step with both twins.
2. `/api/state` gains **additive** keys only: `decisions`, `alerts`, `analytics`,
   `objective`, `safety_mode`, and the additive `ZoneRuntime` fields (`rh_pct`,
   `dew_point_c`, `occ_pct`, `at_capacity`, `conflict`, `locked`). The nine existing zone
   keys and every `meters`/`history`/`feed` key stay byte-identical (G4).
3. New endpoints: `POST /api/objective`, `POST /api/safety_mode`, `POST /api/whatif`,
   `POST /api/approve`, `POST /api/lock`, `GET /api/export`. All validate their body — copy
   the `ComplaintIn` pydantic pattern, not the unguarded `float(body.get(...))` at app.py:214.
4. Fix while in there: return **shallow copies** of `history` and `feed` from `state()`
   (defect 1 in `PROJECT_ARCHITECTURE.md` §8) and wrap the new per-tick monitors in a
   `try/except` — the sim thread has no supervisor and an exception kills the demo silently.
5. What-if must run **outside** `sim.lock`, on clones taken under it. Holding the lock
   across a multi-seed scenario freezes the building on stage, exactly like an un-timeboxed
   LLM call would.

**Phase C exit criteria**: dashboard still renders unchanged against the new payload before
any frontend work starts. That is the proof G4 held.

### Phase D — Frontend (`dashboard/index.html`, single writer)

Additive panels, no rewrite of the race or the floor plan — those are the pitch.

1. Decision drawer: click a zone → its recent `ControllerDecision`s with `summary`,
   `reason_code`, constraint chips, and the energy/comfort deltas.
2. Objective + safety-mode switcher in the header, next to the speed buttons.
3. What-if panel: pick a scenario, show baseline vs scenario with the `measured`/`predicted`
   badge rendered honestly.
4. Maintenance alerts strip, evidence strings shown verbatim.
5. Humidity on the floor plan (RH badge / dew-point on hover).

Constraints inherited from `PRODUCT.md`: single file, all CSS/JS inline, no build step, no
external requests, light theme pinned, must survive the venue having no Wi-Fi.

### Phase E — Tests and evidence

| Test | Asserts |
|---|---|
| `clone()` independence | stepping a clone 60× leaves the original's `t`, `kwh`, `T` untouched |
| Determinism | same seed → identical `metrics()` across two runs |
| A/B lock-step | `us.t == base.t` after N steps at any speed |
| Frozen payload | every key in `PROJECT_ARCHITECTURE.md` §7 present in `/api/state` |
| `zone_id` invariant | `zone_id == zone_ids[0] or None` over the whole benchmark |
| Arbitration | opposing complaints → `conflict=True`, offset strictly between the two `raw_offset`s |
| Decay | a constraint's `weight` halves in 45 sim-min and is 0 past 2 h |
| Contracts | all dataclasses json round-trip; `OBJECTIVE_WEIGHTS` rows sum to 1.0 |
| No-mutation | `run_scenario` leaves live twin and store bit-identical |
| Alert de-dup | a sustained symptom raises one alert, not one per minute |

Then refresh the evidence artefacts: `scripts/demo_day.py` → `evals/results_energy.json`,
`evals/run_nlp_eval.py` → `evals/results_nlp.json`, and only then any number in `README.md`.

---

## 2. File ownership — one writer per file, no exceptions

Hard rule 2: **never edit a file you do not own.** If you need a change elsewhere, report it.

| File | Owner | Status |
|---|---|---|
| `backend/contracts.py` | **A1 · contracts & architecture** | ✅ written |
| `PROJECT_ARCHITECTURE.md`, `DATA_CONTRACTS.md`, `IMPLEMENTATION_PLAN.md` | **A1** | ✅ written |
| `sim/humidity.py` (new), `sim/twin.py` | Physics owner | ✅ Phase A landed |
| `backend/constraints.py` | Constraints owner | Phase A |
| `backend/parser.py`, `backend/prompts.py`, `evals/*` | NLP owner | Phase A |
| `backend/decisions.py` (new), `sim/controllers.py` | Controller owner | Phase B |
| `backend/whatif.py` (new) | Scenarios owner | Phase B |
| `backend/maintenance.py` (new) | Maintenance owner | Phase B |
| `backend/analytics.py` (new) | Analytics owner | Phase B |
| `backend/adapters.py` (new), `backend/privacy.py` (new) | Platform owner | Phase B |
| `backend/app.py` | **Integration owner (sole writer)** | Phase C |
| `dashboard/index.html` | Frontend owner | Phase D |
| `tests/*` (new) | QA owner | Phase E |
| `sim/env.py`, `rl/*` | RL owner | unchanged unless the reward changes |
| `README.md`, `PRODUCT.md`, `IMPLEMENTATION.md` | Whoever re-measures the numbers, in the same commit | on demand |

Shared-file hotspots, and the rule for each:

- **`sim/twin.py`** — physics owner only. Humidity, `clone()` and `zone_snapshot` all land
  together in one pass; other agents consume, never patch.
- **`sim/controllers.py`** — controller owner only. The `.act(twin, store=None)` signature is
  frozen; new behaviour arrives via constructor kwargs with today's defaults.
- **`backend/app.py`** — integration owner only. Five modules want to add state to `LiveSim`;
  they hand over a spec, not a diff.
- **`backend/contracts.py`** — A1 only, and additive only. A field added here is a field
  every agent inherits.

---

## 3. Priority tiers

Ordered by what a judge sees and by what unblocks the most parallel work.

### Tier 1 — Blocking foundation (do these or nothing else can start)

| Item | Unlocks |
|---|---|
| `backend/contracts.py` | shared vocabulary; every other module's imports |
| `DigitalTwin.clone()` | what-if **and** maintenance **and** analytics; the single highest-leverage function in the programme |
| `ConstraintStore.clone()` | scenarios that include live complaints |
| `backend/decisions.py` + `ConstraintAware.last_decisions` | the explainability panel — the answer to "prove it isn't a random number generator" |

### Tier 2 — Headline features (what wins the demo)

| Item | Unlocks |
|---|---|
| `backend/whatif.py` + `/api/whatif` + panel | "what if it's 4 °C hotter tomorrow" — the second-best moment after the race |
| Multi-objective + safety modes | the objective switcher; the human-approval story that separates a demo from a product |
| `backend/maintenance.py` | ops credibility: the building noticing its own faults |
| Humidity through to the UI | makes `humid` and `stuffy` complaints physically real instead of a keyword |

### Tier 3 — Depth and defensibility

| Item | Unlocks |
|---|---|
| `backend/analytics.py` + heatmap | pattern evidence over a simulated week |
| `backend/privacy.py` | a coded answer to the privacy question, not a slide |
| Multi-zone complaints (`zone_ids`, `add_many`) | "both meeting rooms are freezing" in one message |
| Fixes for defects 1–6 in `PROJECT_ARCHITECTURE.md` §8 | stops a stage-time `RuntimeError` and the memory/retraction timestamp bug |

### Tier 4 — Optional, cut first if time runs short

| Item | Unlocks |
|---|---|
| `backend/adapters.py` | the production-path narrative — **only if actually wired into the control path** |
| Export / retention endpoints | data-governance completeness |
| Real weather via `fetch_openmeteo` | realism, at the cost of demo-day network dependence — keep it opt-in |
| Further RL training | may flip the M4 decision; changes nothing structural |

---

## 4. Critical path

```
contracts ──┬──► twin.clone ──┬──► whatif ──────────┐
            │                 ├──► maintenance ─────┤
            │                 └──► analytics ───────┤
            ├──► humidity ──► twin RH ──────────────┤
            ├──► store.clone / add_many ────────────┼──► app.py wiring ──► dashboard ──► tests
            ├──► parser zone_ids ───────────────────┤
            └──► decisions ──► controller objectives┘
```

`twin.clone()` and `backend/contracts.py` are the two nodes that, if late, make everything
late. Nothing in Phase B can be honestly tested without them.

---

## 5. Working agreements

1. **Extend, never rewrite.** An existing signature beats a new one, always.
2. **Additive `/api/state`.** The dashboard reads nine zone keys and six meter keys by name.
3. **Determinism or it didn't happen.** `random.Random(seed)` instances only; no module-level
   `random` or `np.random`.
4. **Clone before you experiment.** Scenario code that touches the live twin is a bug even if
   the numbers look right.
5. **Name the formula.** Any new physics gets a comment naming it (Magnus, SHR latent split).
6. **No fake knobs.** A control that does nothing is worse than no control (hard rule 7).
7. **Byte-compile and run your regression before you report.** Paste the real output, not a
   description of it (hard rule 9).
8. **No new dependencies.** Stdlib plus what is already installed.
9. **Never run** `rl.train`, simulations longer than ~30 s, `git commit`/`push`,
   `pip install`, or a foreground uvicorn server.
