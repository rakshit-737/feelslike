"""Device identity registry.

Every device has a unique id, a fixed assignment (building / floor / zone), the metrics it may
publish, a protocol, a TRUSTED source derived server-side (simulated devices -> "sim" with origin
"simulated_hardware"; registered physical devices -> "hardware"), and a symmetric HMAC-SHA256 key.

Keys live only in memory (and, for physical devices, in a git-ignored JSON file given by
FL_DEVICE_KEYS_FILE). The registry exposes only a key id and a SHA-256 fingerprint prefix; a
new key is returned exactly once, by register / rotate. Simulated devices get random keys at
start-up that never leave the process. Certificate fields exist for the MQTTS / mTLS path but are
not populated by anything in this repository (no certificates are issued here).

Message authentication: sig = HMAC-SHA256(key, canonical JSON of the envelope without "sig").
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
ADMIN_STATES = ("ACTIVE", "INACTIVE", "QUARANTINED", "SIMULATED")
EFFECTIVE_STATES = ADMIN_STATES + ("OFFLINE", "UNKNOWN")
PROTOCOLS = ("https", "mqtts", "mqtt-simulated", "bacnet-gateway", "modbus-gateway", "https-simulated",
             "bacnet-simulated", "modbus-simulated", "http-legacy")
OFFLINE_AFTER_S = 900.0


def canonical(envelope: dict) -> bytes:
    body = {k: v for k, v in envelope.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def sign(key: bytes, envelope: dict) -> str:
    return hmac.new(key, canonical(envelope), hashlib.sha256).hexdigest()


def fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


@dataclass
class DeviceIdentity:
    device_id: str
    building_id: str
    floor_id: str | None
    zone_id: str
    device_type: str
    sensor_type: str | None
    metrics: list
    protocol: str
    source: str
    origin: str
    simulated: bool
    status: str = "ACTIVE"
    firmware_version: str | None = None
    sampling_interval_s: float | None = None
    key_id: str | None = None
    key_fingerprint: str | None = None
    cert_fingerprint: str | None = None
    credential_expires_at: float | None = None
    credential_status: str = "valid"
    quarantine_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_seen: float | None = None
    last_seq: int | None = None
    accepted: int = 0
    rejected: int = 0


class DeviceRegistry:
    def __init__(self, clock=time.time):
        self._lock = threading.RLock()
        self._now = clock
        self.devices: dict = {}
        self._keys: dict = {}

    # ------------------------------------------------------------------ lifecycle
    def register(self, device_id: str, *, building_id: str, zone_id: str, metrics: list, protocol: str,
                 simulated: bool, floor_id=None, device_type="sensor", sensor_type=None, firmware_version=None,
                 sampling_interval_s=None, key: bytes | None = None, credential_ttl_s: float | None = None) -> tuple:
        """-> (identity, key_hex_or_None). The key is returned only if it was generated here."""
        errs = []
        for label, v in (("device_id", device_id), ("building_id", building_id), ("zone_id", zone_id)):
            if not isinstance(v, str) or not ID_RE.match(v):
                errs.append(f"{label} must match [A-Za-z0-9_.:-]{{1,64}}")
        if protocol not in PROTOCOLS:
            errs.append(f"protocol must be one of {PROTOCOLS}")
        if not metrics or not all(isinstance(m, str) and ID_RE.match(m) for m in metrics):
            errs.append("metrics must be a non-empty list of metric names")
        if errs:
            raise ValueError(errs)
        with self._lock:
            if device_id in self.devices:
                raise ValueError(["device already registered"])
            generated = key is None
            k = secrets.token_bytes(32) if key is None else key
            now = self._now()
            ident = DeviceIdentity(
                device_id=device_id, building_id=building_id, floor_id=floor_id, zone_id=zone_id,
                device_type=device_type, sensor_type=sensor_type, metrics=list(metrics), protocol=protocol,
                source="sim" if simulated else "hardware", origin="simulated_hardware" if simulated else "hardware",
                simulated=bool(simulated), status="SIMULATED" if simulated else "ACTIVE",
                firmware_version=firmware_version, sampling_interval_s=sampling_interval_s,
                key_id=f"k-{secrets.token_hex(4)}", key_fingerprint=fingerprint(k),
                credential_expires_at=(now + credential_ttl_s) if credential_ttl_s else None,
                created_at=now, updated_at=now)
            self.devices[device_id] = ident
            self._keys[device_id] = k
        return ident, (k.hex() if generated and not simulated else None)

    def rotate_key(self, device_id: str) -> str:
        with self._lock:
            ident = self._get(device_id)
            k = secrets.token_bytes(32)
            self._keys[device_id] = k
            ident.key_id, ident.key_fingerprint = f"k-{secrets.token_hex(4)}", fingerprint(k)
            ident.credential_status, ident.updated_at = "valid", self._now()
            return k.hex()

    def set_status(self, device_id: str, status: str, reason: str | None = None) -> DeviceIdentity:
        if status not in ADMIN_STATES:
            raise ValueError(f"status must be one of {ADMIN_STATES}")
        with self._lock:
            ident = self._get(device_id)
            if status == "SIMULATED" and not ident.simulated:
                raise ValueError("only simulated devices can be SIMULATED")
            if status == "ACTIVE" and ident.simulated:
                status = "SIMULATED"
            ident.status, ident.quarantine_reason, ident.updated_at = status, (reason if status == "QUARANTINED" else None), self._now()
            return ident

    def revoke_credential(self, device_id: str) -> None:
        with self._lock:
            self._get(device_id).credential_status = "revoked"

    def expire_credential(self, device_id: str) -> None:
        with self._lock:
            ident = self._get(device_id)
            ident.credential_expires_at = self._now() - 1

    def _get(self, device_id: str) -> DeviceIdentity:
        ident = self.devices.get(device_id)
        if ident is None:
            raise KeyError(device_id)
        return ident

    def get(self, device_id) -> DeviceIdentity | None:
        return self.devices.get(device_id) if isinstance(device_id, str) else None

    # ------------------------------------------------------------------ auth
    def credential_state(self, ident: DeviceIdentity) -> str:
        if ident.credential_status == "revoked":
            return "revoked"
        if ident.credential_expires_at is not None and ident.credential_expires_at < self._now():
            return "expired"
        return "valid"

    def verify(self, device_id: str, envelope: dict) -> bool:
        k = self._keys.get(device_id)
        sig = envelope.get("sig")
        if k is None or not isinstance(sig, str) or not re.fullmatch(r"[0-9a-f]{64}", sig):
            return False
        return hmac.compare_digest(sign(k, envelope), sig)

    def sign_for(self, device_id: str, envelope: dict) -> str:
        """In-process signing for SIMULATED devices only (their keys never leave the process)."""
        ident = self._get(device_id)
        if not ident.simulated:
            raise PermissionError("the server never signs on behalf of a physical device")
        return sign(self._keys[device_id], envelope)

    def load_keys_file(self, path) -> int:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        n = 0
        for rec in data.get("devices", []):
            key = bytes.fromhex(rec.pop("key_hex"))
            if len(key) < 32:
                raise ValueError(f"device {rec.get('device_id')!r}: key must be at least 32 bytes")
            dev = rec.pop("device_id")
            self.register(dev, key=key, simulated=False, **rec)
            n += 1
        return n

    # ------------------------------------------------------------------ views
    def effective_status(self, ident: DeviceIdentity) -> str:
        if ident.status in ("INACTIVE", "QUARANTINED"):
            return ident.status
        if ident.last_seen is None:
            return "UNKNOWN" if not ident.simulated else "SIMULATED"
        if self._now() - ident.last_seen > OFFLINE_AFTER_S:
            return "OFFLINE"
        return ident.status

    def public(self, ident: DeviceIdentity) -> dict:
        d = asdict(ident)
        d["effective_status"] = self.effective_status(ident)
        d["credential_state"] = self.credential_state(ident)
        d["authenticated_state"] = ("authenticated" if ident.last_seen and d["credential_state"] == "valid"
                                    else "never authenticated" if not ident.last_seen else d["credential_state"])
        d["last_seen_age_s"] = None if ident.last_seen is None else round(self._now() - ident.last_seen, 1)
        d["label"] = "SIMULATED" if ident.simulated else "HARDWARE"
        return d

    def list(self) -> list:
        with self._lock:
            return [self.public(i) for i in sorted(self.devices.values(), key=lambda x: x.device_id)]
