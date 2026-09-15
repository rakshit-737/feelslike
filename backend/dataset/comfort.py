"""Explainable comfort metrics (DERIVED).

comfort_score = 0.60 * temperature_comfort_score
              + 0.25 * humidity_comfort_score
              + 0.15 * co2_comfort_score                      (each 0..100)
  temperature: 100 inside the profile comfort range, linear to 0 at 3 K outside
               (same thermal term as backend/telemetry.comfort_score)
  humidity:    100 inside the profile RH range, linear to 0 at 20 %RH outside
  CO2:         100 up to 70 % of the limit, 50 at the limit, 0 at 2 x the limit

PMV / PPD: ISO 7730 (Fanger) algorithm, implemented exactly. The INPUTS are the
approximation, and every row uses them:
  mean radiant temperature = air temperature (the twin has no radiant model)
  air speed 0.10 m/s (0.15 m/s at fan level 2)
  metabolic rate by building type (MET), clothing by season (CLO)
  external work 0
So PMV is a modelled estimate for a typical occupant, not a measurement.
Reference check (ASHRAE 55 example): 22 degC, 60 %RH, 0.1 m/s, 1.2 met, 0.5 clo
-> PMV ~ -0.75, PPD ~ 17 % (asserted in tests).
"""
from __future__ import annotations

import math

MET = {"office": 1.2, "mall": 1.6, "hospital": 1.2, "hotel": 1.1, "college": 1.2, "data_center": 1.2}
CLO = {"summer": 0.5, "monsoon": 0.5, "transition": 0.7, "winter": 1.0}
WEIGHTS = {"temperature": 0.60, "humidity": 0.25, "co2": 0.15}


def scores(t: float, rh: float, co2: float, cfg) -> dict:
    """Sub-scores come from backend/comfort.py (the one comfort formula set); the
    dataset keeps its generation-time weights WEIGHTS so stored history is stable."""
    from backend import comfort as engine
    ts = engine.thermal_score(t, cfg.comfort_min_c, cfg.comfort_max_c)[0]
    hs = engine.humidity_score(rh, cfg.humidity_min_pct, cfg.humidity_max_pct)[0]
    cs = engine.co2_score(co2, cfg.co2_max_ppm)
    total = WEIGHTS["temperature"] * ts + WEIGHTS["humidity"] * hs + WEIGHTS["co2"] * cs
    return {"comfort_score": round(total, 1), "temperature_comfort_score": round(ts, 1),
            "humidity_comfort_score": round(hs, 1), "co2_comfort_score": round(cs, 1)}


def pmv_ppd(ta: float, tr: float, vr: float, rh: float, met: float, clo: float,
            wme: float = 0.0) -> tuple:
    """ISO 7730 PMV/PPD. Returns (pmv, ppd) or (None, None) if the iteration fails."""
    pa = rh * 10.0 * math.exp(16.6536 - 4030.183 / (ta + 235.0))
    icl = 0.155 * clo
    m, w = met * 58.15, wme * 58.15
    mw = m - w
    fcl = 1.0 + 1.29 * icl if icl <= 0.078 else 1.05 + 0.645 * icl
    hcf = 12.1 * math.sqrt(vr)
    taa, tra = ta + 273.0, tr + 273.0
    tcla = taa + (35.5 - ta) / (3.5 * icl + 0.1)
    p1 = icl * fcl
    p2, p3, p4 = p1 * 3.96, p1 * 100.0, p1 * taa
    p5 = 308.7 - 0.028 * mw + p2 * (tra / 100.0) ** 4
    xn, xf = tcla / 100.0, tcla / 50.0
    hc, n = hcf, 0
    while abs(xn - xf) > 0.00015:
        xf = (xf + xn) / 2.0
        hcn = 2.38 * abs(100.0 * xf - taa) ** 0.25
        hc = max(hcf, hcn)
        xn = (p5 + p4 * hc - p2 * xf ** 4) / (100.0 + p3 * hc)
        n += 1
        if n > 150:
            return None, None
    tcl = 100.0 * xn - 273.0
    hl1 = 3.05e-3 * (5733.0 - 6.99 * mw - pa)
    hl2 = 0.42 * (mw - 58.15) if mw > 58.15 else 0.0
    hl3 = 1.7e-5 * m * (5867.0 - pa)
    hl4 = 0.0014 * m * (34.0 - ta)
    hl5 = 3.96 * fcl * (xn ** 4 - (tra / 100.0) ** 4)
    hl6 = fcl * hc * (tcl - ta)
    tsn = 0.303 * math.exp(-0.036 * m) + 0.028
    pmv = tsn * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6)
    ppd = 100.0 - 95.0 * math.exp(-0.03353 * pmv ** 4 - 0.2179 * pmv ** 2)
    return pmv, ppd


def thermal_status(pmv) -> str:
    """ASHRAE 7-point sensation from PMV."""
    if pmv is None:
        return "unknown"
    for lim, name in ((-2.5, "cold"), (-1.5, "cool"), (-0.5, "slightly_cool"), (0.5, "neutral"),
                      (1.5, "slightly_warm"), (2.5, "warm")):
        if pmv < lim:
            return name
    return "hot"


def comfort_row(t: float, rh: float, co2: float, vent: int, cfg, btype: str, season: str) -> dict:
    out = scores(t, rh, co2, cfg)
    pmv, ppd = pmv_ppd(t, t, 0.15 if vent >= 2 else 0.10, rh, MET[btype], CLO[season])
    out.update({"pmv": None if pmv is None else round(max(-3.0, min(3.0, pmv)), 2),
                "ppd": None if ppd is None else round(ppd, 1),
                "thermal_comfort_status": thermal_status(pmv)})
    return out
