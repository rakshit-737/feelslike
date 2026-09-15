"""Central security configuration. Nothing else reads FL_* security variables.

MODES
  development  (default) authentication NOT enforced: requests without a token act as an explicit
               development principal (role FL_DEV_ROLE, default admin) and every response carries
               X-FeelsLike-Security: development. Tokens, if sent, are still validated and roles
               still applied. Device signatures are verified when present, not required. Plain HTTP.
               LOCAL / DEVELOPMENT ONLY — nothing is encrypted in transit.
  enforced     authentication and authorization enforced, device authentication required, restrictive
               CORS. HTTP allowed (a local demo of the security model, still not encrypted).
  production   enforced + HTTPS required + TLS material or an upstream-terminated TLS declaration +
               explicit origins + a users file + no legacy unauthenticated device endpoints.
               The app refuses to start if production requirements are not met.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODES = ("development", "enforced", "production")
DEV_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def _bool(v, default=False):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SecurityConfig:
    mode: str = "development"
    https_required: bool = False
    allowed_origins: list = field(default_factory=list)
    token_ttl_s: float = 3600.0
    telemetry_freshness_s: float = 300.0
    stale_accept_s: float = 86400.0
    max_future_s: float = 60.0
    replay_window: int = 1024
    quarantine_after_failures: int = 5
    quarantine_window_s: float = 300.0
    rate_limits: dict = field(default_factory=lambda: {
        "login": (10, 60.0), "telemetry": (600, 60.0), "control": (60, 60.0), "admin": (60, 60.0)})
    audit_enabled: bool = True
    audit_log_path: str | None = None
    users_file: str | None = None
    device_keys_file: str | None = None
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
    tls_terminated_upstream: bool = False
    dev_role: str = "admin"
    slack_signing_secret_set: bool = False

    @property
    def auth_enabled(self) -> bool:
        return self.mode != "development"

    @property
    def device_auth_required(self) -> bool:
        return self.mode != "development"

    @property
    def legacy_device_endpoints(self) -> bool:
        """/api/hw/reading and /api/hw/sensor without signatures (existing firmware)."""
        return self.mode == "development"

    @classmethod
    def from_env(cls, env=None) -> "SecurityConfig":
        e = os.environ if env is None else env
        mode = (e.get("FL_SECURITY_MODE") or "development").strip().lower()
        origins = [o.strip() for o in (e.get("FL_ALLOWED_ORIGINS") or "").split(",") if o.strip()]
        c = cls(mode=mode, https_required=_bool(e.get("FL_HTTPS_REQUIRED"), mode == "production"),
                allowed_origins=origins, token_ttl_s=float(e.get("FL_TOKEN_TTL_S") or 3600),
                telemetry_freshness_s=float(e.get("FL_TELEMETRY_FRESHNESS_S") or 300),
                audit_enabled=_bool(e.get("FL_AUDIT_ENABLED"), True), audit_log_path=e.get("FL_AUDIT_LOG") or None,
                users_file=e.get("FL_USERS_FILE") or None, device_keys_file=e.get("FL_DEVICE_KEYS_FILE") or None,
                tls_cert_path=e.get("FL_TLS_CERT_PATH") or None, tls_key_path=e.get("FL_TLS_KEY_PATH") or None,
                tls_terminated_upstream=_bool(e.get("FL_TLS_TERMINATED_UPSTREAM")),
                dev_role=(e.get("FL_DEV_ROLE") or "admin"),
                slack_signing_secret_set=bool(e.get("FL_SLACK_SIGNING_SECRET")))
        if mode == "development":
            # still enforced, but sized for local automation (the test suite resets the building hundreds
            # of times a minute); enforced / production keep the strict defaults
            c.rate_limits = {"login": (30, 60.0), "telemetry": (6000, 60.0), "control": (6000, 60.0),
                             "admin": (600, 60.0)}
        return c

    def validate(self) -> list:
        errs = []
        from backend.security.auth import ROLES
        if self.mode not in MODES:
            errs.append(f"FL_SECURITY_MODE must be one of {MODES}")
        if self.dev_role not in ROLES:
            errs.append(f"FL_DEV_ROLE must be one of {tuple(ROLES)}")
        if not 60 <= self.token_ttl_s <= 86400:
            errs.append("FL_TOKEN_TTL_S must be between 60 and 86400")
        if any(o == "*" for o in self.allowed_origins) and self.mode != "development":
            errs.append("FL_ALLOWED_ORIGINS must not contain '*' outside development")
        if self.mode == "production":
            if not self.allowed_origins:
                errs.append("production requires FL_ALLOWED_ORIGINS (explicit dashboard origin)")
            if any(o.startswith("http://") for o in self.allowed_origins):
                errs.append("production origins must be https://")
            if not self.https_required:
                errs.append("production requires FL_HTTPS_REQUIRED=1")
            if not self.tls_terminated_upstream:
                for label, p in (("FL_TLS_CERT_PATH", self.tls_cert_path), ("FL_TLS_KEY_PATH", self.tls_key_path)):
                    if not p or not Path(p).is_file():
                        errs.append(f"production requires {label} to point to an existing file "
                                    "(or FL_TLS_TERMINATED_UPSTREAM=1 behind a TLS proxy)")
            if not self.users_file or not Path(self.users_file).is_file():
                errs.append("production requires FL_USERS_FILE (create users with scripts/security_users.py)")
        return errs

    def cors(self) -> dict:
        if self.mode == "development" and not self.allowed_origins:
            return {"allow_origins": [], "allow_origin_regex": DEV_ORIGIN_REGEX}
        return {"allow_origins": list(self.allowed_origins), "allow_origin_regex": None}

    def public(self) -> dict:
        """Safe to show an authorized security viewer: no secrets, no key material."""
        return {"mode": self.mode, "auth_enabled": self.auth_enabled, "https_required": self.https_required,
                "device_auth_required": self.device_auth_required, "legacy_device_endpoints": self.legacy_device_endpoints,
                "allowed_origins": self.allowed_origins or ([DEV_ORIGIN_REGEX] if self.mode == "development" else []),
                "token_ttl_s": self.token_ttl_s, "telemetry_freshness_s": self.telemetry_freshness_s,
                "stale_accept_s": self.stale_accept_s, "replay_window": self.replay_window,
                "quarantine_after_failures": self.quarantine_after_failures,
                "rate_limits": {k: {"requests": v[0], "per_s": v[1]} for k, v in self.rate_limits.items()},
                "audit_enabled": self.audit_enabled, "audit_file": bool(self.audit_log_path),
                "users_file_configured": bool(self.users_file), "device_keys_file_configured": bool(self.device_keys_file),
                "tls_configured": bool(self.tls_cert_path and self.tls_key_path) or self.tls_terminated_upstream,
                "slack_signing_secret_set": self.slack_signing_secret_set,
                "transport_note": ("DEVELOPMENT / LOCAL ONLY — plain HTTP, not encrypted in transit"
                                   if not self.https_required else "HTTPS required")}
