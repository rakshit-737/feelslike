"""FastAPI enforcement.

Every route has an entry in POLICY (method, path template) -> permission | "public" | "session" |
"device". The global dependency `enforce` runs after routing for EVERY request:
  * an unlisted route is DENIED when authentication is enabled (fail closed; a test asserts every
    route is listed),
  * "device" routes authenticate the device inside the endpoint (signature / registry),
  * otherwise the bearer token is resolved to a Principal and the permission checked (401 / 403),
  * zone-scoped checks call require_zone() inside the endpoint with the zone from the server's own
    lookup, never trusting the client's claim alone.
Middleware adds a request id, security headers, the HTTPS redirect (when required) and a safe 500.
"""
from __future__ import annotations

import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.security.audit import AuditLog
from backend.security.auth import Principal, SessionStore, UserStore
from backend.security.config import SecurityConfig
from backend.security.devices import DeviceRegistry
from backend.security.ratelimit import RateLimiter

V, H, O = "view:building", "view:history", "view:occupant"
SIM, WHATIF = "simulation:control", "whatif:run"
POLICY = {
    ("GET", "/"): "public", ("GET", "/occupant"): "public",
    ("POST", "/api/security/login"): "public", ("GET", "/api/security/whoami"): "public",
    ("POST", "/api/security/logout"): "session",
    ("GET", "/api/state"): V, ("GET", "/api/decisions"): V, ("GET", "/api/decisions/{decision_id}"): V,
    ("GET", "/api/constraints"): V, ("GET", "/api/scenarios"): V, ("GET", "/api/analytics"): V,
    ("GET", "/api/maintenance"): V, ("GET", "/api/experiments"): V, ("GET", "/api/rl"): V,
    ("GET", "/api/hw/status"): V, ("GET", "/api/hw/log"): V, ("GET", "/api/hw/sensors"): V,
    ("GET", "/api/hw/sensors/{node_id}/log"): V, ("GET", "/api/telemetry"): V, ("GET", "/api/monitor"): V,
    ("GET", "/api/forecast"): V, ("GET", "/api/external"): V, ("GET", "/api/dataset"): V,
    ("GET", "/api/building"): V, ("GET", "/api/building/profile"): V, ("GET", "/api/building/zones/{zone_id}"): V,
    ("GET", "/api/building/demand"): V, ("GET", "/api/scenario"): V, ("GET", "/api/simhw"): V,
    ("GET", "/api/latest"): V, ("GET", "/api/latest/health"): V, ("GET", "/api/latest/sensors"): V,
    ("GET", "/api/latest/trend"): V, ("GET", "/api/latest/zone/{zone_id}"): V, ("GET", "/api/latest/{metric}"): V,
    ("GET", "/api/comfort"): V, ("GET", "/api/comfort/zone/{zone_id}"): V, ("GET", "/api/comfort/events"): V,
    ("GET", "/api/comfort/history"): V, ("GET", "/api/demo"): V,
    ("GET", "/api/history/catalog"): H, ("GET", "/api/history"): H, ("GET", "/api/history/compare"): H,
    ("GET", "/api/history/anomalies"): H, ("GET", "/api/history/quality"): H, ("GET", "/api/history/export"): H,
    ("GET", "/api/comfort/occupant"): O, ("GET", "/api/occupant/rooms"): O,
    ("POST", "/api/complaint"): "complaint:submit",
    ("POST", "/api/whatif"): WHATIF, ("GET", "/api/comfort/tradeoff"): WHATIF, ("GET", "/api/scenario/compare"): WHATIF,
    ("POST", "/api/speed"): SIM, ("POST", "/api/conditions"): SIM, ("POST", "/api/reset"): SIM,
    ("POST", "/api/scenario"): SIM, ("POST", "/api/scenario/reset"): SIM, ("POST", "/api/demo"): SIM,
    ("POST", "/api/simhw"): SIM, ("POST", "/api/simhw/devices/{device_id}/fault"): SIM,
    ("POST", "/api/simhw/devices/{device_id}/config"): SIM, ("POST", "/api/external/refresh"): SIM,
    ("POST", "/api/controller"): "session",                          # field-level checks in the endpoint
    ("POST", "/api/building/profile"): "building:configure_energy",   # field-level checks in the endpoint
    ("POST", "/api/constraints/{cid}/approve"): "hvac:control", ("POST", "/api/constraints/{cid}/reject"): "hvac:control",
    ("POST", "/api/hw/heater"): "hvac:control", ("POST", "/api/control/zone/{zone_id}"): "hvac:control",
    ("POST", "/api/hw/reading"): "device", ("POST", "/api/hw/sensor"): "device",
    ("POST", "/api/telemetry/ingest"): "device", ("POST", "/api/slack"): "device",
    ("GET", "/api/export"): "privacy:export", ("POST", "/api/redact"): "privacy:redact",
    ("GET", "/api/security/status"): "security:view", ("GET", "/api/security/events"): "security:view",
    ("GET", "/api/security/metrics"): "security:view", ("GET", "/api/security/devices"): "devices:view",
    ("POST", "/api/security/devices"): "devices:manage", ("POST", "/api/security/devices/{device_id}/{action}"): "devices:manage",
    ("GET", "/api/security/users"): "users:manage", ("POST", "/api/security/users"): "users:manage",
    ("POST", "/api/security/users/{username}"): "users:manage",
}
RATE_BUCKET = {SIM: "control", "hvac:control": "control", "building:configure_energy": "control",
               "devices:manage": "admin", "users:manage": "admin", "privacy:redact": "admin"}
