"""Hardware-in-the-loop bridge + the REAL adapters (backend/hardware.py).

The one-line proof this suite exists to give: HttpSensorAdapter and
HttpHVACAdapter pass the exact same assert_conforms() gate as the Sim adapters
— hardware is a first-class citizen of the seam, not a bolt-on.

Everything here runs with NO physical node: a test posting to /api/hw/reading
IS the firmware's poll (same payload, same reply contract), which is exactly
the A5(b) fallback story — the server side is fully exercised against a mock
node before a single wire is connected.

Wall-clock note: the bridge lives on time.time(), not sim time. Tests that
need staleness or duty-cycle maths drive the `now` parameter explicitly; no
test sleeps.
"""
from __future__ import annotations

import pytest

from backend.adapters import (HVACAdapter, NotificationAdapter, OccupancyAdapter,
                              SensorAdapter, WeatherAdapter, assert_conforms,
                              get_adapters)
from backend.hardware import (HEATER_MAX_DUTY, HEATER_WINDOW_S, HW_ZONE, STALE_S,
                              WATCHDOG_S, HardwareBridge, HttpHVACAdapter,
                              HttpSensorAdapter)


def reading(seq=1, temp=26.5, rh=55.0, node="shoebox-1", **extra):
    out = {"node_id": node, "temp_c": temp, "rh_pct": rh,
           "seq": seq, "uptime_s": 4.2}
    out.update(extra)
    return out


@pytest.fixture
def bridge():
    return HardwareBridge()


@pytest.fixture
def hw_fresh(live):
    """Fresh bridge + adapters swapped onto the live sim, so API tests are
    order-independent (the module-level bridge deliberately survives /api/reset
    — it is physical state — which would otherwise leak between tests)."""
    live.hw_bridge = HardwareBridge()
    live.hw_sensor = HttpSensorAdapter(live.hw_bridge)
    live.hw_hvac = HttpHVACAdapter(live.hw_bridge)
    return live.hw_bridge


# --------------------------------------------------------------------------
# 1 · conformance — the same gate for Sim and Http implementations
# --------------------------------------------------------------------------

def test_http_adapters_pass_the_same_conformance_gate_as_sim(bridge):
    assert assert_conforms(HttpSensorAdapter(bridge), SensorAdapter)
    assert assert_conforms(HttpHVACAdapter(bridge), HVACAdapter)


def test_sim_adapters_pass_the_conformance_gate_too(twin):
    """assert_conforms existed but nothing ran it; now every implementation of
    the seam — simulated and real — goes through one gate in one test file."""
    ad = get_adapters(twin)
    assert assert_conforms(ad["hvac"], HVACAdapter)
    assert assert_conforms(ad["sensor"], SensorAdapter)
    assert assert_conforms(ad["occupancy"], OccupancyAdapter)
    assert assert_conforms(ad["weather"], WeatherAdapter)
    assert assert_conforms(ad["notify"], NotificationAdapter)


# --------------------------------------------------------------------------
# 2 · the bridge: validation, staleness, commands
# --------------------------------------------------------------------------

def test_post_reading_returns_the_frozen_command_contract(bridge):
    out = bridge.post_reading(reading(), now=1000.0)
    assert out["ok"] is True
    assert out["fan"] in (0, 1, 2)
    assert isinstance(out["heater"], bool)
    assert out["watchdog_s"] == WATCHDOG_S
    assert out["poll_s"] > 0


@pytest.mark.parametrize("bad,field", [
    ({"node_id": ""}, "node_id"),
    ({"temp_c": "warm"}, "temp_c"),
    ({"temp_c": 400.0}, "temp_c"),
    ({"rh_pct": 130.0}, "rh_pct"),
    ({"seq": "seven"}, "seq"),
])
def test_post_reading_rejects_implausible_payloads_naming_the_field(bridge, bad, field):
    with pytest.raises(ValueError, match=field):
        bridge.post_reading(reading(**bad), now=1000.0)


def test_extra_payload_fields_are_tolerated(bridge):
    out = bridge.post_reading(reading(battery_v=3.9, rssi=-61), now=1000.0)
    assert out["ok"] is True, "a firmware field the server does not know must not break the poll"


def test_staleness_is_wall_clock_and_connected_flips(bridge):
    assert bridge.stale_s(now=50.0) is None, "no reading yet must read as None, not 0"
    assert bridge.connected(now=50.0) is False
    bridge.post_reading(reading(), now=100.0)
    assert bridge.connected(now=100.0 + STALE_S - 1) is True
    assert bridge.connected(now=100.0 + STALE_S + 1) is False


