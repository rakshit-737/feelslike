"""Seasonal / environmental scenarios for the LIVE twin (Phase 5).

No second weather engine: the seasonal mode drives the twin with the Phase-2 temporal weather
model (backend/dataset/weather.py — AR(1) temperature and dew point, rain EVENTS, cloud
persistence, clear-sky solar) through the twin's existing hooks (weather_fn, rh_fn, solar_fn).
Operator overrides (cloud cover, rain, disturbance) are a thin layer OVER the model's output;
the model itself is never changed, so "Reset scenario" restores it exactly.

WEATHER MODELS
  classic   the original sim/weather.py August model (default; everything before Phase 5).
            It has no irradiance, cloud, rain or wind: those are reported "not in the classic
            weather model", never invented.
  seasonal  WeatherModel for season (summer | monsoon | winter | transition | auto) and
            climate (delhi | chennai), anchored on a representative Monday so the sim's
            weekday stays aligned, repeating every 28 days. Seasonal mode also uses a fixed
            12 °C coil apparatus dew point (the dataset's humidity model), because the classic
            ADP approximation pins indoor RH near 88 %; classic mode keeps it (documented).
PHYSICS KNOBS (all bounded; applied to BOTH twins so the A/B comparison stays fair)
  existing  outdoor_offset, humidity_offset, solar_scale, occ_scale, capacity_scale  (set_conditions)
  new       envelope_scale  multiplies every zone's envelope UA (twin hook, 1.0 = unchanged)
            internal_load   people_only (twin default: 100 W/person + solar)
                            | profile (Phase-2 loads: lighting + plugs + equipment/IT of the
                              building profile as sensible gains, HVAC sized for them)
HEATING  the live controller (ConstraintAware) is cooling-only, so heating is NOT MODELLED
         in the live twin; a cold room shows up as cold, never as invented heating energy.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from backend import building, comfort, loads_bridge
from backend.dataset.calendar import season_for
from backend.dataset.weather import CLIMATE, WeatherModel, clear_sky_ghi, heat_index, wet_bulb
from sim.humidity import dew_point
from sim.twin import _OCC_PEAK, COP, DT, FAN_W, ZONES
from sim.weather import outdoor_temp

MODES = ("classic", "seasonal")
SEASONS = ("auto", "summer", "monsoon", "winter", "transition")
CLIMATES = tuple(CLIMATE)
LATITUDE = {"delhi": 28.61, "chennai": 12.84}
ANCHOR = {"summer": datetime(2026, 5, 18), "monsoon": datetime(2026, 7, 20), "winter": datetime(2026, 1, 12),
          "transition": datetime(2026, 10, 19), "auto": datetime(2026, 9, 14)}      # all Mondays
PERIOD_DAYS = 28
SEASONAL_COIL_ADP_C = 12.0
INTERNAL_LOADS = ("people_only", "profile")
KNOB_LIMITS = {"outdoor_offset": (-10.0, 10.0), "humidity_offset": (-30.0, 30.0), "solar_scale": (0.0, 2.0),
               "occ_scale": (0.0, 3.0), "capacity_scale": (0.1, 1.5)}
LIMITS = {"cloud_cover": (0.0, 100.0), "rain_mm_h": (0.0, 50.0), "envelope_scale": (0.5, 2.0),
          "disturbance_delta_c": (-8.0, 8.0), "disturbance_hours": (0.25, 48.0)}
SPEED_PRESETS = [("0.25×", 60.0), ("0.5×", 120.0), ("1×", 240.0), ("2×", 480.0), ("5×", 1200.0), ("10×", 2400.0)]
HEATING_NOTE = "NOT MODELLED — the live controller is cooling-only"


@dataclass
class ScenarioConfig:
    mode: str = "classic"
    season: str = "summer"
    climate: str = "delhi"
    cloud_cover: float | None = None       # None = the weather model decides
    rain_mm_h: float | None = None         # None = model rain events; 0 = force dry
    envelope_scale: float = 1.0
    internal_load: str = "people_only"
    disturbance: dict | None = None        # {"delta_c", "t0", "t1"} in sim seconds

    def validate(self) -> list:
        e = []
        if self.mode not in MODES:
            e.append(f"mode must be one of {MODES}")
        if self.season not in SEASONS:
            e.append(f"season must be one of {SEASONS}")
        if self.climate not in CLIMATES:
            e.append(f"climate must be one of {CLIMATES}")
        if self.internal_load not in INTERNAL_LOADS:
            e.append(f"internal_load must be one of {INTERNAL_LOADS}")
        for k, (lo, hi) in (("cloud_cover", LIMITS["cloud_cover"]), ("rain_mm_h", LIMITS["rain_mm_h"]),
                            ("envelope_scale", LIMITS["envelope_scale"])):
            v = getattr(self, k)
            if v is None and k != "envelope_scale":
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                e.append(f"{k} must be between {lo:g} and {hi:g}")
        return e


class WeatherScenario:
    """The seasonal weather the live twin sees, with the operator override layer on top."""

    def __init__(self, season: str, climate: str, seed: int, cloud_cover=None, rain_mm_h=None,
                 disturbance: dict | None = None):
        self.season, self.climate = season, climate
        self.anchor = ANCHOR[season]
        self.lat = LATITUDE[climate]
        season_of = (lambda ts: season_for(ts, "auto")) if season == "auto" else (lambda ts: season)
        self.model = WeatherModel(self.anchor, PERIOD_DAYS + 1, seed, climate, season_of, self.lat)
        self.cloud_cover, self.rain_mm_h, self.disturbance = cloud_cover, rain_mm_h, disturbance
        self._k, self._v = None, None

    def ts(self, t: float) -> datetime:
        return self.anchor + timedelta(seconds=t % (PERIOD_DAYS * 86400))

    def at(self, t: float) -> dict:
        k = int(t // 60)
        if k == self._k:
            return self._v
        ts = self.ts(t)
        w = dict(self.model.at(ts))
        cloud = w["cloud_cover_percent"] if self.cloud_cover is None else float(self.cloud_cover)
        rain = w["rainfall_mm"] if self.rain_mm_h is None else float(self.rain_mm_h)
        if self.cloud_cover is not None or self.rain_mm_h is not None:
            c = cloud / 100.0
            w["solar_irradiance_w_m2"] = round(clear_sky_ghi(ts, self.lat) * (1 - 0.75 * c ** 3.4)
                                              * (0.35 if rain > 0 else 1.0), 1)
        w["cloud_cover_percent"], w["rainfall_mm"] = round(cloud, 1), round(rain, 2)
        w["weather_condition"] = ("heavy_rain" if rain > 5 else "rain" if rain > 0 else "overcast" if cloud > 75
                                  else "partly_cloudy" if cloud > 40 else "clear")
        d = self.disturbance
        if d and d["t0"] <= t < d["t1"]:
            w["outdoor_temperature_c"] = round(w["outdoor_temperature_c"] + d["delta_c"], 2)
        w["season"] = self.model.season_of(ts)
        self._k, self._v = k, w
        return w

    def weather_fn(self, t):
        return self.at(t)["outdoor_temperature_c"]

    def rh_fn(self, t):
        return self.at(t)["outdoor_humidity_percent"]

    def solar_fn(self, orientation, t):
        return self.model.solar_factor(orientation, self.ts(t), lambda _ts: self.at(t))


def make_weather(cfg: ScenarioConfig, seed: int) -> WeatherScenario | None:
    if cfg.mode != "seasonal":
        return None
    return WeatherScenario(cfg.season, cfg.climate, seed, cfg.cloud_cover, cfg.rain_mm_h, cfg.disturbance)


def apply_to_twin(twin, cfg: ScenarioConfig, ws: WeatherScenario | None, profile, climate: str) -> None:
    """Point one twin's existing hooks at the scenario. Classic restores the original functions."""
    if ws is None:
        seed = twin.seed
        twin.weather_fn = lambda t, s=seed: outdoor_temp(t, s)
        twin.rh_fn = twin.solar_fn = None
        twin.coil_adp_c = None
    else:
        twin.weather_fn, twin.rh_fn, twin.solar_fn = ws.weather_fn, ws.rh_fn, ws.solar_fn
        twin.coil_adp_c = SEASONAL_COIL_ADP_C
    twin.envelope_scale = float(cfg.envelope_scale)
    if cfg.internal_load == "profile":
        twin.gain_fn = loads_bridge.gain_fn(twin, profile, ws)
        twin.capacity_w = loads_bridge.capacities(profile, climate)
    else:
        twin.gain_fn = None
        twin.capacity_w = None


