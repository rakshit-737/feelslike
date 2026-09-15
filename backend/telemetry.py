"""Telemetry store + KPI engine for the Monitor tab.

WHY THIS EXISTS. AnalyticsStore samples every 15 sim-minutes for the analytics
tab (heatmaps, daily energy). A monitoring console needs the twin's OWN cadence
— every 60-second physics step — so a "last 1 hour" view has 60 points, and a
"live" view shows the step that just happened. This module is that finer ring
buffer plus the derived signals a commercial monitoring product shows, each one
with a documented provenance:

  SOURCE TAGS (every series and every KPI carries one — the dashboard renders
  them and never mixes them silently):
    "sim"        the digital twin's own state (sim/twin.py), step-exact
    "derived"    computed here from twin state by a documented formula
                 (CO2 mass balance, comfort score, power). Still simulated:
                 the inputs are twin state, the formula is ours.
    "hardware"   the ESP32 shoebox rig via backend/hardware.py — real sensing,
                 wall-clock, one zone (HW_ZONE) only; absent until a node posts
    "real"       an external live feed (backend/external.py: Open-Meteo)
    "historical" a replayed dataset file (backend/external.py: data/)
    "predicted"  a forward run of the SAME controller on throwaway clones
                 (forecast(), built on the what-if engine's isolation rules)

  NOTHING here feeds the physics. The twin is read, never written. Frozen
  headline numbers are untouched by construction (read-only observer).

CO2 MODEL (derived — the twin carries no CO2 state).  Single-zone mass balance,
the standard ASHRAE 62.1 / Persily form:
        V dC/dt = G_p * n * 1e6  -  Q * (C - C_out)
  C      zone CO2 in ppm (initialised at C_out)
  V      zone air volume, area x CEILING_H (m3) — same volume the moisture
         balance in sim/twin.py uses
  G_p    CO2 generation per person, 0.0052 L/s = 5.2e-6 m3/s (ASHRAE 62.1,
         office activity 1.2 met)
  Q      outdoor-air volume flow, m3/s — from the SAME couplings the twin's
         sensible and moisture balances use: VENT_UA*vent/CP_AIR kg/s plus
         INFIL_ACH air changes/h of the volume, divided by RHO_AIR
  C_out  outdoor CO2, 420 ppm (NOAA global mean, 2024)
It is an ESTIMATE, flagged co2_estimated=True exactly as the adapter contract
in backend/adapters.py already anticipates ("co2_ppm": None, "co2_estimated").
It has NOT been validated against a sensor; the UCI occupancy dataset served by
/api/dataset is the natural validation set (see data/README.md).

COMFORT SCORE (derived).  0..100 per zone, the fraction of "comfort" left:
  thermal term:  100 inside BAND; falls linearly to 0 at 3 degC outside it
  humidity term: -1 point per %RH above RH_HUMID (65 %), floored at -25
  unoccupied zones report the same score (the air does not care), but the KPI
  status treats unoccupied excursions as "info", not "warning".
This is a monitoring heuristic, NOT Fanger PMV/PPD: the twin has no air-speed
or clothing/metabolic model, so PMV would be fabricated precision.

KPI STATUS (normal / warning / critical) uses THRESHOLDS below — one table,
served to the dashboard so the UI never carries its own copy.
"""
from __future__ import annotations

import bisect
from collections import deque

from sim.humidity import outdoor_rh
from sim.twin import (BAND, CEILING_H, COP, CP_AIR, DT, FAN_W, INFIL_ACH,
                      RH_HUMID, RHO_AIR, VENT_UA, ZONE_BY_ID, ZONE_IDS, ZONES,
                      DigitalTwin)

# ------------------------------------------------------------- constants
CO2_OUTDOOR_PPM = 420.0
CO2_GEN_M3S_PER_PERSON = 5.2e-6          # ASHRAE 62.1 App. C, 1.2 met
RETAIN_DAYS = 7.0                        # matches the "last 7 days" filter
CAPACITY = int(RETAIN_DAYS * 86400 / DT) # 10 080 steps
DEFAULT_MAX_POINTS = 360                 # a chart's point budget per request
KPI_PREV_LAG_S = 15 * 60.0               # "previous value" = 15 sim-min ago

