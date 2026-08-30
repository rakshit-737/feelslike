"""Sim-to-real calibration: fit the shoebox rig's RC parameters to a logged
heater step response (Workstream A3).

    python -m scripts.fit_rc --selftest
    python -m scripts.fit_rc --log hw_log.json --power 10.0
    python -m scripts.fit_rc --log step.csv --power 10.0 --ambient 26.0

MODEL. The rig is one lumped thermal zone — the same first-order form as one
zone of sim/twin.py with no neighbours, no solar, no occupants:

    C dT/dt = UA (T_amb - T) + P_heat * heater(t)         R = 1/UA

so the whole calibration story is: the twin's per-zone physics, with R and C
measured from hardware instead of assumed. The protocol (hardware/README.md
bring-up) runs the step with the FAN OFF; rows logged with fan != 0 are
reported and excluded from the fit rather than silently blended in.

METHOD, deliberately whiteboardable (no black-box optimizer, no scipy):
  1. Closed form. The decay after heater-off is T(t) = T_amb + dT0 e^(-t/tau);
     a log-linear fit of ln(T - T_amb) against t gives tau = R*C. If the
     heating phase reached a plateau, dT_ss = P*R gives R directly; otherwise
     R falls out of the transient rise. C = tau / R.
  2. Refinement. Simulate the model over the full log and minimize RMSE on a
     log-spaced (R, C) grid around the closed-form seed, zooming twice.
     Vectorized over candidates with numpy; explicit Euler at the log cadence.

HONESTY. The output JSON carries kind="selftest" (synthetic, known truth) or
kind="measured" (a real rig log). Only a "measured" result may feed the deck's
calibration overlay, and only through the capability gate. This tool shipping
before the rig exists is the point: tested against synthetic truth now, so the
first real log produces a defensible fit, not a debugging session.

INPUT FORMATS.
  - JSON: the GET /api/hw/log response ({"rows":[...]}) or a bare list of rows;
    each row needs t_wall (s), temp_c, heater (bool); fan optional.
  - CSV: header with columns t,temp_c,heater[,fan] — t in seconds.

Output: evals/results_calibration.json (params, residuals, both series).
Stdlib + numpy only (numpy is already a runtime dependency of the RL stack).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

OUT_FILE = Path(__file__).resolve().parent.parent / "evals" / "results_calibration.json"

# Selftest ground truth: a plausible cardboard shoebox. UA ~ 0.67 W/K,
# air + participating cardboard mass ~ 400 J/K -> tau = 600 s, and a 10 W
# resistor gives dT_ss = P*R = 15 K. Chosen for realism, not convenience.
TRUE_R = 1.5          # K/W
TRUE_C = 400.0        # J/K
TRUE_P = 10.0         # W
SELFTEST_DT = 2.0     # s, the node's poll cadence
NOISE_SHT31 = 0.05    # degC RMS  (SHT31-grade sensor)
NOISE_DHT22 = 0.35    # degC RMS  (DHT22-grade sensor, quantized 0.1)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

def simulate(t: np.ndarray, heater: np.ndarray, R: np.ndarray, C: np.ndarray,
             power_w: float, ambient_c: float, t0_c: float) -> np.ndarray:
    """Explicit-Euler first-order response for one or many (R, C) candidates.

    INPUT: t (n,) seconds, heater (n,) 0/1, R and C broadcastable arrays of
      shape (m,), power_w, ambient_c, t0_c (initial temperature).
    OUTPUT: (m, n) simulated temperatures (or (n,) if R and C are scalars).
    SIDE EFFECTS: none. ERROR STATES: none — the Euler step is stable as long
      as dt << tau, which a 2 s poll against a >100 s box always satisfies.
    """
    R = np.atleast_1d(np.asarray(R, dtype=float))
    C = np.atleast_1d(np.asarray(C, dtype=float))
    m, n = R.shape[0], t.shape[0]
    T = np.full((m, n), float(t0_c))
    ua = 1.0 / R
    for i in range(1, n):
        dt = t[i] - t[i - 1]
        q = ua * (ambient_c - T[:, i - 1]) + power_w * heater[i - 1]
        T[:, i] = T[:, i - 1] + dt * q / C
    return T[0] if m == 1 else T


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_rows(path: Path) -> list[dict]:
    """Rows as [{t, temp_c, heater, fan}] from a JSON hw-log or a CSV.

    INPUT: path. OUTPUT: list of dicts, t rebased to start at 0.0 and sorted.
    ERROR STATES: ValueError naming what is missing or unparseable.
    """
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            raw = list(csv.DictReader(f))
        if not raw:
            raise ValueError(f"{path}: empty CSV")
        need = {"t", "temp_c", "heater"} - set(raw[0])
        if need:
            raise ValueError(f"{path}: CSV missing columns {sorted(need)}")
        rows = [{"t": float(r["t"]), "temp_c": float(r["temp_c"]),
                 "heater": r["heater"].strip().lower() in ("1", "true", "on"),
                 "fan": int(float(r.get("fan", 0) or 0))} for r in raw]
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["rows"] if isinstance(data, dict) else data
        if not raw:
            raise ValueError(f"{path}: no rows")
        missing = [k for k in ("t_wall", "temp_c") if k not in raw[0]]
        if missing:
            raise ValueError(f"{path}: rows missing {missing}")
        if "heater" not in raw[0]:
            raise ValueError(
                f"{path}: rows carry no heater state — log with the current "
                f"server (readings now record fan/heater at reading time)")
        rows = [{"t": float(r["t_wall"]), "temp_c": float(r["temp_c"]),
                 "heater": bool(r["heater"]), "fan": int(r.get("fan", 0) or 0)}
                for r in raw if r.get("temp_c") is not None]
    rows.sort(key=lambda r: r["t"])
    t0 = rows[0]["t"]
    for r in rows:
        r["t"] -= t0
    return rows


# --------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------

def closed_form_seed(t: np.ndarray, T: np.ndarray, heater: np.ndarray,
                     power_w: float, ambient_c: float) -> dict:
    """Whiteboard estimates of tau, R, C from the decay and the rise.

    INPUT: aligned arrays + heater power + ambient.
    OUTPUT: {tau_s, R, C, method notes}; any piece that cannot be estimated is
      None with the reason in notes (the grid can still refine from defaults).
    SIDE EFFECTS: none. ERROR STATES: none.
    """
    notes: list[str] = []
    tau = None
    on = heater > 0.5
    # ---- decay: from the last heater-off to the end --------------------
    if on.any() and not on[-1]:
        last_on = int(np.where(on)[0][-1])
        td, Td = t[last_on + 1:], T[last_on + 1:]
        excess = Td - ambient_c
        keep = excess > 0.3                       # above the noise floor
        if keep.sum() >= 8:
            slope, _ = np.polyfit(td[keep], np.log(excess[keep]), 1)
            if slope < 0:
                tau = -1.0 / slope
                notes.append(f"tau from decay log-slope over {int(keep.sum())} samples")
        if tau is None:
            notes.append("decay too short/noisy for a log-linear fit")
    else:
        notes.append("no decay segment (heater never turned off before the log end)")
    # ---- R: plateau if reached, else transient rise --------------------
    R = None
    if on.any():
        seg = np.where(on)[0]
        Ton, tt = T[seg], t[seg]
        span = tt[-1] - tt[0]
        if span > 0 and tau is not None and span > 3.0 * tau:
            plateau = float(np.median(Ton[tt - tt[0] > 0.8 * span]))
            R = (plateau - ambient_c) / power_w
            notes.append("R from steady-state rise (heating ran past 3 tau)")
        elif tau is not None and span > 0:
            # T(t) = amb + P R (1 - e^(-t/tau)) -> invert at the segment end
            frac = 1.0 - math.exp(-span / tau)
            rise = float(Ton[-1]) - ambient_c
            if frac > 0.1 and rise > 0:
                R = rise / (power_w * frac)
                notes.append(f"R from transient rise at {span:.0f}s ({frac:.0%} of step)")
        if R is None:
            notes.append("rise too short to estimate R in closed form")
    else:
        notes.append("heater never on — cannot estimate R (log the step per README)")
    C = (tau / R) if (tau is not None and R is not None and R > 0) else None
    return {"tau_s": tau, "R": R, "C": C, "notes": notes}


def grid_fit(t: np.ndarray, T: np.ndarray, heater: np.ndarray, power_w: float,
             ambient_c: float, seed_R: float, seed_C: float,
             rounds: int = 3, width: float = 4.0, n: int = 21) -> dict:
    """Log-spaced (R, C) grid search around the seed, zooming each round.

    INPUT: aligned arrays; seed_R/seed_C (closed-form or defaults); rounds,
      width (span factor of round 1), n (points per axis).
    OUTPUT: {R, C, tau_s, rmse_c}. SIDE EFFECTS: none. ERROR STATES: none.
    """
    bestR, bestC, best_rmse = float(seed_R), float(seed_C), float("inf")
    for r in range(rounds):
        w = width ** (1.0 / (2 ** r))
        Rs = np.geomspace(bestR / w, bestR * w, n)
        Cs = np.geomspace(bestC / w, bestC * w, n)
        RR, CC = np.meshgrid(Rs, Cs)
        sim = simulate(t, heater, RR.ravel(), CC.ravel(), power_w,
                       ambient_c, float(T[0]))
        rmse = np.sqrt(np.mean((sim - T[None, :]) ** 2, axis=1))
        k = int(np.argmin(rmse))
        bestR, bestC, best_rmse = float(RR.ravel()[k]), float(CC.ravel()[k]), float(rmse[k])
    return {"R": bestR, "C": bestC, "tau_s": bestR * bestC, "rmse_c": best_rmse}


def fit(rows: list[dict], power_w: float, ambient_c: float | None,
        kind: str, source: str) -> dict:
    """The whole pipeline: clean -> seed -> refine -> report.

    INPUT: rows (load_rows/selftest output), heater power in W, ambient degC
      (None = median of the pre-step samples), kind ("measured"|"selftest"),
      source (a filename or "synthetic").
    OUTPUT: the result dict written to evals/results_calibration.json.
    ERROR STATES: ValueError when the log cannot support a fit (too short, no
      heater activity, no way to establish ambient).
    """
    fan_rows = sum(1 for r in rows if r.get("fan"))
    rows = [r for r in rows if not r.get("fan")]
    if len(rows) < 30:
        raise ValueError(f"only {len(rows)} usable rows (fan off); need >= 30")
    t = np.array([r["t"] for r in rows])
    T = np.array([r["temp_c"] for r in rows])
    heater = np.array([1.0 if r["heater"] else 0.0 for r in rows])
    if not heater.any():
        raise ValueError("heater is never on in this log — no step to fit")

    first_on = int(np.argmax(heater > 0.5))
    if ambient_c is None:
        if first_on < 5:
            raise ValueError("no pre-step baseline; pass --ambient explicitly")
        ambient_c = float(np.median(T[:first_on]))

    seed = closed_form_seed(t, T, heater, power_w, ambient_c)
    seed_R = seed["R"] if seed["R"] and seed["R"] > 0 else 1.0
    seed_C = seed["C"] if seed["C"] and seed["C"] > 0 else 500.0
    refined = grid_fit(t, T, heater, power_w, ambient_c, seed_R, seed_C)
    sim = simulate(t, heater, refined["R"], refined["C"], power_w,
                   ambient_c, float(T[0]))

    step = max(1, len(rows) // 400)               # thin the series for the chart
    return {
        "kind": kind,                             # "measured" | "selftest" — honesty flag
        "source": source,
        "n_samples": len(rows),
        "excluded_fan_on_rows": fan_rows,
        "power_w": power_w,
        "ambient_c": round(ambient_c, 3),
        "closed_form": {k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in seed.items()},
        "fit": {"R_K_per_W": round(refined["R"], 4),
                "UA_W_per_K": round(1.0 / refined["R"], 4),
                "C_J_per_K": round(refined["C"], 2),
                "tau_s": round(refined["tau_s"], 1),
                "rmse_c": round(refined["rmse_c"], 4)},
        "series": {"t_s": [round(float(x), 1) for x in t[::step]],
                   "measured_c": [round(float(x), 3) for x in T[::step]],
                   "simulated_c": [round(float(x), 3) for x in sim[::step]],
                   "heater": [int(x) for x in heater[::step]]},
        "note": ("Synthetic self-test — NOT hardware data; never present as measured."
                 if kind == "selftest" else
                 "Fitted to a real rig log. Chart this via the calibration overlay."),
    }


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def synthesize(noise_c: float, seed: int = 7) -> list[dict]:
    """A ground-truth step-response log: 5 min baseline, 30 min heat, 30 min
    decay at the node's 2 s cadence, Gaussian sensor noise. Deterministic."""
    rng = np.random.default_rng(seed)
    t = np.arange(0.0, (5 + 30 + 30) * 60.0, SELFTEST_DT)
    heater = ((t >= 300.0) & (t < 300.0 + 1800.0)).astype(float)
    clean = simulate(t, heater, TRUE_R, TRUE_C, TRUE_P, 26.0, 26.0)
    noisy = clean + rng.normal(0.0, noise_c, clean.shape)
    return [{"t": float(a), "temp_c": float(b), "heater": bool(h > 0.5), "fan": 0}
            for a, b, h in zip(t, noisy, heater)]