def test_fan_command_round_trips_through_the_poll_reply(bridge):
    bridge.post_reading(reading(seq=1), now=100.0)
    assert bridge.set_fan(2) is True
    assert bridge.post_reading(reading(seq=2), now=102.0)["fan"] == 2


def test_fan_rejects_out_of_envelope_levels(bridge):
    assert bridge.set_fan(3) is False
    assert bridge.set_fan("high") is False
    assert bridge.fan == 0
    reasons = [w["reason"] for w in bridge.writes if not w["ok"]]
    assert reasons, "a rejected write must be audited with a reason"


# --------------------------------------------------------------------------
# 3 · heater safety envelope (layer 2)
# --------------------------------------------------------------------------

def test_heater_duty_cap_forces_off_at_the_cap_and_recovers(bridge):
    on_limit = HEATER_WINDOW_S * HEATER_MAX_DUTY          # 300 s of a 600 s window
    r = bridge.set_heater(True, now=0.0)
    assert r["heater"] is True and r["duty_limited"] is False

    # still within budget just before the cap
    bridge.post_reading(reading(seq=1), now=on_limit - 1)
    assert bridge.heater is True

    # at the cap: forced off even though still requested
    bridge.post_reading(reading(seq=2), now=on_limit + 1)
    assert bridge.heater is False
    assert bridge.duty_limited is True
    assert bridge.heater_requested is True, "the cap must not silently drop the request"

    # The on-period (0..~300 s) only starts leaving the trailing window once
    # now > HEATER_WINDOW_S, so recovery lands just past the window edge —
    # not merely after some off-time has passed.
    bridge.post_reading(reading(seq=3), now=HEATER_WINDOW_S - 5.0)
    assert bridge.heater is False, "recovered too early: the full on-period is still in the window"
    bridge.post_reading(reading(seq=4), now=HEATER_WINDOW_S + 30.0)
    assert bridge.heater is True, "duty budget recovered but the heater stayed off"


def test_heater_off_request_wins_immediately(bridge):
    bridge.set_heater(True, now=0.0)
    r = bridge.set_heater(False, now=10.0)
    assert r["heater"] is False and r["requested"] is False


# --------------------------------------------------------------------------
# 4 · the real adapters against the bridge
# --------------------------------------------------------------------------

def test_sensor_adapter_reads_the_bound_zone_and_only_it(bridge):
    bridge.post_reading(reading(temp=27.3, rh=61.0), now=None)   # fresh wall-now
    s = HttpSensorAdapter(bridge)
    out = s.read(HW_ZONE)
    assert out["temp_c"] == 27.3 and out["rh_pct"] == 61.0
    assert out["co2_ppm"] is None and out["co2_estimated"] is False
    assert out["source"] == "esp32-http"
    with pytest.raises(KeyError):
        s.read("zone_a")
    with pytest.raises(KeyError):
        s.health("zone_a")


def test_sensor_health_no_data_then_ok_then_stale(bridge):
    import time as _time
    s = HttpSensorAdapter(bridge)
    h = s.health(HW_ZONE)
    assert h["ok"] is False and h["faults"] == ["no_data"]

    bridge.post_reading(reading(seq=1, temp=26.0), now=_time.time())
    bridge.post_reading(reading(seq=2, temp=26.1), now=_time.time())
    assert s.health(HW_ZONE)["ok"] is True

    stale_bridge = HardwareBridge()
    stale_bridge.post_reading(reading(), now=_time.time() - 100.0)
    assert "stale" in HttpSensorAdapter(stale_bridge).health(HW_ZONE)["faults"]


def test_sensor_health_infers_stuck_from_bit_identical_history(bridge):
    import time as _time
    now = _time.time()
    for i in range(16):
        bridge.post_reading(reading(seq=i, temp=25.0), now=now - (16 - i) * 0.1)
    assert "stuck" in HttpSensorAdapter(bridge).health(HW_ZONE)["faults"]


