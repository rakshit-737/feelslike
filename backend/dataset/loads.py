"""Non-HVAC electrical loads, rooftop PV and HVAC sizing (SIMULATED / DERIVED).

Every lighting, plug and equipment watt is ALSO a sensible heat gain handed to the
twin (DigitalTwin gain_fn), so more equipment -> more cooling -> more HVAC power.
Densities are indicative (ASHRAE 90.1 / CIBSE Guide F order of magnitude).
"""
from __future__ import annotations

from backend import building
from sim.twin import ADJACENCY, COP, FAN_W, VENT_UA, ZONE_BY_ID

PLUG_W_M2 = {"office": 10.0, "mall": 5.0, "hospital": 12.0, "hotel": 6.0, "college": 6.0, "data_center": 8.0}
EQUIP_W_M2 = {"office": 2.0, "mall": 8.0, "hospital": 15.0, "hotel": 5.0, "college": 3.0, "data_center": 5.0}
IT_W_M2 = 400.0                    # data-centre white space (zone_a) IT load density
PV_PERFORMANCE_RATIO = 0.80
PV_TEMP_COEFF = -0.004             # per K above 25 degC cell temperature
DESIGN_MARGIN = 1.25


def lighting_w(cfg, zone_id: str, occ_frac: float, is_open: bool, ghi: float) -> float:
    """Phase-1 lighting estimate with daylight dimming (up to 30 % at 600 W/m2)."""
    w = building.lighting_w(cfg, zone_id, occ_frac, is_open)
    return w * (1.0 - 0.3 * min(1.0, ghi / 600.0))


def plug_w(btype: str, zone_id: str, occ_frac: float) -> float:
    return PLUG_W_M2[btype] * ZONE_BY_ID[zone_id].area * (0.25 + 0.75 * min(1.0, occ_frac))


def equipment_w(btype: str, zone_id: str, is_open: bool) -> float:
    area = ZONE_BY_ID[zone_id].area
    if btype == "data_center":
        return (IT_W_M2 if zone_id == "zone_a" else EQUIP_W_M2[btype]) * area
    duty = {"hospital": 0.85, "mall": 1.0 if is_open else 0.35, "hotel": 0.7,
            "office": 1.0 if is_open else 0.4, "college": 1.0 if is_open else 0.3}[btype]
    return EQUIP_W_M2[btype] * area * duty


def pv_kw(kwp: float, ghi: float, t_amb: float) -> float:
    if kwp <= 0 or ghi <= 0:
        return 0.0
    t_cell = t_amb + ghi / 800.0 * 20.0
    return max(0.0, kwp * ghi / 1000.0 * PV_PERFORMANCE_RATIO * (1 + PV_TEMP_COEFF * (t_cell - 25.0)))


def design_capacities(cfg, btype: str, zone_id: str, capacity_people: int,
                      t_hot: float, t_cold: float) -> tuple:
    """(cooling W, heating W) thermal capacity sized for the zone's peak loads x margin,
    never below the twin's own Zone.max_cool. Heating covers envelope + neighbours'
    worst case at t_cold."""
    z = ZONE_BY_ID[zone_id]
    ua = z.UA + sum(g for (a, b), g in ADJACENCY.items() if zone_id in (a, b)) * 0.2
    peak = (ua * max(0.0, t_hot - cfg.comfort_max_c) + VENT_UA * 2 * max(0.0, t_hot - cfg.comfort_max_c)
            + z.solar_peak + 100.0 * capacity_people
            + building.lighting_w(cfg, zone_id, 1.0, True) + plug_w(btype, zone_id, 1.0)
            + equipment_w(btype, zone_id, True))
    cool = max(z.max_cool, DESIGN_MARGIN * peak)
    heat = DESIGN_MARGIN * (ua + VENT_UA) * max(0.0, cfg.comfort_min_c - t_cold)
    return cool, heat


def design_electrical_kw(cool_w: float, heat_w: float, cfg, btype: str, zone_id: str) -> float:
    """Zone electrical design demand for peak-demand-risk normalisation."""
    return (max(cool_w / COP, heat_w / 3.0) + FAN_W[2]
            + building.lighting_w(cfg, zone_id, 1.0, True) + plug_w(btype, zone_id, 1.0)
            + equipment_w(btype, zone_id, True)) / 1000.0
