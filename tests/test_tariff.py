"""ToD tariff repricing (backend/tariff.py) and the two team-approved additive
surfaces: /api/state -> meters.tou and GET /api/rl.

The tariff module is DISPLAY ONLY by decision (STATUS.md §8): these tests also
pin that the flat-tariff figures the report already publishes are untouched.
"""
from __future__ import annotations

import pytest

from backend import tariff


# --------------------------------------------------------------------------
# 1 · rate structure (TANGEDCO LT-V, TNERC Order 6/2025)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hour,expected_band,expected_rate", [
    (3.0, "night", 10.45 * 0.95),
    (5.0, "normal", 10.45),          # night ends AT 05:00
    (6.0, "peak", 10.45 * 1.25),     # morning peak starts AT 06:00
    (9.99, "peak", 10.45 * 1.25),
    (10.0, "normal", 10.45),
    (12.0, "normal", 10.45),
    (18.0, "peak", 10.45 * 1.25),
    (21.99, "peak", 10.45 * 1.25),
    (22.0, "night", 10.45 * 0.95),
    (23.5, "night", 10.45 * 0.95),
])
def test_tod_bands_and_rates(hour, expected_band, expected_rate):
    assert tariff.band(hour) == expected_band
    assert tariff.tou_rate(hour) == pytest.approx(expected_rate)


def test_hours_wrap_past_midnight():
    assert tariff.tou_rate(26.0) == tariff.tou_rate(2.0)
    assert tariff.band(30.0) == tariff.band(6.0)


# --------------------------------------------------------------------------
# 2 · repricing a cumulative series
# --------------------------------------------------------------------------

def hist(*points):
    """points: (hour_of_day, us_kwh, base_kwh) -> LiveSim.history shape."""
    return [{"t": h * 3600.0, "us": u, "base": b} for h, u, b in points]


def test_one_normal_band_kwh_bills_at_the_base_rate():
    h = hist((12.0, 0.0, 0.0), (12.25, 1.0, 2.0))
    assert tariff.tou_cost(h, "us") == pytest.approx(10.45)
    assert tariff.tou_cost(h, "base") == pytest.approx(20.90)


def test_peak_kwh_costs_25_percent_more():
    normal = tariff.tou_cost(hist((12.0, 0.0, 0), (12.25, 1.0, 0)), "us")
    peak = tariff.tou_cost(hist((7.0, 0.0, 0), (7.25, 1.0, 0)), "us")
    assert peak == pytest.approx(normal * 1.25, abs=0.01)   # tou_cost rounds to paise-ish


def test_short_and_reset_series_are_safe():
    assert tariff.tou_cost([], "us") == 0.0
    assert tariff.tou_cost(hist((12.0, 5.0, 5.0)), "us") == 0.0
    # a reset (cumulative kWh drops) contributes zero, never negative money
    h = hist((12.0, 5.0, 5.0), (12.25, 0.0, 0.0), (12.5, 1.0, 1.0))
    assert tariff.tou_cost(h, "us") == pytest.approx(10.45)


def test_summary_shape_and_consistency():
    h = hist((6.0, 0.0, 0.0), (6.5, 1.0, 2.0), (7.0, 2.0, 4.0))
    s = tariff.summary(h, now_hour=7.0)
    assert s["band_now"] == "peak" and s["rate_now_rs"] == pytest.approx(13.06, abs=0.01)
    assert s["base_rs"] > s["us_rs"] > 0
    assert s["saved_rs"] == pytest.approx(round(s["base_rs"] - s["us_rs"], 2))
    assert s["window_h"] == pytest.approx(1.0)
    assert "display only" in s["note"]


def test_empty_history_summary_is_zeroed_not_broken():
    s = tariff.summary([], now_hour=12.0)
    assert s["us_rs"] == 0.0 and s["saved_rs"] == 0.0 and s["window_h"] == 0.0


# --------------------------------------------------------------------------
# 3 · the API surfaces
# --------------------------------------------------------------------------

def test_state_meters_tou_is_additive_and_flat_figures_untouched(fresh_client, live):
    live.advance(30)                      # give the sampler something to bill
    m = fresh_client.get("/api/state").json()["meters"]
    assert "tou" in m, "the additive ToD block is missing"
    t = m["tou"]
    assert t["tariff"].startswith("TANGEDCO")
    assert t["band_now"] in ("peak", "night", "normal")
    assert t["base_rate_rs"] == 10.45
    # the published flat-tariff figure is untouched: still kwh * 9
    assert m["saved_rs"] == pytest.approx(m["saved_kwh"] * 9.0, abs=0.06)


def test_rl_endpoint_serves_the_committed_trajectory(client):
    r = client.get("/api/rl").json()
    assert r["available"] is True
    pts = r["points"]
    assert 2 <= len(pts) <= 241
    assert all(a["steps"] <= b["steps"] for a, b in zip(pts, pts[1:])), "points must be sorted"
    assert r["final"] == pts[-1]
    assert len(r["table"]) == 4, "the four measured controller rows"
    assert "ConstraintAware" in r["decision"] or "constraint" in r["decision"].lower()
    ppo = [x for x in r["table"] if "PPO" in x["name"]][0]
    assert ppo["viol_min"] == 22.0, "the honest number the decision rests on"


def test_rl_endpoint_degrades_without_the_file(client, monkeypatch, tmp_path):
    import backend.app as appmod
    monkeypatch.setattr(appmod, "RL_PROGRESS_FILE", tmp_path / "missing.csv")
    r = client.get("/api/rl").json()
    assert r["available"] is False and r["points"] == []
    assert "progress.csv" in r["note"]