WINDOWS = {                              # filter key -> seconds (None = live tail)
    "live": 10 * 60.0,
    "1h": 3600.0,
    "6h": 6 * 3600.0,
    "24h": 24 * 3600.0,
    "7d": 7 * 86400.0,
}

# Each metric: label, unit, direction of "bad", warning + critical bands.
# Bands are [lo, hi] "normal" ranges; outside -> warning; beyond crit -> critical.
THRESHOLDS: dict = {
    "temp":     {"label": "Indoor temperature", "unit": "°C",
                 "normal": [BAND[0], BAND[1]], "critical": [BAND[0] - 2.0, BAND[1] + 2.0],
                 "basis": "sim/twin.py BAND (ASHRAE-ish office band); ±2 °C beyond it is critical"},
    "t_out":    {"label": "Outdoor temperature", "unit": "°C",
                 "normal": [-100, 38.0], "critical": [-100, 42.0],
                 "basis": "heat-stress advisory levels (IMD heat-wave criteria start at 40 °C plains)"},
    "rh":       {"label": "Humidity", "unit": "%",
                 "normal": [30.0, RH_HUMID], "critical": [20.0, 80.0],
                 "basis": "ASHRAE 55 / 62.1 comfort guidance; RH_HUMID from sim/twin.py"},
    "co2":      {"label": "CO₂ (estimated)", "unit": "ppm",
                 "normal": [0, 1000.0], "critical": [0, 1500.0],
                 "basis": "ASHRAE 62.1 ~1000 ppm guideline; 1500 ppm = poor ventilation"},
    "pm2_5":    {"label": "PM2.5", "unit": "µg/m³",
                 "normal": [0, 35.0], "critical": [0, 55.0],
                 "basis": "WHO 2021 24-h interim target 3 / US EPA 'unhealthy for sensitive' step"},
    "pm10":     {"label": "PM10", "unit": "µg/m³",
                 "normal": [0, 50.0], "critical": [0, 100.0],
                 "basis": "WHO 2021 24-h interim targets"},
    "aqi":      {"label": "Air quality index", "unit": "EAQI",
                 "normal": [0, 40.0], "critical": [0, 80.0],
                 "basis": "European AQI bands: <=40 good/fair, >80 poor"},
    "light":    {"label": "Light intensity", "unit": "lux",
                 "normal": [300.0, 2000.0], "critical": [100.0, 5000.0],
                 "basis": "EN 12464-1 office task lighting 300–500 lx"},
    "occ":      {"label": "Occupancy", "unit": "people",
                 "normal": [0, 1e9], "critical": [0, 1e9], "basis": "informational"},
    "occ_pct":  {"label": "Occupancy", "unit": "% of design",
                 "normal": [0, 100.0], "critical": [0, 120.0],
                 "basis": "over design headcount = over ventilation design"},
    "comfort":  {"label": "Thermal comfort", "unit": "/100",
                 "normal": [70.0, 100.0], "critical": [40.0, 100.0],
                 "basis": "derived score, see module docstring"},
    "power_w":  {"label": "HVAC power", "unit": "W",
                 "normal": [0, 1e9], "critical": [0, 1e9], "basis": "informational"},
    "kwh":      {"label": "Energy consumed", "unit": "kWh",
                 "normal": [0, 1e9], "critical": [0, 1e9], "basis": "informational"},
    "capacity_pct": {"label": "HVAC capacity used", "unit": "%",
                 "normal": [0, 90.0], "critical": [0, 100.0],
                 "basis": "≥90 % sustained = no headroom; 100 % = at_capacity"},
}


def status_for(metric: str, value) -> str:
    """normal / warning / critical / unknown from THRESHOLDS. Pure."""
    th = THRESHOLDS.get(metric)
    if th is None or value is None:
        return "unknown"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "unknown"
    lo, hi = th["normal"]
    clo, chi = th["critical"]
    if v < clo or v > chi:
        return "critical"
    if v < lo or v > hi:
        return "warning"
    return "normal"


def comfort_score(temp: float, rh: float) -> float:
    """0..100, see module docstring. Pure."""
    lo, hi = BAND
    dev = (lo - temp) if temp < lo else ((temp - hi) if temp > hi else 0.0)
    thermal = max(0.0, 100.0 * (1.0 - dev / 3.0))
    humid = max(-25.0, -max(0.0, rh - RH_HUMID))
    return round(max(0.0, min(100.0, thermal + humid)), 1)


