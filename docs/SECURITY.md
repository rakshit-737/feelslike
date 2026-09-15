# FeelsLike security (Phase 6)

Code: `backend/security/` (config, passwords, auth, audit, ratelimit, devices, ingest, protocols, web),
`backend/app.py` (enforcement wiring, `/api/security/*`, `/api/telemetry/ingest`, `/api/control/zone/{id}`,
`/api/occupant/rooms`), `dashboard/security.js`, `scripts/security_users.py`. Tests: `tests/test_security.py`.

## 0. Status labels used in this document

| Label | Meaning |
|---|---|
| **IMPLEMENTED AND TESTED** | code in this repository, exercised by tests against the real FastAPI routes |
| **ARCHITECTURE PREPARED** | interfaces / configuration / validation exist; the production component is not part of this repo |
| **SIMULATED** | runs in-process to demonstrate behaviour; not a real network, broker or device |
| **PRODUCTION DEPLOYMENT REQUIREMENT** | must be provided by whoever deploys FeelsLike; not provided here |

The local demo runs **plain HTTP in development mode by default. It is not encrypted in transit and
does not enforce authentication.** Every response says so (`X-FeelsLike-Security: development`) and so
does the dashboard header.

## 1. Threat model

### Assets
telemetry and latest values · historical building data · occupancy counts · comfort data · building
configuration · HVAC control levers (objective, safety mode, zone locks, zone commands, constraint
approvals, rig heater) · operating modes · simulation controls · device identities and keys · user
credentials and sessions · audit log · security configuration · device / hardware metadata.

### Threats and mitigations

| Threat | Mitigation | Status |
|---|---|---|
| Unauthorized user | Bearer sessions, global route policy, fail-closed on unlisted routes | IMPLEMENTED AND TESTED |
| Privilege escalation | 6 roles, explicit permission sets, field-level checks (controller, profile), zone scoping | IMPLEMENTED AND TESTED |
| Unauthorized HVAC control | `hvac:control` + zone assignment resolved server-side; operating-mode check; safe bounds | IMPLEMENTED AND TESTED |
| Unauthorized configuration change | `building:configure` / `building:configure_energy` (energy fields only, no emergency) | IMPLEMENTED AND TESTED |
| Credential theft | scrypt hashes, no plaintext, generic login errors, login rate limit, token TTL, revocation | IMPLEMENTED AND TESTED |
| Token theft | opaque tokens stored only as SHA-256 digests; TTL; logout / role change revokes; `sessionStorage` (tab-scoped); never logged | IMPLEMENTED AND TESTED (TLS in transit is a PRODUCTION DEPLOYMENT REQUIREMENT) |
| Unauthorized / unknown device | device registry; unknown ids never trusted; recorded as `unknown_device` | IMPLEMENTED AND TESTED |
| Spoofed telemetry / source spoofing | per-device HMAC-SHA256 signature; zone / building claims checked; client `source` label ignored (trusted source from registry) | IMPLEMENTED AND TESTED |
| Replay / duplicates | per-device sequence window + content check; freshness window; future timestamps rejected | IMPLEMENTED AND TESTED |
| Compromised device | automatic quarantine after repeated failures; admin disable / quarantine / reinstate / rotate / revoke | IMPLEMENTED AND TESTED |
| Man-in-the-middle | HTTPS required in production (HSTS, redirect); MQTTS config validation | ARCHITECTURE PREPARED + PRODUCTION DEPLOYMENT REQUIREMENT |
| Malformed input / injection | pydantic bodies with `extra="forbid"`, enumerations, numeric bounds, id patterns, size limit on ingest, no evaluation of payloads, parameterised SQLite queries (existing) | IMPLEMENTED AND TESTED |
| Sensitive-data leakage | safe error bodies (no stack traces), audit redaction, occupant view without internals, no key material in APIs except one-time new keys to admins | IMPLEMENTED AND TESTED |
| Excessive requests / DoS | token-bucket rate limits on login, telemetry, control and admin | IMPLEMENTED AND TESTED (single process only; see limitations) |
| Stale telemetry | Phase-4 freshness rules; stale data is data quality, not a security event | IMPLEMENTED AND TESTED |
| Insecure browser ↔ backend | CORS allowlist, CSP, frame, referrer, nosniff, permissions policy; HTTPS in production | IMPLEMENTED AND TESTED (headers/CORS) + PRODUCTION DEPLOYMENT REQUIREMENT (TLS) |
| Compromised simulated identity | simulated devices use the same registry + signature + replay logic, keys never leave the process | SIMULATED, TESTED |

