"""Constraint store: complaints become decaying control constraints, with
conflict arbitration and human-readable explanations.

A complaint's influence decays exponentially (half-life 45 min) and expires
after 2 h unless re-reported — recency-weighted democracy, not a ticket queue.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

HALF_LIFE_S = 45 * 60.0
EXPIRY_S = 2 * 3600.0

# issue -> (setpoint offset per severity level {1,2,3}, vent delta)
ISSUE_EFFECTS = {
    "too_hot":  ({1: -0.8, 2: -1.3, 3: -1.8}, 0),
    "too_cold": ({1: +0.8, 2: +1.3, 3: +1.8}, 0),
    "stuffy":   ({1: -0.3, 2: -0.4, 3: -0.5}, +1),
    "humid":    ({1: -0.4, 2: -0.5, 3: -0.6}, +1),
    "drafty":   ({1: +0.3, 2: +0.4, 3: +0.5}, -1),
}

_ids = itertools.count(1)


@dataclass
class Constraint:
    id: int
    zone: str
    issue: str
    severity: int
    confidence: float
    created_t: float          # sim-seconds timestamp
    raw_offset: float
    vent_delta: int
    text: str = ""
    author: str = "anonymous"

    @classmethod
    def from_issue(cls, zone, issue, severity, confidence, now_t, text="", author="anonymous"):
        offs, vent = ISSUE_EFFECTS.get(issue, ({1: 0.0, 2: 0.0, 3: 0.0}, 0))
        sev = int(min(max(severity, 1), 3))
        return cls(next(_ids), zone, issue, sev, float(confidence), now_t,
                   offs[sev], vent, text, author)

    def decay(self, now_t: float) -> float:
        age = max(0.0, now_t - self.created_t)
        return 0.0 if age > EXPIRY_S else 0.5 ** (age / HALF_LIFE_S)

    def weight(self, now_t: float) -> float:
        return self.severity * self.confidence * self.decay(now_t)


class ConstraintStore:
    def __init__(self):
        self.items: list[Constraint] = []

    def add(self, c: Constraint) -> dict:
        self.items.append(c)
        return self.explain(c.zone, c.created_t)

    def active(self, now_t: float, zone: str | None = None):
        return [c for c in self.items
                if c.decay(now_t) > 0.02 and (zone is None or c.zone == zone)]

    def clear_zone(self, zone: str, now_t: float) -> int:
        """All-clear from an occupant: expire the zone's active constraints now.
        Items stay in history (comfort-memory mines them) but stop influencing
        control. Returns how many were cleared."""
        cleared = 0
        for c in self.items:
            if c.zone == zone and c.decay(now_t) > 0.02:
                c.created_t = now_t - EXPIRY_S - 1.0
                cleared += 1
        return cleared

    def zone_adjustments(self, now_t: float) -> dict:
        """zone -> {setpoint_offset, vent_delta, n}. Opposing constraints are
        arbitrated by weighted mean (weight = severity x confidence x decay)."""
        out = {}
        by_zone: dict[str, list[Constraint]] = {}
        for c in self.active(now_t):
            by_zone.setdefault(c.zone, []).append(c)
        for zone, cs in by_zone.items():
            wsum = sum(c.weight(now_t) for c in cs)
            if wsum <= 0:
                continue
            offset = sum(c.raw_offset * c.weight(now_t) for c in cs) / wsum
            vent = sum(c.vent_delta for c in cs if c.weight(now_t) > 0.25)
            out[zone] = {"setpoint_offset": round(offset, 2),
                         "vent_delta": max(-1, min(1, vent)), "n": len(cs)}
        return out

    def explain(self, zone: str, now_t: float) -> dict:
        """Human-readable arbitration summary for one zone."""
        cs = self.active(now_t, zone)
        adj = self.zone_adjustments(now_t).get(zone)
        conflict = len({1 if c.raw_offset > 0 else -1 for c in cs if c.raw_offset != 0}) > 1
        parts = [f"{c.issue.replace('_', ' ')} (sev {c.severity}, "
                 f"{int((now_t - c.created_t) / 60)}m ago)" for c in cs]
        if not adj:
            text = "No active constraints."
        elif conflict:
            text = (f"CONFLICT in zone: {' vs '.join(parts)} -> severity-weighted "
                    f"compromise: {adj['setpoint_offset']:+.1f} degC")
        else:
            text = (f"{len(cs)} active request(s): {', '.join(parts)} -> "
                    f"setpoint {adj['setpoint_offset']:+.1f} degC"
                    + (f", airflow {adj['vent_delta']:+d}" if adj["vent_delta"] else ""))
        return {"zone": zone, "conflict": conflict, "adjustment": adj, "summary": text}

    def unmet_pressure(self, twin, minutes: float) -> float:
        """RL reward term: active complaints whose zone still feels wrong."""
        p = 0.0
        for c in self.active(twin.t):
            T = twin.T.get(c.zone, 25.0)
            if (c.issue == "too_hot" and T > 25.5) or (c.issue == "too_cold" and T < 23.5):
                p += c.weight(twin.t) * minutes / 60.0
        return p
