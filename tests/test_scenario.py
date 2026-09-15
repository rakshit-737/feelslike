"""Phase 5 — seasonal scenarios on the live twin: weather coupling, physical consistency across
controlled scenarios, occupancy / equipment / envelope coupling, capacity limits, causal
explanation, season comparison, APIs. Trends are compared across scenarios, never step by step."""
from __future__ import annotations

import statistics
from dataclasses import replace
from datetime import datetime

import pytest

from backend import scenario, telemetry
from backend.building import default_config
from backend.constraints import ConstraintStore
from backend.dataset.weather import WeatherModel
from sim.controllers import ConstraintAware
from sim.twin import COP, DT, FAN_W, ZONES, DigitalTwin

OFFICE = default_config("office")


def run(cfg: scenario.ScenarioConfig, hours=6.0, start_h=9.0, knobs=None, profile=OFFICE, day=0):
    tw = DigitalTwin(seed=7)
    tw.t = day * 86400 + start_h * 3600
    ws = scenario.make_weather(cfg, 7)
    scenario.apply_to_twin(tw, cfg, ws, profile, cfg.climate)
    if knobs:
        tw.set_conditions(**knobs)
    st, ctrl, tel = ConstraintStore(), ConstraintAware(), telemetry.TelemetryStore()
    kwh0 = tw.kwh
    cool, power, indoor, rh, atcap, outdoor = [], [], [], [], 0, []
    for _ in range(int(hours * 60)):
        sps, v = ctrl.act(tw, st)
        kb = dict(tw.kwh_by_zone)
        tw.step(sps, v)
        pw = {z.id: (tw.kwh_by_zone[z.id] - kb[z.id]) * 3.6e6 / DT for z in ZONES}
        cw = {z.id: max(0.0, (pw[z.id] - FAN_W[int(v.get(z.id, 0) or 0)]) * COP) for z in ZONES}
        row = tel.record(tw, None, pw, cw)
        cool.append(sum(cw.values()))
        power.append(row["power_w"])
        outdoor.append(row["t_out"])
        occ = [z for z in row["zones"].values() if z["occ"] > 0]
        if occ:
            indoor.append(statistics.mean(z["temp"] for z in occ))
            rh.append(statistics.mean(z["rh"] for z in occ))
        atcap += sum(1 for z in row["zones"].values() if z["at_capacity"])
    return {"cool": statistics.mean(cool), "power": statistics.mean(power), "kwh": tw.kwh - kwh0,
            "indoor": statistics.mean(indoor) if indoor else None,        # None: nobody in (e.g. 01:00)
            "rh": statistics.mean(rh) if rh else None, "atcap": atcap,
            "outdoor": statistics.mean(outdoor), "tel": tel, "twin": tw}


SEASONAL = scenario.ScenarioConfig(mode="seasonal")


# 1-5 seasons
def test_season_selection_drives_weather():
    temps = {s: statistics.mean(scenario.WeatherScenario(s, "delhi", 7).at(t)["outdoor_temperature_c"]
                                for t in range(0, 7 * 86400, 1800)) for s in ("summer", "monsoon", "winter", "transition")}
    assert temps["summer"] > temps["monsoon"] > temps["transition"] > temps["winter"]
    mon, summ = scenario.WeatherScenario("monsoon", "delhi", 7), scenario.WeatherScenario("summer", "delhi", 7)
    samples = range(0, 14 * 86400, 1800)
    assert statistics.mean(mon.at(t)["outdoor_humidity_percent"] for t in samples) > \
        statistics.mean(summ.at(t)["outdoor_humidity_percent"] for t in samples)
    assert sum(mon.at(t)["rainfall_mm"] > 0 for t in samples) > sum(summ.at(t)["rainfall_mm"] > 0 for t in samples)
    assert scenario.WeatherScenario("auto", "delhi", 7).at(0)["season"] == "monsoon"      # anchored mid-September