### Trust boundaries and data flow

```
 ┌───────────── untrusted ─────────────┐        ┌──────────────── trusted server process ────────────────┐
 USER / BROWSER ──(HTTPS in production;─────► [TB1] SecurityMiddleware: request id, HTTPS redirect,
 dashboard, occupant   plain HTTP in dev)          headers, CORS allowlist, safe 500
                                                        │
                                                   [TB2] enforce(): bearer session → Principal →
                                                        route POLICY (fail closed) → permission → rate limit
                                                        │
                                                   endpoint: field-level + zone checks (require) → validation
                                                        │                                   │
                                                        ▼                                   ▼
                                               existing services                 AuditLog (redacted)
                                    (controller, constraint store, twin, comfort, …)

 DEVICE / SENSOR ─(HTTPS / MQTTS / gateway)─► [TB3] protocol adapter (HTTPS ingest · SIMULATED MQTT broker
 (hardware: untrusted)                              with topic ACL · fieldbus gateway boundary)
                                                        │
                                                   [TB4] SecureIngest: registry → status → credential →
                                                        HMAC signature → zone/source claims → timestamp →
                                                        replay/duplicate → metric permission
                                                        │
                                                        ▼
                                               LatestStore.ingest (source from registry)
                                                        ▼
                                          digital twin · comfort · energy · demand · dashboard

 SIMULATED HARDWARE ─► SIMULATED protocol adapter ─► [TB3/TB4] the same SecureIngest (no bypass)
 BACnet / Modbus ─► LOCAL GATEWAY (segmented OT network, never internet-exposed) ─► HTTPS/MQTTS ─► [TB3]
```

## 2. Security modes (`backend/security/config.py`, single source of settings)

| Setting (env) | development (default) | enforced | production |
|---|---|---|---|
| `FL_SECURITY_MODE` | development | enforced | production |
| authentication | not enforced (explicit development principal, role `FL_DEV_ROLE`, default admin) | enforced | enforced |
| device signatures | verified if present | required | required |
| legacy `/api/hw/reading`, `/api/hw/sensor` | allowed unsigned | signed + fresh + increasing seq | signed |
| `FL_HTTPS_REQUIRED` | off | optional | **required** (308 redirect GET, 400 other, HSTS) |
| `FL_ALLOWED_ORIGINS` | localhost / 127.0.0.1 any port | explicit list, no `*` | explicit **https** list |
| TLS material | — | — | `FL_TLS_CERT_PATH` + `FL_TLS_KEY_PATH`, or `FL_TLS_TERMINATED_UPSTREAM=1` behind a TLS proxy |
| users | optional `FL_USERS_FILE` | `FL_USERS_FILE` or admin-created | `FL_USERS_FILE` **required** |
| rate limits | relaxed (still on) | strict | strict |
| other | `FL_TOKEN_TTL_S` (60..86400, default 3600), `FL_TELEMETRY_FRESHNESS_S` (300), `FL_AUDIT_ENABLED`, `FL_AUDIT_LOG`, `FL_DEVICE_KEYS_FILE`, `FL_SLACK_SIGNING_SECRET` |

Outside development, `app.py` **refuses to start** if `SecurityConfig.validate()` reports problems
(fail closed, never silently insecure). There are **no default credentials** anywhere.

## 3. Authentication — IMPLEMENTED AND TESTED

- `POST /api/security/login {username, password}` → `{token, token_type: bearer, expires_at, user, permissions}`.
  Wrong user and wrong password return the same `401 {"detail": "Authentication failed."}`; unknown users
  still pay a real scrypt verification (timing). Login is rate-limited per IP + username.
- Passwords: **scrypt** (`hashlib.scrypt`, N=2^15, r=8, p=1, 16-byte salt, format `scrypt$N$r$p$salt$hash`),
  minimum 12 characters. Argon2 / bcrypt are not installed and no dependency is added; scrypt is a
  standard memory-hard KDF (RFC 7914). The format carries parameters for future migration.
