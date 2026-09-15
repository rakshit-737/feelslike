"""Phase 3 — occupant comfort engine, events, durations, priorities, APIs, dashboards."""
from __future__ import annotations

import pytest

from backend import building, comfort
from backend.comfort import ComfortReading, assess
from backend.comfort_events import CLEAR_S, DEBOUNCE_S, ComfortTracker

OFFICE = building.default_config("office")          # 23.0-26.5 °C, 30-65 %, 1000 ppm


def R(**kw):
    base = dict(zone_id="zone_b", t=0.0, temp_c=24.5, rh_pct=50.0, co2_ppm=600.0, occupancy=10, occupancy_pct=80.0,
                hvac={"mode": "cooling", "cooling_pct": 40.0, "vent": 1, "setpoint": 24.0})
    base.update(kw)
    return ComfortReading(**base)


# 1-3 thermal
def test_temperature_inside_range():
    a = assess(R(), OFFICE)
    assert a["thermal"]["score"] == 100.0 and a["thermal"]["status"] == "Comfortable"
    assert a["thermal"]["deviation"] == 0.0 and a["status"] in ("Excellent", "Comfortable")


def test_temperature_above_range_matches_documented_example():
    cfg = building.apply_update(OFFICE, {"comfort_min_c": 23.0, "comfort_max_c": 26.0})[0]
    a = assess(R(temp_c=27.2), cfg)
    assert a["thermal"]["score"] == 60.0 and a["thermal"]["status"] == "Warm"
    assert a["thermal"]["deviation"] == pytest.approx(1.2) and a["status"] == "Warm"
    assert a["primary_cause"]["dimension"] == "thermal"


def test_temperature_below_range():
    a = assess(R(temp_c=21.0), OFFICE)
    assert a["thermal"]["status"] == "Cold" and a["thermal"]["deviation"] == pytest.approx(-2.0)
    assert a["thermal"]["severity"] == "medium" and "cold" in a["issues"]


# 4-6 humidity + CO2
def test_high_and_low_humidity():
    hi = assess(R(rh_pct=72.0), OFFICE)
    assert hi["humidity"]["status"] == "Humid" and hi["humidity"]["score"] == 65.0 and hi["status"] == "Humid"
    lo = assess(R(rh_pct=22.0), OFFICE)
    assert lo["humidity"]["status"] == "Dry" and lo["humidity"]["deviation"] == pytest.approx(-8.0)


def test_high_co2_is_a_ventilation_indicator_and_not_hidden_by_thermal_comfort():
    a = assess(R(co2_ppm=1350.0), OFFICE)
    aq = a["air_quality"]
    assert aq["status"] == "High CO2" and aq["deviation"] == 350 and aq["target"][1] == 1000
    assert "ventilation indicator" in aq["indicator"]
    assert a["thermal"]["status"] == "Comfortable" and a["status"] == "High CO2"   # not just "comfortable"
    assert assess(R(co2_ppm=1600.0), OFFICE)["air_quality"]["status"] == "Poor"
    assert assess(R(co2_ppm=1600.0), OFFICE)["status"] == "Poor Air Quality"


# 7-8 occupancy
def test_unoccupied_zone_is_not_treated_like_an_occupied_one():
    empty = assess(R(temp_c=28.0, occupancy=0, occupancy_pct=0.0), OFFICE)
    full = assess(R(temp_c=28.0, occupancy=12, occupancy_pct=95.0), OFFICE)
    assert empty["status"] == "Unoccupied" and empty["comfort_relevant"] is False
    assert empty["occupied_score"] is None and empty["severity"] == "info"
    assert empty["recommendation"]["action"] == "none"
    assert full["status"] == "Warm" and full["comfort_relevant"] and full["severity"] == "medium"
    assert empty["score"] == full["score"]                   # same air, different relevance
    assert assess(R(occupancy=2, occupancy_pct=20.0), OFFICE)["occupancy_state"] == "Partially Occupied"