def _outdoor_air_m3s(zone, vent: int) -> float:
    """Outdoor-air volume flow for a zone at a vent level — the twin's couplings."""
    m_vent = VENT_UA * int(vent) / CP_AIR                          # kg/s
    m_inf = INFIL_ACH * (zone.area * CEILING_H) * RHO_AIR / 3600.0  # kg/s
    return (m_vent + m_inf) / RHO_AIR                              # m3/s


class TelemetryStore:
    """Step-cadence ring buffer of the live twin plus derived signals.

    INPUT: record(twin, base_twin, zone_power_w, zone_cool_w) once per physics
      step, from the sim loop (caller holds the sim lock).
    OUTPUT: series(...) / kpis(...) / latest() — json-safe, copies only.
    SIDE EFFECTS: own buffers only; owns the CO2 state per zone.
    ERROR STATES: none by design; an empty store yields empty shapes.
    """

    def __init__(self, capacity: int = CAPACITY):
        self.capacity = int(capacity)
        self.rows: deque = deque(maxlen=self.capacity)
        self.ts: deque = deque(maxlen=self.capacity)   # parallel, for bisect
        self.co2 = {z: CO2_OUTDOOR_PPM for z in ZONE_IDS}
        self._last_t: float | None = None

    def __len__(self) -> int:
        return len(self.rows)

    def clear(self) -> None:
        self.rows.clear()
        self.ts.clear()
        self.co2 = {z: CO2_OUTDOOR_PPM for z in ZONE_IDS}
        self._last_t = None

    # ------------------------------------------------------------ ingest
    def record(self, twin: DigitalTwin, base_twin=None, zone_power_w=None,
               zone_cool_w=None, active_by_zone=None) -> dict:
        """Append one row for the step the twin just took. CALLER HOLDS THE LOCK."""
        t = float(twin.t)
        if self._last_t is not None and t < self._last_t:
            self.clear()                              # twin restarted
        dt = DT if self._last_t is None else max(0.0, min(t - self._last_t, 10 * DT))
        self._last_t = t
        t_out = float(twin.weather_fn(t)) + twin.outdoor_offset
        rh_out = min(100.0, max(0.0, outdoor_rh(t, twin.seed) + twin.humidity_offset))

        zones = {}
        p_total = 0.0
        for z in ZONES:
            temp = float(twin.T[z.id])
            rh = float(twin.rh_now(z.id))
            occ = int(twin.occupancy_now(z.id))
            vent = int(twin.last_vents.get(z.id, 0))
            # --- CO2 mass balance (derived), explicit Euler over the step ---
            V = z.area * CEILING_H
            Q = _outdoor_air_m3s(z, vent)
            C = self.co2[z.id]
            dC = (CO2_GEN_M3S_PER_PERSON * occ * 1e6 - Q * (C - CO2_OUTDOOR_PPM)) / V
            C = max(CO2_OUTDOOR_PPM, C + dC * dt)
            self.co2[z.id] = C
            pw = float((zone_power_w or {}).get(z.id, 0.0))
            cw = float((zone_cool_w or {}).get(z.id, 0.0))
            cap = max(1e-6, z.max_cool * twin.capacity_scale)
            p_total += pw
            zones[z.id] = {
                "temp": round(temp, 2), "rh": round(rh, 1), "occ": occ,
                "occ_pct": round(100.0 * occ / _OCC_PEAK[z.id], 1),
                "setpoint": twin.last_setpoints.get(z.id), "vent": vent,
                "co2": round(C, 0),
                "comfort": comfort_score(temp, rh),
                "power_w": round(pw, 1), "cool_w": round(cw, 1),
                "capacity_pct": round(min(100.0, 100.0 * cw / cap), 1),
                "at_capacity": bool(twin._at_cap.get(z.id, False)),
                "kwh": round(float(twin.kwh_by_zone.get(z.id, 0.0)), 4),
                "base_temp": round(float(base_twin.T[z.id]), 2) if base_twin is not None else None,
                "constraints": int((active_by_zone or {}).get(z.id, 0)),
            }
        occ_all = sum(v["occ"] for v in zones.values())
        row = {
            "t": t, "t_out": round(t_out, 2), "rh_out": round(rh_out, 1),
            "kwh_us": round(float(twin.kwh), 4),
            "kwh_base": round(float(getattr(base_twin, "kwh", 0.0)), 4) if base_twin is not None else None,
            "power_w": round(p_total, 1),
            "base_power_w": round(float(getattr(base_twin, "last_power_w", 0.0)), 1) if base_twin is not None else None,
            "viol_us": round(float(twin.viol_min), 1),
            "viol_base": round(float(getattr(base_twin, "viol_min", 0.0)), 1) if base_twin is not None else None,
            "occ": occ_all,
            "zones": zones,
        }
        self.rows.append(row)
        self.ts.append(t)
        return row

    # ------------------------------------------------------------ readers
    def latest(self) -> dict | None:
        return dict(self.rows[-1]) if self.rows else None

    def at_or_before(self, t: float) -> dict | None:
        """The newest row with row.t <= t (None if none)."""
        if not self.ts:
            return None
        i = bisect.bisect_right(self.ts, t) - 1
        return self.rows[i] if i >= 0 else None

    def _slice(self, t_from: float | None, t_to: float | None) -> list:
        if not self.ts:
            return []
        lo = 0 if t_from is None else bisect.bisect_left(self.ts, t_from)
        hi = len(self.ts) if t_to is None else bisect.bisect_right(self.ts, t_to)
        rows = list(self.rows)
        return rows[lo:hi]

    @staticmethod
    def _building_point(r: dict) -> dict:
        zs = r["zones"].values()
        n = max(1, len(r["zones"]))
        occ_zones = [z for z in zs if z["occ"] > 0] or list(zs)
        return {
            "t": r["t"], "t_out": r["t_out"], "rh_out": r["rh_out"],
            "temp": round(sum(z["temp"] for z in occ_zones) / len(occ_zones), 2),
            "rh": round(sum(z["rh"] for z in occ_zones) / len(occ_zones), 1),
            "co2": round(max(z["co2"] for z in zs), 0),
            "comfort": round(sum(z["comfort"] for z in occ_zones) / len(occ_zones), 1),
            "occ": r["occ"],
            "occ_pct": round(100.0 * r["occ"] / _OCC_PEAK_TOTAL, 1),
            "power_w": r["power_w"], "base_power_w": r["base_power_w"],
            "kwh": r["kwh_us"], "kwh_base": r["kwh_base"],
            "base_temp": (round(sum(z["base_temp"] for z in occ_zones) / len(occ_zones), 2)
                          if all(z["base_temp"] is not None for z in occ_zones) else None),
            "capacity_pct": round(max(z["capacity_pct"] for z in zs), 1),
            "vent": max(z["vent"] for z in zs),
            "setpoint": None,
            "constraints": sum(z["constraints"] for z in zs),
        }

    @staticmethod
    def _zone_point(r: dict, zid: str) -> dict:
        z = r["zones"][zid]
        return {"t": r["t"], "t_out": r["t_out"], "rh_out": r["rh_out"], **z,
                "kwh_base": r["kwh_base"], "base_power_w": r["base_power_w"]}

    def series(self, zone: str = "all", window: str = "24h",
               t_from: float | None = None, t_to: float | None = None,
               max_points: int = DEFAULT_MAX_POINTS) -> dict:
        """Downsampled points for one zone (or the building) over a window.

        INPUT: zone "all" or a zone id; window a WINDOWS key or "custom" (then
          t_from/t_to in sim seconds); max_points caps the payload — points are
          bucket-averaged (numeric fields) so a 7-day view is 360 rows, not 10k.
        OUTPUT: {"zone","window","t_from","t_to","step_s","points":[...],
                 "source": "sim"|"derived" per field in "fields", "count_raw"}.
        SIDE EFFECTS: none. ERROR STATES: KeyError for an unknown zone id.
        """
        if zone != "all" and zone not in ZONE_BY_ID:
            raise KeyError(zone)
        if not self.rows:
            return {"zone": zone, "window": window, "t_from": None, "t_to": None,
                    "step_s": DT, "points": [], "count_raw": 0, "fields": FIELD_SOURCES}
        t_end = self.ts[-1]
        if window == "custom":
            f = t_from if t_from is not None else self.ts[0]
            e = t_to if t_to is not None else t_end
        else:
            span = WINDOWS.get(window, WINDOWS["24h"])
            f, e = t_end - span, t_end
        rows = self._slice(f, e)
        mk = (lambda r: self._zone_point(r, zone)) if zone != "all" else self._building_point
        pts = [mk(r) for r in rows]
        n = len(pts)
        mp = max(2, int(max_points))
        if n > mp:
            pts = _bucket(pts, mp)
        return {"zone": zone, "window": window, "t_from": f, "t_to": e,
                "step_s": (e - f) / max(1, len(pts) - 1) if len(pts) > 1 else DT,
                "points": pts, "count_raw": n, "fields": FIELD_SOURCES}

    def kpis(self, zone: str = "all", lag_s: float = KPI_PREV_LAG_S) -> dict:
        """Current vs previous (lag_s ago) for every metric with a status.

        OUTPUT: {"t","prev_t","lag_s","zone","items":{metric:{value,prev,
          delta,delta_pct,status,unit,label,source}}}. Empty items before the
          first step. SIDE EFFECTS: none.
        """
        cur = self.latest()
        if cur is None:
            return {"t": None, "prev_t": None, "lag_s": lag_s, "zone": zone, "items": {}}
        prev = self.at_or_before(cur["t"] - lag_s)
        pc = self._building_point(cur) if zone == "all" else self._zone_point(cur, zone)
        pp = None
        if prev is not None and prev is not cur:
            pp = self._building_point(prev) if zone == "all" else self._zone_point(prev, zone)
        items = {}
        for m in ("temp", "t_out", "rh", "co2", "comfort", "occ", "occ_pct",
                  "power_w", "kwh", "capacity_pct"):
            v = pc.get(m)
            p = pp.get(m) if pp else None
            th = THRESHOLDS[m]
            d = (v - p) if (v is not None and p is not None) else None
            items[m] = {
                "value": v, "prev": p, "delta": None if d is None else round(d, 2),
                "delta_pct": (None if d is None or not p else round(100.0 * d / abs(p), 1)),
                "status": status_for(m, v) if not (m == "temp" and pc.get("occ") == 0 and zone != "all") else "info",
                "unit": th["unit"], "label": th["label"], "source": FIELD_SOURCES.get(m, "sim"),
            }
        items["hvac"] = {
            "value": {"vent": pc.get("vent"), "setpoint": pc.get("setpoint"),
                      "capacity_pct": pc.get("capacity_pct"),
                      "cooling": bool((pc.get("cool_w") or 0) > 0 or (pc.get("power_w") or 0) > 0)},
            "prev": None, "delta": None, "delta_pct": None,
            "status": status_for("capacity_pct", pc.get("capacity_pct")),
            "unit": "", "label": "HVAC status", "source": "sim",
        }
        return {"t": cur["t"], "prev_t": prev["t"] if prev else None, "lag_s": lag_s,
                "zone": zone, "items": items}

    def alerts(self, zone: str = "all") -> list:
        """Threshold breaches on the CURRENT row, per zone. Pure read."""
        cur = self.latest()
        if cur is None:
            return []
        out = []
        zids = ZONE_IDS if zone == "all" else [zone]
        for zid in zids:
            z = cur["zones"][zid]
            for m in ("temp", "rh", "co2", "comfort", "capacity_pct"):
                st = status_for(m, z.get(m))
                if st in ("warning", "critical"):
                    if m == "temp" and z["occ"] == 0:
                        continue           # unoccupied excursions are not comfort alerts
                    a = {"zone": zid, "zone_name": ZONE_BY_ID[zid].name,
                         "metric": m, "value": z.get(m),
                         "unit": THRESHOLDS[m]["unit"], "status": st,
                         "source": FIELD_SOURCES.get(m, "sim"), "t": cur["t"],
                         "normal": THRESHOLDS[m]["normal"]}
                    if m == "rh":
                        a["note"] = RH_LIMITATION_NOTE
                    out.append(a)
        st = status_for("t_out", cur["t_out"])
        if st != "normal":
            out.append({"zone": "outdoor", "zone_name": "Outdoor", "metric": "t_out",
                        "value": cur["t_out"], "unit": "°C", "status": st,
                        "source": "sim", "t": cur["t"], "normal": THRESHOLDS["t_out"]["normal"]})
        order = {"critical": 0, "warning": 1}
        out.sort(key=lambda a: (order.get(a["status"], 9), a["zone"]))
        return out


