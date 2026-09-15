"""Building-type occupancy model (SIMULATED), precomputed per sample slot.

Base curve = the Phase-1 profile schedule (backend.building.expected_fraction, which
already applies operating days/hours and closed_factor). On top of it, per type:
  * zone-specific patterns (cafeteria lunch, hotel guest rooms at night, lobby
    transient) as anchor curves that REPLACE the building curve for that zone,
  * a weekend factor (mall busier, office/college near-empty),
  * the holiday/event calendar factor,
  * a per-day level factor N(1, sd) and a per-zone AR(1) multiplicative wobble.
Capacity per zone = floor area / occupant density for the building type, so a
headcount always fits the physical zone the twin models.
Counts are integers in [0, capacity]. Expected occupancy is the same product
WITHOUT the random terms.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from backend import building
from sim.twin import ZONE_BY_ID

DENSITY_M2_PER_PERSON = {"office": 9.0, "mall": 5.0, "hospital": 10.0, "hotel": 15.0,
                         "college": 3.5, "data_center": 40.0}
WEEKEND_FACTOR = {"office": 1.0, "mall": 1.35, "hospital": 0.9, "hotel": 1.15,
                  "college": 1.0, "data_center": 1.0}    # office/college closed via open_days
DAY_SD = {"office": 0.08, "mall": 0.15, "hospital": 0.06, "hotel": 0.12,
          "college": 0.1, "data_center": 0.05}

# type -> zone -> hourly anchors (fraction of zone capacity on an open day)
ZONE_PATTERNS = {
    "office": {"zone_e": {0: 0.0, 7: 0.0, 8: 0.15, 10: 0.1, 12: 0.9, 14: 0.8, 15: 0.15, 18: 0.05, 19: 0.0},
               "zone_d": {0: 0.02, 7: 0.05, 9: 0.35, 12: 0.25, 17: 0.4, 19: 0.1, 21: 0.02}},
    "mall": {"zone_e": {0: 0.0, 10: 0.1, 13: 0.95, 15: 0.5, 19: 1.0, 21: 0.7, 22: 0.1, 23: 0.0}},
    "hospital": {"zone_b": {0: 0.05, 8: 0.2, 9: 0.9, 13: 0.7, 17: 0.8, 19: 0.2, 21: 0.05},
                 "zone_d": {0: 0.35, 8: 0.5, 11: 0.8, 18: 0.7, 23: 0.4}},
    "hotel": {"zone_a": {0: 0.85, 7: 0.75, 10: 0.3, 16: 0.35, 20: 0.7, 22: 0.9},
              "zone_c": {0: 0.9, 8: 0.7, 11: 0.2, 17: 0.3, 21: 0.8},
              "zone_e": {0: 0.0, 6: 0.1, 8: 0.8, 10: 0.2, 12: 0.7, 14: 0.2, 19: 0.3, 20: 0.9, 22: 0.2, 23: 0.0}},
    "college": {"zone_e": {0: 0.0, 8: 0.1, 11: 0.3, 13: 1.0, 14: 0.4, 16: 0.5, 18: 0.0}},
    "data_center": {"zone_a": {0: 0.1, 9: 0.3, 17: 0.25, 20: 0.1},
                    "zone_c": {0: 0.0, 10: 0.15, 16: 0.0}},
}


def zone_capacity(building_type: str, zone_id: str) -> int:
    return max(1, int(ZONE_BY_ID[zone_id].area / DENSITY_M2_PER_PERSON[building_type]))


def _anchor(anchors: dict, hour: float) -> float:
    pts = sorted(anchors.items())
    for (h0, v0), (h1, v1) in zip(pts, pts[1:] + [(pts[0][0] + 24, pts[0][1])]):
        if h0 <= hour < h1:
            return v0 + (v1 - v0) * (hour - h0) / (h1 - h0)
    return pts[-1][1]


def status_for(count: int, capacity: int) -> str:
    if count <= 0:
        return "EMPTY"
    f = count / capacity
    if f >= 1.0:
        return "AT_CAPACITY"
    if f >= 0.75:
        return "HIGH"
    if f >= 0.3:
        return "MODERATE"
    return "LOW"


class OccupancyModel:
    """Precomputed counts per (zone, slot). `count(zone, ts)` is O(1)."""

    def __init__(self, cfg: building.BuildingConfig, zone_ids: list, start: datetime,
                 days: int, step_min: int, calendar, seed: int, key: str):
        self.cfg, self.start, self.step_min = cfg, start, step_min
        self.type = cfg.building_type
        self.capacity = {z: zone_capacity(self.type, z) for z in zone_ids}
        rng = random.Random(f"occupancy:{seed}:{key}")
        n = days * 24 * 60 // step_min + 2
        self.expected: dict = {z: [0.0] * n for z in zone_ids}
        self.counts: dict = {z: [0] * n for z in zone_ids}
        self.labels: list = [None] * n
        self.spikes: list = []                 # (t0, t1, zone, factor) from anomalies
        day_level, ar = {}, {z: 0.0 for z in zone_ids}
        for i in range(n):
            ts = start + timedelta(minutes=i * step_min)
            h = ts.hour + ts.minute / 60.0
            d = ts.date()
            if d not in day_level:
                day_level[d] = max(0.3, rng.gauss(1.0, DAY_SD[self.type]))
            base = building.expected_fraction(cfg, ts.weekday(), h)
            open_now = building.is_open(cfg, ts.weekday(), h)
            wk = WEEKEND_FACTOR[self.type] if ts.weekday() >= 5 else 1.0
            cal_f, label = calendar.occupancy_factor(ts, self.type)
            self.labels[i] = label
            for z in zone_ids:
                pat = ZONE_PATTERNS.get(self.type, {}).get(z)
                if pat is not None:
                    frac = _anchor(pat, h) * (1.0 if open_now else cfg.closed_factor)
                    frac *= cfg.expected_occupancy_pct / 100.0
                else:
                    frac = base
                exp = min(1.0, frac * wk * cal_f)
                ar[z] = 0.85 * ar[z] + rng.gauss(0, 0.06)
                noisy = exp * day_level[d] * math.exp(ar[z])
                cap = self.capacity[z]
                self.expected[z][i] = round(exp * cap, 2)
                self.counts[z][i] = max(0, min(cap, int(round(noisy * cap))))

    def slot(self, ts: datetime) -> int:
        return max(0, min(len(self.labels) - 1,
                          int((ts - self.start).total_seconds() // (self.step_min * 60))))

    def apply_spike(self, t0: datetime, t1: datetime, zone: str, factor: float) -> None:
        """Occupancy-spike anomaly: multiply counts in the window (still <= capacity)."""
        i0, i1 = self.slot(t0), self.slot(t1)
        cap = self.capacity[zone]
        for i in range(i0, i1 + 1):
            self.counts[zone][i] = min(cap, max(self.counts[zone][i] + 2,
                                                int(round(self.counts[zone][i] * factor))))

    def count(self, zone: str, ts: datetime) -> int:
        return self.counts[zone][self.slot(ts)]

    def expected_count(self, zone: str, ts: datetime) -> float:
        return self.expected[zone][self.slot(ts)]
