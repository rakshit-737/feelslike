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
| `cleared_t` | float\|None | sim-s | **new (D2 fix)** — set by an all-clear (`clear_zone`) or redaction (`privacy.redact_record`); `decay()` returns `0.0` when set. `created_t` is NEVER moved — it is the pattern miner's only timestamp |

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
- **SIDE EFFECTS**: `add` appends to `items` (unbounded). `clear_zone` sets `cleared_t`
  on the zone's active constraints; `created_t` is never modified.
- **ERROR STATES**: an unknown `issue` yields zero offsets via `ISSUE_EFFECTS.get` default.
  A zone with no active constraints is absent from `zone_adjustments` — not present-with-zero.
- **INVARIANT (D2 fixed 2026-08-30)**: expiry is the `cleared_t` flag, never a move of
  `created_t` — `ComfortMemory.patterns` (memory.py:32) clusters on `created_t`, so a
  clock move rewrites history. Regressions: `test_constraints.py` §9 (both the
  `clear_zone` and the `privacy.redact_record` sites).
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

---

## 10. Building contracts — `backend/building.py` (Phase 1, commercial dashboard)

Owned by `backend/building.py`; served by `/api/building*` (see `docs/BUILDING_PROFILES.md`).
**No duplicate source of truth:** every live value is read from the twin, the telemetry
store, the controller or the maintenance monitor and re-projected; the only new state is
`LiveSim.building` (configuration). Operating mode is a named preset over the controller's
existing `objective` / `safety_mode`, never a second copy of them.

### 10.1 Source tags (shared vocabulary with `backend/telemetry.py`)

Every value object is `{value, source, unit, ...extra}`. `source` ∈ `sim` (UI: SIMULATED),
`derived`, `predicted`, `hardware`, `real`, `historical`, plus two used only here:
`config` (UI: CONFIGURED — an operator-set profile value) and `none` (UI: NOT MODELLED —
no source exists; `value` is `null`). `predicted` from this module carries
`basis: "profile_schedule"` to distinguish a planning expectation from `/api/forecast`.

### 10.2 `BuildingConfig` (dataclass) — Building

| Field | Type | Units | Range (validated) | Meaning |
|---|---|---|---|---|
| `building_type` | str | — | `office, mall, hospital, hotel, college, data_center` | Profile key |
| `name` | str | — | 1–80 chars | Display name |
| `floors` | int | — | 1–200 | Configured floors |
| `zones` | int | — | 5–5000, ≥ floors | Configured zones (≥ the 5 the twin models) |
| `occupancy_capacity` | int | people | 1–1 000 000 | Design occupancy |
| `hvac_capacity_w` | float | W | 1e3–1e8 | Design HVAC capacity (configuration only) |
| `open_hour`, `close_hour` | float | h | 0–24, close > open | Operating hours (0–24 = 24 h) |
| `open_days` | list[int] | 0=Mon | non-empty, distinct, 0–6 | Operating days |
| `comfort_min_c`, `comfort_max_c` | float | °C | 16–30 / 16–32, width ≥ 1 | Comfort temperature range |
| `humidity_min_pct`, `humidity_max_pct` | float | % | 10–80 / 20–90, width ≥ 10 | Humidity comfort range |
| `co2_max_ppm` | float | ppm | 600–5000 | CO₂ comfort threshold (critical at ×1.5) |
| `comfort_priority`, `energy_priority` | float | 0–100 | — | Drive the `normal`/`simulation` objective |
| `occupancy_sensitivity` | float | 0–1 | — | How strongly expected HVAC follows people |
| `base_load_fraction` | float | 0–1 | — | Expected HVAC fraction with nobody in |
| `expected_occupancy_pct` | float | % | 0–150 | Scales the schedule |
| `oa_per_person_ls`, `oa_per_area_ls_m2` | float | L/s, L/s·m² | 0–30, 0–5 | Outside-air requirement (ASHRAE 62.1 Rp, Ra) |
| `lighting_w_m2` | float | W/m² | 0–40 | Lighting power density (estimate only) |
| `operating_mode` | str | — | `OPERATING_MODES` keys | See 10.6 |
| `schedule` | list[float] ×24 | fraction | 0–1 | Expected occupancy per hour, open day |
| `closed_factor` | float | — | — | Multiplier outside operating hours |
| `zone_roles`, `zone_floor` | dict[zone_id] | — | all 5 zone ids | Role label and floor per twin zone |
| `model_fit_note`, `basis` | str | — | — | Honesty notes |

