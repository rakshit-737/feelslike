"""Phase 5 — SIMULATED hardware (not real hardware): registry and mapping, protocol adapters,
sensor characteristics, faults, latest-value / comfort integration, APIs."""
from __future__ import annotations

import re
import statistics

import pytest

from backend import comfort
from backend import latest as lv
from backend import simhw
from backend import telemetry_publish as tp
from backend.building import default_config
from sim.twin import _OCC_PEAK, ZONE_IDS


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def rig():
    clk = Clock()
    store = lv.LatestStore(clock=clk)
    hw = simhw.SimHardware(ZONE_IDS, {z: 1 for z in ZONE_IDS}, "b1", seed=3, clock=clk)
    hw.enabled = True
    return clk, store, hw


def truth(temp=24.0, rh=50.0, co2=700.0, occ=10):
    return {z: {"temperature": temp, "humidity": rh, "co2": co2, "occupancy": occ} for z in ZONE_IDS}


# 13, 19, 20 registry + mapping
def test_registry_devices_and_mapping(rig):
    _, store, hw = rig
    assert len(hw.devices) == 4 * len(ZONE_IDS)
    for d in hw.devices.values():
        assert re.match(r"^SIM-(TEMP|HUM|CO2|OCC)-ZONE-[A-E]$", d.device_id) and d.simulated
        assert d.zone_id in ZONE_IDS and d.floor_id == "F1" and d.building_id == "b1"
        assert d.protocol in simhw.ADAPTERS
    reg = hw.registry(store)
    assert all(r["source_label"] == "SIMULATED HARDWARE" and r["protocol_label"].startswith("SIMULATED") for r in reg)
    assert {r["status"] for r in reg} == {"OFFLINE"}                       # enabled, nothing sampled yet
    hw.enabled = False
    assert {r["status"] for r in hw.registry(store)} == {"DISABLED"}


# 14 telemetry through the pipeline + protocol round trips
def test_sampling_publishes_through_latest_store(rig):
    clk, store, hw = rig
    assert hw.tick(store, truth(), 3600.0, _OCC_PEAK) == 20
    r = store.get("zone_a", "temperature")
    assert r["source"] == "sim" and r["origin"] == "simulated_hardware" and r["sensor_id"] == "SIM-TEMP-ZONE-A"
    assert r["device_id"] == "SIM-TEMP-ZONE-A" and r["seq"] == 1 and abs(r["value"] - 24.0) < 1.5
    assert store.get("zone_a", "occupancy_pct")["source"] == "derived"
    assert hw.tick(store, truth(), 3600.0, _OCC_PEAK) == 0                 # not due yet (5 s interval)
    clk.t += 5
    assert hw.tick(store, truth(), 3605.0, _OCC_PEAK) == 20
    assert {r["status"] for r in hw.registry(store)} == {"ONLINE"}


@pytest.mark.parametrize("proto", list(simhw.ADAPTERS))
def test_protocol_adapters_round_trip(proto):
    d = simhw.SimulatedDevice("SIM-TEMP-ZONE-A", "TEMP", "temperature", "°C", "zone_a", "F1", "b1", proto,
                              0.2, 0.1, -40, 85)
    a = simhw.ADAPTERS[proto]
    r = {"value": 24.3, "t_wall": 123.0, "seq": 9}
    out = a.decode(d, a.encode(d, r))
    assert out["value"] == pytest.approx(24.3) and out["t_wall"] == 123.0 and out["seq"] == 9 and a.simulated


# 15-16 noise, precision, bias, drift, range
def test_noise_precision_and_bias(rig):
    clk, store, hw = rig
    vals = []
    for i in range(300):
        hw.tick(store, truth(), 3600.0 + i, _OCC_PEAK, force=True, only="SIM-TEMP-ZONE-A")
        vals.append(store.get("zone_a", "temperature")["value"])
    assert abs(statistics.mean(vals) - 24.0) < 0.06 and 0.12 < statistics.pstdev(vals) < 0.3
    assert all(abs(v * 10 - round(v * 10)) < 1e-6 for v in vals)
    hw.configure("SIM-TEMP-ZONE-A", bias=1.0, noise_sd=0.0)
    hw.tick(store, truth(), 4000.0, _OCC_PEAK, force=True, only="SIM-TEMP-ZONE-A")
    assert store.get("zone_a", "temperature")["value"] == pytest.approx(25.0)
    hw.tick(store, truth(temp=150.0), 4001.0, _OCC_PEAK, force=True, only="SIM-TEMP-ZONE-A")
    r = store.get("zone_a", "temperature")
    # the sensor saturates at its 85 °C range; the pipeline's plausibility limit (70 °C) then marks it invalid
    assert r["quality"] == "invalid" and r["rejected_value"] == 85.0 and r["value"] is None


