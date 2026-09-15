"""Seasonal temporal weather model (SIMULATED), hourly state interpolated to any time.

NOT independent noise per column. One hourly state machine, seeded:
  * cloud cover  - AR(1) around a seasonal mean, pushed to overcast during rain
  * rain         - EVENTS: Poisson starts (seasonal rate, afternoon-weighted),
                   duration and intensity drawn per event, never per-timestep noise
  * temperature  - seasonal mean + day-of-period seasonal drift + diurnal cycle
                   (amplitude damped by cloud) + AR(1) synoptic anomaly + rain cooling
                   + optional disturbance offsets (heat spike / cold snap)
  * dew point    - seasonal mean + slow AR(1); capped at the dry-bulb; rain -> near
                   saturation. RH follows from (T, Td) with the Magnus inverse, so RH
                   is anti-correlated with the diurnal temperature cycle for free
  * pressure     - seasonal mean + AR(1), dips during rain
  * wind         - AR(1) speed (gusty in rain), direction random walk
  * solar        - clear-sky GHI from solar geometry (latitude, day of year, hour)
                   x Kasten-Czeplak cloud factor (1 - 0.75 c^3.4) x rain factor
Derived: dew point (Magnus), heat index (NOAA Rothfusz, valid >= 27 degC, else T),
wet bulb (Stull 2011, valid RH 5-99 %, T -20..50 degC).
Climates are parameter tables (delhi, chennai); seasons are keys inside them.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from sim.humidity import dew_point, sat_pressure

# climate -> season -> parameters
CLIMATE = {
    "delhi": {
        "summer":     {"t_mean": 33.0, "t_amp": 7.0, "td_mean": 12.0, "cloud": 0.15, "rain_per_day": 0.05, "p": 1002.0, "wind": 3.0},
        "monsoon":    {"t_mean": 30.5, "t_amp": 4.0, "td_mean": 24.5, "cloud": 0.60, "rain_per_day": 0.9, "p": 998.0, "wind": 2.5},
        "winter":     {"t_mean": 14.5, "t_amp": 7.0, "td_mean": 7.5, "cloud": 0.20, "rain_per_day": 0.08, "p": 1018.0, "wind": 1.8},
        "transition": {"t_mean": 24.0, "t_amp": 6.5, "td_mean": 14.0, "cloud": 0.20, "rain_per_day": 0.08, "p": 1011.0, "wind": 2.0},
    },
    "chennai": {
        "summer":     {"t_mean": 32.5, "t_amp": 4.5, "td_mean": 24.0, "cloud": 0.25, "rain_per_day": 0.1, "p": 1004.0, "wind": 4.0},
        "monsoon":    {"t_mean": 30.0, "t_amp": 4.0, "td_mean": 24.0, "cloud": 0.55, "rain_per_day": 0.45, "p": 1003.0, "wind": 3.5},
        "winter":     {"t_mean": 25.5, "t_amp": 4.5, "td_mean": 20.0, "cloud": 0.40, "rain_per_day": 0.35, "p": 1012.0, "wind": 3.0},
        "transition": {"t_mean": 28.0, "t_amp": 4.5, "td_mean": 23.0, "cloud": 0.50, "rain_per_day": 0.6, "p": 1008.0, "wind": 3.0},
    },
}
DIRS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def heat_index(t_c: float, rh: float) -> float:
    """NOAA Rothfusz regression (degC in/out). Below 27 degC returns t_c."""
    if t_c < 27.0:
        return t_c
    f = t_c * 9 / 5 + 32
    hi = (-42.379 + 2.04901523 * f + 10.14333127 * rh - 0.22475541 * f * rh
          - 6.83783e-3 * f * f - 5.481717e-2 * rh * rh + 1.22874e-3 * f * f * rh
          + 8.5282e-4 * f * rh * rh - 1.99e-6 * f * f * rh * rh)
    return (hi - 32) * 5 / 9


def wet_bulb(t_c: float, rh: float) -> float:
    """Stull (2011) empirical wet-bulb, degC. Clamped to its validity range."""
    rh = min(99.0, max(5.0, rh))
    return (t_c * math.atan(0.151977 * math.sqrt(rh + 8.313659)) + math.atan(t_c + rh)
            - math.atan(rh - 1.676331) + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
            - 4.686035)


def clear_sky_ghi(ts: datetime, latitude: float) -> float:
    """Clear-sky global horizontal irradiance, W/m2 (Haurwitz model on solar elevation)."""
    doy = ts.timetuple().tm_yday
    decl = math.radians(23.44) * math.sin(2 * math.pi * (284 + doy) / 365.0)
    solar_hour = ts.hour + ts.minute / 60.0          # local solar time approximation
    hra = math.radians(15.0 * (solar_hour - 12.0))
    lat = math.radians(latitude)
    cosz = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hra)
    if cosz <= 0.0:
        return 0.0
    return 1098.0 * cosz * math.exp(-0.057 / cosz)


class WeatherModel:
    """Hourly weather states for [start, start+days]; `at(ts)` interpolates linearly."""

    def __init__(self, start: datetime, days: int, seed: int, climate: str = "delhi",
                 season_of=None, latitude: float = 28.61):
        self.start, self.days, self.latitude = start, int(days), latitude
        self.params = CLIMATE[climate]
        self.season_of = season_of            # callable(datetime) -> season key
        self.rng = random.Random(f"weather:{seed}:{climate}")
        self.disturb: list = []               # (t0, t1, dT) from anomalies
        self.hours = self._simulate()

    def add_disturbance(self, t0: datetime, t1: datetime, delta_c: float) -> None:
        self.disturb.append((t0, t1, float(delta_c)))

    def _simulate(self) -> list:
        rng, out = self.rng, []
        n = self.days * 24 + 2
        anom_t, anom_td, anom_p, cloud, wind = 0.0, 0.0, 0.0, None, None
        wdir = rng.randrange(8)
        rain_left, rain_rate = 0, 0.0
        for i in range(n):
            ts = self.start + timedelta(hours=i)
            sp = self.params[self.season_of(ts)]
            h = ts.hour
            # --- rain events: seasonal daily rate, afternoon-weighted start prob
            if rain_left <= 0:
                weight = 1.8 if 13 <= h <= 19 else 0.6
                if rng.random() < sp["rain_per_day"] / 24.0 * weight:
                    rain_left = rng.randint(1, 5)
                    rain_rate = rng.lognormvariate(math.log(2.5), 0.8)   # mm/h
            raining = rain_left > 0
            if raining:
                rain_left -= 1
            # --- clouds: AR(1), overcast in rain
            if cloud is None:
                cloud = sp["cloud"]
            target = 0.95 if raining else sp["cloud"]
            cloud = min(1.0, max(0.0, cloud + 0.25 * (target - cloud) + rng.gauss(0, 0.07)))
            # --- temperature: diurnal (min ~06, max ~15) damped by cloud + AR(1) anomaly
            anom_t = 0.92 * anom_t + rng.gauss(0, 0.45)
            diurnal = sp["t_amp"] * (1 - 0.5 * cloud) * math.sin(math.pi * (h - 9.0) / 12.0)
            t = sp["t_mean"] + diurnal + anom_t - (2.5 if raining else 0.0)
            # --- dew point: slow AR(1), rain saturates, never above dry bulb
            anom_td = 0.96 * anom_td + rng.gauss(0, 0.3)
            td = sp["td_mean"] + anom_td + (1.5 if raining else 0.0)
            td = min(td, t - (0.3 if raining else 1.0))
            # --- pressure / wind
            anom_p = 0.95 * anom_p + rng.gauss(0, 0.35)
            p = sp["p"] + anom_p - (2.0 if raining else 0.0)
            if wind is None:
                wind = sp["wind"]
            wind = max(0.0, wind + 0.3 * (sp["wind"] * (1.8 if raining else 1.0) - wind)
                       + rng.gauss(0, 0.4) + 0.8 * math.sin(math.pi * (h - 10) / 12.0) * 0.3)
            wdir = (wdir + rng.choice((-1, 0, 0, 0, 1))) % 8
            out.append({"t": t, "td": td, "cloud": cloud, "rain": rain_rate if raining else 0.0,
                        "p": p, "wind": wind, "wdir": wdir})
        return out

    def at(self, ts: datetime) -> dict:
        """Weather at ts. Continuous fields interpolated; rain and direction held per hour."""
        x = (ts - self.start).total_seconds() / 3600.0
        i = max(0, min(len(self.hours) - 2, int(x)))
        k = min(1.0, max(0.0, x - i))
        a, b = self.hours[i], self.hours[i + 1]
        lerp = lambda key: a[key] + (b[key] - a[key]) * k
        t = lerp("t")
        for t0, t1, d in self.disturb:
            if t0 <= ts < t1:
                t += d
        td = min(lerp("td"), t)
        rh = min(100.0, max(1.0, 100.0 * sat_pressure(td) / sat_pressure(t)))
        cloud = lerp("cloud")
        rain = a["rain"]
        ghi = clear_sky_ghi(ts, self.latitude) * (1 - 0.75 * cloud ** 3.4) * (0.35 if rain > 0 else 1.0)
        if rain > 5:
            cond = "heavy_rain"
        elif rain > 0:
            cond = "rain"
        elif cloud > 0.75:
            cond = "overcast"
        elif cloud > 0.4:
            cond = "partly_cloudy"
        else:
            cond = "clear"
        return {
            "outdoor_temperature_c": round(t, 2),
            "outdoor_humidity_percent": round(rh, 1),
            "outdoor_pressure_hpa": round(lerp("p"), 1),
            "outdoor_wind_speed_mps": round(lerp("wind"), 2),
            "outdoor_wind_direction": DIRS[a["wdir"]],
            "solar_irradiance_w_m2": round(max(0.0, ghi), 1),
            "cloud_cover_percent": round(100.0 * cloud, 1),
            "rainfall_mm": round(rain, 2),           # mm per hour (rate)
            "weather_condition": cond,
            "dew_point_c": round(dew_point(t, rh), 2),
            "heat_index_c": round(heat_index(t, rh), 2),
            "wet_bulb_temperature_c": round(wet_bulb(t, rh), 2),
        }

    def solar_factor(self, orientation: str, ts: datetime, at=None) -> float:
        """Facade factor 0..1 for the twin: irradiance share x orientation timing.
        `at` lets a caller pass a memoised self.at (the generator does)."""
        g = (at or self.at)(ts)["solar_irradiance_w_m2"] / 1000.0
        h = ts.hour + ts.minute / 60.0
        peaks = {"E": 9.0, "S": 12.5, "W": 16.0}
        if orientation not in peaks:
            return min(1.0, 0.35 * g)
        return min(1.0, g * (0.35 + 0.65 * math.exp(-((h - peaks[orientation]) ** 2) / (2 * 2.6 ** 2))))
