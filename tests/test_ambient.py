"""Sensor-only nodes: the Uno ambient reference.

Covers backend.hardware.SensorNodeStore, the /api/hw/sensor* endpoints,
scripts/serial_bridge.parse_line and scripts/crosscal_ambient.

The property that matters most is isolation: a sensor-only node can never
receive an actuator command or overwrite the shoebox rig's reading. Everything
runs with no Uno attached - posting to /api/hw/sensor IS what the serial bridge
does. No test sleeps; wall-clock maths drive `now` explicitly.
"""
from __future__ import annotations

import pytest

from backend.hardware import (SENSOR_NODE_MAX, STALE_S, STUCK_WINDOW, HardwareBridge,
                              HttpHVACAdapter, HttpSensorAdapter, SensorNodeStore)
from scripts.crosscal_ambient import MIN_PAIRS, pair_readings, suggest_vref
from scripts.serial_bridge import TRANSPORT, parse_line


def amb(temp=29.4, node="uno-ambient", seq=1, **extra):
    out = {"node_id": node, "temp_c": temp, "counts": 273.4, "seq": seq,
           "uptime_s": 2.0, "role": "ambient", "transport": "usb-serial"}
    out.update(extra)
    return out


@pytest.fixture
def nodes():
    return SensorNodeStore()


@pytest.fixture
def rig_and_sensors(live):
    """Fresh rig bridge + adapters + sensor store on the live sim, so API tests
    are order-independent (both deliberately survive /api/reset)."""
    live.hw_bridge = HardwareBridge()
    live.hw_sensor = HttpSensorAdapter(live.hw_bridge)
    live.hw_hvac = HttpHVACAdapter(live.hw_bridge)
    live.hw_sensors = SensorNodeStore()
    return live


# --------------------------------------------------------------------------
# 1 - the store
# --------------------------------------------------------------------------

def test_a_reading_is_acknowledged_with_no_command_in_the_reply(nodes):
    r = nodes.post(amb(), now=100.0)
    assert r == {"ok": True, "node_id": "uno-ambient", "accepted": True}
    assert "fan" not in r and "heater" not in r, "a sensor node must never be handed a command"
    st = nodes.status(now=101.0)
    n = st["nodes"][0]
    assert n["connected"] and n["reading"]["temp_c"] == 29.4 and n["transport"] == "usb-serial"
    assert st["ambient"]["temp_c"] == 29.4


@pytest.mark.parametrize("bad,field", [
    ({"node_id": ""}, "node_id"),
    ({"node_id": "x" * 65}, "node_id"),
    ({"temp_c": None}, "temp_c"),
    ({"temp_c": "warm"}, "temp_c"),
    ({"temp_c": 400.0}, "temp_c"),
    ({"counts": "many"}, "counts"),
    ({"seq": "seven"}, "seq"),
])
def test_implausible_payloads_are_rejected_naming_the_field(nodes, bad, field):
    with pytest.raises(ValueError, match=field):
        nodes.post(amb(**bad), now=1.0)


def test_a_fault_is_recorded_and_never_turned_into_a_number(nodes):
    r = nodes.post(amb(temp=None, fault="sensor_open_or_short", counts=0.0), now=10.0)
    assert r["accepted"] is False
    st = nodes.status(now=11.0)
    n = st["nodes"][0]
    assert n["reading"]["temp_c"] is None
    assert "sensor_fault" in n["health"]["faults"]
    assert n["faults_reported"] == 1
    assert st["ambient"] is None, "a fault must not be served as the ambient temperature"


def test_staleness_withdraws_the_ambient_reference(nodes):
    nodes.post(amb(), now=0.0)
    assert nodes.status(now=STALE_S - 1)["ambient"] is not None
    late = nodes.status(now=STALE_S + 1)
    assert late["ambient"] is None
    assert "stale" in late["nodes"][0]["health"]["faults"]


