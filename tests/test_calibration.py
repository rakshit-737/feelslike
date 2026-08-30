"""The sim-to-real calibration fitter (scripts/fit_rc.py, Workstream A3).

The rig does not exist yet, so every test here runs against SYNTHETIC step
responses with known ground truth — which is exactly the claim the team makes
on stage: the tool is built and tested, awaiting hardware data. Nothing in
this file may ever be presented as a hardware result.
"""
from __future__ import annotations

import json

import pytest

from scripts.fit_rc import (NOISE_DHT22, NOISE_SHT31, TRUE_C, TRUE_P, TRUE_R,
                            fit, load_rows, synthesize)


def test_fit_recovers_truth_at_sht31_noise():
    res = fit(synthesize(NOISE_SHT31), TRUE_P, None, "selftest", "synthetic")
    assert abs(res["fit"]["R_K_per_W"] - TRUE_R) / TRUE_R < 0.05
    assert abs(res["fit"]["C_J_per_K"] - TRUE_C) / TRUE_C < 0.05
    assert res["fit"]["rmse_c"] < 3 * NOISE_SHT31


def test_fit_recovers_truth_at_dht22_noise():
    """DHT22-grade noise (0.35 degC RMS) is the worst sensor the team would
    actually use; the fit must still land within 15%."""
    res = fit(synthesize(NOISE_DHT22), TRUE_P, None, "selftest", "synthetic")
    assert abs(res["fit"]["R_K_per_W"] - TRUE_R) / TRUE_R < 0.15
    assert abs(res["fit"]["C_J_per_K"] - TRUE_C) / TRUE_C < 0.15


def test_fit_is_deterministic():
    a = fit(synthesize(NOISE_SHT31), TRUE_P, None, "selftest", "synthetic")
    b = fit(synthesize(NOISE_SHT31), TRUE_P, None, "selftest", "synthetic")
    assert a["fit"] == b["fit"], "same synthetic log must produce the same fit"


def test_fan_on_rows_are_excluded_not_blended():
    rows = synthesize(NOISE_SHT31)
    for r in rows[:100]:
        r["fan"] = 2
    res = fit(rows, TRUE_P, None, "selftest", "synthetic")
    assert res["excluded_fan_on_rows"] == 100, \
        "fan-on rows violate the fan-off calibration protocol and must be dropped"


def test_result_carries_the_honesty_flag_and_chartable_series():
    res = fit(synthesize(NOISE_SHT31), TRUE_P, None, "selftest", "synthetic")
    assert res["kind"] == "selftest"
    assert "NOT hardware data" in res["note"]
    s = res["series"]
    assert len(s["t_s"]) == len(s["measured_c"]) == len(s["simulated_c"]) > 50
    json.dumps(res)                                     # json-safe end to end


def test_a_log_with_no_heater_step_is_refused():
    rows = synthesize(NOISE_SHT31)
    for r in rows:
        r["heater"] = False
    with pytest.raises(ValueError, match="heater is never on"):
        fit(rows, TRUE_P, None, "selftest", "synthetic")


def test_no_baseline_requires_explicit_ambient():
    rows = [r for r in synthesize(NOISE_SHT31) if r["t"] >= 300.0]  # cut the pre-step
    with pytest.raises(ValueError, match="ambient"):
        fit(rows, TRUE_P, None, "selftest", "synthetic")
    res = fit(rows, TRUE_P, 26.0, "selftest", "synthetic")          # explicit works
    assert abs(res["fit"]["R_K_per_W"] - TRUE_R) / TRUE_R < 0.1


def test_load_rows_reads_the_hw_log_shape(tmp_path):
    """The GET /api/hw/log response round-trips through the loader."""
    rows = [{"t_wall": 1000.0 + i * 2.0, "temp_c": 26.0 + 0.01 * i, "rh_pct": 50.0,
             "seq": i, "uptime_s": i * 2.0, "fan": 0, "heater": i > 10}
            for i in range(40)]
    p = tmp_path / "log.json"
    p.write_text(json.dumps({"zone": "zone_b", "rows": rows, "count": len(rows)}))
    out = load_rows(p)
    assert len(out) == 40
    assert out[0]["t"] == 0.0, "timestamps must be rebased to start at zero"
    assert out[11]["heater"] is True


def test_load_rows_refuses_a_log_without_heater_state(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"rows": [{"t_wall": 1.0, "temp_c": 25.0}]}))
    with pytest.raises(ValueError, match="heater"):
        load_rows(p)


def test_end_to_end_bridge_log_fits_under_the_real_duty_cap(tmp_path):
    """The whole hardware-day pipeline, minus the wires: a simulated box whose
    heater is driven by the BRIDGE'S OWN commands (so the 50%-per-10-min duty
    cap chops the step into the bang-bang waveform real hardware will see),
    logged through HardwareBridge, saved in the GET /api/hw/log shape, loaded
    by the fitter — which must still recover the box's true R and C, because
    it fits against the RECORDED heater waveform, not an assumed clean step."""
    from backend.hardware import HardwareBridge

    R, C, P, AMB = 1.5, 400.0, 10.0, 26.0
    bridge = HardwareBridge()
    rng_phase = {"applied": False}
    T, now, dt = AMB, 1000.0, 2.0
    bridge.set_heater(False, now=now)
    total_s = (5 + 30 + 30) * 60.0
    i = 0
    while i * dt < total_s:
        t_min = i * dt / 60.0
        if abs(t_min - 5.0) < 1e-9:
            bridge.set_heater(True, now=now)      # operator asks; envelope decides
        if abs(t_min - 35.0) < 1e-9:
            bridge.set_heater(False, now=now)
        # physics uses what the NODE applied on the PREVIOUS good reply
        q = (AMB - T) / R + P * (1.0 if rng_phase["applied"] else 0.0)
        T += dt * q / C
        out = bridge.post_reading({"node_id": "e2e", "temp_c": round(T, 3),
                                   "rh_pct": 50.0, "seq": i, "uptime_s": i * dt},
                                  now=now)
        rng_phase["applied"] = out["heater"]
        now += dt
        i += 1

    rows = bridge.log_rows()
    assert any(r["heater"] for r in rows), "the envelope never allowed the heater on"
    assert not all(r["heater"] for r in rows[150:1050]), \
        "the duty cap never chopped a 30-min continuous request — envelope broken"

    p = tmp_path / "hw_log.json"
    p.write_text(json.dumps({"zone": "zone_b", "rows": rows, "count": len(rows)}))
    res = fit(load_rows(p), P, None, "selftest", "e2e-bridge")
    assert abs(res["fit"]["R_K_per_W"] - R) / R < 0.10, res["fit"]
    assert abs(res["fit"]["C_J_per_K"] - C) / C < 0.10, res["fit"]
