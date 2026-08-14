"""Evidence generator: run all controllers over the SAME 7 simulated days and
print the energy/comfort comparison table. This is the number on your pitch slide.

Usage:  python -m scripts.demo_day  [--days 7] [--seed 7]
Writes: evals/results_energy.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sim.controllers import ConstraintAware, ReactiveComfort, StaticSchedule
from sim.twin import DigitalTwin


def run(controller, days: int, seed: int, store=None) -> dict:
    twin = DigitalTwin(seed=seed)
    steps = int(days * 86400 // 60)
    for _ in range(steps):
        sps, vents = controller.act(twin, store)
        twin.step(sps, vents)
    return twin.metrics()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    controllers = [StaticSchedule(), ReactiveComfort(), ConstraintAware()]
    try:  # include RL agent if a trained model exists
        from sim.controllers import RLPolicy
        controllers.append(RLPolicy())
    except Exception:
        pass

    results = {}
    for c in controllers:
        results[c.name] = run(c, args.days, args.seed)

    base = results[StaticSchedule.name]["kwh"]
    hdr = f"{'Controller':<38}{'kWh':>8}{'Rs':>9}{'kgCO2':>8}{'viol-min':>10}{'hot dgm':>9}{'cold dgm':>9}{'saved':>8}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for name, m in results.items():
        saved = 100.0 * (base - m["kwh"]) / base if base else 0.0
        m["saved_pct_vs_baseline"] = round(saved, 1)
        print(f"{name:<38}{m['kwh']:>8.1f}{m['cost_rs']:>9.0f}{m['co2_kg']:>8.1f}"
              f"{m['viol_min']:>10.0f}{m['hot_deg_min']:>9.0f}{m['cold_deg_min']:>9.0f}"
              f"{saved:>7.1f}%")

    out = Path(__file__).resolve().parent.parent / "evals" / "results_energy.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"days": args.days, "seed": args.seed,
                               "results": results}, indent=2))
    print(f"\nSaved -> {out}")


if __name__ == "__main__":
    main()
