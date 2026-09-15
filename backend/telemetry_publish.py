"""Publishers into the latest-value store + readers out of it (Phase 4).

Every producer goes through LatestStore.ingest() with a SERVER-SIDE source tag:
  publish_twin      the digital twin's zone state      source sim (twin state), derived
                    (CO2 estimate, capacity %, power, demand, energy), predicted (expected demand)
  publish_comfort   comfort engine output               source derived
  publish_hardware  the ESP32 rig reading (bridge)      source hardware
  publish_ambient   the Uno ambient node                source hardware (quality estimated
                                                        until the node reports calibrated)
Consumers read back through comfort_reading() (the Phase-3 ComfortReading seam, now fed
from the store with sources and ages) and latest_state() (the /api/latest payload).
"""
from __future__ import annotations

from backend import comfort
from backend import latest as lv

DEVICE_TWIN = "digital-twin"
SUFFIX = {"temperature": "TEMP", "humidity": "RH", "co2": "CO2", "occupancy": "OCC", "occupancy_pct": "OCCPCT",
          "hvac_mode": "HVACMODE", "cooling_pct": "COOLPCT", "fan_level": "FAN", "setpoint": "SETPT",
          "cooling_w": "COOLW", "hvac_power": "HVACW", "at_capacity": "ATCAP", "controller_action": "CTRL",
          "ventilation_status": "VENT", "demand": "DEMAND", "comfort_score": "COMFORT",
          "comfort_status": "CSTATUS", "thermal_status": "TSTATUS", "humidity_status": "HSTATUS",
          "air_quality_status": "AQSTATUS", "power": "POWER", "energy_today": "EKWH",
          "expected_demand": "EXPDEMAND", "outdoor_temperature": "TOUT"}
PREFIX = {"sim": "SIM", "derived": "DRV", "predicted": "PRD"}
NOT_AVAILABLE = "No current data available"


def sensor_id(prefix: str, zone: str, metric: str) -> str:
    return f"{prefix}-{zone.strip('_').upper().replace('_', '-')}-{SUFFIX.get(metric, metric.upper())}"


def _put(store, source, metric, value, base, quality="good", prefix=None, null_meaning=None):
    r = dict(base, metric=metric, value=value, quality=quality)
    if value is None and null_meaning:
        r["quality"] = quality            # a legitimate "no value" state (HVAC off), not a missing reading
    rec = store.ingest(r, source, sensor_id(prefix or PREFIX[source], base["zone_id"], metric),
                       base.get("device_id") or DEVICE_TWIN)
    if value is None and null_meaning:
        rec_key = (rec["zone_id"], metric, rec["sensor_id"])
        with store._lock:
            s = store._latest[rec_key]
            s["quality"], s["note"] = quality, null_meaning
    return rec


OBSERVED = frozenset({"temperature", "humidity", "co2", "occupancy", "occupancy_pct"})


def publish_twin(store, rows: list, co2: dict, reasons: dict, building_id: str, floors: dict,
                 sim_t: float, t_wall: float, bmetrics: dict, observed=frozenset(),
                 zone_extra: dict | None = None) -> None:
    """Zone rows are LiveSim.zone_rows() — the same numbers /api/state reports.
    `observed` metrics are skipped: simulated hardware sensors publish those instead (Phase 5)."""
    zone_extra = zone_extra or {}
    for r in rows:
        z = r["id"]
        base = {"zone_id": z, "building_id": building_id, "floor_id": f"F{floors.get(z, 1)}",
                "sim_t": sim_t, "t_wall": t_wall, "origin": "twin"}
        mode = ("cooling" if (r.get("cool_w") or 0) > 0 else "ventilation" if r.get("vent") else
                "off" if r.get("setpoint") is None else "idle")
        if "temperature" not in observed:
            _put(store, "sim", "temperature", r["temp"], base)
        if "humidity" not in observed:
            _put(store, "sim", "humidity", r.get("rh"), base)
        if "occupancy" not in observed:
            _put(store, "sim", "occupancy", r["occ"], base)
        if "occupancy_pct" not in observed:
            _put(store, "sim", "occupancy_pct", r.get("occ_pct"), base)
        _put(store, "sim", "heating_demand", None, base, null_meaning="NOT MODELLED — live controller is cooling-only")
        ex = zone_extra.get(z, {})
        if ex.get("internal_gain") is not None:
            _put(store, "derived", "internal_gain", ex["internal_gain"], base)
        _put(store, "sim", "setpoint", r.get("setpoint"), base, null_meaning="HVAC off (no setpoint)")
        _put(store, "sim", "fan_level", r.get("vent"), base)
        _put(store, "sim", "hvac_mode", mode, base)
        _put(store, "sim", "at_capacity", bool(r.get("at_capacity")), base)
        _put(store, "derived", "cooling_pct", r.get("capacity_pct"), base)
        _put(store, "derived", "cooling_w", r.get("cool_w"), base)
        _put(store, "derived", "hvac_power", r.get("power_w"), base)
        _put(store, "derived", "demand", r.get("power_w"), base)
        _put(store, "derived", "controller_action", reasons.get(z) or "base_schedule", base)
        if "co2" not in observed:
            c = co2.get(z)
            _put(store, "derived", "co2", c, base, quality="estimated", prefix="EST")
        vent = int(r.get("vent") or 0)
        _put(store, "derived", "ventilation_status",
             "off" if vent == 0 else "boosted" if vent >= 2 else "normal", base)
    b = {"zone_id": lv.BUILDING, "building_id": building_id, "sim_t": sim_t, "t_wall": t_wall}
    _put(store, "sim", "outdoor_temperature", bmetrics.get("outdoor_temperature"), b)
    _put(store, "sim", "occupancy", bmetrics.get("occupancy"), b)
    _put(store, "derived", "occupancy_pct", bmetrics.get("occupancy_pct"), b)
    _put(store, "derived", "power", bmetrics.get("power"), b)
    _put(store, "derived", "demand", bmetrics.get("power"), b)
    _put(store, "derived", "energy_today", bmetrics.get("energy_today"), b)
    _put(store, "predicted", "expected_demand", bmetrics.get("expected_demand"), b)
    _put(store, "derived", "cooling_capacity_pct", bmetrics.get("cooling_capacity_pct"), b)
    _put(store, "sim", "heating_demand", None, b, null_meaning="NOT MODELLED — live controller is cooling-only")
    if bmetrics.get("equipment_power") is not None:
        _put(store, "derived", "equipment_power", bmetrics["equipment_power"], b)


