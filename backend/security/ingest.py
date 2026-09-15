"""Secure telemetry ingestion — the authenticated front door of the EXISTING pipeline.

    envelope ─> structure ─> device registry ─> device status ─> credential ─> signature
            ─> assignment (zone / source label) ─> timestamp ─> replay / duplicate ─> per-reading
               metric permission ─> LatestStore.ingest(source = trusted registry source)

Envelope (every transport decodes to this):
  {"device_id": str, "seq": int >= 0, "ts": epoch seconds, "readings": [{"metric", "value"}, ...],
   "nonce": str (optional), "zone_id" / "building_id" / "source" (optional claims, checked or ignored),
   "sig": hex HMAC-SHA256}

Outcomes (overall and per reading): ACCEPTED, STALE (accepted but older than the freshness window —
the store reports it stale), REJECTED, QUARANTINED, DUPLICATE, UNKNOWN_DEVICE.

Security vs data quality: a well-formed, authenticated reading with an impossible VALUE is a
sensor fault, not an attack — it is passed to the store, which records it as invalid (value
null, visible, never used), exactly as Phase 4 defined. Everything that is NOT authenticated or
authorized never reaches the store.

Replay protection: per device, a sliding window of the last `replay_window` sequence numbers plus the
last accepted sequence. The same seq with the same signed content is a DUPLICATE (e.g. QoS-1
redelivery, no penalty); the same seq with different content, or a seq older than the window, is
REJECTED as a replay (counts toward quarantine). Timestamps more than max_future_s ahead or older than
stale_accept_s are rejected; older than the freshness window they are accepted as STALE.
SIMULATED devices exercise this exact logic in-process; real protocol-level anti-replay (TLS,
MQTT session state) is a deployment property, not provided by this module.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque

from backend.security.devices import ID_RE

STATUSES = ("ACCEPTED", "STALE", "REJECTED", "QUARANTINED", "DUPLICATE", "UNKNOWN_DEVICE")
MAX_READINGS = 32


class SecureIngest:
    def __init__(self, registry, store, audit, config, clock=time.time):
        self.registry, self.store, self.audit, self.config = registry, store, audit, config
        self._now = clock
        self._lock = threading.Lock()
        self._seen: dict = {}                    # device_id -> OrderedDict(seq -> sig)
        self._failures: dict = {}                # device_id -> deque[t]
        self.rejections: deque = deque(maxlen=1000)
        self.unknown_attempts: deque = deque(maxlen=200)
        self.stats = {s: 0 for s in STATUSES}
        self.stats.update({"replay": 0, "malformed": 0, "auth_failed": 0, "spoof": 0, "envelopes": 0})

    # ------------------------------------------------------------------ helpers
    def _reject(self, env_or_id, status, reason, transport, metric=None, audit_type="telemetry_rejected",
                penalize=False, record=True) -> dict:
        dev = env_or_id if isinstance(env_or_id, str) else (env_or_id.get("device_id") if isinstance(env_or_id, dict) else None)
        dev = dev if isinstance(dev, str) and ID_RE.match(dev) else "<invalid-id>"
        now = self._now()
        with self._lock:
            self.stats[status] = self.stats.get(status, 0) + 1
            if record:
                self.rejections.append({"received_at": now, "device_id": dev, "metric": metric, "status": status,
                                        "reason": reason, "transport": transport})
        if penalize:
            self._penalize(dev, reason, transport)
        self.audit.record(audit_type, dev, "telemetry", target=metric, result="rejected", reason=reason,
                          transport=transport, status=status)
        return {"status": status, "device_id": dev, "reason": reason, "accepted": 0, "results": []}

    def _penalize(self, device_id, reason, transport):
        ident = self.registry.get(device_id)
        if ident is None:
            return
        now = self._now()
        with self._lock:
            q = self._failures.setdefault(device_id, deque(maxlen=64))
            q.append(now)
            recent = sum(1 for t in q if t >= now - self.config.quarantine_window_s)
        if recent >= self.config.quarantine_after_failures and ident.status != "QUARANTINED":
            self.registry.set_status(device_id, "QUARANTINED", f"{recent} security failures in "
                                     f"{self.config.quarantine_window_s:.0f} s (last: {reason})")
            self.audit.record("device_quarantined", device_id, "quarantine", result="quarantined",
                              reason=reason, transport=transport, failures=recent)

    def clear_failures(self, device_id) -> None:
        """Reinstating a device starts a fresh failure window (otherwise one more failure re-quarantines it)."""
        with self._lock:
            self._failures.pop(device_id, None)

    # ------------------------------------------------------------------ pipeline
    def process(self, envelope, transport: str = "https", require_signature: bool | None = None,
                claims: dict | None = None) -> dict:
        """`claims` are assignment claims made by the TRANSPORT (e.g. the MQTT topic's building / zone),
        checked like envelope claims but kept outside the signed envelope."""
        with self._lock:
            self.stats["envelopes"] += 1
        # 1. structure
        if not isinstance(envelope, dict):
            with self._lock:
                self.stats["malformed"] += 1
            return self._reject(None, "REJECTED", "malformed envelope", transport)
        dev = envelope.get("device_id")
        seq, ts, readings = envelope.get("seq"), envelope.get("ts"), envelope.get("readings")
        problems = []
        if not isinstance(dev, str) or not ID_RE.match(dev):
            problems.append("device_id")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            problems.append("seq")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            problems.append("ts")
        if (not isinstance(readings, list) or not 1 <= len(readings) <= MAX_READINGS
                or not all(isinstance(r, dict) and isinstance(r.get("metric"), str) for r in readings)):
            problems.append("readings")
        if "nonce" in envelope and (not isinstance(envelope["nonce"], str) or len(envelope["nonce"]) > 64):
            problems.append("nonce")
        if problems:
            with self._lock:
                self.stats["malformed"] += 1
            return self._reject(envelope, "REJECTED", "malformed: " + ", ".join(problems), transport,
                                penalize=isinstance(dev, str))
        # 2. identity
        ident = self.registry.get(dev)
        if ident is None:
            with self._lock:
                self.unknown_attempts.append({"received_at": self._now(), "device_id": dev, "transport": transport})
            return self._reject(dev, "UNKNOWN_DEVICE", "device not registered", transport, audit_type="unknown_device")
        if ident.status == "QUARANTINED":
            return self._reject(dev, "QUARANTINED", "device quarantined", transport, record=True)
        if ident.status == "INACTIVE":
            return self._reject(dev, "REJECTED", "device disabled", transport)
        # 3. credential + signature
        need = self.config.device_auth_required if require_signature is None else require_signature
        cred = self.registry.credential_state(ident)
        if cred != "valid":
            return self._auth_fail(dev, transport, f"credential {cred}")
        if "sig" in envelope or need:
            if not self.registry.verify(dev, envelope):
                return self._auth_fail(dev, transport, "bad signature" if "sig" in envelope else "missing signature")
        # 4. assignment claims
        for claim, truth in (("zone_id", ident.zone_id), ("building_id", ident.building_id)):
            claimed = [src[claim] for src in (envelope, claims or {}) if claim in src]
            if any(c != truth for c in claimed):
                with self._lock:
                    self.stats["spoof"] += 1
                return self._reject(dev, "REJECTED", f"{claim} does not match the device assignment", transport,
                                    audit_type="spoof_attempt", penalize=True)
        if "source" in envelope and envelope["source"] != ident.source:
            with self._lock:
                self.stats["spoof"] += 1
            self.audit.record("spoof_attempt", dev, "telemetry", result="ignored",
                              reason="client-supplied source label ignored; trusted source comes from the registry",
                              claimed=str(envelope["source"])[:16], trusted=ident.source)
        # 5. timestamp
        now = self._now()
        if ts > now + self.config.max_future_s:
            return self._reject(dev, "REJECTED", "timestamp in the future", transport, penalize=True)
        if ts < now - self.config.stale_accept_s:
            return self._reject(dev, "REJECTED", "timestamp too old", transport)
        stale = ts < now - self.config.telemetry_freshness_s
        # 6. replay / duplicate
        with self._lock:
            seen = self._seen.setdefault(dev, OrderedDict())
            prev_sig = seen.get(seq)
            oldest_allowed = (ident.last_seq or 0) - self.config.replay_window
        if prev_sig is not None:
            if prev_sig == envelope.get("sig"):
                with self._lock:
                    self.stats["DUPLICATE"] += 1
                self.audit.record("duplicate_telemetry", dev, "telemetry", result="dropped",
                                  reason="sequence already accepted (duplicate delivery)", seq=seq, transport=transport)
                return {"status": "DUPLICATE", "device_id": dev, "reason": "duplicate", "accepted": 0, "results": []}
            with self._lock:
                self.stats["replay"] += 1
            return self._reject(dev, "REJECTED", "replayed sequence number with different content", transport,
                                audit_type="replay_detected", penalize=True)
        if ident.last_seq is not None and seq < oldest_allowed:
            with self._lock:
                self.stats["replay"] += 1
            return self._reject(dev, "REJECTED", "sequence number outside the replay window", transport,
                                audit_type="replay_detected", penalize=True)
        # 7. readings
        results, accepted = [], 0
        for r in readings:
            metric = r.get("metric")
            if metric not in ident.metrics:
                results.append({"metric": metric, "status": "REJECTED", "reason": "metric not permitted for this device"})
                self._reject(dev, "REJECTED", "metric not permitted for this device", transport, metric=metric)
                continue
            sensor = dev if len(ident.metrics) == 1 else f"{dev}:{metric}"
            rec = self.store.ingest({"metric": metric, "value": r.get("value"), "zone_id": ident.zone_id,
                                     "building_id": ident.building_id, "floor_id": ident.floor_id, "t_wall": float(ts),
                                     "seq": seq, "sim_t": r.get("sim_t"), "origin": ident.origin},
                                    ident.source, sensor, dev)
            if rec["quality"] == "invalid":
                results.append({"metric": metric, "status": "REJECTED", "reason": "value failed validation "
                                "(recorded as an invalid data-quality reading)", "stored": "invalid"})
                with self._lock:
                    self.stats["REJECTED"] += 1
                    self.rejections.append({"received_at": now, "device_id": dev, "metric": metric, "status": "REJECTED",
                                            "reason": "; ".join(rec.get("problems") or ["invalid value"]), "transport": transport})
                continue
            status = "STALE" if stale else "ACCEPTED"
            results.append({"metric": metric, "status": status, "reading": rec})
            accepted += 1
            with self._lock:
                self.stats[status] += 1
        with self._lock:
            seen[seq] = envelope.get("sig")
            while len(seen) > self.config.replay_window:
                seen.popitem(last=False)
        first_contact = ident.last_seen is None
        ident.last_seen = now
        ident.last_seq = seq if ident.last_seq is None else max(ident.last_seq, seq)
        ident.accepted += accepted
        ident.rejected += len(readings) - accepted
        if first_contact:
            self.audit.record("device_connected", dev, "first authenticated telemetry", result="success", transport=transport)
        overall = ("ACCEPTED" if accepted == len(readings) and not stale else "STALE" if accepted and stale
                   else "REJECTED" if not accepted else "ACCEPTED")
        return {"status": overall, "device_id": dev, "accepted": accepted, "results": results,
                "trusted_source": ident.source, "origin": ident.origin}

    def _auth_fail(self, dev, transport, reason) -> dict:
        with self._lock:
            self.stats["auth_failed"] += 1
        out = self._reject(dev, "REJECTED", "device authentication failed", transport, audit_type="auth_failure",
                           penalize=True)
        self.audit.record("auth_failure", dev, "device authentication", result="rejected", reason=reason, transport=transport)
        return out

    def metrics(self) -> dict:
        with self._lock:
            return {**self.stats, "unknown_device_attempts": len(self.unknown_attempts),
                    "recent_rejections": len(self.rejections)}
