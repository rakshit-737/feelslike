"""BMS schedule controller for the historical dataset + HVAC observables (SIMULATED).

Why not ConstraintAware: the live controller is office-tuned and cooling-only, and it
answers occupant complaints — there are none in generated history. A commercial BMS
runs a profile schedule, so this is that schedule, driven by the SAME Phase-1 profile:
  occupied / open:  cooling SP = comfort_max - 1.0, heating SP = comfort_min + 0.5
  pre-conditioning: the hour before opening uses the occupied setpoints
  unoccupied:       setback cooling 29 degC, heating 16 degC (only if heating exists)
  data-centre white space: 24 degC around the clock (IT load)
Ventilation (demand-controlled): fan 2 when CO2 >= 80 % of the limit or occupancy
>= 70 % of capacity, or as economizer when outdoor air is >= 3 K cooler than the zone
and the zone needs cooling; fan 1 when occupied/open; fan 0 otherwise.
"""
from __future__ import annotations

from datetime import timedelta

from backend import building
from sim.twin import FAN_W

SETBACK_COOL_C = 29.0
SETBACK_HEAT_C = 16.0
MODES = ("OFF", "COOLING", "HEATING", "VENTILATION", "AUTO", "ECONOMY")


def schedule(cfg, btype: str, zone_id: str, ts, occ: int, capacity: int, co2: float,
             t_in: float, t_out: float) -> tuple:
    """-> (cool_sp or None, heat_sp or None, vent 0|1|2, economizer bool, armed bool)."""
    wd, h = ts.weekday(), ts.hour + ts.minute / 60.0
    open_now = building.is_open(cfg, wd, h)
    ahead = ts + timedelta(hours=1)
    pre = building.is_open(cfg, ahead.weekday(), ahead.hour + ahead.minute / 60.0)
    armed = open_now or pre or occ > 0
    if btype == "data_center" and zone_id == "zone_a":
        cool, heat, armed = 24.0, None, True
    elif armed:
        cool, heat = cfg.comfort_max_c - 1.0, cfg.comfort_min_c + 0.5
    else:
        cool, heat = SETBACK_COOL_C, SETBACK_HEAT_C
    frac = occ / capacity if capacity else 0.0
    vent = 0
    if occ > 0 or open_now:
        vent = 1
    if co2 >= 0.8 * cfg.co2_max_ppm or frac >= 0.7:
        vent = 2
    econ = bool(armed and t_out <= t_in - 3.0 and t_in > cool - 0.5)
    if econ:
        vent = 2
    return cool, heat, vent, econ, armed


def observables(t_in: float, cool_w: float, heat_w: float, fan_w: float, vent: int,
                cool_cap: float, heat_cap: float, econ: bool, armed: bool,
                req_oa_ls: float, max_oa_ls: float) -> dict:
    """HVAC columns from interval-mean thermal/electrical flows (all SIMULATED/DERIVED)."""
    cool_pct = 100.0 * cool_w / cool_cap if cool_cap > 0 else 0.0
    heat_pct = 100.0 * heat_w / heat_cap if heat_cap > 0 else 0.0
    if heat_w > 1.0:
        mode = "HEATING"
    elif econ:
        mode = "ECONOMY"
    elif cool_w > 1.0:
        mode = "COOLING"
    elif fan_w > 1.0:
        mode = "VENTILATION"
    elif armed:
        mode = "AUTO"
    else:
        mode = "OFF"
    # supply air: design delta-T 10 K cooling / +15 K heating at full load, proportional
    sat = t_in - 10.0 * min(1.0, cool_pct / 100.0) + 15.0 * min(1.0, heat_pct / 100.0)
    return {
        "hvac_status": "ON" if (cool_w > 1.0 or heat_w > 1.0 or fan_w > 1.0) else "OFF",
        "hvac_mode": mode,
        "cooling_demand_percent": round(min(100.0, cool_pct), 1),
        "heating_demand_percent": round(min(100.0, heat_pct), 1),
        "ventilation_demand_percent": round(min(100.0, 100.0 * req_oa_ls / max_oa_ls) if max_oa_ls else 0.0, 1),
        "supply_air_temperature_c": round(sat, 2),
        "return_air_temperature_c": round(t_in + 0.5, 2),
        "fan_speed_percent": round(100.0 * fan_w / FAN_W[2], 1),
        "damper_position_percent": {0: 10.0, 1: 50.0, 2: 100.0}[int(vent)],
        "compressor_load_percent": round(min(100.0, cool_pct), 1),
    }
