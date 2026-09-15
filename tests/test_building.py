"""Building tab backend: profiles, validation, demand model, KPIs, zone drill-down.

What is under test, and why it matters:
  * six commercial profiles exist, each valid, each an independent copy;
  * validation rejects bad values with a full list and applies nothing;
  * switching type resets to that profile's defaults; editing keeps the rest;
  * an operating mode pulls ONLY the existing controller levers;
  * the demand schedule matches the brief's worked examples (office, mall);
  * the profile comfort score agrees with telemetry's for the office profile;
  * KPIs are computed from the same live state /api/state reports;
  * the zone explanation quotes real backend values, not canned text;
  * nothing in backend/building.py mutates its inputs.
"""
from __future__ import annotations

import copy
import os
import shutil
import subprocess

import pytest

os.environ.setdefault("FL_EXTERNAL", "0")     # no network in the suite

from backend import building, telemetry                              # noqa: E402
from sim.twin import ZONE_IDS                                        # noqa: E402

TYPES = ("office", "mall", "hospital", "hotel", "college", "data_center")


# ----------------------------------------------------------------- profiles
def test_six_profiles_exist_and_all_validate():
    assert set(building.BUILDING_TYPES) == set(TYPES) == set(building.PROFILES)
    for t in TYPES:
        cfg = building.default_config(t)
        assert building.validate(cfg) == [], t
        assert cfg.building_type == t
        assert len(cfg.schedule) == 24 and all(0.0 <= v <= 1.0 for v in cfg.schedule)
        assert set(cfg.zone_roles) == set(ZONE_IDS) == set(cfg.zone_floor)
        for k in ("name", "floors", "zones", "occupancy_capacity", "open_hour", "close_hour",
                  "hvac_capacity_w", "comfort_min_c", "comfort_max_c", "humidity_min_pct",
                  "humidity_max_pct", "co2_max_ppm", "energy_priority", "comfort_priority",
                  "occupancy_sensitivity", "oa_per_person_ls", "oa_per_area_ls_m2"):
            assert getattr(cfg, k) is not None


def test_default_config_is_an_independent_copy():
    a = building.default_config("mall")
    a.schedule[0] = 0.99
    a.zone_roles["zone_a"] = "changed"
    a.open_days.append(9)
    b = building.default_config("mall")
    assert b.schedule[0] != 0.99 and b.zone_roles["zone_a"] != "changed" and 9 not in b.open_days


@pytest.mark.parametrize("changes,needle", [
    ({"comfort_min_c": 26.0, "comfort_max_c": 25.0}, "comfort range"),
    ({"zones": 3}, "zones must be between"),
    ({"floors": 0}, "floors must be between"),
    ({"open_hour": 20.0, "close_hour": 8.0}, "close_hour"),
    ({"co2_max_ppm": 100.0}, "co2_max_ppm"),
    ({"humidity_min_pct": 60.0, "humidity_max_pct": 62.0}, "humidity range"),
    ({"energy_priority": 150.0}, "energy_priority"),
    ({"open_days": [0, 0]}, "open_days"),
    ({"open_days": []}, "open_days"),
    ({"operating_mode": "turbo"}, "operating_mode"),
    ({"name": "   "}, "name"),
    ({"floors": 30, "zones": 10}, "at least the number of floors"),
])
def test_validation_rejects_and_applies_nothing(changes, needle):
    cfg = building.default_config("office")
    before = copy.deepcopy(cfg)
    new, errs = building.apply_update(cfg, changes)
    assert errs and any(needle in e for e in errs), errs
    assert new is cfg and cfg == before


def test_unknown_field_and_unknown_type_are_errors():
    cfg = building.default_config("office")
    assert building.apply_update(cfg, {"bogus": 1})[1]
    assert building.apply_update(cfg, {"building_type": "castle"})[1]


def test_type_switch_resets_to_profile_defaults_but_keeps_mode():
    cfg = building.default_config("office")
    cfg, errs = building.apply_update(cfg, {"name": "Tower 9", "operating_mode": "energy_saving"})
    assert not errs and cfg.name == "Tower 9"
    new, errs = building.apply_update(cfg, {"building_type": "hospital"})
    assert not errs
    hosp = building.PROFILES["hospital"]
    assert new.name == hosp.name and new.co2_max_ppm == hosp.co2_max_ppm == 800.0
    assert new.operating_mode == "energy_saving"
    # switch + edit in one request: other fields apply on top of the new defaults
    new2, errs = building.apply_update(cfg, {"building_type": "mall", "floors": 5})
    assert not errs and new2.floors == 5 and new2.open_hour == building.PROFILES["mall"].open_hour