`apply_update(cfg, changes) -> (cfg, errors)`: a different `building_type` resets to that
profile's defaults then applies the other fields; same type edits in place; unknown keys and
any validation error return the ORIGINAL config plus every error. Never mutates `cfg`.

### 10.3 Topology — Floor

`topology() -> {name, type, configured_floors, configured_zones, modelled_zones, note,
floors: [{floor: int, label, modelled: bool, zones: [zone_id], status}]}`. `status` ∈
`normal|warning|critical` (worst zone) or `not_modelled`. `floor` of a zone is clamped to
`floors`, so every twin zone is always placed.

### 10.4 Zone card — Zone, Occupancy, Environment, HVAC, Energy, Comfort, Alert

| Field | Shape | Source |
|---|---|---|
| `id`, `name`, `role`, `floor` | str/int | twin + config |
| `temp`, `humidity` | value object °C / % | `sim` |
| `occupancy` | value object people, `pct` of zone design peak | `sim` |
| `co2` | value object ppm, `estimated: true` | `derived` |
| `comfort_score` | value object /100 vs profile ranges | `derived` |
| `hvac` | `{setpoint, vent, cool_w, capacity_pct, at_capacity, locked_out, active, source}` | `sim`/`derived` |
| `energy` | `{power_w: value W (derived), kwh: value kWh (sim)}` | — |
| `demand` | 10.5, zone scope | — |
| `flags` | list ⊆ `comfortable, warm, cold, high_co2, high_occupancy, hvac_active, warning, critical` | derived |
| `status` | one of `ZONE_STATUSES` (adds `unoccupied`) | derived |
| `severity` | `normal|warning|critical` | derived |
| `alerts`, `active_constraints`, `conflict` | int/bool | monitor / store |
| `hardware?` | `{connected, temp_c, rh_pct, source:"hardware", note}` — only for the rig's zone | `hardware` |

Severity rules: warning when occupied and outside the comfort range, CO₂ over threshold,
occupied-and-warm at capacity, or a medium/high maintenance alert; critical when occupied
and > 2 °C outside the range, CO₂ > 1.5 × threshold, or a high maintenance alert.

### 10.5 Demand

Zone and building objects share keys (value objects): `current_occupancy`,
`expected_occupancy`, `hvac_demand`, `expected_hvac_demand`, `cooling_demand`,
`heating_demand` (`source: none`), `ventilation_demand` (+`supplied`, `met`),
`lighting_demand`, `energy_demand`, `comfort_demand`, plus `level` / `expected_level` ∈
`low|medium|high|very_high`. Building adds `peak_demand` (+`t`), `is_open`, `scope`.

`demand_series()` point: `{t, day, hour, future, actual_power_w, peak_power_w,
actual_occ_pct, actual_cool_w, expected_occ_pct, expected_hvac_w, expected_level, is_open,
samples}`. `actual_*` is `null` when `samples == 0` (never zero-filled).

### 10.6 Operating mode

`mode_levers(cfg) -> (objective, safety_mode)`; `objective ∈ OBJECTIVES`,
`safety_mode ∈ SAFETY_MODES` (asserted by test). `/api/building → operating_mode` adds
`in_sync`, `controller_objective`, `controller_safety_mode`.

### 10.7 KPIs and Telemetry

`kpis() -> {t, order: [key], items: {key: {label, value, unit, source, status, note?, ...}},
thresholds}` with keys `current_energy, today_energy, hvac_load, occupancy,
avg_temperature, avg_humidity, avg_co2, comfort_score, energy_saving, peak_demand,
active_alerts (+breakdown), system_health (+checks)`; `status ∈ normal|warning|critical|unknown`.
Telemetry input is the existing `TelemetryStore` row (§ Monitor tab); the only addition to
that module is the read-only `rows_between(t_from, t_to)`.

### 10.8a Twin hooks used by Phase 2

`DigitalTwin(..., rh_fn, solar_fn, occupancy_fn, gain_fn, capacity_w, heat_capacity_w,
coil_adp_c)` — all default `None`; with defaults the physics is bit-identical (frozen
numbers re-verified). New read-only attributes after `step()`: `last_cool_w`,
`last_heat_w`, `last_fan_w` (per zone, W). `heat_setpoints` is only read when
`heat_capacity_w` is given. Heating electrical power = `q_heat / HEAT_COP` (3.0).