def weather_snapshot(twin, ws: WeatherScenario | None) -> dict:
    """What the twin is experiencing now (offsets included). Classic has only T and RH."""
    t = twin.t
    t_out = float(twin.weather_fn(t)) + twin.outdoor_offset
    rh = twin._outdoor_rh_at(t)
    snap = {"outdoor_temperature": round(t_out, 2), "outdoor_humidity": round(rh, 1),
            "dew_point": round(dew_point(t_out, rh), 2), "heat_index": round(heat_index(t_out, rh), 2),
            "wet_bulb": round(wet_bulb(t_out, rh), 2)}
    if ws is None:
        missing = "not in the classic weather model"
        snap.update({"wind_speed": None, "wind_direction": None, "solar_irradiance": None, "cloud_cover": None,
                     "rainfall": None, "weather_condition": None, "season": None, "_note": missing,
                     "model": "classic (sim/weather.py, August)"})
        return snap
    w = ws.at(t)
    snap.update({"wind_speed": w["outdoor_wind_speed_mps"], "wind_direction": w["outdoor_wind_direction"],
                 "solar_irradiance": round(w["solar_irradiance_w_m2"] * twin.solar_scale, 1),
                 "cloud_cover": w["cloud_cover_percent"], "rainfall": w["rainfall_mm"],
                 "weather_condition": w["weather_condition"], "season": w["season"], "_note": None,
                 "model": f"seasonal ({ws.season}, {ws.climate})",
                 "overrides": {"cloud_cover": ws.cloud_cover, "rain_mm_h": ws.rain_mm_h,
                               "disturbance": ws.disturbance}})
    return snap


