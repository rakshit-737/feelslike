"""Phase 4 — latest-value telemetry: store, ingestion, freshness, quality, provenance,
twin/derived/hardware publishing, comfort/occupancy/HVAC/energy integration, APIs,
sensor + system health, dashboards."""
from __future__ import annotations

import threading

import pytest

from backend import comfort
from backend import latest as lv
from backend import telemetry_publish as tp
from backend.building import default_config


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clk():
    return Clock()


@pytest.fixture
def store(clk):
    return lv.LatestStore(clock=clk)


def put(store, metric, value, zone="zone_a", source="sim", **kw):
    return store.ingest({"metric": metric, "value": value, "zone_id": zone, "building_id": "b1", **kw}, source)


# 1-6 write / read / zone / building / metric / timestamps
def test_write_and_read(store, clk):
    r = put(store, "temperature", 24.8)
    assert r["value"] == 24.8 and r["unit"] == "°C" and r["source"] == "sim" and r["quality"] == "good"
    assert r["timestamp"].endswith("Z") and r["age_s"] == 0.0 and r["sensor_id"] == "sim:zone_a:temperature"
    g = store.get("zone_a", "temperature")
    assert g["value"] == 24.8 and store.get("zone_a", "humidity") is None


def test_queries_by_zone_building_metric_source_sensor(store):
    put(store, "temperature", 24.0, "zone_a")
    put(store, "temperature", 25.0, "zone_b")
    put(store, "humidity", 50.0, "zone_a")
    put(store, "power", 1200.0, lv.BUILDING, "derived")
    store.ingest({"metric": "temperature", "value": 24.6, "zone_id": "zone_b"}, "hardware", "HW-node1-TEMP", "node1")
    assert len(store.query(zone="zone_a")) == 2
    assert len(store.query(metric="temperature")) == 3
    assert len(store.query(source="hardware")) == 1 and store.query(sensor="HW-node1-TEMP")[0]["device_id"] == "node1"
    assert store.query(zone=lv.BUILDING)[0]["metric"] == "power"
    assert len(store.query(building_id="b1")) == 5                   # hardware row has no building -> matches too
    assert store.query(metric="energy") == [] and store.query(metric="comfort") == []   # aliases resolve


def test_timestamp_handling(store, clk):
    ok = put(store, "temperature", 24.0, t_wall=clk.t - 10)
    assert ok["age_s"] == 10.0 and ok["timestamp"] == lv.iso(clk.t - 10)
    fut = put(store, "temperature", 24.0, "zone_b", t_wall=clk.t + 3600)
    assert fut["quality"] == "invalid" and "invalid timestamp" in fut["problems"]
    for bad in ("yesterday", float("nan"), clk.t - 30 * 86400):
        assert put(store, "humidity", 40.0, "zone_c", t_wall=bad)["quality"] == "invalid"


# 7-8 age + stale
def test_age_and_stale_detection_never_refreshes(store, clk):
    put(store, "temperature", 24.0)
    clk.t += 30
    assert store.get("zone_a", "temperature")["quality"] == "good"
    clk.t += 60
    r = store.get("zone_a", "temperature")
    assert r["quality"] == "aging" and r["age_s"] == 90.0
    clk.t += 400
    r = store.get("zone_a", "temperature")
    assert r["quality"] == "stale" and r["value"] == 24.0 and r["stored_quality"] == "good"
    assert store.counts()["stale"] == 1
    assert lv.freshness(60) == "good" and lv.freshness(61) == "aging" and lv.freshness(301) == "stale"


# 9-10 invalid + missing
@pytest.mark.parametrize("metric,value", [("temperature", 999), ("temperature", -80), ("humidity", 140), ("co2", -5),
                                          ("co2", 20000), ("occupancy", -1), ("power", -3), ("energy_today", -0.1),
                                          ("temperature", "hot"), ("comfort_score", 130), ("at_capacity", "yes")])
def test_invalid_readings_are_marked_not_accepted_as_values(store, metric, value):
    r = put(store, metric, value)
    assert r["quality"] == "invalid" and r["value"] is None and r["problems"]
    assert r["rejected_value"] is not None
    assert store.counts()["invalid"] == 1 and store.invalid == 1


def test_missing_and_unaddressable(store):
    assert put(store, "co2", None)["quality"] == "missing"
    with pytest.raises(ValueError):
        put(store, "radiation", 1.0)
    with pytest.raises(ValueError):
        put(store, "temperature", 20.0, zone="zone_a; DROP TABLE")
    with pytest.raises(ValueError):
        store.ingest({"metric": "temperature", "value": 20}, "trusted-by-client")
    assert store.rejected == 2