def test_stuck_is_inferred_from_bit_identical_history(nodes):
    for i in range(STUCK_WINDOW):
        nodes.post(amb(temp=25.0, seq=i), now=float(i))
    assert "stuck" in nodes.status(now=STUCK_WINDOW)["nodes"][0]["health"]["faults"]

    moving = SensorNodeStore()
    for i in range(STUCK_WINDOW):
        moving.post(amb(temp=25.0 + 0.1 * i, seq=i), now=float(i))
    assert "stuck" not in moving.status(now=STUCK_WINDOW)["nodes"][0]["health"]["faults"]


def test_nodes_are_tracked_separately_and_the_count_is_bounded(nodes):
    ids = [f"node-{i}" for i in range(SENSOR_NODE_MAX + 2)]
    for i, nid in enumerate(ids):
        nodes.post(amb(node=nid), now=float(i))
    kept = [n["node_id"] for n in nodes.status(now=100.0)["nodes"]]
    assert len(kept) == SENSOR_NODE_MAX
    assert "node-0" not in kept and "node-1" not in kept, "least recently seen must be evicted"
    assert ids[-1] in kept


def test_log_is_oldest_first_and_an_unknown_node_raises(nodes):
    for i in range(5):
        nodes.post(amb(seq=i), now=float(i))
    assert [r["seq"] for r in nodes.log_rows("uno-ambient", 3)] == [2, 3, 4]
    with pytest.raises(KeyError):
        nodes.log_rows("nope")


def test_the_calibration_flag_is_carried_and_defaults_to_uncalibrated(nodes):
    nodes.post(amb(), now=1.0)
    assert nodes.status(now=2.0)["ambient"]["calibrated"] is False
    nodes.post(amb(seq=2, calibrated="true"), now=3.0)
    assert nodes.status(now=4.0)["ambient"]["calibrated"] is False, \
        "only a real boolean true may mark a reading calibrated"
    nodes.post(amb(seq=3, calibrated=True), now=5.0)
    assert nodes.status(now=6.0)["ambient"]["calibrated"] is True


# --------------------------------------------------------------------------
# 2 - the HTTP surface, and isolation from the rig
# --------------------------------------------------------------------------

def test_sensor_endpoint_round_trip(fresh_client, rig_and_sensors):
    r = fresh_client.post("/api/hw/sensor", json=amb())
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert fresh_client.get("/api/hw/sensors").json()["ambient"]["temp_c"] == 29.4


def test_sensor_endpoint_rejects_bad_payloads(fresh_client, rig_and_sensors):
    assert fresh_client.post("/api/hw/sensor", json={"node_id": "u"}).status_code == 422
    r = fresh_client.post("/api/hw/sensor", json=amb(temp=400.0))
    assert r.status_code == 422 and "temp_c" in r.json()["detail"]


def test_sensor_log_endpoint(fresh_client, rig_and_sensors):
    for i in range(3):
        fresh_client.post("/api/hw/sensor", json=amb(seq=i))
    assert fresh_client.get("/api/hw/sensors/uno-ambient/log?limit=2").json()["count"] == 2
    assert fresh_client.get("/api/hw/sensors/nope/log").status_code == 404
    assert fresh_client.get("/api/hw/sensors/uno-ambient/log?limit=0").status_code == 400


def test_a_sensor_node_can_never_touch_the_rig(fresh_client, rig_and_sensors):
    fresh_client.post("/api/hw/sensor", json=amb())
    st = fresh_client.get("/api/hw/status").json()
    assert st["connected"] is False and st["polls"] == 0 and st["node_id"] is None, \
        "a sensor-only post leaked into the actuator bridge"
    assert st["ambient"]["temp_c"] == 29.4

    fresh_client.post("/api/hw/reading", json={"node_id": "shoebox-1", "temp_c": 31.0,
                                               "rh_pct": 80.0, "seq": 1, "uptime_s": 2.0})
    fresh_client.post("/api/hw/sensor", json=amb(temp=28.0, seq=2))
    st = fresh_client.get("/api/hw/status").json()
    assert st["node_id"] == "shoebox-1" and st["reading"]["temp_c"] == 31.0, \
        "the ambient node overwrote the rig's reading"
    assert st["ambient"]["temp_c"] == 28.0