def summary(cfg: ScenarioConfig, twin, ws) -> dict:
    return {**asdict(cfg), "knobs": {k: getattr(twin, k) for k in KNOB_LIMITS},
            "humidity_model": ("classic ADP approximation (setpoint − 2 K; indoor RH pinned high — known limitation)"
                               if cfg.mode == "classic" else f"fixed coil apparatus dew point {SEASONAL_COIL_ADP_C:g} °C"),
            "heating": HEATING_NOTE, "weather_now": weather_snapshot(twin, ws),
            "limits": {**{k: list(v) for k, v in KNOB_LIMITS.items()}, **{k: list(v) for k, v in LIMITS.items()}},
            "speed_presets": [{"label": a, "speed": b} for a, b in SPEED_PRESETS],
            "options": {"modes": MODES, "seasons": SEASONS, "climates": CLIMATES, "internal_loads": INTERNAL_LOADS}}


# ------------------------------------------------------------------ causal explanation
def causal(tel, lag_s: float = 15 * 60.0) -> dict:
    """Explain the building's current response with the telemetry buffer's own numbers
    (now vs lag_s sim-seconds ago). No text is produced for a quantity that does not exist."""
    cur = tel.latest()
    if cur is None:
        return {"available": False, "chain": [], "sentence": "No current data available"}
    prev = tel.at_or_before(cur["t"] - lag_s)
    prev = prev if prev is not cur else None

    def agg(r):
        zs = r["zones"]
        cap = sum(z["cool_w"] / max(1e-6, z["capacity_pct"] / 100.0) if z["capacity_pct"] > 0 else 0.0
                  for z in zs.values())
        cool = sum(z["cool_w"] for z in zs.values())
        occ = [z for z in zs.values() if z["occ"] > 0] or list(zs.values())
        design = sum(_OCC_PEAK.values()) or 1
        return {"outdoor_temperature": r["t_out"], "occupancy_pct": round(100.0 * r["occ"] / design, 1),
                "cooling_w": round(cool, 0), "hvac_power_w": r["power_w"],
                "cooling_capacity_pct": round(max((z["capacity_pct"] for z in zs.values()), default=0.0), 1),
                "indoor_temperature": round(sum(z["temp"] for z in occ) / len(occ), 2),
                "energy_kwh": r["kwh_us"], "at_capacity_zones": sum(1 for z in zs.values() if z["at_capacity"]),
                "_cap": cap}
    a, b = agg(cur), (agg(prev) if prev else None)

    def item(key, label, unit, dec=1):
        v = a[key]
        d = None if b is None else round(v - b[key], dec)
        return {"key": key, "label": label, "value": round(v, dec), "unit": unit, "delta": d,
                "direction": None if d is None or abs(d) < 10 ** -dec else ("up" if d > 0 else "down")}
    chain = [item("outdoor_temperature", "Outdoor temperature", "°C"),
             item("occupancy_pct", "Occupancy", "% of design", 0),
             item("cooling_w", "Cooling demand (thermal)", "W", 0),
             item("cooling_capacity_pct", "Highest zone cooling capacity used", "%", 0),
             item("hvac_power_w", "HVAC electrical load", "W", 0),
             item("indoor_temperature", "Indoor temperature (occupied avg)", "°C", 2)]
    energy_rate = None if b is None else round((a["energy_kwh"] - b["energy_kwh"]) / (lag_s / 3600.0), 2)
    arrow = {"up": "rose", "down": "fell", None: "held"}
    c = {x["key"]: x for x in chain}
    cool, out_t, occ_p = c["cooling_w"], c["outdoor_temperature"], c["occupancy_pct"]
    cool_change = arrow[cool["direction"]] + ("" if cool["delta"] is None or cool["direction"] is None
                                              else f" by {abs(cool['delta']):,.0f} W")
    out_change = arrow[out_t["direction"]] + ("" if out_t["delta"] is None or out_t["direction"] is None
                                              else f" {abs(out_t['delta']):.1f} K")
    s = (f"Cooling demand is {cool['value']:,.0f} W ({cool_change} in the last {lag_s / 60:.0f} sim-min) "
         f"with the outdoor temperature at {out_t['value']:.1f} °C ({out_change}) "
         f"and occupancy at {occ_p['value']:.0f} % of design. ")
    s += (f"The busiest zone uses {c['cooling_capacity_pct']['value']:.0f} % of its cooling capacity"
          + (f"; {a['at_capacity_zones']} zone(s) are AT CAPACITY, so indoor temperature can rise" if a["at_capacity_zones"]
             else "") + f". HVAC electrical load is {c['hvac_power_w']['value']:,.0f} W")
    if energy_rate is not None:
        s += f", consuming {energy_rate:.2f} kWh per sim-hour"
    s += "."
    return {"available": True, "chain": chain, "sentence": s, "at_capacity_zones": a["at_capacity_zones"],
            "energy_kwh_per_sim_hour": energy_rate, "window_s": lag_s, "source": "derived",
            "t": cur["t"], "prev_t": prev["t"] if prev else None}