def test_occupied_uncomfortable_zone_root_cause_and_action_from_real_values():
    a = assess(R(temp_c=27.6, co2_ppm=1180.0, occupancy_pct=87.0,
                 hvac={"mode": "cooling", "cooling_pct": 82.0, "vent": 1, "setpoint": 24.0}), OFFICE)
    assert a["primary_cause"]["dimension"] == "thermal" and a["primary_cause"]["value"] == 27.6
    assert a["secondary_causes"][0]["dimension"] == "air_quality" and a["secondary_causes"][0]["value"] == 1180.0
    rec = a["recommendation"]
    assert "increase cooling" in rec["text"].lower() and "ventilation" in rec["text"].lower()
    assert "energy" in rec["energy_consideration"] and rec["expected_effect"]
    at_cap = assess(R(temp_c=27.6, hvac={"cooling_pct": 100.0, "at_capacity": True, "vent": 2}), OFFICE)
    assert "capacity" in at_cap["recommendation"]["text"]


# 9-10 combined + building
def test_combined_score_uses_profile_weights():
    a = assess(R(temp_c=28.0, rh_pct=75.0, co2_ppm=1000.0), OFFICE)          # 50 / 50 / 50
    assert a["thermal"]["score"] == 50.0 and a["humidity"]["score"] == 50.0 and a["air_quality"]["score"] == 50.0
    assert a["score"] == pytest.approx(50.0)
    cfg = building.apply_update(OFFICE, {"comfort_weights": {"thermal": 1.0, "humidity": 0.0, "air_quality": 0.0}})[0]
    assert assess(R(temp_c=26.5 + 1.5, co2_ppm=2000.0), cfg)["score"] == 50.0
    w = OFFICE.comfort_weights
    b = assess(R(temp_c=27.1, rh_pct=50.0, co2_ppm=600.0), OFFICE)
    assert b["score"] == pytest.approx(round(w["thermal"] * b["thermal"]["score"] + w["humidity"] * 100 + w["air_quality"] * 100, 1))


def test_building_summary():
    zs = [assess(R(zone_id="a"), OFFICE), assess(R(zone_id="b", temp_c=28.0), OFFICE),
          assess(R(zone_id="c", co2_ppm=1300.0), OFFICE), assess(R(zone_id="d", occupancy=0, occupancy_pct=0), OFFICE)]
    s = comfort.building_summary(zs)
    assert s["occupied_zones"] == 3 and s["uncomfortable_zones"] == 2 and s["comfortable_zones"] == 1
    assert s["compliance_pct"] == pytest.approx(33.3) and s["worst_zone"]["zone_id"] == "b"
    assert s["warm_zones"] == 1 and s["high_co2_zones"] == 1 and s["overall_score"] is not None


# 11-13 events + durations
def _feed(tr, minutes, **kw):
    t0 = tr._last_t + 60 if tr._last_t is not None else 60.0
    for i in range(minutes):
        tr.tick(t0 + 60 * i, [assess(R(t=t0 + 60 * i, **kw), OFFICE)], {"zone_b": {"zone_name": "Conference Room B", "floor": 2}},
                {"id": "office", "name": "HQ"})


def test_event_creation_debounced_with_context():
    tr = ComfortTracker()
    _feed(tr, int(DEBOUNCE_S / 60) - 1, temp_c=28.1)
    assert not tr.open
    _feed(tr, 3, temp_c=28.1)
    ev = tr.list(tr._last_t, status="open")[0]
    assert ev["event_type"] == "warm" and ev["triggering_metric"] == "temperature" and ev["measured_value"] == 28.1
    assert ev["threshold"] == [23.0, 26.5] and ev["occupancy_state"] == "Occupied" and ev["zone_name"] == "Conference Room B"
    assert ev["hvac_state"]["mode"] == "cooling" and ev["recommended_action"] and ev["severity"] == "medium"
    assert ev["started_t"] == 60.0 and ev["duration_s"] > DEBOUNCE_S


def test_event_resolution_and_vacated():
    tr = ComfortTracker()
    _feed(tr, 10, temp_c=29.2)                 # 2.7 K over 26.5 -> high (3.3 K would be severe)
    _feed(tr, int(CLEAR_S / 60) + 2, temp_c=24.0)
    ev = tr.list(tr._last_t, status="resolved")[0]
    assert ev["resolution"] == "condition returned within range" and ev["peak_severity"] == "high"
    assert ev["resolved_t"] > ev["started_t"] and not tr.open
    tr2 = ComfortTracker()
    _feed(tr2, 10, co2_ppm=1400.0)
    _feed(tr2, 1, co2_ppm=1400.0, occupancy=0, occupancy_pct=0.0)
    assert tr2.list(tr2._last_t)[0]["resolution"] == "zone vacated"


