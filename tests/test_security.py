"""Phase 6 — security: authentication, roles, zone authorization, device identity, secure telemetry
(unknown / spoofed / malformed / out-of-range / stale / duplicate / replay / quarantine), secure control,
audit (and its redaction), rate limiting, CORS, headers, secret leakage, protocol adapters, HTTPS
configuration, privacy. Tests hit the real FastAPI routes in ENFORCED mode; the rest of the suite keeps
running in the default development mode."""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from pathlib import Path

import pytest

from backend.security import auth, devices, passwords
from backend.security.audit import AuditLog, redact
from backend.security.config import SecurityConfig
from backend.security.protocols import (FieldbusGatewayConfig, MqttsClientConfig, mqtt_password,
                                        telemetry_topic)
from backend.security.ratelimit import RateLimiter

ROOT = Path(__file__).resolve().parent.parent
PW = {name: secrets.token_urlsafe(18) for name in ("admin1", "fm1", "em1", "op-a", "occ1", "aud1")}
ROLE_OF = {"admin1": ("admin", None), "fm1": ("facility_manager", None), "em1": ("energy_manager", None),
           "op-a": ("hvac_operator", ["zone_a"]), "occ1": ("occupant", ["zone_b"]), "aud1": ("auditor", None)}


@pytest.fixture(scope="module")
def sec(client):
    import backend.app as A
    old = A.SEC.config
    cfg = SecurityConfig(mode="enforced", allowed_origins=["http://127.0.0.1:8000"])
    cfg.rate_limits = {"login": (5, 60.0), "telemetry": (100000, 60.0), "control": (100000, 60.0), "admin": (100000, 60.0)}
    A.security_reconfigure(cfg)
    for name, (role, zones) in ROLE_OF.items():
        A.SEC.users.add(name, PW[name], role, zones)
    tokens = {n: client.post("/api/security/login", json={"username": n, "password": PW[n]}).json()["token"]
              for n in ROLE_OF}
    yield client, tokens, A
    A.security_reconfigure(old)


def H(tokens, name):
    return {"Authorization": f"Bearer {tokens[name]}"}


@pytest.fixture
def device(sec):
    """A registered PHYSICAL test device (admin API) and its one-time key."""
    client, tokens, A = sec
    dev = "HW-TEST-" + secrets.token_hex(3).upper()
    r = client.post("/api/security/devices", headers=H(tokens, "admin1"),
                    json={"device_id": dev, "zone_id": "zone_c", "metrics": ["temperature", "humidity"],
                          "protocol": "https", "firmware_version": "fw-2.1"})
    assert r.status_code == 200, r.text
    key = bytes.fromhex(r.json()["device_key"])
    state = {"seq": 0}

    def env(value=24.5, metric="temperature", seq=None, ts=None, key_=None, **extra):
        state["seq"] += 1
        e = {"device_id": dev, "seq": state["seq"] if seq is None else seq, "ts": time.time() if ts is None else ts,
             "readings": [{"metric": metric, "value": value}], **extra}
        e["sig"] = devices.sign(key_ or key, e)
        return e
    return dev, key, env


def ingest(client, e):
    return client.post("/api/telemetry/ingest", content=json.dumps(e), headers={"Content-Type": "application/json"})


