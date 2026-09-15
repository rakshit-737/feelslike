"""Read-side analysis over the store: joined series, comparisons, correlations, stats."""
from __future__ import annotations

import math

from backend.dataset import schema as S

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
# resolution chosen from the span so no chart gets thousands of points
AUTO_INTERVAL = [(6 * 3600, 300), (86400, 900), (7 * 86400, 3600), (31 * 86400, 3 * 3600), (10 ** 9, 86400)]
INTERVALS = {"5min": 300, "15min": 900, "1h": 3600, "3h": 10800, "1d": 86400}
MAX_POINTS = 1000

PAIRS = {
    "indoor_vs_outdoor_temperature": ("outdoor_temperature_c", "indoor_temperature_c"),
    "occupancy_vs_energy": ("occupancy_count", "total_power_kw"),
    "occupancy_vs_co2": ("occupancy_count", "indoor_co2_ppm"),
    "outdoor_temperature_vs_hvac_load": ("outdoor_temperature_c", "hvac_power_kw"),
    "hvac_load_vs_energy": ("hvac_power_kw", "energy_consumption_kwh"),
    "comfort_vs_energy": ("total_power_kw", "comfort_score"),
    "cloud_cover_vs_solar": ("cloud_cover_percent", "solar_irradiance_w_m2"),
    "solar_vs_renewable": ("solar_irradiance_w_m2", "renewable_power_kw"),
}


def auto_interval(span_s: int) -> int:
    for lim, iv in AUTO_INTERVAL:
        if span_s <= lim:
            return iv
    return 86400


def pearson(xs: list, ys: list):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    syy = sum((y - my) ** 2 for _, y in pts)
    return None if sxx <= 0 or syy <= 0 else round(sxy / math.sqrt(sxx * syy), 3)


def joined(store, building_id, t0, t1, interval_s, level="building", floor_id=None, zone_id=None,
           metrics=None) -> list:
    """Zone-derived aggregates + site weather (+ building-only fields at building level), by bucket."""
    rows = {r["t"]: r for r in store.series(building_id, t0, t1, interval_s, level, floor_id, zone_id, metrics)}
    for w in store.weather_series(t0, t1, interval_s):
        if w["t"] in rows:
            rows[w["t"]].update({k: v for k, v in w.items() if k != "t"})
    if level == "building":
        for b in store.building_series(building_id, t0, t1, interval_s):
            if b["t"] in rows:
                rows[b["t"]].update({k: v for k, v in b.items() if k != "t"})
    out = [rows[k] for k in sorted(rows)]
    for r in out:
        for k, v in r.items():
            if isinstance(v, float):
                r[k] = round(v, 4)
    return out


DAYLIGHT_ONLY = {"cloud_cover_vs_solar", "solar_vs_renewable"}


def compare(rows: list, pair: str) -> dict:
    """Pearson r over bucketed rows. Solar pairs use 10:00-15:00 buckets only: at night
    irradiance is zero whatever the sky does, which would swamp the cloud effect."""
    x, y = PAIRS[pair]
    if pair in DAYLIGHT_ONLY:
        rows = [r for r in rows if 10 <= (r["t"] % 86400) // 3600 < 15]
    xs, ys = [r.get(x) for r in rows], [r.get(y) for r in rows]
    return {"pair": pair, "x": x, "y": y, "r": pearson(xs, ys), "n": len(rows),
            "points": [{"t": r["t"], "x": r.get(x), "y": r.get(y)} for r in rows]}


def demand_profile(store, building_id, t0, t1) -> dict:
    """Current (the latest day in range) vs historical (all earlier days) hourly demand."""
    rows = store.q("SELECT b.t, b.grid_power_kw, d.hour, d.date FROM building_obs b JOIN time_dim d ON d.t = b.t "
                   "WHERE b.building_id = ? AND b.t >= ? AND b.t < ? ORDER BY b.t", [building_id, t0, t1])
    if not rows:
        return {"hours": [], "current_date": None}
    last = rows[-1]["date"]
    hist = {h: [] for h in range(24)}
    cur = {h: [] for h in range(24)}
    for r in rows:
        (cur if r["date"] == last else hist)[r["hour"]].append(r["grid_power_kw"])

    def q(v, p):
        if not v:
            return None
        s = sorted(v)
        return round(s[min(len(s) - 1, int(p * (len(s) - 1)))], 3)
    return {"current_date": last, "hours": [
        {"hour": h, "current_kw": round(sum(cur[h]) / len(cur[h]), 3) if cur[h] else None,
         "historical_mean_kw": round(sum(hist[h]) / len(hist[h]), 3) if hist[h] else None,
         "historical_p10_kw": q(hist[h], 0.1), "historical_p90_kw": q(hist[h], 0.9),
         "samples": len(hist[h])} for h in range(24)]}


def summary(store) -> dict:
    """Per-building summary statistics + key correlations (hourly buckets, full span)."""
    cat = store.catalog()
    t0, t1 = cat["t_start"], cat["t_end"] + 1
    out = {}
    for b in cat["buildings"]:
        bid = b["building_id"]
        agg = store.q("SELECT AVG(total_power_kw) AS mean_total_kw, MAX(total_power_kw) AS max_total_kw, "
                      "AVG(renewable_power_kw) AS mean_pv_kw, SUM(energy_consumption_kwh) AS energy_kwh, "
                      "SUM(grid_energy_kwh) AS grid_kwh, AVG(occupancy_percent) AS mean_occ_pct, "
                      "MAX(peak_demand_kw) AS peak_kw, AVG(comfort_score) AS mean_comfort "
                      "FROM building_obs WHERE building_id = ?", [bid])[0]
        env = store.q("SELECT AVG(indoor_temperature_c) AS mean_temp, MIN(indoor_temperature_c) AS min_temp, "
                      "MAX(indoor_temperature_c) AS max_temp, AVG(indoor_humidity_percent) AS mean_rh, "
                      "AVG(indoor_co2_ppm) AS mean_co2, MAX(indoor_co2_ppm) AS max_co2, AVG(pmv) AS mean_pmv, "
                      "SUM(heating_demand_kw > 0) AS heating_samples FROM zone_obs WHERE building_id = ?", [bid])[0]
        rows = joined(store, bid, t0, t1, 3600)
        occ_rows = [r for r in rows if (r.get("occupancy_count") or 0) > 0]
        out[bid] = {"type": b["building_type"], **{k: (round(v, 3) if isinstance(v, float) else v)
                                                  for k, v in {**agg, **env}.items()},
                    "correlations_hourly": {p: compare(rows, p)["r"] for p in PAIRS},
                    "occupied_hours_r_outdoor_vs_hvac": compare(occ_rows, "outdoor_temperature_vs_hvac_load")["r"]}
    return {"buildings": out, "row_counts": cat["row_counts"], "timestamps": cat["timestamps"]}


VALID_METRICS = set(S.NUMERIC_ZONE_METRICS)