def test_discomfort_duration_accounting():
    tr = ComfortTracker()
    _feed(tr, 60, temp_c=24.0)                 # 1 h comfortable
    _feed(tr, 30, temp_c=28.0)                 # 30 min warm
    _feed(tr, 30, temp_c=24.0, occupancy=0, occupancy_pct=0.0)   # unoccupied: not counted
    d = tr.durations("zone_b", tr._last_t)
    assert d["today"]["occupied_s"] == pytest.approx(90 * 60, abs=60)
    assert d["today"]["uncomfortable_s"] == pytest.approx(30 * 60, abs=60)
    assert d["today"]["uncomfortable_pct"] == pytest.approx(33.3, abs=1.0)
    assert d["week"]["occupied_s"] >= d["today"]["occupied_s"]


# 14-15 profiles + priorities
def test_building_profile_differences():
    ws = {t: building.PROFILES[t].comfort_weights for t in building.BUILDING_TYPES}
    for t, w in ws.items():
        assert set(w) == {"thermal", "humidity", "air_quality"} and sum(w.values()) == pytest.approx(1.0), t
    assert ws["hospital"]["air_quality"] >= 0.4 and ws["data_center"]["humidity"] > ws["office"]["humidity"]
    r = R(temp_c=25.5, co2_ppm=900.0)
    assert assess(r, building.PROFILES["office"])["status"] in ("Excellent", "Comfortable")
    hosp = assess(r, building.PROFILES["hospital"])
    assert hosp["thermal"]["status"] == "Warm"                                     # 22-25 °C
    assert hosp["air_quality"]["status"] == "High CO2"                             # 800 ppm
    # both low severity; CO2 costs more score (weight 0.4 x 56 pts vs 0.4 x 17 pts) -> headline
    assert hosp["status"] == "High CO2" and hosp["primary_cause"]["dimension"] == "air_quality"
    assert {"warm", "high_co2"} <= set(hosp["issues"])
    assert "operator configurable" in building.catalog()["comfort_weights_note"]
    assert building.apply_update(OFFICE, {"comfort_weights": {"thermal": 0.9, "humidity": 0.9, "air_quality": 0}})[1]


def test_comfort_energy_priority_interface():
    comfy = building.apply_update(OFFICE, {"comfort_priority": 80, "energy_priority": 20})[0]
    thrifty = building.apply_update(OFFICE, {"comfort_priority": 30, "energy_priority": 70})[0]
    pc, pe = comfort.controller_preference(comfy), comfort.controller_preference(thrifty)
    assert pc["comfort_weight"] == 0.8 and pc["objective_hint"] == "comfort" and pc["act_from_severity"] == "low"
    assert pe["energy_weight"] == 0.7 and pe["objective_hint"] == "energy" and pe["act_from_severity"] == "high"
    mild = R(temp_c=27.2)                                   # low severity warm
    assert assess(mild, comfy)["recommendation"]["action"] == "act"
    d = assess(mild, thrifty)["recommendation"]
    assert d["action"] == "defer" and "Energy priority" in d["text"]
    assert building.mode_levers(comfy)[0] == pc["objective_hint"]      # same mapping as the controller levers


# 16-17 data quality
def test_missing_sensor_data_is_reported_not_invented():
    a = assess(R(co2_ppm=None), OFFICE)
    assert a["air_quality"]["status"] == "Unavailable" and a["air_quality"]["score"] is None
    assert "co2" in a["data_quality"]["missing"]
    w = OFFICE.comfort_weights
    assert a["score"] == pytest.approx(100.0)               # renormalised over thermal + humidity
    none = assess(R(temp_c=None, rh_pct=None, co2_ppm=None), OFFICE)
    assert none["score"] is None and none["status"] == "Unavailable" and w


@pytest.mark.parametrize("field,val", [("temp_c", 999.0), ("temp_c", float("nan")), ("rh_pct", 140.0),
                                       ("rh_pct", -3.0), ("co2_ppm", 12.0), ("co2_ppm", "abc")])