def test_same_type_edit_keeps_every_other_field():
    cfg, _ = building.apply_update(building.default_config("hotel"), {"name": "Grand"})
    new, errs = building.apply_update(cfg, {"floors": 12})
    assert not errs and new.name == "Grand" and new.floors == 12
    assert new.building_type == "hotel"


@pytest.mark.parametrize("mode,cp,ep,expect", [
    ("normal", 60, 40, ("balanced", "automatic")),
    ("normal", 90, 10, ("comfort", "automatic")),
    ("normal", 10, 90, ("energy", "automatic")),
    ("energy_saving", 50, 50, ("energy", "automatic")),
    ("comfort_priority", 50, 50, ("comfort", "automatic")),
    ("peak_demand_reduction", 50, 50, ("cost", "automatic")),
    ("emergency", 50, 50, ("balanced", "emergency_override")),
    ("simulation", 50, 50, ("balanced", "recommend_only")),
])
def test_operating_mode_maps_to_existing_levers(mode, cp, ep, expect):
    from backend.contracts import OBJECTIVES, SAFETY_MODES
    cfg, errs = building.apply_update(building.default_config("office"),
                                      {"operating_mode": mode, "comfort_priority": cp,
                                       "energy_priority": ep})
    assert not errs
    obj, safety = building.mode_levers(cfg)
    assert (obj, safety) == expect
    assert obj in OBJECTIVES and safety in SAFETY_MODES


# ----------------------------------------------------------------- demand model
def test_office_schedule_matches_the_worked_example():
    cfg = building.default_config("office")
    lv = lambda h: building.demand_level(building.expected_fraction(cfg, 0, h))  # Monday
    assert lv(8.0) == "low"
    assert lv(9.0) == "medium"
    assert lv(11.0) in ("high", "very_high") and lv(13.0) in ("high", "very_high")
    assert lv(18.0) == "medium"
    assert lv(21.0) == "low"
    # closed on Sunday: scaled down by closed_factor
    assert building.expected_fraction(cfg, 6, 11.0) < building.expected_fraction(cfg, 0, 11.0)


def test_mall_schedule_matches_the_worked_example():
    cfg = building.default_config("mall")
    lv = lambda h: building.demand_level(building.expected_fraction(cfg, 5, h))  # Saturday
    assert lv(10.0) == "medium"
    assert lv(13.0) == "high"
    assert lv(18.0) == "very_high"
    assert lv(22.5) == "low"


def test_expected_occupancy_pct_scales_the_schedule():
    cfg = building.default_config("office")
    half, _ = building.apply_update(cfg, {"expected_occupancy_pct": 50.0})
    assert building.expected_fraction(half, 0, 11.0) == pytest.approx(
        0.5 * building.expected_fraction(cfg, 0, 11.0))


def test_data_center_hvac_expectation_is_flat_and_office_follows_people():
    dc, of = building.default_config("data_center"), building.default_config("office")
    dc_span = building.expected_hvac_fraction(dc, 1.0) - building.expected_hvac_fraction(dc, 0.0)
    of_span = building.expected_hvac_fraction(of, 1.0) - building.expected_hvac_fraction(of, 0.0)
    assert dc_span < 0.05 < of_span


def test_office_comfort_score_agrees_with_telemetry():
    cfg = building.default_config("office")
    for temp in (19.0, 22.5, 23.0, 24.8, 26.5, 27.9, 31.0):
        for rh in (40.0, 65.0, 72.0, 95.0):
            assert building.comfort_score(cfg, temp, rh) == telemetry.comfort_score(temp, rh)


def test_ventilation_requirement_is_the_62_1_formula():
    cfg = building.default_config("office")
    from sim.twin import ZONE_BY_ID
    assert building.required_oa_ls(cfg, "zone_a", 10) == pytest.approx(
        2.5 * 10 + 0.3 * ZONE_BY_ID["zone_a"].area)


def _row(**kw):
    base = {"id": "zone_b", "name": "Conference Room B", "temp": 24.0, "rh": 50.0,
            "occ": 6, "occ_pct": 50.0, "vent": 1, "cool_w": 1200.0, "power_w": 500.0,
            "capacity_pct": 20.0, "at_capacity": False, "setpoint": 24.0}
    base.update(kw)
    return base


