"""Roles, permissions, users, sessions and principals.

Sessions are OPAQUE bearer tokens (secrets.token_urlsafe(32)), stored server-side only as their
SHA-256 digest, with an absolute expiry. They are revocable (logout) and never logged. Bearer
tokens travel in the Authorization header (no cookies), so classic CSRF does not apply.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.security.passwords import dummy_verify, hash_password, verify_password

P = {  # permission -> meaning
    "view:building": "building / zone / telemetry / latest / comfort / analytics state",
    "view:history": "historical dataset",
    "view:occupant": "occupant comfort view of permitted rooms",
    "complaint:submit": "submit a comfort complaint",
    "building:configure": "change any building profile field",
    "building:configure_energy": "change energy-related profile fields (priorities, energy modes)",
    "controller:objective": "change the controller objective",
    "controller:safety": "change the controller safety mode",
    "hvac:control": "zone HVAC control (zone-scoped)",
    "simulation:control": "simulation speed / conditions / scenario / simulated hardware / reset",
    "whatif:run": "run what-if / trade-off / comparison simulations on clones",
    "devices:view": "device registry (no key material)",
    "devices:manage": "register, disable, quarantine, rotate devices",
    "users:manage": "manage users and roles",
    "security:view": "security status, events, metrics",
    "privacy:export": "export occupant records",
    "privacy:redact": "redact occupant records",
}
ALL = set(P)
ROLES = {
    "admin": ALL,
    "facility_manager": {"view:building", "view:history", "view:occupant", "complaint:submit", "building:configure",
                         "building:configure_energy", "controller:objective", "controller:safety", "hvac:control",
                         "simulation:control", "whatif:run", "devices:view", "security:view", "privacy:export"},
    "energy_manager": {"view:building", "view:history", "view:occupant", "building:configure_energy",
                       "controller:objective", "whatif:run"},
    "hvac_operator": {"view:building", "view:occupant", "complaint:submit", "hvac:control"},
    "occupant": {"view:occupant", "complaint:submit"},
    "auditor": {"view:building", "view:history", "view:occupant", "devices:view", "security:view"},
}
ZONE_SCOPED_ROLES = {"hvac_operator", "occupant"}
ENERGY_FIELDS = {"comfort_priority", "energy_priority", "operating_mode"}
ENERGY_MODES = {"normal", "energy_saving", "comfort_priority", "peak_demand_reduction"}
_USER_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")


@dataclass
class User:
    username: str
    role: str
    password_hash: str
    zones: list | None = None          # None = all zones; hvac_operator must list its zones
    disabled: bool = False
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {"username": self.username, "role": self.role, "zones": self.zones, "disabled": self.disabled,
                "created_at": self.created_at}


@dataclass
class Principal:
    kind: str                           # user | development | anonymous | device
    username: str | None
    role: str | None
    zones: list | None = None
    authenticated: bool = False
    session_expires_at: float | None = None

    @property
    def permissions(self) -> set:
        return set(ROLES.get(self.role, set())) if self.role else set()

    def can(self, perm: str) -> bool:
        return perm in self.permissions

    def can_zone(self, zone: str) -> bool:
        if self.role in ZONE_SCOPED_ROLES:
            return self.zones is not None and zone in self.zones if self.role == "hvac_operator" else \
                (self.zones is None or zone in self.zones)
        return self.zones is None or zone in self.zones

    def public(self) -> dict:
        return {"kind": self.kind, "username": self.username, "role": self.role, "zones": self.zones,
                "authenticated": self.authenticated, "permissions": sorted(self.permissions),
                "session_expires_at": self.session_expires_at}


def validate_user_fields(username, role, zones) -> list:
    errs = []
    if not isinstance(username, str) or not _USER_RE.match(username):
        errs.append("username must match [a-z0-9_.-]{3,32}")
    if role not in ROLES:
        errs.append(f"role must be one of {sorted(ROLES)}")
    if zones is not None and (not isinstance(zones, list) or not all(isinstance(z, str) and re.match(r"^[a-z0-9_]{1,32}$", z) for z in zones)):
        errs.append("zones must be a list of zone ids")
    if role == "hvac_operator" and not zones:
        errs.append("hvac_operator requires at least one assigned zone")
    return errs


class UserStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.users: dict = {}

    def add(self, username: str, password: str, role: str, zones=None) -> User:
        errs = validate_user_fields(username, role, zones)
        if errs:
            raise ValueError(errs)
        u = User(username, role, hash_password(password), list(zones) if zones is not None else None)
        with self._lock:
            if username in self.users:
                raise ValueError(["user already exists"])
            self.users[username] = u
        return u

    def update(self, username: str, role=None, zones=..., disabled=None) -> User:
        with self._lock:
            u = self.users.get(username)
            if u is None:
                raise KeyError(username)
            new_role = role if role is not None else u.role
            new_zones = u.zones if zones is ... else zones
            errs = validate_user_fields(username, new_role, new_zones)
            if errs:
                raise ValueError(errs)
            u.role, u.zones = new_role, new_zones
            if disabled is not None:
                u.disabled = bool(disabled)
            return u

    def verify(self, username: str, password: str) -> User | None:
        u = self.users.get(str(username or ""))
        if u is None:
            dummy_verify(str(password or ""))
            return None
        ok = verify_password(str(password or ""), u.password_hash)
        return u if ok and not u.disabled else None

    def load_file(self, path) -> int:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        n = 0
        for rec in data.get("users", []):
            errs = validate_user_fields(rec.get("username"), rec.get("role"), rec.get("zones"))
            if errs or not str(rec.get("password_hash", "")).startswith("scrypt$"):
                raise ValueError(f"invalid user record {rec.get('username')!r}: {errs or ['password_hash must be scrypt']}")
            self.users[rec["username"]] = User(rec["username"], rec["role"], rec["password_hash"], rec.get("zones"),
                                                bool(rec.get("disabled", False)), float(rec.get("created_at", time.time())))
            n += 1
        return n


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    def __init__(self, ttl_s: float = 3600.0, clock=time.time):
        self._lock = threading.Lock()
        self._sessions: dict = {}
        self.ttl_s = float(ttl_s)
        self._now = clock

    def issue(self, user: User) -> tuple:
        token = secrets.token_urlsafe(32)
        exp = self._now() + self.ttl_s
        with self._lock:
            self._sessions[_digest(token)] = {"username": user.username, "exp": exp, "created": self._now()}
            if len(self._sessions) > 10000:
                now = self._now()
                for k in [k for k, s in self._sessions.items() if s["exp"] < now]:
                    del self._sessions[k]
        return token, exp

    def resolve(self, token: str | None, users: UserStore) -> tuple:
        """-> (Principal | None, reason) reason in ok | missing | invalid | expired | disabled."""
        if not token:
            return None, "missing"
        with self._lock:
            s = self._sessions.get(_digest(token))
            if s is None:
                return None, "invalid"
            if s["exp"] < self._now():
                del self._sessions[_digest(token)]
                return None, "expired"
        u = users.users.get(s["username"])
        if u is None or u.disabled:
            return None, "disabled"
        return Principal("user", u.username, u.role, u.zones, True, s["exp"]), "ok"

    def revoke(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(_digest(token), None) is not None

    def revoke_user(self, username: str) -> int:
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s["username"] == username]
            for k in keys:
                del self._sessions[k]
        return len(keys)

    def active(self) -> int:
        now = self._now()
        with self._lock:
            return sum(1 for s in self._sessions.values() if s["exp"] >= now)