def test_invalid_sensor_values(field, val):
    a = assess(R(**{field: val}), OFFICE)
    name = {"temp_c": "temperature", "rh_pct": "humidity", "co2_ppm": "co2"}[field]
    assert a["data_quality"]["fields"][name] == "invalid" and name in a["data_quality"]["missing"]


def test_stale_reading_flagged():
    a = assess(R(age_s={"temp_c": 3600.0}), OFFICE)
    assert a["data_quality"]["fields"]["temperature"] == "stale" and "temperature" in a["data_quality"]["stale"]


def test_duplicate_timestamps_do_not_double_count():
    tr = ComfortTracker()
    a = assess(R(temp_c=28.0), OFFICE)
    tr.tick(600.0, [a], {}, {})
    tr.tick(600.0, [a], {}, {})                             # same t again
    tr.tick(660.0, [a], {}, {})
    assert tr.durations("zone_b", 660.0)["today"]["occupied_s"] == pytest.approx(120.0)


def test_dataset_comfort_uses_the_same_formulas():
    from backend.dataset import comfort as dsc
    s = dsc.scores(27.0, 70.0, 1100.0, OFFICE)
    assert s["temperature_comfort_score"] == comfort.thermal_score(27.0, 23.0, 26.5)[0]
    assert s["humidity_comfort_score"] == comfort.humidity_score(70.0, 30.0, 65.0)[0]
    assert s["co2_comfort_score"] == comfort.co2_score(1100.0, 1000.0)


# 18-19 history + APIs
@pytest.fixture
def office_client(fresh_client):
    fresh_client.post("/api/building/profile", json={"building_type": "office", "operating_mode": "normal"})
    yield fresh_client
    fresh_client.post("/api/building/profile", json={"building_type": "office", "operating_mode": "normal"})
    fresh_client.post("/api/controller", json={"objective": "balanced", "safety_mode": "automatic"})


def test_comfort_api_live(office_client, live):
    live.advance(150)
    j = office_client.get("/api/comfort").json()
    assert set(j) >= {"summary", "zones", "floors", "weights", "priority", "thresholds", "events_open"}
    assert len(j["zones"]) == 5 and j["summary"]["source"] == "derived"
    z = j["zones"][0]
    for k in ("score", "status", "severity", "factors", "recommendation", "occupancy_state", "durations",
              "thermal", "humidity", "air_quality", "data_quality"):
        assert k in z
    assert z["thermal"]["source"] == "sim" and z["air_quality"]["source"] == "derived"
    assert z["status"] in comfort.OVERALL_STATUSES
    st = office_client.get("/api/state").json()
    rows = {r["id"]: r for r in st["zones"]}
    assert all(abs(zz["thermal"]["value"] - rows[zz["zone_id"]]["temp"]) < 1e-9 for zz in j["zones"])
    assert len(office_client.get("/api/comfort?floor=2").json()["zones"]) == 3
    assert office_client.get("/api/comfort?zone=nope").status_code == 400
    assert office_client.get("/api/comfort?issue=nope").status_code == 400
    zd = office_client.get("/api/comfort/zone/zone_b").json()
    assert zd["zone_id"] == "zone_b" and zd["trend"]["points"] and "events" in zd
    assert office_client.get("/api/comfort/zone/zone_x").status_code == 404
    assert "comfort" in office_client.get("/api/building/zones/zone_b").json()


def test_profile_switch_changes_thresholds_and_statuses(office_client, live):
    live.advance(60)
    for t in building.BUILDING_TYPES:
        office_client.post("/api/building/profile", json={"building_type": t})
        j = office_client.get("/api/comfort").json()
        p = building.PROFILES[t]
        assert j["thresholds"]["comfort_c"] == [p.comfort_min_c, p.comfort_max_c]
        assert j["thresholds"]["co2_ppm"] == p.co2_max_ppm and j["weights"] == p.comfort_weights
    r = office_client.post("/api/building/profile", json={"comfort_weights": {"thermal": 0.2, "humidity": 0.2, "air_quality": 0.2}})
    assert r.status_code == 400


