"""Internal loads for the LIVE twin (Phase 5): the Phase-2 load model (backend/dataset/loads.py)
wired into the twin's gain_fn and capacity_w hooks — one load model, two consumers.

Lighting, plug and equipment/IT watts are electrical power AND sensible heat. The twin's own
occupancy (100 W per person) is unchanged; these gains come on top. HVAC capacity is sized
for the profile's peak loads (the dataset's sizing rule), never below the twin's own units.
A data centre therefore carries a large IT load with very few people.
"""
from __future__ import annotations

from backend import building
from backend.dataset import loads
from backend.dataset.weather import CLIMATE
from sim.twin import _OCC_PEAK, ZONES


def zone_gains(profile, zone_id: str, occ: float, t: float, ghi: float) -> dict:
    """Watts by kind for one zone at sim time t (sim seconds since Monday)."""
    h = (t % 86400) / 3600.0
    open_now = building.is_open(profile, int(t // 86400) % 7, h)
    frac = occ / (_OCC_PEAK.get(zone_id) or 1)
    return {"lighting": loads.lighting_w(profile, zone_id, frac, open_now, ghi),
            "plug": loads.plug_w(profile.building_type, zone_id, frac),
            "equipment": loads.equipment_w(profile.building_type, zone_id, open_now)}


def gain_fn(twin, profile, ws):
    def g(zone_id, t):
        ghi = ws.at(t)["solar_irradiance_w_m2"] if ws is not None else 0.0
        return sum(zone_gains(profile, zone_id, twin.occupancy_now(zone_id), t, ghi).values())
    return g


def capacities(profile, climate: str) -> dict:
    p = CLIMATE[climate].values()
    t_hot = max(x["t_mean"] + x["t_amp"] for x in p) + 4.0
    t_cold = min(x["t_mean"] - x["t_amp"] for x in p) - 3.0
    return {z.id: loads.design_capacities(profile, profile.building_type, z.id, _OCC_PEAK[z.id], t_hot, t_cold)[0]
            for z in ZONES}