@pytest.mark.parametrize("row,co2,alerts,status,flag", [
    (_row(), 600, [], "comfortable", "comfortable"),
    (_row(temp=27.0), 600, [], "warning", "warm"),
    (_row(temp=21.5), 600, [], "warning", "cold"),
    (_row(temp=30.0), 600, [], "critical", "warm"),
    (_row(), 1200, [], "warning", "high_co2"),
    (_row(), 2000, [], "critical", "high_co2"),
    (_row(occ_pct=100.0), 600, [], "comfortable", "high_occupancy"),
    (_row(occ=0, occ_pct=0.0, temp=28.0, vent=0, cool_w=0.0), 420, [], "unoccupied", "warm"),
    (_row(), 600, [{"severity": "high"}], "critical", "hvac_active"),
])
def test_zone_status_flags(row, co2, alerts, status, flag):
    cfg = building.default_config("office")
    st = building.zone_state(cfg, row, {"co2": co2}, alerts)
    assert st["status"] == status and flag in st["flags"]


def test_profile_comfort_range_changes_zone_evaluation():
    row = _row(temp=25.5)
    assert building.zone_state(building.default_config("office"), row, {"co2": 500}, [])["status"] == "comfortable"
    assert building.zone_state(building.default_config("hospital"), row, {"co2": 500}, [])["status"] == "warning"


def test_demand_series_never_zero_fills_unsampled_hours():
    cfg = building.default_config("office")
    rows = []
    for i in range(30):                          # 30 steps inside 10:00-10:30 only
        t = 10 * 3600.0 + 60.0 * i
        rows.append({"t": t, "power_w": 100.0, "kwh_us": 0.0,
                     "zones": {z: {"power_w": 20.0, "occ": 2, "cool_w": 50.0} for z in ZONE_IDS}})
    now = 12 * 3600.0 + 5
    out = building.demand_series(cfg, rows, now, "all", hours=6, ahead_h=3)
    pts = out["points"]
    assert len(pts) == 6 + 3
    sampled = [p for p in pts if p["samples"]]
    assert len(sampled) == 1 and sampled[0]["hour"] == 10
    assert sampled[0]["actual_power_w"] == pytest.approx(100.0)
    assert all(p["actual_power_w"] is None for p in pts if not p["samples"])
    assert [p["future"] for p in pts].count(True) == 3
    z = building.demand_series(cfg, rows, now, "zone_c", hours=6, ahead_h=0)
    assert [p for p in z["points"] if p["samples"]][0]["actual_power_w"] == pytest.approx(20.0)


def test_projections_do_not_mutate_inputs():
    cfg = building.default_config("office")
    rows = [_row(id=z) for z in ZONE_IDS]
    latest = {"zones": {z: {"co2": 700.0, "kwh": 1.0} for z in ZONE_IDS}}
    snap = copy.deepcopy((cfg, rows, latest))
    cards = building.zone_cards(cfg, rows, latest, [], 0, 11.0)
    building.topology(cfg, cards)
    building.demand_now(cfg, cards, 0, 11.0, {"power_w": 1.0, "t": 0.0})
    assert (cfg, rows, latest) == snap


def test_topology_marks_unmodelled_floors_and_clamps_floor_numbers():
    cfg, _ = building.apply_update(building.default_config("office"), {"floors": 1, "zones": 5})
    cards = building.zone_cards(cfg, [_row(id=z) for z in ZONE_IDS], None, [], 0, 11.0)
    topo = building.topology(cfg, cards)
    assert len(topo["floors"]) == 1 and sorted(topo["floors"][0]["zones"]) == sorted(ZONE_IDS)
    cfg6 = building.default_config("hospital")
    topo6 = building.topology(cfg6, building.zone_cards(cfg6, [_row(id=z) for z in ZONE_IDS],
                                                        None, [], 0, 11.0))
    assert len(topo6["floors"]) == 6
    assert [f["status"] for f in topo6["floors"][2:]] == ["not_modelled"] * 4