RH_LIMITATION_NOTE = ("Known twin limitation: the coil apparatus-dew-point approximation "
                      "(ADP_APPROACH in sim/twin.py) pins simulated indoor RH high in humid "
                      "weather. This is a model artefact, not a building fault.")

# ------------------------------------------------------------- helpers
from sim.twin import _OCC_PEAK  # noqa: E402  weekday peak headcount per zone
_OCC_PEAK_TOTAL = max(1, sum(_OCC_PEAK.values()))

FIELD_SOURCES = {
    "temp": "sim", "t_out": "sim", "rh": "sim", "rh_out": "sim", "occ": "sim",
    "occ_pct": "sim", "setpoint": "sim", "vent": "sim", "kwh": "sim",
    "kwh_base": "sim", "base_temp": "sim", "at_capacity": "sim",
    "power_w": "derived", "base_power_w": "sim", "cool_w": "derived",
    "capacity_pct": "derived", "co2": "derived", "comfort": "derived",
    "constraints": "sim",
}


def _bucket(pts: list, mp: int) -> list:
    """Average numeric fields into mp buckets; keep last value for non-numeric."""
    n = len(pts)
    out = []
    for b in range(mp):
        lo, hi = (b * n) // mp, ((b + 1) * n) // mp
        if hi <= lo:
            continue
        chunk = pts[lo:hi]
        agg = dict(chunk[-1])
        for k in agg:
            vals = [c[k] for c in chunk if isinstance(c.get(k), (int, float)) and not isinstance(c.get(k), bool)]
            if len(vals) == len(chunk) and k != "t":
                v = sum(vals) / len(vals)
                agg[k] = round(v, 3) if isinstance(v, float) else v
        agg["t"] = chunk[-1]["t"]
        agg["n"] = len(chunk)
        out.append(agg)
    return out


