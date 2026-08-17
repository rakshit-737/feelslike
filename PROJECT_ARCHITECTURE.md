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

## 6. External interfaces

| Interface | Direction | Contract | Failure behaviour |
|---|---|---|---|
| `GET /` | in | Serves `dashboard/index.html` via `FileResponse` | 500 if the file is missing |
| `GET /api/state` | in | The frozen state payload (§7) | — |
| `POST /api/complaint` | in | `{text, author}` JSON → action dict | Pydantic 422 on a missing `text` |
| `POST /api/slack` | in | Slack form-encoded *or* Teams JSON; replies `{response_type, text}` in < 3 s | Empty text → ephemeral help message |
| `POST /api/speed` | in | `{speed}` clamped to 1–3600 sim-s per real-s | Non-numeric → 500 (unguarded `float()`) |
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

1. **Feed / history handed out under a lock, serialised outside it.** `state()` returns
   `self.history` and `self.feed` *by reference* (app.py:104-105). The sim thread then
   calls `history.append(...)` (app.py:61) while FastAPI's JSON encoder may be walking that
   same list, and `add_feed` rebinds `self.feed` (app.py:69). At 1 Hz polling this is rare,
   but it is a genuine `RuntimeError: list changed size during iteration` risk on stage.
   Fix is one word: return shallow copies.
2. **`ConstraintStore.items` never shrinks.** `active()` filters by decay (constraints.py:63)
   but nothing prunes; `zone_adjustments` and `ComfortMemory.patterns` both walk the full
   list every simulated minute. At 960× over a long session this becomes O(n) work per
   step with n growing all session.
3. **`clear_zone` rewrites `created_t` into the past** (constraints.py:74) to expire a
   constraint. That mutation corrupts the constraint's own history: `ComfortMemory.patterns`
   later reads `created_t` (memory.py:32) and will cluster the retracted complaint at the
   *wrong hour of the wrong day*. A separate `cleared_t` / `active` flag is the correct fix.
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