# ----------------------------------------------------------------- HTTP layer
@pytest.fixture
def office(fresh_client):
    """A freshly reset building on the office profile, restored afterwards
    (building config survives /api/reset by design, so tests must put it back)."""
    r = fresh_client.post("/api/building/profile",
                          json={"building_type": "office", "operating_mode": "normal"})
    assert r.status_code == 200
    yield fresh_client
    fresh_client.post("/api/building/profile",
                      json={"building_type": "office", "operating_mode": "normal"})
    fresh_client.post("/api/controller", json={"objective": "balanced", "safety_mode": "automatic"})


def test_building_endpoint_shape_and_every_value_is_tagged(office, live):
    live.advance(90)
    j = office.get("/api/building").json()
    assert set(j) >= {"config", "operating_mode", "topology", "zones", "demand", "kpis", "sources"}
    assert [c["id"] for c in j["zones"]] == ZONE_IDS
    for c in j["zones"]:
        for k in ("temp", "humidity", "occupancy", "co2", "comfort_score"):
            assert c[k]["source"] in ("sim", "derived"), k
        assert c["co2"]["source"] == "derived" and c["co2"]["estimated"] is True
        assert c["status"] in building.ZONE_STATUSES
        assert c["demand"]["heating_demand"]["source"] == "none"
    for key in ("current_energy", "today_energy", "hvac_load", "occupancy", "avg_temperature",
                "avg_humidity", "avg_co2", "comfort_score", "energy_saving", "peak_demand",
                "active_alerts", "system_health"):
        it = j["kpis"]["items"][key]
        assert it["source"] in ("sim", "derived", "predicted", "hardware", "real", "historical")
        assert it["status"] in ("normal", "warning", "critical", "unknown")
    modelled = [z for f in j["topology"]["floors"] for z in f["zones"]]
    assert sorted(modelled) == sorted(ZONE_IDS)
    assert office.get("/api/state").json()["building"]["type"] == "office"


def test_kpis_match_live_state(office, live):
    live.advance(120)
    b = office.get("/api/building").json()
    s = office.get("/api/state").json()
    k = b["kpis"]["items"]
    zones = {z["id"]: z for z in s["zones"]}
    occupied = [z for z in s["zones"] if z["occ"] > 0] or s["zones"]
    assert k["current_energy"]["value"] == pytest.approx(sum(z["power_w"] for z in s["zones"]), abs=0.5)
    assert k["occupancy"]["value"] == sum(z["occ"] for z in s["zones"])
    assert k["avg_temperature"]["value"] == pytest.approx(
        sum(zones[z["id"]]["temp"] for z in occupied) / len(occupied), abs=0.051)
    assert k["energy_saving"]["value"] == pytest.approx(s["meters"]["saved_pct"], abs=0.051)
    assert 0.0 < k["today_energy"]["value"] <= s["meters"]["us"]["kwh"] + 1e-6
    assert k["peak_demand"]["value"] >= k["current_energy"]["value"] - 0.5
    cards = b["zones"]
    comforts = [c["comfort_score"]["value"] for c in cards if c["occupancy"]["value"] > 0] or \
               [c["comfort_score"]["value"] for c in cards]
    assert k["comfort_score"]["value"] == pytest.approx(sum(comforts) / len(comforts), abs=0.051)
    assert k["system_health"]["value"] in ("ok", "degraded", "failing")


def test_switching_profiles_updates_config_topology_thresholds_and_levers(office, live):
    live.advance(30)
    for t in TYPES:
        r = office.post("/api/building/profile", json={"building_type": t})
        assert r.status_code == 200, r.text
        body = r.json()
        prof = building.PROFILES[t]
        assert body["config"]["building_type"] == t and body["levers_applied"] is True
        j = office.get("/api/building").json()
        assert j["config"]["name"] == prof.name
        assert len(j["topology"]["floors"]) == prof.floors
        assert j["kpis"]["thresholds"]["co2_ppm"] == prof.co2_max_ppm
        obj, safety = building.mode_levers(prof)
        ctl = office.get("/api/state").json()["controller"]
        assert ctl["objective"] == obj and ctl["requested_safety_mode"] == safety
        assert j["operating_mode"]["in_sync"] is True
    # the twin keeps its 5 zones whatever the profile
    assert [z["id"] for z in office.get("/api/state").json()["zones"]] == ZONE_IDS