def selftest() -> dict:
    """Fit synthetic logs at SHT31 and DHT22 noise; report recovery error."""
    out = {}
    for label, noise in (("sht31", NOISE_SHT31), ("dht22", NOISE_DHT22)):
        res = fit(synthesize(noise), TRUE_P, None, "selftest", f"synthetic/{label}")
        res["truth"] = {"R": TRUE_R, "C": TRUE_C, "tau_s": TRUE_R * TRUE_C}
        res["recovery_err_pct"] = {
            "R": round(100.0 * abs(res["fit"]["R_K_per_W"] - TRUE_R) / TRUE_R, 2),
            "C": round(100.0 * abs(res["fit"]["C_J_per_K"] - TRUE_C) / TRUE_C, 2),
            "tau": round(100.0 * abs(res["fit"]["tau_s"] - TRUE_R * TRUE_C)
                         / (TRUE_R * TRUE_C), 2),
        }
        out[label] = res
    return out


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", type=Path, help="hw log JSON (GET /api/hw/log) or CSV")
    ap.add_argument("--power", type=float, help="heater electrical power in W")
    ap.add_argument("--ambient", type=float, default=None,
                    help="ambient degC (default: median of pre-step samples)")
    ap.add_argument("--selftest", action="store_true",
                    help="fit synthetic logs with known truth instead of a file")
    args = ap.parse_args()

    if args.selftest:
        results = selftest()
        for label, res in results.items():
            e = res["recovery_err_pct"]
            print(f"[{label}]  noise-> fit R={res['fit']['R_K_per_W']} K/W "
                  f"C={res['fit']['C_J_per_K']} J/K tau={res['fit']['tau_s']}s "
                  f"| truth R={TRUE_R} C={TRUE_C} tau={TRUE_R * TRUE_C:.0f}s "
                  f"| err R {e['R']}% C {e['C']}% tau {e['tau']}%")
        payload: dict = {"kind": "selftest", "results": results}
    else:
        if not args.log or args.power is None:
            ap.error("--log and --power are required (or use --selftest)")
        rows = load_rows(args.log)
        payload = fit(rows, args.power, args.ambient, "measured", str(args.log))
        f = payload["fit"]
        print(f"fit: R={f['R_K_per_W']} K/W (UA={f['UA_W_per_K']} W/K)  "
              f"C={f['C_J_per_K']} J/K  tau={f['tau_s']}s  rmse={f['rmse_c']} degC "
              f"over {payload['n_samples']} samples")

    OUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved -> {OUT_FILE}")


if __name__ == "__main__":
    main()
