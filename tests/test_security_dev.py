"""Phase 6 — development mode is the explicit default (kept apart from test_security.py, whose
module-scoped fixture switches the app to enforced mode)."""
from __future__ import annotations


def test_development_mode_is_explicit(client):
    r = client.get("/api/security/whoami")
    body = r.json()
    assert body["mode"] == "development" and "not enforced" in body["label"] and "not encrypted" in body["label"]
    assert body["principal"]["kind"] == "development" and body["principal"]["authenticated"] is False
    assert r.headers["x-feelslike-security"] == "development"
    assert client.get("/api/state").status_code == 200          # earlier behaviour preserved


def test_security_endpoints_work_in_development(client):
    st = client.get("/api/security/status").json()
    assert st["config"]["mode"] == "development" and any("not enforced" in w for w in st["warnings"])
    assert st["metrics"]["devices"]["simulated"] == 20
    assert all("device_key" not in d for d in client.get("/api/security/devices").json()["devices"])