def test_operating_mode_round_trip_and_sync_flag(office):
    r = office.post("/api/building/profile", json={"operating_mode": "emergency"}).json()
    assert r["controller"]["safety_mode"] == "emergency_override"
    office.post("/api/controller", json={"safety_mode": "automatic"})
    assert office.get("/api/building").json()["operating_mode"]["in_sync"] is False
    r = office.post("/api/building/profile", json={"operating_mode": "simulation"}).json()
    assert r["controller"]["requested_safety_mode"] == "recommend_only"
    assert r["operating_mode"]["in_sync"] is True


def test_profile_validation_over_http(office):
    before = office.get("/api/building/profile").json()["config"]
    r = office.post("/api/building/profile", json={"comfort_min_c": 27, "comfort_max_c": 26,
                                                   "zones": 2})
    assert r.status_code == 400
    errs = r.json()["detail"]["errors"]
    assert len(errs) >= 1
    assert office.get("/api/building/profile").json()["config"] == before
    assert office.post("/api/building/profile", json={}).status_code == 400
    assert office.post("/api/building/profile", json={"bogus": 1}).status_code == 422
    assert office.post("/api/building/profile", json={"floors": "many"}).status_code == 422
    cat = office.get("/api/building/profile").json()["catalog"]
    assert [t["key"] for t in cat["types"]] == list(TYPES)
    assert set(cat["operating_modes"]) == set(building.OPERATING_MODES)


def test_profile_survives_reset(office):
    office.post("/api/building/profile", json={"building_type": "hotel"})
    office.post("/api/reset")
    assert office.get("/api/building").json()["config"]["building_type"] == "hotel"


def test_zone_drill_down_uses_real_backend_values(office, live):
    live.advance(60)
    office.post("/api/complaint", json={"text": "conference room b is way too hot"})
    live.advance(5)
    for z in ZONE_IDS:
        d = office.get(f"/api/building/zones/{z}?window=1h").json()
        assert d["zone"]["id"] == z and z in d["floor"]["zones"]
    d = office.get("/api/building/zones/zone_b?window=1h").json()
    card, x = d["zone"], d["explanation"]
    facts = {f["label"]: f for f in x["factors"]}
    assert facts["Temperature"]["value"] == card["temp"]["value"]
    assert facts["Occupancy"]["value"] == card["occupancy"]["pct"]
    assert facts["CO₂ (estimated)"]["source"] == "derived"
    assert str(card["temp"]["value"]) in x["sentence"]
    decisions = [dec for dec in live.ctrl_us.last_decisions if dec.zone == "zone_b"]
    if decisions:
        assert x["reason_code"] == decisions[0].reason_code
    assert x["constraint"]["adjustment"] is not None                 # the complaint is live
    assert d["events"]["feed"] and "too hot" in d["events"]["feed"][0]["text"]
    assert len(d["trends"]["points"]) > 0
    assert {"temp", "rh", "occ_pct", "co2", "power_w", "comfort"} <= set(d["trends"]["points"][0])
    assert office.get("/api/building/zones/zone_q").status_code == 404
    assert office.get("/api/building/zones/zone_a?window=forever").status_code == 400


def test_demand_endpoint(office, live):
    live.advance(150)
    j = office.get("/api/building/demand?zone=all&hours=6&ahead_h=4").json()
    assert len(j["points"]) == 10
    assert j["fields"]["expected_hvac_w"] == "predicted" and j["fields"]["actual_occ_pct"] == "sim"
    past = [p for p in j["points"] if not p["future"] and p["samples"]]
    assert past and all(p["actual_power_w"] is not None for p in past)
    assert all(p["actual_power_w"] is None for p in j["points"] if p["future"])
    assert office.get("/api/building/demand?zone=zone_e&hours=3").status_code == 200
    for bad in ("hours=0", "hours=500", "ahead_h=-1", "zone=nope"):
        assert office.get("/api/building/demand?" + bad).status_code == 400


# ----------------------------------------------------------------- dashboard
def test_dashboard_serves_building_tab(client):
    html = client.get("/").text
    assert 'id="tab-building"' in html and 'id="panel-building"' in html
    assert "/static/building.js" in html and "'building'" in html
    assert 'id="tab-overview"' in html and 'id="panel-overview"' in html    # nothing removed
    js = client.get("/static/building.js")
    assert js.status_code == 200 and "registerPanel('building'" in js.text
    assert "window.FLChart" in client.get("/static/monitor.js").text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", ["dashboard/building.js", "dashboard/monitor.js"])
def test_dashboard_js_parses(path):
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(["node", "--check", str(root / path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
