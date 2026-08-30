"""Time-of-day tariff repricing — DISPLAY ONLY, by decision.

WHAT THIS IS. The verified TANGEDCO LT-V commercial tariff (TNERC Tariff Order
No. 6 of 2025, effective 2025-07-01; sources and access dates in
docs/FEASIBILITY.md §6) applied to the SAME measured kWh series the dashboard
already shows. It answers the brief's "pricing data" ask honestly: the energy
is measured, the repricing is verified, and the label says the controller does
NOT exploit time-of-day yet — control still optimizes kWh against the flat
tariff, so the frozen headline numbers and every cost_rs figure derived from
sim.twin.TARIFF are untouched.

THE TARIFF (LT-V, FY 2025-26):
  energy charge   ₹6.65/kWh first 100 units/month, ₹10.45 above — every office
                  crosses 100 units, so the MARGINAL rate ₹10.45 is used here
  ToD adders      peak   06:00-10:00 and 18:00-22:00  -> +25%
                  night  22:00-05:00                  -> -5%
                  normal everything else              -> +0%
  not included    5% TN electricity tax, fixed ₹/kW charges (see FEASIBILITY)

WHY DISPLAY ONLY. Exploiting ToD (shifting pre-cool off-peak) would change
control behaviour and therefore kWh — an unverified optimization this project
refuses to ship. The landing place for the real thing is documented: the cost
row of OBJECTIVE_WEIGHTS plus this module's rate curve replacing the flat
TARIFF constant. Decision logged in STATUS.md §8.

Stdlib only. Deterministic. No I/O.
"""
from __future__ import annotations

TARIFF_NAME = "TANGEDCO LT-V (TNERC Order 6/2025)"
BASE_RATE_RS = 10.45          # marginal ₹/kWh above 100 units/month
PEAK_MULT = 1.25              # 06-10 and 18-22
NIGHT_MULT = 0.95             # 22-05
PEAK_WINDOWS = ((6.0, 10.0), (18.0, 22.0))
NIGHT_WINDOWS = ((22.0, 24.0), (0.0, 5.0))
NOTE = ("Same measured kWh, repriced at the verified ToD tariff — display only; "
        "control optimizes against the flat tariff and does not exploit "
        "time-of-day yet.")


def tou_rate(hour: float, base: float = BASE_RATE_RS) -> float:
    """₹/kWh in force at a wall-clock hour of day.

    INPUT: hour 0..24 (fractional fine; values outside are wrapped), base rate.
    OUTPUT: float ₹/kWh. SIDE EFFECTS: none. ERROR STATES: none.
    """
    h = float(hour) % 24.0
    if any(a <= h < b for a, b in PEAK_WINDOWS):
        return base * PEAK_MULT
    if any(a <= h < b for a, b in NIGHT_WINDOWS):
        return base * NIGHT_MULT
    return base


def band(hour: float) -> str:
    """'peak' | 'night' | 'normal' for a wall-clock hour — UI label."""
    h = float(hour) % 24.0
    if any(a <= h < b for a, b in PEAK_WINDOWS):
        return "peak"
    if any(a <= h < b for a, b in NIGHT_WINDOWS):
        return "night"
    return "normal"


def tou_cost(samples: list, key: str) -> float:
    """Reprice one cumulative-kWh series at the ToD tariff.

    INPUT: samples — the LiveSim.history shape, oldest first:
      [{"t": sim-seconds, "<key>": cumulative kWh, ...}, ...]; key "us"|"base".
      Each interval's energy delta is billed at the rate in force at the
      interval's MIDPOINT (15-min sampling never straddles more than one ToD
      boundary by more than half a sample, so midpoint billing is exact to
      ±7.5 min of boundary energy).
    OUTPUT: ₹ for the span the samples cover (0.0 for fewer than 2 samples).
      Monotonic-safe: a negative delta (series reset) contributes zero.
    SIDE EFFECTS: none. ERROR STATES: none — missing keys read as 0.0.
    """
    if not samples or len(samples) < 2:
        return 0.0
    total = 0.0
    for prev, cur in zip(samples, samples[1:]):
        d_kwh = float(cur.get(key, 0.0) or 0.0) - float(prev.get(key, 0.0) or 0.0)
        if d_kwh <= 0.0:
            continue
        mid_t = (float(prev.get("t", 0.0)) + float(cur.get("t", 0.0))) / 2.0
        total += d_kwh * tou_rate((mid_t % 86400.0) / 3600.0)
    return round(total, 2)


def summary(history: list, now_hour: float) -> dict:
    """The /api/state -> meters.tou block. Everything json-safe, all additive.

    INPUT: LiveSim.history (oldest first), current sim hour-of-day.
    OUTPUT: dict(tariff, note, rate_now_rs, band_now, base_rate_rs,
      peak_mult, night_mult, us_rs, base_rs, saved_rs, window_h) — us/base/saved
      cover only the span history holds (last ~8 sim-days), which the window_h
      field states so the UI can never imply a since-Monday total it isn't.
    SIDE EFFECTS: none. ERROR STATES: none — empty history yields zeros.
    """
    us = tou_cost(history, "us")
    base = tou_cost(history, "base")
    span_s = (float(history[-1]["t"]) - float(history[0]["t"])) if len(history) > 1 else 0.0
    return {
        "tariff": TARIFF_NAME,
        "note": NOTE,
        "rate_now_rs": round(tou_rate(now_hour), 2),
        "band_now": band(now_hour),
        "base_rate_rs": BASE_RATE_RS,
        "peak_mult": PEAK_MULT,
        "night_mult": NIGHT_MULT,
        "us_rs": us,
        "base_rs": base,
        "saved_rs": round(max(0.0, base - us), 2),
        "window_h": round(span_s / 3600.0, 1),
    }
