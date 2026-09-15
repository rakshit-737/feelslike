"""Manage the FeelsLike users file (scrypt hashes only; never plaintext).

    python -m scripts.security_users add  --file data/security/users.json --user fm1 --role facility_manager
    python -m scripts.security_users add  --file data/security/users.json --user op-a --role hvac_operator --zones zone_a
    python -m scripts.security_users list --file data/security/users.json

The password is read from the FL_NEW_PASSWORD environment variable or prompted without echo; it is
never accepted as a command-line argument (shell history) and never printed. data/security/ is
git-ignored. Point the server at the file with FL_USERS_FILE.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from backend.security.auth import ROLES, validate_user_fields
from backend.security.passwords import hash_password


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--file", required=True)
    a.add_argument("--user", required=True)
    a.add_argument("--role", required=True, choices=sorted(ROLES))
    a.add_argument("--zones", help="comma list of zone ids (required for hvac_operator)")
    ls = sub.add_parser("list")
    ls.add_argument("--file", required=True)
    args = ap.parse_args(argv)
    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"users": []}
    if args.cmd == "list":
        for u in data["users"]:
            print(f"{u['username']:<20} {u['role']:<18} zones={u.get('zones')} disabled={u.get('disabled', False)}")
        return 0
    zones = [z.strip() for z in args.zones.split(",")] if args.zones else None
    errs = validate_user_fields(args.user, args.role, zones)
    if any(u["username"] == args.user for u in data["users"]):
        errs.append("user already exists")
    if errs:
        print("error:", "; ".join(errs), file=sys.stderr)
        return 2
    pw = os.environ.get("FL_NEW_PASSWORD") or getpass.getpass("password (min 12 chars): ")
    try:
        h = hash_password(pw)
    except ValueError as e:
        print("error:", e, file=sys.stderr)
        return 2
    data["users"].append({"username": args.user, "role": args.role, "zones": zones, "password_hash": h,
                          "disabled": False, "created_at": time.time()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"added {args.user} ({args.role}) to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
