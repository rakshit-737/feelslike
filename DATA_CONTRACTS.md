# FeelsLike — Data Contracts

The field-level reference for every type that crosses a module boundary.
Canonical types live in **`backend/contracts.py`** — stdlib only, no internal imports, so
it can never create a cycle. Nobody redefines these types anywhere else.

Conventions used throughout:

- **Time**: `t` is always *sim-seconds since Monday 00:00* (float). Wall-clock time appears
  only as the display string `sim_clock` (`"Wed 14:35"`).
- **Temperature**: `°C` everywhere. No Kelvin, no Fahrenheit, ever.
- **Energy**: `kWh` cumulative; `W` instantaneous; `kW` never.
- **Money / carbon**: `₹` at `TARIFF = 9.0 ₹/kWh`, `kgCO₂` at `GRID_CO2 = 0.71 kg/kWh`
  (module constants `TARIFF` and `GRID_CO2` in `sim/twin.py`). Both are flat scalars of kWh.
- **Nullability**: `setpoint = None` means *HVAC off*, not *unknown*. `zone_id = None` means
  *no zone identified* and must trigger a clarifying question, never a guess.
- **Serialisation**: `contracts.to_dict(obj)` → `json.dumps` with no custom encoder.
  Non-finite floats become `null`; sets are sorted by `repr` for determinism.

---

## 1. Literal vocabularies

| Alias | Values | Runtime tuple | Used by |
|---|---|---|---|
| `Issue` | `too_hot`, `too_cold`, `stuffy`, `humid`, `drafty`, `other` | `ISSUES` | parser, constraints, memory |
| `Objective` | `comfort`, `energy`, `cost`, `carbon`, `balanced` | `OBJECTIVES` | controller, store, UI |
| `SafetyMode` | `automatic`, `recommend_only`, `human_approval`, `emergency_override`, `maintenance_lockout` | `SAFETY_MODES` (dict → description) | controller |
| `Language` | `en`, `hinglish`, `mixed` | `LANGUAGES` | parser (`ParsedComplaint.language`) |
| `AlertKind` | `capacity`, `sensor`, `actuator`, `recurring` | `ALERT_KINDS` | maintenance |
| `AlertSeverity` | `low`, `medium`, `high` | `ALERT_SEVERITIES` | maintenance |
| `ResultKind` | `measured`, `predicted` | `RESULT_KINDS` | what-if |

`Literal` is erased at runtime, which is why each alias ships with a tuple for validation.

### `OBJECTIVE_WEIGHTS`

| Objective | comfort | energy | Note |
|---|---|---|---|
| `balanced` | 0.70 | 0.30 | Default. What ships. |
| `comfort` | 0.95 | 0.05 | Occupants win almost every tie. |
| `energy` | 0.25 | 0.75 | Minimise kWh; comfort is a soft floor. |
| `cost` | 0.35 | 0.65 | Identical control law to `carbon` — see below. |
| `carbon` | 0.35 | 0.65 | Identical control law to `cost` — see below. |

**Why `cost` and `carbon` share weights:** the sim's tariff and grid factor are both *flat*
scalars of kWh, so minimising rupees and minimising kilograms are the same optimisation
problem — only the *reporting unit* differs (₹ vs kgCO₂). Giving them different weights
would be fake precision. They are separate objectives so that a future time-of-use tariff or
a carbon-intensity curve has an obvious place to land; those two rows are the only thing
that must change when it does. All rows sum to `1.0` (asserted in the A1 regression).

Use `objective_weights(o)` rather than indexing: it returns a copy and falls back to
`balanced` for an unknown string, because a typo in a UI dropdown must not kill the controller.

### `SAFETY_MODES`

| Mode | Meaning for a controller about to write a setpoint |
|---|---|
| `automatic` | Applies immediately. Default; today's behaviour. |
| `recommend_only` | Decision computed and logged with `applied=False`; twin untouched. |
| `human_approval` | Queued in `pending_recommendations`; applied only after approval. |
| `emergency_override` | Occupant constraints ignored; every zone driven to the safe band. |
| `maintenance_lockout` | Locked zones frozen at base schedule; their constraints are recorded but not applied. |

