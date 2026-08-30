"""Pre-flight: every check from PRESENTATION_SCRIPT.md Appendix C, one command.

    python -m scripts.preflight                       # offline checks only
    python -m scripts.preflight --server http://127.0.0.1:8000
    python -m scripts.preflight --server http://127.0.0.1:8000 --exercise

Run it 15 minutes before presenting (and at the start of hardware day). Checks:

  offline   frozen headline numbers (scripts.demo_day reproduces 722.4/530.3/0),
            contract suite green, NLP dev split 30/30, every demo-critical file
            present (dashboard, panels, occupant page, firmware, feasibility,
            committed result JSONs).
  --server  /api/state serves every load-bearing block (meters.tou and hardware
            included), /api/rl trajectory available, /static/panels.js served,
            guided demo endpoint answers; reports (never fails on) whether a
            hardware node is live.
  --exercise  additionally posts one throwaway complaint and asserts the
            applied/parse round trip — MUTATES the live building (one
            constraint, decays in 2 h). Don't use it after you've warmed the
            demo state you intend to present.

Exit code 0 = all PASS. Anything else: fix before you walk on stage.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FROZEN = {"base_kwh": "722.4", "us_kwh": "530.3", "us_viol": 0}
MUST_EXIST = [
    "dashboard/index.html", "dashboard/panels.js", "dashboard/occupant.html",
    "hardware/firmware/feelslike_node/feelslike_node.ino", "hardware/README.md",
    "docs/FEASIBILITY.md", "docs/PRESENTATION_SCRIPT.md",
    "evals/results_energy.json", "evals/results_nlp.json",
    "evals/blind_probe.json", "rl/models/progress.csv",
]

results: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def run(mod: str, *extra: str, timeout: int = 300) -> str:
    p = subprocess.run([sys.executable, "-m", mod, *extra], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=timeout,
                       env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    return p.stdout + p.stderr


def get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read().decode()
    try:
        return r.status, json.loads(body)
    except ValueError:
        return r.status, body


def post(url: str, payload: dict, timeout: float = 20.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def offline_checks() -> None:
    print("[offline]")
    missing = [f for f in MUST_EXIST if not (ROOT / f).is_file()]
    check("demo-critical files present", not missing,
          ("missing: " + ", ".join(missing)) if missing else f"{len(MUST_EXIST)} files")

    out = run("scripts.demo_day")
    line = next((ln for ln in out.splitlines() if "constraint-aware" in ln), "")
    parts = line.split()
    # columns from the right: saved%, cold dgm, hot dgm, viol-min, kgCO2, Rs, kWh
    ok = (FROZEN["base_kwh"] in out and len(parts) >= 7
          and parts[-7] == FROZEN["us_kwh"]
          and int(float(parts[-4])) == FROZEN["us_viol"])
    check("frozen numbers reproduce (722.4 / 530.3 kWh, 0 viol-min)", ok,
          "" if ok else "OUTPUT CHANGED — do not present old numbers; investigate first")

    out = run("pytest", "tests/test_contract_conformance.py", "-q")
    ok = bool(re.search(r"\b49 passed\b", out)) and "failed" not in out
    check("contract suite green (49)", ok, "" if ok else out.strip().splitlines()[-1][:120])

    out = run("evals.run_nlp_eval", "--rules")
    ok = bool(re.search(r"Exact triple\s+30/30", out))
    check("NLP dev split 30/30 (rules, offline)", ok)


def server_checks(base: str, exercise: bool) -> None:
    print(f"[server] {base}")
    try:
        _, s = get(base + "/api/state")
    except (urllib.error.URLError, OSError) as e:
        check("GET /api/state", False, f"unreachable: {e}")
        return
    need = ["sim", "zones", "meters", "history", "feed", "controller",
            "decisions", "alerts", "privacy", "hardware", "health"]
    missing = [k for k in need if k not in s]
    check("/api/state carries every load-bearing block", not missing,
          ("missing: " + ", ".join(missing)) if missing else f"{len(need)} blocks")
    check("meters.tou present (ToD display)", isinstance(s.get("meters", {}).get("tou"), dict))
    check("5 zones with frozen keys", len(s.get("zones", [])) == 5
          and all(k in s["zones"][0] for k in ("id", "temp", "setpoint", "vent", "offset")))

    _, rl = get(base + "/api/rl")
    check("/api/rl trajectory available", rl.get("available") is True,
          "" if rl.get("available") else rl.get("note", ""))
    st, _ = get(base + "/static/panels.js")
    check("/static/panels.js served", st == 200)
    _, demo = get(base + "/api/demo")
    check("guided demo answers", isinstance(demo.get("total"), int) and demo["total"] > 0)

    hw = s.get("hardware") or {}
    print(f"  INFO  hardware node: "
          + (f"CONNECTED ({hw.get('node_id')}, {hw.get('stale_s')}s ago)"
             if hw.get("connected") else
             "not connected — all-sim demo path (fine; rehearse the no-rig variant)"))

    if exercise:
        _, r = post(base + "/api/complaint",
                    {"text": "preflight check: it's really stuffy in conference room b",
                     "author": "preflight"})
        check("complaint round trip (MUTATED live state)",
              r.get("action") == "applied" and r.get("parsed", {}).get("zone_id") == "zone_b",
              f"action={r.get('action')} source={r.get('source')} {r.get('latency_ms')}ms")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default=None, help="live server base URL to check too")
    ap.add_argument("--exercise", action="store_true",
                    help="with --server: post one throwaway complaint (mutates state)")
    ap.add_argument("--skip-offline", action="store_true")
    args = ap.parse_args()

    if not args.skip_offline:
        offline_checks()
    if args.server:
        server_checks(args.server.rstrip("/"), args.exercise)

    fails = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} checks passed"
          + (f" — FIX BEFORE PRESENTING: {', '.join(fails)}" if fails else " — go."))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