# 11 provenance: payload cannot relabel itself
def test_payload_source_label_is_ignored(store):
    r = store.ingest({"metric": "temperature", "value": 21.0, "zone_id": "zone_a", "source": "real"}, "sim")
    assert r["source"] == "sim"


def test_preference_policy(store):
    put(store, "temperature", 24.0, "zone_b", "sim")
    store.ingest({"metric": "temperature", "value": 26.0, "zone_id": "zone_b"}, "hardware", "HW-n-TEMP", "n")
    assert store.get("zone_b", "temperature", ("sim", "hardware"))["source"] == "sim"
    assert store.get("zone_b", "temperature", ("hardware", "sim"))["source"] == "hardware"


def test_stale_hardware_does_not_win_over_fresh_sim(store, clk):
    store.ingest({"metric": "temperature", "value": 26.0, "zone_id": "zone_b"}, "hardware", "HW-n-TEMP", "n")
    clk.t += 1000
    put(store, "temperature", 24.0, "zone_b", "sim")
    assert store.get("zone_b", "temperature", ("hardware", "sim"))["source"] == "sim"


# 15 concurrency
def test_concurrent_reads_and_writes(store):
    errors = []

    def writer(i):
        try:
            for k in range(300):
                put(store, "temperature", 20 + (k % 10), f"zone_{i}")
        except Exception as e:                       # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            for _ in range(300):
                store.query()
                store.counts()
                store.sensors()
                store.recent("zone_1", "temperature")
        except Exception as e:                       # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)] + [threading.Thread(target=reader) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and store.accepted == 1800 and len(store.query(metric="temperature")) == 6


# 16-17 comfort + occupancy via the store
def test_comfort_reading_from_store_carries_sources_and_ages(store, clk):
    for m, v, s in (("temperature", 27.5, "sim"), ("humidity", 50.0, "sim"), ("occupancy", 8, "sim"),
                    ("occupancy_pct", 70.0, "sim")):
        put(store, m, v, source=s)
    put(store, "co2", 900.0, source="derived", quality="estimated")
    rd = tp.comfort_reading(store, "zone_a", ("sim", "derived"))
    assert rd.temp_c == 27.5 and rd.source["co2_ppm"] == "derived" and rd.age_s["temp_c"] == 0.0
    a = comfort.assess(rd, default_config("office"))
    assert a["thermal"]["status"] == "Warm" and a["comfort_relevant"]
    clk.t += 400                                                  # temperature goes stale
    for m, v in (("humidity", 50.0), ("occupancy", 8), ("occupancy_pct", 70.0)):
        put(store, m, v)
    put(store, "co2", 900.0, source="derived", quality="estimated")
    a2 = comfort.assess(tp.comfort_reading(store, "zone_a", ("sim", "derived")), default_config("office"))
    assert "temperature" in a2["data_quality"]["stale"]
    put(store, "occupancy", 0)
    put(store, "occupancy_pct", 0.0)
    a3 = comfort.assess(tp.comfort_reading(store, "zone_a", ("sim", "derived")), default_config("office"))
    assert a3["occupancy_state"] == "Unoccupied" and a3["status"] == "Unoccupied"
    put(store, "temperature", 999.0)
    assert tp.comfort_reading(store, "zone_a", ("sim",)).temp_c is None     # invalid never used


def test_comfort_published_with_oldest_input_time(store, clk):
    put(store, "temperature", 24.0, t_wall=clk.t - 500)
    put(store, "humidity", 50.0)
    rd = tp.comfort_reading(store, "zone_a", ("sim", "derived"))
    a = comfort.assess(rd, default_config("office"))
    tp.publish_comfort(store, [a], ("sim", "derived"), "b1", {"zone_a": 1}, 0.0, clk.t)
    c = store.get("zone_a", "comfort_score")
    assert c["source"] == "derived" and c["quality"] == "stale"


def test_views_and_missing_blocks(store):
    blk = tp.zone_block(store, "zone_c", "Cabin C", 2, ("sim",))
    assert blk["temperature"]["value"] is None and blk["temperature"]["message"] == tp.NOT_AVAILABLE
    assert blk["comfort"]["missing_inputs"] == ["temperature", "humidity", "co2", "occupancy"]


