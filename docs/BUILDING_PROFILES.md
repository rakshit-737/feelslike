# Building profiles, demand model and the Building tab

Phase 1 of the commercial-building dashboard. Code: `backend/building.py` (model, pure),
`backend/app.py` (`LiveSim.building`, `/api/building*`), `dashboard/building.js` (the tab).
Tests: `tests/test_building.py`. Field-level contracts: `DATA_CONTRACTS.md` §10.

## 1. What a profile is, and what it is not

The digital twin (`sim/twin.py`) models **five zones of an office-like floor plate**. That
does not change. A *building profile* is the operator's description of the building those
zones sit in. Switching profile changes:

| Changes | How |
|---|---|
| How zone state is **evaluated** | comfort range, humidity range, CO₂ threshold, operating hours drive zone status, KPI status and comfort score |
| What is **expected** | a 24-hour occupancy schedule, base HVAC load and occupancy sensitivity give expected occupancy / HVAC demand per hour |
| Which **controller levers** are pulled | the operating mode maps to an `(objective, safety_mode)` pair — the same levers `POST /api/controller` writes |
| Display **roles and floors** | each twin zone gets a role ("Outpatient consultation") and a floor number |

A profile **never** changes the physics, the zone list, the parser's zone vocabulary, or
the A/B baseline race. Occupancy capacity and HVAC capacity are configuration only; the
five modelled zones are **not** extrapolated to the configured building size (that
scale-out is a later phase). Every floor and zone the twin does not model is shown as
**NOT MODELLED** with no values.

## 2. The six profiles

Defaults are indicative design values (ASHRAE 55 comfort practice, ASHRAE 62.1 outdoor-air
rates, ASHRAE 90.1-order lighting power density). All are operator-editable and validated.

| Type | Floors / zones | Capacity | Hours | Comfort °C | RH % | CO₂ ppm | Comfort / energy priority | Occ. sensitivity | Base load |
|---|---|---|---|---|---|---|---|---|---|
| Office | 4 / 12 | 120 | 08–20 Mon–Fri | 23.0–26.5 | 30–65 | 1000 | 60 / 40 | 0.8 | 0.15 |
| Shopping Mall | 3 / 40 | 3000 | 10–22 daily | 23–26 | 30–65 | 1100 | 50 / 50 | 0.9 | 0.20 |
| Hospital | 6 / 60 | 800 | 24 h | 22–25 | 30–60 | 800 | 85 / 15 | 0.4 | 0.55 |
| Hotel | 8 / 150 | 400 | 24 h | 22–25 | 30–60 | 1000 | 75 / 25 | 0.6 | 0.40 |
| College | 4 / 30 | 1500 | 08–18 Mon–Sat | 23.0–26.5 | 30–65 | 1000 | 50 / 50 | 0.9 | 0.12 |
| Data Center | 2 / 10 | 40 | 24 h | 22–26 | 20–60 | 1000 | 40 / 60 | 0.1 | 0.85 |

Each profile also carries a `model_fit_note` that says plainly where the office-shaped twin
does not match the building type (e.g. data-centre IT heat load is not modelled).

### Schedules (expected occupancy, fraction of design)

Stored as anchor points in `PROFILES` and linearly interpolated to 24 hourly values.
Demand levels: `< 0.35` low, `< 0.60` medium, `< 0.85` high, otherwise very high.
Verified by `test_office_schedule_matches_the_worked_example` and
`test_mall_schedule_matches_the_worked_example`:

- Office (weekday): 08:00 low · 09:00 medium · 11:00 high · 13:00 high · 18:00 medium · 21:00 low
- Mall: 10:00 medium · 13:00 high · 18:00 very high · 22:30 low

Outside operating hours the schedule is multiplied by `closed_factor`; the whole curve is
scaled by `expected_occupancy_pct`.

## 3. Demand model

For each modelled zone and for the building (sum over modelled zones):

| Demand | Formula | Data state |
|---|---|---|
| Current occupancy | twin headcount; % of that zone's weekday design peak | SIMULATED |
| Expected occupancy | `schedule(h) × expected_occupancy_pct` (building: × occupancy capacity) | PREDICTED (`basis: profile_schedule`) |
| HVAC demand | electrical power from the twin's own kWh accounting | DERIVED |
| Expected HVAC demand | `(base + (1 − base) × sensitivity × expected_occ) × design power` | PREDICTED |
| Cooling demand | thermal cooling `q_cool`, exact inverse of `p = q/COP + fan` | DERIVED |
| Heating demand | — the twin is cooling-only | NOT MODELLED |
| Ventilation demand | ASHRAE 62.1 `Rp·Pz + Ra·Az` (L/s), with what the twin actually supplies and a met flag | DERIVED |
| Lighting demand | `LPD × area × max(10 %, occupancy)` when open, 10 % closed — **estimate, not in kWh** | DERIVED |
| Estimated energy demand | HVAC + lighting estimate | DERIVED |
| Comfort demand | °C outside the profile comfort range (worst occupied zone) | DERIVED |
| Peak demand | highest total HVAC power in telemetry since 00:00 sim time | DERIVED |

