"""Comfort events + discomfort duration (Phase 3). Stateful, lock-free; the caller
(LiveSim, under its lock) feeds it one set of zone assessments per physics step.

EVENT RULES
  open     an OCCUPIED zone shows an issue (warm|cold|humid|dry|high_co2) continuously
           for DEBOUNCE_S; started_t is when it first appeared (not when confirmed)
  update   peak value / peak severity / last controller action while open
  resolve  the issue has been absent for CLEAR_S (resolution_t = when it cleared), or the
           zone became unoccupied (resolution "zone vacated", immediately)
DURATION ACCOUNTING (per zone, per sim day, kept RETAIN_DAYS)
  occupied_s       seconds the zone was occupied
  uncomfortable_s  occupied seconds with discomfort severity above none
Both are sim seconds; nothing is interpolated between ticks.
"""
from __future__ import annotations

from collections import deque

from backend.comfort import ISSUES

DEBOUNCE_S = 300.0
CLEAR_S = 300.0
MAX_EVENTS = 500
RETAIN_DAYS = 7
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
METRIC = {"warm": "temperature", "cold": "temperature", "humid": "humidity", "dry": "humidity",
          "high_co2": "co2"}
DIM = {"temperature": "thermal", "humidity": "humidity", "co2": "air_quality"}
_SEV = {"none": 0, "low": 1, "medium": 2, "high": 3, "severe": 4}


def clock(t: float) -> str:
    s = int(t)
    return f"{DAYS[(s // 86400) % 7]} {s % 86400 // 3600:02d}:{s % 3600 // 60:02d}"


