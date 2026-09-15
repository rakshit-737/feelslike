"""Cross-calibrate the Uno LM35 ambient node against the rig's DHT22.

    python -m scripts.crosscal_ambient                  # last 10 min, node uno-ambient
    python -m scripts.crosscal_ambient --minutes 15 --vref-now 1.100

WHY. The Uno reads the LM35 on its internal ~1.1 V reference, which varies up
to +/-10 % chip to chip (about +/-3 degC at room temperature). Until that is
measured, the ambient node's numbers are not used for anything.

PROCEDURE (hardware/README.md, ambient node section):
  1. LM35 right beside the DHT22, box open, fan off, heater off.
  2. About 15 minutes to settle, both nodes posting.
  3. Run this, then set VREF_V in ambient_node_uno.ino to the suggested value.

THE MATHS. The LM35 is linear through 0 degC (10 mV/degC, no offset), so a
reference error is a pure GAIN: T_true = T_uno * (VREF_true / VREF_now). The
suggested VREF is VREF_now * mean(T_dht22) / mean(T_uno) over time-paired
readings. The offset left over after that correction is reported as a check:
if it is large, something other than the reference is wrong (wiring, the two
sensors not in the same air, self-heating). The result can be no better than
the DHT22's own +/-0.5 degC accuracy, and the script states that uncertainty.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from bisect import bisect_left

DHT22_ACCURACY_C = 0.5
MIN_PAIRS = 30


def pair_readings(dht_rows: list, uno_rows: list, max_gap_s: float = 3.0) -> list:
    """Time-pair two reading logs.

    INPUT: rows carrying t_wall and temp_c; rows with no temperature (or a
      fault) are skipped.
    OUTPUT: [(t_wall, dht_temp, uno_temp)] - each Uno reading matched to the
      nearest DHT22 reading within max_gap_s; unmatched readings are dropped.
    SIDE EFFECTS: none. ERROR STATES: none.
    """
    d = sorted((float(r["t_wall"]), float(r["temp_c"])) for r in dht_rows
               if r.get("temp_c") is not None)
    ts = [t for t, _ in d]
    out = []
    for r in uno_rows:
        if r.get("temp_c") is None or r.get("fault"):
            continue
        t = float(r["t_wall"])
        i = bisect_left(ts, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ts) and (best is None or abs(ts[j] - t) < abs(ts[best] - t)):
                best = j
        if best is not None and abs(ts[best] - t) <= max_gap_s:
            out.append((t, d[best][1], float(r["temp_c"])))
    return out


def suggest_vref(pairs: list, vref_now: float = 1.1) -> dict:
    """Gain correction from time-paired readings. See the module docstring.

    ERROR STATES: ValueError with fewer than MIN_PAIRS pairs, or a non-positive
      Uno mean (a wiring fault, not a room).
    """
    n = len(pairs)
    if n < MIN_PAIRS:
        raise ValueError(f"need at least {MIN_PAIRS} paired readings, have {n}")
    mean_d = sum(p[1] for p in pairs) / n
    mean_u = sum(p[2] for p in pairs) / n
    if mean_u <= 0:
        raise ValueError("Uno mean temperature is not positive - check the LM35 wiring")
    gain = mean_d / mean_u
    resid = [p[1] - p[2] * gain for p in pairs]
    return {
        "pairs": n,
        "mean_dht22_c": round(mean_d, 3),
        "mean_uno_c": round(mean_u, 3),
        "raw_offset_c": round(mean_d - mean_u, 3),
        "gain": round(gain, 5),
        "vref_now_v": vref_now,
        "vref_suggested_v": round(vref_now * gain, 4),
        "vref_uncertainty_v": round(vref_now * gain * DHT22_ACCURACY_C / mean_d, 4),
        "residual_offset_c": round(sum(resid) / n, 3),
        "residual_rmse_c": round(math.sqrt(sum(x * x for x in resid) / n), 3),
    }


def _get(gateway: str, path: str, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(gateway.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--node", default="uno-ambient")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--vref-now", type=float, default=1.1,
                    help="the VREF_V currently flashed on the Uno")
    ap.add_argument("--max-gap", type=float, default=3.0, help="pairing window in seconds")
    args = ap.parse_args()

    since = time.time() - args.minutes * 60.0
    try:
        rig = [r for r in _get(args.gateway, "/api/hw/log?limit=8192")["rows"]
               if r["t_wall"] >= since]
        uno = [r for r in _get(args.gateway, f"/api/hw/sensors/{args.node}/log?limit=8192")["rows"]
               if r["t_wall"] >= since]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"gateway said HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"cannot reach the gateway at {args.gateway}: {e}")

    heat = sum(1 for r in rig if r.get("heater"))
    fan = sum(1 for r in rig if r.get("fan"))
    if heat or fan:
        print(f"[warn] {heat} rig readings with the heater on and {fan} with the fan on in this "
              f"window. The two sensors must share still air - rerun with both off.")
    pairs = pair_readings(rig, uno, args.max_gap)
    try:
        res = suggest_vref(pairs, args.vref_now)
    except ValueError as e:
        raise SystemExit(f"cannot cross-calibrate: {e} (window {args.minutes} min, "
                         f"{len(rig)} DHT22 and {len(uno)} Uno readings)")

    print(f"paired readings     {res['pairs']}  (within {args.max_gap} s)")
    print(f"mean DHT22          {res['mean_dht22_c']} degC")
    print(f"mean Uno LM35       {res['mean_uno_c']} degC   (raw offset {res['raw_offset_c']:+} degC)")
    print(f"gain                {res['gain']}")
    print(f"after correction    offset {res['residual_offset_c']:+} degC, rmse {res['residual_rmse_c']} degC")
    print(f"VREF_V now          {res['vref_now_v']} V")
    print(f"VREF_V suggested    {res['vref_suggested_v']} V  "
          f"(+/- {res['vref_uncertainty_v']} V from the DHT22's +/-{DHT22_ACCURACY_C} degC)")
    print(f"\nSet in ambient_node_uno.ino:  const float VREF_V = {res['vref_suggested_v']:.3f};")
    print("                              const bool CROSS_CALIBRATED = true;")
    if abs(res["residual_offset_c"]) > DHT22_ACCURACY_C:
        print("[warn] offset left after the gain fix exceeds the DHT22's accuracy - check that "
              "both sensors are really in the same air before trusting this.")


if __name__ == "__main__":
    main()