def test_summer_winter_monsoon_transition_building_response():
    r = {s: run(replace(SEASONAL, season=s)) for s in ("summer", "monsoon", "transition", "winter")}
    assert r["summer"]["cool"] > r["transition"]["cool"] > r["winter"]["cool"]
    assert r["summer"]["kwh"] > r["transition"]["kwh"] > r["winter"]["kwh"]
    assert r["monsoon"]["rh"] > r["summer"]["rh"]                     # humidity challenge
    assert r["winter"]["indoor"] < r["summer"]["indoor"]              # no heating: cooler rooms, not heating energy


def test_classic_mode_is_the_original_twin_bit_for_bit():
    a, b = DigitalTwin(seed=7), DigitalTwin(seed=7)
    scenario.apply_to_twin(b, scenario.ScenarioConfig(), None, OFFICE, "delhi")
    for tw in (a, b):
        tw.t = 9 * 3600
    ca, cb = ConstraintAware(), ConstraintAware()
    for _ in range(240):
        a.step(*ca.act(a, None))
        b.step(*cb.act(b, None))
    assert a.kwh == b.kwh and a.T == b.T and a.W == b.W


# 6-7 weather -> thermal / HVAC ; envelope ; solar
def test_hotter_outdoor_more_cooling_and_power():
    base = run(replace(SEASONAL, season="transition"))
    hot = run(replace(SEASONAL, season="transition"), knobs={"outdoor_offset": 6})
    assert hot["outdoor"] > base["outdoor"] + 5
    assert hot["cool"] > base["cool"] and hot["power"] > base["power"] and hot["kwh"] > base["kwh"]


def test_envelope_and_solar_coupling():
    tight = run(replace(SEASONAL, season="summer", envelope_scale=0.5))
    leaky = run(replace(SEASONAL, season="summer", envelope_scale=2.0))
    assert leaky["cool"] > tight["cool"]
    clear = run(replace(SEASONAL, season="summer", cloud_cover=0.0, rain_mm_h=0.0))
    cloudy = run(replace(SEASONAL, season="summer", cloud_cover=100.0, rain_mm_h=0.0))
    assert clear["cool"] > cloudy["cool"]


def test_cloud_and_rain_reduce_solar_and_change_condition():
    wm = WeatherModel(datetime(2026, 7, 1), 20, 3, "delhi", lambda ts: "monsoon")
    rows = [wm.at(datetime(2026, 7, 1 + d, h)) for d in range(20) for h in (11, 12, 13)]
    med = statistics.median(r["cloud_cover_percent"] for r in rows)
    lo = [r["solar_irradiance_w_m2"] for r in rows if r["cloud_cover_percent"] < med]
    hi = [r["solar_irradiance_w_m2"] for r in rows if r["cloud_cover_percent"] > med]
    assert lo and hi and statistics.mean(lo) > statistics.mean(hi)
    ws = scenario.WeatherScenario("summer", "delhi", 1, cloud_cover=0.0, rain_mm_h=0.0)
    t_noon = 12 * 3600
    dry = ws.at(t_noon)
    wet = scenario.WeatherScenario("summer", "delhi", 1, cloud_cover=0.0, rain_mm_h=8.0).at(t_noon)
    assert dry["weather_condition"] == "clear" and wet["weather_condition"] == "heavy_rain"
    assert wet["solar_irradiance_w_m2"] < dry["solar_irradiance_w_m2"]
    dist = scenario.WeatherScenario("summer", "delhi", 1, disturbance={"delta_c": 5.0, "t0": 0, "t1": 7200})
    assert dist.at(3600)["outdoor_temperature_c"] == pytest.approx(scenario.WeatherScenario("summer", "delhi", 1).at(3600)["outdoor_temperature_c"] + 5.0)


# 8-11 occupancy / CO2 / ventilation / equipment
def test_occupancy_raises_internal_gain_cooling_and_co2():
    lo = run(replace(SEASONAL, season="transition"), knobs={"occ_scale": 0.5})
    hi = run(replace(SEASONAL, season="transition"), knobs={"occ_scale": 2.0})
    assert hi["cool"] > lo["cool"]
    co2_lo = statistics.mean(r["zones"]["zone_a"]["co2"] for r in lo["tel"].rows)
    co2_hi = statistics.mean(r["zones"]["zone_a"]["co2"] for r in hi["tel"].rows)
    assert co2_hi > co2_lo


