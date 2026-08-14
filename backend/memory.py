"""Comfort memory (stretch, IMPLEMENTATION.md §7): learn recurring per-zone
complaint patterns and pre-apply a gentle constraint BEFORE the complaint.

Pattern = same (zone, issue) reported on >= PATTERN_DAYS distinct sim-days at a
similar hour (+/- TOLERANCE_H). Thirty minutes before the pattern's usual hour,
a low-severity, low-confidence constraint is applied and announced — "Room B
usually runs stuffy at 2 pm — pre-ventilating." Only real occupant complaints
count as evidence; the memory never learns from its own pre-applies.
"""
from __future__ import annotations

from backend.constraints import Constraint, ConstraintStore

AUTHOR = "comfort-memory"
PATTERN_DAYS = 2        # distinct days needed before we trust a pattern
TOLERANCE_H = 1.0       # hour-of-day clustering tolerance
LEAD_S = 1800.0         # pre-apply this long before the usual complaint time
PRE_SEVERITY = 1
PRE_CONFIDENCE = 0.5


class ComfortMemory:
    def __init__(self):
        self._done: set = set()      # (day, zone, issue) already pre-applied

    def patterns(self, store: ConstraintStore) -> list[dict]:
        """Mine occupant complaints for recurring (zone, issue, hour) habits."""
        groups: dict = {}
        for c in store.items:
            if c.author == AUTHOR:
                continue
            h = (c.created_t % 86400.0) / 3600.0
            groups.setdefault((c.zone, c.issue), []).append((int(c.created_t // 86400.0), h))
        out = []
        for (zone, issue), evts in groups.items():
            evts.sort(key=lambda e: e[1])
            # Greedy hour-clustering: complaints within TOLERANCE_H of the
            # cluster mean belong together.
            clusters: list[list] = []
            for day, h in evts:
                for cl in clusters:
                    mean = sum(x[1] for x in cl) / len(cl)
                    if abs(h - mean) <= TOLERANCE_H:
                        cl.append((day, h))
                        break
                else:
                    clusters.append([(day, h)])
            for cl in clusters:
                days = {d for d, _ in cl}
                if len(days) >= PATTERN_DAYS:
                    out.append({"zone": zone, "issue": issue,
                                "hour": sum(h for _, h in cl) / len(cl),
                                "n_days": len(days)})
        return out

    def tick(self, twin, store: ConstraintStore) -> list[dict]:
        """Call every sim step. Pre-applies due patterns; returns feed entries."""
        announcements = []
        for p in self.patterns(store):
            due_t = twin.day * 86400.0 + p["hour"] * 3600.0 - LEAD_S
            key = (twin.day, p["zone"], p["issue"])
            if key in self._done or not (due_t <= twin.t < due_t + 3600.0):
                continue
            self._done.add(key)
            # Skip if occupants already re-reported it today.
            if any(c.issue == p["issue"] for c in store.active(twin.t, p["zone"])):
                continue
            store.add(Constraint.from_issue(
                p["zone"], p["issue"], PRE_SEVERITY, PRE_CONFIDENCE, twin.t,
                text=f"auto: recurring {p['issue']} pattern ({p['n_days']} days)",
                author=AUTHOR))
            hh, mm = int(p["hour"]), int((p["hour"] % 1) * 60)
            announcements.append({
                "author": AUTHOR,
                "text": f"{p['zone']} usually reports {p['issue'].replace('_', ' ')} "
                        f"around {hh:02d}:{mm:02d} — pre-applying a gentle fix.",
                "source": "memory", "latency_ms": 0,
                "action": "pre-applied",
                "parsed": {"is_comfort_complaint": True, "zone_id": p["zone"],
                           "issue": p["issue"], "severity": PRE_SEVERITY,
                           "confidence": PRE_CONFIDENCE,
                           "reasoning": f"Learned pattern over {p['n_days']} days."},
            })
        return announcements