`demand_series()` buckets the step-cadence telemetry (`backend/telemetry.py`) into hours.
An hour with no samples carries `null` actual values — it is never zero-filled. The
seam for later phases: replace the telemetry rows with real meter rows and the expected
schedule with a learned one; the payload shape does not change.

## 4. Operating modes

| Mode | Objective | Safety mode |
|---|---|---|
| Normal | from priorities: energy − comfort ≥ 30 → `energy`, ≤ −30 → `comfort`, else `balanced` | `automatic` |
| Energy Saving | `energy` | `automatic` |
| Comfort Priority | `comfort` | `automatic` |
| Peak Demand Reduction | `cost` (no dedicated peak-shaving law yet — stated in the UI) | `automatic` |
| Emergency | `balanced` | `emergency_override` |
| Simulation | from priorities | `recommend_only` (dry run: decisions logged, not applied) |

Levers are written only when `building_type`, `operating_mode` or a priority changes.
If an operator later changes the objective on the Control tab, `operating_mode.in_sync`
turns false and the Building tab says so, rather than silently disagreeing.

## 5. KPIs (executive strip)

| KPI | Definition | State |
|---|---|---|
| Current energy draw | Σ zone HVAC electrical power (W) | DERIVED |
| Today's energy | twin kWh now − kWh at 00:00 (or since build, flagged) | SIMULATED |
| Current HVAC load | Σ cooling W ÷ Σ thermal capacity | DERIVED |
| Occupancy | Σ twin headcount, % of modelled design headcount | SIMULATED |
| Average temperature / humidity | mean over occupied zones | SIMULATED |
| Average CO₂ | mean of the mass-balance estimate over occupied zones | DERIVED |
| Comfort score | mean over occupied zones, against the **profile** ranges (same formula as telemetry; identical for the office profile) | DERIVED |
| Energy saving % | vs the static-schedule baseline twin on the same weather | DERIVED |
| Peak demand today | see §3 | DERIVED |
| Active alerts | maintenance alerts + zones in warning/critical | DERIVED |
| System health | subsystem failures, telemetry, hardware rig, external feed | DERIVED |

Known limitation surfaced, not hidden: simulated indoor RH sits high because of the coil ADP
approximation in `sim/twin.py`, so the humidity KPI often reads critical; its note says so.

## 6. APIs

| Method + path | Purpose | Errors |
|---|---|---|
| `GET /api/building` | config, operating mode, topology, zone cards, building demand, KPIs, source legend | — |
| `GET /api/building/profile` | active config + catalog (six types with defaults, limits, modes) | — |
| `POST /api/building/profile` | switch type and/or edit fields and/or operating mode; all-or-nothing | 400 `{detail: {errors: [...]}}`, 400 empty body, 422 unknown field / wrong type |
| `GET /api/building/zones/{zone_id}?window=1h\|6h\|24h\|7d\|live` | drill-down: card, trends, WHY, constraint, alerts, events | 404 zone, 400 window |
| `GET /api/building/demand?zone=all\|zone_id&hours=1..168&ahead_h=0..24` | hourly actual vs expected | 400 |

`/api/state` gains an additive `building {type, type_label, name, operating_mode}` block.
The building configuration survives `POST /api/reset` (it is configuration, like the objective).

## 7. Dashboard behaviour (Building tab)

- Landing tab when the URL has no hash (`#overview` and every other tab still deep-link;
  if `building.js` fails to load, the shell falls back to Overview after 4 s).
- **Control bar:** building type, floor, zone, operating mode, comfort and energy
  priority, simulation speed (`/api/speed`).
- **Header:** name, type, floors/zones configured vs modelled, capacity, hours, comfort
  constraints, current vs expected demand level, mode → levers, model-fit note.
- **Executive KPIs:** 12 cards with data-state badge and normal/warning/critical status.
- **Building overview:** floors → zone cards (temp, RH, occupancy, CO₂, HVAC, power,
  demand, comfort, status flags). Consecutive unmodelled floors collapse into one row.
  Floor filter narrows the tree and the zone selector.
- **Demand:** current vs expected table for building or selected zone; interactive chart
  (6 h – 7 d range; HVAC power / occupancy / cooling) with crosshair tooltip; expected
  demand level strip per hour.
- **Zone drill-down** (click a card or pick a zone): live tiles, "Why the HVAC is doing
  this" built from the controller's decision record and live values, factor table with
  data states and flags, current constraint, alerts, recent decisions and occupant
  messages, six trend charts with 1 h – 7 d windows.
- **Building configuration** (collapsible form): every profile field, server-validated;
  errors are listed and nothing is applied until all fields pass; reset to type defaults.

Charts reuse the Monitor tab's SVG primitive (`window.FLChart`, exported by `monitor.js`).

## 8. Not in this phase

Dataset expansion, real-time telemetry architecture, seasonal weather, hardware
simulation, security architecture, scale-out of the 5 modelled zones to the configured
building, per-zone profile overrides, a dedicated peak-shaving control law, and heating.