# system health
def test_system_status_rules(store, clk):
    zones = ["zone_a", "zone_b"]
    assert lv.system_status(store, zones)["status"] == "OFFLINE"
    put(store, "temperature", 24.0, "zone_a")
    assert lv.system_status(store, zones)["status"] == "DEGRADED"          # zone_b has no temperature
    put(store, "temperature", 24.0, "zone_b")
    assert lv.system_status(store, zones)["status"] == "SIMULATION MODE"
    store.ingest({"metric": "temperature", "value": 25.0, "zone_id": "zone_b"}, "hardware", "HW-n-TEMP", "n")
    assert lv.system_status(store, zones)["status"] == "LIVE TELEMETRY"
    clk.t += 200
    put(store, "temperature", 24.0, "zone_a")
    put(store, "temperature", 24.0, "zone_b")
    clk.t += 150                      # hardware now 350 s old -> stale; sim 150 s -> aging
    assert lv.system_status(store, zones)["status"] in ("DEGRADED", "STALE DATA")
    clk.t += 400
    assert lv.system_status(store, zones)["status"] == "OFFLINE"


# 26 sensor health
def test_sensor_health_statuses(store, clk):
    store.ingest({"metric": "temperature", "value": 24.0, "zone_id": "zone_a"}, "sim", "SIM-ZONE-A-TEMP", "digital-twin")
    store.ingest({"metric": "humidity", "value": 400.0, "zone_id": "zone_a"}, "sim", "SIM-ZONE-A-RH", "digital-twin")
    s = {x["sensor_id"]: x for x in store.sensors()}
    assert s["SIM-ZONE-A-TEMP"]["status"] == "Healthy" and s["SIM-ZONE-A-TEMP"]["simulated"]
    assert s["SIM-ZONE-A-RH"]["status"] == "Invalid"
    clk.t += 120
    assert {x["sensor_id"]: x for x in store.sensors()}["SIM-ZONE-A-TEMP"]["status"] == "Aging"
    clk.t += 2000
    assert {x["sensor_id"]: x for x in store.sensors()}["SIM-ZONE-A-TEMP"]["status"] == "Offline"


def test_recent_buffer_is_bounded_to_15_minutes(store, clk):
    for i in range(40):
        put(store, "temperature", 20.0 + i * 0.1)
        clk.t += 60
    pts = store.recent("zone_a", "temperature")[0]["points"]
    assert 14 <= len(pts) <= 16 and pts[-1]["value"] == pytest.approx(23.9)


# ---------------------------------------------------------------- API (12 simulated, 13 derived, 14 hw seam, 18-25)
@pytest.fixture
def api(fresh_client, live):
    fresh_client.post("/api/building/profile", json={"building_type": "office", "operating_mode": "normal"})
    live.advance(30)
    return fresh_client


def test_latest_endpoint_publishes_twin_state(api):
    j = api.get("/api/latest").json()
    for k in ("timestamp", "building", "zones", "data_quality", "system_health", "sources", "poll_interval_s", "notice"):
        assert k in j
    assert len(j["zones"]) == 5
    st = {z["id"]: z for z in api.get("/api/state").json()["zones"]}
    for z in j["zones"]:
        t = z["temperature"]
        assert t["source"] == "sim" and t["quality"] == "good" and t["age_s"] < 60 and t["timestamp"]
        assert t["sensor_id"] == "SIM-" + z["zone_id"].upper().replace("_", "-") + "-TEMP"
        assert t["value"] == st[z["zone_id"]]["temp"]
        assert z["occupancy"]["value"] == st[z["zone_id"]]["occ"] and z["occupancy"]["source"] == "sim"
        assert z["co2"]["source"] == "derived" and z["co2"]["quality"] == "estimated"
        assert z["comfort"]["score"]["source"] == "derived"
        hv = z["hvac"]
        assert hv["hvac_mode"]["value"] in ("cooling", "ventilation", "off", "idle")
        assert hv["cooling_pct"]["source"] == "derived" and hv["controller_action"]["value"]
        assert z["energy"]["power"]["unit"] == "W" and z["demand"]["demand"]["source"] == "derived"
    b = j["building"]["metrics"]
    assert b["power"]["unit"] == "W" and b["energy_today"]["unit"] == "kWh"
    assert b["power"]["source"] == "derived" and b["expected_demand"]["source"] == "predicted"
    assert b["outdoor_temperature"]["source"] == "sim"
    assert j["system_health"]["status"] in ("SIMULATION MODE", "LIVE TELEMETRY", "DEGRADED")
    assert j["sources"]["temperature"] and "sim" in j["sources"]["temperature"]