### `REASON_CODES`

Stable machine-readable vocabulary for `ControllerDecision.reason_code`. UI copy may change;
these keys may not: `occupied_base`, `precool`, `setback`, `constraint_applied`,
`conflict_compromise`, `clamped`, `at_capacity`, `locked`, `recommend_only`,
`awaiting_approval`, `emergency`.

### `ARBITRATION_MODES`

`none`, `single`, `weighted_mean`, `conflict_weighted_mean`, `ignored_emergency`,
`ignored_locked`.

---

## 2. `ConstraintView`

Read-only projection of one live `Constraint` at an instant. A value object: mutating it
changes no control.

| Field | Type | Units | Meaning | Example |
|---|---|---|---|---|
| `id` | int | — | `Constraint.id`, monotonic counter | `7` |
| `issue` | str (`Issue`) | — | What the occupant reported | `"too_hot"` |
| `severity` | int | 1–3 | 1 mild, 2 clear discomfort, 3 urgent | `2` |
| `confidence` | float | 0–1 | Parser's confidence in the extraction | `0.9` |
| `weight` | float | — | `severity × confidence × decay(t)` right now | `1.44` |
| `age_min` | float | sim-min | Since the complaint was filed | `12.0` |
| `expires_in_min` | float | sim-min | Until `EXPIRY_S`; `0.0` = already expired | `108.0` |
| `raw_offset` | float | °C | Setpoint shift this issue asks for | `-1.3` |
| `author` | str | — | Occupant handle, `"comfort-memory"`, or an anonymised token | `"rakshit"` |
| `text` | str | — | Original complaint (may be PII-scrubbed for display) | `"conf room is a sauna"` |

---

## 3. `ControllerDecision`

One `act()` outcome for **one zone**. A 5-zone `act()` produces five of these. This is the
audit record behind the explainability panel.

| Field | Type | Units | Meaning | Example |
|---|---|---|---|---|
| `t` | float | sim-s | When the decision was taken | `52200.0` |
| `sim_clock` | str | — | Display clock, same format as `/api/state` | `"Mon 14:30"` |
| `zone` | str | — | `zone_id` | `"zone_b"` |
| `zone_name` | str | — | Display name | `"Conference Room B"` |
| `applied` | bool | — | `False` = recommendation only, twin untouched | `true` |
| `prev_setpoint` | float\|None | °C | Before this act; `None` = HVAC was off | `25.0` |
| `new_setpoint` | float\|None | °C | Written (or proposed); `None` = off | `24.4` |
| `base_setpoint` | float\|None | °C | What the schedule alone would have chosen | `25.0` |
| `offset_c` | float | °C | `new_setpoint − base_setpoint` | `-0.6` |
| `prev_vent` | int | 0/1/2 | Fan level before | `1` |
| `new_vent` | int | 0/1/2 | Fan level after | `2` |
| `occupancy` | float | people | Right now | `12` |
| `occupancy_pct` | float | % | Of that zone's peak occupancy | `100.0` |
| `outdoor_c` | float | °C | Dry-bulb outdoors | `35.8` |
| `indoor_c` | float | °C | Zone air temperature | `26.9` |
| `rh_pct` | float | % | Zone relative humidity | `58.2` |
| `constraints` | list[`ConstraintView`] | — | Everything that fed this decision | `[…]` |
| `conflict` | bool | — | Opposing constraints were arbitrated | `true` |
| `arbitration` | str | — | One of `ARBITRATION_MODES` | `"conflict_weighted_mean"` |
| `objective` | str (`Objective`) | — | In force at decision time | `"balanced"` |
| `safety_mode` | str (`SafetyMode`) | — | In force at decision time | `"automatic"` |
| `est_energy_delta_pct` | float | % | vs base schedule; **+ = more energy** | `4.1` |
| `est_comfort_delta_pct` | float | % | vs base schedule; **+ = more comfortable** | `11.7` |
| `summary` | str | — | One human sentence, safe to show a judge | `"CONFLICT in Conference Room B: … compromise −0.6 °C"` |
| `reason_code` | str | — | Key into `REASON_CODES` | `"conflict_compromise"` |
| `id` | str | — | `new_id("dec", n)`; empty until logged | `"dec-0007"` |