RATE_METHODS = {"POST"}
# successful POSTs audited generically by the middleware (endpoints with richer audit records do their own)
AUDITED_POSTS = ("/api/speed", "/api/conditions", "/api/reset", "/api/scenario", "/api/simhw", "/api/demo",
                 "/api/external/refresh")


class SecurityState:
    def __init__(self, config: SecurityConfig | None = None):
        self.configure(config or SecurityConfig.from_env())

    def configure(self, config: SecurityConfig) -> None:
        self.config = config
        self.users = UserStore()
        self.sessions = SessionStore(config.token_ttl_s)
        self.audit = AuditLog(path=config.audit_log_path, enabled=config.audit_enabled)
        self.limiter = RateLimiter(config.rate_limits)
        if config.users_file:
            try:
                self.users.load_file(config.users_file)
            except (OSError, ValueError) as e:
                if config.mode != "development":
                    raise RuntimeError(f"cannot load FL_USERS_FILE: {type(e).__name__}") from None


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def bearer(request: Request) -> str | None:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def resolve_principal(state: SecurityState, request: Request) -> tuple:
    token = bearer(request)
    if token:
        p, reason = state.sessions.resolve(token, state.users)
        return p, reason
    if not state.config.auth_enabled:
        return Principal("development", "dev-anonymous", state.config.dev_role, None, False), "development"
    return None, "missing"


def make_enforce(state: SecurityState):
    def enforce(request: Request):
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        method = request.method.upper()
        if method == "HEAD":
            method = "GET"
        rid = request.scope.get("fl_request_id")
        perm = POLICY.get((method, path))
        if perm == "public":
            request.state.principal = resolve_principal(state, request)[0] or Principal("anonymous", None, None)
            return
        if perm == "device":
            request.state.principal = Principal("device", None, None)
            return
        principal, reason = resolve_principal(state, request)
        if principal is None:
            if reason in ("invalid", "expired", "disabled") or state.config.auth_enabled:
                # no credentials at all is not a failed authentication attempt; keep the two counts apart
                state.audit.record("auth_failure" if reason != "missing" else "unauthenticated_request", None,
                                   f"{method} {path}", result="rejected",
                                   reason=f"token {reason}", request_id=rid, ip=client_ip(request))
                raise HTTPException(401, "Authentication required.",
                                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"' if reason != "missing" else "Bearer"})
        request.state.principal = principal
        if perm is None:
            if state.config.auth_enabled:
                state.audit.record("authorization_failure", principal.username, f"{method} {path}",
                                   result="rejected", reason="no policy for route (fail closed)", request_id=rid)
                raise HTTPException(403, "Not permitted.")
            return
        if perm != "session" and not principal.can(perm):
            state.audit.record("authorization_failure", principal.username, f"{method} {path}", result="rejected",
                               reason=f"role {principal.role} lacks {perm}", request_id=rid)
            raise HTTPException(403, "Not permitted.")
        if perm == "session" and state.config.auth_enabled and not principal.authenticated:
            raise HTTPException(401, "Authentication required.", headers={"WWW-Authenticate": "Bearer"})
        bucket = RATE_BUCKET.get(perm)
        if bucket and method in RATE_METHODS:
            ok, retry = state.limiter.allow(bucket, principal.username or client_ip(request))
            if not ok:
                state.audit.record("rate_limited", principal.username, f"{method} {path}", result="rejected",
                                   reason=f"{bucket} rate limit", request_id=rid)
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": str(int(retry) + 1)})
    return enforce


def require(state: SecurityState, request: Request, perm: str, zone: str | None = None, action: str = "") -> Principal:
    p = getattr(request.state, "principal", None) or Principal("anonymous", None, None)
    if not p.can(perm):
        state.audit.record("authorization_failure", p.username, action or perm, result="rejected",
                           reason=f"role {p.role} lacks {perm}", zone=zone)
        raise HTTPException(403, "Not permitted.")
    if zone is not None and not p.can_zone(zone):
        state.audit.record("authorization_failure", p.username, action or perm, target=zone, zone=zone,
                           result="rejected", reason="zone not assigned to this user")
        raise HTTPException(403, "Not permitted for this zone.")
    return p


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
       "object-src 'none'")


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, state: SecurityState):
        super().__init__(app)
        self.state = state

    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id")
        rid = rid if rid and len(rid) <= 64 and rid.replace("-", "").isalnum() else secrets.token_hex(8)
        request.scope["fl_request_id"] = rid
        cfg = self.state.config
        if cfg.https_required and not cfg.tls_terminated_upstream_ok(request):
            if request.method in ("GET", "HEAD"):
                return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
            return JSONResponse({"detail": "HTTPS required."}, status_code=400)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:                                   # noqa: BLE001 — never leak internals
            self.state.audit.record("security_configuration", None, f"{request.method} {request.url.path}",
                                    result="error", reason="unhandled server error", request_id=rid)
            response = JSONResponse({"detail": "Internal server error.", "request_id": rid}, status_code=500)
        if (request.method == "POST" and response.status_code < 400
                and any(request.url.path.startswith(x) for x in AUDITED_POSTS)):
            p = getattr(request.state, "principal", None)
            self.state.audit.record("configuration_change", getattr(p, "username", None), f"POST {request.url.path}",
                                    result="success", role=getattr(p, "role", None), request_id=rid)
        h = response.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Content-Security-Policy"] = CSP
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["X-FeelsLike-Security"] = cfg.mode
        if cfg.https_required:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            h["Cache-Control"] = "no-store"
        h["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
        return response


def _tls_ok(self, request) -> bool:
    if request.url.scheme == "https":
        return True
    return self.tls_terminated_upstream and request.headers.get("x-forwarded-proto", "").lower() == "https"


SecurityConfig.tls_terminated_upstream_ok = _tls_ok
