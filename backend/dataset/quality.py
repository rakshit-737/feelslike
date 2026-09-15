"""RAW sensor layer, validation and CLEAN layer. The simulated truth is never modified.

RAW   = what a field sensor would report for the truth: gaussian noise (x noise_level),
        per-sensor temperature drift (linear, undetectable without a reference — kept
        and documented, never "cleaned"), missing readings, delayed readings (the
        previous sample re-sent; reading_delay_s says so), outliers, plus the SENSOR
        anomalies: temp_sensor_fault (offset or stuck) and communication_gap.
VALIDATE = physical range checks + spike test against the median of +/-2 neighbours
        + stuck detection (>= 12 identical consecutive temperature readings).
CLEAN = invalid -> removed; gaps <= max_interpolate_gap samples linearly interpolated;
        longer gaps stay missing. Per-field quality flag:
        ok | interpolated | missing | outlier_removed | stuck_removed | delayed
"""
from __future__ import annotations

import random
import statistics

FIELDS = ("temp", "rh", "co2", "occ", "power")
RANGE = {"temp": (0.0, 50.0), "rh": (0.0, 100.0), "co2": (350.0, 5000.0), "occ": (0, 10_000),
         "power": (0.0, 10_000.0)}
SPIKE = {"temp": 2.5, "rh": 15.0, "co2": 450.0}
STUCK_RUN = 12


def make_raw(truth: dict, qc, noise_level: float, step_min: int, rng: random.Random,
             faults: list) -> dict:
    """truth: {"temp": [...], "rh": [...], "co2": [...], "occ": [...], "power": [...]}.
    faults: [(i0, i1, "temp_sensor_fault"|"communication_gap", params)].
    -> {"temp": [...], ..., "delay_s": [...], "flags": [...]} same length."""
    n = len(truth["temp"])
    nl = noise_level if qc.noise_enabled else 0.0
    sd = {"temp": qc.temp_noise_c * nl, "rh": qc.rh_noise_pct * nl, "co2": qc.co2_noise_ppm * nl}
    drift = rng.uniform(-qc.temp_drift_c_per_day_max, qc.temp_drift_c_per_day_max) if qc.drift_enabled else 0.0
    out = {f: [None] * n for f in FIELDS}
    out["delay_s"], out["flags"] = [0] * n, [""] * n
    per_day = 1440.0 / step_min
    stuck_val = None
    for i in range(n):
        flags = []
        src = i
        if i > 0 and rng.random() < qc.delay_rate:
            src, flags = i - 1, ["delayed"]
            out["delay_s"][i] = step_min * 60
        temp = truth["temp"][src] + rng.gauss(0, sd["temp"]) + drift * (i / per_day)
        rh = truth["rh"][src] + rng.gauss(0, sd["rh"])
        co2 = truth["co2"][src] + rng.gauss(0, sd["co2"])
        occ = truth["occ"][src]
        power = truth["power"][src] * (1 + rng.gauss(0, qc.power_noise_frac * nl))
        if nl > 0:
            flags.append("noise")
        if drift:
            flags.append("drift")
        if rng.random() < qc.outlier_rate:
            which = rng.choice(("temp", "rh", "co2", "power"))
            if which == "temp":
                temp += rng.choice((-1, 1)) * rng.uniform(5, 10)
            elif which == "rh":
                rh += rng.choice((-1, 1)) * 30
            elif which == "co2":
                co2 *= 2.5
            else:
                power *= 3.0
            flags.append(f"outlier:{which}")
        vals = {"temp": temp, "rh": min(100.0, max(0.0, rh)), "co2": max(0.0, co2),
                "occ": occ, "power": max(0.0, power)}
        for i0, i1, kind, params in faults:
            if i0 <= i < i1:
                if kind == "communication_gap":
                    vals = {f: None for f in FIELDS}
                    flags.append("comm_gap")
                elif kind == "temp_sensor_fault":
                    if params.get("stuck"):
                        stuck_val = stuck_val if stuck_val is not None else round(vals["temp"], 1)
                        vals["temp"] = stuck_val
                        flags.append("sensor_stuck")
                    else:
                        vals["temp"] += params.get("offset_c", 3.0)
                        flags.append("sensor_offset")
        if not any(i0 <= i < i1 and k == "temp_sensor_fault" for i0, i1, k, _ in faults):
            stuck_val = None
        for f in FIELDS:
            if vals[f] is not None and rng.random() < qc.missing_data_rate:
                vals[f] = None
                flags.append(f"missing:{f}")
        for f in FIELDS:
            v = vals[f]
            out[f][i] = None if v is None else (int(v) if f == "occ" else round(v, 2))
        out["flags"][i] = ",".join(flags)
    return out


def validate(raw: dict, capacity: int, power_max_kw: float) -> dict:
    """-> {field: [None | "range" | "spike" | "stuck"]} per sample."""
    n = len(raw["temp"])
    issues = {f: [None] * n for f in FIELDS}
    lim = dict(RANGE, occ=(0, capacity), power=(0.0, power_max_kw))
    for f in FIELDS:
        s = raw[f]
        lo, hi = lim[f]
        for i, v in enumerate(s):
            if v is None:
                continue
            if not lo <= v <= hi:
                issues[f][i] = "range"
            elif f in SPIKE:
                nb = [s[j] for j in range(max(0, i - 2), min(n, i + 3)) if j != i and s[j] is not None]
                if len(nb) >= 2 and abs(v - statistics.median(nb)) > SPIKE[f]:
                    issues[f][i] = "spike"
            elif f == "power":
                nb = [s[j] for j in range(max(0, i - 2), min(n, i + 3)) if j != i and s[j] is not None]
                if len(nb) >= 2:
                    med = statistics.median(nb)
                    if v > 2.2 * med + 0.5:
                        issues[f][i] = "spike"
    run_start = 0
    t = raw["temp"]
    for i in range(1, n + 1):
        if i == n or t[i] is None or t[i] != t[run_start]:
            if t[run_start] is not None and i - run_start >= STUCK_RUN:
                for j in range(run_start, i):
                    issues["temp"][j] = "stuck"
            run_start = i
    return issues


def clean(raw: dict, issues: dict, qc) -> dict:
    """-> {"temp": [...], ..., "quality": {field: [flag]}}; raw is not modified."""
    n = len(raw["temp"])
    out, quality = {}, {}
    for f in FIELDS:
        vals = list(raw[f])
        q = ["ok"] * n
        for i in range(n):
            iss = issues[f][i]
            if iss is not None:
                vals[i] = None
                q[i] = "stuck_removed" if iss == "stuck" else "outlier_removed"
            elif vals[i] is None:
                q[i] = "missing"
            elif raw["delay_s"][i]:
                q[i] = "delayed"
        i = 0
        while i < n:
            if vals[i] is not None:
                i += 1
                continue
            j = i
            while j < n and vals[j] is None:
                j += 1
            if 0 < i and j < n and (j - i) <= qc.max_interpolate_gap:
                a, b = vals[i - 1], vals[j]
                for k in range(i, j):
                    v = a + (b - a) * (k - i + 1) / (j - i + 1)
                    vals[k] = int(round(v)) if f == "occ" else round(v, 2)
                    q[k] = "interpolated"
            else:
                for k in range(i, j):
                    if q[k] == "ok":
                        q[k] = "missing"
            i = j
        out[f], quality[f] = vals, q
    out["quality"] = quality
    return out