Sign conventions are load-bearing: a negative `offset_c` means *colder setpoint*, and a
positive `est_energy_delta_pct` means the decision **costs** energy. Do not flip either.

---

## 4. `MaintenanceAlert`

| Field | Type | Units | Meaning | Example |
|---|---|---|---|---|
| `id` | str | — | `new_id("mnt", n)` | `"mnt-0003"` |
| `kind` | str (`AlertKind`) | — | Fault family | `"capacity"` |
| `zone` | str | — | `zone_id` | `"zone_e"` |
| `zone_name` | str | — | Display name | `"Cafeteria E"` |
| `severity` | str (`AlertSeverity`) | — | Operator triage level | `"high"` |
| `confidence` | float | 0–1 | How sure the heuristic is | `0.82` |
| `evidence` | list[str] | — | Observed facts, each with a number in it | `["at capacity 47 of last 60 min", …]` |
| `recommendation` | str | — | What a technician should do | `"Inspect Cafeteria E AHU…"` |
| `first_seen_t` | float | sim-s | Symptom first appeared | `45000.0` |
| `last_seen_t` | float | sim-s | Most recent observation | `48600.0` |

Alerts are advisory: they never change control on their own.

---

## 5. `ScenarioSpec` and `ScenarioResult`

### `ScenarioSpec`

| Field | Type | Units | Meaning | Example |
|---|---|---|---|---|
| `name` | str | — | Human label | `"Heat wave +4 °C"` |
| `kind` | str | — | Scenario family key into `whatif.SCENARIOS` | `"heatwave"` |
| `params` | dict | — | Knob → value, scenario-specific | `{"outdoor_offset": 4.0}` |
| `horizon_h` | float | sim-hours | How long to run | `8.0` |
| `seeds` | list[int] | — | Weather seeds; `[]` = use the live twin's seed | `[7, 8, 9]` |

### `ScenarioResult`

| Field | Type | Units | Meaning | Example |
|---|---|---|---|---|
| `name` | str | — | Echoes the spec | `"Heat wave +4 °C"` |
| `kind` | str (`ResultKind`) | — | `measured` = clones were actually stepped; `predicted` = closed-form estimate | `"measured"` |
| `metrics` | dict | mixed | Headline metrics, `twin.metrics()` shape | `{"kwh": 96.3, "viol_min": 0.0}` |
| `per_seed` | list[dict] | mixed | One metrics dict per seed; `[]` for single-run | `[{…}, {…}]` |
| `mean` | dict | mixed | Metric → mean across seeds | `{"kwh": 96.3}` |
| `sd` | dict | mixed | Metric → sample stdev (`0.0` when n < 2) | `{"kwh": 2.1}` |
| `ci95` | dict | mixed | Metric → **half-width** of the 95 % interval | `{"kwh": 2.6}` |

`kind` is an honesty flag. Labelling an analytic estimate as `measured` would undermine the
one thing this project sells — that the numbers came out of a simulation that really ran.
`mean`, `sd` and `ci95` must share a key set.

---

## 6. `ZoneRuntime`

Everything the UI needs about one zone at one instant. **The first nine field names are
byte-identical to the zone dict `/api/state` already returns**, so `to_dict(ZoneRuntime)` is
a drop-in superset of today's payload. Additive fields only, forever.

