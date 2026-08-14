"""Ablation table: RL agent vs rules vs baselines across N random days.
This is the 'is the RL actually learning?' proof for technical judges.

Run:  python -m rl.evaluate --episodes 10
"""
from __future__ import annotations

import argparse
import statistics as st

from sim.controllers import ConstraintAware, ReactiveComfort, StaticSchedule
from scripts.demo_day import run


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=10)
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args()

    controllers = [StaticSchedule(), ReactiveComfort(), ConstraintAware()]
    try:
        from sim.controllers import RLPolicy
        controllers.append(RLPolicy())
    except Exception as e:
        print(f"(RL model not available: {e} — run python -m rl.train first)")

    print(f"\n{'Controller':<38}{'kWh mean':>10}{'kWh sd':>8}{'viol-min mean':>15}")
    print("-" * 71)
    for c in controllers:
        kwhs, viols = [], []
        for ep in range(args.episodes):
            m = run(c, args.days, seed=100 + ep)
            kwhs.append(m["kwh"])
            viols.append(m["viol_min"])
        print(f"{c.name:<38}{st.mean(kwhs):>10.1f}{(st.stdev(kwhs) if len(kwhs) > 1 else 0):>8.1f}"
              f"{st.mean(viols):>15.0f}")


if __name__ == "__main__":
    main()
