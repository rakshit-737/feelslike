"""Reproducibility artifact for the what-if engine: run scenarios, print a
comparison table, write evals/results_whatif.json.

Why a script and not a notebook: the report needs a number anyone can regenerate
with one command, and the number has to come from the same code path the live
dashboard would call (backend.whatif.compare), not from a parallel copy.

The "live" building the scenarios branch off is built deterministically here:
a DigitalTwin at the given seed, fast-forwarded to --start-hour, warmed up for
--warmup-h with the constraint-aware controller so temperatures are settled
rather than at the 28 degC cold start, then given two seeded complaints so the
constraint scenarios (ignore_complaint, complaint_expires) have something to act
on. Everything is a flag, so a reader can change the setup and rerun.

Usage:
  python -m scripts.run_experiments --scenario occupancy_up --seeds 3 --hours 6
  python -m scripts.run_experiments --scenario all --seeds 3 --hours 6
  python -m scripts.run_experiments --list
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from backend import whatif
from backend.constraints import Constraint, ConstraintStore
from backend.contracts import to_dict
from sim.controllers import ConstraintAware
from sim.twin import DigitalTwin

# Complaints seeded into the "live" store so constraint scenarios are not no-ops.
SEED_COMPLAINTS = [
    ("zone_b", "too_hot", 2, 0.9, "conference room is roasting", "priya"),
    ("zone_d", "too_cold", 1, 0.8, "lobby is freezing at the desk", "arun"),
]

# Metrics shown in the console table (the JSON carries all of METRIC_KEYS).
TABLE_METRICS = ("kwh", "viol_min", "hot_deg_min", "mean_temp", "mean_rh", "interventions")


def build_live(seed: int, start_hour: float, warmup_h: float,
               complaints: bool = True) -> tuple:
    """Construct the deterministic starting point every scenario branches from.

    INPUT: seed, start_hour (hours past Monday 00:00), warmup_h (simulated hours
      of settling), complaints (whether to seed SEED_COMPLAINTS).
    OUTPUT: (twin, store) — a stepped DigitalTwin and a populated ConstraintStore.
    SIDE EFFECTS: none outside the objects it creates.
    ERROR STATES: none.
    """
    twin = DigitalTwin(seed=seed)
    twin.t = float(start_hour) * 3600.0
    store = ConstraintStore()
    ctrl = ConstraintAware()
    for _ in range(int(round(warmup_h * 60))):
        sps, vents = ctrl.act(twin, store)
        twin.step(sps, vents)
    if complaints:
        for zone, issue, sev, conf, text, author in SEED_COMPLAINTS:
            store.add(Constraint.from_issue(zone, issue, sev, conf, twin.t, text, author))
    return twin, store


def _fmt(v) -> str:
    """Table cell: ints stay ints, floats get 2 dp, None becomes a dash."""
    if v is None:
        return "-"
    if isinstance(v, bool) or isinstance(v, int):
        return str(v)
    return f"{v:.2f}"


def print_table(rows: list, seeds: list, hours: float) -> None:
    """Console comparison table, ASCII only (the Windows console is cp1252).

    INPUT: rows = list of (key, compare_dict); the seeds and horizon used.
    OUTPUT: none (prints).
    SIDE EFFECTS: writes to stdout.
    ERROR STATES: none.
    """
    hdr = f"{'scenario':<22}{'metric':<18}{'baseline':>11}{'scenario':>11}{'delta':>10}{'pct':>9}"
    print(f"\nHorizon {hours:g} h  |  seeds {seeds}  |  control every "
          f"{whatif.CONTROL_INTERVAL_S:.0f} s  |  kind=measured")
    print(hdr)
    print("-" * len(hdr))
    for key, cmp_ in rows:
        first = True
        for m in TABLE_METRICS:
            d = cmp_["delta"][m]
            label = key if first else ""
            pct = "-" if d["pct"] is None else f"{d['pct']:+.1f}%"
            print(f"{label:<22}{m:<18}{_fmt(d['baseline']):>11}{_fmt(d['scenario']):>11}"
                  f"{_fmt(d['abs']):>10}{pct:>9}")
            first = False
        print(f"{'':<22}-> {cmp_['headline']}")
        print("-" * len(hdr))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run FeelsLike what-if scenarios.")
    ap.add_argument("--scenario", default="all",
                    help="a SCENARIOS key, or 'all' (default)")
    ap.add_argument("--seeds", type=int, default=3,
                    help="how many seeds to run (default 3)")
    ap.add_argument("--seed-list", default="",
                    help="explicit comma-separated seeds; overrides --seeds")
    ap.add_argument("--hours", type=float, default=whatif.DEFAULT_HORIZON_H,
                    help="simulated hours per run")
    ap.add_argument("--base-seed", type=int, default=7,
                    help="first seed and the live twin's seed (default 7)")
    ap.add_argument("--start-hour", type=float, default=9.0,
                    help="sim clock the scenarios start from (default 09:00 Mon)")
    ap.add_argument("--warmup-h", type=float, default=2.0,
                    help="simulated hours of settling before the branch point")
    ap.add_argument("--no-complaints", action="store_true",
                    help="start with an empty constraint store")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--out", default="",
                    help="output json (default evals/results_whatif.json)")
    args = ap.parse_args()

    if args.list:
        for k, e in whatif.SCENARIOS.items():
            print(f"{k:<20} [{e['kind']:<10}] {e['label']}\n{'':<20} {e['help']}")
        return

    if args.seed_list.strip():
        seeds = [int(s) for s in args.seed_list.split(",") if s.strip()]
    else:
        seeds = [args.base_seed + i for i in range(max(1, args.seeds))]

    keys = list(whatif.SCENARIOS) if args.scenario == "all" else [args.scenario]
    unknown = [k for k in keys if k not in whatif.SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s) {unknown}; try --list")

    twin, store = build_live(args.base_seed, args.start_hour, args.warmup_h,
                             complaints=not args.no_complaints)
    fp_before = whatif.state_fingerprint(twin, store)

    rows, payload = [], {}
    t0 = time.time()
    for k in keys:
        spec = whatif.scenario_spec(k, horizon_h=args.hours, seeds=seeds)
        cmp_ = whatif.compare(twin, store, spec)
        rows.append((k, cmp_))
        payload[k] = {
            "label": whatif.SCENARIOS[k]["label"],
            "family": whatif.SCENARIOS[k]["kind"],
            "help": whatif.SCENARIOS[k]["help"],
            "spec": cmp_["spec"],
            "headline": cmp_["headline"],
            "baseline": to_dict(cmp_["baseline"]),
            "scenario": to_dict(cmp_["scenario"]),
            "delta": cmp_["delta"],
        }
    elapsed = time.time() - t0
    fp_after = whatif.state_fingerprint(twin, store)
    isolated = fp_after == fp_before

    print_table(rows, seeds, args.hours)
    print(f"{len(keys)} scenario(s) x (baseline + scenario) x {len(seeds)} seed(s) "
          f"in {elapsed:.2f} s")
    print(f"live state fingerprint unchanged: {isolated}  ({fp_before[:16]}...)")
    print(f"verify_isolation(): {whatif.verify_isolation(twin, store)}")
    print(whatif.CI95_NOTE)
    if not isolated:
        raise SystemExit("ISOLATION VIOLATED - scenario run mutated the live state")

    out = Path(args.out) if args.out else \
        Path(__file__).resolve().parent.parent / "evals" / "results_whatif.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "horizon_h": args.hours,
        "seeds": seeds,
        "base_seed": args.base_seed,
        "start_hour": args.start_hour,
        "warmup_h": args.warmup_h,
        "complaints": [] if args.no_complaints else [list(c) for c in SEED_COMPLAINTS],
        "control_interval_s": whatif.CONTROL_INTERVAL_S,
        "ci95_note": whatif.CI95_NOTE,
        "isolation_held": isolated,
        "elapsed_s": round(elapsed, 3),
        "scenarios": payload,
    }, indent=2))
    print(f"Saved -> {out}")


if __name__ == "__main__":
    main()
