"""Download and rebuild data/uci_occupancy.csv from the UCI repository.

    python -m scripts.fetch_datasets            # writes data/uci_occupancy.csv
    python -m scripts.fetch_datasets --check    # verify the bundled file only

Source: UCI ML Repository dataset 357, "Occupancy Detection" (Candanedo &
Feldheim, 2016), CC BY 4.0. The zip holds three text files; this script merges
them chronologically into one CSV with a `split` column naming the original
file. Deterministic: the same zip always yields the same bytes.

Stdlib only (urllib + zipfile + csv). See data/README.md for provenance.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "uci_occupancy.csv"
URL = "https://archive.ics.uci.edu/static/public/357/occupancy+detection.zip"
FILES = [("datatraining.txt", "training"), ("datatest.txt", "test1"), ("datatest2.txt", "test2")]
HEADER = ["timestamp", "temp_c", "rh_pct", "light_lux", "co2_ppm",
          "humidity_ratio", "occupied", "split"]


def build(zip_bytes: bytes) -> list:
    rows = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = {Path(n).name: n for n in z.namelist()}
        for fname, split in FILES:
            if fname not in names:
                raise SystemExit(f"{fname} missing from the archive")
            with z.open(names[fname]) as fh:
                reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"))
                next(reader)                                   # header
                for r in reader:
                    if len(r) < 8:
                        continue
                    rows.append([r[1].strip('"'), r[2], r[3], r[4], r[5], r[6], r[7], split])
    rows.sort(key=lambda r: r[0])
    return rows


def write(rows: list, out: Path = OUT) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)


def check(out: Path = OUT) -> int:
    if not out.is_file():
        print(f"missing: {out}")
        return 1
    with out.open(newline="", encoding="utf-8") as fh:
        r = csv.reader(fh)
        hdr = next(r)
        n = sum(1 for _ in r)
    ok = hdr == HEADER and n > 20000
    print(f"{out}: {n} rows, header {'ok' if hdr == HEADER else hdr}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify the bundled CSV only")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    print(f"downloading {URL} …")
    with urllib.request.urlopen(URL, timeout=60) as resp:
        data = resp.read()
    rows = build(data)
    write(rows)
    print(f"wrote {OUT} ({len(rows)} rows, {rows[0][0]} → {rows[-1][0]})")
    return check()


if __name__ == "__main__":
    sys.exit(main())
