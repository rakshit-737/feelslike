"""Rare, configurable anomaly schedule. Every anomaly is a labelled record; the
generator applies PHYSICAL anomalies to the simulation (so their consequences show
up downstream) and SENSOR anomalies only to the RAW layer (the truth is untouched).

  type                 layer     effect
  occupancy_spike      physical  zone headcount x1.6-2.2 (capped at capacity)
  hvac_failure         physical  zone cooling AND heating capacity -> 0
  co2_spike            physical  extra CO2 generation (x3-6 people-equivalent)
  abnormal_energy      physical  +20-40 W/m2 equipment load (heat + power)
  weather_disturbance  physical  site-wide outdoor temperature +/-3-5 K (all buildings)
  temp_sensor_fault    sensor    raw temperature offset +2.5..4 K or stuck value
  communication_gap    sensor    raw readings missing for the zone
"""
from __future__ import annotations

import math
import random
from datetime import timedelta

PHYSICAL = {"occupancy_spike", "hvac_failure", "co2_spike", "abnormal_energy", "weather_disturbance"}
SENSOR = {"temp_sensor_fault", "communication_gap"}
DURATION_H = {"occupancy_spike": (1, 3), "hvac_failure": (2, 8), "co2_spike": (0.5, 2),
              "abnormal_energy": (3, 12), "weather_disturbance": (6, 24),
              "temp_sensor_fault": (6, 36), "communication_gap": (0.5, 4)}


def _poisson(rng, lam: float) -> int:
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def schedule(cfg, zones_by_building: dict) -> list:
    """-> list of anomaly dicts (metadata + params), deterministic for the seed."""
    ac = cfg.anomalies
    if not ac.enabled or ac.rate_per_building_day <= 0:
        return []
    rng = random.Random(f"anomalies:{cfg.seed}")
    start, days = cfg.start_dt, int(cfg.days)
    types = [t for t in ac.types if t != "weather_disturbance"]
    out = []

    def add(btype, bid, zone):
        lo, hi = DURATION_H[btype]
        t0 = start + timedelta(minutes=rng.randrange(0, days * 1440 - 60))
        t1 = min(start + timedelta(days=days), t0 + timedelta(hours=rng.uniform(lo, hi)))
        sev = rng.choice(("low", "medium", "high"))
        mag = {"low": 0.0, "medium": 0.5, "high": 1.0}[sev]
        params = {
            "occupancy_spike": {"factor": round(1.6 + 0.6 * mag, 2)},
            "hvac_failure": {"capacity_factor": 0.0},
            "co2_spike": {"extra_people_equiv": round(3 + 3 * mag, 1)},
            "abnormal_energy": {"extra_w_m2": round(20 + 20 * mag, 1)},
            "weather_disturbance": {"delta_c": round(rng.choice((-1, 1)) * (3 + 2 * mag), 1)},
            "temp_sensor_fault": {"offset_c": round(2.5 + 1.5 * mag, 2), "stuck": rng.random() < 0.3},
            "communication_gap": {},
        }[btype]
        desc = {
            "occupancy_spike": f"Unexpected occupancy spike x{params.get('factor')}",
            "hvac_failure": "HVAC unit failure: no cooling or heating capacity",
            "co2_spike": "CO2 generation spike (crowding / poor ventilation)",
            "abnormal_energy": f"Abnormal equipment load +{params.get('extra_w_m2')} W/m2",
            "weather_disturbance": f"Outdoor temperature disturbance {params.get('delta_c', 0):+} K",
            "temp_sensor_fault": ("Temperature sensor stuck" if params.get("stuck")
                                  else f"Temperature sensor offset +{params.get('offset_c')} K"),
            "communication_gap": "Sensor communication gap (no readings)",
        }[btype]
        out.append({"anomaly_id": f"anm-{len(out) + 1:04d}", "anomaly_type": btype,
                    "severity": sev, "start_time": t0.isoformat(timespec="minutes"),
                    "end_time": t1.isoformat(timespec="minutes"), "building_id": bid,
                    "affected_zone": zone, "layer": "sensor" if btype in SENSOR else "physical",
                    "description": desc, "params": params, "_t0": t0, "_t1": t1})

    for bid, zones in zones_by_building.items():
        for _ in range(_poisson(rng, ac.rate_per_building_day * days)):
            if types:
                add(rng.choice(types), bid, rng.choice(zones))
    if "weather_disturbance" in ac.types:
        for _ in range(_poisson(rng, ac.rate_per_building_day * days * 0.25)):
            add("weather_disturbance", "*", "*")
    out.sort(key=lambda a: a["_t0"])
    for i, a in enumerate(out, 1):
        a["anomaly_id"] = f"anm-{i:04d}"
    return out


class ActiveIndex:
    """Fast 'which anomalies are active here now' lookups."""

    def __init__(self, anomalies: list):
        self.by_key: dict = {}
        for a in anomalies:
            self.by_key.setdefault((a["building_id"], a["affected_zone"], a["anomaly_type"]), []).append(a)

    def get(self, bid: str, zone: str, atype: str, ts):
        for a in self.by_key.get((bid, zone, atype), ()):
            if a["_t0"] <= ts < a["_t1"]:
                return a
        return None
