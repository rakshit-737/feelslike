"""Phase 2 historical dataset: generation, relationships, consistency, quality, store, API.

A small dataset (3 days from Friday 2026-06-05, office x2 floors + mall + data centre,
5-min, high anomaly rate) is generated ONCE per module into a temp SQLite file; a
1-day winter run exercises heating. Tests assert physics-level relationships, not
just "the script ran".
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime

import pytest

from backend.dataset import analysis, comfort, quality
from backend.dataset.anomalies import schedule as anomaly_schedule
from backend.dataset.config import AnomalyConfig, BuildingSpec, DatasetConfig, QualityConfig
from backend.dataset.generator import generate
from backend.dataset.store import MemorySink, SqliteStore
from backend.dataset.weather import WeatherModel

STEP = 5


def _cfg(**kw):
    base = dict(start="2026-06-05", days=3, step_min=STEP, seed=7,
                buildings=[BuildingSpec("b-office", "office", floors=2, pv_kwp=15.0),
                           BuildingSpec("b-mall", "mall", floors=1, pv_kwp=10.0),
                           BuildingSpec("b-dc", "data_center", floors=1)],
                anomalies=AnomalyConfig(rate_per_building_day=0.7))
    base.update(kw)
    return DatasetConfig(**base)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("ds") / "hist.sqlite"
    s = SqliteStore(path)
    s.create()
    generate(_cfg(db_path=str(path)), s)
    yield SqliteStore(path)


@pytest.fixture(scope="module")
def winter():
    sink = MemorySink()
    generate(DatasetConfig(start="2026-01-12", days=1, step_min=15, seed=3, season="winter",
                           buildings=[BuildingSpec("b-w", "office", floors=1)],
                           anomalies=AnomalyConfig(enabled=False)), sink)
    return sink


# ------------------------------------------------------------ generation + continuity
def test_row_counts_and_timestamp_continuity(store):
    ts = [r["t"] for r in store.q("SELECT t FROM time_dim ORDER BY t")]
    assert len(ts) == 3 * 24 * 60 // STEP
    assert {b - a for a, b in zip(ts, ts[1:])} == {STEP * 60}
    zones = store.q("SELECT COUNT(*) AS n FROM zone_dim")[0]["n"]
    assert zones == 20                                     # 2+1+1 floors x 5 zones
    n = store.q("SELECT COUNT(*) AS n FROM zone_obs")[0]["n"]
    assert n == len(ts) * zones
    assert store.q("SELECT COUNT(*) AS n FROM building_obs")[0]["n"] == len(ts) * 3
    assert store.q("SELECT COUNT(*) AS n FROM raw_obs")[0]["n"] == n
    assert store.q("SELECT COUNT(*) AS n FROM clean_obs")[0]["n"] == n
    per_zone = store.q("SELECT COUNT(DISTINCT t) AS n FROM zone_obs GROUP BY building_id, zone_id")
    assert {r["n"] for r in per_zone} == {len(ts)}


def test_time_features_are_derived(store):
    r = store.q("SELECT * FROM time_dim WHERE ts = '2026-06-06T14:35'")[0]
    assert (r["hour"], r["minute"], r["day_of_week"], r["is_weekend"], r["month"]) == (14, 35, 5, 1, 6)
    assert r["season"] == "monsoon" and r["day_part"] == "afternoon"
    assert r["week_of_year"] == datetime(2026, 6, 6).isocalendar()[1]


def test_building_profiles_and_zones(store):
    b = {r["building_id"]: r for r in store.q("SELECT * FROM building_dim")}
    assert b["b-office"]["building_type"] == "office" and b["b-office"]["floors"] == 2
    assert json.loads(b["b-mall"]["profile_json"])["building_type"] == "mall"
    z = store.q("SELECT floor_id, COUNT(*) AS n FROM zone_dim WHERE building_id='b-office' GROUP BY floor_id")
    assert {(r["floor_id"], r["n"]) for r in z} == {("F1", 5), ("F2", 5)}


def test_reproducible_with_seed():
    def digest(seed):
        sink = MemorySink()
        generate(DatasetConfig(start="2026-06-05", days=1, step_min=15, seed=seed,
                               buildings=[BuildingSpec("b", "hotel", 1)]), sink)
        return hashlib.sha256(json.dumps(sink.data["zone_obs"], sort_keys=True).encode()).hexdigest()
    assert digest(11) == digest(11) != digest(12)


# ------------------------------------------------------------ sanity bounds
def test_sanity_bounds(store):
    r = store.q("SELECT MIN(total_power_kw) p, MIN(occupancy_count) o, SUM(occupancy_count > occupancy_capacity) over_cap, "
                "MIN(indoor_humidity_percent) rh0, MAX(indoor_humidity_percent) rh1, MIN(indoor_co2_ppm) co2, "
                "MIN(energy_consumption_kwh) e, MIN(indoor_temperature_c) t0, MAX(indoor_temperature_c) t1 FROM zone_obs")[0]
    assert r["p"] >= 0 and r["o"] >= 0 and r["over_cap"] == 0 and r["e"] >= 0
    assert 5 <= r["rh0"] and r["rh1"] <= 100 and r["co2"] >= 420
    assert 10 < r["t0"] and r["t1"] < 45
    w = store.q("SELECT MIN(outdoor_humidity_percent) a, MAX(outdoor_humidity_percent) b, "
                "MIN(solar_irradiance_w_m2) s FROM weather_obs")[0]
    assert 1 <= w["a"] and w["b"] <= 100 and w["s"] >= 0


# ------------------------------------------------------------ weather
def test_weather_temporal_structure(store):
    rows = store.q("SELECT w.*, d.hour FROM weather_obs w JOIN time_dim d USING (t) ORDER BY t")
    night = [r["solar_irradiance_w_m2"] for r in rows if r["hour"] in (0, 1, 2, 3, 22, 23)]
    noon = [r["solar_irradiance_w_m2"] for r in rows if r["hour"] in (11, 12, 13)]
    assert max(night) == 0 and statistics.mean(noon) > 150
    t15 = statistics.mean(r["outdoor_temperature_c"] for r in rows if r["hour"] == 15)
    t05 = statistics.mean(r["outdoor_temperature_c"] for r in rows if r["hour"] == 5)
    assert t15 > t05 + 2
    steps = [abs(b["outdoor_temperature_c"] - a["outdoor_temperature_c"]) for a, b in zip(rows, rows[1:])]
    assert max(steps) < 2.0                                # gradual, not white noise
    rain = [r["rainfall_mm"] > 0 for r in rows]
    if sum(rain):
        runs = sum(1 for a, b in zip([False] + rain, rain) if b and not a)
        assert sum(rain) / runs >= 12                      # events last >= 1 h, not per-sample noise


def test_seasons_differ():
    def mean_t(season):
        wm = WeatherModel(datetime(2026, 1, 1), 5, 1, "delhi", lambda ts: season)
        return statistics.mean(h["t"] for h in wm.hours)
    assert mean_t("summer") > mean_t("monsoon") > mean_t("transition") > mean_t("winter")
    mon = WeatherModel(datetime(2026, 7, 1), 20, 1, "delhi", lambda ts: "monsoon")
    summ = WeatherModel(datetime(2026, 7, 1), 20, 1, "delhi", lambda ts: "summer")
    assert statistics.mean(h["cloud"] for h in mon.hours) > statistics.mean(h["cloud"] for h in summ.hours)
    assert sum(h["rain"] > 0 for h in mon.hours) > sum(h["rain"] > 0 for h in summ.hours)


# ------------------------------------------------------------ occupancy
def _occ(store, bid, where):
    return store.q(f"SELECT AVG(occupancy_percent) AS p FROM building_obs b JOIN time_dim d USING (t) "
                   f"WHERE building_id = ? AND {where}", [bid])[0]["p"]


def test_occupancy_patterns_by_type(store):
    assert _occ(store, "b-office", "d.date='2026-06-05' AND d.hour BETWEEN 10 AND 16") > 40
    assert _occ(store, "b-office", "d.hour BETWEEN 1 AND 4") < 5
    assert _occ(store, "b-office", "d.is_weekend=1 AND d.hour BETWEEN 10 AND 16") < 15
    assert _occ(store, "b-mall", "d.hour BETWEEN 1 AND 6") < 5
    assert _occ(store, "b-mall", "d.date='2026-06-06' AND d.hour BETWEEN 17 AND 20") > \
        _occ(store, "b-mall", "d.date='2026-06-05' AND d.hour BETWEEN 10 AND 11")
    # data centre: few PEOPLE (absolute headcount), big load around the clock
    heads = {r["building_id"]: r for r in store.q(
        "SELECT building_id, AVG(occupancy_count) n, AVG(total_power_kw) p, MIN(total_power_kw) pmin "
        "FROM building_obs GROUP BY building_id")}
    assert heads["b-dc"]["n"] < 10 < heads["b-office"]["n"]
    assert heads["b-dc"]["pmin"] > 50 and heads["b-dc"]["p"] > heads["b-office"]["p"]


def test_holiday_and_event_calendar_scales_occupancy():
    from backend.dataset.calendar import Calendar
    cal = Calendar([{"date": "2026-08-15", "name": "I-Day", "factor": {"*": 0.2, "mall": 1.3}}],
                   [{"date": "2026-08-15", "name": "Sale", "building_types": ["mall"], "start_hour": 12,
                     "end_hour": 20, "factor": 1.5}])
    ts = datetime(2026, 8, 15, 14)
    assert cal.occupancy_factor(ts, "office") == (0.2, "I-Day")
    f, label = cal.occupancy_factor(ts, "mall")
    assert f == pytest.approx(1.95) and "Sale" in label
    assert cal.occupancy_factor(datetime(2026, 8, 16, 14), "office") == (1.0, None)


# ------------------------------------------------------------ relationships
def test_weather_occupancy_hvac_energy_relationships(store):
    t0 = store.q("SELECT MIN(t) a, MAX(t) b FROM time_dim")[0]
    rows = analysis.joined(store, "b-office", t0["a"], t0["b"] + 1, 3600)
    occ = [r for r in rows if r["occupancy_count"] > 0]
    assert analysis.compare(rows, "occupancy_vs_co2")["r"] > 0.6
    assert analysis.compare(rows, "occupancy_vs_energy")["r"] > 0.6
    assert analysis.compare(occ, "outdoor_temperature_vs_hvac_load")["r"] > 0.3
    assert analysis.compare(rows, "hvac_load_vs_energy")["r"] > 0.7
    assert analysis.compare(rows, "solar_vs_renewable")["r"] > 0.9
    assert analysis.compare(rows, "cloud_cover_vs_solar")["r"] < 0.2


def test_more_heat_more_cooling_same_occupancy():
    """Controlled experiment: identical config, +4 K summer vs baseline -> more cooling kWh."""
    def cool(season):
        sink = MemorySink()
        generate(DatasetConfig(start="2026-06-08", days=1, step_min=15, seed=5, season=season,
                               buildings=[BuildingSpec("b", "office", 1)],
                               anomalies=AnomalyConfig(enabled=False)), sink)
        return sum(r["hvac_power_kw"] for r in sink.data["zone_obs"])
    assert cool("summer") > cool("transition") > cool("winter")


def test_winter_generates_heating(winter):
    rows = winter.data["zone_obs"]
    assert sum(r["heating_demand_kw"] for r in rows) > 0
    assert any(r["hvac_mode"] == "HEATING" for r in rows)
    assert all(r["heating_setpoint_c"] is not None for r in rows)


# ------------------------------------------------------------ energy / demand consistency
def test_energy_is_internally_consistent(store):
    bad = store.q("SELECT COUNT(*) n FROM zone_obs WHERE ABS(total_power_kw - (hvac_power_kw + ventilation_power_kw "
                  "+ lighting_power_kw + plug_load_kw + equipment_power_kw)) > 0.002 "
                  f"OR ABS(energy_consumption_kwh - total_power_kw * {STEP / 60}) > 0.001 "
                  "OR ABS(energy_consumption_kwh - (hvac_energy_kwh + lighting_energy_kwh + equipment_energy_kwh)) > 0.002")
    assert bad[0]["n"] == 0
    mism = store.q("SELECT COUNT(*) n FROM building_obs b JOIN (SELECT building_id, t, SUM(total_power_kw) s "
                   "FROM zone_obs GROUP BY building_id, t) z USING (building_id, t) WHERE ABS(b.total_power_kw - z.s) > 0.01")
    assert mism[0]["n"] == 0
    g = store.q("SELECT COUNT(*) n FROM building_obs WHERE ABS(grid_power_kw - MAX(0, total_power_kw - renewable_power_kw)) > 0.002")
    assert g[0]["n"] == 0


def test_peak_and_daily_energy(store):
    for r in store.q("SELECT b.building_id, d.date, MAX(b.peak_demand_kw) pk, AVG(b.grid_power_kw) mean_grid, "
                     "MAX(b.grid_power_kw) mx, MAX(b.daily_energy_kwh) de, SUM(b.energy_consumption_kwh) e "
                     "FROM building_obs b JOIN time_dim d USING (t) GROUP BY b.building_id, d.date"):
        assert r["pk"] >= r["mean_grid"] and r["pk"] == pytest.approx(r["mx"], abs=1e-3)
        assert r["de"] == pytest.approx(r["e"], rel=1e-3, abs=0.01)


def test_demand_category_and_forecast(store):
    rows = store.q("SELECT * FROM building_obs WHERE building_id='b-office' ORDER BY t")
    for r in rows:
        risk = r["peak_demand_risk"]
        exp = "LOW" if risk < 0.35 else "MEDIUM" if risk < 0.6 else "HIGH" if risk < 0.85 else "CRITICAL"
        assert r["demand_category"] == exp
    per_day = 24 * 60 // STEP
    # Friday -> Saturday is a different day-type, Saturday -> Sunday is the same: seasonal naive
    sat, sun = rows[per_day:2 * per_day], rows[2 * per_day:]
    assert all(b["forecast_demand_kw"] == pytest.approx(a["grid_power_kw"], abs=1e-3) for a, b in zip(sat, sun))


# ------------------------------------------------------------ comfort
def test_pmv_reference_and_score_formula():
    pmv, ppd = comfort.pmv_ppd(22.0, 22.0, 0.1, 60.0, 1.2, 0.5)
    assert pmv == pytest.approx(-0.75, abs=0.1) and ppd == pytest.approx(17, abs=2.5)
    assert comfort.thermal_status(0.0) == "neutral" and comfort.thermal_status(2.0) == "warm"
    from backend.building import default_config
    cfg = default_config("office")
    s = comfort.scores(24.0, 50.0, 500.0, cfg)
    assert s["comfort_score"] == 100.0
    s = comfort.scores(cfg.comfort_max_c + 1.5, cfg.humidity_max_pct + 10, cfg.co2_max_ppm, cfg)
    assert s["temperature_comfort_score"] == 50.0 and s["humidity_comfort_score"] == 50.0 and s["co2_comfort_score"] == 50.0
    assert s["comfort_score"] == pytest.approx(50.0)


def test_comfort_columns_populated(store):
    r = store.q("SELECT MIN(comfort_score) a, MAX(comfort_score) b, COUNT(pmv) np, COUNT(*) n FROM zone_obs")[0]
    assert 0 <= r["a"] <= r["b"] <= 100 and r["np"] == r["n"]


# ------------------------------------------------------------ aggregation
def test_zone_floor_building_aggregation_no_double_counting(store):
    t = store.q("SELECT MIN(t) a, MAX(t) b FROM time_dim")[0]
    t0, t1 = t["a"], t["b"] + 1
    bld = store.series("b-office", t0, t1, 3600, "building", metrics=["energy_consumption_kwh", "occupancy_count"])
    f1 = store.series("b-office", t0, t1, 3600, "floor", floor_id="F1", metrics=["energy_consumption_kwh", "occupancy_count"])
    f2 = store.series("b-office", t0, t1, 3600, "floor", floor_id="F2", metrics=["energy_consumption_kwh", "occupancy_count"])
    for a, b, c in zip(bld, f1, f2):
        assert a["energy_consumption_kwh"] == pytest.approx(b["energy_consumption_kwh"] + c["energy_consumption_kwh"], abs=1e-6)
        assert a["occupancy_count"] == pytest.approx(b["occupancy_count"] + c["occupancy_count"], abs=1e-6)
    total = store.q("SELECT SUM(energy_consumption_kwh) e FROM zone_obs WHERE building_id='b-office'")[0]["e"]
    assert sum(r["energy_consumption_kwh"] for r in bld) == pytest.approx(total, rel=1e-6)
    z = store.series("b-office", t0, t1, 3600, "zone", zone_id="F1-zone_a", metrics=["indoor_temperature_c"])
    assert len(z) == len(bld) == 72


# ------------------------------------------------------------ quality
def test_cleaning_interpolates_short_gaps_and_keeps_raw():
    qc = QualityConfig(max_interpolate_gap=2)
    raw = {"temp": [24.0, None, 24.2, 24.3, None, None, None, 24.7, 50.0 + 60, 24.8],
           "rh": [50.0] * 10, "co2": [600.0] * 10, "occ": [3] * 10, "power": [1.0] * 10,
           "delay_s": [0] * 10, "flags": [""] * 10}
    before = json.dumps(raw)
    iss = quality.validate(raw, 10, 5.0)
    out = quality.clean(raw, iss, qc)
    assert json.dumps(raw) == before                      # raw never overwritten
    assert out["temp"][1] == pytest.approx(24.1) and out["quality"]["temp"][1] == "interpolated"
    assert out["temp"][4] is None and out["quality"]["temp"][5] == "missing"   # 3-gap > 2
    assert out["quality"]["temp"][8] == "outlier_removed"


def test_raw_layer_has_injected_defects_and_clean_is_close_to_truth(store):
    r = store.q("SELECT COUNT(*) n, SUM(temperature_raw_c IS NULL) miss, SUM(raw_flags LIKE '%outlier%') outl "
                "FROM raw_obs")[0]
    assert 0 < r["miss"] < 0.05 * r["n"]
    err = store.q("SELECT AVG(ABS(c.temperature_clean_c - z.indoor_temperature_c)) e FROM clean_obs c JOIN zone_obs z "
                  "USING (building_id, zone_id, t) WHERE c.temperature_quality IN ('ok','interpolated')")[0]["e"]
    assert err < 0.6


def test_noise_can_be_disabled():
    rng = __import__("random").Random(1)
    truth = {"temp": [24.0] * 50, "rh": [50.0] * 50, "co2": [600.0] * 50, "occ": [2] * 50, "power": [1.0] * 50}
    qc = QualityConfig(noise_enabled=False, drift_enabled=False, missing_data_rate=0, outlier_rate=0, delay_rate=0)
    raw = quality.make_raw(truth, qc, 1.0, 5, rng, [])
    assert raw["temp"] == truth["temp"] and raw["co2"] == truth["co2"]


# ------------------------------------------------------------ anomalies
def test_anomaly_metadata_and_effects(store):
    an = store.anomalies()
    assert an, "rate 0.7/building/day over 3 days should schedule anomalies"
    for a in an:
        assert {"anomaly_id", "anomaly_type", "severity", "start_time", "end_time", "affected_zone",
                "description"} <= set(a) and a["start_time"] < a["end_time"]
    for a in [x for x in an if x["anomaly_type"] == "hvac_failure"]:
        rows = store.q("SELECT cooling_demand_kw, heating_demand_kw FROM zone_obs WHERE building_id=? AND zone_id=? "
                       "AND ts >= ? AND ts < ?", [a["building_id"], a["affected_zone"], a["start_time"], a["end_time"]])
        assert rows and all(r["cooling_demand_kw"] == 0 and r["heating_demand_kw"] == 0 for r in rows[1:])
    tagged = store.q("SELECT COUNT(*) n FROM zone_obs WHERE anomaly_ids IS NOT NULL")[0]["n"]
    total = store.q("SELECT COUNT(*) n FROM zone_obs")[0]["n"]
    assert 0 < tagged < 0.5 * total                        # rare, not the whole dataset


def test_anomalies_disabled_or_zero_rate():
    assert anomaly_schedule(DatasetConfig(anomalies=AnomalyConfig(enabled=False)), {"b": ["F1-zone_a"]}) == []
    assert anomaly_schedule(DatasetConfig(anomalies=AnomalyConfig(rate_per_building_day=0)), {"b": ["F1-zone_a"]}) == []


def test_config_validation():
    assert DatasetConfig().validate() == []
    bad = DatasetConfig(days=0, step_min=7, season="spring", buildings=[BuildingSpec("x", "castle")])
    errs = bad.validate()
    assert len(errs) >= 4


# ------------------------------------------------------------ API
@pytest.fixture
def hclient(client, store, monkeypatch):
    import backend.app as appmod
    monkeypatch.setattr(appmod, "HISTORY", store)
    return client


def test_history_api_filters(hclient):
    cat = hclient.get("/api/history/catalog").json()
    assert cat["available"] and len(cat["buildings"]) == 3 and "simulated" in cat["data_state"]
    b = hclient.get("/api/history?building_id=b-office&range=24h").json()
    assert b["interval_s"] == 900 and 90 <= len(b["points"]) <= 96
    p = b["points"][-1]
    assert {"indoor_temperature_c", "outdoor_temperature_c", "total_power_kw", "renewable_power_kw",
            "forecast_demand_kw", "comfort_score", "occupancy_count"} <= set(p)
    f = hclient.get("/api/history?building_id=b-office&level=floor&floor_id=F2&range=6h").json()
    z = hclient.get("/api/history?building_id=b-office&level=zone&zone_id=F2-zone_b&range=6h&interval=5min").json()
    assert f["interval_s"] == 300 and len(z["points"]) == 72
    assert "renewable_power_kw" not in z["points"][0]
    w = hclient.get("/api/history?building_id=b-mall&start_time=2026-06-06T10:00&end_time=2026-06-06T12:00&interval=15min").json()
    assert w["points"][0]["ts"] == "2026-06-06T10:00" and len(w["points"]) == 8
    m = hclient.get("/api/history?building_id=b-dc&range=30d").json()
    assert len(m["points"]) <= 1000
    for bad in ("building_id=nope", "level=zone", "level=floor&floor_id=F9", "range=2y", "interval=7min",
                "metrics=bogus", "start_time=yesterday"):
        assert hclient.get("/api/history?" + bad).status_code == 400, bad


def test_history_compare_anomalies_quality_export(hclient):
    c = hclient.get("/api/history/compare?pair=occupancy_vs_co2&building_id=b-office&range=7d").json()
    assert c["r"] > 0.5 and c["n"] == len(c["points"])
    d = hclient.get("/api/history/compare?pair=current_vs_historical_demand&building_id=b-office&range=7d").json()
    assert len(d["hours"]) == 24 and d["current_date"] == "2026-06-07"
    assert hclient.get("/api/history/compare?pair=nope").status_code == 400
    an = hclient.get("/api/history/anomalies?building_id=b-mall").json()["anomalies"]
    assert all(a["building_id"] in ("b-mall", "*") for a in an)
    q = hclient.get("/api/history/quality?building_id=b-office&zone_id=F1-zone_a&range=24h").json()
    assert q["points"] and any(k.startswith("temperature_quality:") for k in q["flag_counts"])
    csv_r = hclient.get("/api/history/export?format=csv&layer=zone&building_id=b-office&zone_id=F1-zone_a&range=1h")
    assert csv_r.status_code == 200 and csv_r.text.splitlines()[0].startswith("t,ts,building_id")
    assert len(csv_r.text.strip().splitlines()) == 13
    j = hclient.get("/api/history/export?format=json&layer=building&building_id=b-dc&range=1h").json()
    assert j["count"] == 12 and "db_path" not in json.dumps(j)
    assert hclient.get("/api/history/export?format=xml").status_code == 400


def test_history_unavailable_is_honest(client, tmp_path, monkeypatch):
    import backend.app as appmod
    monkeypatch.setattr(appmod, "HISTORY", SqliteStore(tmp_path / "missing.sqlite"))
    assert client.get("/api/history/catalog").json()["available"] is False
    assert client.get("/api/history").status_code == 503


def test_dashboard_serves_history_tab(client):
    html = client.get("/").text
    assert 'id="tab-history"' in html and "/static/history.js" in html and 'id="tab-building"' in html
    js = client.get("/static/history.js")
    assert js.status_code == 200 and "registerPanel('history'" in js.text and "/api/history" in js.text