def test_ventilation_reduces_co2_at_equal_occupancy():
    tw = DigitalTwin(seed=7)
    tw.t = 11 * 3600
    res = {}
    for vent in (0, 2):
        t2, tel = tw.clone(), telemetry.TelemetryStore()
        for _ in range(120):
            t2.step({z.id: 24.0 for z in ZONES}, {z.id: vent for z in ZONES})
            tel.record(t2)
        res[vent] = tel.latest()["zones"]["zone_a"]["co2"]
    assert res[0] > res[2]


def test_equipment_load_drives_cooling_even_without_people():
    dc = default_config("data_center")
    people = run(replace(SEASONAL, season="transition"), start_h=1.0, hours=3, profile=dc)
    it = run(replace(SEASONAL, season="transition", internal_load="profile"), start_h=1.0, hours=3, profile=dc)
    assert it["cool"] > 3 * people["cool"]          # IT heat at 01:00 with nobody in


# 12 capacity limit
def test_capacity_limit_is_visible_and_the_room_warms():
    ok = run(replace(SEASONAL, season="summer"))
    starved = run(replace(SEASONAL, season="summer"), knobs={"capacity_scale": 0.15, "outdoor_offset": 6})
    assert starved["atcap"] > 0 and starved["indoor"] > ok["indoor"] + 0.5
    assert max(z["capacity_pct"] for r in starved["tel"].rows for z in r["zones"].values()) == 100.0


def test_causal_explanation_uses_real_numbers():
    r = run(replace(SEASONAL, season="summer"), hours=1)
    c = scenario.causal(r["tel"])
    last = r["tel"].latest()
    assert c["available"] and f"{last['t_out']:.1f} °C" in c["sentence"]
    assert {x["key"] for x in c["chain"]} >= {"outdoor_temperature", "cooling_w", "hvac_power_w", "indoor_temperature"}
    assert c["chain"][0]["delta"] is not None
    assert scenario.causal(telemetry.TelemetryStore())["sentence"] == "No current data available"


def test_compare_seasons_is_isolated_and_honest_about_heating():
    tw = DigitalTwin(seed=7)
    tw.t = 10 * 3600
    st = ConstraintStore()
    before = (tw.t, tw.kwh, dict(tw.T))
    out = scenario.compare_seasons(tw, st, ConstraintAware, SEASONAL, OFFICE, horizon_h=1.0)
    assert (tw.t, tw.kwh, dict(tw.T)) == before
    s = out["seasons"]
    assert set(s) == {"summer", "monsoon", "winter", "transition"} and out["label"] == "SIMULATED / WHAT-IF"
    assert all(v["heating_demand"].startswith("NOT MODELLED") for v in s.values())
    assert s["summer"]["outdoor_temperature_c"] > s["winter"]["outdoor_temperature_c"]
    assert s["summer"]["energy_kwh"] > s["winter"]["energy_kwh"]
    with pytest.raises(ValueError):
        scenario.compare_seasons(tw, st, ConstraintAware, SEASONAL, OFFICE, horizon_h=30)


def test_config_validation():
    assert scenario.ScenarioConfig().validate() == []
    bad = scenario.ScenarioConfig(mode="arcade", season="spring", climate="mars", cloud_cover=150,
                                  envelope_scale=9, internal_load="nuclear")
    assert len(bad.validate()) == 6


# ---------------------------------------------------------------- API (25 reset, 26 speed, 27-29)
@pytest.fixture
def sc_client(fresh_client, live):
    fresh_client.post("/api/scenario/reset")
    live.advance(20)
    yield fresh_client
    fresh_client.post("/api/scenario/reset")
    fresh_client.post("/api/speed", json={"speed": 1})