### 10.8 Zone explanation

`explain_zone() -> {action, headline, sentence, factors: [{label, value, unit, source, flag}],
flagged, reason_code ("base_schedule" when the controller logged no material event),
decision_summary, decision_id, objective, safety_mode, est_energy_delta_pct,
est_comfort_delta_pct, constraint}`. Every number is a field of the zone card, the
`ControllerDecision` or `ConstraintStore.explain()`; there is no generated free text.

---

## 11. Historical dataset schema — `backend/dataset/` (Phase 2)

Single source of the column list: `backend/dataset/schema.py`. Storage: SQLite
(`data/dataset/feelslike_history.sqlite`, generated, git-ignored). Narrative and models:
`docs/DATASET.md`. **Data state of every table: SIMULATED history** (demand/comfort are
DERIVED from it, `forecast_demand_kw` is PREDICTED). Never live / real / hardware.

**Row conventions.** `t` = site wall-clock time as unix seconds (no timezone), `ts` = the
same as ISO minutes. A row covers `[ts, ts + step)`: powers are interval means, energies
are interval totals, temperature/RH/CO2 are the state at the end, occupancy is the
headcount for the interval. Units follow §0: °C, %, ppm, **kW** and **kWh** in this dataset
(building-scale quantities; the live twin API keeps W).

### 11.1 IDENTITY — `building_dim`, `zone_dim`

| Column | Type | Meaning |
|---|---|---|
| `building_id` | TEXT | `bldg-office-01` |
| `building_type` | TEXT | Phase-1 profile key |
| `building_name` | TEXT | profile name |
| `floors`, `zones` | INT | zones = floors × 5 (one twin floor plate per floor) |
| `pv_kwp`, `occupancy_capacity`, `design_demand_kw` | REAL/INT | building design values |
| `profile_json` | TEXT | the Phase-1 `BuildingConfig` used |
| `floor_id` | TEXT | `F1`… |
| `zone_id` | TEXT | `F1-zone_a` (unique within a building) |
| `twin_zone`, `zone_role`, `area_m2`, `cooling_capacity_kw`, `heating_capacity_kw` | | zone design |

### 11.2 TIME — `time_dim` (keyed by `t`)

`ts, date, time, hour, minute, day_of_week (0=Mon), day_of_month, month, week_of_year (ISO),
is_weekend, is_holiday, holiday_name, season (summer|monsoon|winter|transition),
day_part (night|morning|afternoon|evening)`. `operating_hours` is building-specific and
lives in `building_obs`. All derived from the timestamp.

### 11.3 WEATHER — `weather_obs` (site-wide, keyed by `t`)

`outdoor_temperature_c, outdoor_humidity_percent, outdoor_pressure_hpa,
outdoor_wind_speed_mps, outdoor_wind_direction (N..NW), solar_irradiance_w_m2,
cloud_cover_percent, rainfall_mm (mm/h rate), weather_condition
(clear|partly_cloudy|overcast|rain|heavy_rain), dew_point_c, heat_index_c,
wet_bulb_temperature_c`.

### 11.4 Zone observations — `zone_obs` (one row per zone per `t`)

| Group | Columns |
|---|---|
| OCCUPANCY | `occupancy_count` (0..capacity), `occupancy_percent`, `occupancy_capacity`, `expected_occupancy`, `occupancy_change`, `occupancy_status` (EMPTY/LOW/MODERATE/HIGH/AT_CAPACITY) |
| INDOOR ENVIRONMENT | `indoor_temperature_c`, `indoor_humidity_percent`, `indoor_co2_ppm` (≥ 420), `indoor_air_quality_index` (0–500, lower better), `temperature_setpoint_c` (cooling), `heating_setpoint_c` (null without heating), `humidity_setpoint_percent`, `co2_limit_ppm` |
| HVAC | `hvac_status` (ON/OFF), `hvac_mode` (OFF/COOLING/HEATING/VENTILATION/AUTO/ECONOMY), `cooling_demand_percent`, `heating_demand_percent`, `ventilation_demand_percent`, `hvac_power_kw` (compressor + heat pump, excl. fans), `supply_air_temperature_c`, `return_air_temperature_c`, `fan_speed_percent`, `damper_position_percent`, `compressor_load_percent` |
| ENERGY | `total_power_kw`, `lighting_power_kw`, `plug_load_kw`, `ventilation_power_kw` (fans), `equipment_power_kw`, `energy_consumption_kwh`, `hvac_energy_kwh` (hvac + fans), `lighting_energy_kwh`, `equipment_energy_kwh` (plug + equipment) |
| DEMAND | `current_demand_kw` (= total), `hvac_demand_kw`, `cooling_demand_kw` / `heating_demand_kw` (thermal), `ventilation_demand_kw`, `occupancy_demand_factor` |
| COMFORT | `comfort_score`, `temperature_comfort_score`, `humidity_comfort_score`, `co2_comfort_score`, `pmv` (−3..3), `ppd` (%), `thermal_comfort_status` (ASHRAE 7-point) |
| ANOMALIES | `anomaly_ids` (comma list of active ids, null normally) |