| Field | Type | Units | Meaning | Example | Frozen? |
|---|---|---|---|---|---|
| `id` | str | — | `zone_id` | `"zone_b"` | ✅ |
| `name` | str | — | Display name | `"Conference Room B"` | ✅ |
| `temp` | float | °C | FeelsLike twin | `24.9` | ✅ |
| `base_temp` | float | °C | Baseline twin (A/B partner) | `22.1` | ✅ |
| `setpoint` | float\|None | °C | `None` = HVAC off | `24.4` | ✅ |
| `vent` | int | 0/1/2 | Fan level | `2` | ✅ |
| `occ` | int | people | Rounded | `12` | ✅ |
| `offset` | float | °C | Applied by active constraints | `-0.6` | ✅ |
| `active_constraints` | int | count | Non-decayed constraints | `2` | ✅ |
| `occ_pct` | float | % | Of that zone's peak occupancy | `100.0` | additive |
| `rh_pct` | float | % | Relative humidity | `58.2` | additive |
| `dew_point_c` | float | °C | Magnus dew point | `16.1` | additive |
| `capacity_w` | float | W | `Zone.max_cool` thermal capacity | `7000.0` | additive |
| `at_capacity` | bool | — | Unit saturated this step | `false` | additive |
| `conflict` | bool | — | Opposing constraints active | `true` | additive |
| `locked` | bool | — | Maintenance lockout in force | `false` | additive |

---

## 7. Types owned elsewhere (referenced, not redefined)

### `ParsedComplaint` — owned by `backend/parser.py` (pydantic v2)

| Field | Type | Meaning | Status |
|---|---|---|---|
| `is_comfort_complaint` | bool | Thermal/air-quality complaint? | existing |
| `zone_id` | str\|None | **Back-compat field. Never removed.** Equals `zone_ids[0]` or `None` | existing |
| `issue` | str (`Issue`) | Extracted issue | existing |
| `severity` | int 1–3 | Validated by `Field(ge=1, le=3)` | existing |
| `confidence` | float 0–1 | Validated by `Field(ge=0, le=1)` | existing |
| `reasoning` | str | One short sentence | existing |
| `zone_ids` | list[str] | All zones named; multi-zone complaints | **pinned, new** |
| `requires_clarification` | bool | Ask instead of act | **pinned, new** |
| `language` | str (`Language`) | `en` / `hinglish` / `mixed` | **pinned, new** |
| `normalized_text` | str | Lower-cased, de-typo'd text used for matching | **pinned, new** |
| `zone_confidence` | dict[str,float] | zone_id → 0–1 | **pinned, new** |

`clean()` (parser.py:37) is the guardrail: a `zone_id` outside `VALID_ZONES` is nulled
(never act on a hallucinated zone) and an unknown `issue` collapses to `other`.

**Invariant, asserted by test:** `zone_id == (zone_ids[0] if zone_ids else None)`. Five call
sites read `zone_id` (app.py:136, 141, 149, 155, 194) and two dashboard sites
(index.html:334, 408). Breaking it breaks the Slack reply and the conflict outline.

### `Constraint` — owned by `backend/constraints.py`

| Field | Type | Units | Meaning |
|---|---|---|---|
| `id` | int | — | From a module-level `itertools.count(1)` (constraints.py:24) |
| `zone` | str | — | `zone_id` |
| `issue` | str (`Issue`) | — | — |
| `severity` | int | 1–3 | Clamped in `from_issue` |
| `confidence` | float | 0–1 | From the parser |
| `created_t` | float | sim-s | Filing time |
| `raw_offset` | float | °C | From `ISSUE_EFFECTS[issue][0][severity]` |
| `vent_delta` | int | −1/0/+1 | From `ISSUE_EFFECTS[issue][1]` |
| `text` | str | — | Original complaint |
| `author` | str | — | Default `"anonymous"` |
| `complaint_id` | str | — | **pinned, new** — links constraints created from one message |
| `approved` | bool | — | **pinned, new** — `False` while awaiting human approval |
| `anonymous` | bool | — | **pinned, new** — author was anonymised |

`ISSUE_EFFECTS` (constraints.py:16-22), °C per severity {1,2,3} and vent delta:

| Issue | sev 1 | sev 2 | sev 3 | vent |
|---|---|---|---|---|
| `too_hot` | −0.8 | −1.3 | −1.8 | 0 |
| `too_cold` | +0.8 | +1.3 | +1.8 | 0 |
| `stuffy` | −0.3 | −0.4 | −0.5 | +1 |
| `humid` | −0.4 | −0.5 | −0.6 | +1 |
| `drafty` | +0.3 | +0.4 | +0.5 | −1 |

Decay: `0.5 ** (age / HALF_LIFE_S)` with `HALF_LIFE_S = 2700` (45 min), hard zero past
`EXPIRY_S = 7200` (2 h). `weight = severity × confidence × decay`. `active()` keeps anything
with `decay > 0.02` (≈ 2.8 h of half-lives, so `EXPIRY_S` is the binding cutoff).

### `zone_adjustments()` return — the controller's actual input

`dict[zone_id] -> {"setpoint_offset": float °C, "vent_delta": int −1..+1, "n": int}`.
Weighted mean over active constraints; `vent_delta` sums only constraints with
`weight > 0.25` then clamps to ±1. Zones with `wsum <= 0` are omitted entirely — callers must
use `.get(zone_id)` and handle `None`.

### Feed entry — the `/api/state` `feed[]` element

`{author, text, source ("llm"|"rules"|"memory"), latency_ms, parsed{ParsedComplaint},
action (str), sim_clock, explanation? {zone, conflict, adjustment, summary}}`.
`action` is a **long human string** (`"all-clear — 2 constraint(s) cleared in zone_b"`);
the dashboard badges on `action.split(' ')[0]` (index.html:328) and on
`explanation.conflict`. Prefixes in use: `applied`, `clarify`, `ignored`, `all-clear`,
`noted`, `pre-applied`.

---

## 8. Subsystem boundaries

Each block below is the contract another agent codes against.

### 8.1 Parser — `backend/parser.py`

- **INPUT**: `parse(text: str, force_rules: bool = False)`. Free-form occupant text, any
  language, arbitrary length. Also `rules_parse(text)` and `detect_retraction(text)`.
- **OUTPUT**: `(ParsedComplaint, source: "llm"|"rules", latency_ms: int)`. Always a valid,
  `clean()`ed object — never `None`, never a raw dict.
- **SIDE EFFECTS**: one outbound HTTPS call when a key is present (15 s timeout). Reads
  `.env` at import (parser.py:16-20). No writes, no global mutation.
- **ERROR STATES**: any provider exception, malformed JSON, or schema violation falls back
  to `rules_parse` and reports `source="rules"`. An unknown `zone_id` is nulled by `clean()`.
  Note: a failure on the first provider **breaks** the loop (parser.py:174) rather than
  trying the second.
- **DEPENDENCIES**: `backend.prompts`, `sim.twin.ZONES`, `httpx`, `pydantic`, `dotenv`.
- **TESTS**: `python -m evals.run_nlp_eval --rules` — 50 cases, dev/held-out split.
  Current honest numbers: rules 30/30 dev, 11/20 held-out exact triple.

### 8.2 Constraint store — `backend/constraints.py`

- **INPUT**: `Constraint.from_issue(zone, issue, severity, confidence, now_t, text, author)`
  then `store.add(c)`. All time arguments are sim-seconds. Pinned additions: `add_many(...)`,
  `clone()`, `set_objective(o)`.
- **OUTPUT**: `add()` returns the `explain()` dict. `active(t, zone=None)` → list.
  `zone_adjustments(t)` → the controller's offset dict. `explain(zone, t)` →
  `{zone, conflict, adjustment, summary}`. `unmet_pressure(twin, minutes)` → float (RL reward).
- **SIDE EFFECTS**: `add` appends to `items` (unbounded). `clear_zone` **rewrites
  `created_t` into the past** — see the caveat below.
- **ERROR STATES**: an unknown `issue` yields zero offsets via `ISSUE_EFFECTS.get` default.
  A zone with no active constraints is absent from `zone_adjustments` — not present-with-zero.