# 1-4 authentication
def test_unauthenticated_invalid_and_expired(sec):
    client, tokens, A = sec
    r = client.get("/api/state")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert client.get("/").status_code == 200 and client.get("/api/security/whoami").status_code == 200
    assert client.get("/api/state", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401
    t = client.post("/api/security/login", json={"username": "aud1", "password": PW["aud1"]}).json()["token"]
    assert client.get("/api/latest", headers={"Authorization": f"Bearer {t}"}).status_code == 200
    for s in A.SEC.sessions._sessions.values():
        if s["username"] == "aud1":
            s["exp"] = 0
    r = client.get("/api/latest", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 401 and "invalid_token" in r.headers["www-authenticate"]
    tokens["aud1"] = client.post("/api/security/login", json={"username": "aud1", "password": PW["aud1"]}).json()["token"]


def test_login_failure_is_generic_and_logout_revokes(sec):
    client, tokens, A = sec
    r = client.post("/api/security/login", json={"username": "fm1", "password": "wrong-password-123"})
    assert r.status_code == 401 and r.json() == {"detail": "Authentication failed."}
    r2 = client.post("/api/security/login", json={"username": "nobody-here", "password": "whatever-123456"})
    assert r2.json() == r.json()
    t = client.post("/api/security/login", json={"username": "em1", "password": PW["em1"]}).json()["token"]
    assert client.post("/api/security/logout", headers={"Authorization": f"Bearer {t}"}).json()["revoked"]
    assert client.get("/api/building", headers={"Authorization": f"Bearer {t}"}).status_code == 401
    A.SEC.limiter.reset()


# 5-10 roles
def test_role_permissions_on_real_routes(sec):
    client, tokens, A = sec
    assert client.get("/api/state", headers=H(tokens, "fm1")).status_code == 200
    assert client.post("/api/speed", headers=H(tokens, "em1"), json={"speed": 240}).status_code == 403
    assert client.post("/api/speed", headers=H(tokens, "fm1"), json={"speed": 1}).status_code == 200
    assert client.post("/api/building/profile", headers=H(tokens, "aud1"), json={"floors": 5}).status_code == 403
    assert client.post("/api/complaint", headers=H(tokens, "aud1"), json={"text": "hot in cabin c"}).status_code == 403
    assert client.post("/api/scenario/reset", headers=H(tokens, "aud1")).status_code == 403
    assert client.get("/api/security/users", headers=H(tokens, "fm1")).status_code == 403
    assert client.get("/api/security/users", headers=H(tokens, "admin1")).status_code == 200
    assert client.get("/api/security/status", headers=H(tokens, "fm1")).status_code == 200
    assert client.get("/api/history/catalog", headers=H(tokens, "op-a")).status_code == 403   # operator: no history
    # energy manager: energy fields only, never emergency
    assert client.post("/api/building/profile", headers=H(tokens, "em1"),
                       json={"comfort_priority": 40, "energy_priority": 60}).status_code == 200
    assert client.post("/api/building/profile", headers=H(tokens, "em1"), json={"operating_mode": "emergency"}).status_code == 403
    assert client.post("/api/building/profile", headers=H(tokens, "em1"), json={"floors": 9}).status_code == 403
    assert client.post("/api/controller", headers=H(tokens, "em1"), json={"objective": "energy"}).status_code == 200
    assert client.post("/api/controller", headers=H(tokens, "em1"), json={"safety_mode": "automatic"}).status_code == 403
    client.post("/api/building/profile", headers=H(tokens, "admin1"), json={"building_type": "office", "operating_mode": "normal"})
    client.post("/api/controller", headers=H(tokens, "admin1"), json={"objective": "balanced", "safety_mode": "automatic"})


def test_occupant_cannot_control_or_see_internals(sec):
    client, tokens, A = sec
    o = H(tokens, "occ1")
    assert client.post("/api/control/zone/zone_b", headers=o, json={"action": "cooler"}).status_code == 403
    assert client.post("/api/controller", headers=o, json={"lock_zone": "zone_b"}).status_code == 403
    assert client.post("/api/hw/heater", headers=o, json={"on": True}).status_code == 403
    for path in ("/api/state", "/api/security/status", "/api/security/devices", "/api/security/events",
                 "/api/export", "/api/simhw", "/api/latest"):
        assert client.get(path, headers=o).status_code == 403, path
    rooms = client.get("/api/occupant/rooms", headers=o).json()
    assert [z["id"] for z in rooms["zones"]] == ["zone_b"] and set(rooms["zones"][0]) == {"id", "name", "temp", "setpoint", "active_constraints"}
    assert client.get("/api/comfort/occupant?zone=zone_a", headers=o).status_code == 403
    c = client.get("/api/comfort/occupant?zone=zone_b", headers=o)
    assert c.status_code == 200 and not re.search(r"node_id|device|sensor_id|key|token|reason_code", c.text)
    assert client.post("/api/complaint", headers=o, json={"text": "conference room b is too hot"}).status_code == 200


# 8, 18, 28 zone authorization + secure control + audit old/new
def test_zone_scoped_operator_control_and_audit(sec):
    client, tokens, A = sec
    op = H(tokens, "op-a")
    assert client.post("/api/control/zone/zone_b", headers=op, json={"action": "cooler"}).status_code == 403
    assert client.post("/api/controller", headers=op, json={"lock_zone": "zone_b"}).status_code == 403
    r = client.post("/api/control/zone/zone_a", headers=op, json={"target_setpoint_c": 23.0, "reason": "meeting at 3"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"].get("issue") in ("too_hot", "too_cold") or body["applied"].get("no_change")
    assert client.post("/api/control/zone/zone_a", headers=op, json={"target_setpoint_c": 40}).status_code == 400
    assert client.post("/api/control/zone/zone_a", headers=op, json={"action": "melt"}).status_code == 400
    assert client.post("/api/control/zone/zone_a", headers=op, json={"action": "cooler", "target_setpoint_c": 23}).status_code == 400
    assert client.post("/api/control/zone/zone_q", headers=op, json={"action": "cooler"}).status_code == 404
    ev = [e for e in A.SEC.audit.list(event_type="control_command") if e["actor"] == "op-a" and e["result"] == "accepted"]
    assert ev and ev[0]["zone"] == "zone_a" and "old" in ev[0]["details"] and "new" in ev[0]["details"]
    assert ev[0]["reason"] == "meeting at 3"
    # constraint decisions are zone-scoped from the store, not the client
    with A.sim.lock:
        cid = next(c.id for c in A.sim.store.items if c.zone == "zone_a")
    assert client.post(f"/api/constraints/{cid}/reject", headers=H(tokens, "occ1")).status_code == 403
    assert client.post(f"/api/constraints/{cid}/approve", headers=op).status_code == 200
    # operating mode forbids control
    client.post("/api/controller", headers=H(tokens, "fm1"), json={"safety_mode": "emergency_override"})
    assert client.post("/api/control/zone/zone_a", headers=op, json={"action": "warmer"}).status_code == 409
    client.post("/api/controller", headers=H(tokens, "fm1"), json={"safety_mode": "automatic"})
    assert client.post("/api/control/zone/zone_a", headers=H(tokens, "fm1"), json={"action": "clear"}).status_code == 200


# 11-20 secure telemetry
def test_unknown_spoofed_and_source_label(sec, device):
    client, tokens, A = sec
    dev, key, env = device
    e = env()
    e["device_id"] = "HW-NOT-REGISTERED"
    r = ingest(client, e)
    assert r.status_code == 401 and r.json()["status"] == "UNKNOWN_DEVICE"
    assert any(u["device_id"] == "HW-NOT-REGISTERED" for u in A.SEC_INGEST.unknown_attempts)
    assert ingest(client, env(key_=secrets.token_bytes(32))).status_code == 401          # wrong key = spoofed device
    assert ingest(client, env(zone_id="zone_a")).status_code == 403                      # claims another zone
    r = ingest(client, env(value=25.1, source="real"))
    assert r.status_code == 200 and r.json()["trusted_source"] == "hardware"
    rec = A.sim.latest.query(sensor=f"{dev}:temperature")[0]
    assert rec["source"] == "hardware" and rec["origin"] == "hardware" and rec["zone_id"] == "zone_c"
    assert any(x["event_type"] == "spoof_attempt" and x["actor"] == dev for x in A.SEC.audit.list())


def test_malformed_range_stale_duplicate_replay_sequence(sec, device):
    client, tokens, A = sec
    dev, key, env = device
    bad = env()
    bad["readings"] = "nope"
    bad["sig"] = devices.sign(key, bad)
    assert ingest(client, bad).status_code == 422
    assert client.post("/api/telemetry/ingest", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 422
    r = ingest(client, env(value=999.0))
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "REJECTED"
    assert A.sim.latest.query(sensor=f"{dev}:temperature")[0]["quality"] == "invalid"
    r = ingest(client, env(value=24.0, ts=time.time() - 900))
    assert r.json()["status"] == "STALE" and A.sim.latest.query(sensor=f"{dev}:temperature")[0]["quality"] == "stale"
    assert ingest(client, env(ts=time.time() + 3600)).status_code == 422
    e = env(value=23.0)
    assert ingest(client, e).json()["status"] == "ACCEPTED"
    assert ingest(client, e).json()["status"] == "DUPLICATE"                             # identical redelivery
    replay = dict(e, readings=[{"metric": "temperature", "value": 30.0}])
    replay["sig"] = devices.sign(key, replay)
    assert ingest(client, replay).status_code == 409                                     # same seq, new content
    assert ingest(client, env(seq=5000)).status_code == 200
    assert ingest(client, env(seq=1)).status_code == 409                                 # outside the window
    neg = env(seq=-4)
    assert ingest(client, neg).status_code == 422
    # that was the 5th penalized failure (malformed, future, 2 replays, bad seq): auto-quarantine
    assert A.SEC_REGISTRY.get(dev).status == "QUARANTINED"
    assert client.post(f"/api/security/devices/{dev}/reinstate", headers=H(tokens, "admin1")).status_code == 200
    r = ingest(client, env(metric="co2", value=500, seq=5001))              # newer than the last accepted seq
    assert r.status_code == 200 and r.json()["results"][0]["reason"] == "metric not permitted for this device"


def test_quarantine_after_repeated_failures_and_reinstate(sec, device):
    client, tokens, A = sec
    dev, key, env = device
    for _ in range(A.SEC.config.quarantine_after_failures):
        ingest(client, env(key_=secrets.token_bytes(32)))
    assert A.SEC_REGISTRY.get(dev).status == "QUARANTINED"
    r = ingest(client, env())
    assert r.status_code == 403 and r.json()["status"] == "QUARANTINED"
    assert client.post(f"/api/security/devices/{dev}/reinstate", headers=H(tokens, "fm1")).status_code == 403
    assert client.post(f"/api/security/devices/{dev}/reinstate", headers=H(tokens, "admin1")).status_code == 200
    assert ingest(client, env(value=22.2)).status_code == 200
    assert any(e["event_type"] == "device_quarantined" for e in A.SEC.audit.list())


# 27 device registry permissions, key handling, rotation
def test_device_registry_permissions_and_key_once(sec, device):
    client, tokens, A = sec
    dev, key, env = device
    body = {"device_id": "HW-X-1", "zone_id": "zone_a", "metrics": ["temperature"]}
    assert client.post("/api/security/devices", headers=H(tokens, "occ1"), json=body).status_code == 403
    assert client.post("/api/security/devices", headers=H(tokens, "fm1"), json=body).status_code == 403
    listing = client.get("/api/security/devices", headers=H(tokens, "fm1"))
    assert listing.status_code == 200 and key.hex() not in listing.text
    assert all("device_key" not in d for d in listing.json()["devices"])
    sim_dev = next(d for d in listing.json()["devices"] if d["device_id"] == "SIM-TEMP-ZONE-A")
    assert sim_dev["simulated"] and sim_dev["label"] == "SIMULATED" and sim_dev["source"] == "sim"
    r = client.post(f"/api/security/devices/{dev}/rotate", headers=H(tokens, "admin1")).json()
    new_key = bytes.fromhex(r["device_key"])
    assert ingest(client, env(key_=key)).status_code == 401 and ingest(client, env(key_=new_key)).status_code == 200
    rs = client.post("/api/security/devices/SIM-TEMP-ZONE-A/rotate", headers=H(tokens, "admin1")).json()
    assert "device_key" not in rs                                                       # simulated keys never leave


# 21-22 audit + redaction + no secrets anywhere
def test_audit_records_and_never_contains_secrets(sec, device):
    client, tokens, A = sec
    types = {e["event_type"] for e in A.SEC.audit.list(limit=1000)}
    assert {"auth_success", "auth_failure", "unauthenticated_request", "authorization_failure", "control_command", "device_registered",
            "configuration_change", "unknown_device"} <= types
    blob = json.dumps(A.SEC.audit.list(limit=5000))
    for name, pw in PW.items():
        assert pw not in blob
    for t in tokens.values():
        assert t not in blob
    assert device[1].hex() not in blob and "scrypt$" not in blob
    log = AuditLog()
    ev = log.record("auth_failure", "x", "login", password="hunter2hunter2", api_token="abc", device_key="00ff",
                    note="Bearer " + "A" * 40)
    assert ev["details"]["password"] == ev["details"]["api_token"] == ev["details"]["device_key"] == "[REDACTED]"
    assert "A" * 40 not in json.dumps(ev)
    assert redact({"nested": {"client_secret": "s"}})["nested"]["client_secret"] == "[REDACTED]"
    ev_resp = client.get("/api/security/events?limit=50", headers=H(tokens, "aud1"))
    assert ev_resp.status_code == 200 and "password_hash" not in ev_resp.text
    st = client.get("/api/security/status", headers=H(tokens, "admin1")).text
    assert "password_hash" not in st and "key_hex" not in st
    assert client.get("/api/security/events", headers=H(tokens, "em1")).status_code == 403


# 23 rate limiting
def test_login_rate_limit(sec):
    client, tokens, A = sec
    A.SEC.limiter.reset()
    codes = [client.post("/api/security/login", json={"username": "fm1", "password": "bad-password-000"}).status_code
             for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[-1] == 429
    assert any(e["event_type"] == "rate_limited" for e in A.SEC.audit.list())
    lim = RateLimiter({"x": (2, 10.0)}, clock=lambda: 0.0)
    assert lim.allow("x", "k")[0] and lim.allow("x", "k")[0] and not lim.allow("x", "k")[0]
    A.SEC.limiter.reset()


# 24-25 CORS + headers
def test_cors_allowlist_and_security_headers(sec):
    client, tokens, A = sec
    ok = client.options("/api/latest", headers={"Origin": "http://127.0.0.1:8000", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:8000"
    evil = client.options("/api/latest", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers
    r = client.get("/")
    for h, v in (("x-content-type-options", "nosniff"), ("x-frame-options", "DENY"), ("referrer-policy", "no-referrer")):
        assert r.headers[h] == v
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and "connect-src 'self'" in r.headers["content-security-policy"]
    api = client.get("/api/security/whoami")
    assert api.headers["cache-control"] == "no-store" and re.fullmatch(r"[0-9a-f]{16}", api.headers["x-request-id"])
    assert api.headers["x-feelslike-security"] == "enforced"


# 26 secrets
def test_no_committed_secrets_or_keys():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not [f for f in files if re.search(r"(^|/)\.env$|\.pem$|\.key$|\.crt$|\.p12$|secrets\.h$|data/security/", f)]
    rx = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(sk-ant-|gsk_|AIza)[A-Za-z0-9_\-]{20,}")
    hits = []
    for f in files:
        p = ROOT / f
        if p.suffix in (".py", ".js", ".html", ".md", ".json", ".txt", ".ino", ".h", ".example", ".ini") and p.is_file():
            if rx.search(p.read_text(encoding="utf-8", errors="ignore")):
                hits.append(f)
    assert not hits


# 29-30 protocols
def test_simulated_mqtt_broker_acl_auth_qos(sec, device):
    client, tokens, A = sec
    dev, key, env = device
    broker = A.SIM_MQTT
    assert not broker.connect("c-bad", dev, "0" * 64)
    assert broker.connect("c-good", dev, mqtt_password(key, "c-good"))
    e = env(value=21.5)
    wrong = broker.publish("c-good", telemetry_topic("live-building", "zone_a"), json.dumps(e).encode())
    assert not wrong["ok"] and wrong["reason"] == "topic not authorized"
    assert not broker.publish("c-good", telemetry_topic("live-building", "zone_c"), b"{}", retain=True)["ok"]
    ok = broker.publish("c-good", telemetry_topic("live-building", "zone_c"), json.dumps(env(value=21.7)).encode(), qos=1)
    assert ok["ok"] and ok["results"][0]["status"] == "ACCEPTED"
    assert not broker.publish("nobody", telemetry_topic("live-building", "zone_c"), b"{}")["ok"]
    assert "SIMULATED" in broker.label
    broker.disconnect("c-good")


def test_protocol_config_validation():
    errs = MqttsClientConfig(host="broker.example", port=1883).validate()
    assert any("1883" in x for x in errs) and any("client_cert_path" in x for x in errs)
    assert any("internet" in x for x in FieldbusGatewayConfig("modbus", "GW-1", exposed_to_internet=True).validate())
    reg = devices.DeviceRegistry()
    reg.register("GW-1", building_id="b", zone_id="zone_a", metrics=["temperature"], protocol="modbus-gateway",
                 simulated=False, device_type="gateway")
    assert FieldbusGatewayConfig("bacnet", "GW-1").validate(reg) == []
    assert FieldbusGatewayConfig("bacnet", "GW-9").validate(reg)


# 31 HTTPS / production configuration
def test_production_configuration_validation(tmp_path, sec):
    errs = SecurityConfig(mode="production").validate()
    assert any("FL_ALLOWED_ORIGINS" in e for e in errs) and any("FL_TLS_CERT_PATH" in e for e in errs)
    assert any("FL_USERS_FILE" in e for e in errs) and any("HTTPS" in e for e in errs)
    cert, keyf, users = tmp_path / "c.pem", tmp_path / "k.pem", tmp_path / "u.json"
    cert.write_text("x"); keyf.write_text("x"); users.write_text('{"users": []}')
    good = SecurityConfig(mode="production", https_required=True, allowed_origins=["https://fm.example"],
                          tls_cert_path=str(cert), tls_key_path=str(keyf), users_file=str(users))
    assert good.validate() == []
    assert SecurityConfig(mode="enforced", allowed_origins=["*"]).validate()
    client, tokens, A = sec
    cfg = A.SEC.config
    cfg.https_required = True
    try:
        r = client.get("/api/latest", headers=H(tokens, "fm1"), follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"].startswith("https://")
        assert client.post("/api/speed", headers=H(tokens, "fm1"), json={"speed": 1}).status_code == 400
    finally:
        cfg.https_required = False


# fail-closed coverage, legacy device gate, slack, simulated hardware through the secure pipeline
def test_every_route_has_a_policy():
    from backend.app import app
    from backend.security.web import POLICY
    missing = []
    for r in app.routes:
        for m in getattr(r, "methods", None) or []:
            if m in ("HEAD", "OPTIONS") or not hasattr(r, "path") or r.path.startswith(("/docs", "/openapi", "/redoc")):
                continue
            if (m, r.path) not in POLICY:
                missing.append((m, r.path))
    assert not missing, missing


def test_legacy_device_endpoints_require_signatures_when_enforced(sec):
    client, tokens, A = sec
    assert client.post("/api/hw/reading", json={"node_id": "shoebox-1", "temp_c": 25.0}).status_code == 401
    r = client.post("/api/security/devices", headers=H(tokens, "admin1"),
                    json={"device_id": "shoebox-sec", "zone_id": "zone_b", "metrics": ["temperature", "humidity"],
                          "protocol": "https", "device_type": "sensor_node"})
    key = bytes.fromhex(r.json()["device_key"])
    payload = {"node_id": "shoebox-sec", "temp_c": 25.5, "rh_pct": 60.0, "seq": 1, "ts": time.time()}
    env = {"device_id": "shoebox-sec", "seq": 1, "ts": payload["ts"],
           "body": {"temp_c": 25.5, "rh_pct": 60.0, "uptime_s": 0.0}}
    payload["sig"] = devices.sign(key, env)
    assert client.post("/api/hw/reading", json=payload).status_code == 200
    assert client.post("/api/hw/reading", json=payload).status_code == 409                  # replayed seq


def test_slack_requires_signature_when_enforced(sec, monkeypatch):
    client, tokens, A = sec
    assert client.post("/api/slack", data={"text": "hot in cabin c"}).status_code == 403
    import hashlib
    import hmac
    monkeypatch.setenv("FL_SLACK_SIGNING_SECRET", "test-signing-secret")
    body = b"text=&user_name=x"
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"test-signing-secret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    ok = client.post("/api/slack", content=body, headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                                                          "Content-Type": "application/x-www-form-urlencoded"})
    assert ok.status_code == 200
    bad = client.post("/api/slack", content=body, headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": "v0=00",
                                                           "Content-Type": "application/x-www-form-urlencoded"})
    assert bad.status_code == 401


def test_simulated_hardware_uses_the_secure_pipeline_and_security_faults(sec):
    client, tokens, A = sec
    fm = H(tokens, "fm1")
    before = A.SEC_INGEST.metrics()
    assert client.post("/api/simhw", headers=fm, json={"enabled": True}).status_code == 200
    try:
        lat = client.get("/api/latest/zone/zone_a", headers=fm).json()
        assert lat["temperature"]["origin"] == "simulated_hardware" and lat["temperature"]["source"] == "sim"
        assert A.SEC_INGEST.metrics()["ACCEPTED"] > before["ACCEPTED"]
        assert A.SIM_MQTT.stats["publish_ok"] > 0                                           # TEMP/HUM devices use MQTT
        for mode, check in (("unknown_identity", lambda m: m["UNKNOWN_DEVICE"] > before["UNKNOWN_DEVICE"]),
                            ("replay", lambda m: m["DUPLICATE"] > before["DUPLICATE"]),
                            ("malformed", lambda m: m["malformed"] > before["malformed"])):
            # occupancy truth is always present (CO2 truth can be absent before the first telemetry tick)
            assert client.post("/api/simhw/devices/SIM-OCC-ZONE-D/fault", headers=fm, json={"mode": mode}).status_code == 200
            assert check(A.SEC_INGEST.metrics()), mode
        client.post("/api/simhw/devices/SIM-OCC-ZONE-D/fault", headers=fm, json={"mode": "none"})
        denied = A.SIM_MQTT.stats["publish_denied"]
        client.post("/api/simhw/devices/SIM-TEMP-ZONE-E/fault", headers=fm, json={"mode": "spoof_zone"})
        assert A.SIM_MQTT.stats["publish_denied"] > denied
        for _ in range(A.SEC.config.quarantine_after_failures):
            client.post("/api/simhw/devices/SIM-HUM-ZONE-E/fault", headers=fm, json={"mode": "bad_credentials"})
        assert A.SEC_REGISTRY.get("SIM-HUM-ZONE-E").status == "QUARANTINED"
        client.post("/api/simhw/devices/SIM-OCC-ZONE-A/fault", headers=fm, json={"mode": "expired_identity"})
        assert A.SEC_REGISTRY.credential_state(A.SEC_REGISTRY.get("SIM-OCC-ZONE-A")) == "expired"
    finally:
        for d in ("SIM-TEMP-ZONE-E", "SIM-HUM-ZONE-E", "SIM-OCC-ZONE-A"):
            client.post(f"/api/simhw/devices/{d}/fault", headers=fm, json={"mode": "none"})
        A.SEC_REGISTRY.set_status("SIM-HUM-ZONE-E", "SIMULATED")
        client.post("/api/simhw", headers=fm, json={"enabled": False})


def test_user_management_and_role_change_revokes_sessions(sec):
    client, tokens, A = sec
    adm = H(tokens, "admin1")
    pw = secrets.token_urlsafe(16)
    assert client.post("/api/security/users", headers=adm, json={"username": "op-b", "password": pw, "role": "hvac_operator"}).status_code == 400
    assert client.post("/api/security/users", headers=adm, json={"username": "op-b", "password": pw, "role": "hvac_operator", "zones": ["zone_e"]}).status_code == 200
    t = client.post("/api/security/login", json={"username": "op-b", "password": pw}).json()["token"]
    assert client.post("/api/security/users/op-b", headers=adm, json={"role": "occupant", "zones": ["zone_e"]}).json()["sessions_revoked"] == 1
    assert client.get("/api/latest", headers={"Authorization": f"Bearer {t}"}).status_code == 401


# unit-level building blocks
def test_password_hashing_and_principal_rules():
    h = passwords.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and passwords.verify_password("correct horse battery", h)
    assert not passwords.verify_password("wrong horse battery", h)
    with pytest.raises(ValueError):
        passwords.hash_password("short")
    op = auth.Principal("user", "o", "hvac_operator", ["zone_a"], True)
    assert op.can("hvac:control") and op.can_zone("zone_a") and not op.can_zone("zone_b") and not op.can("users:manage")
    assert not auth.Principal("user", "x", "hvac_operator", None, True).can_zone("zone_a")   # unassigned operator: none
    assert auth.Principal("user", "a", "auditor", None, True).permissions.isdisjoint(
        {"building:configure", "hvac:control", "simulation:control", "devices:manage", "complaint:submit"})