def test_latest_zone_metric_sensors_health_trend(api, live):
    z = api.get("/api/latest/zone/zone_b").json()
    assert z["zone_id"] == "zone_b" and set(z["alerts"]) == {"maintenance", "thresholds", "comfort_events", "data_quality"}
    assert z["sensors"] and all(s["zone_id"] == "zone_b" for s in z["sensors"])
    assert api.get("/api/latest/zone/zone_q").status_code == 404
    m = api.get("/api/latest/temperature?zone=zone_a").json()
    assert m["unit"] == "°C" and len(m["readings"]) >= 1 and m["readings"][0]["zone_id"] == "zone_a"
    assert api.get("/api/latest/energy").json()["metric"] == "energy_today"
    assert api.get("/api/latest/hvac_power?source=derived").json()["readings"]
    assert api.get("/api/latest/wind_chill").status_code == 404
    assert api.get("/api/latest/temperature?source=gossip").status_code == 400
    assert api.get("/api/latest/humidity?zone=nope").json()["message"] == tp.NOT_AVAILABLE
    only = api.get("/api/latest?zone=zone_c").json()
    assert [x["zone_id"] for x in only["zones"]] == ["zone_c"]
    assert api.get("/api/latest?zone=zzz").status_code == 400
    h = api.get("/api/latest/health").json()
    assert h["system_health"]["counts"]["healthy"] > 0 and h["stale_after_s"] == 300
    s = api.get("/api/latest/sensors?zone=zone_a&source=sim").json()
    assert s["count"] > 0 and all(x["simulated"] for x in s["sensors"])
    live.advance(3)
    tr = api.get("/api/latest/trend?zone=zone_a&minutes=15").json()
    assert tr["label"] == "LIVE SIMULATION" and tr["series"]["temperature"][0]["points"]
    assert api.get("/api/latest/trend?minutes=90").status_code == 400


def test_hardware_seam_same_pipeline(api):
    r = api.post("/api/hw/reading", json={"node_id": "shoebox-test", "temp_c": 29.4, "rh_pct": 61.0, "seq": 5})
    assert r.status_code == 200
    j = api.get("/api/latest/zone/zone_b").json()
    hw = [a for a in j["alternatives"]["temperature"] if a["source"] == "hardware"]
    assert hw and hw[0]["value"] == 29.4 and hw[0]["sensor_id"] == "HW-shoebox-test-TEMP" and hw[0]["seq"] == 5
    assert j["temperature"]["source"] == "sim"                    # hardware not authoritative by default
    assert api.get("/api/latest").json()["system_health"]["status"] == "LIVE TELEMETRY"
    assert api.post("/api/hw/reading", json={"node_id": "x", "temp_c": 900}).status_code == 422   # bridge validation first
    amb = api.post("/api/hw/sensor", json={"node_id": "uno-1", "temp_c": 27.0, "seq": 1})
    assert amb.status_code == 200
    a = api.get("/api/latest/temperature?zone=ambient").json()["readings"][0]
    assert a["source"] == "hardware" and a["quality"] == "estimated"          # uncalibrated


def test_reset_republishes_immediately_and_no_fake_zero(fresh_client):
    j = fresh_client.get("/api/latest").json()
    for z in j["zones"]:
        assert z["temperature"]["value"] is not None
        assert z["co2"]["value"] is None and z["co2"]["message"] == tp.NOT_AVAILABLE    # no telemetry row yet
        assert "co2" in z["comfort"]["missing_inputs"]


def test_comfort_endpoints_now_read_the_store(api, live):
    live.latest.ingest({"metric": "temperature", "value": 999, "zone_id": "zone_d"}, "sim", "SIM-ZONE-D-TEMP", "digital-twin")
    z = [x for x in api.get("/api/comfort").json()["zones"] if x["zone_id"] == "zone_d"][0]
    assert z["thermal"]["status"] == "Unavailable" and "temperature" in z["data_quality"]["missing"]
    occ = api.get("/api/comfort/occupant?zone=zone_a").json()
    assert occ["updated_age_s"] is not None
    live.advance(1)                                               # the twin republishes a valid value
    z = [x for x in api.get("/api/comfort").json()["zones"] if x["zone_id"] == "zone_d"][0]
    assert z["thermal"]["status"] != "Unavailable"


# 22-24 dashboards
def test_dashboard_single_central_poll(client):
    html = client.get("/").text
    assert "/api/latest" in html and 'id="livestat"' in html and "fl:latest" in html and "fl.pollS" in html
    assert html.count("apiGet('/api/latest')") == 1                     # one poller, not one per panel
    assert "OFFLINE" in html                                             # backend failure path exists
    bjs = client.get("/static/building.js").text
    assert "FL.on('latest'" in bjs and "Unavailable" in bjs and "/api/latest')" not in bjs
    for f in ("comfort.js", "history.js", "monitor.js", "panels.js"):
        assert "/api/latest'" not in client.get("/static/" + f).text