**Invariants (tested):** `total = hvac + ventilation + lighting + plug + equipment`;
`energy = total × step_h = hvac_energy + lighting_energy + equipment_energy`;
counts ≤ capacity; all powers/energies ≥ 0.

### 11.5 Building observations — `building_obs` (one row per building per `t`)

`operating_hours, special_day, occupancy_count, occupancy_capacity, occupancy_percent,
total_power_kw (= Σ zones), hvac_power_kw, ventilation_power_kw, lighting_power_kw,
plug_load_kw, equipment_power_kw, renewable_power_kw, grid_power_kw (= max(0, total − PV)),
energy_consumption_kwh, grid_energy_kwh, daily_energy_kwh (since 00:00),
peak_demand_kw (running daily max of grid), current_demand_kw (= grid),
forecast_demand_kw (PREDICTED, seasonal naive), hvac_demand_kw, cooling_demand_kw,
heating_demand_kw, ventilation_demand_kw, occupancy_demand_factor, peak_demand_risk
(grid ÷ design), demand_category (LOW|MEDIUM|HIGH|CRITICAL), comfort_score (occupied mean)`.

### 11.6 QUALITY — `raw_obs`, `clean_obs`

RAW: `temperature_raw_c, humidity_raw_percent, co2_raw_ppm, occupancy_raw_count,
power_raw_kw, reading_delay_s, raw_flags` (noise, drift, delayed, outlier:<f>, missing:<f>,
sensor_offset, sensor_stuck, comm_gap). CLEAN: `temperature_clean_c, …, power_clean_kw` +
`<field>_quality ∈ ok|interpolated|missing|outlier_removed|stuck_removed|delayed`.
Key `(building_id, zone_id, t)` matches `zone_obs`.

### 11.7 ANOMALIES — `anomalies`

`anomaly_id, anomaly_type, severity (low|medium|high), start_time, end_time, building_id
('*' = site-wide), affected_zone ('*'), layer (physical|sensor), description, params_json`.

### 11.8 Aggregation rule (zone → floor → building)

`schema.AGG[col] = (across zones at one t, across time in a bucket)`: power / demand /
occupancy counts `(SUM, AVG)`, energies `(SUM, SUM)`, temperatures / RH / CO2 / scores /
percentages `(AVG, AVG)`. Each zone row is disjoint, so sums never double count
(building = Σ floors = Σ zones, tested). `total_power_kw_max` is added per bucket.

### 11.9a Phase-3 note
`backend/dataset/comfort.py` now takes its three sub-score formulas from `backend/comfort.py`
(identical arithmetic; stored history unchanged, weights 60/25/15 kept for the dataset).

### 11.9b (see §12 for the comfort contracts)

### 11.9 Future data sources

`/api/history` reads only the store. A later telemetry pipeline or real hardware can write
the same tables (or a table with the same columns) and every consumer keeps working; the
`data_state` field must then change from simulated to the real source.

---

## 12. Occupant comfort contracts — `backend/comfort*.py` (Phase 3)

Formulas and rationale: `docs/COMFORT.md`. Engineering comfort index, **not** a PMV/PPD
certification. Source tags: inputs keep their own (`sim` temperature/RH/occupancy,
`derived` CO₂); every score, status, event and duration is `derived`; dataset comfort is
`historical`; the trade-off is `predicted` ("SIMULATED / WHAT-IF").

### 12.1 `ComfortReading` (engine input — the Phase-4 latest-value seam)

