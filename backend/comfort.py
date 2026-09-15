"""Occupant comfort engine — the ONE place comfort is calculated (Phase 3).

WHAT THIS IS. An ENGINEERING COMFORT INDEX: transparent, per-dimension, explainable,
computed in the backend from values the twin / telemetry / dataset actually produce.
It is NOT an ASHRAE 55 / ISO 7730 certification. The project's formal thermal-comfort
calculation is PMV/PPD in backend/dataset/comfort.py (with stated input assumptions);
this index is what operators act on.

INPUT is a ComfortReading (latest values + provenance + age), so the same engine can be
fed by the live twin today and by a latest-value telemetry pipeline later (Phase 4).
Nothing here reads HTTP, polls, or touches the twin.

FORMULAS (all documented in docs/COMFORT.md; weights live in the building profile):
  thermal score   = 100                       inside [comfort_min, comfort_max]
                  = 100 * (1 - dev / 3)       dev = K outside the range, floor 0
  humidity score  = 100                       inside [humidity_min, humidity_max]
                  = 100 * (1 - dev / 20)      dev = %RH outside the range, floor 0
  CO2 score       = 100                       co2 <= 0.7 * limit
                  = 100 - 50 * (co2 - 0.7L)/(0.3L)   up to the limit (50 at the limit)
                  = max(0, 50 - 50 * (co2 - L)/L)     above it (0 at 2 x limit)
  comfort score   = sum(w_i * score_i) / sum(w_i over AVAILABLE dimensions)
                    (a missing/invalid reading drops out and is reported; it is never
                    replaced by a guessed value)
  severity        worst dimension: thermal dev K (<=1 low, <=2 medium, <=3 high, >3 severe),
                  humidity dev %RH (<=5, <=10, <=20, >20), CO2 excess vs limit
                  (<=20 %, <=50 %, <=100 %, >100 %)
  occupancy state Unoccupied (0) · Partially Occupied (< 50 % of design) · Occupied
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------------ vocabulary
OVERALL_STATUSES = ("Excellent", "Comfortable", "Acceptable", "Warm", "Cold", "Humid", "Dry",
                    "High CO2", "Poor Air Quality", "Severe Discomfort", "Unoccupied", "Unavailable")
THERMAL_STATUSES = ("Comfortable", "Warm", "Cold", "Unavailable")
HUMIDITY_STATUSES = ("Comfortable", "Humid", "Dry", "Unavailable")
AIR_STATUSES = ("Good", "Moderate", "High CO2", "Poor", "Unavailable")
SEVERITIES = ("none", "low", "medium", "high", "severe")
OCCUPANCY_STATES = ("Unoccupied", "Partially Occupied", "Occupied")
ISSUES = ("warm", "cold", "humid", "dry", "high_co2")

DEFAULT_WEIGHTS = {"thermal": 0.5, "humidity": 0.2, "air_quality": 0.3}
PARTIAL_OCCUPANCY_PCT = 50.0
THERMAL_ZERO_K = 3.0
HUMIDITY_ZERO_PCT = 20.0
# physically possible sensor ranges; outside = invalid reading (not "very uncomfortable")
VALID = {"temp_c": (-20.0, 60.0), "rh_pct": (0.0, 100.0), "co2_ppm": (300.0, 10000.0)}
STALE_AFTER_S = 300.0         # = backend/latest.py STALE_S (Phase 4 freshness rule)

_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class ComfortReading:
    """Latest values for one zone. None = no reading. `source` maps field -> provenance tag
    (sim|derived|hardware|real|historical|predicted); `age_s` maps field -> seconds since the
    reading (staleness); both optional."""
    zone_id: str
    t: float | None = None
    temp_c: float | None = None
    rh_pct: float | None = None
    co2_ppm: float | None = None
    occupancy: float | None = None
    occupancy_pct: float | None = None
    hvac: dict = field(default_factory=dict)      # mode, cooling_pct, vent, setpoint, at_capacity
    source: dict = field(default_factory=dict)
    age_s: dict = field(default_factory=dict)


# ------------------------------------------------------------------ building blocks
def check_value(name: str, value, age_s=None) -> tuple:
    """-> (usable value or None, quality) quality in ok|missing|invalid|stale."""
    if value is None:
        return None, "missing"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, "invalid"
    if v != v or v in (float("inf"), float("-inf")):
        return None, "invalid"
    lo, hi = VALID[name]
    if not lo <= v <= hi:
        return None, "invalid"
    if age_s is not None and age_s > STALE_AFTER_S:
        return v, "stale"                       # still shown, flagged; the caller decides
    return v, "ok"


def thermal_score(temp: float, lo: float, hi: float) -> tuple:
    dev = temp - hi if temp > hi else temp - lo if temp < lo else 0.0
    return round(max(0.0, 100.0 * (1.0 - abs(dev) / THERMAL_ZERO_K)), 1), round(dev, 2)


def humidity_score(rh: float, lo: float, hi: float) -> tuple:
    dev = rh - hi if rh > hi else rh - lo if rh < lo else 0.0
    return round(max(0.0, 100.0 * (1.0 - abs(dev) / HUMIDITY_ZERO_PCT)), 1), round(dev, 1)


def co2_score(co2: float, limit: float) -> float:
    if co2 <= 0.7 * limit:
        s = 100.0
    elif co2 <= limit:
        s = 100.0 - 50.0 * (co2 - 0.7 * limit) / (0.3 * limit)
    else:
        s = max(0.0, 50.0 - 50.0 * (co2 - limit) / limit)
    return round(s, 1)


def _sev_thermal(dev):
    a = abs(dev)
    return "none" if a == 0 else "low" if a <= 1 else "medium" if a <= 2 else "high" if a <= 3 else "severe"


def _sev_humidity(dev):
    a = abs(dev)
    return "none" if a == 0 else "low" if a <= 5 else "medium" if a <= 10 else "high" if a <= 20 else "severe"


def _sev_co2(co2, limit):
    if co2 <= limit:
        return "none"
    x = (co2 - limit) / limit
    return "low" if x <= 0.2 else "medium" if x <= 0.5 else "high" if x <= 1.0 else "severe"


def occupancy_state(occupancy, occupancy_pct) -> str | None:
    if occupancy is None and occupancy_pct is None:
        return None
    if (occupancy or 0) <= 0 and (occupancy_pct or 0) <= 0:
        return "Unoccupied"
    return "Partially Occupied" if (occupancy_pct if occupancy_pct is not None else 100.0) < PARTIAL_OCCUPANCY_PCT else "Occupied"


def weights_for(cfg) -> dict:
    w = dict(getattr(cfg, "comfort_weights", None) or DEFAULT_WEIGHTS)
    return {k: float(w.get(k, 0.0)) for k in DEFAULT_WEIGHTS}


def controller_preference(cfg) -> dict:
    """The clean interface a controller consumes: comfort vs energy weighting from the
    profile priorities. It does not change any control law by itself. `act_from_severity`
    is the lowest discomfort severity worth spending energy on; `objective_hint` is the
    same mapping backend.building.mode_levers uses in normal mode."""
    cp, ep = float(cfg.comfort_priority), float(cfg.energy_priority)
    tot = (cp + ep) or 1.0
    lead = ep - cp
    return {"comfort_weight": round(cp / tot, 3), "energy_weight": round(ep / tot, 3),
            "objective_hint": "energy" if lead >= 30 else "comfort" if lead <= -30 else "balanced",
            "act_from_severity": "low" if cp >= 70 else "high" if ep >= 70 else "medium"}


# ------------------------------------------------------------------ the engine
def assess(reading: ComfortReading, cfg) -> dict:
    """Full comfort assessment for one zone. Pure. Every number is an input or a
    documented function of inputs; nothing is invented when a reading is missing."""
    W = weights_for(cfg)
    q = {}
    temp, q["temperature"] = check_value("temp_c", reading.temp_c, reading.age_s.get("temp_c"))
    rh, q["humidity"] = check_value("rh_pct", reading.rh_pct, reading.age_s.get("rh_pct"))
    co2, q["co2"] = check_value("co2_ppm", reading.co2_ppm, reading.age_s.get("co2_ppm"))
    occ_state = occupancy_state(reading.occupancy, reading.occupancy_pct)
    src = reading.source

    dims, factors = {}, []
    if temp is not None:
        s, dev = thermal_score(temp, cfg.comfort_min_c, cfg.comfort_max_c)
        st = "Warm" if dev > 0 else "Cold" if dev < 0 else "Comfortable"
        dims["thermal"] = {"score": s, "status": st, "value": temp, "unit": "°C", "deviation": dev,
                           "target": [cfg.comfort_min_c, cfg.comfort_max_c], "severity": _sev_thermal(dev),
                           "source": src.get("temp_c", "sim"), "quality": q["temperature"]}
    else:
        dims["thermal"] = {"score": None, "status": "Unavailable", "value": None, "unit": "°C",
                           "target": [cfg.comfort_min_c, cfg.comfort_max_c], "severity": "none",
                           "quality": q["temperature"], "source": src.get("temp_c", "sim")}
    if rh is not None:
        s, dev = humidity_score(rh, cfg.humidity_min_pct, cfg.humidity_max_pct)
        st = "Humid" if dev > 0 else "Dry" if dev < 0 else "Comfortable"
        dims["humidity"] = {"score": s, "status": st, "value": rh, "unit": "%", "deviation": dev,
                            "target": [cfg.humidity_min_pct, cfg.humidity_max_pct],
                            "severity": _sev_humidity(dev), "source": src.get("rh_pct", "sim"),
                            "quality": q["humidity"]}
    else:
        dims["humidity"] = {"score": None, "status": "Unavailable", "value": None, "unit": "%",
                            "target": [cfg.humidity_min_pct, cfg.humidity_max_pct], "severity": "none",
                            "quality": q["humidity"], "source": src.get("rh_pct", "sim")}
    lim = cfg.co2_max_ppm
    if co2 is not None:
        s = co2_score(co2, lim)
        sev = _sev_co2(co2, lim)
        st = ("Poor" if co2 > 1.5 * lim else "High CO2" if co2 > lim else
              "Moderate" if co2 > 0.7 * lim else "Good")
        dims["air_quality"] = {"score": s, "status": st, "value": co2, "unit": "ppm",
                               "deviation": round(max(0.0, co2 - lim), 0), "target": [None, lim],
                               "severity": sev, "source": src.get("co2_ppm", "derived"),
                               "quality": q["co2"], "indicator": "CO2 / ventilation indicator — "
                               "not a measurement of every aspect of indoor air quality"}
    else:
        dims["air_quality"] = {"score": None, "status": "Unavailable", "value": None, "unit": "ppm",
                               "target": [None, lim], "severity": "none", "quality": q["co2"],
                               "source": src.get("co2_ppm", "derived"),
                               "indicator": "CO2 / ventilation indicator"}

    avail = {k: d for k, d in dims.items() if d["score"] is not None and W[k] > 0}
    wsum = sum(W[k] for k in avail)
    score = round(sum(W[k] * d["score"] for k, d in avail.items()) / wsum, 1) if wsum else None
    for k, d in dims.items():
        if d["score"] is not None:
            factors.append({"dimension": k, "status": d["status"], "value": d["value"], "unit": d["unit"],
                            "target": d["target"], "deviation": d.get("deviation"), "score": d["score"],
                            "weight": W[k], "impact": round(W[k] * (100.0 - d["score"]), 1),
                            "severity": d["severity"], "source": d["source"]})
    factors.sort(key=lambda f: (-f["impact"], -_SEV_RANK[f["severity"]]))
    # causes rank by SEVERITY first, then score impact, so the primary cause always agrees
    # with the headline status (a severe humidity problem outranks a medium CO2 one)
    issues = sorted([f for f in factors if f["severity"] != "none"],
                    key=lambda f: (-_SEV_RANK[f["severity"]], -f["impact"]))
    severity = max((f["severity"] for f in factors), key=lambda s: _SEV_RANK[s], default="none")

    issue_status = None
    if issues:
        top = max(issues, key=lambda f: (_SEV_RANK[f["severity"]], f["impact"]))
        issue_status = top["status"] if top["dimension"] != "air_quality" else (
            "Poor Air Quality" if top["status"] == "Poor" else "High CO2")
    if score is None:
        overall = "Unavailable"
    elif occ_state == "Unoccupied":
        overall = "Unoccupied"
    elif severity == "severe":
        overall = "Severe Discomfort"
    elif issue_status:
        overall = issue_status
    else:
        overall = "Excellent" if score >= 90 else "Comfortable" if score >= 75 else "Acceptable"

    relevant = occ_state in ("Occupied", "Partially Occupied")
    missing = [k for k, v in q.items() if v in ("missing", "invalid")]
    rec = recommend(dims, issues, reading.hvac, cfg, relevant)
    return {
        "zone_id": reading.zone_id, "t": reading.t,
        "score": score, "occupied_score": score if relevant else None,
        "status": overall, "severity": severity if relevant else ("info" if severity != "none" else "none"),
        "condition_severity": severity,
        "occupancy_state": occ_state, "occupancy": reading.occupancy, "occupancy_pct": reading.occupancy_pct,
        "comfort_relevant": relevant,
        "thermal": dims["thermal"], "humidity": dims["humidity"], "air_quality": dims["air_quality"],
        "factors": factors, "primary_cause": issues[0] if issues else None,
        "secondary_causes": issues[1:],
        "issues": sorted({_issue_key(f) for f in issues}),
        "recommendation": rec, "hvac": dict(reading.hvac),
        "weights": W, "data_quality": {"fields": q, "missing": missing,
                                       "stale": [k for k, v in q.items() if v == "stale"]},
        "source": "derived",
        "index": "engineering comfort index (not a PMV/PPD certification)",
    }


def _issue_key(f) -> str:
    if f["dimension"] == "thermal":
        return "warm" if f["status"] == "Warm" else "cold"
    if f["dimension"] == "humidity":
        return "humid" if f["status"] == "Humid" else "dry"
    return "high_co2"


def recommend(dims: dict, issues: list, hvac: dict, cfg, relevant: bool) -> dict:
    """Rule-based action from the measured cause and the current HVAC state."""
    pref = controller_preference(cfg)
    if not issues:
        return {"action": "none", "text": "No action: conditions are within the configured ranges.",
                "expected_effect": None, "energy_consideration": None, "deferred": False}
    if not relevant:
        return {"action": "none", "text": "Zone unoccupied: conditions are outside range but no occupant is "
                "affected; the schedule will condition it before occupancy.",
                "expected_effect": None, "energy_consideration": "No energy spent on an empty zone.",
                "deferred": True}
    worst = max(issues, key=lambda f: _SEV_RANK[f["severity"]])
    deferred = _SEV_RANK[worst["severity"]] < _SEV_RANK[pref["act_from_severity"]]
    acts, effects, energy = [], [], []
    keys = {_issue_key(f) for f in issues}
    cool_pct = hvac.get("cooling_pct")
    vent = hvac.get("vent")
    if "warm" in keys:
        if hvac.get("at_capacity") or (cool_pct is not None and cool_pct >= 95):
            acts.append("cooling is at capacity — reduce internal/solar load or inspect the unit")
            effects.append("no further thermal gain available from this unit")
        else:
            acts.append("increase cooling (lower the setpoint)")
            effects.append("improve thermal comfort")
            energy.append("higher cooling energy")
    if "cold" in keys:
        if (cool_pct or 0) > 0:
            acts.append("reduce cooling (raise the setpoint)")
            effects.append("improve thermal comfort")
            energy.append("lower cooling energy")
        else:
            acts.append("increase heating or reduce outdoor-air intake")
            effects.append("improve thermal comfort")
            energy.append("heating energy if heating is available")
    if "humid" in keys:
        acts.append("extend cooling-coil runtime for dehumidification")
        effects.append("reduce relative humidity")
        energy.append("higher cooling energy (latent load)")
    if "dry" in keys:
        acts.append("check humidification (not modelled by the twin)")
        effects.append("raise relative humidity")
    if "high_co2" in keys:
        if vent is not None and vent >= 2:
            acts.append("ventilation already at maximum — reduce occupancy or inspect the outdoor-air damper")
            effects.append("CO2 will fall only when occupancy or outdoor-air delivery changes")
        else:
            acts.append("increase ventilation (fan level up)")
            effects.append("reduce CO2")
            energy.append("higher fan energy and outdoor-air load")
    text = "; ".join(acts).capitalize() + "."
    if deferred:
        text = (f"Energy priority ({cfg.energy_priority:g}) defers action on {worst['severity']} discomfort. "
                f"If acted on: {text}")
    return {"action": "defer" if deferred else "act", "text": text,
            "expected_effect": "; ".join(effects) or None,
            "energy_consideration": "; ".join(energy) or "no significant energy change expected",
            "deferred": deferred, "priority": pref}


def building_summary(assessments: list) -> dict:
    """Building KPIs from zone assessments (live instant)."""
    scored = [a for a in assessments if a["score"] is not None]
    occupied = [a for a in scored if a["comfort_relevant"]]
    uncomfortable = [a for a in occupied if a["severity"] not in ("none", "info")]

    def mean(xs):
        return round(sum(xs) / len(xs), 1) if xs else None
    counts = {i: sum(1 for a in occupied if i in a["issues"]) for i in ISSUES}
    worst = min(occupied, key=lambda a: a["score"], default=None)
    freq = max(counts, key=lambda k: counts[k]) if any(counts.values()) else None
    return {
        "overall_score": mean([a["score"] for a in scored]),
        "occupied_score": mean([a["score"] for a in occupied]),
        "zones": len(assessments), "occupied_zones": len(occupied),
        "comfortable_zones": len(occupied) - len(uncomfortable), "uncomfortable_zones": len(uncomfortable),
        "compliance_pct": round(100.0 * (len(occupied) - len(uncomfortable)) / len(occupied), 1) if occupied else None,
        "worst_zone": {"zone_id": worst["zone_id"], "score": worst["score"], "status": worst["status"]} if worst else None,
        "most_frequent_issue": freq,
        "high_co2_zones": counts["high_co2"], "warm_zones": counts["warm"], "cold_zones": counts["cold"],
        "humid_zones": counts["humid"], "dry_zones": counts["dry"],
        "unavailable_zones": len(assessments) - len(scored),
        "source": "derived",
    }