- Sessions: opaque `secrets.token_urlsafe(32)` bearer tokens; only the SHA-256 digest is stored, with
  an absolute expiry. `POST /api/security/logout` revokes; a role / zone / disabled change revokes all of
  that user's sessions. States: unauthenticated (401, `WWW-Authenticate: Bearer`), invalid or expired
  (401, `error="invalid_token"`), authenticated.
- **CSRF:** tokens travel only in the `Authorization` header — no cookies, no ambient credentials — so a
  cross-site form cannot attach them, and CORS blocks cross-origin reads. No CSRF token is added because
  there is nothing for it to protect; if cookie sessions are introduced later, CSRF protection becomes
  mandatory.
- Browser storage: `sessionStorage` (per tab, cleared when the tab closes). An XSS bug could read it;
  every dashboard string reaching `innerHTML` is escaped and the CSP forbids external scripts, but
  `'unsafe-inline'` remains because the dashboard uses inline scripts (limitation).
- Users: `scripts/security_users.py add|list` writes hashes to a git-ignored file (password from
  `FL_NEW_PASSWORD` or a no-echo prompt, never the command line); `GET/POST /api/security/users` and
  `POST /api/security/users/{username}` for admins (in-memory until restart).

## 4. Authorization and roles — IMPLEMENTED AND TESTED

Every route is listed in `backend/security/web.py:POLICY` (a test fails if one is missing; unlisted routes
are denied when auth is enabled). Permissions:

| Permission | admin | facility_manager | energy_manager | hvac_operator | occupant | auditor |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| view:building (state, latest, building, comfort, analytics, telemetry, monitor…) | ✔ | ✔ | ✔ | ✔ | | ✔ |
| view:history | ✔ | ✔ | ✔ | | | ✔ |
| view:occupant (occupant rooms + comfort, zone-scoped) | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| complaint:submit | ✔ | ✔ | | ✔ | ✔ | |
| building:configure (all profile fields) | ✔ | ✔ | | | | |
| building:configure_energy (priorities, non-emergency modes only) | ✔ | ✔ | ✔ | | | |
| controller:objective | ✔ | ✔ | ✔ | | | |
| controller:safety | ✔ | ✔ | | | | |
| hvac:control (zone-scoped: zone commands, zone locks, constraint approvals, rig heater) | ✔ | ✔ | | assigned zones | | |
| simulation:control (speed, conditions, reset, scenario, simulated hardware, demo) | ✔ | ✔ | | | | |
| whatif:run | ✔ | ✔ | ✔ | | | |
| devices:view | ✔ | ✔ | | | | ✔ |
| devices:manage | ✔ | | | | | |
| users:manage | ✔ | | | | | |
| security:view | ✔ | ✔ | | | | ✔ |
| privacy:export / privacy:redact | ✔ / ✔ | ✔ / | | | | |

**Zone-level authorization:** `Principal.can_zone` — an hvac_operator acts only in its assigned zones (an
operator without zones has none); occupants with assigned rooms see only those rooms. The zone checked is
always the server's own: the path zone for zone commands, the constraint's stored zone for approvals,
`HW_ZONE` for the heater — never a zone merely claimed in a body. 401 = not authenticated, 403 =
authenticated but not permitted, 400/422 = invalid input, 404 = unknown resource, 409 = state conflict
(operating mode / replay), 429 = rate limited.

## 5. Secure control commands — IMPLEMENTED AND TESTED