| Field | Type | Units | Meaning |
|---|---|---|---|
| `zone_id` | str | — | zone |
| `t` | float\|None | sim-s | reading time |
| `temp_c`, `rh_pct`, `co2_ppm` | float\|None | °C, %, ppm | None = no reading (never guessed) |
| `occupancy`, `occupancy_pct` | float\|None | people, % of design | relevance |
| `hvac` | dict | — | `mode, cooling_pct, vent, setpoint, at_capacity, reason_code` |
| `source` | dict | — | field → provenance tag |
| `age_s` | dict | s | field → age; > 900 s = stale |

### 12.2 Assessment (`comfort.assess`)

`zone_id, t, score (0–100|null), occupied_score (null when not relevant), status
(OVERALL_STATUSES), severity (none|low|medium|high|severe|info), condition_severity,
occupancy_state (Unoccupied|Partially Occupied|Occupied), occupancy, occupancy_pct,
comfort_relevant, thermal, humidity, air_quality, factors[], primary_cause,
secondary_causes[], issues[] (warm|cold|humid|dry|high_co2), recommendation, hvac,
weights, data_quality{fields{temperature,humidity,co2: ok|missing|invalid|stale}, missing[],
stale[]}, source, index`.

Dimension object: `score, status, value, unit, deviation, target [lo, hi] (CO₂: [null, limit]),
severity, source, quality` (+ `indicator` for CO₂). Factor: `dimension, status, value, unit,
target, deviation, score, weight, impact = weight·(100 − score), severity, source`.
Recommendation: `action (none|act|defer), text, expected_effect, energy_consideration,
deferred, priority`.

### 12.3 Building summary (`comfort.building_summary`)

`overall_score, occupied_score, zones, occupied_zones, comfortable_zones,
uncomfortable_zones, compliance_pct, worst_zone{zone_id, score, status},
most_frequent_issue, high_co2_zones, warm_zones, cold_zones, humid_zones, dry_zones,
unavailable_zones` (+ `average_event_duration_s`, `open_events` from the API).

### 12.4 Comfort event (`ComfortTracker`)

`event_id (cmf-NNNN), status (open|resolved), started_t, started_clock, building_id,
building_name, floor, zone_id, zone_name, event_type (issue), triggering_metric
(temperature|humidity|co2), threshold, unit, measured_value, peak_value, severity,
peak_severity, occupancy_state, occupancy_pct, hvac_state{mode, cooling_pct, vent,
setpoint, at_capacity}, controller_action (reason code), recommended_action, resolved_t,
resolved_clock, resolution, last_seen_t, duration_s (on read), source`.

### 12.5 Durations

`current_discomfort_s, today{occupied_s, uncomfortable_s, uncomfortable_pct},
week{occupied_s, uncomfortable_s, uncomfortable_pct, days_covered}, source`.

### 12.6 Profile additions

`BuildingConfig.comfort_weights: {thermal, humidity, air_quality}` — each 0..1, sum 1
(± 0.01), validated; editable through `POST /api/building/profile`.
`comfort.controller_preference(cfg) -> {comfort_weight, energy_weight, objective_hint,
act_from_severity}`.

### 12.7 Trade-off (`comfort_whatif.tradeoff`)

`kind: predicted, label: "SIMULATED / WHAT-IF", zone_id, action (cool|raise|vent),
action_text, horizon_h, current{comfort_score, occupied_comfort_score, occupied_steps,
energy_kwh, zone_energy_kwh}, proposed{…}, delta{comfort_score, occupied_comfort_score,
energy_kwh, zone_energy_kwh}, note, isolation_verified, method`.

---

## 13. Latest-value telemetry — `backend/latest.py`, `backend/telemetry_publish.py` (Phase 4)

"What is happening right now." **The current implementation uses the digital twin as its
main telemetry source; that is not physical building telemetry.** Twin values are tagged
`sim`; values computed from them `derived`; the profile expectation `predicted`; the ESP32
rig and ambient node `hardware`. No new source vocabulary.

### 13.1 Reading (stored per `(zone_id, metric, sensor_id)`)

