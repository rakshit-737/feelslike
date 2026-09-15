"""Structured security / audit log.

In memory (bounded) and, when FL_AUDIT_LOG is set, appended as JSON lines to that file (keep it
outside the repository or in the git-ignored data/security/). Every event passes through
redact(): any field whose NAME looks like a credential is replaced and any value that looks like
a bearer token / key is masked. Passwords, tokens, keys and signatures are never written.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from pathlib import Path

SENSITIVE_KEY = re.compile(r"(pass|token|secret|key|sig|signature|authorization|credential|cookie|hash)", re.I)
TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-]{32,}")
EVENT_TYPES = ("auth_success", "auth_failure", "unauthenticated_request", "logout", "authorization_failure", "rate_limited",
               "device_registered", "device_status_changed", "device_key_rotated", "device_connected",
               "device_disconnected", "telemetry_rejected", "replay_detected", "duplicate_telemetry",
               "unknown_device", "spoof_attempt", "device_quarantined", "control_command",
               "configuration_change", "role_change", "user_created", "security_configuration")
SECURITY_SEVERITY = {"auth_failure": "warning", "authorization_failure": "warning", "rate_limited": "warning",
                     "replay_detected": "high", "spoof_attempt": "high", "unknown_device": "warning",
                     "device_quarantined": "high", "telemetry_rejected": "info"}


def redact(obj, depth=0):
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if SENSITIVE_KEY.search(str(k)) else redact(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, depth + 1) for v in obj][:50]
    if isinstance(obj, str):
        s = obj[:300]
        return TOKEN_LIKE.sub("[MASKED]", s)
    return obj


class AuditLog:
    def __init__(self, maxlen: int = 5000, path: str | None = None, enabled: bool = True, clock=time.time):
        self._lock = threading.Lock()
        self.events: deque = deque(maxlen=maxlen)
        self.path = Path(path) if path else None
        self.enabled = enabled
        self._now = clock
        self._seq = 0
        self.counts: dict = {}

    def record(self, event_type: str, actor: str | None, action: str, target: str | None = None,
               result: str = "success", reason: str | None = None, zone: str | None = None,
               request_id: str | None = None, **extra) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            ev = {"id": f"aud-{self._seq:06d}", "timestamp": self._now(), "event_type": event_type,
                  "severity": SECURITY_SEVERITY.get(event_type, "info"), "actor": redact(actor),
                  "action": redact(action), "target": redact(target), "zone": zone, "result": result,
                  "reason": redact(reason), "request_id": request_id, "details": redact(extra)}
            self.events.append(ev)
            self.counts[event_type] = self.counts.get(event_type, 0) + 1
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(ev, default=str) + "\n")
                except OSError:
                    pass                                   # never let audit I/O break a request
        return ev

    def list(self, event_type: str | None = None, actor: str | None = None, result: str | None = None,
             since: float | None = None, limit: int = 200) -> list:
        with self._lock:
            evs = list(self.events)
        out = [e for e in reversed(evs) if (event_type is None or e["event_type"] == event_type)
               and (actor is None or e["actor"] == actor) and (result is None or e["result"] == result)
               and (since is None or e["timestamp"] >= since)]
        return out[:limit]

    def count_since(self, event_type: str, seconds: float) -> int:
        t0 = self._now() - seconds
        with self._lock:
            return sum(1 for e in self.events if e["event_type"] == event_type and e["timestamp"] >= t0)