- **CAVEAT (known defect)**: `clear_zone` (constraints.py:67-76) back-dates `created_t`, which
  corrupts the timestamp `ComfortMemory.patterns` reads (memory.py:32). A retracted complaint
  will later be clustered at the wrong hour. A separate `cleared_t` flag is the correct fix;
  the writer of `clone()`/`add_many` should not entrench the back-dating.
- **DEPENDENCIES**: none (stdlib only).
- **TESTS**: post two opposing complaints to one zone and assert `explain()["conflict"]` is
  `True` and the weighted offset sits between the two `raw_offset`s.

### 8.3 Controller — `sim/controllers.py`

- **INPUT**: `controller.act(twin, store=None) -> (setpoints, vents)`. **This signature is
  frozen** — callers are `backend/app.py:52,54`, `scripts/demo_day.py:21`, `sim/env.py:38`,
  `rl/evaluate.py`. `store=None` must keep working (baselines are called without one).
- **OUTPUT**: `setpoints: dict[zone_id] -> float °C | None` (None = HVAC off) and
  `vents: dict[zone_id] -> 0|1|2`. Every zone id must be present in both dicts.
- **SIDE EFFECTS**: none today — `act()` is pure w.r.t. the twin. The pinned
  `last_decisions` list makes it stateful *on the controller object only*; it must never
  mutate the twin or the store.
- **ERROR STATES**: setpoints are clamped to `[21.5, 29.0]` °C and vents to `[0, 2]`
  (controllers.py:68-69). A zone missing from `zone_adjustments` is simply unadjusted.
- **DEPENDENCIES**: `sim.twin.ZONES`; `RLPolicy` additionally lazy-imports `sim.env`
  **inside** `act()` (controllers.py:86) — keep it there or you create a circular import.
- **TESTS**: `python -m scripts.demo_day --days 7 --seed 7` must still print
  **530 kWh / 0 viol-min** for `ConstraintAware` after any change. That is the regression gate.

### 8.4 Twin — `sim/twin.py`

- **INPUT**: `DigitalTwin(seed=0, start_temp=28.0, weather_fn=None)`;
  `step(setpoints, vents, dt=60.0)`. Landed additions (verified present): `clone()`,
  `rh_now`, `dew_point_now`, `zone_snapshot`, `set_conditions`, the four knobs
  `occ_scale` / `capacity_scale` / `solar_scale` / `outdoor_offset` (defaults
  `1.0, 1.0, 1.0, 0.0`), and `last_setpoints` / `last_vents`.
- **OUTPUT**: `step` → `{t, t_out, power_w}`. `metrics()` → `{kwh, cost_rs, co2_kg, viol_min,
  hot_deg_min, cold_deg_min, humid_viol_min, mean_rh, at_capacity_min}` — **all rounded**,
  so use the raw attributes for arithmetic and `metrics()` for display only.
- **SIDE EFFECTS**: mutates `T`, `t`, `kwh`, `kwh_by_zone`, `viol_min`, `hot_deg_min`,
  `cold_deg_min`, `last_power_w`. Counters are monotonic and never reset.
- **ERROR STATES**: a missing zone in `setpoints` is read as `None` (HVAC off) via `.get`; a
  missing vent reads as `0`. `FAN_W[vent]` **will KeyError** on a vent outside `{0,1,2}` —
  clamp before calling.
- **DEPENDENCIES**: `sim.weather` only.
- **INVARIANT**: two twins constructed with the same seed and stepped with the same inputs
  must produce identical floats. Any new randomness must come from `random.Random(seed)`.
- **TESTS**: after `clone()`, step the clone 60 times and assert the original's `t`, `kwh`
  and every `T[zone]` are unchanged.

### 8.5 What-if — `backend/whatif.py` (not built)

- **INPUT**: `run_scenario(twin, store, spec: ScenarioSpec, seeds=None)`;
  `compare(twin, store, spec)`. `SCENARIOS: dict[key] -> {label, kind, params, help}`.