# ------------------------------------------------------------- forecast
def forecast(twin: DigitalTwin, store, controller_factory, horizon_h: float = 3.0,
             zone: str = "all", max_points: int = 90, co2_init: dict | None = None) -> dict:
    """PREDICTED series: run the same control law forward on throwaway clones.

    INPUT: the live twin + store (cloned here, never mutated), a callable that
      builds a fresh controller, horizon in sim-hours, zone/all, point budget.
    OUTPUT: {"kind":"predicted","horizon_h","t_from","t_to","points":[{t,temp,
      rh,t_out,power_w,kwh(delta),co2,comfort,occ}]} — kwh is the DELTA over
      the horizon, so nothing inherited from the live counters is reported.
    SIDE EFFECTS: none on live state (clone-based, like backend/whatif.py).
    """
    tw = twin.clone()
    st = store.clone() if hasattr(store, "clone") else store
    ctrl = controller_factory()
    steps = max(1, int(round(horizon_h * 3600.0 / DT)))
    every = max(1, int(round(steps / max_points)))
    tel = TelemetryStore(capacity=steps + 2)
    if co2_init:                      # continue the live CO2 estimate, don't restart it
        tel.co2.update({k: float(v) for k, v in co2_init.items() if k in tel.co2})
    kwh0 = tw.kwh
    pts = []
    for i in range(steps):
        sps, vents = ctrl.act(tw, st)
        kwh_before = dict(tw.kwh_by_zone)
        tw.step(sps, vents)
        pw = {z.id: (tw.kwh_by_zone[z.id] - kwh_before[z.id]) * 3.6e6 / DT for z in ZONES}
        cw = {z.id: max(0.0, (pw[z.id] - FAN_W.get(int(vents.get(z.id, 0) or 0), 0.0)) * COP)
              for z in ZONES}
        row = tel.record(tw, None, pw, cw)
        if i % every == 0 or i == steps - 1:
            p = tel._building_point(row) if zone == "all" else tel._zone_point(row, zone)
            p["kwh"] = round(tw.kwh - kwh0, 3)
            pts.append({k: p.get(k) for k in ("t", "temp", "rh", "t_out", "power_w",
                                               "kwh", "co2", "comfort", "occ", "setpoint", "vent")})
    return {"kind": "predicted", "horizon_h": horizon_h, "zone": zone,
            "t_from": pts[0]["t"] if pts else None, "t_to": pts[-1]["t"] if pts else None,
            "points": pts,
            "note": ("Forward run of the SAME ConstraintAware controller on clones of the "
                     "live twin and constraint store; the live building is never touched. "
                     "CO2 restarts from outdoor level in the clone (no CO2 state in the twin).")}
