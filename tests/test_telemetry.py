"""Monitor-tab backend: telemetry store, KPIs, alerts, forecast, external, dataset.

Provenance rules under test: the store is a read-only observer (physics is
bit-identical with or without it), CO2/comfort are DERIVED and say so, the
forecast never touches the live twin, and the external feed degrades to an
honest available=false shape with no network.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("FL_EXTERNAL", "0")     # no network in the suite

from backend import telemetry                                        # noqa: E402
from backend.constraints import ConstraintStore                      # noqa: E402
from backend.external import Dataset, ExternalFeed                   # noqa: E402
from sim.controllers import ConstraintAware                          # noqa: E402
from sim.twin import BAND, DigitalTwin                               # noqa: E402


def _run(steps=120, seed=7, start_h=8.0):
    tw = DigitalTwin(seed=seed)
    tw.t = start_h * 3600.0
    st, ctrl, tel = ConstraintStore(), ConstraintAware(), telemetry.TelemetryStore()
    for _ in range(steps):
        sps, v = ctrl.act(tw, st)
        tw.step(sps, v)
        tel.record(tw, None, {}, {})
    return tw, st, tel


def test_record_is_read_only_and_deterministic():
    a, _, ta = _run(200)
    b, _, tb = _run(200)
    assert a.kwh == b.kwh and a.T == b.T             # observer never perturbs physics
    assert [r["t"] for r in ta.rows] == [r["t"] for r in tb.rows]
    assert ta.latest()["zones"]["zone_a"]["co2"] == tb.latest()["zones"]["zone_a"]["co2"]


def test_co2_estimate_rises_with_people_and_decays_when_empty():
    tw, st, tel = _run(1)
    z = "zone_a"
    tw.occ_scale = 3.0                                   # crowd it
    for _ in range(60):
        tw.step({}, {z: 0})
        tel.record(tw)
    high = tel.co2[z]
    assert high > telemetry.CO2_OUTDOOR_PPM + 50
    tw.t = 3 * 3600.0                                    # 03:00 — nobody in (clock
    for _ in range(120):                                 # jump resets the estimate)
        tw.step({}, {z: 2})
        tel.record(tw)
    assert tel.co2[z] < high and tel.co2[z] == pytest.approx(telemetry.CO2_OUTDOOR_PPM)


def test_comfort_score_bounds_and_shape():
    assert telemetry.comfort_score(24.5, 50.0) == 100.0
    assert telemetry.comfort_score(BAND[1] + 3.0, 50.0) == 0.0
    assert telemetry.comfort_score(24.5, 90.0) == 75.0         # -25 floor on humidity
    assert 0.0 <= telemetry.comfort_score(40.0, 100.0) <= 100.0


@pytest.mark.parametrize("metric,val,expect", [
    ("temp", 24.0, "normal"), ("temp", 27.0, "warning"), ("temp", 29.0, "critical"),
    ("co2", 900, "normal"), ("co2", 1200, "warning"), ("co2", 2000, "critical"),
    ("rh", None, "unknown"), ("nope", 1, "unknown"),
])
def test_status_thresholds(metric, val, expect):
    assert telemetry.status_for(metric, val) == expect


def test_series_windows_and_downsampling():
    _, _, tel = _run(400)
    s = tel.series("all", "1h")
    assert 55 <= len(s["points"]) <= 61 and s["count_raw"] == 61
    s7 = tel.series("zone_b", "7d", max_points=40)
    assert len(s7["points"]) == 40 and s7["count_raw"] == 400
    assert all("n" in p for p in s7["points"])          # bucketed rows say so
    c = tel.series("all", "custom", t_from=9 * 3600.0, t_to=9.5 * 3600.0)
    assert c["count_raw"] == 31
    with pytest.raises(KeyError):
        tel.series("zone_z", "1h")
    assert tel.series("all", "1h")["fields"]["co2"] == "derived"


def test_kpis_have_previous_and_status():
    _, _, tel = _run(40)
    k = tel.kpis("zone_a")
    assert k["prev_t"] == k["t"] - telemetry.KPI_PREV_LAG_S
    it = k["items"]
    assert {"temp", "rh", "co2", "comfort", "kwh", "hvac"} <= set(it)
    assert it["temp"]["prev"] is not None
    assert it["temp"]["status"] in ("normal", "warning", "critical", "info")
    assert it["co2"]["source"] == "derived"
    assert telemetry.TelemetryStore().kpis()["items"] == {}


def test_alerts_flag_breaches_only_when_relevant():
    _, _, tel = _run(60)
    for a in tel.alerts():
        assert a["status"] in ("warning", "critical")
        if a["metric"] == "temp":
            assert tel.latest()["zones"][a["zone"]]["occ"] > 0
        if a["metric"] == "rh":
            assert "limitation" in a["note"]


def test_forecast_is_isolated_and_predicted():
    tw, st, tel = _run(30)
    fp = (tw.t, tw.kwh, dict(tw.T))
    out = telemetry.forecast(tw, st, ConstraintAware, horizon_h=1.0, zone="zone_c",
                             max_points=20, co2_init=tel.co2)
    assert (tw.t, tw.kwh, dict(tw.T)) == fp               # live twin untouched
    assert out["kind"] == "predicted" and out["t_from"] > tw.t
    assert out["t_to"] == pytest.approx(tw.t + 3600.0)
    assert 15 <= len(out["points"]) <= 25 and out["points"][-1]["kwh"] > 0


def test_external_disabled_and_offline_shapes():
    f = ExternalFeed(enabled=False)
    s = f.snapshot()
    assert s["available"] is False and s["source"] == "real" and "disabled" in s["error"]
    f2 = ExternalFeed(enabled=True)
    f2.refresh = lambda: False                            # no network: never raises
    assert f2.snapshot()["current"] is None


def test_dataset_loads_and_windows():
    d = Dataset()
    if not d.available:
        pytest.skip("data/uci_occupancy.csv not present")
    assert d.info()["rows"] > 20000 and d.info()["source"] == "historical"
    w = d.window("6h", max_points=50)
    assert len(w["points"]) == 50 and w["count_raw"] == 360
    assert set(w["points"][0]) >= {"time", "temp_c", "co2_ppm", "light_lux", "occupied"}
    w2 = d.window("custom", start="2015-02-05 00:00:00", end="2015-02-05 01:00:00",
                  max_points=500)
    assert 55 <= w2["count_raw"] <= 61


# ---- HTTP layer -------------------------------------------------------
def test_monitor_endpoints_via_api(client):
    r = client.get("/api/telemetry?zone=all&window=1h&max_points=30")
    assert r.status_code == 200 and r.json()["co2_estimated"] is True
    assert client.get("/api/telemetry?zone=bad").status_code == 400
    assert client.get("/api/telemetry?window=bad").status_code == 400
    m = client.get("/api/monitor?zone=zone_b").json()
    assert set(m) >= {"kpis", "alerts", "thresholds", "hardware", "external", "sources"}
    assert m["hardware"]["zone"] == "zone_b"
    fc = client.get("/api/forecast?zone=all&horizon_h=0.5&max_points=10").json()
    assert fc["kind"] == "predicted"
    assert client.get("/api/forecast?horizon_h=30").status_code == 400
    ext = client.get("/api/external").json()
    assert ext["source"] == "real" and "available" in ext
    ds = client.get("/api/dataset?window=1h&max_points=10").json()
    assert ds["source"] == "historical" and "info" in ds
    st = client.get("/api/state").json()
    assert "monitor" in st and "rows" in st["monitor"]
