# Seasonal simulation (Phase 5)

Code: `backend/scenario.py`, `backend/loads_bridge.py`, `sim/twin.py` (hooks), `backend/app.py`
(`/api/scenario*`), `dashboard/scenario.js` (Scenario tab). Tests: `tests/test_scenario.py`.

## 1. Causal chain

```
 OUTSIDE WEATHER  (seasonal WeatherModel + operator override layer)
       │ outdoor T, RH, solar irradiance, cloud, rain, wind           twin hooks: weather_fn, rh_fn, solar_fn
       ▼
 BUILDING ENVELOPE  UA × envelope_scale × (T_out − T_zone)  +  solar_peak × facade factor × solar_scale
       ▼
 INDOOR ENVIRONMENT  the existing 5-zone RC twin (60-s physics, moisture balance)
       ▲
 OCCUPANCY + INTERNAL LOAD  100 W/person (twin) [+ lighting / plugs / equipment-IT of the profile]
       ▼
 HVAC RESPONSE  ConstraintAware (unchanged) — capacity-limited cooling, fans
       ▼
 COMFORT (Phase 3 engine) ─> ENERGY / DEMAND ─> LATEST TELEMETRY (Phase 4) ─> DASHBOARD
```

No new physics engine: every arrow is an existing mechanism, now driven by selectable weather.
Both twins (FeelsLike and the static baseline) always get the same scenario, so the A/B
comparison stays fair.

## 2. Weather models

| Model | What drives the twin | Irradiance / cloud / rain / wind |
|---|---|---|
| **classic** (default) | `sim/weather.py` August model — exactly the pre-Phase-5 twin (bit-for-bit, tested) | not produced; reported "not in the classic weather model" |
| **seasonal** | `backend/dataset/weather.py` WeatherModel (Phase 2): AR(1) temperature and dew point, rain **events**, cloud persistence, Haurwitz clear-sky solar × cloud × rain | all produced |

Seasons: `summer`, `monsoon`, `winter`, `transition` (a fixed season, anchored on a
representative Monday: 18 May, 20 Jul, 12 Jan, 19 Oct) or `auto` (IMD months from 14 Sep,
advancing with sim time). Climates: `delhi`, `chennai`. The model repeats every 28 days.

## 3. Controls (all bounded, applied to both twins)

| Control | Range | Mechanism |
|---|---|---|
| Outdoor temperature offset | −10..+10 °C | existing `outdoor_offset` knob |
| Humidity offset | −30..+30 %RH | existing `humidity_offset` knob |
| Solar gain scale | 0..2 | existing `solar_scale` knob |
| Occupancy scale | 0..3 | existing `occ_scale` knob |
| HVAC capacity scale | 0.1..1.5 | existing `capacity_scale` knob |
| Cloud cover override | 0..100 % or model | override layer: recomputes irradiance and condition |
| Rain override | 0..50 mm/h or model | override layer: condition, irradiance × 0.35 |
| Weather disturbance | −8..+8 °C for 0.25..48 h | override layer from "now" |
| Envelope conductance scale | 0.5..2 | new twin hook `envelope_scale` (1.0 = unchanged) |
| Internal loads | people_only / profile | `loads_bridge`: Phase-2 lighting + plug + equipment/IT as sensible gains; HVAC capacity sized for them |
| Speed presets | 0.25× … 10× (60 … 2400 sim-s/s) | existing `/api/speed` |

Overrides never modify the weather model; **Reset scenario** returns to classic weather,
default knobs, no overrides and clears simulated-sensor faults — without rebuilding the
building or touching history, configuration or the simulated-hardware on/off choice.

## 4. Seasonal physics — what is and is not modelled

- **Summer:** higher outdoor T → more envelope and ventilation heat → more cooling → more HVAC
  power and kWh; with `capacity_scale` down the coil saturates (`at_capacity`, cooling 100 %) and
  the room warms, comfort degrades (tested).
- **Monsoon:** high dew point → higher indoor RH; cloud and rain events cut irradiance (and
  PV in the dataset); condition changes.
- **Winter:** lower outdoor T → lower cooling demand. **Heating is NOT MODELLED in the live twin:**
  the twin physics has a heating hook (used by the Phase-2 dataset's BMS), but the live
  ConstraintAware controller is cooling-only, so cold rooms show up as cold — never as invented
  heating energy. Every API and card says "NOT MODELLED".
- **Transition:** moderate demand.
- **Humidity model:** classic mode keeps the documented coil ADP approximation (setpoint − 2 K;
  indoor RH pinned high). Seasonal mode uses the dataset's fixed 12 °C apparatus dew point so
  humidity responds plausibly. The active model is shown in the UI.
- **CO₂:** unchanged telemetry mass balance (occupancy × generation vs outdoor-air flow from the
  twin's fan level and infiltration).
- **Solar:** the facade factor from the seasonal irradiance replaces the classic facade curve —
  not added to it, so solar is not double-counted.

## 5. Causal explanation and season comparison

`scenario.causal(telemetry)` reads the step-cadence buffer now vs 15 sim-min ago: outdoor T,
occupancy %, cooling W, highest zone capacity %, HVAC W, occupied indoor T — values and
deltas — plus zones at capacity and kWh per sim-hour, and builds one sentence from them.

`/api/scenario/compare?horizon_h=` steps four clones of the live twin (same start state, same
controller, current envelope/loads/knobs) under each season and reports outdoor/indoor T,
indoor RH, mean cooling, max capacity %, HVAC power, kWh, occupied comfort, peak demand,
zone-minutes at capacity, irradiance and rain hours; heating "NOT MODELLED". Labelled
SIMULATED / WHAT-IF. The first hours carry the starting indoor state.

## 6. Physical-consistency tests (controlled scenarios, not per-step monotonicity)

summer > transition > winter cooling and kWh · monsoon indoor RH > summer · +6 K offset → more
cooling, power, kWh · leaky envelope > tight envelope cooling · clear sky > overcast cooling ·
cloudier middays → lower irradiance · rain → condition + lower irradiance · occ ×2 → more
cooling and CO₂ · fan 2 → lower CO₂ than fan 0 at equal occupancy · data-centre IT load at
01:00 with nobody in → > 3× cooling · capacity 15 % on a hot day → at-capacity minutes and a
warmer room · classic mode bit-identical to the original twin · frozen 7-day numbers exact.

## 7. Limitations

One site weather for the live building; floors and zones beyond the 5 modelled remain NOT
MODELLED; no wind or rain effect on infiltration; no heating control in the live twin;
seasonal anchors are fixed dates; internal-load "profile" mode keeps the twin's office
occupancy schedules.