`POST /api/control/zone/{zone_id} {action: cooler|warmer|more_ventilation|less_ventilation|clear |
target_setpoint_c: 21.5..29.0, severity 1..3, reason ≤ 200 chars}`:
authenticate → `hvac:control` → zone assigned → body validated → safe bounds (the controller's envelope)
→ operating mode (`emergency_override`, or `maintenance_lockout` for a locked zone → 409) → executed
through the **existing constraint store** as an operator constraint (the ConstraintAware controller applies
it on its next step inside its 21.5–29 °C / ±1.8 K / fan 0–2 envelope; a target setpoint maps to the
nearest supported offset) → audit `control_command` with old setpoint, request and reason. There is no
path from client JSON directly into the twin. `/api/controller` checks each field's permission separately
and audits old/new values.

## 6. Device identity — IMPLEMENTED AND TESTED (keys: SIMULATED devices in-process; physical devices via admin API / keys file)

Registry fields: `device_id, building_id, floor_id, zone_id, device_type (sensor|sensor_node|gateway),
sensor_type, metrics, protocol, source, origin, simulated, status, firmware_version, sampling_interval_s,
key_id, key_fingerprint, cert_fingerprint, credential_expires_at, credential_status, quarantine_reason,
created_at, updated_at, last_seen, last_seq, accepted, rejected`. Admin states ACTIVE / INACTIVE /
QUARANTINED / SIMULATED; effective states add OFFLINE (> 15 min silent) and UNKNOWN (never seen).

- The 20 Phase-5 simulated devices (`SIM-TEMP-ZONE-A` …) are registered at start-up as **SIMULATED**
  (source `sim`, origin `simulated_hardware`). Their keys are random, in-process, never returned.
- Physical devices: `POST /api/security/devices` (admin) returns a 256-bit HMAC key **once**; or load
  `FL_DEVICE_KEYS_FILE` (git-ignored JSON). Trusted `source = "hardware"` comes from the registry.
- Lifecycle: create → register → use → rotate (`/rotate`, new key once, old key stops working) → revoke
  (`/revoke`) → replace (register a new id). Credential expiry supported (`credential_ttl_s`).
- Certificates / mTLS fingerprints: fields exist; nothing in this repo issues certificates —
  ARCHITECTURE PREPARED; a PKI is a PRODUCTION DEPLOYMENT REQUIREMENT.

## 7. Secure telemetry ingestion — IMPLEMENTED AND TESTED

`POST /api/telemetry/ingest` (≤ 16 KB, rate-limited per device) with envelope
`{device_id, seq, ts, readings:[{metric, value}], nonce?, zone_id?, building_id?, source?, sig}` where
`sig = HMAC-SHA256(key, canonical JSON without sig)`. `SecureIngest` (the only path into the store for
authenticated devices, also used by simulated hardware and the simulated MQTT adapter):

1. structure (ids, seq ≥ 0, finite ts, 1–32 readings) → **REJECTED** (422)
2. device registered → else **UNKNOWN_DEVICE** (401), recorded, never auto-trusted
3. status → **QUARANTINED** (403) / disabled (403)
4. credential valid (not expired / revoked) and signature valid → else REJECTED (401), counts toward quarantine
5. zone / building claims must match the assignment (403, `spoof_attempt`); a client `source` label is
   ignored and audited
6. timestamp: > 60 s in the future or > 24 h old → REJECTED; older than the freshness window (300 s) →
   accepted as **STALE**
7. replay: same seq + same content → **DUPLICATE** (dropped, no penalty — QoS-1 redelivery); same seq +
   different content or seq older than the 1024-message window → REJECTED (409, `replay_detected`)
8. per reading: metric must be permitted for the device; values validated by the Phase-4 store ranges
9. store with the registry's trusted source / origin; update last_seen / last_seq

**Data quality vs security:** a well-formed, authenticated reading with an impossible value is a sensor
fault — recorded as an invalid reading (value null, visible, never used), reported as REJECTED in the
result. Anything unauthenticated or unauthorized never reaches the store. Rejections are kept (bounded)
with `received_at, device_id, metric, status, reason, transport` — never signatures or keys.
**Automatic quarantine:** 5 security failures within 300 s.

**Replay protection scope:** the sequence / content / freshness logic above is implemented and exercised
by simulated devices in-process. Protocol-level guarantees (TLS record protection, MQTT session state)
depend on the deployed transport — a PRODUCTION DEPLOYMENT REQUIREMENT.

Legacy rig endpoints: in enforced / production the node must sign `{device_id, seq, ts, body}` and send
`ts` + `sig` with a strictly increasing seq. **The current ESP32 / Uno firmware does not sign**, so
enforced mode needs a firmware update before the physical rig can post (limitation, documented).

## 8. Protocols

| Protocol | Status |
|---|---|
| HTTPS device ingest | IMPLEMENTED AND TESTED (encrypted only when deployed behind TLS) |
| MQTT | **SIMULATED MQTT ADAPTER**: in-process broker — CONNECT auth (HMAC of client id with the device key), topic ACL `building/{building_id}/zone/{zone_id}/telemetry` (a zone-A device cannot publish as zone B), QoS 0/1 with one DUP redelivery, retained telemetry refused, connection / last-seen tracking → `MqttTelemetryAdapter` → SecureIngest. No network and no real broker. |
| MQTTS client | **PRODUCTION-READY MQTT INTERFACE**: `MqttsClientConfig` validation (DNS host, 8883 not 1883, TLS ≥ 1.2, CA + per-device client cert/key, QoS ≤ 1, no retain). No client library bundled; broker deployment required. |
| BACnet / Modbus | ARCHITECTURE PREPARED: `FieldbusGatewayConfig` — only through a registered `gateway` device on a segmented building network, upstream HTTPS/MQTTS, never internet-exposed (validation refuses it). Phase-5 simulated Modbus/BACnet encoders run in-process behind SecureIngest. BACnet/SC documented, not implemented. Use VPN / network segmentation in deployment. |

Simulated MQTT MQTT-protocol devices (TEMP / HUM) really connect to and publish through the simulated
broker; CO2 / OCC (simulated Modbus / BACnet) go straight to SecureIngest as a local-gateway stand-in.
Security faults on simulated devices (`bad_credentials, replay, spoof_zone, malformed, unknown_identity,
expired_identity`) exercise the same pipeline.

## 9. HTTPS / TLS

- Development and enforced modes: plain HTTP — **not encrypted in transit** (labelled everywhere).
- Production: `FL_HTTPS_REQUIRED=1` (HTTP GET → 308 to https, other methods → 400, HSTS header). Run
  `uvicorn backend.app:app --ssl-certfile $FL_TLS_CERT_PATH --ssl-keyfile $FL_TLS_KEY_PATH`, or terminate
  TLS at a reverse proxy and set `FL_TLS_TERMINATED_UPSTREAM=1` (then `X-Forwarded-Proto: https` is
  required). No certificate or key is committed (`*.pem, *.key, *.crt, *.p12` and `data/security/` are
  git-ignored). Configuration validation is tested; a live TLS handshake is not tested in this repository.

## 10. CORS and headers — IMPLEMENTED AND TESTED

CORS: development allows only `http(s)://localhost|127.0.0.1[:port]`; otherwise the explicit
`FL_ALLOWED_ORIGINS`, methods GET/POST, headers Authorization / Content-Type / X-Request-ID, no credentials.
Headers on every response: `Content-Security-Policy` (`default-src 'self'`, inline scripts/styles allowed
for the existing dashboard, `connect-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`),
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
`Permissions-Policy`, `Cross-Origin-Opener-Policy`, `X-Request-ID`, `X-FeelsLike-Security`; `/api/*`:
`Cache-Control: no-store`; HTTPS mode: `Strict-Transport-Security`. Unhandled errors return
`{"detail": "Internal server error.", "request_id"}` — no stack trace.

## 11. Rate limiting — IMPLEMENTED AND TESTED

Token buckets per user / device / IP: login 10/min, telemetry 600/min per device, control 60/min, admin
60/min (development: relaxed, still on). 429 with `Retry-After`, audited. Limitation: in-process memory
only — not shared across workers or hosts; also rate-limit at the reverse proxy in production.

## 12. Audit logging and monitoring — IMPLEMENTED AND TESTED

Events: auth_success, auth_failure (bad/expired/disabled token or password), unauthenticated_request (no
credentials sent), logout, authorization_failure, rate_limited, device_registered,
device_status_changed, device_key_rotated, device_connected, device_disconnected, telemetry_rejected,
replay_detected, duplicate_telemetry, unknown_device, spoof_attempt, device_quarantined, control_command,
configuration_change, role_change, user_created. Fields: id, timestamp, event_type, severity, actor,
action, target, zone, result, reason, request_id, details. `redact()` replaces any field named like
pass/token/secret/key/sig/authorization/credential/cookie/hash and masks long token-like strings; tests
assert passwords, tokens, device keys and password hashes never appear. In memory (5000) and optionally
JSON lines to `FL_AUDIT_LOG` (keep outside the repo / in `data/security/`). Tamper-evident, append-only
off-host storage is a PRODUCTION DEPLOYMENT REQUIREMENT.

`GET /api/security/metrics` separates **security events** (failed auth, unauthorized, rate-limit, replay,
duplicates, unknown devices, spoofing, device auth failures, quarantined, control and failed-auth rates)
from **telemetry security** (accepted / stale / rejected / malformed) and **data quality** (the Phase-4
store counts) — a stale sensor is not a security incident.

APIs (all role-restricted): `GET /api/security/status | metrics | events | devices | users`,
`POST /api/security/devices`, `POST /api/security/devices/{id}/{disable|enable|quarantine|reinstate|rotate|revoke}`,
`POST /api/security/users`, `POST /api/security/users/{username}`; public: `POST /api/security/login`,
`GET /api/security/whoami`; session: `POST /api/security/logout`.

## 13. Encryption, secrets, keys

| Area | Approach | Status |
|---|---|---|
| In transit | TLS (HTTPS, MQTTS) | PRODUCTION DEPLOYMENT REQUIREMENT (local demo is plain HTTP) |
| Passwords | scrypt | IMPLEMENTED AND TESTED |
| Device message integrity / authenticity | HMAC-SHA256 per-device keys | IMPLEMENTED AND TESTED (not confidentiality) |
| Secrets | environment variables / git-ignored files (`.env`, `data/security/`, `secrets.h`); nothing in source; repository scan test | IMPLEMENTED AND TESTED |
| At rest | the SQLite history, audit file and users file are **not encrypted** by this project | PRODUCTION DEPLOYMENT REQUIREMENT: encrypted volumes / database encryption, encrypted backups, a secret manager, key rotation schedule |
| Custom cryptography | none — stdlib `hashlib.scrypt`, `hmac`, `secrets` only | — |

Repository secret scan (this phase): no API keys, passwords, private keys or certificates are tracked.
`.env` (with the LLM provider key) is git-ignored and was never committed.

## 14. Privacy

Collected: occupancy **counts** per zone (no identities), comfort readings, complaint text (PII scrubbed
before parsing, optional anonymous authors — Phase-1 `backend/privacy.py`), simulated history, audit
events (usernames of operators/devices, never credentials). Purpose: comfort control, energy analytics,
accountability. Access: see §4 — occupants see only the minimal room view (`/api/occupant/rooms`,
`/api/comfort/occupant`, zone-scoped) and never controller, device or security internals; export and
redaction are admin / facility-manager actions and are audited. Retention: existing feed retention policy
(`privacy.RetentionPolicy`), bounded audit / rejection buffers, git-ignored generated history.
No names, face recognition or personal location tracking is added.

## 15. Incident response (runbook)

1. **Detect**: Security tab / `GET /api/security/metrics` — failed-auth spikes, spoof / replay attempts,
   quarantines, unknown devices, rate-limit violations; `GET /api/security/events`.
2. **Contain**: quarantine or disable the device (`/api/security/devices/{id}/quarantine|disable`),
   revoke its credential, disable the user (`POST /api/security/users/{u} {"disabled": true}` — sessions
   revoked), switch the controller to `recommend_only` or `maintenance_lockout` if control is suspect.
3. **Eradicate**: rotate device keys (`/rotate`), reset user passwords (users file), rotate
   `FL_SLACK_SIGNING_SECRET`, review CORS / TLS configuration.
4. **Recover**: reinstate devices, verify telemetry health returns to SIMULATION MODE / LIVE TELEMETRY.
5. **Review**: export the audit file (request ids correlate API calls), update this threat model.

## 16. Production deployment requirements (not provided by this repository)

TLS certificates and termination · a real MQTTS broker with per-device mTLS and topic ACLs · a local
BACnet / Modbus gateway on a segmented network (or BACnet/SC) · a secret manager and key rotation
schedule · encrypted storage and backups · off-host append-only audit storage · multi-instance rate
limiting at the proxy · signing firmware for physical nodes · a PKI if certificates replace HMAC keys ·
account provisioning / MFA via an identity provider · regular dependency and container scanning.

## 17. Known limitations

- Development mode (the default) does not enforce authentication and is plain HTTP.
- Sessions, users created via API, rate-limit state, device registry changes and the audit buffer are in
  memory (lost on restart; users and physical device keys can be persisted to git-ignored files).
- CSP keeps `'unsafe-inline'` (the existing dashboard is built on inline scripts).
- HMAC device keys are symmetric (the server can forge a device's messages); mTLS / asymmetric signatures
  are the production upgrade path.
- The physical rig firmware does not sign yet; enforced mode blocks it until updated.
- MQTT is simulated in-process; no broker runs; MQTTS is configuration validation only.
- No live TLS handshake, MFA or SSO is tested here.
- The controller still reads the twin, not sensor readings (Phase 5 limitation, unchanged).
