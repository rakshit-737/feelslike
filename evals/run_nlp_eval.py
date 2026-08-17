"""NLP benchmark runner — the number almost no other team will have.

Usage:
  python -m evals.run_nlp_eval            # LLM if key present, else rules
  python -m evals.run_nlp_eval --rules    # force offline rules parser

Scores complaint-detection, zone extraction, issue extraction, MULTI-ZONE set
extraction and the clarification flag against gold labels in
evals/benchmark.json. Writes evals/results_nlp.json.

Splits: "dev" = cases the rules were tuned on. "heldout" and "heldout2" were
written as tests, never as tuning targets — those are the honest numbers for the
slide. Report them even when they are ugly; a benchmark you cannot fail is
worthless.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.parser import parse

HERE = Path(__file__).resolve().parent

# Metric keys in report order. "triple" is the pre-existing headline (detection
# + single zone + issue) and keeps its exact meaning so old runs stay comparable.
ROWS = [("Cases", "n"), ("Complaint detection", "det"), ("Zone extraction", "zone"),
        ("Issue extraction", "issue"), ("Zone set (multi)", "zoneset"),
        ("Clarification flag", "clarify"), ("Exact triple", "triple")]
KEYS = [k for _l, k in ROWS]


def gold_zone_ids(g: dict) -> list:
    """Gold multi-zone list, falling back to the single-zone label for old cases."""
    if "zone_ids" in g:
        return list(g["zone_ids"])
    return [g["zone_id"]] if g.get("zone_id") else []


def gold_clarify(g: dict) -> bool:
    """Gold clarification flag.

    Explicit when the case labels it. Otherwise DERIVED from the contract rule
    "an intent with no resolvable zone must ask": a complaint with no gold zone
    needs clarification, anything else does not. Derivation keeps the 50 frozen
    cases usable for this metric without relabelling them.
    """
    if "requires_clarification" in g:
        return bool(g["requires_clarification"])
    return bool(g["is_comfort_complaint"]) and not gold_zone_ids(g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true", help="force rules fallback")
    args = ap.parse_args()

    cases = json.loads((HERE / "benchmark.json").read_text(encoding="utf-8"))

    splits: dict = {}
    failures = []
    for case in cases:
        got, source, _ms = parse(case["text"], force_rules=args.rules)
        g = case["gold"]
        ok = {
            "det": got.is_comfort_complaint == g["is_comfort_complaint"],
            "zone": got.zone_id == g["zone_id"],
            "issue": (not g["is_comfort_complaint"]) or (got.issue == g["issue"]),
            "zoneset": list(got.zone_ids) == gold_zone_ids(g),
            "clarify": bool(got.requires_clarification) == gold_clarify(g),
        }
        ok["triple"] = ok["det"] and ok["zone"] and ok["issue"]
        s = splits.setdefault(case.get("split", "dev"), {k: 0 for k in KEYS})
        s["n"] += 1
        for k, v in ok.items():
            s[k] += v
        if not all(ok.values()):
            failures.append({"text": case["text"], "split": case.get("split", "dev"),
                             "gold": g, "got": got.model_dump(), "source": source,
                             "missed": [k for k, v in ok.items() if not v and k != "triple"]})

    total = {k: 0 for k in KEYS}
    for s in splits.values():
        for k in total:
            total[k] += s[k]

    print(f"\nParser mode: {'rules (offline)' if args.rules else 'auto (llm if key set)'}")
    print(f"{'':<22}" + "".join(f"{name:>14}" for name in [*splits, "TOTAL"]))
    for label, key in ROWS:
        cells = [*splits.values(), total]
        if key == "n":
            print(f"{label:<22}" + "".join(f"{c['n']:>14}" for c in cells))
        else:
            print(f"{label:<22}" + "".join(
                f"{c[key]}/{c['n']} ({100 * c[key] / c['n']:.0f}%)".rjust(14)
                for c in cells))
    if failures:
        print(f"\n{len(failures)} failure(s) — show these honestly on the slide:")
        for f in failures[:16]:
            print(f"  - [{f['split']}] {f['text']!r} missed={f['missed']}: "
                  f"gold={{zone_ids:{gold_zone_ids(f['gold'])}, issue:{f['gold']['issue']}, "
                  f"complaint:{f['gold']['is_comfort_complaint']}, "
                  f"clarify:{gold_clarify(f['gold'])}}} got="
                  f"{{zone_ids:{f['got']['zone_ids']}, issue:{f['got']['issue']}, "
                  f"complaint:{f['got']['is_comfort_complaint']}, "
                  f"clarify:{f['got']['requires_clarification']}}}")

    (HERE / "results_nlp.json").write_text(json.dumps(
        {"splits": splits, "total": total, "failures": failures}, indent=2))
    print(f"\nSaved -> {HERE / 'results_nlp.json'}")


if __name__ == "__main__":
    main()