class ComfortTracker:
    def __init__(self):
        self.events: deque = deque(maxlen=MAX_EVENTS)
        self.open: dict = {}            # (zone, issue) -> event
        self._pending: dict = {}        # (zone, issue) -> first seen t
        self._clearing: dict = {}       # (zone, issue) -> first absent t
        self.days: dict = {}            # day -> zone -> {"occupied_s", "uncomfortable_s"}
        self._seq = 0
        self._last_t = None

    # ------------------------------------------------------------------ ingest
    def tick(self, t: float, assessments: list, meta: dict, building: dict) -> list:
        """meta: zone_id -> {zone_name, floor}. Returns events opened/resolved this tick."""
        dt = 60.0 if self._last_t is None else max(0.0, min(t - self._last_t, 600.0))
        if self._last_t is not None and t < self._last_t:
            self.__init__()                                   # clock went back: rebuilt sim
            dt = 60.0
        self._last_t = t
        day = int(t // 86400)
        changed = []
        for a in assessments:
            z = a["zone_id"]
            relevant = a["comfort_relevant"]
            d = self.days.setdefault(day, {}).setdefault(z, {"occupied_s": 0.0, "uncomfortable_s": 0.0})
            if relevant:
                d["occupied_s"] += dt
                if a["condition_severity"] != "none":
                    d["uncomfortable_s"] += dt
            active = set(a["issues"]) if relevant else set()
            for issue in ISSUES:
                key = (z, issue)
                if issue in active:
                    self._clearing.pop(key, None)
                    ev = self.open.get(key)
                    if ev is not None:
                        self._update(ev, a, issue, t)
                    else:
                        first = self._pending.setdefault(key, t)
                        if t - first >= DEBOUNCE_S:
                            ev = self._open(a, issue, first, meta.get(z, {}), building)
                            self._update(ev, a, issue, t)
                            changed.append(ev)
                    continue
                self._pending.pop(key, None)
                ev = self.open.get(key)
                if ev is None:
                    continue
                if not relevant:
                    changed.append(self._resolve(key, t, "zone vacated"))
                    continue
                c = self._clearing.setdefault(key, t)
                if t - c >= CLEAR_S:
                    changed.append(self._resolve(key, c, "condition returned within range"))
        for old in [k for k in self.days if k < day - RETAIN_DAYS + 1]:
            del self.days[old]
        return changed

    def _open(self, a, issue, t0, meta, building) -> dict:
        self._seq += 1
        dim = a[DIM[METRIC[issue]]]
        hv = a.get("hvac", {})
        ev = {"event_id": f"cmf-{self._seq:04d}", "status": "open",
              "started_t": t0, "started_clock": clock(t0),
              "building_id": building.get("id"), "building_name": building.get("name"),
              "floor": meta.get("floor"), "zone_id": a["zone_id"], "zone_name": meta.get("zone_name"),
              "event_type": issue, "triggering_metric": METRIC[issue],
              "threshold": dim["target"], "unit": dim["unit"],
              "measured_value": dim["value"], "peak_value": dim["value"],
              "severity": dim["severity"], "peak_severity": dim["severity"],
              "occupancy_state": a["occupancy_state"], "occupancy_pct": a["occupancy_pct"],
              "hvac_state": {k: hv.get(k) for k in ("mode", "cooling_pct", "vent", "setpoint", "at_capacity")},
              "controller_action": hv.get("reason_code"),
              "recommended_action": a["recommendation"]["text"],
              "resolved_t": None, "resolved_clock": None, "resolution": None,
              "source": "derived"}
        self.open[(a["zone_id"], issue)] = ev
        self.events.append(ev)
        return ev

    def _update(self, ev, a, issue, t) -> None:
        dim = a[DIM[METRIC[issue]]]
        v = dim["value"]
        worse = (v > ev["peak_value"]) if issue in ("warm", "humid", "high_co2") else (v < ev["peak_value"])
        if worse:
            ev["peak_value"] = v
        ev["severity"] = dim["severity"]
        if _SEV[dim["severity"]] > _SEV[ev["peak_severity"]]:
            ev["peak_severity"] = dim["severity"]
        ev["last_seen_t"] = t
        hv = a.get("hvac", {})
        ev["controller_action"] = hv.get("reason_code") or ev["controller_action"]

    def _resolve(self, key, t, why) -> dict:
        ev = self.open.pop(key)
        self._clearing.pop(key, None)
        ev.update(status="resolved", resolved_t=t, resolved_clock=clock(t), resolution=why)
        return ev

    # ------------------------------------------------------------------ readers
    def list(self, now_t: float, zone=None, issue=None, status="all", limit=100) -> list:
        out = []
        for ev in reversed(self.events):
            if zone and ev["zone_id"] != zone:
                continue
            if issue and ev["event_type"] != issue:
                continue
            if status != "all" and ev["status"] != status:
                continue
            e = dict(ev)
            end = e["resolved_t"] if e["resolved_t"] is not None else now_t
            e["duration_s"] = round(max(0.0, end - e["started_t"]), 0)
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def durations(self, zone: str, now_t: float) -> dict:
        day = int(now_t // 86400)
        today = self.days.get(day, {}).get(zone, {"occupied_s": 0.0, "uncomfortable_s": 0.0})
        occ_w = sum(self.days[d].get(zone, {}).get("occupied_s", 0.0) for d in self.days)
        unc_w = sum(self.days[d].get(zone, {}).get("uncomfortable_s", 0.0) for d in self.days)
        opened = [now_t - ev["started_t"] for (z, _), ev in self.open.items() if z == zone]
        return {
            "current_discomfort_s": round(max(opened), 0) if opened else 0.0,
            "today": {"occupied_s": today["occupied_s"], "uncomfortable_s": today["uncomfortable_s"],
                      "uncomfortable_pct": _pct(today["uncomfortable_s"], today["occupied_s"])},
            "week": {"occupied_s": occ_w, "uncomfortable_s": unc_w, "uncomfortable_pct": _pct(unc_w, occ_w),
                     "days_covered": len(self.days)},
            "source": "derived",
        }

    def average_event_duration_s(self, now_t: float):
        ds = [((e["resolved_t"] if e["resolved_t"] is not None else now_t) - e["started_t"]) for e in self.events]
        return round(sum(ds) / len(ds), 0) if ds else None


def _pct(a, b):
    return round(100.0 * a / b, 1) if b > 0 else None
