"""NLP benchmark runner — the number almost no other team will have.

Usage:
  python -m evals.run_nlp_eval            # LLM if key present, else rules
  python -m evals.run_nlp_eval --rules    # force offline rules parser

Scores complaint-detection, zone extraction, and issue extraction against
gold labels in evals/benchmark.json. Writes evals/results_nlp.json.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.parser import parse

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true", help="force rules fallback")
    args = ap.parse_args()

    cases = json.loads((HERE / "benchmark.json").read_text(encoding="utf-8"))

    # Score per split: "dev" = cases the rules were tuned on; "heldout" = never
    # used for tuning. The heldout number is the honest one for the slide.
    splits: dict = {}
    failures = []
    for case in cases:
        got, source, _ms = parse(case["text"], force_rules=args.rules)
        g = case["gold"]
        ok_det = got.is_comfort_complaint == g["is_comfort_complaint"]
        ok_zone = got.zone_id == g["zone_id"]
        ok_issue = (not g["is_comfort_complaint"]) or (got.issue == g["issue"])
        s = splits.setdefault(case.get("split", "dev"),
                              {"n": 0, "det": 0, "zone": 0, "issue": 0, "triple": 0})
        s["n"] += 1
        s["det"] += ok_det
        s["zone"] += ok_zone
        s["issue"] += ok_issue
        s["triple"] += ok_det and ok_zone and ok_issue
        if not (ok_det and ok_zone and ok_issue):
            failures.append({"text": case["text"], "split": case.get("split", "dev"),
                             "gold": g, "got": got.model_dump(), "source": source})

    total = {"n": 0, "det": 0, "zone": 0, "issue": 0, "triple": 0}
    for s in splits.values():
        for k in total:
            total[k] += s[k]

    print(f"\nParser mode: {'rules (offline)' if args.rules else 'auto (llm if key set)'}")
    print(f"{'':<22}" + "".join(f"{name:>14}" for name in [*splits, "TOTAL"]))
    rows = [("Cases", "n"), ("Complaint detection", "det"),
            ("Zone extraction", "zone"), ("Issue extraction", "issue"),
            ("Exact triple", "triple")]
    for label, key in rows:
        cells = [*splits.values(), total]
        if key == "n":
            print(f"{label:<22}" + "".join(f"{c['n']:>14}" for c in cells))
        else:
            print(f"{label:<22}" + "".join(
                f"{c[key]}/{c['n']} ({100 * c[key] / c['n']:.0f}%)".rjust(14)
                for c in cells))
    if failures:
        print(f"\n{len(failures)} failure(s) — show these honestly on the slide:")
        for f in failures[:12]:
            print(f"  - [{f['split']}] {f['text']!r}: gold={f['gold']} got="
                  f"{{zone:{f['got']['zone_id']}, issue:{f['got']['issue']}, "
                  f"complaint:{f['got']['is_comfort_complaint']}}}")

    (HERE / "results_nlp.json").write_text(json.dumps(
        {"splits": splits, "total": total, "failures": failures}, indent=2))
    print(f"\nSaved -> {HERE / 'results_nlp.json'}")


if __name__ == "__main__":
    main()