# ------------------------------------------------------------------ season comparison
def compare_seasons(twin, store, controller_factory, cfg: ScenarioConfig, profile, horizon_h: float = 6.0,
                    seasons=("summer", "monsoon", "winter", "transition"), co2_init: dict | None = None) -> dict:
    """PREDICTED / SIMULATED WHAT-IF: the same building, same start state, same controller,
    stepped for horizon_h under each season's weather (current envelope, loads and knobs)."""
    from backend import telemetry
    if not 0 < horizon_h <= 24:
        raise ValueError("horizon_h must be > 0 and <= 24")
    steps = int(round(horizon_h * 3600 / DT))
    out = {}
    for season in seasons:
        sc = ScenarioConfig(**{**asdict(cfg), "mode": "seasonal", "season": season, "disturbance": None})
        ws = make_weather(sc, twin.seed)
        tw, st = twin.clone(), store.clone()
        apply_to_twin(tw, sc, ws, profile, sc.climate)
        ctrl = controller_factory()
        tel = telemetry.TelemetryStore(capacity=steps + 2)
        tel.co2.update({k: float(v) for k, v in (co2_init or {}).items() if k in tel.co2})
        kwh0 = tw.kwh
        acc = {"t_out": 0.0, "indoor": [], "cool": 0.0, "power": 0.0, "peak": 0.0, "capmax": 0.0, "atcap": 0.0,
               "rh": [], "solar": 0.0, "comfort": [], "rain_steps": 0}
        for _ in range(steps):
            sps, vents = ctrl.act(tw, st)
            kb = dict(tw.kwh_by_zone)
            tw.step(sps, vents)
            pw = {z.id: (tw.kwh_by_zone[z.id] - kb[z.id]) * 3.6e6 / DT for z in ZONES}
            cw = {z.id: max(0.0, (pw[z.id] - FAN_W.get(int(vents.get(z.id, 0) or 0), 0.0)) * COP) for z in ZONES}
            row = tel.record(tw, None, pw, cw)
            w = ws.at(tw.t)
            acc["t_out"] += row["t_out"]
            acc["solar"] += w["solar_irradiance_w_m2"]
            acc["rain_steps"] += 1 if w["rainfall_mm"] > 0 else 0
            acc["cool"] += sum(cw.values())
            acc["power"] += row["power_w"]
            acc["peak"] = max(acc["peak"], row["power_w"])
            for zid, z in row["zones"].items():
                acc["capmax"] = max(acc["capmax"], z["capacity_pct"])
                if z["occ"] > 0:
                    acc["indoor"].append(z["temp"])
                    acc["rh"].append(z["rh"])
                    a = comfort.assess(comfort.ComfortReading(zone_id=zid, temp_c=z["temp"], rh_pct=z["rh"],
                                                              co2_ppm=z["co2"], occupancy=z["occ"],
                                                              occupancy_pct=z["occ_pct"]), profile)
                    if a["score"] is not None:
                        acc["comfort"].append(a["score"])
                    if z["at_capacity"]:
                        acc["atcap"] += DT / 60.0
        mean = lambda xs: round(sum(xs) / len(xs), 2) if xs else None     # noqa: E731
        out[season] = {"outdoor_temperature_c": round(acc["t_out"] / steps, 2),
                       "indoor_temperature_c": mean(acc["indoor"]), "indoor_humidity_pct": mean(acc["rh"]),
                       "cooling_demand_w": round(acc["cool"] / steps, 0),
                       "max_cooling_capacity_pct": round(acc["capmax"], 1),
                       "heating_demand": HEATING_NOTE, "hvac_power_w": round(acc["power"] / steps, 0),
                       "energy_kwh": round(tw.kwh - kwh0, 3), "comfort_score": mean(acc["comfort"]),
                       "peak_demand_w": round(acc["peak"], 0), "at_capacity_zone_min": round(acc["atcap"], 0),
                       "solar_irradiance_w_m2": round(acc["solar"] / steps, 1),
                       "rain_hours": round(acc["rain_steps"] * DT / 3600.0, 2)}
    return {"kind": "predicted", "label": "SIMULATED / WHAT-IF", "horizon_h": horizon_h, "start_t": twin.t,
            "climate": cfg.climate, "seasons": out,
            "not_modelled": {"heating_demand": HEATING_NOTE},
            "note": ("Each season steps a clone of the live twin (same starting indoor state, same controller, "
                     "current envelope / internal loads / knobs) under that season's weather model. Indoor "
                     "conditions carry the starting state for the first hours.")}