| Field | Type | Meaning |
|---|---|---|
| `metric` | str | key of `latest.METRICS` (aliases: `energy`→`energy_today`, `comfort`→`comfort_score`, `hvac_load`→`hvac_power`) |
| `value` | float\|str\|bool\|None | None when missing/invalid, or a legitimate null state with `note` (setpoint when HVAC is off) |
| `unit` | str | fixed by the metric (°C, %, ppm, people, W, kWh, score, level) |
| `timestamp` | str | ISO-8601 UTC wall clock of the reading |
| `t_wall` | float | same, epoch seconds |
| `sim_t` | float\|None | simulated building time (the twin is accelerated; `timestamp` is when it was published) |
| `building_id`, `floor_id`, `zone_id` | str | ids match `[A-Za-z0-9_.:-]{1,64}`; building-level metrics use `zone_id = "_building"`; the ambient node uses `"ambient"` |
| `source` | str | set by the SERVER-SIDE adapter; a payload's own `source` is ignored |
| `quality` | str | on read: `good`, `estimated`, `aging`, `stale`, `invalid`, `missing` (`simulated` reserved) |
| `stored_quality` | str | quality at ingest, before freshness |
| `age_s` | float | `now − t_wall`, computed at read time |
| `sensor_id`, `device_id`, `seq` | str/str/int | `SIM-ZONE-A-TEMP`, `EST-ZONE-A-CO2`, `DRV-ZONE-A-COMFORT`, `PRD-_BUILDING-EXPDEMAND`, `HW-<node>-TEMP`; device `digital-twin` or the node id |
| `problems`, `rejected_value` | list/any | why a reading was marked invalid, and what was sent |

### 13.2 Validation (`LatestStore.ingest`)

| Metric | Valid range |
|---|---|
| temperature, setpoint, outdoor_temperature | −50 .. 70 °C |
| humidity | 0 .. 100 % |
| co2 | 0 .. 10 000 ppm |
| occupancy, occupancy_pct | ≥ 0 |
| power, hvac_power, cooling_w, demand, expected_demand | ≥ 0 W (no bidirectional contract exists) |
| energy_today | ≥ 0 kWh |
| cooling_pct 0..100, fan_level 0..2, comfort_score 0..100, at_capacity bool, status/mode strings (≤ 120 chars) |

Out-of-range / non-numeric / non-finite value → stored with `quality = invalid`, `value = None`,
`problems`, `rejected_value` (visible, never used). Timestamp more than 60 s in the future,
more than 7 days old or non-finite → invalid. `None` → `missing`. Unknown metric, malformed
identifier or unknown source → `ValueError` (HTTP layer: 400/404/422), counted in
`rejected_total`. The hardware bridge's own stricter validation (−20..70 °C, 422) runs first.

### 13.3 Freshness

`good` ≤ 60 s · `aging` ≤ 300 s · `stale` > 300 s (wall clock). Sensor status: Healthy /
Aging / Stale / Invalid / Missing / Offline (> 1800 s). A stale value keeps its value and is
reported stale — never refreshed. The comfort engine uses the same 300 s rule
(`comfort.STALE_AFTER_S`); published comfort takes its **oldest input's** timestamp, so comfort
computed from stale inputs is itself stale.

### 13.4 Preference

`LatestStore.get(zone, metric, prefer)` returns the most preferred source among readings
that are not stale/invalid/missing, falling back to stale ones only when nothing else exists.
Default `prefer = sim, derived, hardware, real, predicted`: the rig is a shoebox, not the
conference room, so its readings are shown as `alternatives` beside the twin value.
`FL_HW_AUTHORITATIVE=1` puts `hardware, real` first.

### 13.5 `/api/latest` payload

`timestamp, sim_t, sim_clock, poll_interval_s, notice, hardware_authoritative,
building{building_id, type, name, metrics{outdoor_temperature, occupancy, occupancy_pct, power (W,
instantaneous), energy_today (kWh, accumulated), demand, expected_demand (predicted),
comfort_score}}, zones[{zone_id, name, floor_id, temperature, humidity, co2, occupancy,
occupancy_pct, hvac{hvac_mode, cooling_pct, fan_level, setpoint, hvac_power, cooling_w, at_capacity,
controller_action, ventilation_status}, energy{power}, demand{demand}, comfort{score, status,
thermal_status, humidity_status, air_quality_status, stale_inputs[], missing_inputs[]},
alternatives{metric: [views]}}], data_quality{readings, healthy, aging, stale, invalid, missing,
by_quality, by_source, last_update, last_update_age_s, accepted_total, invalid_total,
rejected_total}, system_health{status, label, reasons[], counts, subsystem_errors[]},
sources{temperature, humidity, co2, occupancy, hvac, power, demand, comfort}`.

Metric view: `value, unit, source, quality, timestamp, age_s, sensor_id, device_id, seq, sim_t`
(+ `note`, `display: Unavailable|Stale`, `problems`). No reading at all:
`{value: null, quality: "missing", source: null, message: "No current data available"}`.

