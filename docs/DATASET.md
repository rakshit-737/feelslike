# FeelsLike historical dataset (Phase 2)

A multi-building, multi-zone, 5-minute time-series dataset for dashboards, historical
analysis, anomaly detection, forecasting and future ML. **Every value is SIMULATED
history** — generated, not live, not real, not hardware. Code: `backend/dataset/`.
Schema: `DATA_CONTRACTS.md` §11. Tests: `tests/test_dataset.py`.

## 1. Architecture

```
 config.py (ONE place: dates, step, seed, buildings, season, climate, noise, anomalies, calendar)
      │
 Weather (weather.py) ───────────────┐  outdoor T, RH, solar, cloud, rain, wind, pressure
      │                              │
 Calendar ─> Occupancy (occupancy.py)┤  per building type, zone, day, holiday, event
      │                              │
 Internal gains (loads.py) ──────────┤  people + lighting + plugs + equipment (IT) = heat AND power
                                     ▼
            Building thermal model = the EXISTING sim/twin.py, one 5-zone twin per floor,
            stepped every 60 s  <── BMS schedule (hvac.py): setpoints, heating, fans, economizer
                                     │
            Indoor T / RH (twin) · CO2 (telemetry mass balance) ─> Comfort (comfort.py)
                                     │
            HVAC + non-HVAC power ─> Energy / PV / grid / demand / forecast (generator.py)
                                     │
            RAW sensors ─> VALIDATE ─> CLEAN (quality.py)          anomalies.py (rare, labelled)
                                     │
            SQLite store (store.py) ─> analysis.py ─> /api/history* ─> History tab
```

Pipeline stages are separate modules: **raw generation** (weather, occupancy, loads,
twin) → **validation** (`quality.validate`) → **cleaning** (`quality.clean`) → **feature
engineering** (`calendar.time_features`, psychrometrics) → **derived metrics** (comfort,
demand, IAQ, aggregates) → **API/dashboard**.

### Reuse, not duplication

| Concept | Reused from | Phase-2 addition |
|---|---|---|
| Thermal + moisture physics | `sim/twin.py` `DigitalTwin` | optional hooks (weather RH, solar, occupancy, internal gains, per-zone capacity, heating, coil ADP); defaults bit-identical — frozen 722.4/493.4/530.3 kWh re-verified |
| Psychrometrics | `sim/humidity.py` | heat index, wet bulb (weather.py) |
| Building profiles, schedules, comfort limits, OA rates, lighting LPD | `backend/building.py` (Phase 1) | zone occupancy patterns, densities, plug/equipment loads |
| CO2 model | `backend/telemetry.py` mass balance + constants | per-minute integration per zone |
| Comfort score shape | `backend/telemetry.comfort_score` thermal term | humidity + CO2 terms, PMV/PPD |

## 2. Size and defaults

`python -m scripts.generate_dataset` with no arguments: **42 days from 2026-05-18**
(late summer into monsoon), **5-minute** rows, **6 buildings** (office with 2 floors,
mall, hospital, hotel, college, data centre), **35 zones**, seed 42, Delhi climate.
That is 12 096 timestamps, 423 360 zone rows, 72 576 building rows, 423 360 raw and
423 360 clean sensor rows. The file is git-ignored; regenerate it any time.

Scaling needs configuration only: `--days 365`, more floors (`office:6`), more buildings,
`--step-min 15`. Rows = days × 1440/step × zones.

## 3. Regenerating

```bash
python -m scripts.generate_dataset                                   # defaults / data/dataset/config.json
python -m scripts.generate_dataset --days 90 --seed 7
python -m scripts.generate_dataset --season winter --start 2026-01-05 --days 30
python -m scripts.generate_dataset --buildings office:3:20,mall:1:10 --anomaly-rate 0.02 --noise-level 0.5
python -m scripts.generate_dataset --quick                           # 3 days
```
Same config + seed = identical data (asserted by test). The script prints summary
statistics and correlations at the end. Optional `data/dataset/config.json` overrides any
`DatasetConfig` field (buildings, holidays, events, quality, anomalies).

## 4. Models

### Weather (`weather.py`)
One hourly state machine per site, interpolated to 5 min. Temperature = seasonal mean +
diurnal sine (min ~06:00, max ~15:00, amplitude damped by cloud) + AR(1) synoptic anomaly
− 2.5 K while raining. Dew point: seasonal mean + slow AR(1), capped at the dry bulb, near
saturation in rain; **RH is computed from T and dew point** (Magnus), so it is
anti-correlated with the daily temperature cycle. Rain comes as **events** (Poisson starts,
afternoon-weighted, 1–5 h, log-normal intensity). Cloud: AR(1) around the seasonal mean,
overcast in rain. Solar GHI = Haurwitz clear sky from latitude / day-of-year / hour ×
(1 − 0.75·cloud^3.4) × 0.35 in rain. Derived: dew point (Magnus), heat index (NOAA
Rothfusz, ≥ 27 °C), wet bulb (Stull 2011). Seasons: summer, monsoon, winter, transition
(IMD months or a fixed `--season`); climates: `delhi`, `chennai`.

### Occupancy (`occupancy.py`)
Capacity per zone = floor area ÷ type density (office 9 m²/person, mall 5, hospital 10,
hotel 15, college 3.5, data centre 40). Fraction = Phase-1 profile schedule (operating
days/hours, closed factor) or a zone-specific pattern (cafeteria lunch, hotel guest rooms
at night, hospital OPD) × weekend factor (mall 1.35) × holiday/event factor × per-day level
N(1, sd) × per-zone AR(1) wobble. Counts are integers in [0, capacity]; expected occupancy
is the same product without random terms.