def test_drift_accumulates_with_simulated_time(rig):
    _, store, hw = rig
    hw.configure("SIM-CO2-ZONE-B", noise_sd=0.0, drift_per_hour=20.0)
    hw.tick(store, truth(), 0.0, _OCC_PEAK, force=True, only="SIM-CO2-ZONE-B")
    hw.tick(store, truth(), 5 * 3600.0, _OCC_PEAK, force=True, only="SIM-CO2-ZONE-B")
    assert store.get("zone_b", "co2")["value"] == pytest.approx(800.0)
    hw.set_fault("SIM-CO2-ZONE-B", "drift")
    hw.tick(store, truth(), 6 * 3600.0, _OCC_PEAK, force=True, only="SIM-CO2-ZONE-B")
    assert store.get("zone_b", "co2")["value"] == pytest.approx(700 + 120 + 60)


# 17-18 failures + stale
def test_offline_sensor_goes_stale_then_offline_never_zero(rig):
    clk, store, hw = rig
    hw.tick(store, truth(), 0.0, _OCC_PEAK, force=True)
    hw.set_fault("SIM-TEMP-ZONE-C", "offline")
    for i in range(1, 70):
        clk.t += 5
        hw.tick(store, truth(), 60.0 * i, _OCC_PEAK)
    r = store.get("zone_c", "temperature")
    assert r["quality"] == "stale" and r["value"] is not None and r["age_s"] == 345.0
    reg = {d["device_id"]: d for d in hw.registry(store)}
    assert reg["SIM-TEMP-ZONE-C"]["status"] == "OFFLINE" and reg["SIM-HUM-ZONE-C"]["status"] == "ONLINE"
    blk = tp.zone_block(store, "zone_c", "Cabin C", 2, ("sim", "derived"))
    assert blk["temperature"]["quality"] == "stale" and blk["comfort"]["stale_inputs"] == ["temperature"]
    a = comfort.assess(tp.comfort_reading(store, "zone_c", ("sim", "derived")), default_config("office"))
    assert "temperature" in a["data_quality"]["stale"]
    assert lv.system_status(store, ZONE_IDS)["status"] in ("DEGRADED", "STALE DATA")


def test_invalid_stuck_delay_and_dropouts(rig):
    clk, store, hw = rig
    hw.set_fault("SIM-HUM-ZONE-D", "invalid")
    hw.tick(store, truth(), 0.0, _OCC_PEAK, force=True)
    r = store.get("zone_d", "humidity")
    assert r["quality"] == "invalid" and r["value"] is None and r["rejected_value"] > 100
    assert {d["device_id"]: d for d in hw.registry(store)}["SIM-HUM-ZONE-D"]["status"] == "INVALID"
    a = comfort.assess(tp.comfort_reading(store, "zone_d", ("sim", "derived")), default_config("office"))
    assert a["humidity"]["status"] == "Unavailable"
    hw.set_fault("SIM-TEMP-ZONE-E", "stuck")
    first = store.get("zone_e", "temperature")["value"]
    hw.tick(store, truth(temp=30.0), 60.0, _OCC_PEAK, force=True, only="SIM-TEMP-ZONE-E")
    assert store.get("zone_e", "temperature")["value"] == first
    hw.set_fault("SIM-OCC-ZONE-A", "delay")
    hw.tick(store, truth(occ=17), 120.0, _OCC_PEAK, force=True, only="SIM-OCC-ZONE-A")
    assert store.get("zone_a", "occupancy")["value"] != 17 and hw.pending
    clk.t += simhw.FAULT_DELAY_S
    hw.tick(store, truth(occ=17), 240.0, _OCC_PEAK)
    r = store.get("zone_a", "occupancy")
    assert r["value"] == 17 and r["age_s"] == simhw.FAULT_DELAY_S          # timestamped at sampling, not delivery
    hw.configure("SIM-CO2-ZONE-A", failure_probability=0.5)
    for i in range(200):
        hw.tick(store, truth(), 300.0 + i, _OCC_PEAK, force=True, only="SIM-CO2-ZONE-A")
    assert 50 < hw.devices["SIM-CO2-ZONE-A"].dropped < 150


def test_fault_expiry_and_validation(rig):
    clk, store, hw = rig
    hw.set_fault("SIM-TEMP-ZONE-A", "offline", duration_s=10)
    clk.t += 11
    hw.tick(store, truth(), 0.0, _OCC_PEAK, force=True, only="SIM-TEMP-ZONE-A")
    assert hw.devices["SIM-TEMP-ZONE-A"].fault == "none" and store.get("zone_a", "temperature")
    with pytest.raises(KeyError):
        hw.set_fault("REAL-TEMP-ZONE-A", "offline")
    with pytest.raises(ValueError):
        hw.set_fault("SIM-TEMP-ZONE-A", "melted")
    with pytest.raises(ValueError):
        hw.configure("SIM-TEMP-ZONE-A", failure_probability=0.9)
    with pytest.raises(ValueError):
        hw.configure("SIM-TEMP-ZONE-A", firmware_version=2)


