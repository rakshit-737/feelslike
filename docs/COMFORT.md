# Occupant comfort (Phase 3)

Code: `backend/comfort.py` (engine), `backend/comfort_events.py` (events + durations),
`backend/comfort_whatif.py` (comfort vs energy), `backend/app.py` (`/api/comfort*`),
`dashboard/comfort.js` (Comfort tab), `dashboard/building.js` (drill-down block),
`dashboard/occupant.html` (occupant card). Tests: `tests/test_comfort.py`.
Schema: `DATA_CONTRACTS.md` §12.

## 1. What it is — and is not

An **engineering comfort index**: transparent, per-dimension, computed in the backend from
values the twin, telemetry or dataset actually produce, and explainable number by number.
It is **not** an ASHRAE 55 / ISO 7730 compliance or certification calculation. The project's
formal thermal-comfort estimate is PMV/PPD (ISO 7730 algorithm with stated input
assumptions) in the historical dataset (`backend/dataset/comfort.py`, `docs/DATASET.md`).
Comfort ranges, CO₂ thresholds and weights are **indicative / operator configurable**
building-profile values, not regulatory requirements. CO₂ is shown as a **ventilation
indicator**, never as a measurement of every aspect of indoor air quality.

## 2. Inputs (and the Phase-4 seam)

The engine takes a `ComfortReading` — `zone_id, t, temp_c, rh_pct, co2_ppm, occupancy,
occupancy_pct, hvac{mode, cooling_pct, vent, setpoint, at_capacity, reason_code},
source{field: tag}, age_s{field: seconds}` — and never reads HTTP, polls, or touches the
twin. Today `LiveSim.comfort_readings()` builds readings from live state (temperature, RH
and occupancy **sim**, CO₂ **derived** from the telemetry mass balance). A latest-value
telemetry pipeline (Phase 4) only has to produce the same objects with `hardware`/`real`
tags and ages.

## 3. Formulas

| Dimension | Score (0–100) | Status | Severity (by deviation) |
|---|---|---|---|
| Thermal | 100 inside `[comfort_min_c, comfort_max_c]`; `100·(1 − dev/3 K)` outside, floor 0 | Comfortable / Warm / Cold | ≤ 1 K low · ≤ 2 medium · ≤ 3 high · > 3 severe |
| Humidity | 100 inside `[humidity_min_pct, humidity_max_pct]`; `100·(1 − dev/20 %RH)` outside | Comfortable / Humid / Dry | ≤ 5 low · ≤ 10 medium · ≤ 20 high · > 20 severe |
| CO₂ (ventilation indicator) | 100 up to 0.7·limit; linear to 50 at the limit; `50 − 50·(co2 − L)/L` above, floor 0 | Good / Moderate (> 0.7 L) / High CO2 (> L) / Poor (> 1.5 L) | excess ≤ 20 % low · ≤ 50 medium · ≤ 100 high · > 100 severe |

Worked example (spec): 27.2 °C against 23–26 °C → deviation +1.2 K, thermal score
`100·(1 − 1.2/3) = 60`, status Warm, severity medium (1 K < 1.2 K ≤ 2 K). Tested.

**Combined score** = `Σ wᵢ·scoreᵢ / Σ wᵢ` over the dimensions that have a **valid** reading.
A missing or invalid reading drops out and is listed in `data_quality.missing`; no value
is invented. No valid reading at all → score `null`, status Unavailable.

**Weights** (`BuildingConfig.comfort_weights`, editable, must sum to 1):

| Profile | thermal | humidity | CO₂ | Rationale (indicative) |
|---|---|---|---|---|
| Office | 0.50 | 0.20 | 0.30 | thermal + ventilation dominate complaints |
| Shopping Mall | 0.50 | 0.15 | 0.35 | dense crowds → ventilation |
| Hospital | 0.40 | 0.20 | 0.40 | air quality and thermal both critical |
| Hotel | 0.55 | 0.25 | 0.20 | guest thermal comfort, low density |
| College | 0.45 | 0.15 | 0.40 | dense classrooms → CO₂ |
| Data Center | 0.30 | 0.50 | 0.20 | RH / condensation matters for equipment; IT thermal envelopes (TC 9.9 classes) are **not** modelled |

**Occupancy state**: Unoccupied (0 people) · Partially Occupied (< 50 % of the zone's design
headcount) · Occupied. Comfort is *relevant* only when occupied or partially occupied:
unoccupied zones keep their score but report status **Unoccupied**, severity `info`,
`occupied_score = null`, and no action.

**Overall status** (no careless contradictions): Unavailable → Unoccupied → Severe
Discomfort → the worst issue (Warm / Cold / Humid / Dry / High CO2 / Poor Air Quality,
ranked by severity then score impact `wᵢ·(100 − scoreᵢ)`) → Excellent (≥ 90) / Comfortable
(≥ 75) / Acceptable. Each dimension's own status is always returned alongside, so
"thermal Comfortable + air High CO2" is shown as exactly that.