def test_scenario_api_round_trip(sc_client, live):
    g = sc_client.get("/api/scenario").json()
    assert g["mode"] == "classic" and g["heating"].startswith("NOT MODELLED") and len(g["speed_presets"]) == 6
    w = sc_client.get("/api/latest").json()["weather"]
    assert w["outdoor_temperature"]["source"] == "sim" and w["solar_irradiance"]["value"] is None
    assert "classic" in w["solar_irradiance"]["note"]
    r = sc_client.post("/api/scenario", json={"mode": "seasonal", "season": "summer", "climate": "delhi",
                                              "outdoor_offset": 2, "internal_load": "profile"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["mode"] == "seasonal" and j["knobs"]["outdoor_offset"] == 2 and "12" in j["humidity_model"]
    live.advance(10)
    lat = sc_client.get("/api/latest").json()
    assert lat["scenario"]["season"] == "summer" and lat["weather"]["season"]["value"] == "summer"
    assert lat["weather"]["solar_irradiance"]["value"] is not None
    assert lat["zones"][0]["internal_gain"]["value"] > 0 and lat["zones"][0]["internal_gain"]["source"] == "derived"
    assert lat["building"]["metrics"]["heating_demand"]["value"] is None
    assert "NOT MODELLED" in lat["building"]["metrics"]["heating_demand"]["note"]
    assert lat["causal"]["available"] and lat["telemetry_mode"].startswith("DIGITAL TWIN")
    st = sc_client.get("/api/state").json()
    assert abs(st["sim"]["t_out"] - lat["weather"]["outdoor_temperature"]["value"]) < 0.2
    assert sc_client.post("/api/scenario", json={"cloud_cover": 90, "rain_mm_h": 4}).json()["weather_now"]["weather_condition"] == "rain"
    assert sc_client.post("/api/scenario", json={"disturbance_delta_c": 4, "disturbance_hours": 2}).json()["disturbance"]["delta_c"] == 4


def test_scenario_api_validation(sc_client):
    for body in ({"cloud_cover": 150}, {"envelope_scale": 5}, {"season": "spring"}, {"outdoor_offset": 40},
                 {"humidity_offset": -99}, {"disturbance_delta_c": 30}, {}):
        assert sc_client.post("/api/scenario", json=body).status_code == 400, body
    assert sc_client.post("/api/scenario", json={"teleport": 1}).status_code == 422


def test_scenario_reset_keeps_building_and_history(sc_client, live):
    sc_client.post("/api/scenario", json={"mode": "seasonal", "season": "winter", "occ_scale": 2, "capacity_scale": 0.5})
    t_before = live.us.t
    j = sc_client.post("/api/scenario/reset").json()
    assert j["mode"] == "classic" and j["knobs"] == {"outdoor_offset": 0.0, "humidity_offset": 0.0, "solar_scale": 1.0,
                                                     "occ_scale": 1.0, "capacity_scale": 1.0}
    assert live.us.t == t_before and len(live.telemetry) > 0              # no rebuild, buffer kept
    assert live.us.rh_fn is None and live.base.rh_fn is None


def test_speed_presets_and_compare_api(sc_client):
    for p in sc_client.get("/api/scenario").json()["speed_presets"]:
        assert sc_client.post("/api/speed", json={"speed": p["speed"]}).json()["speed"] == p["speed"]
    c = sc_client.get("/api/scenario/compare?horizon_h=0.5").json()
    assert set(c["seasons"]) == {"summer", "monsoon", "winter", "transition"} and c["kind"] == "predicted"
    assert sc_client.get("/api/scenario/compare?horizon_h=48").status_code == 400


def test_dashboard_serves_scenario_tab(client):
    html = client.get("/").text
    assert 'id="tab-scenario"' in html and "/static/scenario.js" in html
    js = client.get("/static/scenario.js").text
    assert "registerPanel('scenario'" in js and "/api/scenario" in js and "/api/simhw" in js
    assert "SIMULATED HARDWARE" in js and "FL.on('latest'" in js