WEATHER_METRICS = ("outdoor_humidity", "dew_point", "heat_index", "wind_speed", "wind_direction",
                   "solar_irradiance", "cloud_cover", "rainfall", "weather_condition", "season")


def publish_weather(store, snap: dict, building_id: str, sim_t: float, t_wall: float) -> None:
    """Outdoor weather the twin is experiencing (backend/scenario.weather_snapshot). Quantities the
    active weather model does not produce are published as a noted null, never invented."""
    b = {"zone_id": lv.BUILDING, "building_id": building_id, "sim_t": sim_t, "t_wall": t_wall, "origin": "twin"}
    for m in WEATHER_METRICS:
        key = m
        v = snap.get(key)
        _put(store, "sim", m, v, b, null_meaning=snap.get("_note") or "not available")


def comfort_reading(store, zone: str, prefer) -> comfort.ComfortReading:
    """The Phase-3 ComfortReading, now built from the latest-value store with every
    input's source and age. Invalid/missing readings become None (never guessed)."""
    def g(metric):
        r = store.get(zone, metric, prefer)
        if r is None or r["quality"] in ("invalid", "missing"):
            return None, r
        return r["value"], r
    temp, rt = g("temperature")
    rh, rr = g("humidity")
    co2, rc = g("co2")
    occ, ro = g("occupancy")
    occ_pct, _ = g("occupancy_pct")
    hv = {m: g(m)[0] for m in ("hvac_mode", "cooling_pct", "fan_level", "setpoint", "at_capacity", "controller_action")}
    src = {k: r["source"] for k, r in (("temp_c", rt), ("rh_pct", rr), ("co2_ppm", rc), ("occupancy", ro)) if r}
    age = {k: r["age_s"] for k, r in (("temp_c", rt), ("rh_pct", rr), ("co2_ppm", rc), ("occupancy", ro)) if r}
    return comfort.ComfortReading(
        zone_id=zone, t=(rt or {}).get("sim_t"), temp_c=temp, rh_pct=rh, co2_ppm=co2, occupancy=occ,
        occupancy_pct=occ_pct,
        hvac={"mode": hv["hvac_mode"], "cooling_pct": hv["cooling_pct"], "vent": hv["fan_level"],
              "setpoint": hv["setpoint"], "at_capacity": hv["at_capacity"], "reason_code": hv["controller_action"]},
        source=src, age_s=age)


def publish_comfort(store, assessments: list, prefer, building_id: str, floors: dict, sim_t: float,
                    now: float) -> None:
    """Comfort is timestamped with its OLDEST input, so comfort computed from a stale
    temperature is itself stale — it never looks current."""
    occ_scores = []
    for a in assessments:
        z = a["zone_id"]
        ins = [store.get(z, m, prefer) for m in ("temperature", "humidity", "co2", "occupancy")]
        walls = [r["t_wall"] for r in ins if r]
        base = {"zone_id": z, "building_id": building_id, "floor_id": f"F{floors.get(z, 1)}", "sim_t": sim_t,
                "t_wall": min(walls) if walls else now}
        _put(store, "derived", "comfort_score", a["score"], base)
        _put(store, "derived", "comfort_status", a["status"], base)
        _put(store, "derived", "thermal_status", a["thermal"]["status"], base)
        _put(store, "derived", "humidity_status", a["humidity"]["status"], base)
        _put(store, "derived", "air_quality_status", a["air_quality"]["status"], base)
        if a["comfort_relevant"] and a["score"] is not None:
            occ_scores.append(a["score"])
    b = {"zone_id": lv.BUILDING, "building_id": building_id, "sim_t": sim_t, "t_wall": now}
    _put(store, "derived", "comfort_score", round(sum(occ_scores) / len(occ_scores), 1) if occ_scores else None, b,
         null_meaning="no occupied zone right now")