**Data quality**: each field is `ok | missing | invalid | stale`. Invalid = outside
physically possible ranges (temperature −20..60 °C, RH 0..100 %, CO₂ 300..10 000 ppm),
NaN or non-numeric. Stale = older than 15 min (still shown, flagged).

## 4. Root cause and recommendation

`factors` are all scored dimensions sorted by impact; `primary_cause` / `secondary_causes`
are those with severity above none. The recommendation is rule-based on the cause **and
the current HVAC state**: warm → increase cooling (or "cooling at capacity" when saturated);
cold → reduce cooling / increase heating; humid → extend coil runtime; dry → humidification
(not modelled); high CO₂ → fan level up (or "ventilation at maximum"). Each carries an
expected effect and an energy consideration. Every number comes from the reading.

## 5. Comfort vs energy priority

`controller_preference(cfg)` is the interface a controller consumes:
`comfort_weight = cp/(cp+ep)`, `energy_weight`, `objective_hint` (same 30-point rule as
`building.mode_levers`, which already drives the controller objective), and
`act_from_severity`: comfort priority ≥ 70 → act from **low**; energy priority ≥ 70 → only
from **high**; else **medium**. Below that severity the recommendation is `defer`, with the
action still stated. The ConstraintAware control law is not rewritten.

## 6. Comfort events and durations

Event opens when an **occupied** zone shows an issue continuously for 5 sim-min (start time =
first appearance); updates peak value/severity and controller action; resolves after 5
sim-min back in range (resolution time = when it cleared) or immediately when the zone is
vacated. Fields: event_id, started/resolved time, building, floor, zone, event_type,
severity + peak, triggering metric, threshold, measured + peak value, occupancy state/%,
HVAC state, controller reason code, recommended action, status, duration, resolution.

Durations per zone per sim-day (kept 7 days): occupied seconds and uncomfortable occupied
seconds → current episode, today, week, % of occupied time uncomfortable. Building average
event duration comes from the event log. All accumulate from the live twin since reset.

## 7. History and trade-off

`/api/comfort/history?source=live` uses the twin's 60-s telemetry buffer (7 sim-days),
stride-sampled to ≤ 240 points, each scored with the **current** profile; a range longer
than the buffer is marked `partial`, an empty buffer returns "No data available".
`source=historical` serves the Phase-2 dataset's stored comfort (weights 60/25/15 at
generation, labelled HISTORICAL). Results are cached per (zone, span, last row, profile).

`/api/comfort/tradeoff` runs two clone simulations of the live twin (same controller; the
proposed run applies setpoint −1 K / +1 K or fan +1 to one zone) and reports comfort and
kWh for both, the difference, and `isolation_verified`. Labelled **SIMULATED / WHAT-IF**.

## 8. APIs

| Endpoint | Returns |
|---|---|
| `GET /api/comfort?floor=&zone=&issue=` | summary KPIs, zone assessments + durations, floors, open events, weights, priority, thresholds |
| `GET /api/comfort/zone/{zone_id}` | one assessment + events + 6 h trend (404 unknown) |
| `GET /api/comfort/events?zone=&issue=&status=open\|resolved\|all&limit=` | events, newest first |
| `GET /api/comfort/history?zone=&range=1h\|6h\|12h\|24h\|7d\|30d&source=live\|historical&building_id=` | trend points |
| `GET /api/comfort/tradeoff?zone=&action=auto\|cool\|raise\|vent&horizon_h=` | what-if comparison |
| `GET /api/comfort/occupant?zone=` | occupant view (no hardware / network / controller internals) |
| `GET /api/building/zones/{id}` | now also carries `comfort` (Phase-1 drill-down) |
| `POST /api/building/profile` | now accepts `comfort_weights` |

## 9. Dashboards

- **Comfort tab** (facility manager): filters floor / zone / issue / range / source (live
  or historical dataset building); 12 KPIs; heatmap floor × zone with per-dimension
  statuses (click → Building drill-down); worst zones with primary/secondary cause,
  occupancy, HVAC, recommendation, expected effect, energy, data quality; trend charts
  (score incl. occupied-only, temperature, humidity, CO₂, occupancy) or "No data
  available"; events table; duration table; comfort-vs-energy runner.
- **Building tab drill-down**: new "Occupant comfort" block (dimension table with data
  states, root cause, recommendation, today's discomfort).
- **Occupant page**: "How this room feels right now" — status, three readings with ranges,
  one-sentence explanation, recent score sparkline.

## 10. Limitations

- Air temperature only: no radiant temperature, air speed, clothing or activity in the index.
- CO₂ is a mass-balance estimate in the twin (`co2_estimated`), not a sensor.
- Humidity limits apply to comfort; dehumidification energy is not charged (twin assumption).
- Events and durations live in memory and reset with the simulation.
- The trade-off covers one zone and one action at a time over ≤ 6 h.
- Data-centre equipment environmental classes are not modelled.
