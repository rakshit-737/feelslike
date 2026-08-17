"""Psychrometrics for the FeelsLike twin: pure functions, no state, no imports
from sim.weather (weather.py re-exports from here, so the dependency is one-way).

Why this exists: a Delhi office in August is a *latent* problem as much as a
sensible one. "It's stuffy" and "it's clammy" are humidity complaints, and a
thermostat that only knows dry-bulb temperature cannot explain or answer them.
This module gives the twin enough moisture physics to report RH and dew point
defensibly.

MODELING ASSUMPTION (and known limitation): latent load is *tracked and
reported*, never *charged* to the energy model. The coil's dehumidification is
computed from the sensible cooling it is already doing (via a fixed sensible
heat ratio), so adding humidity leaves kWh, cost, CO2 and the temperature
trajectory bit-for-bit unchanged. A real chiller pays extra compressor work for
latent removal; a future version should split the coil load and divide by a
latent-aware COP. Until then treat RH as an observability signal, not a cost.

Formulas are named at each call site: Magnus/Tetens for saturation pressure and
dew point, the standard mixing-ratio identity for humidity ratio.
"""
from __future__ import annotations

import math

P_ATM = 101325.0            # Pa, sea-level standard atmosphere
LATENT_PER_PERSON_W = 60.0  # W latent per seated office occupant (ASHRAE 1.1 met)
H_FG = 2.45e6               # J/kg, latent heat of vaporisation of water ~25 degC
SHR = 0.75                  # sensible heat ratio of a typical DX cooling coil

# Magnus/Tetens coefficients (over liquid water, valid roughly -40..60 degC)
_MAG_A = 17.625
_MAG_B = 243.04
_MAG_C = 610.94             # Pa


def sat_pressure(temp_c: float) -> float:
    """Saturation vapour pressure over liquid water.

    Magnus/Tetens: p_sat = 610.94 * exp(17.625*T / (T + 243.04))  [Pa]

    INPUT:  temp_c  dry-bulb temperature, degC (finite).
    OUTPUT: Pa, always > 0.
    SIDE EFFECTS: none.
    ERROR STATES: temp_c <= -243.04 would divide by zero; the denominator is
        floored at 1e-6 so absurd inputs return a tiny positive pressure
        instead of raising.
    """
    denom = temp_c + _MAG_B
    if denom < 1e-6:
        denom = 1e-6
    return _MAG_C * math.exp(_MAG_A * temp_c / denom)


def humidity_ratio(temp_c: float, rh_pct: float) -> float:
    """Humidity ratio (mixing ratio) W from dry-bulb temp and relative humidity.

    p_v = rh/100 * p_sat(T);  W = 0.622 * p_v / (P_atm - p_v)   [kg water / kg dry air]

    INPUT:  temp_c degC; rh_pct 0..100 (values outside are clamped).
    OUTPUT: kg/kg, >= 0.
    SIDE EFFECTS: none.
    ERROR STATES: p_v is capped just below P_atm so the ratio never blows up
        at superheated/absurd inputs.
    """
    rh = min(100.0, max(0.0, rh_pct))
    p_v = rh / 100.0 * sat_pressure(temp_c)
    p_v = min(p_v, P_ATM - 1.0)
    return 0.622 * p_v / (P_ATM - p_v)


def rh_from_ratio(temp_c: float, w: float) -> float:
    """Inverse of humidity_ratio: relative humidity from W and dry-bulb temp.

    p_v = P_atm * W / (0.622 + W);  rh = 100 * p_v / p_sat(T)

    INPUT:  temp_c degC; w kg/kg (negative treated as 0).
    OUTPUT: percent, clamped to 0..100.
    SIDE EFFECTS: none.
    ERROR STATES: none (all divisions have positive denominators).
    """
    ww = max(0.0, w)
    p_v = P_ATM * ww / (0.622 + ww)
    return min(100.0, max(0.0, 100.0 * p_v / sat_pressure(temp_c)))


def dew_point(temp_c: float, rh_pct: float) -> float:
    """Dew-point temperature.

    Magnus inverse: g = ln(rh/100) + a*T/(T+b);  Td = b*g / (a - g)

    INPUT:  temp_c degC; rh_pct 0..100 (clamped to >= 0.1 so the log is finite).
    OUTPUT: degC, never above temp_c.
    SIDE EFFECTS: none.
    ERROR STATES: rh <= 0 would make ln undefined; floored at 0.1%.
    """
    rh = min(100.0, max(0.1, rh_pct))
    denom = temp_c + _MAG_B
    if denom < 1e-6:
        denom = 1e-6
    g = math.log(rh / 100.0) + _MAG_A * temp_c / denom
    if abs(_MAG_A - g) < 1e-9:
        return temp_c
    return min(temp_c, _MAG_B * g / (_MAG_A - g))


def _day_wobble(day: int, seed: int = 0) -> float:
    """Deterministic per-day RH wobble in [-2.5, +2.5] %RH.

    Same hash-style trick sim.weather._day_offset uses (fract of a scaled sine)
    but with different constants, so the RH wobble is not locked to the
    temperature wobble for a given seed.
    """
    x = math.sin((day + 1) * 39.3467 + seed * 11.135 + 4.7) * 24634.6345
    return (x - math.floor(x)) * 5.0 - 2.5


def outdoor_rh(t_seconds: float, seed: int = 0) -> float:
    """Synthetic outdoor relative humidity, monsoon-season Indian city (August).

    Reasoning: outdoor absolute moisture is nearly flat over a day, so RH is
    driven almost entirely by dry-bulb temperature — high at the pre-dawn
    minimum (air near saturation, dew on the glass) and lowest at the mid
    afternoon peak. sim.weather.outdoor_temp uses swing = 6*sin(pi*(h-9)/12),
    which minimises at 03:00 and maximises at 15:00, so we use the SAME phase
    term with a NEGATIVE amplitude to get the anti-correlation for free without
    importing weather.py (that would create an import cycle).

    Daily mean ~71.5%, diurnal amplitude 13.5, per-day wobble +/-2.5 and a small
    smooth intra-day wobble +/-1.0 for passing cloud/breeze — roughly 55..88%,
    then hard-clamped to 5..100.

    INPUT:  t_seconds sim seconds since Monday 00:00; seed int.
    OUTPUT: percent RH, 5..100. Same (t, seed) always gives the same value.
    SIDE EFFECTS: none (no global RNG touched).
    ERROR STATES: none.
    """
    day = int(t_seconds // 86400)
    h = (t_seconds % 86400) / 3600.0
    base = 71.5 + _day_wobble(day, seed)
    swing = -13.5 * math.sin(math.pi * (h - 9.0) / 12.0)   # anti-phase to temp
    wobble = 1.0 * math.sin(h * 1.7 + day * 0.9)
    return min(100.0, max(5.0, base + swing + wobble))