`system_health.status`: OFFLINE (no readings or no update for > 300 s) · DEGRADED (stale or
invalid readings, zones without temperature, or hardware gone stale) · STALE DATA (stale ≥
healthy) · LIVE TELEMETRY (fresh hardware/real readings, nothing degraded) · SIMULATION MODE
(everything fresh and from sim/derived/predicted).

---

## 14. Seasonal scenarios and simulated hardware (Phase 5)

Narrative: `docs/SEASONAL_SIMULATION.md`, `docs/HARDWARE_SIMULATION.md`.
**SIMULATED HARDWARE IS NOT REAL HARDWARE.**

### 14.1 Twin hook (additive)
`DigitalTwin.envelope_scale: float = 1.0` multiplies every zone's `UA` in `step()` (1.0 is
bit-identical; copied by `clone()`).

### 14.2 `ScenarioConfig` (`backend/scenario.py`; LiveSim configuration, survives a building reset)
`mode (classic|seasonal), season (auto|summer|monsoon|winter|transition), climate (delhi|chennai),
cloud_cover (0..100 | null = model), rain_mm_h (0..50 | null = model), envelope_scale (0.5..2),
internal_load (people_only|profile), disturbance ({delta_c −8..8, t0, t1 sim-s} | null)`.
Twin knobs stay on the twin (`outdoor_offset ±10, humidity_offset ±30, solar_scale 0..2,
occ_scale 0..3, capacity_scale 0.1..1.5`); the scenario API **rejects** out-of-range values (400)
rather than clamping.

`GET /api/scenario` → config + `knobs, humidity_model, heating ("NOT MODELLED — the live controller
is cooling-only"), weather_now, limits, speed_presets[{label, speed}], options, causal, sim_clock,
speed, simulated_hardware`.

### 14.3 Latest-value additions (`latest.METRICS`)
`outdoor_humidity %, dew_point °C, heat_index °C, wind_speed m/s, wind_direction, solar_irradiance
W/m² (0..1500), cloud_cover %, rainfall mm/h, weather_condition, season, heating_demand W,
internal_gain W, equipment_power W, cooling_capacity_pct %`. Reading field **`origin`** ∈
`twin | simulated_hardware | hardware | derived` (source vocabulary unchanged: simulated
hardware is `source = "sim"`, `origin = "simulated_hardware"`). Classic weather publishes the
metrics it cannot produce as `value: null` with `note: "not in the classic weather model"`;
heating is always `null` with the NOT MODELLED note.

`/api/latest` gains `weather{outdoor_temperature, outdoor_humidity, …, season}`,
`scenario{mode, season, climate, internal_load, envelope_scale, weather_model, heating}`,
`causal{available, chain[{key, label, value, unit, delta, direction}], sentence,
at_capacity_zones, energy_kwh_per_sim_hour, window_s, source, t, prev_t}`,
`telemetry_mode ("DIGITAL TWIN (direct)" | "SIMULATED HARDWARE — not real hardware")`;
zones gain `internal_gain` and `hvac.heating_demand`; building metrics gain
`cooling_capacity_pct, heating_demand, equipment_power`.

### 14.4 Season comparison (`GET /api/scenario/compare?horizon_h=0..24`)
`kind: predicted, label: "SIMULATED / WHAT-IF", horizon_h, start_t, climate, seasons{season:
{outdoor_temperature_c, indoor_temperature_c, indoor_humidity_pct, cooling_demand_w,
max_cooling_capacity_pct, heating_demand ("NOT MODELLED …"), hvac_power_w, energy_kwh,
comfort_score, peak_demand_w, at_capacity_zone_min, solar_irradiance_w_m2, rain_hours}},
not_modelled, note, live_state_untouched, fingerprint_at_start`.

### 14.5 Simulated hardware (`backend/simhw.py`)
Registry entry: see `docs/HARDWARE_SIMULATION.md` §2. APIs: `GET /api/simhw` →
`{enabled, noise_level, notice, devices[], faults[], protocols{name: "SIMULATED …"},
config_limits}`; `POST /api/simhw {enabled?, noise_level 0..3?}`;
`POST /api/simhw/devices/{device_id}/fault {mode: none|offline|stuck|drift|invalid|delay,
duration_s 1..86400?}` (404 unknown id, 400 bad mode); `POST /api/simhw/devices/{device_id}/config
{sampling_interval_s, noise_sd, bias, drift_per_hour, failure_probability, comm_delay_s}`
(bounded, 422 unknown field). Readings: `sensor_id = device_id`, `seq` per device, `t_wall` = sampling
time (a delayed reading arrives old), derived `occupancy_pct` sensor `DRV-ZONE-X-OCCPCT`.

