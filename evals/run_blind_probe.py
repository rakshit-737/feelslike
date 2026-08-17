"""Blind-probe runner — the honest NLP number.

Scores the parser against evals/blind_probe.json, a set of cases that were never
used to develop the parser. The gap between this score and the benchmark's
held-out score is the measurement of how much of that held-out score is real
generalization versus development-against-the-test-set.

Report THIS number publicly. Writes evals/results_blind.json.

Usage:
  python -m evals.run_blind_probe            # LLM if a key is set, else rules
  python -m evals.run_blind_probe --rules    # force the offline rules parser
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from backend.parser import parse

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true", help="force rules fallback")
    ap.add_argument("--quiet", action="store_true", help="totals only")
    args = ap.parse_args()

    doc = json.loads((HERE / "blind_probe.json").read_text(encoding="utf-8"))
    cases = doc["cases"]
    n = len(cases)

    tally = {"det": 0, "zoneset": 0, "issue": 0, "triple": 0}
    by_category: dict = defaultdict(lambda: {"n": 0, "triple": 0})
    failures = []

    for case in cases:
        got, source, _ms = parse(case["text"], force_rules=args.rules)
        g = case["gold"]
        ok = {
            "det": got.is_comfort_complaint == g["is_comfort_complaint"],
            "zoneset": list(got.zone_ids) == list(g["zone_ids"]),
            "issue": (not g["is_comfort_complaint"]) or (got.issue == g["issue"]),
        }
        ok["triple"] = all(ok.values())
        for k, v in ok.items():
            tally[k] += v
        cat = by_category[case.get("category", "uncategorised")]
        cat["n"] += 1
        cat["triple"] += ok["triple"]
        if not ok["triple"]:
            failures.append({
                "text": case["text"], "category": case.get("category", ""),
                "gold": g, "got": {"is_comfort_complaint": got.is_comfort_complaint,
                                   "zone_ids": list(got.zone_ids), "issue": got.issue},
                "missed": [k for k, v in ok.items() if not v and k != "triple"],
                "source": source,
            })

    mode = "rules (offline)" if args.rules else "auto (llm if key set)"
    print(f"\nBLIND PROBE — cases the parser has never been developed against")
    print(f"Parser mode: {mode}   Cases: {n}")
    for label, key in (("Complaint detection", "det"), ("Zone set (multi)", "zoneset"),
                       ("Issue extraction", "issue"), ("Exact triple", "triple")):
        print(f"  {label:<22}{tally[key]}/{n}  ({100 * tally[key] / n:.0f}%)")

    if not args.quiet:
        print("\nBy category:")
        for cat, v in sorted(by_category.items()):
            print(f"  {cat:<26}{v['triple']}/{v['n']}")
        if failures:
            print(f"\n{len(failures)} failure(s) — these are the real remaining gaps:")
            for f in failures:
                print(f"  [{f['category']}] {f['text']!r}")
                print(f"      missed {f['missed']}: gold zones={f['gold']['zone_ids']} "
                      f"issue={f['gold']['issue']} complaint={f['gold']['is_comfort_complaint']}")
                print(f"      got    zones={f['got']['zone_ids']} "
                      f"issue={f['got']['issue']} complaint={f['got']['is_comfort_complaint']}")

    out = {"n": n, "mode": mode, "tally": tally,
           "pct": {k: round(100 * v / n) for k, v in tally.items()},
           "by_category": dict(by_category), "failures": failures}
    (HERE / "results_blind.json").write_text(json.dumps(out, indent=2))
    print(f"\nSaved -> {HERE / 'results_blind.json'}")


if __name__ == "__main__":
    main()
