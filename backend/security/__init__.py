"""FeelsLike security layer (Phase 6). See docs/SECURITY.md.

  config.py     one central SecurityConfig (development | enforced | production), validated
  passwords.py  scrypt password hashing (stdlib hashlib.scrypt, RFC 7914)
  auth.py       roles, permissions, users, opaque server-side sessions, principals
  audit.py      structured audit log with secret redaction
  ratelimit.py  token-bucket throttling
  devices.py    device identity registry, per-device HMAC keys, rotation, quarantine
  ingest.py     secure telemetry ingestion in front of the existing LatestStore
  protocols.py  simulated MQTT broker + ACLs, MQTTS config validation, fieldbus gateway boundary
  web.py        FastAPI enforcement: route policy table, headers, request ids, safe errors

No new dependencies; no custom cryptography (scrypt, HMAC-SHA256, secrets.token_urlsafe only).
"""
