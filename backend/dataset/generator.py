"""The generation pipeline. Weather -> occupancy -> internal gains -> the EXISTING
5-zone RC twin (sim/twin.py, one twin per floor, 60-s physics) -> HVAC -> indoor
environment -> comfort -> energy / demand -> RAW sensors -> validation -> CLEAN.

Nothing is sampled independently per column: every indoor, HVAC and energy value is
a consequence of the twin being stepped under that weather, that occupancy and those
internal gains, by a profile BMS schedule (hvac.py).

    Weather ──────────────┐
       │                  │ (outdoor air, solar, humidity)
    Calendar ─> Occupancy ─┼─> Internal gains (people, lights, plugs, equipment)
                           ▼
                  Building thermal model (sim/twin.py, per floor)
                           │ <── BMS setpoints / fans (hvac.py)
                           ▼
          Indoor temp / RH / CO2 ─> Comfort (scores, PMV/PPD)
                           │
                    HVAC + non-HVAC power ─> Energy / demand / PV / grid
                           │
              RAW sensors ─> validate ─> CLEAN          (quality.py)
"""
from __future__ import annotations

import calendar as _cal
import json
import random
from datetime import datetime, timedelta

from backend import building
from backend.dataset import anomalies as A
from backend.dataset import comfort, hvac, loads, quality
from backend.dataset.calendar import Calendar, season_for, time_features
from backend.dataset.occupancy import OccupancyModel, status_for
from backend.dataset.weather import CLIMATE, WeatherModel
from backend.telemetry import CO2_GEN_M3S_PER_PERSON, CO2_OUTDOOR_PPM, _outdoor_air_m3s
from sim.twin import CEILING_H, COP, HEAT_COP, ZONE_BY_ID, ZONE_IDS, DigitalTwin

WARMUP_MIN = 360
COIL_ADP_C = 12.0            # realistic DX coil apparatus dew point for generated history
VERSION = "phase2-dataset-v1"


def epoch(ts: datetime) -> int:
    """Site wall-clock time as unix seconds (no timezone: site local time)."""
    return _cal.timegm(ts.timetuple())


def iaq_index(co2: float, rh: float) -> float:
    """0..500 indoor-air-quality index (DERIVED heuristic, lower is better):
    CO2 piecewise (400->0, 600->50, 1000->100, 1500->150, 2500->250, 5000->500)
    + 1 point per %RH outside 30..60."""
    pts = ((400, 0), (600, 50), (1000, 100), (1500, 150), (2500, 250), (5000, 500))
    base = 500.0
    for (c0, v0), (c1, v1) in zip(pts, pts[1:]):
        if co2 <= c1:
            base = v0 + (v1 - v0) * max(0.0, co2 - c0) / (c1 - c0)
            break
    return round(min(500.0, base + max(0.0, rh - 60.0) + max(0.0, 30.0 - rh)), 1)


def demand_category(risk: float) -> str:
    return "LOW" if risk < 0.35 else "MEDIUM" if risk < 0.6 else "HIGH" if risk < 0.85 else "CRITICAL"