def publish_hardware(store, reading: dict, zone: str, building_id: str, floor: int) -> list:
    """ESP32 rig reading (already validated by HardwareBridge) -> the same pipeline."""
    node = lv.clean_id(str(reading.get("node_id") or "node")[:64].replace(" ", "_"), "device_id") if \
        lv._ID.match(str(reading.get("node_id") or "")) else "hw-node"
    base = {"zone_id": zone, "building_id": building_id, "floor_id": f"F{floor}", "t_wall": reading.get("t_wall"),
            "seq": reading.get("seq"), "device_id": node}
    out = [store.ingest(dict(base, metric="temperature", value=reading.get("temp_c")), "hardware",
                        f"HW-{node}-TEMP", node)]
    if reading.get("rh_pct") is not None:
        out.append(store.ingest(dict(base, metric="humidity", value=reading.get("rh_pct")), "hardware",
                                f"HW-{node}-RH", node))
    return out


def publish_ambient(store, payload: dict, t_wall: float, building_id: str) -> dict | None:
    """Sensor-only ambient node. Not a twin zone: stored under zone_id 'ambient'."""
    if payload.get("temp_c") is None:
        return None
    node = payload.get("node_id") if lv._ID.match(str(payload.get("node_id") or "")) else "ambient-node"
    return store.ingest({"zone_id": "ambient", "building_id": building_id, "metric": "temperature",
                         "value": payload.get("temp_c"), "t_wall": t_wall, "seq": payload.get("seq"),
                         "quality": "good" if payload.get("calibrated") is True else "estimated"},
                        "hardware", f"HW-{node}-TEMP", node)


# ------------------------------------------------------------------ read side
def view(r, metric: str) -> dict:
    if r is None:
        return {"value": None, "unit": lv.METRICS[metric][0], "source": None, "quality": "missing",
                "timestamp": None, "age_s": None, "message": NOT_AVAILABLE}
    out = {k: r.get(k) for k in ("value", "unit", "source", "quality", "timestamp", "age_s", "sensor_id",
                                 "device_id", "seq", "sim_t", "origin")}
    if r.get("note"):
        out["note"] = r["note"]
    if r["quality"] in ("invalid", "missing", "stale"):
        out["display"] = "Unavailable" if r["quality"] != "stale" else "Stale"
        if r["value"] is None and not r.get("note"):
            out["message"] = NOT_AVAILABLE
        if r.get("problems"):
            out["problems"] = r["problems"]
    return out


def zone_block(store, zone: str, name: str, floor: int, prefer) -> dict:
    g = lambda m: view(store.get(zone, m, prefer), m)          # noqa: E731
    comfort_ins = {m: store.get(zone, m, prefer) for m in ("temperature", "humidity", "co2", "occupancy")}
    alts = {}
    for m in ("temperature", "humidity", "co2", "occupancy"):
        rs = store.query(zone=zone, metric=m)
        if len(rs) > 1:
            alts[m] = [view(r, m) for r in rs]
    return {
        "zone_id": zone, "name": name, "floor_id": f"F{floor}",
        "temperature": g("temperature"), "humidity": g("humidity"), "co2": g("co2"),
        "occupancy": g("occupancy"), "occupancy_pct": g("occupancy_pct"),
        "hvac": {m: g(m) for m in ("hvac_mode", "cooling_pct", "fan_level", "setpoint", "hvac_power", "cooling_w",
                                   "at_capacity", "controller_action", "ventilation_status", "heating_demand")},
        "internal_gain": g("internal_gain"),
        "energy": {"power": g("hvac_power")},
        "demand": {"demand": g("demand")},
        "comfort": {"score": g("comfort_score"), "status": g("comfort_status"), "thermal_status": g("thermal_status"),
                    "humidity_status": g("humidity_status"), "air_quality_status": g("air_quality_status"),
                    "stale_inputs": [m for m, r in comfort_ins.items() if r and r["quality"] == "stale"],
                    "missing_inputs": [m for m, r in comfort_ins.items() if r is None or r["quality"] in ("invalid", "missing")]},
        "alternatives": alts,
    }


def source_summary(zones: list) -> dict:
    def srcs(path):
        s = set()
        for z in zones:
            v = z
            for p in path:
                v = v.get(p, {}) if isinstance(v, dict) else {}
            if v.get("source"):
                s.add(v["source"])
        return "+".join(sorted(s)) or None
    return {"temperature": srcs(["temperature"]), "humidity": srcs(["humidity"]), "co2": srcs(["co2"]),
            "occupancy": srcs(["occupancy"]), "hvac": srcs(["hvac", "hvac_mode"]),
            "power": srcs(["energy", "power"]), "demand": srcs(["demand", "demand"]),
            "comfort": srcs(["comfort", "score"])}