- **OUTPUT**: `ScenarioResult`; `compare` → `{"baseline": ScenarioResult,
  "scenario": ScenarioResult, "delta": dict}` where `delta` uses the same metric keys.
- **SIDE EFFECTS**: **none on the inputs — clone first, always** (hard rule 6). CPU-bound;
  runs in the request thread and will stutter the live sim if the horizon is large.
- **ERROR STATES**: `KeyError` for an unknown scenario key or an unrecognised param;
  `ValueError` for `horizon_h <= 0` or an empty seed list.
- **DEPENDENCIES**: `DigitalTwin.clone()`, `ConstraintStore.clone()`, `backend.contracts`.
- **TESTS**: run a scenario, then assert the live twin's `t`, `kwh` and `T` and the live
  store's `len(items)` are all bit-identical to before the call.

### 8.6 Maintenance — `backend/maintenance.py` (not built)

- **INPUT**: `MaintenanceMonitor.tick(twin, store, decisions=None)`, called on the sim loop.
- **OUTPUT**: list of `MaintenanceAlert` dicts (new/updated this tick); `alerts()` returns all.
- **SIDE EFFECTS**: accumulates internal per-zone symptom counters. Must never write a
  setpoint or touch the store.
- **ERROR STATES**: none may propagate — an exception here would kill the sim thread, which
  has no supervisor (app.py:43 starts it with no restart logic). Wrap the body defensively.
- **DEPENDENCIES**: `twin.zone_snapshot`, `at_capacity`, `backend.contracts`.
- **TESTS**: a zone pinned above setpoint at capacity for N consecutive minutes raises
  exactly one `capacity` alert, not one per minute; normal lunch-hour saturation in the
  cafeteria does **not** raise one.

### 8.7 Analytics — `backend/analytics.py` (not built)

- **INPUT**: `sample(twin, base_twin, store)` on a fixed sim-time cadence;
  `complaint_stats(feed)`, `controller_stats(decisions)`.
- **OUTPUT**: `comfort_heatmap()` (zone × hour buckets), `energy_series()`,
  `summary()` — all json-safe via `to_dict`.
- **SIDE EFFECTS**: appends to fixed-size ring buffers. Size them in sim-days and say so.
- **ERROR STATES**: an empty buffer must return empty structures, never `None` and never a
  divide-by-zero (the UI renders whatever it gets).
- **DEPENDENCIES**: both twins, the store, `backend.contracts`.
- **TESTS**: only aggregate buckets that were actually sampled — assert an unsampled hour is
  absent rather than zero-filled, so the heatmap cannot imply data that does not exist.

---

## 9. Helpers in `backend/contracts.py`

| Helper | Signature | Contract |
|---|---|---|
| `to_dict` | `to_dict(obj) -> Any` | Recursive, json-safe. Dataclasses, pydantic models (`model_dump`), dicts, lists/tuples/sets, scalars. Non-finite floats → `None`; sets sorted by `repr`; unknown objects → `str(obj)`. **Never raises** — it is the last line of defence before the wire. |
| `new_id` | `new_id(prefix, counter: int \| Iterator[int]) -> str` | `"dec-0007"`. Advances the iterator when given one. `TypeError` for anything else. Deterministic by design: **no uuid4**, so replays reproduce. |
| `id_counter` | `id_counter(start=1) -> Iterator[int]` | Fresh `itertools.count`. |
| `objective_weights` | `objective_weights(o) -> dict` | Copy of the weights; unknown objective silently falls back to `balanced`. |

### Regression for this module

```bash
.venv/Scripts/python -c "import backend.contracts as c, json; from dataclasses import asdict; print(sorted(n for n in dir(c) if not n.startswith('_')))"
```

Plus: every dataclass default-constructs and survives `json.dumps(to_dict(x))`; all
`OBJECTIVE_WEIGHTS` rows sum to `1.0`; `ZoneRuntime` contains all nine frozen zone keys;
`new_id` yields `cmp-0001, cmp-0002, cmp-0003` from a fresh counter.