def generate(cfg, sink, progress=None) -> dict:
    errs = cfg.validate()
    if errs:
        raise ValueError("; ".join(errs))
    start, days, step = cfg.start_dt, int(cfg.days), int(cfg.step_min)
    total_min = days * 1440
    cal = Calendar(cfg.holidays, cfg.events)
    season_of = lambda ts: season_for(ts, cfg.season)                      # noqa: E731
    wx = WeatherModel(start, days, cfg.seed, cfg.climate, season_of, cfg.latitude)
    params = CLIMATE[cfg.climate].values()
    t_hot = max(p["t_mean"] + p["t_amp"] for p in params) + 4.0
    t_cold = min(p["t_mean"] - p["t_amp"] for p in params) - 3.0

    zones_by_building = {b.building_id: [f"F{f}-{z}" for f in range(1, b.floors + 1) for z in ZONE_IDS]
                         for b in cfg.buildings}
    anoms = A.schedule(cfg, zones_by_building)
    for a in anoms:
        if a["anomaly_type"] == "weather_disturbance":
            wx.add_disturbance(a["_t0"], a["_t1"], a["params"]["delta_c"])
    idx = A.ActiveIndex(anoms)
    sink.write("anomalies", [{**{k: v for k, v in a.items() if not k.startswith("_") and k != "params"},
                              "params_json": json.dumps(a["params"])} for a in anoms])

    memo = {"k": None, "v": None}

    def wx_at(ts):
        k = ts
        if memo["k"] != k:
            memo["k"], memo["v"] = k, wx.at(ts)
        return memo["v"]

    # ---- time + weather dimensions (site-wide, once)
    t_rows, w_rows = [], []
    for i in range(total_min // step):
        ts = start + timedelta(minutes=i * step)
        tf = time_features(ts, cal, season_of(ts), False)
        t_rows.append({**tf, "ts": tf["timestamp"], "t": epoch(ts)})
        w_rows.append({**wx.at(ts), "t": epoch(ts)})
    sink.write("time_dim", t_rows)
    sink.write("weather_obs", w_rows)

    counts = {"zone_obs": 0, "building_obs": 0, "raw_obs": 0}
    for bspec in cfg.buildings:
        bid, btype = bspec.building_id, bspec.building_type
        prof = building.default_config(btype)
        if bspec.name:
            prof.name = bspec.name
        floors = [f"F{f}" for f in range(1, bspec.floors + 1)]
        banoms = [a for a in anoms if a["building_id"] in (bid, "*")]
        fctx = []
        zdim = []
        for fi, fl in enumerate(floors):
            occm = OccupancyModel(prof, ZONE_IDS, start, days, step, cal, cfg.seed, f"{bid}:{fl}")
            for a in banoms:
                if a["anomaly_type"] == "occupancy_spike" and a["affected_zone"].startswith(fl + "-"):
                    occm.apply_spike(a["_t0"], a["_t1"], a["affected_zone"].split("-", 1)[1], a["params"]["factor"])
            caps = {z: loads.design_capacities(prof, btype, z, occm.capacity[z], t_hot, t_cold) for z in ZONE_IDS}
            heating = any(c[1] > 0 for c in caps.values())
            gain = {z: 0.0 for z in ZONE_IDS}
            tw = DigitalTwin(
                seed=cfg.seed, start_temp=(prof.comfort_min_c + prof.comfort_max_c) / 2,
                weather_fn=lambda t: wx_at(start + timedelta(seconds=t))["outdoor_temperature_c"],
                rh_fn=lambda t: wx_at(start + timedelta(seconds=t))["outdoor_humidity_percent"],
                solar_fn=lambda o, t: wx.solar_factor(o, start + timedelta(seconds=t), wx_at),
                occupancy_fn=lambda z, t, _o=occm: _o.count(z, start + timedelta(seconds=t)),
                gain_fn=lambda z, t, _g=gain: _g[z],
                capacity_w={z: caps[z][0] for z in ZONE_IDS},
                heat_capacity_w={z: caps[z][1] for z in ZONE_IDS} if heating else None,
                coil_adp_c=COIL_ADP_C)
            tw.solar_scale = 1.0 + 0.08 * fi                     # upper floors: more sun
            design = {z: loads.design_electrical_kw(caps[z][0], caps[z][1], prof, btype, z) for z in ZONE_IDS}
            for z in ZONE_IDS:
                zdim.append({"building_id": bid, "floor_id": fl, "zone_id": f"{fl}-{z}", "twin_zone": z,
                             "zone_role": prof.zone_roles.get(z, ZONE_BY_ID[z].name),
                             "area_m2": ZONE_BY_ID[z].area, "occupancy_capacity": occm.capacity[z],
                             "cooling_capacity_kw": round(caps[z][0] / 1000, 2),
                             "heating_capacity_kw": round(caps[z][1] / 1000, 2) if heating else 0.0,
                             "design_demand_kw": round(design[z], 2)})
            fctx.append({"fl": fl, "occ": occm, "caps": caps, "heating": heating, "gain": gain, "tw": tw,
                         "co2": {z: CO2_OUTDOOR_PPM for z in ZONE_IDS}, "prev_occ": {z: 0 for z in ZONE_IDS},
                         "acc": {z: _acc() for z in ZONE_IDS},
                         "truth": {z: {f: [] for f in quality.FIELDS} for z in ZONE_IDS},
                         "design": design})
        design_bkw = sum(sum(c["design"].values()) for c in fctx)
        cap_people = sum(sum(c["occ"].capacity.values()) for c in fctx)
        sink.write("building_dim", [{"building_id": bid, "building_type": btype, "building_name": prof.name,
                                     "floors": len(floors), "zones": len(floors) * len(ZONE_IDS),
                                     "pv_kwp": bspec.pv_kwp, "occupancy_capacity": cap_people,
                                     "design_demand_kw": round(design_bkw, 2),
                                     "profile_json": json.dumps(building.config_dict(prof))}])
        sink.write("zone_dim", zdim)

        z_rows, b_rows = [], []
        daily = {"date": None, "kwh": 0.0, "peak": 0.0}
        fc_hist: dict = {}
        for m in range(-WARMUP_MIN, total_min):
            ts = start + timedelta(minutes=m)
            w = wx_at(ts)
            t_out, ghi = w["outdoor_temperature_c"], w["solar_irradiance_w_m2"]
            wd, h = ts.weekday(), ts.hour + ts.minute / 60.0
            open_now = building.is_open(prof, wd, h)
            for c in fctx:
                tw, occm, fl = c["tw"], c["occ"], c["fl"]
                sps, vents, ctl = {}, {}, {}
                for z in ZONE_IDS:
                    zid = f"{fl}-{z}"
                    occ, cap_p = occm.count(z, ts), occm.capacity[z]
                    fail = idx.get(bid, zid, "hvac_failure", ts)
                    tw.capacity_w[z] = 0.0 if fail else c["caps"][z][0]
                    if c["heating"]:
                        tw.heat_capacity_w[z] = 0.0 if fail else c["caps"][z][1]
                    frac = occ / cap_p
                    light = loads.lighting_w(prof, z, frac, open_now, ghi)
                    plug = loads.plug_w(btype, z, frac)
                    equip = loads.equipment_w(btype, z, open_now)
                    ae = idx.get(bid, zid, "abnormal_energy", ts)
                    if ae:
                        equip += ae["params"]["extra_w_m2"] * ZONE_BY_ID[z].area
                    c["gain"][z] = light + plug + equip
                    cool, heat_sp, vent, econ, armed = hvac.schedule(prof, btype, z, ts, occ, cap_p,
                                                                     c["co2"][z], tw.T[z], t_out)
                    sps[z], vents[z] = cool, vent
                    tw.heat_setpoints[z] = heat_sp if c["heating"] else None
                    ctl[z] = (occ, light, plug, equip, econ, armed, cool, heat_sp, vent)
                tw.t = m * 60.0
                tw.step(sps, vents)
                if m < 0:
                    for z in ZONE_IDS:
                        _co2_step(c, z, ctl[z][0], ctl[z][8], bid, fl, idx, ts)
                    continue
                for z in ZONE_IDS:
                    occ, light, plug, equip, econ, armed, cool, heat_sp, vent = ctl[z]
                    _co2_step(c, z, occ, vent, bid, fl, idx, ts)
                    a = c["acc"][z]
                    qc_, qh = tw.last_cool_w[z], tw.last_heat_w[z]
                    a["cool"] += qc_; a["heat"] += qh; a["fan"] += tw.last_fan_w[z]
                    a["hvac"] += qc_ / COP + qh / HEAT_COP
                    a["light"] += light; a["plug"] += plug; a["equip"] += equip; a["n"] += 1
                    a["last"] = (econ, armed, cool, heat_sp, vent)
            if m < 0 or (m + 1) % step:
                continue
            ts0 = start + timedelta(minutes=m + 1 - step)
            season = season_of(ts0)
            step_h = step / 60.0
            tb = {"total": 0.0, "hvac": 0.0, "vent": 0.0, "light": 0.0, "plug": 0.0, "equip": 0.0,
                  "cool": 0.0, "heat": 0.0, "occ": 0, "comfort": []}
            for c in fctx:
                tw, occm, fl = c["tw"], c["occ"], c["fl"]
                for z in ZONE_IDS:
                    zid = f"{fl}-{z}"
                    a = c["acc"][z]
                    n = max(1, a["n"])
                    econ, armed, cool, heat_sp, vent = a["last"]
                    hvac_kw, vent_kw = a["hvac"] / n / 1000, a["fan"] / n / 1000
                    light_kw, plug_kw, equip_kw = a["light"] / n / 1000, a["plug"] / n / 1000, a["equip"] / n / 1000
                    total = hvac_kw + vent_kw + light_kw + plug_kw + equip_kw
                    occ, cap_p = occm.count(z, ts0), occm.capacity[z]
                    temp, rh, co2 = tw.T[z], tw.rh_now(z), c["co2"][z]
                    cool_w, heat_w = a["cool"] / n, a["heat"] / n
                    obs = hvac.observables(temp, cool_w, heat_w, a["fan"] / n, vent, tw._cool_cap(ZONE_BY_ID[z]),
                                           tw.heat_capacity_w.get(z, 0.0) if tw.heat_capacity_w else 0.0,
                                           econ, armed, building.required_oa_ls(prof, z, occ),
                                           building.supplied_oa_ls(z, 2))
                    com = comfort.comfort_row(temp, rh, co2, vent, prof, btype, season)
                    active = [x["anomaly_id"] for x in banoms if x["_t0"] <= ts0 < x["_t1"]
                              and x["affected_zone"] in (zid, "*")]
                    row = {
                        "t": epoch(ts0), "ts": ts0.isoformat(timespec="minutes"),
                        "building_id": bid, "floor_id": fl, "zone_id": zid,
                        "occupancy_count": occ, "occupancy_percent": round(100.0 * occ / cap_p, 1),
                        "occupancy_capacity": cap_p, "expected_occupancy": occm.expected_count(z, ts0),
                        "occupancy_change": occ - c["prev_occ"][z], "occupancy_status": status_for(occ, cap_p),
                        "indoor_temperature_c": round(temp, 2), "indoor_humidity_percent": round(rh, 1),
                        "indoor_co2_ppm": round(co2, 0), "indoor_air_quality_index": iaq_index(co2, rh),
                        "temperature_setpoint_c": cool, "heating_setpoint_c": heat_sp if c["heating"] else None,
                        "humidity_setpoint_percent": prof.humidity_max_pct, "co2_limit_ppm": prof.co2_max_ppm,
                        **obs, "hvac_power_kw": round(hvac_kw, 4),
                        "total_power_kw": round(total, 4), "lighting_power_kw": round(light_kw, 4),
                        "plug_load_kw": round(plug_kw, 4), "ventilation_power_kw": round(vent_kw, 4),
                        "equipment_power_kw": round(equip_kw, 4),
                        "energy_consumption_kwh": round(total * step_h, 5),
                        "hvac_energy_kwh": round((hvac_kw + vent_kw) * step_h, 5),
                        "lighting_energy_kwh": round(light_kw * step_h, 5),
                        "equipment_energy_kwh": round((plug_kw + equip_kw) * step_h, 5),
                        "current_demand_kw": round(total, 4), "hvac_demand_kw": round(hvac_kw + vent_kw, 4),
                        "cooling_demand_kw": round(cool_w / 1000, 4), "heating_demand_kw": round(heat_w / 1000, 4),
                        "ventilation_demand_kw": round(vent_kw, 4),
                        "occupancy_demand_factor": round(occ / cap_p, 3),
                        **com, "anomaly_ids": ",".join(active) or None,
                    }
                    z_rows.append(row)
                    c["prev_occ"][z] = occ
                    tr = c["truth"][z]
                    tr["temp"].append(temp); tr["rh"].append(rh); tr["co2"].append(co2)
                    tr["occ"].append(occ); tr["power"].append(total)
                    c["acc"][z] = _acc()
                    tb["total"] += total; tb["hvac"] += hvac_kw; tb["vent"] += vent_kw
                    tb["light"] += light_kw; tb["plug"] += plug_kw; tb["equip"] += equip_kw
                    tb["cool"] += cool_w / 1000; tb["heat"] += heat_w / 1000; tb["occ"] += occ
                    if occ > 0:
                        tb["comfort"].append(com["comfort_score"])
            wx0 = wx.at(ts0)
            pv = loads.pv_kw(bspec.pv_kwp, wx0["solar_irradiance_w_m2"], wx0["outdoor_temperature_c"])
            grid = max(0.0, tb["total"] - pv)
            if daily["date"] != ts0.date():
                daily.update(date=ts0.date(), kwh=0.0, peak=0.0)
            daily["kwh"] += tb["total"] * step_h
            daily["peak"] = max(daily["peak"], grid)
            key = (ts0.weekday() >= 5 or cal.holiday(ts0) is not None, ts0.hour * 60 + ts0.minute)
            forecast = fc_hist.get(key, grid)            # seasonal-naive; persistence before history
            fc_hist[key] = grid
            risk = grid / design_bkw if design_bkw else 0.0
            b_rows.append({
                "t": epoch(ts0), "ts": ts0.isoformat(timespec="minutes"), "building_id": bid,
                "operating_hours": int(building.is_open(prof, ts0.weekday(), ts0.hour + ts0.minute / 60)),
                "special_day": fctx[0]["occ"].labels[fctx[0]["occ"].slot(ts0)],
                "occupancy_count": tb["occ"], "occupancy_capacity": cap_people,
                "occupancy_percent": round(100.0 * tb["occ"] / cap_people, 1),
                "total_power_kw": round(tb["total"], 4), "hvac_power_kw": round(tb["hvac"], 4),
                "ventilation_power_kw": round(tb["vent"], 4), "lighting_power_kw": round(tb["light"], 4),
                "plug_load_kw": round(tb["plug"], 4), "equipment_power_kw": round(tb["equip"], 4),
                "renewable_power_kw": round(pv, 4), "grid_power_kw": round(grid, 4),
                "energy_consumption_kwh": round(tb["total"] * step_h, 5), "grid_energy_kwh": round(grid * step_h, 5),
                "daily_energy_kwh": round(daily["kwh"], 3), "peak_demand_kw": round(daily["peak"], 4),
                "current_demand_kw": round(grid, 4), "forecast_demand_kw": round(forecast, 4),
                "hvac_demand_kw": round(tb["hvac"] + tb["vent"], 4), "cooling_demand_kw": round(tb["cool"], 4),
                "heating_demand_kw": round(tb["heat"], 4), "ventilation_demand_kw": round(tb["vent"], 4),
                "occupancy_demand_factor": round(tb["occ"] / cap_people, 3),
                "peak_demand_risk": round(risk, 3), "demand_category": demand_category(risk),
                "comfort_score": round(sum(tb["comfort"]) / len(tb["comfort"]), 1) if tb["comfort"] else None,
            })
            if len(z_rows) >= 20_000:
                sink.write("zone_obs", z_rows); sink.write("building_obs", b_rows)
                counts["zone_obs"] += len(z_rows); counts["building_obs"] += len(b_rows)
                z_rows, b_rows = [], []
                if progress:
                    progress(f"{bid}: {ts0.date()}")
        sink.write("zone_obs", z_rows); sink.write("building_obs", b_rows)
        counts["zone_obs"] += len(z_rows); counts["building_obs"] += len(b_rows)

        # ---- RAW -> VALIDATE -> CLEAN, per zone (truth untouched)
        for c in fctx:
            for z in ZONE_IDS:
                zid = f"{c['fl']}-{z}"
                faults = [(_slot(a["_t0"], start, step), _slot(a["_t1"], start, step), a["anomaly_type"], a["params"])
                          for a in banoms if a["affected_zone"] == zid and a["layer"] == "sensor"]
                rng = random.Random(f"quality:{cfg.seed}:{bid}:{zid}")
                raw = quality.make_raw(c["truth"][z], cfg.quality, cfg.noise_level, step, rng, faults)
                iss = quality.validate(raw, c["occ"].capacity[z], c["design"][z] * 3)
                cl = quality.clean(raw, iss, cfg.quality)
                r_rows, c_rows = [], []
                for i in range(len(raw["temp"])):
                    t = epoch(start + timedelta(minutes=i * step))
                    r_rows.append({"t": t, "building_id": bid, "zone_id": zid,
                                   "temperature_raw_c": raw["temp"][i], "humidity_raw_percent": raw["rh"][i],
                                   "co2_raw_ppm": raw["co2"][i], "occupancy_raw_count": raw["occ"][i],
                                   "power_raw_kw": raw["power"][i], "reading_delay_s": raw["delay_s"][i],
                                   "raw_flags": raw["flags"][i] or None})
                    q = cl["quality"]
                    c_rows.append({"t": t, "building_id": bid, "zone_id": zid,
                                   "temperature_clean_c": cl["temp"][i], "humidity_clean_percent": cl["rh"][i],
                                   "co2_clean_ppm": cl["co2"][i], "occupancy_clean_count": cl["occ"][i],
                                   "power_clean_kw": cl["power"][i], "temperature_quality": q["temp"][i],
                                   "humidity_quality": q["rh"][i], "co2_quality": q["co2"][i],
                                   "occupancy_quality": q["occ"][i], "power_quality": q["power"][i]})
                sink.write("raw_obs", r_rows)
                sink.write("clean_obs", c_rows)
                counts["raw_obs"] += len(r_rows)
            c["truth"] = None
        if progress:
            progress(f"{bid}: done")

    meta = {"version": VERSION, "config": cfg.public_dict(), "timestamps": total_min // step,
            "row_counts": counts, "anomalies": len(anoms), "coil_adp_c": COIL_ADP_C,
            "data_state": "simulated history — generated by backend/dataset from sim/twin.py physics; "
                          "not live, not real, not hardware"}
    sink.finish(meta)
    return meta


def _acc() -> dict:
    return {"cool": 0.0, "heat": 0.0, "fan": 0.0, "hvac": 0.0, "light": 0.0, "plug": 0.0, "equip": 0.0,
            "n": 0, "last": (False, False, None, None, 0)}


def _slot(ts, start, step) -> int:
    return max(0, int((ts - start).total_seconds() // (step * 60)))


def _co2_step(c, z, occ, vent, bid, fl, idx, ts) -> None:
    """Same single-zone mass balance as backend/telemetry.py (ASHRAE 62.1 form), 60-s Euler."""
    zone = ZONE_BY_ID[z]
    extra = 0.0
    sp = idx.get(bid, f"{fl}-{z}", "co2_spike", ts)
    if sp:
        extra = sp["params"]["extra_people_equiv"]
    V = zone.area * CEILING_H
    Q = _outdoor_air_m3s(zone, vent)
    C = c["co2"][z]
    dC = (CO2_GEN_M3S_PER_PERSON * (occ + extra) * 1e6 - Q * (C - CO2_OUTDOOR_PPM)) / V
    c["co2"][z] = max(CO2_OUTDOOR_PPM, C + dC * 60.0)
