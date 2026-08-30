"""A software ESP32: the firmware's exact wire behaviour, no hardware.

    python -m scripts.mock_node                       # against localhost:8000
    python -m scripts.mock_node --gateway http://192.168.1.7:8000 --seconds 120

WHAT THIS IS. The A5(b) fallback made runnable, and the hardware-day rehearsal
tool: it speaks the frozen /api/hw/reading contract exactly like
hardware/firmware/feelslike_node/feelslike_node.ino, wrapped around a simulated
shoebox (first-order RC, the same model scripts/fit_rc.py fits). With this
running, the dashboard's Physical-zone card is live, the demo moment
(complaint -> fan command) is rehearsable end to end, and a full calibration
run can be exercised before a single wire exists.

FIRMWARE-FAITHFUL, deliberately:
  - actuators "off" at boot; commands adopted only from a good (HTTP 200,
    ok=true) response;
  - watchdog: no good response for watchdog_s -> fan and heater forced off
    locally, exactly like the sketch (so killing the server mid-run
    demonstrates safety layer 1 without hardware);
  - adopts poll_s / watchdog_s from the response within the same sanity bounds.

THE BOX PHYSICS (what the "sensor" reads):
    C dT/dt = UA (T_amb - T) - UA_FAN * fan_level * (T - T_amb) + P * heater
i.e. the fan mixes box air with ambient (extra conductance per level), the
heater injects P watts, and R = 1/UA. Defaults match fit_rc's self-test truth
(R 1.5 K/W, C 400 J/K, P 10 W), so a calibration rehearsal against this node
should recover approximately those values — approximately, not exactly,
because the fitter's model ignores the fan term on purpose (the protocol says
fan off during calibration; rows with fan on are excluded from the fit).

--fast N accelerates the BOX (N physics-seconds per wall-second) for quick
demos. WARNING printed when used: timestamps stay wall-clock, so a calibration
fit against a --fast log recovers tau/N, not tau. Never fit a --fast log.

Stdlib only. Ctrl+C to stop (prints a summary).
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.error
import urllib.request

BOUNDS_POLL_S = (0.5, 60.0)          # same sanity bounds as the firmware
BOUNDS_WATCHDOG_S = (1.0, 120.0)
UA_FAN_PER_LEVEL = 0.8               # W/K of extra box<->ambient mixing per fan level


def post_json(url: str, payload: dict, timeout: float = 1.5) -> dict | None:
    """One POST; None on any transport/HTTP/parse failure (firmware: no update)."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--node-id", default="mock-shoebox")
    ap.add_argument("--ambient", type=float, default=26.0, help="room degC around the box")
    ap.add_argument("--r", type=float, default=1.5, help="box R in K/W (truth)")
    ap.add_argument("--c", type=float, default=400.0, help="box C in J/K (truth)")
    ap.add_argument("--power", type=float, default=10.0, help="heater W (truth)")
    ap.add_argument("--noise", type=float, default=0.05, help="sensor noise degC RMS")
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long (0 = forever)")
    ap.add_argument("--fast", type=float, default=1.0,
                    help="physics acceleration; >1 breaks calibration fits (warned)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.fast != 1.0:
        print(f"[warn] --fast {args.fast}: box runs {args.fast}x wall clock. "
              f"Fine for demo/UI; NEVER fit a calibration against this log.")

    rng = random.Random(args.seed)
    url = args.gateway.rstrip("/") + "/api/hw/reading"
    T = args.ambient                      # box air starts settled at ambient
    fan, heater = 0, False                # boot-safe, like the firmware
    poll_s, watchdog_s = 2.0, 10.0
    last_good = None                      # wall time of last good response
    tripped = False
    seq, good, bad = 0, 0, 0
    t0 = time.time()
    print(f"[boot] mock node '{args.node_id}' -> {url}  "
          f"(truth R={args.r} K/W, C={args.c} J/K, P={args.power} W, tau={args.r * args.c:.0f}s)")

    try:
        while True:
            now = time.time()
            if args.seconds and now - t0 >= args.seconds:
                break
            # ---- watchdog (safety layer 1, faithfully) --------------------
            fresh = last_good is not None and (now - last_good) < watchdog_s
            if not fresh and (fan or heater):
                fan, heater = 0, False
                if not tripped:
                    print(f"[wdog] TRIP: no good response for > {watchdog_s:.0f}s — actuators OFF")
                    tripped = True
            # ---- physics step over one poll interval ----------------------
            dt = poll_s * args.fast
            ua = 1.0 / args.r + UA_FAN_PER_LEVEL * fan
            q = ua * (args.ambient - T) + args.power * (1.0 if (heater and fresh) else 0.0)
            T += dt * q / args.c
            reading = round(T + rng.gauss(0.0, args.noise), 2)
            rh = round(55.0 + rng.gauss(0.0, 1.0), 1)
            # ---- the poll --------------------------------------------------
            out = post_json(url, {"node_id": args.node_id, "temp_c": reading,
                                  "rh_pct": rh, "seq": seq,
                                  "uptime_s": round(now - t0, 1)})
            seq += 1
            if out and out.get("ok") is True:
                good += 1
                last_good = now
                new_fan = max(0, min(2, int(out.get("fan", 0))))
                new_heater = bool(out.get("heater", False))
                if (new_fan, new_heater) != (fan, heater):
                    print(f"[cmd ] fan={new_fan} heater={new_heater}")
                fan, heater = new_fan, new_heater
                w = float(out.get("watchdog_s", watchdog_s) or watchdog_s)
                p = float(out.get("poll_s", poll_s) or poll_s)
                if BOUNDS_WATCHDOG_S[0] <= w <= BOUNDS_WATCHDOG_S[1]:
                    watchdog_s = w
                if BOUNDS_POLL_S[0] <= p <= BOUNDS_POLL_S[1]:
                    poll_s = p
                if tripped:
                    print("[wdog] recovered — commands re-enabled")
                    tripped = False
                if seq % 15 == 0:
                    print(f"[read] temp={reading:.2f} rh={rh:.0f} fan={fan} "
                          f"heater={heater} (box true {T:.2f})")
            else:
                bad += 1
                if bad % 5 == 1:
                    print(f"[post] seq={seq - 1} failed (server down / bad reply); "
                          f"watchdog counting")
            time.sleep(poll_s)
    except KeyboardInterrupt:
        pass
    print(f"[done] {seq} polls, {good} good, {bad} failed, "
          f"box ended at {T:.2f} degC (ambient {args.ambient})")


if __name__ == "__main__":
    main()
