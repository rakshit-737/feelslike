"""Protocol boundary for device telemetry.

WHAT IS REAL HERE
  * HTTPS device ingestion: POST /api/telemetry/ingest (signed envelopes) — implemented and tested over the
    local HTTP test server; it is only encrypted in transit when the app runs behind TLS.
  * SimulatedMqttBroker + MqttTelemetryAdapter — SIMULATED MQTT ADAPTER: an in-process broker with per-device
    authentication, topic ACLs, QoS-1 redelivery and no retained telemetry. No network, no real broker.
  * MqttsClientConfig — PRODUCTION-READY MQTT INTERFACE: configuration + validation for connecting to a real
    MQTTS broker (TLS 1.2+, CA, per-device client cert/key, port 8883). No MQTT client library is bundled.
  * FieldbusGatewayConfig — boundary rules for BACnet / Modbus: they are local, untrusted building-network
    protocols reached ONLY through a registered gateway on a segmented network, never exposed to the
    internet. BACnet/SC is documented as the modern secure option; it is not implemented here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

TOPIC_RE = re.compile(r"^building/([A-Za-z0-9_.:\-]{1,64})/zone/([A-Za-z0-9_.:\-]{1,64})/telemetry$")
SIMULATED_LABEL = "SIMULATED MQTT ADAPTER — in-process broker, no network"


def telemetry_topic(building_id: str, zone_id: str) -> str:
    return f"building/{building_id}/zone/{zone_id}/telemetry"


def mqtt_password(key_hex_or_bytes, client_id: str) -> str:
    """How a device proves its identity at MQTT CONNECT in the simulation: HMAC(key, client_id)."""
    key = bytes.fromhex(key_hex_or_bytes) if isinstance(key_hex_or_bytes, str) else key_hex_or_bytes
    return hmac.new(key, client_id.encode(), hashlib.sha256).hexdigest()


class SimulatedMqttBroker:
    label = SIMULATED_LABEL

    def __init__(self, registry, audit, clock=time.time):
        self.registry, self.audit, self._now = registry, audit, clock
        self.sessions: dict = {}            # client_id -> {"device_id", "connected_at", "last_seen"}
        self.subscribers: list = []         # (pattern_regex, callback)
        self.retained: dict = {}
        self.stats = {"connect_ok": 0, "connect_denied": 0, "publish_ok": 0, "publish_denied": 0, "redelivered": 0}

    def connect(self, client_id: str, username: str, password: str) -> bool:
        ident = self.registry.get(username)
        key = self.registry._keys.get(username) if ident else None
        ok = bool(ident and key and ident.status not in ("INACTIVE", "QUARANTINED")
                  and self.registry.credential_state(ident) == "valid"
                  and hmac.compare_digest(mqtt_password(key, client_id), str(password)))
        if not ok:
            self.stats["connect_denied"] += 1
            self.audit.record("auth_failure", str(username)[:64], "mqtt connect", result="rejected",
                              reason="MQTT authentication failed", transport="mqtt-simulated")
            return False
        self.sessions[client_id] = {"device_id": username, "connected_at": self._now(), "last_seen": self._now()}
        self.stats["connect_ok"] += 1
        self.audit.record("device_connected", username, "mqtt connect", result="success", transport="mqtt-simulated")
        return True

    def disconnect(self, client_id: str) -> None:
        s = self.sessions.pop(client_id, None)
        if s:
            self.audit.record("device_disconnected", s["device_id"], "mqtt disconnect", transport="mqtt-simulated")

    def subscribe(self, pattern: str, callback) -> None:
        rx = re.compile("^" + re.escape(pattern).replace(r"\+", "[^/]+").replace(r"\#", ".*") + "$")
        self.subscribers.append((rx, callback))

    def acl_allows(self, device_id: str, topic: str) -> bool:
        ident = self.registry.get(device_id)
        m = TOPIC_RE.match(topic)
        return bool(ident and m and m.group(1) == ident.building_id and m.group(2) == ident.zone_id)

    def publish(self, client_id: str, topic: str, payload: bytes, qos: int = 1, retain: bool = False) -> dict:
        s = self.sessions.get(client_id)
        if s is None:
            self.stats["publish_denied"] += 1
            return {"ok": False, "reason": "not connected"}
        if qos not in (0, 1):
            return {"ok": False, "reason": "QoS 2 not supported for telemetry"}
        if retain:
            self.stats["publish_denied"] += 1
            return {"ok": False, "reason": "retained telemetry is refused"}
        if not self.acl_allows(s["device_id"], topic):
            self.stats["publish_denied"] += 1
            self.audit.record("spoof_attempt", s["device_id"], "mqtt publish", target=topic[:120], result="rejected",
                              reason="topic ACL: device may only publish to its assigned zone topic",
                              transport="mqtt-simulated")
            return {"ok": False, "reason": "topic not authorized"}
        s["last_seen"] = self._now()
        results = []
        for rx, cb in self.subscribers:
            if rx.match(topic):
                try:
                    results.append(cb(topic, payload, False))
                except Exception:                      # noqa: BLE001 — QoS 1: redeliver once with DUP
                    if qos == 1:
                        self.stats["redelivered"] += 1
                        results.append(cb(topic, payload, True))
        self.stats["publish_ok"] += 1
        return {"ok": True, "results": results}


class MqttTelemetryAdapter:
    """Subscribes to building/+/zone/+/telemetry and hands each decoded envelope to SecureIngest."""
    label = SIMULATED_LABEL

    def __init__(self, broker: SimulatedMqttBroker, ingest):
        self.broker, self.ingest = broker, ingest
        broker.subscribe("building/+/zone/+/telemetry", self.on_message)

    def on_message(self, topic: str, payload: bytes, dup: bool) -> dict:
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self.ingest.process(None, transport="mqtt-simulated")
        m = TOPIC_RE.match(topic)
        # the topic is a transport-level claim: verified by SecureIngest, never written into the signed envelope
        claims = {"building_id": m.group(1), "zone_id": m.group(2)} if m else None
        return self.ingest.process(envelope, transport="mqtt-simulated", claims=claims)


@dataclass
class MqttsClientConfig:
    host: str
    port: int = 8883
    ca_path: str | None = None
    client_cert_path: str | None = None
    client_key_path: str | None = None
    tls_min_version: str = "TLSv1.2"
    qos: int = 1
    retain_telemetry: bool = False
    label: str = "PRODUCTION-READY MQTT INTERFACE (configuration and validation only; broker deployment required)"

    def validate(self) -> list:
        errs = []
        if not self.host or self.host in ("0.0.0.0",):
            errs.append("host must be the broker's DNS name")
        if self.port == 1883:
            errs.append("port 1883 is plaintext MQTT; use MQTTS (8883)")
        if self.tls_min_version not in ("TLSv1.2", "TLSv1.3"):
            errs.append("tls_min_version must be TLSv1.2 or TLSv1.3")
        for label, p in (("ca_path", self.ca_path), ("client_cert_path", self.client_cert_path),
                         ("client_key_path", self.client_key_path)):
            if not p or not Path(p).is_file():
                errs.append(f"{label} must point to an existing file (per-device mutual TLS)")
        if self.qos not in (0, 1):
            errs.append("qos must be 0 or 1 for telemetry")
        if self.retain_telemetry:
            errs.append("telemetry must not be retained")
        return errs


@dataclass
class FieldbusGatewayConfig:
    protocol: str                        # bacnet | modbus
    gateway_device_id: str
    listen_network: str = "building-ot-vlan"
    exposed_to_internet: bool = False
    upstream: str = "https"              # https | mqtts to the backend
    bacnet_sc: bool = False

    def validate(self, registry=None) -> list:
        errs = []
        if self.protocol not in ("bacnet", "modbus"):
            errs.append("protocol must be bacnet or modbus")
        if self.exposed_to_internet:
            errs.append("raw BACnet/Modbus must never be exposed to the internet — use a local gateway, VPN or BACnet/SC")
        if self.upstream not in ("https", "mqtts"):
            errs.append("gateway upstream must be HTTPS or MQTTS")
        if registry is not None:
            ident = registry.get(self.gateway_device_id)
            if ident is None or ident.device_type != "gateway":
                errs.append("gateway must be a registered device of type 'gateway'")
        if self.bacnet_sc:
            errs.append("BACnet/SC is documented as a deployment option but not implemented in this repository")
        return errs
