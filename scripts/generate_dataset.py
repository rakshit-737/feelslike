"""Generate the FeelsLike historical dataset (SIMULATED history) into SQLite.

    python -m scripts.generate_dataset                      # defaults (data/dataset/config.json if present)
    python -m scripts.generate_dataset --days 90 --step-min 5 --seed 42
    python -m scripts.generate_dataset --season winter --start 2026-01-05 --days 30
    python -m scripts.generate_dataset --buildings office:2,mall:1,data_center:1 --anomaly-rate 0.05
    python -m scripts.generate_dataset --quick              # 3 days, fast smoke run

Prints summary statistics and the major correlations when finished.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from backend.dataset.analysis import summary
from backend.dataset.config import BuildingSpec, load_config
from backend.dataset.generator import generate
from backend.dataset.store import SqliteStore


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--start")
    ap.add_argument("--days", type=int)
    ap.add_argument("--step-min", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--season")
    ap.add_argument("--climate")
    ap.add_argument("--buildings", help="type:floors[:pv_kwp],... e.g. office:2:15,mall:1")
    ap.add_argument("--anomaly-rate", type=float)
    ap.add_argument("--noise-level", type=float)
    ap.add_argument("--missing-rate", type=float)
    ap.add_argument("--no-noise", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args(argv)

    ov = {k: v for k, v in {"start": a.start, "days": a.days, "step_min": a.step_min, "seed": a.seed,
                             "season": a.season, "climate": a.climate, "noise_level": a.noise_level,
                             "db_path": a.out}.items() if v is not None}
    if a.quick:
        ov.setdefault("days", 3)
    cfg = load_config(a.config, ov)
    if a.buildings:
        specs = []
        for i, part in enumerate(a.buildings.split(",")):
            bits = part.split(":")
            specs.append(BuildingSpec(f"bldg-{bits[0]}-{i + 1:02d}", bits[0], int(bits[1]) if len(bits) > 1 else 1,
                                      pv_kwp=float(bits[2]) if len(bits) > 2 else 0.0))
        cfg.buildings = specs
    if a.anomaly_rate is not None:
        cfg.anomalies.rate_per_building_day = a.anomaly_rate
    if a.missing_rate is not None:
        cfg.quality.missing_data_rate = a.missing_rate
    if a.no_noise:
        cfg.quality.noise_enabled = False
    errs = cfg.validate()
    if errs:
        print("invalid config:", *errs, sep="\n  ")
        return 2

    store = SqliteStore(cfg.db_path)
    store.create()
    t = time.time()
    meta = generate(cfg, store, progress=lambda s: print(f"  [{time.time() - t:6.1f}s] {s}", flush=True))
    print(f"\nwrote {cfg.db_path} in {time.time() - t:.1f}s  rows={meta['row_counts']}  anomalies={meta['anomalies']}")
    print(json.dumps(summary(SqliteStore(cfg.db_path)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
