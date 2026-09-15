"""Latest-value telemetry store (Phase 4): "what is happening RIGHT NOW?".

ONE pipeline for every current value, whatever produced it:

    digital twin ──┐  publish_twin()            source=sim / derived
    hardware rig ──┼─> ingest() ─> validate ─> LatestStore ─> comfort engine (ComfortReading)
    ambient node ──┘  publish_hardware()        source=hardware        │
                                                                        └─> /api/latest ─> dashboard

The current implementation uses the DIGITAL TWIN as its main telemetry source. That does
not mean the building has real physical telemetry; every twin value is tagged `sim`.

READING  metric, value, unit, timestamp (wall clock, ISO UTC), t_wall (epoch s), sim_t,
         building_id, floor_id, zone_id, source, quality, sensor_id, device_id, seq.
SOURCES  sim | derived | historical | hardware | real | predicted  (the project vocabulary).
         The source is set by the SERVER-SIDE adapter that calls ingest(); a payload's own
         "source" field is ignored, so a client cannot label itself hardware/real.
QUALITY  stored:  good | estimated | invalid | missing | simulated (reserved; the twin uses
                  source=sim + quality=good so "simulated" is carried by the source tag)
         on read: good values age into  aging (> GOOD_S)  and  stale (> STALE_S);
                  a stale value keeps its value and says stale — it is never refreshed.
AGE      age_s = now − t_wall, computed at read time, never stored.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

SOURCES = ("sim", "derived", "historical", "hardware", "real", "predicted")
STORED_QUALITIES = ("good", "estimated", "invalid", "missing", "simulated")
QUALITIES = STORED_QUALITIES + ("aging", "stale")
GOOD_S = 60.0
STALE_S = 300.0
OFFLINE_S = 1800.0
BUFFER_S = 15 * 60.0           # recent buffer per key (live trend)
BUFFER_MAX = 1000
MAX_FUTURE_S = 60.0
MAX_PAST_S = 7 * 86400.0
BUILDING = "_building"         # zone_id for building-level metrics
_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")

# metric -> (unit, kind, lo, hi)   kind: num | text | bool
METRICS = {
    "temperature":        ("°C", "num", -50.0, 70.0),
    "humidity":           ("%", "num", 0.0, 100.0),
    "co2":                ("ppm", "num", 0.0, 10000.0),
    "occupancy":          ("people", "num", 0.0, None),
    "occupancy_pct":      ("%", "num", 0.0, None),
    "hvac_mode":          ("", "text", None, None),
    "cooling_pct":        ("%", "num", 0.0, 100.0),
    "fan_level":          ("level", "num", 0.0, 2.0),
    "setpoint":           ("°C", "num", -50.0, 70.0),
    "cooling_w":          ("W", "num", 0.0, None),
    "hvac_power":         ("W", "num", 0.0, None),
    "at_capacity":        ("", "bool", None, None),
    "controller_action":  ("", "text", None, None),
    "ventilation_status": ("", "text", None, None),
    "power":              ("W", "num", 0.0, None),
    "energy_today":       ("kWh", "num", 0.0, None),
    "demand":             ("W", "num", 0.0, None),
    "expected_demand":    ("W", "num", 0.0, None),
    "comfort_score":      ("score", "num", 0.0, 100.0),
    "comfort_status":     ("", "text", None, None),
    "thermal_status":     ("", "text", None, None),
    "humidity_status":    ("", "text", None, None),
    "air_quality_status": ("", "text", None, None),
    "outdoor_temperature": ("°C", "num", -50.0, 70.0),
    # Phase 5: weather + seasonal physics
    "outdoor_humidity":   ("%", "num", 0.0, 100.0),
    "dew_point":          ("°C", "num", -60.0, 60.0),
    "heat_index":         ("°C", "num", -60.0, 90.0),
    "wind_speed":         ("m/s", "num", 0.0, 100.0),
    "wind_direction":     ("", "text", None, None),
    "solar_irradiance":   ("W/m²", "num", 0.0, 1500.0),
    "cloud_cover":        ("%", "num", 0.0, 100.0),
    "rainfall":           ("mm/h", "num", 0.0, 500.0),
    "weather_condition":  ("", "text", None, None),
    "season":             ("", "text", None, None),
    "heating_demand":     ("W", "num", 0.0, None),
    "internal_gain":      ("W", "num", 0.0, None),
    "equipment_power":    ("W", "num", 0.0, None),
    "cooling_capacity_pct": ("%", "num", 0.0, 100.0),
}
# Where a `sim` value physically came from — keeps SIMULATED HARDWARE distinguishable from the
# twin's direct state without inventing a new source tag.
ORIGINS = ("twin", "simulated_hardware", "hardware", "derived")
ALIASES = {"energy": "energy_today", "comfort": "comfort_score", "hvac_load": "hvac_power"}


def iso(t_wall: float) -> str:
    return datetime.fromtimestamp(t_wall, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def freshness(age_s: float) -> str:
    return "good" if age_s <= GOOD_S else "aging" if age_s <= STALE_S else "stale"


def clean_id(v, field: str, required: bool = False):
    if v is None or v == "":
        if required:
            raise ValueError(f"{field} is required")
        return None
    s = str(v)
    if not _ID.match(s):
        raise ValueError(f"{field} must match [A-Za-z0-9_.:-]{{1,64}}")
    return s


class LatestStore:
    """Thread-safe latest value per (zone, metric, sensor) + a short recent buffer.

    Lock discipline: the store has its OWN small lock and never calls out while holding
    it. Callers may hold sim.lock when writing (sim.lock -> store lock is the only order);
    readers need no sim lock.
    """

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._now = clock
        self._latest: dict = {}        # (zone, metric, sensor) -> reading
        self._buf: dict = {}           # (zone, metric, sensor) -> deque[(t_wall, value, quality)]
        self.accepted = 0
        self.invalid = 0
        self.rejected = 0
        self.last_write_wall = None

    # ------------------------------------------------------------------ write
    def ingest(self, reading: dict, source: str, sensor_id: str | None = None,
               device_id: str | None = None) -> dict:
        """Validate and store one reading. Never raises for bad VALUES (they are stored
        with quality=invalid and value=None); raises ValueError only for a payload that
        cannot be addressed at all (unknown metric, bad identifier, bad source)."""
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        metric = ALIASES.get(reading.get("metric"), reading.get("metric"))
        if metric not in METRICS:
            with self._lock:
                self.rejected += 1
            raise ValueError(f"unknown metric {reading.get('metric')!r}")
        try:
            zone = clean_id(reading.get("zone_id") or BUILDING, "zone_id", True)
            building = clean_id(reading.get("building_id"), "building_id")
            floor = clean_id(reading.get("floor_id"), "floor_id")
            sensor = clean_id(sensor_id or reading.get("sensor_id") or f"{source}:{zone}:{metric}", "sensor_id")
            device = clean_id(device_id or reading.get("device_id"), "device_id")
        except ValueError:
            with self._lock:
                self.rejected += 1
            raise
        unit, kind, lo, hi = METRICS[metric]
        now = self._now()
        quality = reading.get("quality") or "good"
        if quality not in STORED_QUALITIES:
            quality = "good"
        problems = []
        t_wall = reading.get("t_wall", now)
        try:
            t_wall = float(t_wall)
            if not math.isfinite(t_wall) or t_wall > now + MAX_FUTURE_S or t_wall < now - MAX_PAST_S:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("invalid timestamp")
            t_wall = now
        value = reading.get("value")
        if value is None:
            quality = "missing" if quality != "invalid" else quality
        elif kind == "num":
            try:
                v = float(value)
                if isinstance(value, bool) or not math.isfinite(v):
                    raise ValueError
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    problems.append(f"{v:g} {unit} outside [{lo}, {hi if hi is not None else '∞'}]")
                value = v
            except (TypeError, ValueError):
                problems.append("not a number")
        elif kind == "bool":
            if not isinstance(value, bool):
                problems.append("not a boolean")
        else:
            value = str(value)[:120]
        rejected_value = None
        if problems:
            rejected_value = reading.get("value")
            rejected_value = rejected_value if isinstance(rejected_value, (int, float, str, bool)) else repr(rejected_value)[:40]
            quality, value = "invalid", None
        rec = {"metric": metric, "value": value, "unit": unit, "t_wall": t_wall,
               "sim_t": reading.get("sim_t"), "building_id": building, "floor_id": floor,
               "zone_id": zone, "source": source, "quality": quality, "sensor_id": sensor,
               "device_id": device, "seq": reading.get("seq") if isinstance(reading.get("seq"), int) else None,
               "problems": problems, "rejected_value": rejected_value,
               "origin": reading.get("origin") if reading.get("origin") in ORIGINS else None}
        key = (zone, metric, sensor)
        with self._lock:
            self._latest[key] = rec
            b = self._buf.get(key)
            if b is None:
                b = self._buf[key] = deque(maxlen=BUFFER_MAX)
            b.append((t_wall, value, quality, reading.get("sim_t")))
            while b and b[0][0] < t_wall - BUFFER_S:
                b.popleft()
            self.accepted += 1
            if quality == "invalid":
                self.invalid += 1
            self.last_write_wall = now
        return self._view(rec, now)

    # ------------------------------------------------------------------ read
    def _view(self, rec: dict, now: float) -> dict:
        age = max(0.0, now - rec["t_wall"])
        q = rec["quality"]
        if q in ("good", "estimated", "simulated") and age > GOOD_S:
            q = freshness(age)
        out = dict(rec)
        out.update(age_s=round(age, 1), quality=q, stored_quality=rec["quality"], timestamp=iso(rec["t_wall"]))
        return out

    def query(self, zone: str | None = None, metric: str | None = None, source: str | None = None,
              sensor: str | None = None, building_id: str | None = None) -> list:
        metric = ALIASES.get(metric, metric)
        now = self._now()
        with self._lock:
            recs = [r for (z, m, s), r in self._latest.items()
                    if (zone is None or z == zone) and (metric is None or m == metric)
                    and (source is None or r["source"] == source) and (sensor is None or s == sensor)
                    and (building_id is None or r["building_id"] in (building_id, None))]
        return [self._view(r, now) for r in recs]

    def get(self, zone: str, metric: str, prefer=("hardware", "real", "sim", "derived", "predicted", "historical")):
        """Best current reading for (zone, metric): a fresh hardware/real reading wins,
        otherwise the most preferred source; stale readings only when nothing else exists.
        Returns None when there is no reading at all (never a fake zero)."""
        rs = self.query(zone=zone, metric=metric)
        if not rs:
            return None
        rank = {s: i for i, s in enumerate(prefer)}
        usable = [r for r in rs if r["quality"] not in ("stale", "invalid", "missing")]
        pool = usable or rs
        return sorted(pool, key=lambda r: (rank.get(r["source"], 99), r["age_s"]))[0]

    def recent(self, zone: str, metric: str, seconds: float = BUFFER_S, sensor: str | None = None) -> list:
        metric = ALIASES.get(metric, metric)
        now = self._now()
        with self._lock:
            keys = [k for k in self._buf if k[0] == zone and k[1] == metric and (sensor is None or k[2] == sensor)]
            out = {k[2]: [{"t_wall": t, "timestamp": iso(t), "value": v, "quality": q, "sim_t": st}
                          for t, v, q, st in self._buf[k] if t >= now - seconds] for k in keys}
            srcs = {k[2]: self._latest[k]["source"] for k in keys}
        return [{"sensor_id": s, "source": srcs[s], "points": p} for s, p in out.items()]

    def counts(self) -> dict:
        now = self._now()
        with self._lock:
            recs = list(self._latest.values())
            last = self.last_write_wall
            acc, inv, rej = self.accepted, self.invalid, self.rejected
        views = [self._view(r, now) for r in recs]
        by_q = {}
        for v in views:
            by_q[v["quality"]] = by_q.get(v["quality"], 0) + 1
        return {"readings": len(views),
                "healthy": sum(by_q.get(q, 0) for q in ("good", "estimated", "simulated")),
                "aging": by_q.get("aging", 0), "stale": by_q.get("stale", 0),
                "invalid": by_q.get("invalid", 0), "missing": by_q.get("missing", 0),
                "by_quality": by_q, "last_update": iso(last) if last else None,
                "last_update_age_s": round(now - last, 1) if last else None,
                "accepted_total": acc, "invalid_total": inv, "rejected_total": rej,
                "by_source": _count(views, "source")}

    def sensors(self) -> list:
        now = self._now()
        out = []
        for v in self.query():
            if v["zone_id"] == BUILDING and v["source"] == "derived":
                continue
            age = v["age_s"]
            st = ("Invalid" if v["quality"] == "invalid" else "Offline" if age > OFFLINE_S else
                  "Stale" if v["quality"] == "stale" else "Aging" if v["quality"] == "aging" else
                  "Missing" if v["quality"] == "missing" else "Healthy")
            out.append({"sensor_id": v["sensor_id"], "device_id": v["device_id"], "metric": v["metric"],
                        "origin": v.get("origin"),
                        "zone_id": v["zone_id"], "source": v["source"], "last_seen": v["timestamp"],
                        "age_s": age, "quality": v["quality"], "status": st,
                        "simulated": v["source"] in ("sim", "derived", "predicted")})
        _ = now
        return sorted(out, key=lambda s: (s["zone_id"], s["metric"], s["sensor_id"]))

    def remove(self, predicate) -> int:
        """Drop readings whose record matches predicate(rec) (e.g. a sensor family that stopped
        existing when simulated hardware was switched off). Returns how many were removed."""
        with self._lock:
            keys = [k for k, r in self._latest.items() if predicate(r)]
            for k in keys:
                self._latest.pop(k, None)
                self._buf.pop(k, None)
        return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._buf.clear()


def _count(views, key) -> dict:
    d = {}
    for v in views:
        d[v[key]] = d.get(v[key], 0) + 1
    return d


def system_status(store: LatestStore, expected_zones: list) -> dict:
    """LIVE TELEMETRY / SIMULATION MODE / DEGRADED / STALE DATA / OFFLINE — from freshness only."""
    c = store.counts()
    views = store.query()
    reasons = []
    if not views or c["last_update_age_s"] is None or c["last_update_age_s"] > STALE_S:
        return {"status": "OFFLINE", "label": "● OFFLINE", "reasons": ["no current telemetry" if not views
                else f"no update for {c['last_update_age_s']:.0f} s"], "counts": c}
    hw = [v for v in views if v["source"] in ("hardware", "real")]
    hw_fresh = [v for v in hw if v["quality"] in ("good", "aging")]
    missing_zones = [z for z in expected_zones if not any(v["zone_id"] == z and v["metric"] == "temperature" for v in views)]
    if c["stale"]:
        reasons.append(f"{c['stale']} stale reading(s)")
    if c["invalid"]:
        reasons.append(f"{c['invalid']} invalid reading(s)")
    if missing_zones:
        reasons.append("no temperature for " + ", ".join(missing_zones))
    if hw and not hw_fresh:
        reasons.append("hardware telemetry went stale")
    if reasons:
        status = "STALE DATA" if c["stale"] and c["stale"] >= c["healthy"] else "DEGRADED"
    elif hw_fresh:
        status = "LIVE TELEMETRY"
        reasons.append(f"{len({v['zone_id'] for v in hw_fresh})} zone(s) with fresh hardware readings; others simulated")
    else:
        status = "SIMULATION MODE"
        reasons.append("all current values come from the digital twin or are derived from it")
    return {"status": status, "label": "● " + status, "reasons": reasons, "counts": c}