def test_state_hardware_block_carries_ambient_additively(fresh_client, rig_and_sensors):
    hw = fresh_client.get("/api/state").json()["hardware"]
    assert hw["ambient"] is None and "connected" in hw and "reading" in hw
    fresh_client.post("/api/hw/sensor", json=amb(temp=27.5))
    assert fresh_client.get("/api/state").json()["hardware"]["ambient"]["temp_c"] == 27.5


def test_monitor_hardware_block_carries_ambient(fresh_client, rig_and_sensors):
    assert fresh_client.get("/api/monitor").json()["hardware"]["ambient"] is None
    fresh_client.post("/api/hw/sensor", json=amb(temp=26.5))
    got = fresh_client.get("/api/monitor").json()["hardware"]["ambient"]
    assert got["temp_c"] == 26.5 and got["calibrated"] is False


# --------------------------------------------------------------------------
# 3 - the serial bridge's line parser (no pyserial needed)
# --------------------------------------------------------------------------

def test_parse_line_accepts_a_node_line_and_tags_the_transport():
    p = parse_line(b'{"node_id":"uno-ambient","temp_c":30.52,"counts":284.1,"seq":12,"uptime_s":24.0}\r\n')
    assert p["temp_c"] == 30.52 and p["transport"] == TRANSPORT and p["role"] == "ambient"


@pytest.mark.parametrize("raw", [b"", b"   \r\n", b"# ambient_node_uno: booting",
                                 b"garbage{", b"[1, 2]", b'{"temp_c": 30.0}',
                                 b'{"node_id": ""}'])
def test_parse_line_skips_what_it_cannot_use(raw):
    assert parse_line(raw) is None


def test_parse_line_passes_a_fault_through_and_keeps_an_explicit_role():
    p = parse_line('{"node_id":"uno-ambient","fault":"sensor_open_or_short","counts":0.0,"role":"probe"}')
    assert p["fault"] == "sensor_open_or_short" and p["role"] == "probe"


# --------------------------------------------------------------------------
# 4 - cross-calibration maths
# --------------------------------------------------------------------------

def _logs(n=40, gain=1.04, uno_shift_s=0.5):
    dht = [{"t_wall": 2.0 * i, "temp_c": 30.0 + 0.01 * i} for i in range(n)]
    uno = [{"t_wall": 2.0 * i + uno_shift_s, "temp_c": (30.0 + 0.01 * i) / gain} for i in range(n)]
    return dht, uno


def test_suggest_vref_recovers_a_pure_gain_error():
    dht, uno = _logs(gain=1.04)
    res = suggest_vref(pair_readings(dht, uno), vref_now=1.1)
    assert res["gain"] == pytest.approx(1.04, abs=1e-4)
    assert res["vref_suggested_v"] == pytest.approx(1.144, abs=1e-3)
    assert res["residual_rmse_c"] == pytest.approx(0.0, abs=1e-3)
    assert res["vref_uncertainty_v"] > 0, "the DHT22's own accuracy must be reported"


def test_pairing_respects_the_gap_and_skips_faults():
    dht, uno = _logs(n=5)
    uno.append({"t_wall": 500.0, "temp_c": 29.0})                 # nothing within 3 s
    uno.append({"t_wall": 4.2, "temp_c": None, "fault": "open"})  # a fault, not a number
    assert len(pair_readings(dht, uno, max_gap_s=3.0)) == 5


def test_too_few_pairs_refuses_to_guess():
    dht, uno = _logs(n=MIN_PAIRS - 1)
    with pytest.raises(ValueError, match="paired readings"):
        suggest_vref(pair_readings(dht, uno))