---

## 15. Security contracts (Phase 6)

Full description: `docs/SECURITY.md`. Status labels: IMPLEMENTED AND TESTED · ARCHITECTURE PREPARED ·
SIMULATED · PRODUCTION DEPLOYMENT REQUIREMENT.

### 15.1 Principal
`kind (user|development|anonymous|device), username, role (admin|facility_manager|energy_manager|
hvac_operator|occupant|auditor), zones (list | null = all; hvac_operator must list zones), authenticated,
permissions[], session_expires_at`. Permissions and the role matrix: `backend/security/auth.py` (`ROLES`).

### 15.2 Session / login
`POST /api/security/login {username, password}` → `{token, token_type: "bearer", expires_at, user{username,
role, zones, disabled, created_at}, permissions[]}`. Tokens: opaque, `Authorization: Bearer <token>`,
stored server-side as SHA-256 digests, TTL `FL_TOKEN_TTL_S`. Errors are generic (`"Authentication failed."`).

### 15.3 Route policy
`POLICY[(METHOD, route_path)] -> permission | "public" | "session" | "device"`; every route must be listed
(tested). 401 unauthenticated / invalid / expired, 403 not permitted, 429 rate limited.

### 15.4 Device identity
`device_id, building_id, floor_id, zone_id, device_type (sensor|sensor_node|gateway), sensor_type, metrics[],
protocol (https|mqtts|mqtt-simulated|bacnet-gateway|modbus-gateway|https-simulated|bacnet-simulated|
modbus-simulated|http-legacy), source (sim|hardware — trusted, server-side), origin, simulated, status
(ACTIVE|INACTIVE|QUARANTINED|SIMULATED), effective_status (+OFFLINE|UNKNOWN), firmware_version,
sampling_interval_s, key_id, key_fingerprint (sha256 prefix), cert_fingerprint, credential_expires_at,
credential_status (valid|revoked), credential_state (valid|expired|revoked), quarantine_reason, created_at,
updated_at, last_seen, last_seq, accepted, rejected, authenticated_state, label (SIMULATED|HARDWARE)`.
Key material is never part of this record; a new key is returned once as `device_key` (hex, 32 bytes).

### 15.5 Telemetry envelope (`POST /api/telemetry/ingest`, MQTT payload, simulated devices)
```json
{"device_id": "HW-ZONE-C-01", "seq": 42, "ts": 1789000000.0, "nonce": "optional",
 "readings": [{"metric": "temperature", "value": 24.6}],
 "zone_id": "optional claim", "building_id": "optional claim", "source": "ignored claim",
 "sig": "hex HMAC-SHA256(device key, canonical JSON of every field except sig, sorted keys, no spaces)"}
```
Response: `{status (ACCEPTED|STALE|REJECTED|QUARANTINED|DUPLICATE|UNKNOWN_DEVICE), device_id, accepted,
reason, trusted_source, results[{metric, status, reason?, stored?}]}`; errors 401 / 403 / 409 / 413 / 422 / 429
with a generic `detail` and the machine `status` / `reason`. Accepted readings enter `LatestStore` with the
registry's source / origin; sensor id = device id (single-metric) or `device_id:metric`.

### 15.6 Audit event
`id, timestamp, event_type, severity (info|warning|high), actor, action, target, zone, result, reason,
request_id, details{…redacted}`. Never contains passwords, tokens, keys, signatures or password hashes.

### 15.7 Zone control command (`POST /api/control/zone/{zone_id}`)
`{action: cooler|warmer|more_ventilation|less_ventilation|clear} | {target_setpoint_c: 21.5..29.0}`,
`severity 1..3`, `reason ≤ 200`. Response `{ok, zone_id, old_setpoint_c, applied{issue, severity,
constraint_ids, expected_offset_c, expected_vent_delta} | {cleared_constraints} | {no_change}, note}`.

### 15.8 Occupant rooms (`GET /api/occupant/rooms`)
`{sim{clock, t_out}, zones[{id, name, temp, setpoint, active_constraints}]}` — zone-scoped for occupants.
