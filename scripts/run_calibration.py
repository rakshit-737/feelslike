"""Hardware-day calibration, one command: drive the step, log it, fit it, chart it.

    python -m scripts.run_calibration --power 10.0
    python -m scripts.run_calibration --power 10.0 --baseline-min 3 --heat-min 10 --decay-min 12
    python -m scripts.run_calibration --power 10.0 --fit-only evals/hw_step_log.json

PREREQUISITES (the bring-up checklist in hardware/README.md, through step 5):
  - the server is running (uvicorn backend.app:app) and GET /api/hw/status says
    connected: true (a real node — or scripts.mock_node for a full rehearsal);
  - the FAN IS OFF and stays off: the fit excludes fan-on rows, so an active
    "stuffy" constraint on the rig zone during the run wastes your data — run
    this on a quiet building (fresh /api/reset is ideal);
  - --power is the heater's REAL electrical draw in watts (measure it: V^2/R
    for the resistor at the actual supply voltage — do not guess).

WHAT IT DOES: baseline (ambient estimate) -> heater ON via POST /api/hw/heater
(the server's 50%/10-min duty cap will chop a long request into a bang-bang
waveform; that is fine and expected — the fitter uses the RECORDED heater
timeline, and the end-to-end test proves recovery within 10% under the cap)
-> heater OFF -> decay -> saves the log (evals/hw_step_log.json), fits R and C
(scripts.fit_rc, kind="measured"), writes evals/results_calibration.json and
the deck overlay evals/calibration_overlay.svg.

SAFETY: this script only requests; the envelope (server duty cap), the
firmware watchdog and the physical switch all outrank it. Keep a hand near the
switch on the first heated run anyway.

Stdlib only. Wall-clock runtime = baseline + heat + decay (default 65 min).
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_OUT = ROOT / "evals" / "hw_step_log.json"
RESULTS_OUT = ROOT / "evals" / "results_calibration.json"


def api(gateway: str, path: str, payload: dict | None = None, timeout: float = 5.0):
    url = gateway.rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_phase(gateway: str, label: str, minutes: float) -> None:
    """Sleep a phase out, printing the live reading every 30 s."""
    end = time.time() + minutes * 60.0
    while time.time() < end:
        try:
            st = api(gateway, "/api/hw/status")
            r = st.get("reading") or {}
            print(f"[{label:>8}] t-{(end - time.time()) / 60.0:4.1f} min  "
                  f"temp={r.get('temp_c')} rh={r.get('rh_pct')} "
                  f"fan={st.get('fan')} heater={st.get('heater')} "
                  f"duty={st.get('duty')}"
                  + ("  !! FAN ON — these rows will be excluded from the fit"
                     if st.get("fan") else ""))
            if not st.get("connected"):
                print(f"[{label:>8}] node went STALE — check power/WiFi; continuing to wait")
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[{label:>8}] server unreachable ({type(e).__name__}) — retrying")
        time.sleep(30.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--power", type=float, required=True,
                    help="heater electrical watts, MEASURED (V^2/R at the real supply)")
    ap.add_argument("--ambient", type=float, default=None,
                    help="room degC; default = median of the baseline samples")
    ap.add_argument("--baseline-min", type=float, default=5.0)
    ap.add_argument("--heat-min", type=float, default=30.0)
    ap.add_argument("--decay-min", type=float, default=30.0)
    ap.add_argument("--fit-only", type=Path, default=None,
                    help="skip the run; fit this saved log file instead")
    args = ap.parse_args()

    from scripts.fit_rc import fit, load_rows        # after argparse: fast --help
    from scripts.plot_calibration import OUT_FILE as SVG_OUT, render_svg

    if args.fit_only is None:
        # ---- preconditions ------------------------------------------------
        st = api(args.gateway, "/api/hw/status")
        if not st.get("connected"):
            raise SystemExit(
                "No live node (GET /api/hw/status -> connected: false).\n"
                "Real rig: run the bring-up checklist in hardware/README.md.\n"
                "Rehearsal:  python -m scripts.mock_node")
        health = st.get("sensor_health") or {}
        if health.get("faults"):
            print(f"[warn] sensor health faults: {health['faults']} — a stuck/"
                  f"out-of-range sensor makes the fit garbage. Fix first if real.")
        if st.get("fan"):
            print("[warn] fan is commanded ON — clear the rig zone's constraints "
                  "(or POST /api/reset) so the whole run is fan-off.")
        print(f"[plan] baseline {args.baseline_min} min -> heater ON "
              f"{args.heat_min} min (duty cap will chop it — expected) -> "
              f"OFF, decay {args.decay_min} min. Total "
              f"{args.baseline_min + args.heat_min + args.decay_min:.0f} min. "
              f"Keep a hand near the physical switch.")

        wait_phase(args.gateway, "baseline", args.baseline_min)
        print("[step] heater ON")
        api(args.gateway, "/api/hw/heater", {"on": True})
        wait_phase(args.gateway, "heating", args.heat_min)
        print("[step] heater OFF")
        api(args.gateway, "/api/hw/heater", {"on": False})
        wait_phase(args.gateway, "decay", args.decay_min)

        log = api(args.gateway, "/api/hw/log?limit=8192", timeout=30.0)
        LOG_OUT.write_text(json.dumps(log, indent=1), encoding="utf-8")
        print(f"[save] {log.get('count')} rows -> {LOG_OUT}")
        log_path = LOG_OUT
    else:
        log_path = args.fit_only

    # ---- fit + artifacts --------------------------------------------------
    rows = load_rows(log_path)
    result = fit(rows, args.power, args.ambient, "measured", str(log_path))
    RESULTS_OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    f = result["fit"]
    print(f"[fit ] R={f['R_K_per_W']} K/W (UA={f['UA_W_per_K']} W/K)  "
          f"C={f['C_J_per_K']} J/K  tau={f['tau_s']}s  rmse={f['rmse_c']} degC")
    print(f"[save] {RESULTS_OUT}")
    SVG_OUT.write_text(render_svg(result), encoding="utf-8")
    print(f"[save] {SVG_OUT}  (clean chart: kind=measured)")
    print("[next] team eyeballs the overlay -> flip the calibration capability "
          "flag -> python -m scripts.update_docs")


if __name__ == "__main__":
    main()