# ---------------------------------------------------------------- API (21-24, 27-29)
@pytest.fixture
def hw_client(fresh_client, live):
    fresh_client.post("/api/simhw", json={"enabled": False})
    fresh_client.post("/api/scenario/reset")
    live.advance(10)
    yield fresh_client
    fresh_client.post("/api/simhw", json={"enabled": False})
    fresh_client.post("/api/scenario/reset")


def test_simhw_api_enable_pipeline_and_disable(hw_client, live):
    j = hw_client.post("/api/simhw", json={"enabled": True}).json()
    assert j["enabled"] and "not physical devices" in j["notice"] and len(j["devices"]) == 20
    lat = hw_client.get("/api/latest").json()
    assert lat["telemetry_mode"].startswith("SIMULATED HARDWARE")
    for z in lat["zones"]:
        for m in ("temperature", "humidity", "occupancy"):
            assert z[m]["origin"] == "simulated_hardware" and z[m]["source"] == "sim", (z["zone_id"], m)
        assert z["temperature"]["sensor_id"].startswith("SIM-TEMP-")
        assert z["comfort"]["score"]["source"] == "derived"
    twin_direct = hw_client.get("/api/latest/sensors?source=sim").json()["sensors"]
    assert not [s for s in twin_direct if s["sensor_id"] in ("SIM-ZONE-A-TEMP", "SIM-ZONE-A-RH", "SIM-ZONE-A-OCC")]
    reg = {d["device_id"]: d for d in hw_client.get("/api/simhw").json()["devices"]}
    assert reg["SIM-TEMP-ZONE-A"]["status"] == "ONLINE" and reg["SIM-TEMP-ZONE-A"]["zone_id"] == "zone_a"
    # invalid sensor -> Unavailable, never 0; comfort names the gap
    f = hw_client.post("/api/simhw/devices/SIM-TEMP-ZONE-B/fault", json={"mode": "invalid"}).json()
    assert f["status"] == "INVALID"
    zb = hw_client.get("/api/latest/zone/zone_b").json()
    assert zb["temperature"]["value"] is None and zb["temperature"]["display"] == "Unavailable"
    assert zb["temperature"]["quality"] == "invalid" and "temperature" in zb["comfort"]["missing_inputs"]
    cz = [x for x in hw_client.get("/api/comfort").json()["zones"] if x["zone_id"] == "zone_b"][0]
    assert cz["thermal"]["status"] == "Unavailable"
    assert hw_client.get("/api/latest").json()["system_health"]["status"] == "DEGRADED"
    # offline sensor keeps its last value (aging -> stale), controller keeps running on the twin
    hw_client.post("/api/simhw/devices/SIM-CO2-ZONE-C/fault", json={"mode": "offline"})
    t0 = live.us.t
    live.advance(5)
    assert live.us.t > t0
    assert {d["device_id"]: d for d in hw_client.get("/api/simhw").json()["devices"]}["SIM-CO2-ZONE-C"]["status"] == "OFFLINE"
    assert hw_client.post("/api/simhw/devices/SIM-TEMP-ZONE-B/fault", json={"mode": "none"}).json()["status"] == "ONLINE"
    # validation + untrusted ids
    assert hw_client.post("/api/simhw/devices/ESP32-REAL/fault", json={"mode": "offline"}).status_code == 404
    assert hw_client.post("/api/simhw/devices/SIM-TEMP-ZONE-A/fault", json={"mode": "exploded"}).status_code == 400
    assert hw_client.post("/api/simhw/devices/SIM-TEMP-ZONE-A/config", json={"noise_sd": 9999}).status_code == 400
    assert hw_client.post("/api/simhw/devices/SIM-TEMP-ZONE-A/config", json={"sampling_interval_s": 2}).json()["sampling_interval_s"] == 2
    assert hw_client.post("/api/simhw/devices/SIM-TEMP-ZONE-A/config", json={"voltage": 5}).status_code == 422
    assert hw_client.post("/api/simhw", json={"noise_level": 9}).status_code == 400
    # disable: simulated devices vanish from the store, the twin publishes directly again
    hw_client.post("/api/simhw", json={"enabled": False})
    lat = hw_client.get("/api/latest").json()
    assert lat["telemetry_mode"].startswith("DIGITAL TWIN")
    assert all(z["temperature"]["sensor_id"].startswith("SIM-ZONE-") and z["temperature"]["origin"] == "twin"
               for z in lat["zones"])
    assert not [s for s in hw_client.get("/api/latest/sensors").json()["sensors"] if s["sensor_id"].startswith("SIM-TEMP")]


def test_scenario_reset_clears_sensor_faults(hw_client):
    hw_client.post("/api/simhw", json={"enabled": True})
    hw_client.post("/api/simhw/devices/SIM-TEMP-ZONE-A/fault", json={"mode": "stuck"})
    hw_client.post("/api/scenario/reset")
    reg = {d["device_id"]: d for d in hw_client.get("/api/simhw").json()["devices"]}
    assert reg["SIM-TEMP-ZONE-A"]["fault"] == "none" and hw_client.get("/api/simhw").json()["enabled"]