def test_comfort_events_and_history_api(office_client, live):
    office_client.post("/api/conditions", json={"capacity_scale": 0.1, "outdoor_offset": 8})
    try:
        live.advance(180)
        ev = office_client.get("/api/comfort/events").json()["events"]
        assert ev, "a crippled HVAC on a hot day must produce comfort events"
        e = ev[0]
        for k in ("event_id", "started_t", "zone_id", "event_type", "severity", "triggering_metric", "threshold",
                  "measured_value", "occupancy_state", "hvac_state", "recommended_action", "status", "duration_s"):
            assert k in e
        assert office_client.get("/api/comfort/events?status=open&zone=zone_a").status_code == 200
        assert office_client.get("/api/comfort/events?status=maybe").status_code == 400
    finally:
        office_client.post("/api/conditions", json={"capacity_scale": 1.0, "outdoor_offset": 0})
    h = office_client.get("/api/comfort/history?zone=all&range=1h").json()
    assert h["available"] and h["points"] and h["source"] == "sim" and h["score_source"] == "derived"
    assert {"comfort_score", "temperature_c", "humidity_pct", "co2_ppm", "occupancy"} <= set(h["points"][0])
    long = office_client.get("/api/comfort/history?zone=zone_a&range=30d").json()
    assert long["partial"] is True                          # 3 sim-hours cannot fill 30 days
    assert office_client.get("/api/comfort/history?range=2y").status_code == 400


def test_history_empty_buffer_says_no_data(fresh_client):
    j = fresh_client.get("/api/comfort/history?range=1h").json()
    assert (not j["points"] and j["message"] == "No data available") or j["points"]


def test_historical_source_and_aggregation(client, tmp_path, monkeypatch):
    import backend.app as appmod
    from backend.dataset.config import AnomalyConfig, BuildingSpec, DatasetConfig
    from backend.dataset.generator import generate
    from backend.dataset.store import SqliteStore
    p = tmp_path / "h.sqlite"
    s = SqliteStore(p)
    s.create()
    generate(DatasetConfig(start="2026-06-08", days=1, step_min=15, seed=1,
                           buildings=[BuildingSpec("b", "office", 1)], anomalies=AnomalyConfig(enabled=False)), s)
    monkeypatch.setattr(appmod, "HISTORY", SqliteStore(p))
    j = client.get("/api/comfort/history?source=historical&building_id=b&range=24h").json()
    assert j["source"] == "historical" and j["available"] and len(j["points"]) == 96   # 24 h auto = 15 min
    assert all(0 <= x["comfort_score"] <= 100 for x in j["points"])
    z = client.get("/api/comfort/history?source=historical&building_id=b&zone=F1-zone_a&range=6h")
    assert z.status_code in (200, 400)
    monkeypatch.setattr(appmod, "HISTORY", SqliteStore(tmp_path / "none.sqlite"))
    assert client.get("/api/comfort/history?source=historical").json()["message"] == "No data available"


def test_tradeoff_is_labelled_whatif_and_isolated(office_client, live):
    live.advance(120)
    before = (live.us.t, live.us.kwh)
    j = office_client.get("/api/comfort/tradeoff?zone=zone_a&action=cool&horizon_h=0.5").json()
    assert j["label"] == "SIMULATED / WHAT-IF" and j["kind"] == "predicted" and j["isolation_verified"]
    assert j["proposed"]["energy_kwh"] >= j["current"]["energy_kwh"] - 1e-9
    assert (live.us.t, live.us.kwh) == before
    assert office_client.get("/api/comfort/tradeoff?zone=zone_a&action=teleport").status_code == 400


def test_occupant_view_is_simple_and_excludes_infrastructure(office_client, live):
    live.advance(60)
    j = office_client.get("/api/comfort/occupant?zone=zone_b").json()
    assert set(j) >= {"status", "score", "temperature", "humidity", "air_quality", "explanation", "trend", "issue"}
    blob = str(j).lower()
    for secret in ("hardware", "node_id", "ssid", "password", "ip", "watchdog", "reason_code", "duty"):
        assert f"'{secret}'" not in blob
    assert j["air_quality"]["label"].startswith("CO₂")


# 20 dashboards
def test_dashboards_serve_comfort(client):
    html = client.get("/").text
    assert 'id="tab-comfort"' in html and "/static/comfort.js" in html
    js = client.get("/static/comfort.js").text
    assert "registerPanel('comfort'" in js and "/api/comfort" in js
    occ = client.get("/occupant").text
    assert "/api/comfort/occupant" in occ and 'id="comfortcard"' in occ
    assert "fl:select-zone" in client.get("/static/building.js").text