### Loads and HVAC (`loads.py`, `hvac.py`)
Lighting (LPD × area × occupancy, daylight dimming), plugs (W/m² × (25 % + 75 % occupancy)),
equipment (type duty; data-centre IT 400 W/m² in the white space) — each is electrical power
**and** a sensible gain into the twin. Cooling capacity is sized per zone at 1.25 × peak
load; heating (heat pump, COP 3) at 1.25 × envelope + ventilation load at the climate's cold
design temperature. The BMS schedule: occupied cooling SP = comfort max − 1, heating SP =
comfort min + 0.5, pre-conditioning 1 h before opening, setback 29 / 16 °C unoccupied,
demand-controlled ventilation on CO2 (≥ 80 % of limit) or occupancy (≥ 70 %), economizer when
outdoor air is ≥ 3 K cooler. The coil apparatus dew point is fixed at 12 °C for generated
history (the live twin keeps its documented approximation).

HVAC observables: mode (OFF / COOLING / HEATING / VENTILATION / AUTO / ECONOMY), demand %
vs capacity, supply air = return − 10 K × cooling load (+15 K × heating load), return air =
zone + 0.5 K, fan speed from fan power, damper 10 / 50 / 100 % by fan level.

### Energy and demand (`generator.py`)
Zone: `total = hvac (compressor/heat pump) + ventilation (fans) + lighting + plug + equipment`;
`energy = total × step`. Building: sums of zones (no double counting) + rooftop PV
(kWp × GHI × 0.8 × temperature derate) → `grid = max(0, total − PV)`. Daily energy resets at
midnight; `peak_demand_kw` is the running daily maximum of grid demand.
`forecast_demand_kw` (PREDICTED) = seasonal naive: the same time on the most recent day of
the same day-type (workday vs weekend/holiday), persistence before any history exists.
`peak_demand_risk = grid ÷ design electrical demand`; category LOW < 0.35 ≤ MEDIUM < 0.6 ≤
HIGH < 0.85 ≤ CRITICAL.

### Comfort (`comfort.py`)
`comfort_score = 0.60·temperature + 0.25·humidity + 0.15·CO2` (each 0–100: temperature 100
inside the profile range → 0 at 3 K outside; humidity 100 inside → 0 at 20 %RH outside;
CO2 100 up to 70 % of the limit, 50 at the limit, 0 at 2×). **PMV/PPD: the ISO 7730
algorithm exactly, with stated input assumptions**: mean radiant = air temperature, air
speed 0.10 m/s (0.15 at fan 2), metabolic rate by building type, clothing by season. Checked
against the ASHRAE 55 example (22 °C, 60 %, 1.2 met, 0.5 clo → PMV −0.75, PPD 17 %).
Indoor air quality index (0–500): CO2 piecewise + RH outside 30–60 %.

## 5. Holidays, events, anomalies, data quality

Holidays and events live in the config (dates, names, per-building-type factors; events
also have hours) — add a date, regenerate. Anomalies (default 0.08 per building per day,
weather disturbances site-wide): occupancy spike, HVAC failure, CO2 spike, abnormal energy,
weather disturbance (**physical** — applied to the simulation so the consequences propagate),
temperature sensor fault and communication gap (**sensor** — raw layer only). Each has
`anomaly_id, anomaly_type, severity, start_time, end_time, affected_zone, description`.

| Layer | Table | What it is |
|---|---|---|
| Simulated truth + derived | `zone_obs`, `building_obs`, `weather_obs` | the simulation state and everything computed from it |
| RAW | `raw_obs` | what sensors would report: noise (× `noise_level`), temperature drift, missing (0.5 %), delayed (0.2 %), outliers (0.1 %), sensor anomalies |
| CLEAN | `clean_obs` | validated (range, spike vs neighbour median, stuck ≥ 12 samples) → removed → gaps ≤ 3 samples interpolated; per-field flag `ok / interpolated / missing / outlier_removed / stuck_removed / delayed` |

Raw is never overwritten. Drift cannot be removed without a reference and is left in.

## 6. API

All read the SQLite file; 503 with a regeneration hint when it does not exist.

| Endpoint | Purpose |
|---|---|
| `GET /api/history/catalog` | buildings → floors → zones, span, row counts, public config, pairs, metrics |
| `GET /api/history?building_id=&level=building\|floor\|zone&floor_id=&zone_id=&range=1h\|6h\|24h\|7d\|30d&start_time=&end_time=&interval=auto\|5min\|15min\|1h\|3h\|1d&metrics=` | aggregated series + weather (+ PV/grid/forecast at building level), ≤ 1000 points |
| `GET /api/history/compare?pair=…` | relationship with Pearson r, or `current_vs_historical_demand` hourly profile |
| `GET /api/history/anomalies?building_id=&start_time=&end_time=` | anomaly metadata |
| `GET /api/history/quality?building_id=&zone_id=&range=` | raw vs clean + flag counts |
| `GET /api/history/export?format=csv\|json&layer=zone\|building\|raw\|clean&…` | download (≤ 50 000 rows) |

`range` is relative to the dataset **end**. Auto interval: ≤ 6 h → 5 min, ≤ 24 h → 15 min,
≤ 7 d → 1 h, ≤ 31 d → 3 h.

## 7. Known limitations

- One 5-zone floor plate per floor (zones per floor fixed at 5); floors are thermally independent.
- The BMS schedule is not the live ConstraintAware controller (no complaints in history).
- Humidity dehumidification is not charged to kWh (inherited twin assumption).
- PMV uses assumed clothing, metabolic rate, air speed and radiant temperature.
- One site weather shared by all buildings; no wind or rain effect on infiltration.
- Supply/return air temperatures, damper and fan percentages are simplified observables.