def test_hvac_adapter_is_honestly_vent_only(bridge):
    h = HttpHVACAdapter(bridge)
    caps = h.capabilities()
    assert caps["supports_setpoint"] is False
    assert caps["protocol"] == "http-esp32"
    assert caps["vent_levels"] == [0, 1, 2]

    assert h.write_setpoint(HW_ZONE, 24.0) is False, \
        "a rig with no cooling plant must reject setpoint writes, not fake them"
    assert any("vent-only" in w["reason"] for w in h.writes), \
        "the rejection must carry its reason in the audit log"

    assert h.write_vent(HW_ZONE, 1) is True
    assert bridge.fan == 1
    st = h.read_state(HW_ZONE)
    assert st["mode"] == "vent-only" and st["capacity_w"] == 0.0
    with pytest.raises(KeyError):
        h.read_state("zone_c")


# --------------------------------------------------------------------------
# 5 · the HTTP surface
# --------------------------------------------------------------------------

def test_reading_endpoint_round_trip_and_status(fresh_client, hw_fresh):
    r = fresh_client.post("/api/hw/reading", json=reading())
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ok", "fan", "heater", "watchdog_s", "poll_s"}

    st = fresh_client.get("/api/hw/status").json()
    assert st["connected"] is True and st["node_id"] == "shoebox-1"
    assert st["zone"] == HW_ZONE
    assert st["sensor_health"]["reads"] == 1
    assert st["hvac_capabilities"]["supports_setpoint"] is False


def test_reading_endpoint_rejects_bad_payloads(fresh_client, hw_fresh):
    assert fresh_client.post("/api/hw/reading", json={"node_id": "x"}).status_code == 422
    r = fresh_client.post("/api/hw/reading", json=reading(temp=400.0))
    assert r.status_code == 422 and "temp_c" in r.json()["detail"]


def test_state_carries_the_hardware_block(fresh_client, hw_fresh):
    hw = fresh_client.get("/api/state").json()["hardware"]
    assert hw["connected"] is False and hw["reading"] is None, \
        "no node has posted; the block must be empty-but-valid, not absent"
    fresh_client.post("/api/hw/reading", json=reading())
    assert fresh_client.get("/api/state").json()["hardware"]["connected"] is True


def test_heater_endpoint_reports_the_envelope(fresh_client, hw_fresh):
    body = fresh_client.post("/api/hw/heater", json={"on": True}).json()
    assert body["ok"] is True and body["requested"] is True
    assert fresh_client.post("/api/hw/heater", json={"on": False}).json()["heater"] is False
    assert fresh_client.post("/api/hw/heater", json={"onn": True}).status_code == 422


def test_log_endpoint_serves_the_calibration_rows(fresh_client, hw_fresh):
    for i in range(5):
        fresh_client.post("/api/hw/reading", json=reading(seq=i, temp=25.0 + i))
    out = fresh_client.get("/api/hw/log?limit=3").json()
    assert out["count"] == 3
    assert [r["seq"] for r in out["rows"]] == [2, 3, 4], "rows must be oldest-first, newest kept"
    assert fresh_client.get("/api/hw/log?limit=0").status_code == 400


def test_bridge_survives_a_simulation_reset(fresh_client, hw_fresh):
    fresh_client.post("/api/hw/reading", json=reading())
    fresh_client.post("/api/reset")
    st = fresh_client.get("/api/hw/status").json()
    assert st["connected"] is True and st["polls"] == 1, \
        "a sim reset zeroed the physical rig's state — the rig is not sim state"


# --------------------------------------------------------------------------
# 6 · the demo moment, end to end, with a mock node
# --------------------------------------------------------------------------

def test_stuffy_complaint_reaches_the_physical_fan(fresh_client, hw_fresh, live):
    """The finals demo moment as a regression: complaint -> parser -> constraint
    -> ConstraintAware -> HttpHVACAdapter -> the fan command in the node's next
    poll reply. Same controller, same seam, no special case."""
    # node comes alive; let the controller run one step with no constraints
    fresh_client.post("/api/hw/reading", json=reading(seq=1))
    live.advance(2)
    fan_before = fresh_client.post("/api/hw/reading", json=reading(seq=2)).json()["fan"]
    assert fan_before == 0, "empty conference room at 08:00 should not be ventilating"

    body = fresh_client.post("/api/complaint", json={
        "text": "it's really stuffy in conference room b"}).json()
    assert body["parsed"]["zone_id"] == HW_ZONE

    live.advance(2)                       # controller acts; _tick_hardware forwards
    fan_after = fresh_client.post("/api/hw/reading", json=reading(seq=3)).json()["fan"]
    assert fan_after >= 1, (
        "a stuffy complaint on the hardware zone must raise the physical fan; "
        f"got {fan_after}")
